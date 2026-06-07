"""Bridge / Patient View — drill-in audit & timeline.

For one patient bridge, show:
  * patient demographics + predicted next transfusion
  * timeline visual: past donations + predicted next + outreach attempts
  * donation history (audit trail)
  * full agent log scoped to this bridge
  * ranked donor pool
  * 'Run agent for THIS patient' button
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from boto3.dynamodb.conditions import Key

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

st.set_page_config(page_title="Bridge — Spandan", page_icon="🩺", layout="wide")

from services.auth import gate
gate(required_role="coordinator")

from services import agent, db, forecasting, ranking

st.markdown("# Patient bridge")
st.caption("Drill into a single patient: their transfusion history, the agent's complete decision audit trail, and ranked donor pool.")


@st.cache_data(ttl=60)
def list_bridges() -> list[dict]:
    table = db.get_table("bridges")
    items: list[dict] = []
    response = table.scan(Limit=300)
    items.extend(response.get("Items", []))
    while "LastEvaluatedKey" in response:
        response = table.scan(ExclusiveStartKey=response["LastEvaluatedKey"], Limit=300)
        items.extend(response.get("Items", []))
    return items


bridges = list_bridges()
if not bridges:
    st.warning("No bridges in DynamoDB. Run the dataset loader.")
    st.stop()

# Sort bridges by urgency for easy demo (most urgent first)
def _days_until(b: dict) -> int:
    p = forecasting.predict_next_transfusion(b)
    if p is None:
        return 9999
    return (p - date.today()).days


bridges_sorted = sorted(bridges, key=_days_until)

labels = []
for b in bridges_sorted:
    du = _days_until(b)
    when = "OVERDUE" if du < 0 else ("TODAY" if du == 0 else f"+{du}d")
    labels.append(
        f"[{when:<7}] {b.get('patient_name','?')} · {b.get('blood_group','')} · age {int(b.get('patient_age') or 0)} · {b.get('hospital','')}"
    )

selected_idx = st.selectbox(
    f"Pick a patient bridge ({len(bridges_sorted)} total — sorted by urgency)",
    range(len(bridges_sorted)),
    format_func=lambda i: labels[i],
)
bridge = bridges_sorted[selected_idx]
bridge_id = bridge["bridge_id"]


# ---------- Header KPIs ----------
predicted = forecasting.predict_next_transfusion(bridge)
days_until = (predicted - date.today()).days if predicted else None
last_donor = bridge.get("last_confirmed_donor")
last_at = bridge.get("last_confirmed_at")

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Patient", bridge.get("patient_name", "—"))
c2.metric("Age", int(bridge.get("patient_age") or 0))
c3.metric("Blood group", bridge.get("blood_group", "—"))
c4.metric("Hospital", bridge.get("hospital", "—"))
when = (
    "OVERDUE" if days_until is not None and days_until < 0
    else (f"+{days_until}d" if days_until is not None else "—")
)
c5.metric("Next transfusion", predicted.isoformat() if predicted else "—", delta=when)

if last_donor:
    st.success(f"Most recent confirmed donor: **{last_donor}** at {(last_at or '')[:19].replace('T', ' ')}")

st.divider()


# ---------- Timeline ----------
@st.cache_data(ttl=15)
def fetch_donations_for(bid: str) -> list[dict]:
    table = db.get_table("donations")
    items: list[dict] = []
    resp = table.scan(Limit=500)
    items.extend([d for d in resp.get("Items", []) if d.get("bridge_id") == bid])
    items.sort(key=lambda d: d.get("scheduled_date", ""))
    return items


@st.cache_data(ttl=15)
def fetch_outreach(bid: str) -> list[dict]:
    msgs_table = db.get_table("messages")
    items: list[dict] = []
    resp = msgs_table.scan(Limit=500)
    items.extend([m for m in resp.get("Items", []) if m.get("bridge_id") == bid])
    items.sort(key=lambda x: x.get("ts", ""))
    return items


@st.cache_data(ttl=15)
def fetch_agent_log_for(bid: str) -> list[dict]:
    log_table = db.get_table("agent_log")
    items: list[dict] = []
    resp = log_table.scan(Limit=500)
    items.extend([it for it in resp.get("Items", []) if it.get("bridge_id") == bid])
    items.sort(key=lambda x: x.get("ts", ""))
    return items


donations = fetch_donations_for(bridge_id)
outreach = fetch_outreach(bridge_id)
agent_log_rows = fetch_agent_log_for(bridge_id)


st.subheader("Transfusion timeline")
fig = go.Figure()

# Past last_transfusion
last_tx = bridge.get("last_transfusion_date")
if last_tx:
    try:
        d = datetime.strptime(str(last_tx)[:10], "%Y-%m-%d").date()
        fig.add_trace(go.Scatter(
            x=[d], y=["Transfusions"], mode="markers+text",
            marker=dict(size=18, color="#7a7f87", symbol="circle"),
            text=["Last"], textposition="top center",
            name="Last transfusion (recorded)", showlegend=False,
        ))
    except Exception:
        pass

# Confirmed donations (past + upcoming scheduled)
for don in donations:
    sched = don.get("scheduled_date", "")
    try:
        d = datetime.strptime(str(sched)[:10], "%Y-%m-%d").date()
    except Exception:
        continue
    color = "#0a8f3f" if don.get("status") == "confirmed" else "#7a7f87"
    fig.add_trace(go.Scatter(
        x=[d], y=["Transfusions"], mode="markers+text",
        marker=dict(size=20, color=color, symbol="diamond"),
        text=[(don.get("donor_name") or "")[:14]], textposition="bottom center",
        showlegend=False,
        hovertemplate=f"Donor: {don.get('donor_name','?')}<br>Status: {don.get('status','?')}<extra></extra>",
    ))

# Predicted next
if predicted:
    fig.add_trace(go.Scatter(
        x=[predicted], y=["Transfusions"], mode="markers+text",
        marker=dict(size=22, color="#c0273f", symbol="star"),
        text=["Predicted next"], textposition="top center",
        showlegend=False,
        hovertemplate=f"Predicted: {predicted}<extra></extra>",
    ))

# Today reference line
fig.add_vline(x=date.today(), line_dash="dot", line_color="#9aa0a8")

# Outreach markers (small dots above the line)
for m in outreach[-30:]:
    ts = m.get("ts", "")
    try:
        d = datetime.strptime(ts[:10], "%Y-%m-%d").date()
    except Exception:
        continue
    fig.add_trace(go.Scatter(
        x=[d], y=["Outreach"], mode="markers",
        marker=dict(size=8, color=("#2a4ea1" if m.get("direction") == "outbound" else "#0a8f3f")),
        showlegend=False,
        hovertemplate=f"{m.get('direction','?')}: {m.get('donor_name','?')}<extra></extra>",
    ))

fig.update_layout(
    height=240, margin=dict(t=20, b=20, l=10, r=10),
    yaxis=dict(categoryorder="array", categoryarray=["Outreach", "Transfusions"]),
    xaxis_title="", yaxis_title="",
)
st.plotly_chart(fig, use_container_width=True)

st.divider()


# ---------- Three-column drill-in ----------
left, mid, right = st.columns([1, 1, 1])

with left:
    st.subheader("Donation history")
    if donations:
        rows = []
        for d in donations:
            rows.append({
                "Date": str(d.get("scheduled_date", ""))[:10],
                "Donor": d.get("donor_name") or "—",
                "Status": d.get("status", "—"),
                "Source": d.get("source", "agent"),
                "Confirmed at": (d.get("confirmed_at") or "")[:19].replace("T", " "),
            })
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True, height=300)
    else:
        st.info("No donations yet for this patient.")

with mid:
    st.subheader("Agent decision log")
    if agent_log_rows:
        rows = []
        for it in agent_log_rows[-50:]:
            extras = " · ".join(
                f"{k}={str(v)[:30]}"
                for k, v in it.items()
                if k not in {"cycle_id", "ts", "action", "bridge_id"}
            )
            rows.append({
                "Time": (it.get("ts") or "")[:19].replace("T", " "),
                "Action": it.get("action", "—"),
                "Detail": extras[:80],
            })
        st.dataframe(pd.DataFrame(rows[::-1]), hide_index=True, use_container_width=True, height=300)
    else:
        st.info("No agent activity yet for this patient.")

with right:
    st.subheader("Outreach messages")
    if outreach:
        rows = []
        for m in outreach[-30:][::-1]:
            rows.append({
                "Time": (m.get("ts") or "")[:19].replace("T", " "),
                "Dir": m.get("direction", ""),
                "Donor": m.get("donor_name") or "—",
                "Lang": str(m.get("language", "")).title(),
                "Status": m.get("delivery_status", "—"),
            })
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True, height=300)
    else:
        st.info("No outreach yet.")

st.divider()


# ---------- Donor pool ranked by ML ----------
st.subheader("Donor pool — ranked by ML")

@st.cache_data(ttl=120)
def hydrate_pool(b_serialised: dict) -> list[dict]:
    ids = b_serialised.get("donor_pool") or []
    donors_table = db.get_table("donors")
    out = []
    for uid in list(ids)[:60]:
        try:
            resp = donors_table.get_item(Key={"user_id": uid})
            if "Item" in resp:
                out.append(resp["Item"])
        except Exception:
            pass
    if len(out) < 8 and b_serialised.get("blood_group"):
        try:
            resp = donors_table.query(
                IndexName="blood_group-index",
                KeyConditionExpression=Key("blood_group").eq(b_serialised["blood_group"]),
                Limit=60,
            )
            out.extend(resp.get("Items", []))
        except Exception:
            pass
    seen = set()
    uniq = []
    for d in out:
        u = d.get("user_id")
        if u and u not in seen:
            seen.add(u)
            uniq.append(d)
    return uniq


pool = hydrate_pool({"donor_pool": list(bridge.get("donor_pool") or []), "blood_group": bridge.get("blood_group")})

if pool:
    ranked = ranking.rank_for_bridge(bridge, pool, top_n=10)
    rows = []
    for i, d in enumerate(ranked, start=1):
        rows.append({
            "Rank": i,
            "Donor": d.get("name"),
            "Blood": d.get("blood_group"),
            "Language": str(d.get("preferred_language", "")).title(),
            "Donations": int(d.get("donations_till_date") or 0),
            "Eligibility": d.get("eligibility_status", "—"),
            "Score": round(float(d.get("score", 0.0)), 3),
            "Email": d.get("email", "—"),
        })
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True, height=320)
else:
    st.info("No donors matched for this blood group.")

st.divider()


# ---------- Run agent for THIS patient ----------
if st.button(
    f"Run agent for {bridge.get('patient_name', 'this patient')}",
    type="primary",
    use_container_width=True,
    help="Force a cycle scoped to this specific bridge",
):
    with st.spinner("Agent working — forecasting → ranking → Bedrock → email send → wait → escalate ..."):
        result = agent.run_agent_cycle(target_bridge_id=bridge_id)
    if result["result"] == "confirmed":
        st.success(f"Confirmed! {result.get('donor_name')} agreed for {bridge.get('patient_name')}.")
    elif result["result"] == "escalated":
        st.warning(f"Tried {result.get('tried', 0)} donors — none responded. Logged for human follow-up.")
    elif result["result"] == "no_donors":
        st.info("Donor pool exhausted for this bridge (likely all in 24h cooldown).")
    else:
        st.info(f"Result: {result['result']}")
    st.cache_data.clear()
    st.rerun()
