"""Donor ranking ML model.

Trains a logistic regression on the provided dataset to predict the
probability that a donor will respond positively to an outreach.

Features:
    calls_to_donations_ratio
    donations_till_date (log-scaled)
    eligibility_status (eligible vs not eligible)
    frequency_in_days
    donor_type (One-Time / Regular / Other)
    total_calls (log-scaled)

Label:
    donated_earlier (true / false in dataset)

Provides two public functions for the agent:
    train_and_save()     -> trains, saves .pkl locally and to S3
    rank_for_bridge(b)   -> returns top-N donors for a given bridge
"""

from __future__ import annotations

import io
import os
import pickle
from pathlib import Path
from typing import Optional

import boto3
import numpy as np
import pandas as pd
from dotenv import load_dotenv
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

load_dotenv()

REGION = os.getenv("AWS_DEFAULT_REGION", "us-east-1")
S3_BUCKET = os.getenv("S3_BUCKET", "")
MODEL_KEY = "models/donor_ranking.pkl"
LOCAL_PATH = Path(__file__).resolve().parents[1] / "models" / "donor_ranking.pkl"

DATASET_PATH = Path(__file__).resolve().parents[2] / "Dataset.csv"

FEATURES = [
    "calls_to_donations_ratio",
    "donations_till_date_log",
    "is_eligible",
    "frequency_in_days",
    "is_regular",
    "is_one_time",
    "total_calls_log",
]


def _num_col(df: pd.DataFrame, col: str, default: float) -> pd.Series:
    """Always return a numeric Series of len(df), even if the column is absent.

    DynamoDB items can omit optional attributes, so the resulting DataFrame
    may not have every expected column. `df.get(missing_col)` returns None,
    which then becomes a scalar nan and breaks `.fillna()`.
    """
    if col in df.columns:
        s = pd.to_numeric(df[col], errors="coerce")
    else:
        s = pd.Series([np.nan] * len(df), index=df.index, dtype="float64")
    return s.fillna(default)


def _str_col(df: pd.DataFrame, col: str) -> pd.Series:
    if col in df.columns:
        return df[col].astype(str).fillna("").str.lower()
    return pd.Series([""] * len(df), index=df.index, dtype="object")


def _engineer_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["calls_to_donations_ratio"] = _num_col(df, "calls_to_donations_ratio", 1.0)
    df["donations_till_date"] = _num_col(df, "donations_till_date", 0)
    df["donations_till_date_log"] = np.log1p(df["donations_till_date"])
    df["frequency_in_days"] = _num_col(df, "frequency_in_days", 180)
    df["total_calls"] = _num_col(df, "total_calls", 0)
    df["total_calls_log"] = np.log1p(df["total_calls"])

    elig = _str_col(df, "eligibility_status")
    df["is_eligible"] = (elig == "eligible").astype(int)

    donor_type = _str_col(df, "donor_type")
    df["is_regular"] = donor_type.str.contains("regular").astype(int)
    df["is_one_time"] = donor_type.str.contains("one-time").astype(int)
    return df


def _label(df: pd.DataFrame) -> pd.Series:
    return df.get("donated_earlier", "").astype(str).str.lower().eq("true").astype(int)


def train_and_save(verbose: bool = True) -> dict:
    if not DATASET_PATH.exists():
        raise FileNotFoundError(f"Dataset not found at {DATASET_PATH}")

    df = pd.read_csv(DATASET_PATH)
    df.columns = [c.strip() for c in df.columns]
    df = _engineer_features(df)

    X = df[FEATURES].fillna(0).values
    y = _label(df).values

    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, random_state=42, stratify=y if y.sum() > 0 else None
    )

    pipeline = Pipeline([
        ("scaler", StandardScaler()),
        ("clf", LogisticRegression(max_iter=400, random_state=42, class_weight="balanced")),
    ])

    pipeline.fit(X_train, y_train)
    y_pred = pipeline.predict_proba(X_test)[:, 1]
    try:
        auc = float(roc_auc_score(y_test, y_pred))
    except Exception:
        auc = float("nan")

    LOCAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"model": pipeline, "features": FEATURES, "auc": auc, "n_train": len(X_train)}
    with open(LOCAL_PATH, "wb") as f:
        pickle.dump(payload, f)

    if S3_BUCKET:
        try:
            s3 = boto3.client("s3", region_name=REGION)
            buf = io.BytesIO()
            pickle.dump(payload, buf)
            buf.seek(0)
            s3.put_object(Bucket=S3_BUCKET, Key=MODEL_KEY, Body=buf.read())
            if verbose:
                print(f"  saved to s3://{S3_BUCKET}/{MODEL_KEY}")
        except Exception as exc:
            if verbose:
                print(f"  WARNING: could not upload to S3: {exc}")

    if verbose:
        print(f"Donor ranking trained. AUC={auc:.3f}, n_train={len(X_train)}")
    return payload


def load_model() -> dict:
    if LOCAL_PATH.exists():
        with open(LOCAL_PATH, "rb") as f:
            return pickle.load(f)
    if S3_BUCKET:
        s3 = boto3.client("s3", region_name=REGION)
        obj = s3.get_object(Bucket=S3_BUCKET, Key=MODEL_KEY)
        return pickle.loads(obj["Body"].read())
    raise FileNotFoundError("No trained ranking model available")


def score_donors(donors: list[dict]) -> list[float]:
    """Return predicted positive-response probability for each donor."""
    payload = load_model()
    model = payload["model"]
    df = pd.DataFrame(donors)
    df = _engineer_features(df)
    X = df[FEATURES].fillna(0).values
    return model.predict_proba(X)[:, 1].tolist()


def rank_for_bridge(bridge: dict, donors_pool: list[dict], top_n: int = 10) -> list[dict]:
    """Filter eligible donors with matching/compatible blood group, score, sort.

    bridge: {"bridge_id":..., "blood_group": "B+", ...}
    donors_pool: list of donor dicts (DynamoDB items)
    """
    target = (bridge.get("blood_group") or "").strip()
    candidates = [d for d in donors_pool if _compatible(d.get("blood_group"), target)
                  and (d.get("eligibility_status") or "").lower() == "eligible"]
    if not candidates:
        candidates = [d for d in donors_pool if _compatible(d.get("blood_group"), target)]

    scores = score_donors(candidates) if candidates else []
    for d, s in zip(candidates, scores):
        skip = d.get("skip_score") or 0
        try:
            skip = float(skip)
        except (TypeError, ValueError):
            skip = 0.0
        d["score"] = float(s) - 0.05 * skip

    ranked = sorted(candidates, key=lambda d: d.get("score", 0.0), reverse=True)
    return ranked[:top_n]


COMPATIBILITY = {
    "A+": {"A+", "A-", "O+", "O-"},
    "A-": {"A-", "O-"},
    "B+": {"B+", "B-", "O+", "O-"},
    "B-": {"B-", "O-"},
    "AB+": {"A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-"},
    "AB-": {"A-", "B-", "AB-", "O-"},
    "O+": {"O+", "O-"},
    "O-": {"O-"},
}


def _compatible(donor_bg: Optional[str], patient_bg: Optional[str]) -> bool:
    if not donor_bg or not patient_bg:
        return False
    accept = COMPATIBILITY.get(patient_bg.strip(), set())
    return donor_bg.strip() in accept


if __name__ == "__main__":
    train_and_save()
