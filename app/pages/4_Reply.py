"""Magic-link reply page.

When a donor clicks YES / NO / QUESTION inside their email, the link opens
this page with token + donor_id + bridge_id + ts + intent in the query
string. We verify the token (HMAC), record the inbound message in
DynamoDB, and immediately ask the agent's process_pending_replies()
handler to classify and act.

This page deliberately has no auth — the token is the auth.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services import agent, db, delivery

st.set_page_config(page_title="Spandan · Reply", page_icon="🩸", layout="centered")


# Hide the sidebar nav so this looks like a one-shot landing page,
# not part of an internal dashboard.
st.markdown(
    """
    <style>
    [data-testid="stSidebar"] {display: none;}
    [data-testid="collapsedControl"] {display: none;}
    .reply-card {
        background: #fff;
        border-radius: 14px;
        padding: 36px 40px;
        box-shadow: 0 4px 18px rgba(0,0,0,0.06);
        max-width: 540px;
        margin: 24px auto;
    }
    .reply-header {
        background: linear-gradient(135deg,#c0273f,#8a1c2c);
        color: #fff;
        padding: 22px 32px;
        border-radius: 14px 14px 0 0;
        margin: -36px -40px 24px -40px;
    }
    .pill { display:inline-block;padding:4px 10px;border-radius:999px;font-size:12px;font-weight:600;margin-right:6px; }
    .pill-yes { background:#e7f6ec;color:#0a7c34; }
    .pill-no { background:#fdecec;color:#a52828; }
    </style>
    """,
    unsafe_allow_html=True,
)


def _qparam(name: str) -> str:
    val = st.query_params.get(name, "")
    if isinstance(val, list):
        val = val[0] if val else ""
    return str(val or "")


donor_id = _qparam("donor_id")
bridge_id = _qparam("bridge_id")
ts = _qparam("ts")
token = _qparam("token")
intent = (_qparam("intent") or "").upper()

if not donor_id or not ts or not token or not intent:
    st.markdown('<div class="reply-card">', unsafe_allow_html=True)
    st.markdown('<div class="reply-header"><h2 style="margin:0;">Spandan · Blood Warriors</h2></div>', unsafe_allow_html=True)
    st.error("This reply link is incomplete. Please use the buttons inside the email we sent you.")
    st.markdown('</div>', unsafe_allow_html=True)
    st.stop()

if not delivery.verify_reply_token(donor_id, bridge_id, ts, token):
    st.markdown('<div class="reply-card">', unsafe_allow_html=True)
    st.markdown('<div class="reply-header"><h2 style="margin:0;">Spandan · Blood Warriors</h2></div>', unsafe_allow_html=True)
    st.error("Sorry — this reply link looks tampered with or has expired. Please contact the coordinator.")
    st.markdown('</div>', unsafe_allow_html=True)
    st.stop()


# Resolve donor + bridge for context
donor = db.get_table("donors").get_item(Key={"user_id": donor_id}).get("Item") or {}
bridge = (db.get_table("bridges").get_item(Key={"bridge_id": bridge_id}).get("Item") or {}) if bridge_id else {}

donor_name = donor.get("name") or "Donor"
language = (donor.get("preferred_language") or "english").lower()


# Map intent -> message body. Only YES / NO are valid magic-link
# intents; anything else (legacy QUESTION links, malformed clicks) is
# coerced to NO so the agent can move on cleanly. Donors with questions
# call the helpline numbers in the email instead.
INTENT_TEXT = {
    "YES": "YES, I will donate.",
    "NO": "Sorry, I cannot make it this time.",
}
INTENT_PILL = {
    "YES": ("pill pill-yes", "Confirmed"),
    "NO": ("pill pill-no", "Declined"),
}
if intent not in INTENT_TEXT:
    intent = "NO"


# Persist the answer once per token+intent combo (prevent double-submit on
# refresh)
state_key = f"submitted_{token}_{intent}"
if not st.session_state.get(state_key):
    body = INTENT_TEXT.get(intent, intent)
    agent.simulate_donor_reply(
        donor_id=donor_id,
        bridge_id=bridge_id,
        donor_name=donor_name,
        language=language,
        text=body,
    )
    try:
        agent.process_pending_replies(max_replies=2)
    except Exception:
        pass
    st.session_state[state_key] = True


# Render confirmation card
pill_cls, pill_label = INTENT_PILL.get(intent, ("pill", intent))

st.markdown('<div class="reply-card">', unsafe_allow_html=True)
st.markdown('<div class="reply-header"><h2 style="margin:0;font-weight:600;">Spandan · Blood Warriors</h2><div style="opacity:0.85;font-size:13px;letter-spacing:1px;margin-top:4px;">YOUR REPLY HAS BEEN RECORDED</div></div>', unsafe_allow_html=True)
st.markdown(f'<div style="margin-bottom:8px;"><span class="{pill_cls}">{pill_label}</span></div>', unsafe_allow_html=True)

if intent == "YES":
    st.markdown(f"### Thank you, **{donor_name}**")
    if bridge:
        st.write(
            f"Your confirmation for **{bridge.get('patient_name','—')}** "
            f"({bridge.get('blood_group','—')}) is recorded."
        )
        st.markdown("##### What you need to know")
        st.markdown(
            f"""
            - **Hospital:** {bridge.get('hospital','—')}
            - **Reach the donation desk** at the blood bank — ground floor reception will direct you.
            - **Carry a government-issued photo ID** (Aadhaar, PAN, or driving licence).
            - **Eat well and stay hydrated** for 24 hours before donating; avoid alcohol.
            - **Time needed:** about 30 minutes (10 for actual donation, 20 for paperwork & rest).
            """
        )
        st.markdown("##### If you have any doubts")
        st.markdown(
            """
            <div style='background:#f8f9fb;border:1px solid #e7e9ed;border-radius:10px;padding:12px 16px;font-size:14px;'>
              <div style='display:flex;justify-content:space-between;'>
                <span><b>📞 Helpline 1</b><br><span style='color:#7a7f87;'>Mon–Sat, 8 AM–8 PM</span></span>
                <span style='font-family:monospace;font-weight:600;color:#c0273f;'>+91&nbsp;90100&nbsp;48485</span>
              </div>
              <hr style='border:none;border-top:1px solid #eee;margin:8px 0;'>
              <div style='display:flex;justify-content:space-between;'>
                <span><b>📞 Helpline 2</b> (urgent)<br><span style='color:#7a7f87;'>24×7 emergency</span></span>
                <span style='font-family:monospace;font-weight:600;color:#c0273f;'>+91&nbsp;78934&nbsp;21156</span>
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    else:
        st.write("Your confirmation is recorded.")
elif intent == "NO":
    st.markdown(f"### Thank you for letting us know, **{donor_name}**.")
    st.write(
        "No problem — Spandan will reach out to another donor right away. "
        "We appreciate your past contributions and will get in touch for the next request."
    )
    st.markdown(
        """
        <div style='background:#f8f9fb;border:1px solid #e7e9ed;border-radius:10px;padding:12px 16px;font-size:13px;color:#5a6068;margin-top:12px;'>
          If you actually wanted to ask a question, please call our helpline:
          <b style='color:#c0273f;'>+91 90100 48485</b> (Mon–Sat, 8 AM–8 PM).
        </div>
        """,
        unsafe_allow_html=True,
    )

st.markdown(
    "<div style='margin-top:24px;font-size:12px;color:#9aa0a8;text-align:center;'>"
    "Spandan AI Coordinator · Blood Warriors · Hyderabad, India"
    "</div>",
    unsafe_allow_html=True,
)
st.markdown('</div>', unsafe_allow_html=True)
