"""Pluggable email delivery layer.

Backends:
  - mailpit  (default for demo)  — local SMTP catcher at localhost:1025, web UI :8025
  - ses      (production)         — AWS SES via boto3
  - console  (CI/tests)           — print to stdout, no real send

Picks up new outbound rows from DynamoDB messages table and dispatches them.
Marks each row's delivery_status when done.

Public API:
    deliver_message(msg_row)   → dict with delivery result, updates DB row
    deliver_pending(limit=20)  → drains the outbox, returns summary
    start_delivery_worker()    → background thread, polls every 5s
"""
from __future__ import annotations

import hmac
import hashlib
import logging
import os
import smtplib
import threading
import time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from typing import Optional, Iterable
from urllib.parse import urlencode

import boto3
from boto3.dynamodb.conditions import Attr
from dotenv import load_dotenv

from services import db
from services.email_template import build_html_email, build_plain_email

load_dotenv()
logger = logging.getLogger("spandan.delivery")

BACKEND = os.getenv("DELIVERY_BACKEND", "mailpit").lower()
SMTP_HOST = os.getenv("SMTP_HOST", "localhost")
SMTP_PORT = int(os.getenv("SMTP_PORT", "1025"))
SMTP_FROM = os.getenv("SMTP_FROM", "spandan@bloodwarriors.in")
SMTP_FROM_NAME = os.getenv("SMTP_FROM_NAME", "Spandan AI Coordinator")
SES_REGION = os.getenv("AWS_DEFAULT_REGION", "us-east-1")
REPLY_BASE_URL = os.getenv("REPLY_BASE_URL", "http://localhost:8501/Reply")
REPLY_TOKEN_SECRET = os.getenv("REPLY_TOKEN_SECRET", "spandan-default-secret-change-me")


# -- Magic-link token helpers --------------------------------------------------

def _sign(payload: str) -> str:
    return hmac.new(
        REPLY_TOKEN_SECRET.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()[:24]


def make_reply_token(donor_id: str, bridge_id: str, ts: str) -> str:
    """Stateless signed token. Format: <donor_short>.<bridge_short>.<ts_short>.<sig>"""
    payload = f"{donor_id}|{bridge_id}|{ts}"
    sig = _sign(payload)
    # Keep token short by truncating ids; we still pass the originals as
    # query params so we can verify the signature server-side.
    return sig


def verify_reply_token(donor_id: str, bridge_id: str, ts: str, token: str) -> bool:
    expected = _sign(f"{donor_id}|{bridge_id}|{ts}")
    return hmac.compare_digest(expected, token or "")


def reply_url(donor_id: str, bridge_id: str, ts: str, intent: str) -> str:
    token = make_reply_token(donor_id, bridge_id, ts)
    qs = urlencode({
        "donor_id": donor_id,
        "bridge_id": bridge_id or "",
        "ts": ts,
        "token": token,
        "intent": intent,
    })
    return f"{REPLY_BASE_URL}?{qs}"


# -- Recipient resolution -----------------------------------------------------

def _resolve_donor_email(donor_id: str) -> tuple[Optional[str], Optional[str]]:
    """Return (email, name) for a donor_id.

    If the donor has no email attribute (pre-existing rows from earlier
    loads), synthesise one deterministically and persist it back so the UI
    and future cycles see a consistent address.
    """
    try:
        item = db.get_table("donors").get_item(Key={"user_id": donor_id}).get("Item")
    except Exception:
        item = None
    if not item:
        return None, None
    name = item.get("name") or "Donor"
    email = item.get("email")
    if not email:
        try:
            from data.load_dataset import _gen_demo_email, _gen_demo_phone
            email = _gen_demo_email(donor_id, name)
            phone = item.get("phone") or _gen_demo_phone(donor_id)
            db.get_table("donors").update_item(
                Key={"user_id": donor_id},
                UpdateExpression="SET email = if_not_exists(email, :e), phone = if_not_exists(phone, :p)",
                ExpressionAttributeValues={":e": email, ":p": phone},
            )
        except Exception as exc:
            logger.warning(f"could not synthesize email for {donor_id[:12]}: {exc}")
    return email, name


def _resolve_bridge(bridge_id: str) -> dict:
    if not bridge_id:
        return {}
    try:
        return db.get_table("bridges").get_item(Key={"bridge_id": bridge_id}).get("Item") or {}
    except Exception:
        return {}


# -- Backend implementations ---------------------------------------------------

def _send_smtp(to_email: str, to_name: str, subject: str, html: str, plain: str) -> dict:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr((SMTP_FROM_NAME, SMTP_FROM))
    msg["To"] = formataddr((to_name or "Donor", to_email))
    msg.attach(MIMEText(plain, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as s:
        s.sendmail(SMTP_FROM, [to_email], msg.as_string())
    return {"backend": "smtp", "to": to_email, "ok": True}


def _send_ses(to_email: str, to_name: str, subject: str, html: str, plain: str) -> dict:
    client = boto3.client("ses", region_name=SES_REGION)
    resp = client.send_email(
        Source=formataddr((SMTP_FROM_NAME, SMTP_FROM)),
        Destination={"ToAddresses": [to_email]},
        Message={
            "Subject": {"Data": subject, "Charset": "UTF-8"},
            "Body": {
                "Html": {"Data": html, "Charset": "UTF-8"},
                "Text": {"Data": plain, "Charset": "UTF-8"},
            },
        },
    )
    return {"backend": "ses", "to": to_email, "ok": True, "message_id": resp.get("MessageId")}


def _send_console(to_email: str, to_name: str, subject: str, html: str, plain: str) -> dict:
    logger.info(f"[CONSOLE] To: {to_name} <{to_email}>  Subject: {subject}")
    logger.info(plain[:400])
    return {"backend": "console", "to": to_email, "ok": True}


_BACKENDS = {
    "mailpit": _send_smtp,
    "smtp": _send_smtp,
    "ses": _send_ses,
    "console": _send_console,
}


# -- Public delivery API -------------------------------------------------------

def deliver_message(msg_row: dict) -> dict:
    """Deliver one outbound message row. Updates its delivery_status in DDB.

    Returns a result dict.
    """
    donor_id = msg_row.get("donor_id")
    bridge_id = msg_row.get("bridge_id") or ""
    ts = msg_row.get("ts")
    body = msg_row.get("message") or ""
    language = msg_row.get("language") or "english"
    msg_type = msg_row.get("type") or "outreach"

    if not donor_id or not ts:
        return {"ok": False, "reason": "missing_keys"}

    email, donor_name = _resolve_donor_email(donor_id)
    if not email:
        _mark(donor_id, ts, "skipped", "no_email")
        return {"ok": False, "reason": "no_email"}

    bridge = _resolve_bridge(bridge_id)
    donor = {"name": donor_name, "language": language}

    subject = _subject_for(bridge, donor_name, language, msg_type)
    reply_links = {
        "yes": reply_url(donor_id, bridge_id, ts, "YES"),
        "no": reply_url(donor_id, bridge_id, ts, "NO"),
    }
    html = build_html_email(
        donor_name=donor_name or "Donor",
        body_text=body,
        bridge=bridge,
        language=language,
        msg_type=msg_type,
        reply_links=reply_links,
    )
    plain = build_plain_email(
        donor_name=donor_name or "Donor",
        body_text=body,
        bridge=bridge,
        language=language,
        msg_type=msg_type,
        reply_links=reply_links,
    )

    sender = _BACKENDS.get(BACKEND, _send_console)
    try:
        result = sender(email, donor_name, subject, html, plain)
        _mark(donor_id, ts, "sent", BACKEND, email=email)
        return {"ok": True, "to": email, **result}
    except Exception as exc:
        logger.exception(f"delivery failed for {email}: {exc}")
        _mark(donor_id, ts, "failed", str(exc)[:100])
        return {"ok": False, "to": email, "error": str(exc)}


def _subject_for(bridge: dict, donor_name: Optional[str], language: str, msg_type: str) -> str:
    blood = bridge.get("blood_group") or ""
    patient = bridge.get("patient_name") or "a thalassemia patient"
    name_for = f"For: {donor_name}" if donor_name else ""
    if msg_type == "reminder":
        return f"[{name_for}] Reminder: {patient} needs your blood donation tomorrow"
    if msg_type == "thanks":
        return f"[{name_for}] Thank you, {donor_name or 'donor'}!"
    return f"[{name_for}] Urgent: {blood} blood needed for {patient}"


def _mark(donor_id: str, ts: str, status: str, detail: str = "", email: Optional[str] = None) -> None:
    try:
        update = "SET delivery_status = :s, delivery_detail = :d, delivered_at = :ts"
        values = {":s": status, ":d": detail, ":ts": _now_iso()}
        if email:
            update += ", delivered_to = :e"
            values[":e"] = email
        db.get_table("messages").update_item(
            Key={"donor_id": donor_id, "ts": ts},
            UpdateExpression=update,
            ExpressionAttributeValues=values,
        )
    except Exception as exc:
        logger.warning(f"could not update delivery status: {exc}")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def deliver_pending(limit: int = 20) -> dict:
    """Find outbound messages without delivery_status and dispatch them."""
    table = db.get_table("messages")
    pending: list[dict] = []
    scan_kwargs = {
        "FilterExpression": Attr("direction").eq("outbound") & Attr("delivery_status").not_exists(),
        "Limit": 100,
    }
    while len(pending) < limit:
        resp = table.scan(**scan_kwargs)
        for item in resp.get("Items", []):
            pending.append(item)
            if len(pending) >= limit:
                break
        if "LastEvaluatedKey" not in resp:
            break
        scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    summary = {"attempted": 0, "sent": 0, "skipped": 0, "failed": 0, "results": []}
    for m in pending:
        res = deliver_message(m)
        summary["attempted"] += 1
        if res.get("ok"):
            summary["sent"] += 1
        elif res.get("reason") == "no_email":
            summary["skipped"] += 1
        else:
            summary["failed"] += 1
        summary["results"].append(res)
    return summary


def recent_deliveries(limit: int = 50) -> Iterable[dict]:
    """Recently delivered messages whose status is terminal (sent / failed /
    skipped). Excludes 'pending' rows that are mid-dispatch — those would
    just flash grey in the UI for a few hundred ms before flipping to
    sent."""
    table = db.get_table("messages")
    items: list[dict] = []
    scan_kwargs = {
        "FilterExpression": Attr("delivery_status").is_in(["sent", "failed", "skipped"]),
        "Limit": 200,
    }
    resp = table.scan(**scan_kwargs)
    items.extend(resp.get("Items", []))
    items.sort(key=lambda x: x.get("delivered_at", x.get("ts", "")), reverse=True)
    return items[:limit]


# -- Background worker --------------------------------------------------------

_worker_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()


def _worker_loop(interval: int):
    logger.info(f"delivery worker started (backend={BACKEND}, interval={interval}s)")
    while not _stop_event.is_set():
        try:
            summary = deliver_pending(limit=20)
            if summary["attempted"]:
                logger.info(
                    f"delivery: attempted={summary['attempted']} sent={summary['sent']} "
                    f"skipped={summary['skipped']} failed={summary['failed']}"
                )
        except Exception as exc:
            logger.exception(f"delivery worker iteration crashed: {exc}")
        _stop_event.wait(interval)
    logger.info("delivery worker stopped")


def start_delivery_worker(interval: int = 5) -> bool:
    global _worker_thread
    if _worker_thread and _worker_thread.is_alive():
        return False
    _stop_event.clear()
    _worker_thread = threading.Thread(target=_worker_loop, args=(interval,), daemon=True)
    _worker_thread.start()
    return True


def stop_delivery_worker() -> None:
    _stop_event.set()


def is_worker_running() -> bool:
    return bool(_worker_thread and _worker_thread.is_alive())
