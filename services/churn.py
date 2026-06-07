"""Donor churn prediction.

Predicts the probability that a donor is currently INACTIVE (has churned)
given their behavioural features. Trained on the dataset's
`user_donation_active_status` column (Active/Inactive).

Used by the Coordinator dashboard to flag at-risk donors and by the
agent to deprioritise donors who are likely to ignore outreach.
"""

from __future__ import annotations

import io
import os
import pickle
from pathlib import Path

import boto3
import numpy as np
import pandas as pd
from dotenv import load_dotenv
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, roc_auc_score
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

load_dotenv()

REGION = os.getenv("AWS_DEFAULT_REGION", "us-east-1")
S3_BUCKET = os.getenv("S3_BUCKET", "")
MODEL_KEY = "models/donor_churn.pkl"
LOCAL_PATH = Path(__file__).resolve().parents[1] / "models" / "donor_churn.pkl"
DATASET_PATH = Path(__file__).resolve().parents[2] / "Dataset.csv"

FEATURES = [
    "calls_to_donations_ratio",
    "donations_till_date_log",
    "frequency_in_days",
    "total_calls_log",
    "is_one_time",
    "is_eligible",
]


def _num_col(df: pd.DataFrame, col: str, default: float) -> pd.Series:
    if col in df.columns:
        s = pd.to_numeric(df[col], errors="coerce")
    else:
        s = pd.Series([np.nan] * len(df), index=df.index, dtype="float64")
    return s.fillna(default)


def _str_col(df: pd.DataFrame, col: str) -> pd.Series:
    if col in df.columns:
        return df[col].astype(str).fillna("").str.lower()
    return pd.Series([""] * len(df), index=df.index, dtype="object")


def _engineer(df: pd.DataFrame) -> pd.DataFrame:
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
    df["is_one_time"] = donor_type.str.contains("one-time").astype(int)
    return df


def train_and_save(verbose: bool = True) -> dict:
    df = pd.read_csv(DATASET_PATH)
    df.columns = [c.strip() for c in df.columns]
    df = _engineer(df)

    status = df.get("user_donation_active_status", "").astype(str).str.lower()
    y = (status == "inactive").astype(int).values

    X = df[FEATURES].fillna(0).values
    if y.sum() == 0:
        raise RuntimeError("No 'Inactive' donors in dataset")

    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, random_state=42, stratify=y
    )

    pipeline = Pipeline([
        ("scaler", StandardScaler()),
        ("clf", LogisticRegression(max_iter=400, random_state=42, class_weight="balanced")),
    ])

    pipeline.fit(X_train, y_train)
    y_pred = pipeline.predict_proba(X_test)[:, 1]
    auc = float(roc_auc_score(y_test, y_pred))
    acc = float(accuracy_score(y_test, (y_pred >= 0.5).astype(int)))

    LOCAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"model": pipeline, "features": FEATURES, "auc": auc, "accuracy": acc}
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
        print(f"Donor churn trained. AUC={auc:.3f}  Acc={acc:.3f}")
    return payload


def load_model() -> dict:
    if LOCAL_PATH.exists():
        with open(LOCAL_PATH, "rb") as f:
            return pickle.load(f)
    if S3_BUCKET:
        s3 = boto3.client("s3", region_name=REGION)
        obj = s3.get_object(Bucket=S3_BUCKET, Key=MODEL_KEY)
        return pickle.loads(obj["Body"].read())
    raise FileNotFoundError("No trained churn model available")


def predict_churn(donors: list[dict]) -> list[float]:
    """Predict churn probability for a list of donor dicts."""
    if not donors:
        return []
    payload = load_model()
    model = payload["model"]
    df = pd.DataFrame(donors)
    df = _engineer(df)
    X = df[FEATURES].fillna(0).values
    return model.predict_proba(X)[:, 1].tolist()


if __name__ == "__main__":
    train_and_save()
