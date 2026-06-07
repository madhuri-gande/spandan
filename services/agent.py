"""Spandan autonomous agent.

The agent loops over upcoming patient bridges, ranks donors via the
ML model, generates a multilingual outreach message via Bedrock,
"sends" it (writes to DynamoDB messages table), waits for a reply
(via DynamoDB polling), classifies the reply intent, and either
confirms a donation or escalates to the next donor.

Entry points:
    run_agent_cycle()         - run a single end-to-end cycle (one bridge)
    start_background_agent()  - start a thread that runs cycles every N seconds
    stop_background_agent()
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from boto3.dynamodb.conditions import Key

from services import bedrock_chat, db, forecasting, ranking

logger = logging.getLogger("spandan.agent")
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%H:%M:%S")

REPLY_TIMEOUT_SECONDS = int(os.getenv("AGENT_REPLY_TIMEOUT", "60"))
REPLY_POLL_INTERVAL = 2
MAX_DONORS_PER_BRIDGE = int(os.getenv("AGENT_MAX_DONORS", "5"))
CYCLE_INTERVAL_SECONDS = int(os.getenv("AGENT_CYCLE_INTERVAL", "60"))
COOLDOWN_HOURS = int(os.getenv("AGENT_COOLDOWN_HOURS", "24"))

# How long to wait for a donor to reply before moving to the next-ranked
# donor (sequential pacing for NORMAL / BACKUP modes). In production this
# is one hour; demos can shorten it via the UI dropdown.
_donor_wait_seconds = int(os.getenv("DONOR_WAIT_SECONDS", "3600"))


def get_donor_wait_seconds() -> int:
    return _donor_wait_seconds


def set_donor_wait_seconds(seconds: int) -> None:
    global _donor_wait_seconds
    _donor_wait_seconds = max(10, int(seconds))


_active_cycle_state: dict = {
    "active": False, "status": "idle",
    "donor_name": None, "donor_id": None,
    "patient": None, "language": None,
    "started": None, "elapsed_seconds": 0,
    "remaining_seconds": 0, "wait_total_seconds": 0,
}

# Serialise agent cycles so the background thread, "Advance all patients"
# button and Streamlit re-runs can never run two cycles in parallel and
# double-email the same donor. Use a non-blocking acquire — overlapping
# requests just no-op.
_cycle_lock = threading.Lock()

# In-memory dedup of the last few outbound emails. Key is
# f"{donor_id}|{bridge_id}", value is the unix-time we sent it. We refuse
# to send the same (donor, bridge, outreach) pair twice within
# DEDUP_WINDOW_SECONDS regardless of what DDB scans see (eventual
# consistency). Cheap, race-proof, and resets when the process restarts.
_recent_email_lock = threading.Lock()
_recent_emails: dict[str, float] = {}
DEDUP_WINDOW_SECONDS = 90


def _claim_email_slot(donor_id: str, bridge_id: str) -> bool:
    """Atomic check-and-claim: returns True if this (donor, bridge) is
    free to email (and the slot is now reserved), False if another
    thread/cycle reserved it within DEDUP_WINDOW_SECONDS.

    The atomic version (single critical section over the read AND the
    write) is essential — without it, two threads could both pass an
    independent _was_recently_emailed check before either recorded,
    and both end up emailing. That's the "same donor got 4 emails"
    bug surfaced by the user's surge click racing with the autonomous
    cycle.
    """
    if not donor_id:
        return True  # nothing to dedup against
    key = f"{donor_id}|{bridge_id or ''}"
    now = time.time()
    with _recent_email_lock:
        last = _recent_emails.get(key, 0.0)
        if (now - last) < DEDUP_WINDOW_SECONDS:
            return False
        _recent_emails[key] = now
        if len(_recent_emails) > 500:
            cutoff = now - DEDUP_WINDOW_SECONDS * 4
            for k in [k for k, v in _recent_emails.items() if v < cutoff]:
                _recent_emails.pop(k, None)
    return True


def _was_recently_emailed(donor_id: str, bridge_id: str) -> bool:
    """Read-only check (no claim). Kept for diagnostics / non-critical
    paths; the hot path uses _claim_email_slot for atomicity."""
    if not donor_id:
        return False
    key = f"{donor_id}|{bridge_id or ''}"
    with _recent_email_lock:
        last = _recent_emails.get(key, 0.0)
    return (time.time() - last) < DEDUP_WINDOW_SECONDS


def _record_email_sent(donor_id: str, bridge_id: str) -> None:
    """Legacy helper for paths that don't want to claim atomically.
    Prefer _claim_email_slot which is race-safe."""
    if not donor_id:
        return
    key = f"{donor_id}|{bridge_id or ''}"
    with _recent_email_lock:
        _recent_emails[key] = time.time()
        if len(_recent_emails) > 500:
            cutoff = time.time() - DEDUP_WINDOW_SECONDS * 4
            for k in [k for k, v in _recent_emails.items() if v < cutoff]:
                _recent_emails.pop(k, None)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _agent_log(cycle_id: str, action: str, **fields) -> None:
    item = {
        "cycle_id": cycle_id,
        "ts": _now_iso(),
        "action": action,
        **{k: v for k, v in fields.items() if v is not None},
    }
    db.batch_write("agent_log", [item])
    logger.info(f"[{cycle_id[-6:]}] {action}  {fields}")


def _recently_contacted_donors(hours: int = COOLDOWN_HOURS) -> set[str]:
    """Return donor_ids who have received an outbound agent message within
    the last `hours` window. Used to enforce a per-donor cooldown so the
    agent never re-spams the same person."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    table = db.get_table("messages")
    contacted: set[str] = set()
    try:
        scan_kwargs = {
            "FilterExpression": "direction = :d AND ts >= :c",
            "ExpressionAttributeValues": {":d": "outbound", ":c": cutoff},
            "ProjectionExpression": "donor_id",
        }
        while True:
            resp = table.scan(**scan_kwargs)
            for item in resp.get("Items", []):
                did = item.get("donor_id")
                if did:
                    contacted.add(did)
            if "LastEvaluatedKey" not in resp:
                break
            scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    except Exception as exc:
        logger.warning(f"cooldown scan failed: {exc}")
    return contacted


def _recently_confirmed_donors(days: int = 7) -> set[str]:
    """Return donor_ids who already have an active confirmed donation in
    the last `days` window. We don't want to spam a donor who already
    said YES for *any* patient by asking them about a different patient
    while their previous donation hasn't even happened yet.

    This is independent of the email-cooldown filter: the email cooldown
    is short (hours), this is a longer hold (~one donation cycle).
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    table = db.get_table("donations")
    confirmed: set[str] = set()
    try:
        scan_kwargs = {
            "FilterExpression": "#s = :c AND confirmed_at >= :t",
            "ExpressionAttributeValues": {":c": "confirmed", ":t": cutoff},
            "ExpressionAttributeNames": {"#s": "status"},
            "ProjectionExpression": "donor_id",
        }
        while True:
            resp = table.scan(**scan_kwargs)
            for item in resp.get("Items", []):
                did = item.get("donor_id")
                if did:
                    confirmed.add(did)
            if "LastEvaluatedKey" not in resp:
                break
            scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    except Exception as exc:
        logger.warning(f"recently-confirmed scan failed: {exc}")
    return confirmed


def _load_donor_pool(bridge: dict) -> list[dict]:
    """Resolve a bridge's donor_pool into full donor records.

    If bridge has explicit donor IDs, fetch those. Otherwise, fall back to
    a blood-group-compatible pool from the donors table.
    """
    table = db.get_table("donors")
    explicit = bridge.get("donor_pool") or []
    donors: list[dict] = []
    if explicit:
        for uid in explicit[:200]:
            try:
                resp = table.get_item(Key={"user_id": uid})
                if "Item" in resp:
                    donors.append(resp["Item"])
            except Exception as exc:
                logger.warning(f"failed to load donor {uid[:12]}: {exc}")

    if len(donors) >= 5:
        return donors

    bg = bridge.get("blood_group")
    if not bg:
        return donors

    try:
        idx_resp = table.query(
            IndexName="blood_group-index",
            KeyConditionExpression=Key("blood_group").eq(bg),
            Limit=200,
        )
        donors.extend(idx_resp.get("Items", []))
    except Exception as exc:
        logger.warning(f"GSI query failed: {exc}")

    seen = set()
    unique = []
    for d in donors:
        uid = d.get("user_id")
        if uid and uid not in seen:
            seen.add(uid)
            unique.append(d)
    return unique


def _send_message(donor: dict, bridge: dict, body: str, language: str,
                  msg_type: str = "outreach") -> Optional[dict]:
    """Write an outbound message and dispatch it via the delivery layer.

    Refuses to send a *fresh* outreach for the same (donor, bridge) pair
    within DEDUP_WINDOW_SECONDS — guards against concurrent cycles racing
    each other, and against the delivery worker double-sending while
    DynamoDB is propagating the new row.

    Reminders, QA-answers and "already covered" messages are *not*
    deduped — they're rare and valid follow-ups.
    """
    donor_id = donor.get("user_id")
    bridge_id = bridge.get("bridge_id", "") or ""

    if msg_type == "outreach" and donor_id:
        # Atomic check-and-claim. If another thread already claimed this
        # slot (e.g. user clicked surge while the autonomous cycle was
        # mid-loop on the same bridge), return None and skip the send.
        if not _claim_email_slot(donor_id, bridge_id):
            logger.info(
                f"skipping duplicate outreach to {donor.get('name','?')} "
                f"for bridge {bridge_id[:14]} (sent <{DEDUP_WINDOW_SECONDS}s ago)"
            )
            return None

    msg = {
        "donor_id": donor_id,
        "ts": _now_iso(),
        "bridge_id": bridge_id,
        "message": body,
        "language": language,
        "direction": "outbound",
        "status": "sent",
        "type": msg_type,
        "donor_name": donor.get("name"),
        # Claim a slot in the delivery pipeline up-front. The worker scans
        # for `delivery_status not exists`; setting it to "pending" here
        # means the inline delivery call below is the *only* path that
        # will actually email this row, even if the worker scan races.
        "delivery_status": "pending",
    }
    db.batch_write("messages", [msg])

    # Fire-and-forget delivery (email via MailPit / SES). Failures are
    # logged but never block the agent loop. The delivery worker only
    # picks up rows that have NO delivery_status, so this inline path is
    # authoritative for every freshly-written message.
    try:
        from services import delivery
        delivery.deliver_message(msg)
    except Exception as exc:
        logger.warning(f"inline delivery failed: {exc}")

    return msg


def _wait_for_reply(donor_id: str, sent_after_iso: str, timeout: int) -> Optional[dict]:
    """Legacy blocking poll, kept for any path that explicitly wants it.

    The state-machine cycle (`_step_bridge`) does NOT use this — it relies
    on the cycle interval itself to provide the wait window so the agent
    thread is never tied up.
    """
    table = db.get_table("messages")
    start = time.time()
    while time.time() - start < timeout:
        resp = table.query(
            KeyConditionExpression=Key("donor_id").eq(donor_id) & Key("ts").gt(sent_after_iso),
            ScanIndexForward=False,
            Limit=5,
        )
        for item in resp.get("Items", []):
            if item.get("direction") == "inbound":
                return item
        time.sleep(REPLY_POLL_INTERVAL)
    return None


def _outbound_for_bridge(bridge_id: str) -> list[tuple[str, str]]:
    """Outreach emails sent for this bridge as [(donor_id, ts), ...] newest first.

    Only counts the original outreach (type='outreach' or unset), not the
    polite "thanks already covered", reminders, or QA answers. Used by the
    sequential state machine to know which donors have already been asked.
    """
    if not bridge_id:
        return []
    table = db.get_table("messages")
    try:
        resp = table.scan(
            FilterExpression="bridge_id = :b AND direction = :d",
            ExpressionAttributeValues={":b": bridge_id, ":d": "outbound"},
            Limit=500,
        )
    except Exception:
        return []
    items = resp.get("Items", [])
    items = [i for i in items if (i.get("type") or "outreach") == "outreach"]
    items.sort(key=lambda i: i.get("ts", ""), reverse=True)
    return [(i.get("donor_id"), i.get("ts")) for i in items if i.get("donor_id")]


def _inbound_after(donor_id: str, after_ts: str) -> Optional[dict]:
    """Most recent inbound message from this donor strictly after `after_ts`.

    Used by the state machine to check whether the most-recently-emailed
    donor has replied since we sent them the request.
    """
    if not donor_id or not after_ts:
        return None
    table = db.get_table("messages")
    try:
        resp = table.query(
            KeyConditionExpression=Key("donor_id").eq(donor_id) & Key("ts").gt(after_ts),
            ScanIndexForward=False,
            Limit=10,
        )
    except Exception:
        return None
    for it in resp.get("Items", []):
        if it.get("direction") == "inbound":
            return it
    return None


def _seconds_since(iso_ts: str) -> float:
    """Seconds elapsed since the given ISO timestamp (UTC). Returns 0 on parse error."""
    if not iso_ts:
        return 0.0
    try:
        ts = datetime.fromisoformat(str(iso_ts).replace("Z", "+00:00"))
        return max(0.0, (datetime.now(timezone.utc) - ts).total_seconds())
    except Exception:
        return 0.0


def _get_donor(donor_id: str) -> Optional[dict]:
    if not donor_id:
        return None
    try:
        return db.get_table("donors").get_item(Key={"user_id": donor_id}).get("Item")
    except Exception:
        return None


def get_active_cycle() -> dict:
    return dict(_active_cycle_state)


def simulate_donor_reply(donor_id: str, bridge_id: str, donor_name: str,
                         language: str, text: str) -> dict:
    """Write an inbound message exactly like a real WhatsApp webhook would.

    This is the one-click replacement for opening Donor View, picking a
    donor, and typing a reply. Useful for fast demos.
    """
    msg = {
        "donor_id": donor_id,
        "ts": _now_iso(),
        "bridge_id": bridge_id or "",
        "message": text,
        "language": language or "english",
        "direction": "inbound",
        "status": "received",
        "donor_name": donor_name,
    }
    db.batch_write("messages", [msg])
    return msg


def recent_messages(limit: int = 30) -> list[dict]:
    """Most recent messages across all donors, both directions."""
    table = db.get_table("messages")
    items: list[dict] = []
    response = table.scan(Limit=300)
    items.extend(response.get("Items", []))
    items.sort(key=lambda x: x.get("ts", ""), reverse=True)
    return items[:limit]


def process_pending_replies(max_replies: int = 10) -> list[dict]:
    """Scan recent inbound messages whose intent has not been classified by
    the agent (no `reply_received` log row referencing this message), then
    classify and act on them.

    This makes the demo resilient when a donor replies AFTER the original
    cycle has already timed out and moved on.

    Returns a list of result dicts with what was processed.
    """
    msgs = recent_messages(limit=80)
    inbound = [m for m in msgs if m.get("direction") == "inbound"]
    inbound.sort(key=lambda m: m.get("ts", ""))  # oldest first

    log_table = db.get_table("agent_log")
    seen_inbound_ts: set[str] = set()
    response = log_table.scan(Limit=500)
    for it in response.get("Items", []):
        if it.get("action") in ("reply_received", "reply_received_followup", "delayed_reply_processed"):
            ts_field = it.get("processed_inbound_ts") or ""
            if ts_field:
                seen_inbound_ts.add(ts_field)

    processed: list[dict] = []
    for m in inbound:
        if len(processed) >= max_replies:
            break
        if m.get("ts") in seen_inbound_ts:
            continue

        donor_id = m.get("donor_id")
        bridge_id = m.get("bridge_id")
        text = m.get("message", "")
        intent = bedrock_chat.classify_intent(text)

        cycle_id = _new_id("cyc")
        _agent_log(
            cycle_id, "delayed_reply_processed",
            donor_id=str(donor_id)[:14], donor_name=m.get("donor_name"),
            intent=intent, processed_inbound_ts=m.get("ts"),
            reply_preview=text[:80],
        )

        if intent == "YES" and bridge_id:
            try:
                bridge = db.get_table("bridges").get_item(Key={"bridge_id": bridge_id}).get("Item")
                donor = db.get_table("donors").get_item(Key={"user_id": donor_id}).get("Item")
                if bridge and donor:
                    # Over-confirmation guard: if this bridge already has
                    # `confirmation_target` confirmed donations, don't create
                    # another one. Instead send a gracious "we have enough"
                    # message so the donor doesn't show up uninvited.
                    target = _confirmation_target(bridge)
                    already = _confirmed_count(bridge_id)
                    if already >= target:
                        decline = bedrock_chat.thanks_already_covered(donor, bridge)
                        _send_message(donor, bridge, decline,
                                      donor.get("preferred_language", "english"),
                                      msg_type="already_covered")
                        _agent_log(
                            cycle_id, "thanks_already_covered",
                            donor_id=str(donor_id)[:14], donor_name=donor.get("name"),
                            bridge_id=bridge_id, already=already, target=target,
                        )
                        processed.append({"intent": "YES", "donor_id": donor_id,
                                          "donor_name": donor.get("name"),
                                          "bridge_id": bridge_id, "already_covered": True})
                        continue

                    donation = _confirm_donation(donor, bridge)
                    _agent_log(
                        cycle_id, "confirmed",
                        donor_id=str(donor_id)[:14], donor_name=donor.get("name"),
                        bridge_id=bridge_id, donation_id=donation["donation_id"],
                        source="delayed_reply",
                    )
                    processed.append({"intent": "YES", "donor_id": donor_id, "donor_name": donor.get("name"),
                                       "bridge_id": bridge_id, "donation_id": donation["donation_id"]})
                    continue
            except Exception as exc:
                logger.exception(f"could not confirm donation for delayed reply: {exc}")

        if intent == "CANCEL" and bridge_id:
            try:
                cancelled = _cancel_donation(donor_id, bridge_id, reason=text[:120])
                _agent_log(
                    cycle_id, "donation_cancelled",
                    donor_id=str(donor_id)[:14], donor_name=m.get("donor_name"),
                    bridge_id=bridge_id, cancelled=cancelled,
                    reason=text[:120],
                )
                # Auto re-enqueue: run a fresh cycle for the same patient
                # to pick the next-ranked donor (cooldown will skip the
                # one who just cancelled).
                followup = run_agent_cycle(target_bridge_id=bridge_id)
                _agent_log(
                    cycle_id, "auto_reenqueued",
                    bridge_id=bridge_id,
                    followup_result=str(followup.get("result", "?")),
                    followup_donor=followup.get("donor_name"),
                )
                processed.append({"intent": "CANCEL", "donor_id": donor_id,
                                  "donor_name": m.get("donor_name"),
                                  "bridge_id": bridge_id,
                                  "followup_result": followup.get("result"),
                                  "followup_donor": followup.get("donor_name")})
                continue
            except Exception as exc:
                logger.exception(f"could not cancel + re-enqueue: {exc}")

        if intent == "QUESTION" and bridge_id:
            # Answer the question via Bedrock and send the reply email.
            try:
                bridge = db.get_table("bridges").get_item(Key={"bridge_id": bridge_id}).get("Item")
                donor = db.get_table("donors").get_item(Key={"user_id": donor_id}).get("Item")
                if bridge and donor:
                    answer = bedrock_chat.answer_donor_question(donor, text, bridge)
                    sent = _send_message(donor, bridge, answer,
                                         donor.get("preferred_language", "english"),
                                         msg_type="qa_answer")
                    _agent_log(
                        cycle_id, "question_answered",
                        donor_id=str(donor_id)[:14], donor_name=donor.get("name"),
                        bridge_id=bridge_id, answer_preview=answer[:80],
                    )
                    processed.append({"intent": "QUESTION", "donor_id": donor_id,
                                      "donor_name": donor.get("name"),
                                      "bridge_id": bridge_id, "answered": True})
                    continue
            except Exception as exc:
                logger.exception(f"could not answer question: {exc}")

        processed.append({"intent": intent, "donor_id": donor_id,
                          "donor_name": m.get("donor_name"), "bridge_id": bridge_id})

    return processed


def _confirmation_target(bridge: dict) -> int:
    """How many confirmed donors does this bridge need?"""
    try:
        return int(bridge.get("confirmation_target") or
                   os.getenv("DEFAULT_CONFIRMATION_TARGET", "1"))
    except (TypeError, ValueError):
        return 1


def _confirmed_count(bridge_id: str) -> int:
    """How many ACTIVE (non-cancelled) confirmed donations does this bridge
    already have? Used to enforce confirmation_target across both the
    in-cycle path and the delayed-reply path."""
    if not bridge_id:
        return 0
    try:
        resp = db.get_table("donations").scan(
            FilterExpression="bridge_id = :b AND #s = :c",
            ExpressionAttributeValues={":b": bridge_id, ":c": "confirmed"},
            ExpressionAttributeNames={"#s": "status"},
            Limit=200,
        )
        return len(resp.get("Items", []))
    except Exception:
        return 0


def _cancel_donation(donor_id: str, bridge_id: str, reason: str = "") -> int:
    """Mark any pending/confirmed donation for this donor+bridge as cancelled.

    Returns number of rows updated.
    """
    table = db.get_table("donations")
    resp = table.scan(
        FilterExpression="donor_id = :d AND bridge_id = :b",
        ExpressionAttributeValues={":d": donor_id, ":b": bridge_id},
    )
    cancelled = 0
    for item in resp.get("Items", []):
        if item.get("status") in ("cancelled", "no_show", "completed"):
            continue
        try:
            table.update_item(
                Key={"donation_id": item["donation_id"]},
                UpdateExpression="SET #s = :s, cancelled_at = :ts, cancel_reason = :r",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":s": "cancelled", ":ts": _now_iso(), ":r": reason,
                },
            )
            cancelled += 1
        except Exception as exc:
            logger.warning(f"could not cancel donation {item.get('donation_id')}: {exc}")
    # Reset bridge timeline so the patient is back in the urgent pipeline.
    try:
        db.get_table("bridges").update_item(
            Key={"bridge_id": bridge_id},
            UpdateExpression="REMOVE last_confirmed_donor, last_confirmed_at",
        )
    except Exception:
        pass
    return cancelled


def _auto_send_due_reminders() -> dict:
    """Autonomous post-confirmation reminder.

    For every confirmed donation that:
      - has not yet received a reminder, AND
      - was confirmed BETWEEN [now - REMINDER_MAX_AGE_HOURS, now -
        REMINDER_DELAY_SECONDS]
    send a brief Bedrock-generated "see you soon" note in the donor's
    language and mark `reminder_sent = True`.

    The upper-age cap stops the agent from suddenly emailing reminders for
    confirmations that happened in PREVIOUS demo sessions when the user
    restarts the stack. Old un-reminded donations are quietly marked as
    reminded (without sending a stale message) so they don't keep
    re-appearing in this scan.
    """
    delay_seconds = int(os.getenv("REMINDER_DELAY_SECONDS", "180"))
    max_age_hours = int(os.getenv("REMINDER_MAX_AGE_HOURS", "1"))
    now = datetime.now(timezone.utc)
    delay_cutoff_iso = (now - timedelta(seconds=delay_seconds)).isoformat()
    age_cutoff_iso = (now - timedelta(hours=max_age_hours)).isoformat()

    don_table = db.get_table("donations")
    try:
        resp = don_table.scan(
            FilterExpression="#s = :c AND attribute_not_exists(reminder_sent)",
            ExpressionAttributeValues={":c": "confirmed"},
            ExpressionAttributeNames={"#s": "status"},
        )
    except Exception as exc:
        logger.warning(f"auto-reminder scan failed: {exc}")
        return {"reminders_sent": 0, "candidates_skipped": 0}

    items = resp.get("Items", [])
    sent = 0
    skipped = 0
    for d in items:
        confirmed_at = str(d.get("confirmed_at") or "")
        if not confirmed_at:
            skipped += 1
            continue
        if confirmed_at >= delay_cutoff_iso:
            # not aged enough yet; will pick up in a future cycle
            skipped += 1
            continue
        if confirmed_at < age_cutoff_iso:
            # Too old (e.g. confirmed in a previous session). Don't send a
            # stale reminder — but DO mark reminder_sent so we never look
            # at this row again.
            try:
                don_table.update_item(
                    Key={"donation_id": d["donation_id"]},
                    UpdateExpression="SET reminder_sent = :t, reminder_sent_at = :ts, reminder_skipped_reason = :r",
                    ExpressionAttributeValues={
                        ":t": True,
                        ":ts": _now_iso(),
                        ":r": "too_old",
                    },
                )
            except Exception:
                pass
            skipped += 1
            continue

        donor_id = d.get("donor_id")
        bridge_id = d.get("bridge_id")
        if not donor_id or not bridge_id:
            skipped += 1
            continue
        donor = db.get_table("donors").get_item(Key={"user_id": donor_id}).get("Item")
        bridge = db.get_table("bridges").get_item(Key={"bridge_id": bridge_id}).get("Item")
        if not (donor and bridge):
            skipped += 1
            continue

        try:
            reminder_msg = bedrock_chat.donation_reminder(
                donor, bridge, str(d.get("scheduled_date", "")),
            )
        except Exception as exc:
            logger.warning(f"could not generate reminder: {exc}")
            continue

        _send_message(
            donor, bridge, reminder_msg,
            donor.get("preferred_language", "english"),
            msg_type="reminder",
        )
        try:
            don_table.update_item(
                Key={"donation_id": d["donation_id"]},
                UpdateExpression="SET reminder_sent = :t, reminder_sent_at = :ts",
                ExpressionAttributeValues={":t": True, ":ts": _now_iso()},
            )
        except Exception as exc:
            logger.warning(f"could not flag donation reminder_sent: {exc}")

        cycle_id = _new_id("rmd")
        _agent_log(
            cycle_id, "reminder_sent",
            donor_id=str(donor_id)[:14], donor_name=donor.get("name"),
            bridge_id=bridge_id,
            patient_name=bridge.get("patient_name"),
        )
        sent += 1

    return {"reminders_sent": sent, "candidates_skipped": skipped}


def send_reminders() -> dict:
    """Manual override: same logic as the autonomous reminder but bypasses
    the time delay (so a coordinator can force a reminder out immediately).
    Kept for backwards-compatibility / debugging; the dashboard no longer
    exposes a button for it because the agent now sends reminders
    automatically REMINDER_DELAY_SECONDS after each confirmation.
    """
    don_table = db.get_table("donations")
    resp = don_table.scan(Limit=500)
    items = [d for d in resp.get("Items", []) if d.get("status") == "confirmed"
             and not d.get("reminder_sent")]

    sent = 0
    skipped = 0
    for d in items:
        donor = db.get_table("donors").get_item(Key={"user_id": d["donor_id"]}).get("Item")
        bridge = db.get_table("bridges").get_item(Key={"bridge_id": d["bridge_id"]}).get("Item")
        if not (donor and bridge):
            skipped += 1
            continue
        reminder = bedrock_chat.donation_reminder(
            donor, bridge, str(d.get("scheduled_date", "")),
        )
        _send_message(donor, bridge, reminder,
                      donor.get("preferred_language", "english"),
                      msg_type="reminder")
        try:
            don_table.update_item(
                Key={"donation_id": d["donation_id"]},
                UpdateExpression="SET reminder_sent = :t, reminder_sent_at = :ts",
                ExpressionAttributeValues={":t": True, ":ts": _now_iso()},
            )
        except Exception:
            pass
        sent += 1
    return {"reminders_sent": sent, "candidates_skipped": skipped}


def run_surge(bridge_id: str) -> dict:
    """Dispatch an emergency surge for a single bridge — IMMEDIATELY,
    bypassing the agent's cycle lock.

    Surge is a one-shot, idempotent operation (already-emailed donors
    are filtered out) so running it concurrently with another autonomous
    cycle is safe — we can never double-email the same donor for the
    same bridge. This is exactly what "emergency" should mean: the
    button does the thing now, regardless of whether the background
    agent happens to be mid-cycle.
    """
    cycle_id = _new_id("cyc")
    _agent_log(cycle_id, "cycle_start", source="emergency_surge")

    resp = db.get_table("bridges").get_item(Key={"bridge_id": bridge_id})
    bridge = resp.get("Item")
    if not bridge:
        _agent_log(cycle_id, "no_bridge", reason=f"bridge {bridge_id} not found")
        return {"cycle_id": cycle_id, "result": "no_bridge"}

    target = _confirmation_target(bridge)
    confirmed_n = _confirmed_count(bridge_id)
    if confirmed_n >= target:
        _agent_log(
            cycle_id, "already_covered",
            bridge_id=bridge_id, confirmed=confirmed_n, target=target,
        )
        return {
            "cycle_id": cycle_id, "result": "already_covered",
            "bridge_id": bridge_id,
            "patient_name": bridge.get("patient_name"),
            "confirmed": confirmed_n, "target": target,
        }

    # allow_partial=True so a manual click ALWAYS blasts every remaining
    # un-emailed donor for this patient, even if normal-pacing already
    # sent one earlier. The button means "emergency, do it now."
    result = _surge_blast_bridge(bridge, cycle_id, target, allow_partial=True)

    # Reset urgency_mode so subsequent autonomous cycles don't re-blast.
    try:
        db.get_table("bridges").update_item(
            Key={"bridge_id": bridge_id},
            UpdateExpression="SET urgency_mode = :m",
            ExpressionAttributeValues={":m": "normal"},
        )
    except Exception as exc:
        logger.warning(
            f"could not reset urgency_mode for {bridge_id[:14]}: {exc}"
        )
    return result


def _confirm_donation(donor: dict, bridge: dict) -> dict:
    scheduled = forecasting.predict_next_transfusion(bridge) or datetime.now().date()
    donation = {
        "donation_id": _new_id("don"),
        "donor_id": donor["user_id"],
        "bridge_id": bridge["bridge_id"],
        "scheduled_date": scheduled.isoformat(),
        "status": "confirmed",
        "confirmed_at": _now_iso(),
        "donor_name": donor.get("name"),
        "patient_name": bridge.get("patient_name"),
    }
    db.batch_write("donations", [donation])

    # Bump donor stats
    try:
        donors_table = db.get_table("donors")
        donors_table.update_item(
            Key={"user_id": donor["user_id"]},
            UpdateExpression="ADD response_count :one",
            ExpressionAttributeValues={":one": 1},
        )
    except Exception as exc:
        logger.warning(f"could not update donor stats: {exc}")

    # Advance the patient's transfusion timeline so the next agent cycle
    # picks the *next* most urgent patient instead of looping on this one.
    try:
        cadence = int(bridge.get("frequency_in_days") or 21)
    except (TypeError, ValueError):
        cadence = 21
    try:
        next_due = scheduled + timedelta(days=cadence)
        db.get_table("bridges").update_item(
            Key={"bridge_id": bridge["bridge_id"]},
            UpdateExpression=(
                "SET last_transfusion_date = :ldate, "
                "expected_next_transfusion_date = :ndate, "
                "last_confirmed_donor = :dn, "
                "last_confirmed_at = :ts"
            ),
            ExpressionAttributeValues={
                ":ldate": scheduled.isoformat(),
                ":ndate": next_due.isoformat(),
                ":dn": donor.get("name") or donor.get("user_id"),
                ":ts": _now_iso(),
            },
        )
    except Exception as exc:
        logger.warning(f"could not advance bridge timeline: {exc}")

    return donation


def _bump_skip(donor_id: str) -> None:
    try:
        db.get_table("donors").update_item(
            Key={"user_id": donor_id},
            UpdateExpression="ADD skip_score :one",
            ExpressionAttributeValues={":one": 1},
        )
    except Exception as exc:
        logger.warning(f"could not bump skip score: {exc}")


def run_agent_cycle(target_bridge_id: Optional[str] = None) -> dict:
    """Run a single agent cycle.

    NORMAL/BACKUP mode is now a one-step state machine per patient:
    each cycle the agent advances each upcoming patient by exactly one
    step (email next donor, OR keep waiting, OR confirm, OR escalate).
    The wait window between donors is `DONOR_WAIT_SECONDS` (configurable
    in the UI). This guarantees we never email all 8 donors at once for
    the same patient — only one donor per wait window.

    SURGE mode still blasts the top donors in parallel.

    If `target_bridge_id` is set, act only on that bridge.

    The whole call is serialised by `_cycle_lock` so the background
    thread, "Advance all patients" button and Streamlit reruns can't
    overlap and double-email anyone. If a cycle is already in flight,
    the new request is no-op'd and reported as `skipped_concurrent`.
    """
    global _last_cycle_ts, _last_cycle_result
    acquired = _cycle_lock.acquire(blocking=False)
    if not acquired:
        logger.info("run_agent_cycle: another cycle in flight — skipping")
        return {"result": "skipped_concurrent"}

    try:
        res = _run_agent_cycle_locked(target_bridge_id)
        _last_cycle_ts = time.time()
        _last_cycle_result = str((res or {}).get("result", "?"))
        return res
    finally:
        _cycle_lock.release()


def _run_agent_cycle_locked(target_bridge_id: Optional[str] = None) -> dict:
    # Always sweep pending donor replies BEFORE doing per-bridge work, so a
    # YES from any donor (especially in surge mode where multiple donors
    # were emailed in parallel) is reflected in DDB before the per-bridge
    # state machine asks "is this bridge already covered?". Without this,
    # the agent could keep emailing the next-ranked donor even though
    # someone earlier in the queue has already confirmed.
    try:
        process_pending_replies(max_replies=10)
    except Exception as exc:
        logger.warning(f"auto process_pending_replies failed: {exc}")

    # Autonomous post-confirmation reminders. For every donor who said YES
    # more than REMINDER_DELAY_SECONDS ago (default 3 min), send a brief
    # reconfirm reminder once. No manual button needed.
    try:
        _auto_send_due_reminders()
    except Exception as exc:
        logger.warning(f"auto reminders failed: {exc}")

    if target_bridge_id:
        cycle_id = _new_id("cyc")
        _agent_log(cycle_id, "cycle_start")
        resp = db.get_table("bridges").get_item(Key={"bridge_id": target_bridge_id})
        bridge = resp.get("Item")
        if not bridge:
            _agent_log(cycle_id, "no_bridge", reason=f"bridge {target_bridge_id} not found")
            return {"cycle_id": cycle_id, "result": "no_bridge"}
        return _run_for_bridge(bridge, cycle_id)

    upcoming = forecasting.upcoming_demand(days=90)
    if not upcoming:
        cycle_id = _new_id("cyc")
        _agent_log(cycle_id, "cycle_start")
        _agent_log(cycle_id, "no_demand")
        return {"cycle_id": cycle_id, "result": "no_demand"}

    parent_cycle = _new_id("cyc")
    _agent_log(parent_cycle, "cycle_start", upcoming_count=len(upcoming))

    # Advance EVERY upcoming patient by one step this tick. With the new
    # per-donor wait window, most patients will simply return "waiting" —
    # cheap and idempotent. Only the patients whose wait window expired (or
    # who haven't been contacted yet) will actually send an email.
    results: list[dict] = []
    for target in upcoming[:15]:
        try:
            sub_cycle_id = _new_id("cyc")
            resp = db.get_table("bridges").get_item(Key={"bridge_id": target["bridge_id"]})
            bridge = resp.get("Item")
            if not bridge:
                continue
            res = _run_for_bridge(bridge, sub_cycle_id)
            results.append(res)
        except Exception as exc:
            logger.exception(f"per-bridge cycle error: {exc}")

    # Summarise — surface the most "interesting" outcome so the UI's single
    # toast still tells a clear story when the user clicks Run all pending.
    priority = ["confirmed", "next_donor_emailed", "surge_dispatched",
                "question_answered", "already_covered", "waiting",
                "escalated", "all_in_cooldown", "no_donors", "no_ranked"]
    highlight = None
    for level in priority:
        for r in results:
            if r.get("result") == level:
                highlight = r
                break
        if highlight:
            break

    summary = {
        "cycle_id": parent_cycle,
        "result": (highlight or {}).get("result", "no_action"),
        "bridges_processed": len(results),
        "results": results,
    }
    if highlight:
        for k in ("bridge_id", "patient_name", "donor_name", "donation_id",
                  "elapsed_seconds", "remaining_seconds"):
            if highlight.get(k) is not None:
                summary[k] = highlight[k]
    return summary


def _run_for_bridge(bridge: dict, cycle_id: str) -> dict:
    """Per-bridge dispatcher.

    Routes to:
      * `_step_bridge` for NORMAL / BACKUP — one-step sequential pacing
        with a per-donor wait window.
      * `_surge_blast_bridge` for SURGE — parallel blast, idempotent
        across cycles.
    """
    bridge_id = bridge["bridge_id"]

    _agent_log(
        cycle_id, "selected_bridge",
        bridge_id=bridge_id,
        blood_group=bridge.get("blood_group"),
        patient_name=bridge.get("patient_name"),
        predicted_next=str(forecasting.predict_next_transfusion(bridge)),
    )

    target = _confirmation_target(bridge)
    confirmed_n = _confirmed_count(bridge_id)
    if confirmed_n >= target:
        _agent_log(
            cycle_id, "already_covered",
            bridge_id=bridge_id, confirmed=confirmed_n, target=target,
        )
        return {
            "cycle_id": cycle_id, "result": "already_covered",
            "bridge_id": bridge_id,
            "patient_name": bridge.get("patient_name"),
            "confirmed": confirmed_n, "target": target,
        }

    urgency_mode = (bridge.get("urgency_mode") or "normal").lower()
    _agent_log(
        cycle_id, "mode",
        confirmation_target=target, urgency_mode=urgency_mode,
    )

    if urgency_mode == "surge":
        result = _surge_blast_bridge(bridge, cycle_id, target)
        # Surge is a ONE-SHOT blast by design — flip the bridge back to
        # normal mode so subsequent autonomous cycles don't keep
        # re-blasting more donors batch-by-batch. Inbound replies still
        # flow through process_pending_replies() and the over-confirmation
        # guard still applies. We reset for every surge outcome (sent,
        # idempotent, all-in-cooldown) so the flag never sticks.
        try:
            db.get_table("bridges").update_item(
                Key={"bridge_id": bridge_id},
                UpdateExpression="SET urgency_mode = :m",
                ExpressionAttributeValues={":m": "normal"},
            )
        except Exception as exc:
            logger.warning(
                f"could not reset urgency_mode for {bridge_id[:14]}: {exc}"
            )
        return result
    return _step_bridge(bridge, cycle_id, target)


def _step_bridge(bridge: dict, cycle_id: str, target: int) -> dict:
    """One-step state machine for NORMAL / BACKUP mode.

    Each call advances by exactly one step:

      1. Inspect the most-recently-emailed donor for this bridge.
         - YES        -> confirm (or polite "already covered" if target met) -> stop
         - QUESTION   -> answer, donor still in flight, stop this tick
         - CANCEL/NO  -> mark, fall through to email the next donor
         - no reply yet:
             * wait window NOT expired  -> return 'waiting'
             * wait window     expired  -> bump skip, fall through
      2. Email the next-ranked donor not yet contacted.
      3. If pool exhausted -> 'escalated'.
    """
    bridge_id = bridge["bridge_id"]

    sent_emails = _outbound_for_bridge(bridge_id)  # newest first

    if sent_emails:
        last_donor_id, last_ts = sent_emails[0]
        last_donor = _get_donor(last_donor_id) or {}
        last_name = last_donor.get("name") or "donor"
        reply = _inbound_after(last_donor_id, last_ts)

        if reply:
            intent = bedrock_chat.classify_intent(reply.get("message", ""))
            _agent_log(
                cycle_id, "reply_received",
                donor_id=str(last_donor_id)[:12],
                donor_name=last_name,
                intent=intent,
                bridge_id=bridge_id,
                processed_inbound_ts=reply.get("ts"),
                reply_preview=reply.get("message", "")[:80],
            )

            if intent == "YES":
                # Re-check over-confirmation guard at confirmation time.
                if _confirmed_count(bridge_id) >= target:
                    decline = bedrock_chat.thanks_already_covered(last_donor, bridge)
                    _send_message(
                        last_donor or {"user_id": last_donor_id, "name": last_name},
                        bridge, decline,
                        last_donor.get("preferred_language", "english"),
                        msg_type="already_covered",
                    )
                    _agent_log(
                        cycle_id, "thanks_already_covered",
                        donor_id=str(last_donor_id)[:12],
                        donor_name=last_name, bridge_id=bridge_id,
                    )
                    _active_cycle_state.update({
                        "active": False, "status": "already_covered",
                        "donor_name": last_name, "donor_id": last_donor_id,
                        "patient": bridge.get("patient_name"),
                        "language": last_donor.get("preferred_language"),
                        "started": _now_iso(),
                        "elapsed_seconds": 0, "remaining_seconds": 0,
                        "wait_total_seconds": 0,
                    })
                    return {
                        "cycle_id": cycle_id, "result": "already_covered",
                        "bridge_id": bridge_id,
                        "patient_name": bridge.get("patient_name"),
                        "donor_name": last_name,
                    }

                donor_for_confirm = last_donor or {
                    "user_id": last_donor_id, "name": last_name,
                }
                donation = _confirm_donation(donor_for_confirm, bridge)
                _agent_log(
                    cycle_id, "confirmed",
                    donor_id=str(last_donor_id)[:12], donor_name=last_name,
                    bridge_id=bridge_id, donation_id=donation["donation_id"],
                )
                _active_cycle_state.update({
                    "active": False, "status": "confirmed",
                    "donor_name": last_name, "donor_id": last_donor_id,
                    "patient": bridge.get("patient_name"),
                    "language": last_donor.get("preferred_language"),
                    "started": _now_iso(),
                    "elapsed_seconds": 0, "remaining_seconds": 0,
                    "wait_total_seconds": 0,
                })
                return {
                    "cycle_id": cycle_id, "result": "confirmed",
                    "bridge_id": bridge_id, "donor_id": last_donor_id,
                    "donor_name": last_name, "donation_id": donation["donation_id"],
                    "patient_name": bridge.get("patient_name"),
                }

            if intent == "QUESTION":
                if last_donor:
                    answer = bedrock_chat.answer_donor_question(
                        last_donor, reply.get("message", ""), bridge,
                    )
                    _send_message(
                        last_donor, bridge, answer,
                        last_donor.get("preferred_language", "english"),
                        msg_type="qa_answer",
                    )
                _agent_log(
                    cycle_id, "question_answered",
                    donor_id=str(last_donor_id)[:12], donor_name=last_name,
                    bridge_id=bridge_id,
                )
                # Donor still in flight — give them a fresh wait window
                # before we move on. Do not email anyone else this tick.
                return {
                    "cycle_id": cycle_id, "result": "question_answered",
                    "bridge_id": bridge_id,
                    "patient_name": bridge.get("patient_name"),
                    "donor_name": last_name,
                }

            if intent == "CANCEL":
                _cancel_donation(last_donor_id, bridge_id,
                                 reason=reply.get("message", "")[:120])
                _bump_skip(last_donor_id)
                _agent_log(
                    cycle_id, "donation_cancelled",
                    donor_id=str(last_donor_id)[:12], donor_name=last_name,
                    bridge_id=bridge_id,
                )
                # Fall through and email next donor immediately.

            elif intent == "NO":
                _bump_skip(last_donor_id)
                _agent_log(
                    cycle_id, "donor_declined",
                    donor_id=str(last_donor_id)[:12], donor_name=last_name,
                    bridge_id=bridge_id,
                )
                # Fall through and email next donor immediately.

            else:
                # UNKNOWN / unparsable — treat like a NO so we don't get stuck.
                _bump_skip(last_donor_id)

        else:
            # No reply — check if the wait window has elapsed.
            elapsed = _seconds_since(last_ts)
            wait = get_donor_wait_seconds()
            if elapsed < wait:
                remaining = int(wait - elapsed)
                _active_cycle_state.update({
                    "active": True, "status": "waiting_for_reply",
                    "donor_name": last_name, "donor_id": last_donor_id,
                    "patient": bridge.get("patient_name"),
                    "language": last_donor.get("preferred_language"),
                    "started": last_ts,
                    "elapsed_seconds": int(elapsed),
                    "remaining_seconds": remaining,
                    "wait_total_seconds": wait,
                })
                _agent_log(
                    cycle_id, "donor_wait_active",
                    bridge_id=bridge_id,
                    donor_id=str(last_donor_id)[:12], donor_name=last_name,
                    elapsed_seconds=int(elapsed), remaining_seconds=remaining,
                )
                return {
                    "cycle_id": cycle_id, "result": "waiting",
                    "bridge_id": bridge_id,
                    "patient_name": bridge.get("patient_name"),
                    "donor_name": last_name,
                    "elapsed_seconds": int(elapsed),
                    "remaining_seconds": remaining,
                    "wait_total_seconds": wait,
                }
            _bump_skip(last_donor_id)
            _agent_log(
                cycle_id, "donor_wait_expired",
                bridge_id=bridge_id,
                donor_id=str(last_donor_id)[:12], donor_name=last_name,
                elapsed_seconds=int(elapsed),
            )

    # Email the next-ranked donor that hasn't been contacted yet for this bridge.
    pool = _load_donor_pool(bridge)
    _agent_log(cycle_id, "donor_pool_loaded", count=len(pool))
    if not pool:
        _agent_log(cycle_id, "no_donors", bridge_id=bridge_id)
        return {
            "cycle_id": cycle_id, "result": "no_donors",
            "bridge_id": bridge_id,
            "patient_name": bridge.get("patient_name"),
        }

    already_emailed_ids = {d_id for d_id, _ in sent_emails}
    cooldown_ids = _recently_contacted_donors(COOLDOWN_HOURS)
    confirmed_ids = _recently_confirmed_donors(days=7)
    skip_ids = cooldown_ids | confirmed_ids
    eligible = [
        d for d in pool
        if d.get("user_id") not in already_emailed_ids
        and d.get("user_id") not in skip_ids
    ]
    skipped_cd = sum(1 for d in pool if d.get("user_id") in skip_ids)
    if skipped_cd:
        _agent_log(cycle_id, "cooldown_skip", skipped=skipped_cd, hours=COOLDOWN_HOURS)

    rank = len(sent_emails) + 1

    if not eligible:
        # Nothing more we can do this tick.
        if rank == 1 and skipped_cd:
            _agent_log(
                cycle_id, "all_in_cooldown",
                bridge_id=bridge_id, pool_size=skipped_cd,
            )
            return {
                "cycle_id": cycle_id, "result": "all_in_cooldown",
                "bridge_id": bridge_id,
                "patient_name": bridge.get("patient_name"),
                "in_cooldown": skipped_cd,
            }
        _agent_log(
            cycle_id, "escalated",
            bridge_id=bridge_id, tried=len(sent_emails),
        )
        return {
            "cycle_id": cycle_id, "result": "escalated",
            "bridge_id": bridge_id,
            "patient_name": bridge.get("patient_name"),
            "tried": len(sent_emails),
        }

    ranked = ranking.rank_for_bridge(bridge, eligible, top_n=1)
    if not ranked:
        _agent_log(cycle_id, "no_ranked", bridge_id=bridge_id)
        return {
            "cycle_id": cycle_id, "result": "no_ranked",
            "bridge_id": bridge_id,
            "patient_name": bridge.get("patient_name"),
        }

    next_donor = ranked[0]
    bedrock_msg = bedrock_chat.generate_outreach(next_donor, bridge)
    sent = _send_message(
        next_donor, bridge, bedrock_msg,
        next_donor.get("preferred_language", "english"),
    )
    if not sent:
        # Dedup guard fired (rare race). Don't log a fake outreach_sent
        # and don't claim the donor was emailed — the next cycle will
        # naturally retry once the dedup window expires.
        _agent_log(
            cycle_id, "outreach_skipped_duplicate",
            donor_id=str(next_donor["user_id"])[:12],
            donor_name=next_donor.get("name"),
            bridge_id=bridge_id,
        )
        return {
            "cycle_id": cycle_id, "result": "waiting",
            "bridge_id": bridge_id,
            "patient_name": bridge.get("patient_name"),
            "donor_name": next_donor.get("name"),
        }

    _agent_log(
        cycle_id, "outreach_sent",
        donor_id=str(next_donor["user_id"])[:12],
        donor_name=next_donor.get("name"),
        language=next_donor.get("preferred_language"),
        bridge_id=bridge_id,
        rank=rank,
    )

    wait = get_donor_wait_seconds()
    _active_cycle_state.update({
        "active": True, "status": "emailed_waiting",
        "donor_name": next_donor.get("name"),
        "donor_id": next_donor["user_id"],
        "patient": bridge.get("patient_name"),
        "language": next_donor.get("preferred_language"),
        "started": _now_iso(),
        "elapsed_seconds": 0,
        "remaining_seconds": wait,
        "wait_total_seconds": wait,
    })

    return {
        "cycle_id": cycle_id, "result": "next_donor_emailed",
        "bridge_id": bridge_id,
        "patient_name": bridge.get("patient_name"),
        "donor_name": next_donor.get("name"),
        "rank": rank,
    }


def _surge_blast_bridge(bridge: dict, cycle_id: str, target: int,
                         *, allow_partial: bool = False) -> dict:
    """SURGE mode: blast top-N donors in parallel.

    Always idempotent within a bridge (donors already emailed for this
    bridge are filtered out — we never double-email).

    Two call shapes:

      * Autonomous (allow_partial=False, default): if ANY donor has
        already been emailed for this bridge, short-circuit and don't
        send more. This is what the background cycle uses so
        `urgency_mode = "surge"` can never cause batch-by-batch
        reblasting across cycles.

      * Manual (allow_partial=True): the user explicitly clicked the
        emergency button — blast every remaining un-emailed eligible
        donor right now, regardless of who's been contacted before.
    """
    bridge_id = bridge["bridge_id"]

    pool = _load_donor_pool(bridge)
    _agent_log(cycle_id, "donor_pool_loaded", count=len(pool))
    if not pool:
        _agent_log(cycle_id, "no_donors", bridge_id=bridge_id)
        return {
            "cycle_id": cycle_id, "result": "no_donors",
            "bridge_id": bridge_id,
            "patient_name": bridge.get("patient_name"),
        }

    already_emailed_ids = {d_id for d_id, _ in _outbound_for_bridge(bridge_id)}

    # Autonomous one-shot guard. With allow_partial=True (manual click),
    # we instead just FILTER already-emailed donors out of `eligible`
    # below and email the rest, so the user always gets a fresh blast
    # to anyone we haven't asked yet for this patient.
    if not allow_partial and already_emailed_ids:
        _agent_log(
            cycle_id, "surge_idempotent",
            bridge_id=bridge_id, already_dispatched=len(already_emailed_ids),
        )
        return {
            "cycle_id": cycle_id, "result": "surge_dispatched",
            "bridge_id": bridge_id,
            "patient_name": bridge.get("patient_name"),
            "dispatched": 0,
            "note": f"Already emailed {len(already_emailed_ids)} donor(s); awaiting replies.",
        }

    cooldown_ids = _recently_contacted_donors(COOLDOWN_HOURS)
    confirmed_ids = _recently_confirmed_donors(days=7)
    skip_ids = cooldown_ids | confirmed_ids | already_emailed_ids
    eligible = [
        d for d in pool
        if d.get("user_id") not in skip_ids
    ]
    if not eligible:
        _agent_log(
            cycle_id, "all_in_cooldown",
            bridge_id=bridge_id, pool_size=len(pool),
        )
        return {
            "cycle_id": cycle_id, "result": "all_in_cooldown",
            "bridge_id": bridge_id,
            "patient_name": bridge.get("patient_name"),
        }

    ranked = ranking.rank_for_bridge(bridge, eligible, top_n=MAX_DONORS_PER_BRIDGE)
    _agent_log(
        cycle_id, "ranked",
        top=[{"id": d["user_id"][:12], "score": round(float(d.get("score", 0)), 3)} for d in ranked],
    )
    if not ranked:
        return {
            "cycle_id": cycle_id, "result": "no_ranked",
            "bridge_id": bridge_id,
            "patient_name": bridge.get("patient_name"),
        }

    actually_sent = 0
    for idx, donor in enumerate(ranked):
        bedrock_msg = bedrock_chat.generate_outreach(donor, bridge)
        sent = _send_message(
            donor, bridge, bedrock_msg,
            donor.get("preferred_language", "english"),
        )
        if not sent:
            continue
        actually_sent += 1
        _agent_log(
            cycle_id, "outreach_sent",
            donor_id=donor["user_id"][:12], donor_name=donor.get("name"),
            language=donor.get("preferred_language"),
            bridge_id=bridge_id, rank=idx + 1,
        )

    _agent_log(
        cycle_id, "surge_dispatched",
        bridge_id=bridge_id, dispatched=actually_sent,
    )
    return {
        "cycle_id": cycle_id, "result": "surge_dispatched",
        "bridge_id": bridge_id,
        "patient_name": bridge.get("patient_name"),
        "dispatched": actually_sent,
        "note": "Replies arrive asynchronously; over-confirm guard prevents duplicates.",
    }


# Background loop machinery -------------------------------------------------

_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()


_last_cycle_ts: Optional[float] = None
_last_cycle_result: Optional[str] = None
_last_cycle_interval: int = CYCLE_INTERVAL_SECONDS


def _background_loop(interval: int):
    global _last_cycle_ts, _last_cycle_result, _last_cycle_interval
    _last_cycle_interval = interval
    while not _stop_event.is_set():
        try:
            res = run_agent_cycle()
            _last_cycle_ts = time.time()
            _last_cycle_result = str((res or {}).get("result", "?"))
        except Exception as exc:
            logger.exception(f"agent cycle error: {exc}")
            _last_cycle_ts = time.time()
            _last_cycle_result = f"error:{exc.__class__.__name__}"
        _stop_event.wait(interval)


def _reset_stale_surge_flags() -> int:
    """One-time cleanup at agent startup: any bridge still flagged as
    `urgency_mode = "surge"` is reset to "normal".

    Surge is a one-shot operation in our model — leftover flags from a
    previous run (or from before the reset logic existed) would otherwise
    cause the agent to attempt one more surge dispatch on the next cycle.
    """
    try:
        table = db.get_table("bridges")
        resp = table.scan()
        reset = 0
        for b in resp.get("Items", []):
            if (b.get("urgency_mode") or "").lower() == "surge":
                try:
                    table.update_item(
                        Key={"bridge_id": b["bridge_id"]},
                        UpdateExpression="SET urgency_mode = :m",
                        ExpressionAttributeValues={":m": "normal"},
                    )
                    reset += 1
                except Exception:
                    pass
        if reset:
            logger.info(f"reset {reset} stale surge flag(s) at agent startup")
        return reset
    except Exception as exc:
        logger.warning(f"could not scan for stale surge flags: {exc}")
        return 0


def start_background_agent(interval: Optional[int] = None) -> None:
    global _thread, _last_cycle_interval
    if _thread and _thread.is_alive():
        return
    _reset_stale_surge_flags()
    _stop_event.clear()
    iv = int(interval or CYCLE_INTERVAL_SECONDS)
    _last_cycle_interval = iv
    _thread = threading.Thread(
        target=_background_loop,
        args=(iv,),
        daemon=True,
        name="spandan-agent",
    )
    _thread.start()


def stop_background_agent() -> None:
    _stop_event.set()


def is_running() -> bool:
    return bool(_thread and _thread.is_alive())


def agent_status() -> dict:
    """Live snapshot of the background agent for the UI status bar.

    Returns a dict with:
      running                 - bool, is the worker thread alive?
      cycle_interval_seconds  - how often the agent wakes up
      donor_wait_seconds      - per-donor reply window
      last_cycle_seconds_ago  - int or None, seconds since last cycle finished
      seconds_until_next      - int or None, ETA for next cycle
      last_result             - str or None, what the last cycle returned
    """
    running = is_running()
    if _last_cycle_ts is None:
        return {
            "running": running,
            "cycle_interval_seconds": _last_cycle_interval,
            "donor_wait_seconds": _donor_wait_seconds,
            "last_cycle_seconds_ago": None,
            "seconds_until_next": None,
            "last_result": None,
        }
    elapsed = max(0, int(time.time() - _last_cycle_ts))
    until_next = max(0, _last_cycle_interval - elapsed) if running else None
    return {
        "running": running,
        "cycle_interval_seconds": _last_cycle_interval,
        "donor_wait_seconds": _donor_wait_seconds,
        "last_cycle_seconds_ago": elapsed,
        "seconds_until_next": until_next,
        "last_result": _last_cycle_result,
    }


def recent_log(limit: int = 50) -> list[dict]:
    """Return the most recent agent_log items across all cycles."""
    table = db.get_table("agent_log")
    items: list[dict] = []
    response = table.scan(Limit=200)
    items.extend(response.get("Items", []))
    items.sort(key=lambda x: x.get("ts", ""), reverse=True)
    return items[:limit]


@db.retry_on_clock_skew
def patient_pipeline(days: int = 90) -> list[dict]:
    """Return per-patient pipeline status for the Coordinator overview.

    For every bridge whose predicted next transfusion is within `days`,
    return a row showing:
      - patient_name, blood_group, urgency (days_until)
      - donors_contacted: total outreach emails sent across all cycles
      - status: 'Confirmed' | 'In progress' | 'Escalated' | 'Pending'
      - last_confirmed_donor (if any)

    Decorated with @retry_on_clock_skew so a stale AWS signature (e.g.
    after the laptop wakes from sleep with a drifted clock) is recovered
    automatically instead of bubbling a 500 to the dashboard.
    """
    upcoming = forecasting.upcoming_demand(days=days)

    log_table = db.get_table("agent_log")
    log_items = log_table.scan(Limit=800).get("Items", [])
    log_items.sort(key=lambda i: i.get("ts", ""), reverse=True)

    # All outreach + escalation events grouped by bridge_id (across cycles).
    contacted_per_bridge: dict[str, int] = {}
    state_per_bridge: dict[str, str] = {}  # most recent meaningful action
    last_outreach_per_bridge: dict[str, dict] = {}  # most recent outreach_sent event
    for it in log_items:  # newest first
        bid = it.get("bridge_id")
        if not bid:
            continue
        action = it.get("action")
        if action == "outreach_sent":
            contacted_per_bridge[bid] = contacted_per_bridge.get(bid, 0) + 1
            if bid not in last_outreach_per_bridge:
                last_outreach_per_bridge[bid] = {
                    "donor_name": it.get("donor_name"),
                    "ts": it.get("ts"),
                }
        if bid in state_per_bridge:
            continue
        if action in (
            "confirmed", "escalated", "donor_wait_active",
            "donor_wait_expired", "outreach_sent", "donor_declined",
            "donation_cancelled", "thanks_already_covered",
            "all_in_cooldown", "surge_dispatched",
        ):
            state_per_bridge[bid] = action

    # Confirmations: who said YES per bridge.
    bridges_with_confirm: dict[str, str] = {}
    don_items = db.get_table("donations").scan(Limit=500).get("Items", [])
    for d in don_items:
        if d.get("status") != "confirmed":
            continue
        bid = d.get("bridge_id")
        if not bid:
            continue
        prev = bridges_with_confirm.get(bid, "")
        cur = d.get("confirmed_at", "")
        if cur > prev:
            bridges_with_confirm[bid] = d.get("donor_name") or "donor"

    rows: list[dict] = []
    for u in upcoming:
        bid = u["bridge_id"]
        contacted = contacted_per_bridge.get(bid, 0)

        if bid in bridges_with_confirm:
            cycle_status = "Confirmed"
        else:
            last_action = state_per_bridge.get(bid)
            if last_action == "confirmed":
                cycle_status = "Confirmed"
            elif last_action == "escalated":
                cycle_status = "Escalated"
            elif last_action in (
                "outreach_sent", "donor_wait_active", "donor_wait_expired",
                "donor_declined", "thanks_already_covered", "surge_dispatched",
                "donation_cancelled",
            ):
                cycle_status = "In progress"
            elif last_action == "all_in_cooldown":
                cycle_status = "In progress"
            else:
                cycle_status = "Pending"

        last_out = last_outreach_per_bridge.get(bid) or {}
        last_donor_name = last_out.get("donor_name")
        last_outreach_ts = last_out.get("ts")
        wait_remaining = None
        if (last_outreach_ts and cycle_status == "In progress"
                and bid not in bridges_with_confirm):
            elapsed = _seconds_since(last_outreach_ts)
            wait = get_donor_wait_seconds()
            if elapsed < wait:
                wait_remaining = int(wait - elapsed)

        rows.append({
            "bridge_id": bid,
            "patient": u.get("patient_name"),
            "age": u.get("patient_age"),
            "blood_group": u.get("blood_group"),
            "hospital": u.get("hospital"),
            "days_until": u.get("days_until"),
            "predicted_next_date": u.get("predicted_next_date"),
            "donors_contacted": contacted,
            "status": cycle_status,
            "last_confirmed_donor": bridges_with_confirm.get(bid) or u.get("last_confirmed_donor"),
            "last_donor_emailed": last_donor_name,
            "wait_remaining_seconds": wait_remaining,
        })

    return rows


def last_cycle_outreach() -> Optional[dict]:
    """Return the most recently active patient's full sequential donor queue.

    With the new state-machine pacing each cycle emails ONE donor and
    returns. So instead of grouping by cycle_id we group by `bridge_id` —
    the queue shows every donor we've asked for this patient (in order)
    plus their current status.
    """
    log = recent_log(limit=600)
    if not log:
        return None

    # Find the most recent outreach activity to pick a patient.
    bridge_id: Optional[str] = None
    for it in log:
        if it.get("action") in ("outreach_sent", "donor_wait_active", "confirmed",
                                 "escalated", "donor_declined", "donation_cancelled",
                                 "thanks_already_covered") and it.get("bridge_id"):
            bridge_id = it.get("bridge_id")
            break
    if not bridge_id:
        return None

    # Most recent selected_bridge for this bridge — gives us patient_name etc.
    bridge_event = next(
        (it for it in log if it.get("action") == "selected_bridge"
         and it.get("bridge_id") == bridge_id),
        None,
    )

    # All outreach_sent for this bridge, oldest first.
    sent_events = [
        it for it in log
        if it.get("action") == "outreach_sent" and it.get("bridge_id") == bridge_id
    ]
    sent_events.sort(key=lambda i: i.get("ts", ""))

    # Dedup: a donor might appear twice if escalated and reasked, keep first.
    seen: set[str] = set()
    queue: list[dict] = []
    for ev in sent_events:
        did = str(ev.get("donor_id", ""))[:12]
        if did in seen:
            continue
        seen.add(did)
        queue.append({
            "rank": len(queue) + 1,
            "donor_name": ev.get("donor_name"),
            "donor_id": ev.get("donor_id"),
            "language": ev.get("language"),
            "status": "waiting",
            "ts": ev.get("ts"),
        })

    # Walk events newest-last and apply per-donor state transitions.
    bridge_events = [it for it in log if it.get("bridge_id") == bridge_id]
    bridge_events.sort(key=lambda i: i.get("ts", ""))
    for it in bridge_events:
        action = it.get("action")
        donor_short = str(it.get("donor_id", ""))[:12]
        for q in queue:
            if str(q.get("donor_id", ""))[:12] != donor_short:
                continue
            if action == "confirmed":
                q["status"] = "CONFIRMED YES"
            elif action == "thanks_already_covered":
                q["status"] = "thanked (already covered)"
            elif action in ("reply_received", "delayed_reply_processed",
                            "reply_received_followup"):
                intent = (it.get("intent") or "?").upper()
                if intent == "YES" and q["status"] != "CONFIRMED YES":
                    q["status"] = "replied YES"
                elif intent == "NO":
                    q["status"] = "replied NO"
                elif intent == "CANCEL":
                    q["status"] = "cancelled"
                elif intent == "QUESTION":
                    q["status"] = "asked a question"
            elif action == "donor_declined" and q["status"] == "waiting":
                q["status"] = "replied NO"
            elif action == "donation_cancelled" and q["status"] != "CONFIRMED YES":
                q["status"] = "cancelled"
            elif action == "donor_wait_expired" and q["status"] == "waiting":
                q["status"] = "no reply (window closed)"
            elif action == "no_reply" and q["status"] == "waiting":
                q["status"] = "no reply"

    # Determine overall result.
    if any(q["status"] == "CONFIRMED YES" for q in queue):
        result = "confirmed"
    elif bridge_events and bridge_events[-1].get("action") == "escalated":
        result = "escalated"
    else:
        result = "in_progress"

    # Compute live waiting time on the most recent donor (if still pending).
    waiting = None
    if queue:
        last = queue[-1]
        if last.get("status") == "waiting" and last.get("ts"):
            elapsed = _seconds_since(last["ts"])
            wait = get_donor_wait_seconds()
            if elapsed < wait:
                waiting = {
                    "donor_name": last["donor_name"],
                    "elapsed_seconds": int(elapsed),
                    "remaining_seconds": int(wait - elapsed),
                    "wait_total_seconds": wait,
                }

    return {
        "bridge_id": bridge_id,
        "patient_name": (bridge_event or {}).get("patient_name"),
        "blood_group": (bridge_event or {}).get("blood_group"),
        "predicted_next": (bridge_event or {}).get("predicted_next"),
        "queue": queue,
        "result": result,
        "waiting": waiting,
    }


if __name__ == "__main__":
    print("Running a single agent cycle ...")
    result = run_agent_cycle()
    print("\nResult:", result)
