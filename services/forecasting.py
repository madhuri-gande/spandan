"""Demand forecasting.

For each bridge (patient), predict the next transfusion date based on:
  - expected_next_transfusion_date if present
  - else last_transfusion_date + frequency_in_days
  - else fallback to a default frequency (90 days)

Also exposes upcoming_demand(days=N) which returns bridges with predicted
next-transfusion within the next N days (sorted by urgency). This drives
the agent's "what to act on next" decision.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

from services import db


def _parse_date(s) -> Optional[date]:
    if not s or not isinstance(s, str):
        return None
    s = s.strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s).date()
    except Exception:
        return None


def _safe_int(v, default=90) -> int:
    try:
        if v is None:
            return default
        n = int(float(v))
        return n if n > 0 else default
    except Exception:
        return default


def predict_next_transfusion(bridge: dict, today: Optional[date] = None) -> Optional[date]:
    """Predict the next transfusion date.

    The training dataset is historical (2025), so expected/last dates may be
    in the past. We project forward by the donor's cadence until we land on
    a date >= today. This makes the dashboard show realistic upcoming events.
    """
    today = today or date.today()
    cadence = _safe_int(bridge.get("frequency_in_days") or bridge.get("cycle_of_donations"), 90)

    expected = _parse_date(bridge.get("expected_next_transfusion_date"))
    last = _parse_date(bridge.get("last_transfusion_date"))

    candidate = expected or (last + timedelta(days=cadence) if last else None)
    if candidate is None:
        return None

    while candidate < today:
        candidate = candidate + timedelta(days=cadence)
    return candidate


def fetch_all_bridges() -> list[dict]:
    table = db.get_table("bridges")
    items: list[dict] = []
    response = table.scan()
    items.extend(response.get("Items", []))
    while "LastEvaluatedKey" in response:
        response = table.scan(ExclusiveStartKey=response["LastEvaluatedKey"])
        items.extend(response.get("Items", []))
    return items


def upcoming_demand(days: int = 90, today: Optional[date] = None) -> list[dict]:
    """Return list of {bridge_id, blood_group, predicted_next_date, days_until, ...}
    sorted by days_until ascending. Includes overdue bridges (negative days_until).

    Bridges that already have a confirmed donation in the last 24 hours are
    excluded — the agent has already served them, so it should move on to the
    next most urgent patient instead of re-queuing the same one.
    """
    today = today or date.today()
    bridges = fetch_all_bridges()
    served_recently = _bridges_with_recent_confirmation()
    upcoming = []
    for b in bridges:
        if b.get("bridge_id") in served_recently:
            continue
        predicted = predict_next_transfusion(b, today=today)
        if predicted is None:
            continue
        delta = (predicted - today).days
        if delta > days:
            continue
        upcoming.append({
            "bridge_id": b.get("bridge_id"),
            "blood_group": b.get("blood_group"),
            "predicted_next_date": predicted.isoformat(),
            "days_until": delta,
            "patient_name": b.get("patient_name"),
            "patient_age": int(b.get("patient_age") or 0),
            "hospital": b.get("hospital"),
            "donor_pool_size": int(b.get("donor_pool_size") or 0),
            "last_confirmed_donor": b.get("last_confirmed_donor"),
        })
    upcoming.sort(key=lambda x: x["days_until"])
    return upcoming


def _bridges_with_recent_confirmation(hours: int = 24) -> set[str]:
    """Set of bridge_ids that received a confirmed donation within the
    last `hours`. Used to skip patients the agent has already served."""
    cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
    served: set[str] = set()
    try:
        table = db.get_table("donations")
        scan_kwargs = {
            "FilterExpression": "confirmed_at >= :c",
            "ExpressionAttributeValues": {":c": cutoff},
            "ProjectionExpression": "bridge_id",
        }
        while True:
            resp = table.scan(**scan_kwargs)
            for it in resp.get("Items", []):
                bid = it.get("bridge_id")
                if bid:
                    served.add(bid)
            if "LastEvaluatedKey" not in resp:
                break
            scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    except Exception:
        pass
    return served


def demand_histogram(days: int = 90) -> list[dict]:
    """Return number of bridges needing blood per day for the next N days."""
    today = date.today()
    upcoming = upcoming_demand(days=days, today=today)
    by_day: dict[int, int] = {i: 0 for i in range(0, days + 1)}
    for u in upcoming:
        d = u["days_until"]
        if 0 <= d <= days:
            by_day[d] += 1
    return [
        {"day_offset": d, "date": (today + timedelta(days=d)).isoformat(), "count": c}
        for d, c in sorted(by_day.items())
    ]


if __name__ == "__main__":
    print("Upcoming demand (next 90 days):")
    for u in upcoming_demand(days=90):
        print(f"  {u['days_until']:+d}d  {u['blood_group']:<4}  bridge={u['bridge_id'][:24]}...  {u['patient_name']} ({u['patient_age']}y) at {u['hospital']}")
