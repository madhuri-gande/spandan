"""Load the provided Dataset.csv into DynamoDB.

Steps:
  1. Read Dataset.csv (locally or from S3).
  2. Split rows into donor records and bridge (patient) records.
  3. Create DynamoDB tables if missing.
  4. Batch-write donors and bridges.
  5. Print a summary.

Usage:
    python -m data.load_dataset                  # reads ../Dataset.csv
    python -m data.load_dataset --from-s3        # reads s3://<bucket>/data/Dataset.csv
    python -m data.load_dataset --reset          # drop + recreate tables
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path

import boto3
import pandas as pd
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import db  # noqa: E402

load_dotenv()

DEFAULT_LOCAL_PATH = Path(__file__).resolve().parents[2] / "Dataset.csv"
S3_BUCKET = os.getenv("S3_BUCKET", "")
S3_KEY = "data/Dataset.csv"

DEMO_LANGUAGES = ["telugu", "hindi", "tamil", "english"]


def read_dataset(from_s3: bool = False) -> pd.DataFrame:
    if from_s3:
        if not S3_BUCKET:
            raise RuntimeError("S3_BUCKET env not set")
        s3 = boto3.client("s3", region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"))
        obj = s3.get_object(Bucket=S3_BUCKET, Key=S3_KEY)
        df = pd.read_csv(io.BytesIO(obj["Body"].read()))
        print(f"  loaded {len(df)} rows from s3://{S3_BUCKET}/{S3_KEY}")
    else:
        df = pd.read_csv(DEFAULT_LOCAL_PATH)
        print(f"  loaded {len(df)} rows from {DEFAULT_LOCAL_PATH}")
    df.columns = [c.strip() for c in df.columns]
    return df


def assign_language(idx: int) -> str:
    return DEMO_LANGUAGES[idx % len(DEMO_LANGUAGES)]


def safe_int(v, default: int = 0) -> int:
    try:
        if v is None or (isinstance(v, float) and v != v):
            return default
        return int(float(v))
    except (TypeError, ValueError):
        return default


def safe_str(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v != v:
        return ""
    return str(v).strip()


def normalize_blood_group(raw) -> str:
    if not isinstance(raw, str):
        return ""
    s = raw.strip()
    mapping = {
        "A Positive": "A+", "A Negative": "A-",
        "B Positive": "B+", "B Negative": "B-",
        "O Positive": "O+", "O Negative": "O-",
        "AB Positive": "AB+", "AB Negative": "AB-",
    }
    return mapping.get(s, s)


def clean_user_id(raw: str) -> str:
    if not isinstance(raw, str):
        return ""
    return raw.replace("\\x27", "").replace("'", "").strip()


def parse_donors(df: pd.DataFrame) -> list[dict]:
    """One donor row per user_id (deduplicated)."""
    df = df.copy()
    df["user_id_clean"] = df["user_id"].apply(clean_user_id)
    df = df[df["user_id_clean"].astype(bool)]

    grouped = df.groupby("user_id_clean", as_index=False).first()

    donors = []
    for i, row in grouped.iterrows():
        uid = row["user_id_clean"]
        name = _gen_demo_name(uid, safe_str(row.get("gender")))
        donor = {
            "user_id": uid,
            "role": safe_str(row.get("role")),
            "blood_group": normalize_blood_group(row.get("blood_group")),
            "blood_group_raw": safe_str(row.get("blood_group")),
            "gender": safe_str(row.get("gender")),
            "latitude": row.get("latitude"),
            "longitude": row.get("longitude"),
            "donor_type": safe_str(row.get("donor_type")),
            "registration_date": safe_str(row.get("registration_date")),
            "last_contacted_date": safe_str(row.get("last_contacted_date")),
            "last_donation_date": safe_str(row.get("last_donation_date")),
            "next_eligible_date": safe_str(row.get("next_eligible_date")),
            "donations_till_date": safe_int(row.get("donations_till_date")),
            "eligibility_status": safe_str(row.get("eligibility_status")),
            "cycle_of_donations": safe_int(row.get("cycle_of_donations")),
            "total_calls": safe_int(row.get("total_calls")),
            "frequency_in_days": safe_int(row.get("frequency_in_days")),
            "calls_to_donations_ratio": row.get("calls_to_donations_ratio"),
            "user_donation_active_status": safe_str(row.get("user_donation_active_status")),
            "inactive_trigger_comment": safe_str(row.get("inactive_trigger_comment")),
            "donated_earlier": safe_str(row.get("donated_earlier")).lower() == "true",
            "preferred_language": assign_language(i),
            "skip_score": 0,
            "response_count": 0,
            "name": name,
            "email": _gen_demo_email(uid, name),
            "phone": _gen_demo_phone(uid),
        }
        donors.append(donor)
    print(f"  parsed {len(donors)} unique donors")
    return donors


def parse_bridges(df: pd.DataFrame) -> list[dict]:
    """One bridge per bridge_id; bridge donors collected as a set of user_ids."""
    df = df.copy()
    df["user_id_clean"] = df["user_id"].apply(clean_user_id)
    df = df[df["bridge_id"].notna() & (df["bridge_id"].astype(str).str.strip() != "")]
    df["bridge_id_clean"] = df["bridge_id"].apply(clean_user_id)

    bridges = []
    for bridge_id, group in df.groupby("bridge_id_clean"):
        first = group.iloc[0]
        donors = sorted({uid for uid in group["user_id_clean"] if uid})
        bridges.append({
            "bridge_id": bridge_id,
            "blood_group": normalize_blood_group(first.get("bridge_blood_group")),
            "bridge_blood_group_raw": safe_str(first.get("bridge_blood_group")),
            "bridge_gender": safe_str(first.get("bridge_gender")),
            "quantity_required": safe_int(first.get("quantity_required"), default=1),
            "last_transfusion_date": safe_str(first.get("last_transfusion_date")),
            "expected_next_transfusion_date": safe_str(first.get("expected_next_transfusion_date")),
            "status_of_bridge": safe_str(first.get("status_of_bridge")),
            "donor_pool": donors,
            "donor_pool_size": len(donors),
            "patient_age": _hash_age(bridge_id),
            "hospital": _hash_hospital(bridge_id),
            "patient_name": _hash_patient_name(bridge_id),
        })
    print(f"  parsed {len(bridges)} unique bridges")
    return bridges


_FIRST_NAMES = ["Aarav", "Vivaan", "Aditya", "Vihaan", "Arjun", "Saanvi", "Ananya",
                "Aadhya", "Diya", "Pari", "Kavya", "Ira", "Riya", "Anaya"]
_LAST_NAMES = ["Reddy", "Sharma", "Iyer", "Nair", "Gupta", "Patel", "Krishna",
               "Verma", "Rao", "Kumar"]
_HOSPITALS = ["Apollo Hyderabad", "Care Hospital Banjara Hills", "Yashoda Secunderabad",
              "Continental Gachibowli", "Rainbow Children's Hospital", "Niloufer Hospital"]


def _stable_index(seed: str, n: int) -> int:
    return abs(hash(seed)) % n


def _gen_demo_name(uid: str, gender: str) -> str:
    return f"{_FIRST_NAMES[_stable_index(uid, len(_FIRST_NAMES))]} {_LAST_NAMES[_stable_index(uid + 'l', len(_LAST_NAMES))]}"


def _hash_age(bridge_id: str) -> int:
    return 5 + (_stable_index(bridge_id, 15))


def _hash_hospital(bridge_id: str) -> str:
    return _HOSPITALS[_stable_index(bridge_id, len(_HOSPITALS))]


def _hash_patient_name(bridge_id: str) -> str:
    return f"{_FIRST_NAMES[_stable_index(bridge_id + 'p', len(_FIRST_NAMES))]} {_LAST_NAMES[_stable_index(bridge_id + 'p', len(_LAST_NAMES))]}"


def _gen_demo_email(uid: str, name: str) -> str:
    """Synthesise a stable per-donor email address.

    Real Blood Warriors data is anonymised — no email column. For the demo
    we deterministically generate addresses from the hashed user_id, which
    MailPit catches locally so judges see distinct recipients (one per
    donor) without spamming any real inbox.
    """
    parts = [p.lower() for p in name.split() if p]
    if not parts:
        slug = "donor"
    else:
        slug = ".".join(parts)
    short = abs(hash(uid)) % 0xFFFFFF
    return f"{slug}+{short:06x}@spandan.test"


def _gen_demo_phone(uid: str) -> str:
    """Synthesise a stable Indian-format phone number for the donor."""
    n = abs(hash(uid + "phone")) % 9000000000 + 1000000000
    return f"+91-{n:010d}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--from-s3", action="store_true", help="Read Dataset.csv from S3 instead of local")
    parser.add_argument("--reset", action="store_true", help="Drop tables before recreating")
    args = parser.parse_args()

    print("=== Spandan dataset loader ===")

    if args.reset:
        print("Dropping existing tables ...")
        db.drop_all_tables(confirm=True)
        print("  waiting 15s for full table teardown ...")
        import time
        time.sleep(15)

    print("Creating tables ...")
    db.create_tables()

    print("Reading dataset ...")
    df = read_dataset(from_s3=args.from_s3)

    print("Parsing donors ...")
    donors = parse_donors(df)

    print("Parsing bridges ...")
    bridges = parse_bridges(df)

    print(f"Writing {len(donors)} donors to DynamoDB ...")
    n = db.batch_write("donors", donors)
    print(f"  wrote {n} donor records")

    print(f"Writing {len(bridges)} bridges to DynamoDB ...")
    n = db.batch_write("bridges", bridges)
    print(f"  wrote {n} bridge records")

    print("Done.")
    print(f"Sample donor ID: {donors[0]['user_id'][:32]}...")
    print(f"Sample bridge ID: {bridges[0]['bridge_id'][:32] if bridges else 'NONE'}...")


if __name__ == "__main__":
    main()
