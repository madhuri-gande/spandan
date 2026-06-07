"""Demo seeder + pipeline diagnostic.

Both the CLI (`tools/seed_pipeline.py`) and the Coordinator dashboard
call into this module so they share one implementation.

Public API:

    diagnose_pipeline() -> dict
        Counts bridges and tells you how many are due in the next
        7 / 30 / 90 days plus the closest 10.

    seed_imminent_patients(target=5, force=False) -> dict
        Bumps up to `target` bridges so their `expected_next_transfusion_date`
        is in the next 0-3 days. Idempotent — only ever updates that one
        attribute (plus a `seeded_for_demo` flag for auditing). Skips if
        the pipeline already has >= target imminent patients, unless
        `force=True`.

    unseed_all() -> dict
        Reverse of seed_imminent_patients. Clears the `seeded_for_demo`
        flag and drops `expected_next_transfusion_date` from every bridge
        that was ever date-bumped, so the natural cadence
        (last_transfusion_date + frequency_in_days) takes over again.
        Use this when the dataset already provides enough real upcoming
        patients and you no longer want demo dates polluting the table.
"""
from __future__ import annotations

import random
from datetime import date, timedelta
from typing import Optional

from services import db, forecasting


def diagnose_pipeline() -> dict:
    """Snapshot of how many bridges are imminent.

    Mirrors the same exclusion the Coordinator dashboard uses (bridges
    that already got a confirmed donation in the last 24h are *served*
    and don't show up as "upcoming demand"), so the seeder's notion of
    "imminent" matches what the user sees on screen.

    Returns:
        {
            "total":          total bridges in DDB,
            "no_date":        bridges with no usable date hint,
            "counts": {7: N, 30: N, 90: N, 365: N},
            "upcoming_30": [{patient_name, blood_group, days_until, bridge_id}, ...]
        }
    """
    bridges = forecasting.fetch_all_bridges()
    served_recently = forecasting._bridges_with_recent_confirmation()
    today = date.today()
    counts = {7: 0, 30: 0, 90: 0, 365: 0}
    upcoming_30: list[dict] = []
    no_date = 0

    for b in bridges:
        if b.get("bridge_id") in served_recently:
            continue
        predicted = forecasting.predict_next_transfusion(b, today=today)
        if predicted is None:
            no_date += 1
            continue
        delta = (predicted - today).days
        for w in counts:
            if delta <= w:
                counts[w] += 1
        if delta <= 30:
            upcoming_30.append({
                "bridge_id": b.get("bridge_id"),
                "patient_name": b.get("patient_name"),
                "blood_group": b.get("blood_group"),
                "days_until": delta,
            })

    upcoming_30.sort(key=lambda x: x["days_until"])
    return {
        "total": len(bridges),
        "no_date": no_date,
        "counts": counts,
        "upcoming_30": upcoming_30,
    }


def seed_imminent_patients(target: int = 5, force: bool = False,
                           seed_random: Optional[int] = 42) -> dict:
    """Bump up to `target` bridges to be due in the next 0-3 days.

    Returns:
        {
            "seeded":            number of bridges actually updated,
            "already_imminent":  how many were already in the 30-day window,
            "patients":          [{patient_name, blood_group, due, bridge_id}, ...],
            "skipped_reason":    optional string when seeded=0
        }
    """
    d = diagnose_pipeline()
    already = d["counts"][30]
    if not force and already >= target:
        return {
            "seeded": 0,
            "already_imminent": already,
            "patients": [],
            "skipped_reason": f"pipeline already has {already} imminent patient(s)",
        }

    needed = max(0, target - already) if not force else target
    if needed <= 0:
        return {
            "seeded": 0, "already_imminent": already, "patients": [],
            "skipped_reason": "nothing to seed",
        }

    imminent_ids = {p["bridge_id"] for p in d["upcoming_30"]}
    # The Coordinator pipeline excludes bridges that have a confirmed
    # donation in the last 24h ("served"). If the seeder picks one of
    # those and just bumps its date, the bumped bridge STILL won't show
    # up in the pipeline UI — so the user clicks 🌱 and sees an empty
    # pipeline. Skip served bridges from candidates so every seeded
    # patient is guaranteed to render.
    served_recently = forecasting._bridges_with_recent_confirmation()
    candidates: list[dict] = []
    for b in forecasting.fetch_all_bridges():
        if not b.get("blood_group"):
            continue
        if b.get("bridge_id") in imminent_ids:
            continue
        if b.get("bridge_id") in served_recently:
            continue
        candidates.append(b)

    if not candidates:
        return {
            "seeded": 0, "already_imminent": already, "patients": [],
            "skipped_reason": (
                "no fresh bridges to seed — every bridge with a blood "
                "group is either already in the pipeline or was "
                "confirmed in the last 24h"
            ),
        }

    if seed_random is not None:
        random.seed(seed_random)
    random.shuffle(candidates)
    picks = candidates[:needed]

    table = db.get_table("bridges")
    today = date.today()
    seeded: list[dict] = []
    for i, b in enumerate(picks):
        # Spread across days 0..3 so the demo has a "today / tomorrow /
        # day after" mix in the urgency column.
        offset = i % 4
        new_date = (today + timedelta(days=offset)).isoformat()
        try:
            table.update_item(
                Key={"bridge_id": b["bridge_id"]},
                UpdateExpression=(
                    "SET expected_next_transfusion_date = :d, "
                    "    seeded_for_demo = :f, "
                    "    seeded_at = :t"
                ),
                ExpressionAttributeValues={
                    ":d": new_date,
                    ":f": True,
                    ":t": today.isoformat(),
                },
            )
            seeded.append({
                "bridge_id": b["bridge_id"],
                "patient_name": b.get("patient_name"),
                "blood_group": b.get("blood_group"),
                "due": new_date,
                "days_until": offset,
            })
        except Exception:
            continue

    return {
        "seeded": len(seeded),
        "already_imminent": already,
        "patients": seeded,
    }


def unseed_all() -> dict:
    """Roll back every demo-seeded bridge to its natural prediction.

    Removes both `seeded_for_demo` and `expected_next_transfusion_date`
    so `predict_next_transfusion` falls back to
    `last_transfusion_date + frequency_in_days`.

    Returns:
        {"unseeded": N, "patients": [{patient_name, blood_group, bridge_id}]}
    """
    bridges = forecasting.fetch_all_bridges()
    table = db.get_table("bridges")
    cleared: list[dict] = []
    for b in bridges:
        if not b.get("seeded_for_demo"):
            continue
        try:
            table.update_item(
                Key={"bridge_id": b["bridge_id"]},
                UpdateExpression=(
                    "REMOVE seeded_for_demo, "
                    "       expected_next_transfusion_date, "
                    "       seeded_at"
                ),
            )
            cleared.append({
                "bridge_id": b["bridge_id"],
                "patient_name": b.get("patient_name"),
                "blood_group": b.get("blood_group"),
            })
        except Exception:
            continue
    return {"unseeded": len(cleared), "patients": cleared}
