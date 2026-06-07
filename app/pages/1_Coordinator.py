"""Coordinator Dashboard.

Clean, professional single-page console for the Spandan AI agent. Shows
only what the coordinator needs to act on:

  * 4 big KPIs
  * one live status card (current activity)
  * action bar (Run all pending, Send reminders, Reset demo, MailPit link)
  * cadence + auto-refresh controls
  * one color-coded patient pipeline table
  * a compact recent-emails list
  * collapsible expanders: per-patient urgency, churn risk, demand chart
"""
from __future__ import annotations

import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

st.set_page_config(page_title="Coordinator — Spandan", page_icon="🎯", layout="wide")

from services.auth import gate
gate(required_role="coordinator")

from services import agent, churn, db, delivery, forecasting


# ---------- Auto-start agent + delivery worker once per session ----------
# Pacing presets — single dropdown controls BOTH the agent cycle interval
# (how often we wake up) and the per-donor wait window (how long each
# donor has to reply before we move to the next-ranked donor for the
# SAME patient). Cycle is always shorter than wait so we don't miss
# expiry by more than one tick.
PACING_PRESETS = {
    "Hackathon (5 min per donor)":   {"cycle": 30,   "wait": 300},
    "Realistic (30 min per donor)":  {"cycle": 120,  "wait": 1800},
    "Production (1 hour per donor)": {"cycle": 300,  "wait": 3600},
    "Live demo (1 min per donor)":   {"cycle": 20,   "wait": 60},
    "Off (manual only)":             {"cycle": 0,    "wait": 3600},
}
DEFAULT_PRESET = "Hackathon (5 min per donor)"

if not st.session_state.get("autostarted"):
    preset = PACING_PRESETS[DEFAULT_PRESET]
    try:
        agent.set_donor_wait_seconds(preset["wait"])
    except Exception:
        pass
    try:
        if preset["cycle"] > 0:
            agent.start_background_agent(preset["cycle"])
    except Exception:
        pass
    try:
        delivery.start_delivery_worker(interval=5)
    except Exception:
        pass
    st.session_state["pacing_label"] = DEFAULT_PRESET
    st.session_state["autostarted"] = True


# ---------- Page-wide style ----------
st.markdown(
    """
    <style>
    .pill { display:inline-block; padding: 3px 12px; border-radius: 999px;
            font-size: 12px; font-weight: 600; letter-spacing: 0.3px; }
    .pill-green   { background:#e7f6ec; color:#0a7c34; }
    .pill-red     { background:#fdecec; color:#a52828; }
    .pill-blue    { background:#eef3fc; color:#2a4ea1; }
    .pill-amber   { background:#fff5e2; color:#a06808; }
    .pill-grey    { background:#eeeff2; color:#5a6068; }
    .status-card {
        background: linear-gradient(135deg, #fff 0%, #fbfcfd 100%);
        border: 1px solid #e7e9ed;
        border-left: 4px solid #c0273f;
        border-radius: 12px;
        padding: 18px 22px;
        margin: 6px 0 18px 0;
    }
    .status-row { display:flex; align-items:center; gap: 14px; }
    .status-dot { width: 10px; height: 10px; border-radius: 999px; background:#0a8f3f;
                  box-shadow: 0 0 0 4px rgba(10,143,63,0.15); }
    .status-dot.idle { background:#9aa0a8; box-shadow: 0 0 0 4px rgba(154,160,168,0.15); }
    .status-title { font-size: 15px; font-weight: 600; color:#1a1a1a; }
    .status-sub { font-size: 13px; color: #5a6068; margin-top: 2px; }
    .compact-row { display:flex; justify-content:space-between; align-items:center;
                   padding: 8px 12px; border-bottom: 1px solid #f1f2f5; font-size: 13px; }
    .compact-row:last-child { border-bottom: none; }
    .muted { color:#7a7f87; }
    .h-tag { font-size: 11px; letter-spacing: 1.5px; color:#7a7f87; font-weight:600;
             text-transform:uppercase; margin-bottom: 6px; }
    /* Tighter dataframes */
    [data-testid="stDataFrame"] { border: 1px solid #e7e9ed; border-radius: 10px; }
    </style>
    """,
    unsafe_allow_html=True,
)


# ---------- Header ----------
hdr_l, hdr_r = st.columns([3, 1])
with hdr_l:
    st.markdown(
        "<div><div style='font-size:30px;font-weight:700;color:#c0273f;letter-spacing:-0.5px;'>Spandan · Coordinator</div>"
        "<div style='font-size:12px;color:#7a7f87;letter-spacing:1.5px;'>AUTONOMOUS BLOOD CARE NETWORK</div></div>",
        unsafe_allow_html=True,
    )
with hdr_r:
    mailpit_url = os.getenv("MAILPIT_UI_URL", "http://localhost:8025")
    st.markdown(
        f"<div style='text-align:right;padding-top:8px;'>"
        f"<a href='{mailpit_url}' target='_blank' style='text-decoration:none;background:#eef3fc;color:#2a4ea1;"
        f"padding:8px 14px;border-radius:8px;font-size:13px;font-weight:500;'>📧 Open MailPit inbox</a></div>",
        unsafe_allow_html=True,
    )

st.markdown("<div style='margin: 10px 0 4px 0; height:1px; background:#e7e9ed;'></div>", unsafe_allow_html=True)


# ---------- KPIs ----------
@st.cache_data(ttl=4)
def fetch_kpis() -> dict:
    return {
        "donors": db.count_table("donors"),
        "bridges": db.count_table("bridges"),
        "messages": db.count_table("messages"),
        "donations": db.count_table("donations"),
    }


@st.cache_data(ttl=4)
def fetch_today_counts() -> dict:
    """Count outbound emails + confirmed donations in the last 24 hours.

    We compare against a UTC cutoff (matching how `confirmed_at` and `ts`
    are stored — both produced by datetime.now(timezone.utc).isoformat()).
    Using a sliding 24h window instead of a calendar-day boundary avoids
    the midnight rollover gotcha (where IST evening = previous UTC day,
    so the count would read 0 even right after a confirmation).
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    msgs = db.get_table("messages")
    sent = confirmed = 0
    try:
        resp = msgs.scan(Limit=400)
        for m in resp.get("Items", []):
            ts = m.get("ts") or ""
            if ts >= cutoff and m.get("direction") == "outbound":
                sent += 1
    except Exception:
        pass
    try:
        dons = db.get_table("donations").scan(Limit=400)
        for d in dons.get("Items", []):
            cd = d.get("confirmed_at") or ""
            if cd >= cutoff and d.get("status") == "confirmed":
                confirmed += 1
    except Exception:
        pass
    return {"sent_today": sent, "confirmed_today": confirmed}


@st.fragment(run_every=10)
def render_kpis():
    """Refreshes only the KPI strip every 10s — no full-page dim."""
    kpis = fetch_kpis()
    today = fetch_today_counts()
    k1, k2, k3 = st.columns(3)
    k1.metric("Donors registered", f"{kpis['donors']:,}")
    k2.metric("Patient bridges", f"{kpis['bridges']:,}")
    k3.metric("Donations confirmed (24h)", f"{today['confirmed_today']:,}")


render_kpis()


# ---------- Status card (fragment, refreshes every 2s) ----------
def _fmt_remaining(seconds: int) -> str:
    seconds = max(0, int(seconds or 0))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        m, s = divmod(seconds, 60)
        return f"{m}m {s}s" if s else f"{m}m"
    h, rem = divmod(seconds, 3600)
    m = rem // 60
    return f"{h}h {m}m" if m else f"{h}h"


@st.fragment(run_every=2)
def render_status_card():
    """Only renders when the agent is actively doing something for a
    specific patient (emailing, waiting for reply, just confirmed,
    just declined). Stays silent when idle so the dashboard isn't
    cluttered with 'agent online' chatter."""
    state = agent.get_active_cycle()
    status = state.get("status") or ("active" if state.get("active") else "idle")

    title = sub = None
    if status == "waiting_for_reply" and state.get("donor_name"):
        wait = int(state.get("wait_total_seconds") or 0) or agent.get_donor_wait_seconds()
        try:
            from datetime import datetime as _dt, timezone as _tz
            started = state.get("started")
            if started:
                ts = _dt.fromisoformat(str(started).replace("Z", "+00:00"))
                elapsed = max(0, int((_dt.now(_tz.utc) - ts).total_seconds()))
            else:
                elapsed = int(state.get("elapsed_seconds") or 0)
        except Exception:
            elapsed = int(state.get("elapsed_seconds") or 0)
        remaining = max(0, wait - elapsed)
        title = (
            f"Waiting on <b>{state.get('donor_name')}</b> for "
            f"<b>{state.get('patient', '')}</b>"
        )
        sub = (
            f"Donor was emailed {_fmt_remaining(elapsed)} ago · "
            f"if no reply in <b>{_fmt_remaining(remaining)}</b>, "
            f"the agent will email the next-ranked donor."
        )
    elif status == "emailed_waiting" and state.get("donor_name"):
        wait = int(state.get("wait_total_seconds") or 0) or agent.get_donor_wait_seconds()
        title = (
            f"Just emailed <b>{state.get('donor_name')}</b> "
            f"({str(state.get('language', '')).title()}) for "
            f"<b>{state.get('patient', '')}</b>"
        )
        sub = (
            f"Holding the next-ranked donor for <b>{_fmt_remaining(wait)}</b> "
            f"while we wait for a reply."
        )
    elif status == "confirmed":
        title = (
            f"Confirmed <b>{state.get('donor_name')}</b> for "
            f"<b>{state.get('patient', '')}</b>"
        )
        sub = "First YES wins · agent stops asking for this patient."
    elif status == "already_covered":
        title = "Patient already covered"
        sub = (
            f"<b>{state.get('donor_name')}</b> said YES but a confirmation "
            f"already exists — sent a polite 'thanks, we're covered' note."
        )

    if title is None:
        # Idle / paused / between cycles — render nothing.
        return

    st.markdown(
        f"""
        <div class='status-card'>
          <div class='status-row'>
            <div class='status-dot'></div>
            <div>
              <div class='status-title'>{title}</div>
              <div class='status-sub'>{sub}</div>
            </div>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


render_status_card()


# ---------- Action bar + Modes ----------
act_l, act_r = st.columns([3, 1])

with act_l:
    st.markdown(
        "<div class='h-tag'>Actions <span style='text-transform:none;letter-spacing:0;"
        "font-weight:400;color:#7a7f87;margin-left:8px;'>· optional manual overrides — "
        "the agent runs autonomously on the schedule shown to the right</span></div>",
        unsafe_allow_html=True,
    )
    a1, a2, a3 = st.columns(3)

    if a1.button("▶ Advance all patients", type="primary", use_container_width=True,
                 help="Advance every patient in the pipeline by one step "
                      "(email next donor, OR keep waiting, OR confirm, OR escalate)"):
        pl = agent.patient_pipeline(days=90)
        targets = [r for r in pl if r["status"] in ("Pending", "In progress", "Escalated")]
        if not targets:
            st.toast("No actionable patients in the pipeline.", icon="ℹ️")
        else:
            with st.spinner(f"Advancing {len(targets)} patient(s) ..."):
                summary: dict[str, int] = {}
                for r in targets:
                    res = agent.run_agent_cycle(target_bridge_id=r["bridge_id"])
                    rkey = str(res.get("result", "other"))
                    summary[rkey] = summary.get(rkey, 0) + 1
            parts = []
            order = [
                ("next_donor_emailed", "emailed next donor"),
                ("confirmed", "confirmed YES"),
                ("waiting", "still in wait window"),
                ("already_covered", "thanked (already covered)"),
                ("question_answered", "answered question"),
                ("surge_dispatched", "surge blasts"),
                ("escalated", "escalated"),
                ("all_in_cooldown", "all donors in cooldown"),
            ]
            for k, label in order:
                if summary.get(k):
                    parts.append(f"{summary[k]} {label}")
            msg = "Done · " + (" · ".join(parts) if parts else "no changes")
            st.success(msg)

    if a2.button("📨 Process inbox", use_container_width=True,
                 help="Classify any donor replies that arrived after the original cycle window"):
        with st.spinner("Classifying replies via Bedrock ..."):
            processed = agent.process_pending_replies()
        if processed:
            yes = sum(1 for p in processed if p.get("intent") == "YES")
            cancel = sum(1 for p in processed if p.get("intent") == "CANCEL")
            qn = sum(1 for p in processed if p.get("intent") == "QUESTION")
            st.success(f"Processed {len(processed)} reply/replies · {yes} confirmed · {cancel} cancelled (re-enqueued) · {qn} questions answered.")
        else:
            st.toast("No new replies to process.", icon="ℹ️")

    if a3.button("📧 Email next donor now", use_container_width=True,
                 help="Manual demo trigger: runs ONE agent cycle right now, "
                      "which picks the most-urgent patient in the pipeline "
                      "and emails their #1-ranked donor. No seeding — uses "
                      "real patients from the dataset."):
        with st.spinner("Picking most-urgent patient and emailing donor #1 ..."):
            cycle_res = agent.run_agent_cycle()

        sub = cycle_res.get("results") or [cycle_res]
        emailed = [r for r in sub if r.get("result") == "next_donor_emailed"]
        confirmed = [r for r in sub if r.get("result") == "confirmed"]
        waiting = [r for r in sub if r.get("result") == "waiting"]
        no_demand = any(r.get("result") == "no_demand" for r in sub)

        if emailed:
            r0 = emailed[0]
            st.success(
                f"📧 Emailed **{r0.get('donor_name', 'donor')}** for patient "
                f"**{r0.get('patient_name', '?')}** — check MailPit at "
                f"http://localhost:8025."
            )
            st.cache_data.clear()
        elif confirmed:
            r0 = confirmed[0]
            st.success(
                f"✓ Already confirmed: **{r0.get('donor_name', 'donor')}** "
                f"said YES for patient **{r0.get('patient_name', '?')}**."
            )
            st.cache_data.clear()
        elif waiting:
            r0 = waiting[0]
            st.info(
                f"⏳ Most-urgent patient **{r0.get('patient_name', '?')}** is "
                f"waiting on a reply from **{r0.get('donor_name', 'donor')}**. "
                f"Agent will email the next donor when the window closes."
            )
        elif no_demand:
            st.warning(
                "Pipeline is empty — no patients due in the next 90 days. "
                "Make sure the dataset is loaded "
                "(`python data/load_dataset.py`)."
            )
        else:
            code = str(cycle_res.get("result") or "no_change")
            st.info(f"Cycle result: `{code}` — nothing to email this tick.")

with act_r:
    st.markdown("<div class='h-tag'>Agent</div>", unsafe_allow_html=True)

    @st.fragment(run_every=2)
    def render_agent_pulse():
        """Tiny live indicator so 'is the agent running?' is never a question.
        Refreshes every 2s with last-cycle / next-cycle ETA."""
        s = agent.agent_status()
        if s["running"]:
            cyc = s.get("cycle_interval_seconds") or 0
            ago = s.get("last_cycle_seconds_ago")
            nxt = s.get("seconds_until_next")
            if ago is None:
                detail = f"Booting · first cycle in ≤{cyc}s"
            else:
                last_part = "just now" if ago < 2 else f"{ago}s ago"
                next_part = ("now" if (nxt or 0) <= 1 else f"in {nxt}s")
                detail = f"Last cycle {last_part} · next {next_part}"
            dot = "#0a8f3f"
            label = "AGENT ON"
            sub = detail
        else:
            dot = "#9aa0a8"
            label = "AGENT OFF"
            sub = "Restart the app to resume autonomous outreach."

        st.markdown(
            f"""
            <div style='display:flex;align-items:center;gap:10px;
                        background:#f8f9fb;border:1px solid #e7e9ed;
                        border-radius:10px;padding:10px 14px;'>
              <div style='width:9px;height:9px;border-radius:999px;
                          background:{dot};box-shadow:0 0 0 4px {dot}22;
                          flex:0 0 auto;'></div>
              <div style='display:flex;flex-direction:column;'>
                <span style='font-size:11px;font-weight:700;
                             letter-spacing:1.5px;color:#1a1a1a;'>{label}</span>
                <span style='font-size:11px;color:#5a6068;'>{sub}</span>
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    render_agent_pulse()


# ---------- Patient pipeline (fragment, refreshes every 8s) ----------
def _when_pill(days: int) -> str:
    if days < 0:
        return f"<span class='pill pill-red'>OVERDUE {abs(days)}d</span>"
    if days == 0:
        return "<span class='pill pill-red'>TODAY</span>"
    if days <= 2:
        return f"<span class='pill pill-amber'>+{days}d</span>"
    return f"<span class='pill pill-grey'>+{days}d</span>"


def _email_status_cell(r: dict) -> str:
    """One cell that summarizes the donor outreach state for this patient.

    NOT_SENT      -> grey  "Not contacted"
    AWAITING      -> blue  "Awaiting <donor name>"
    WAIT_CLOSED   -> amber "Wait closed" (clean — name omitted)
    CONFIRMED     -> green "Confirmed by <donor>"
    ESCALATED     -> red   "Escalated"
    """
    contacted = int(r.get("donors_contacted") or 0)
    status = (r.get("status") or "").lower()
    confirmed_by = r.get("last_confirmed_donor")
    last_donor = r.get("last_donor_emailed")
    wait_remaining = r.get("wait_remaining_seconds")

    if "confirmed" in status and confirmed_by:
        return (f"<span class='pill pill-green'>CONFIRMED</span> "
                f"&nbsp;<span style='color:#1a1a1a;'>by <b>{confirmed_by}</b></span>")
    if "escalat" in status:
        return f"<span class='pill pill-red'>ESCALATED</span>"
    if "progress" in status or contacted > 0:
        if wait_remaining is not None and last_donor:
            return (f"<span class='pill pill-blue'>AWAITING</span> "
                    f"&nbsp;<span style='color:#1a1a1a;'><b>{last_donor}</b></span>")
        if last_donor:
            return f"<span class='pill pill-amber'>WAIT CLOSED</span>"
        return f"<span class='pill pill-blue'>AWAITING</span>"
    return f"<span class='pill pill-grey'>NOT CONTACTED</span>"


@st.fragment(run_every=8)
def render_pipeline():
    st.markdown("<div class='h-tag' style='margin-top: 22px;'>Patient pipeline (next 90 days)</div>", unsafe_allow_html=True)
    try:
        pipeline = agent.patient_pipeline(days=90)
    except Exception as exc:
        # Most common cause: AWS rejected the SigV4 signature because
        # the laptop clock drifted (Mac sleep/wake without NTP resync).
        # We've installed an auto-correcting retry inside
        # services.db.retry_on_clock_skew, so the next refresh should
        # succeed. Show a friendly note instead of a Python traceback.
        msg = str(exc)
        if "InvalidSignature" in msg or "Signature expired" in msg:
            st.warning(
                "Synchronizing with AWS — your laptop clock drifted. "
                "Retrying automatically; the pipeline will reload in a "
                "few seconds."
            )
        else:
            st.warning(f"Backend hiccup: `{type(exc).__name__}`. Retrying ...")
        return
    if not pipeline:
        st.info("No upcoming demand. Click ▶ Run all pending or change cadence.")
        return

    rows_html = []
    for r in pipeline[:25]:
        rows_html.append(
            "<tr>"
            f"<td style='padding:10px 14px;'>{_when_pill(int(r['days_until']))}</td>"
            f"<td style='padding:10px 14px;font-weight:500;'>{r['patient']}</td>"
            f"<td style='padding:10px 14px;color:#c0273f;font-weight:600;'>{r['blood_group']}</td>"
            f"<td style='padding:10px 14px;'>{r['hospital']}</td>"
            f"<td style='padding:10px 14px;'>{_email_status_cell(r)}</td>"
            "</tr>"
        )
    st.markdown(
        "<table style='width:100%;border-collapse:separate;border-spacing:0;border:1px solid #e7e9ed;border-radius:12px;overflow:hidden;font-size:13px;'>"
        "<thead><tr style='background:#f8f9fb;color:#5a6068;text-align:left;font-size:11px;letter-spacing:1px;'>"
        "<th style='padding:10px 14px;'>WHEN</th>"
        "<th style='padding:10px 14px;'>PATIENT</th>"
        "<th style='padding:10px 14px;'>BLOOD</th>"
        "<th style='padding:10px 14px;'>HOSPITAL</th>"
        "<th style='padding:10px 14px;'>EMAIL STATUS</th>"
        "</tr></thead><tbody>"
        + "".join(rows_html)
        + "</tbody></table>",
        unsafe_allow_html=True,
    )

    df_p = pd.DataFrame(pipeline)
    confirmed = (df_p["status"] == "Confirmed").sum()
    in_progress = (df_p["status"] == "In progress").sum()
    pending = (df_p["status"] == "Pending").sum()
    escalated = (df_p["status"] == "Escalated").sum()
    st.markdown(
        f"""
        <div style='display:flex;gap:10px;margin-top:10px;font-size:12px;'>
          <span class='pill pill-green'>{int(confirmed)} confirmed</span>
          <span class='pill pill-blue'>{int(in_progress)} awaiting</span>
          <span class='pill pill-amber'>{int(pending)} not contacted</span>
          <span class='pill pill-red'>{int(escalated)} escalated</span>
        </div>
        """,
        unsafe_allow_html=True,
    )


render_pipeline()


# ---------- Emergency surge action ----------
st.markdown("<div style='height: 22px;'></div>", unsafe_allow_html=True)

with st.expander("🚨 Emergency: send to ALL donors at once", expanded=False):
    st.markdown(
        "<div style='background:#fdecec;border:1px solid #f0b9b9;border-radius:10px;"
        "padding:10px 14px;margin-bottom:12px;font-size:13px;color:#1a1a1a;'>"
        "Use only in emergencies — emails every eligible donor for the "
        "selected patient at once."
        "</div>",
        unsafe_allow_html=True,
    )

    pl = agent.patient_pipeline(days=90)
    if not pl:
        st.info("No upcoming patients — nothing to surge.")
    else:
        names = [f"{r['patient']} · {r['blood_group']} · +{r['days_until']}d"
                 for r in pl]
        col_p, col_b = st.columns([3, 1])
        idx = col_p.selectbox(
            "Patient", range(len(pl)),
            format_func=lambda i: names[i], key="surge_patient_picker",
            label_visibility="collapsed",
        )
        target = pl[idx]

        if col_b.button("🚨 Send to all donors", type="primary",
                        use_container_width=True, key="surge_send_btn"):
            try:
                # Mark surge mode on the bridge (so process_pending_replies
                # applies the over-confirm guard correctly), then dispatch
                # IMMEDIATELY via run_surge() — this bypasses the agent's
                # cycle lock so the emergency button always works, even if
                # the autonomous cycle is mid-run.
                db.get_table("bridges").update_item(
                    Key={"bridge_id": target["bridge_id"]},
                    UpdateExpression=(
                        "SET urgency_mode = :m, confirmation_target = :t"
                    ),
                    ExpressionAttributeValues={":m": "surge", ":t": 1},
                )
                with st.spinner(f"Surging {target['patient']} ..."):
                    res = agent.run_surge(target["bridge_id"])
                dispatched = int(res.get("dispatched") or 0)
                if res.get("result") == "surge_dispatched":
                    if dispatched:
                        st.success(
                            f"🚨 Emergency surge for **{target['patient']}** "
                            f"({target['blood_group']}) — emailed "
                            f"**{dispatched}** donor(s) in parallel."
                        )
                    else:
                        st.info(
                            f"No fresh donors to email for {target['patient']} — "
                            f"every eligible donor has either already been "
                            f"contacted for this patient or is in the global "
                            f"cooldown / recently-confirmed list."
                        )
                elif res.get("result") == "already_covered":
                    st.warning(
                        f"{target['patient']} is already covered "
                        f"(confirmation already exists). No new emails sent."
                    )
                elif res.get("result") == "all_in_cooldown":
                    st.warning(
                        f"All eligible donors for {target['patient']} are in "
                        f"cooldown (recently contacted within "
                        f"{int(os.getenv('AGENT_COOLDOWN_HOURS', '2'))}h). "
                        f"Try again later."
                    )
                else:
                    code = str(res.get("result") or "")
                    pretty = {
                        "no_donors":
                            f"No eligible donors found for "
                            f"{target['patient']} ({target['blood_group']}).",
                        "no_ranked":
                            f"No ranked donors for {target['patient']}.",
                        "no_upcoming":
                            "No upcoming patients in the pipeline.",
                        "skipped_concurrent":
                            "Another agent cycle was already running — "
                            "the surge has been queued.",
                        "no_bridge":
                            "Could not find this patient in the database.",
                    }.get(code, f"Surge complete · awaiting donor replies.")
                    st.info(pretty)
            except Exception as e:
                st.error(f"Surge failed: {e}")

with st.expander("Demand by day (next 90 days)"):
    hist = forecasting.demand_histogram(days=90)
    df_h = pd.DataFrame(hist)
    if df_h.empty:
        st.info("Not enough data.")
    else:
        fig = px.bar(df_h, x="date", y="count",
                     labels={"date": "Date", "count": "Patients needing blood"})
        fig.update_layout(height=240, margin=dict(t=10, b=10, l=0, r=0))
        st.plotly_chart(fig, use_container_width=True)


# Auto-refresh is now handled per-fragment via @st.fragment(run_every=...).
# That keeps the rest of the page perfectly still — no full-page dim, no
# session_state churn, no auth flicker.
