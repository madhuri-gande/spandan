"""Pipeline diagnostic + demo seeder (CLI).

The same logic also powers the *Seed demo patients* button on the
Coordinator dashboard — both call into `services/demo_seed.py`.

Run this when you don't see anything in the Coordinator's "Patient pipeline".

Examples:

    # Just diagnose — don't change anything
    python tools/seed_pipeline.py

    # Make sure at least 5 patients are due in the next 30 days
    python tools/seed_pipeline.py --seed

    # Force 8 imminent patients regardless of what's already there
    python tools/seed_pipeline.py --seed --target 8 --force
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from services.demo_seed import diagnose_pipeline, seed_imminent_patients  # noqa: E402


def print_diagnosis(d: dict) -> None:
    print("=" * 60)
    print("  Spandan pipeline diagnostic")
    print("=" * 60)
    print(f"  Total bridges in DDB:           {d['total']:>5}")
    print(f"  Bridges with no usable date:    {d['no_date']:>5}")
    print()
    print("  Patients due within...")
    for w, c in d["counts"].items():
        print(f"      {w:>3} days: {c:>5}")
    print()
    if d["upcoming_30"]:
        print(f"  First {min(10, len(d['upcoming_30']))} patients in the next-30-day window:")
        for p in d["upcoming_30"][:10]:
            tag = f"OVERDUE {abs(p['days_until'])}d" if p["days_until"] < 0 else f"+{p['days_until']}d"
            print(f"      {tag:<12}  {p['blood_group']:<4}  {p['patient_name']}")
    else:
        print("  No patients in the next 30 days.")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description="Spandan pipeline diagnostic + seeder")
    ap.add_argument("--seed", action="store_true",
                    help="Seed imminent demo patients if pipeline is empty")
    ap.add_argument("--target", type=int, default=5,
                    help="Minimum patients to have in the next 30 days (default 5)")
    ap.add_argument("--force", action="store_true",
                    help="Seed --target bridges even if pipeline already has data")
    args = ap.parse_args()

    print_diagnosis(diagnose_pipeline())

    if args.seed:
        print(f"  Seeding pipeline (target={args.target}, force={args.force}) ...")
        result = seed_imminent_patients(target=args.target, force=args.force)
        if result["seeded"]:
            for p in result["patients"]:
                print(f"    + {p['patient_name']:<24} {p['blood_group']:<4} -> due {p['due']}")
            print(f"  Done. Seeded {result['seeded']} bridge(s).")
            print()
            print("  Re-running diagnostic ...")
            print()
            print_diagnosis(diagnose_pipeline())
        else:
            reason = result.get("skipped_reason", "no candidates")
            print(f"  Nothing to seed: {reason}")
    else:
        if diagnose_pipeline()["counts"][30] == 0:
            print("  Pipeline is empty. Run with --seed to bump 5 demo patients.")
            print("  Or click '🌱 Seed demo patients' on the Coordinator dashboard.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
