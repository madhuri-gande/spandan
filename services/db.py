"""DynamoDB client and table management for Spandan."""

from __future__ import annotations

import datetime as _dt
import os
import re as _re
import time
from decimal import Decimal
from typing import Any, Iterable

import boto3
import botocore.auth
from botocore.exceptions import ClientError
from dotenv import load_dotenv

load_dotenv()

REGION = os.getenv("AWS_DEFAULT_REGION", "us-east-1")


# ---------------------------------------------------------------------------
# Clock-skew auto-correction
# ---------------------------------------------------------------------------
# When the laptop sleeps + wakes, macOS sometimes doesn't auto-resync NTP, so
# the local clock can be 5-30 min behind real UTC. Every boto3 call then
# fails with InvalidSignatureException because AWS rejects signatures
# outside a 15-minute window.
#
# We patch botocore's signer so that, on the first SigV4 signing attempt
# after a skew is detected, every signature uses (real_utc_now =
# system_now + offset). The offset is set once we see an
# InvalidSignatureException error message that includes AWS's "current"
# time. After that, all subsequent requests sign correctly.

_clock_offset_seconds: float = 0.0
_TIMESTAMP_RE = _re.compile(r"\((\d{8}T\d{6}Z)\s*-\s*15\s*min")


def _set_clock_skew_from_error(err_msg: str) -> bool:
    """Parse '(20260606T234308Z - 15 min.)' from an AWS SigV4 error and
    set the global offset so subsequent calls sign with corrected time.
    Returns True iff offset was updated."""
    global _clock_offset_seconds
    m = _TIMESTAMP_RE.search(err_msg or "")
    if not m:
        return False
    aws_time = _dt.datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ")
    aws_time = aws_time.replace(tzinfo=_dt.timezone.utc)
    offset = (aws_time - _dt.datetime.now(_dt.timezone.utc)).total_seconds()
    if abs(offset) > 60:  # only kick in for >1min skew
        _clock_offset_seconds = offset
        return True
    return False


class _CorrectingDatetime(_dt.datetime):
    """Subclass of datetime.datetime whose `now`/`utcnow` apply the
    detected clock offset. Used to swap out botocore.auth's `datetime`
    reference so SigV4 signatures get the corrected wall time without
    affecting any other code."""

    @classmethod
    def utcnow(cls):
        return _dt.datetime.utcnow() + _dt.timedelta(seconds=_clock_offset_seconds)

    @classmethod
    def now(cls, tz=None):
        return _dt.datetime.now(tz) + _dt.timedelta(seconds=_clock_offset_seconds)


class _CorrectingDatetimeModule:
    """Stand-in for the `datetime` MODULE inside botocore.auth, exposing
    only the names that module references (`datetime.datetime`).
    Everything else is forwarded to the real module so unrelated calls
    keep working."""
    datetime = _CorrectingDatetime
    timedelta = _dt.timedelta
    timezone = _dt.timezone


botocore.auth.datetime = _CorrectingDatetimeModule  # type: ignore[assignment]


def _maybe_handle_clock_skew(exc: ClientError) -> bool:
    """If `exc` is a SignatureExpired/InvalidSignature with parseable
    AWS time, update the global offset. Returns True if caller should
    retry the operation immediately."""
    code = exc.response.get("Error", {}).get("Code", "")
    if code not in ("InvalidSignatureException", "SignatureDoesNotMatch"):
        return False
    msg = exc.response.get("Error", {}).get("Message", "") or str(exc)
    return _set_clock_skew_from_error(msg)

TABLES = {
    "donors": {
        "KeySchema": [{"AttributeName": "user_id", "KeyType": "HASH"}],
        "AttributeDefinitions": [
            {"AttributeName": "user_id", "AttributeType": "S"},
            {"AttributeName": "blood_group", "AttributeType": "S"},
        ],
        "GlobalSecondaryIndexes": [
            {
                "IndexName": "blood_group-index",
                "KeySchema": [{"AttributeName": "blood_group", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
        "BillingMode": "PAY_PER_REQUEST",
    },
    "bridges": {
        "KeySchema": [{"AttributeName": "bridge_id", "KeyType": "HASH"}],
        "AttributeDefinitions": [
            {"AttributeName": "bridge_id", "AttributeType": "S"},
        ],
        "BillingMode": "PAY_PER_REQUEST",
    },
    "messages": {
        "KeySchema": [
            {"AttributeName": "donor_id", "KeyType": "HASH"},
            {"AttributeName": "ts", "KeyType": "RANGE"},
        ],
        "AttributeDefinitions": [
            {"AttributeName": "donor_id", "AttributeType": "S"},
            {"AttributeName": "ts", "AttributeType": "S"},
        ],
        "BillingMode": "PAY_PER_REQUEST",
    },
    "agent_log": {
        "KeySchema": [
            {"AttributeName": "cycle_id", "KeyType": "HASH"},
            {"AttributeName": "ts", "KeyType": "RANGE"},
        ],
        "AttributeDefinitions": [
            {"AttributeName": "cycle_id", "AttributeType": "S"},
            {"AttributeName": "ts", "AttributeType": "S"},
        ],
        "BillingMode": "PAY_PER_REQUEST",
    },
    "donations": {
        "KeySchema": [{"AttributeName": "donation_id", "KeyType": "HASH"}],
        "AttributeDefinitions": [
            {"AttributeName": "donation_id", "AttributeType": "S"},
            {"AttributeName": "bridge_id", "AttributeType": "S"},
            {"AttributeName": "donor_id", "AttributeType": "S"},
        ],
        "GlobalSecondaryIndexes": [
            {
                "IndexName": "bridge_id-index",
                "KeySchema": [{"AttributeName": "bridge_id", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": "donor_id-index",
                "KeySchema": [{"AttributeName": "donor_id", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
        "BillingMode": "PAY_PER_REQUEST",
    },
}

TABLE_PREFIX = "spandan_"


def get_dynamodb_resource():
    return boto3.resource("dynamodb", region_name=REGION)


def get_dynamodb_client():
    return boto3.client("dynamodb", region_name=REGION)


def get_table(name: str):
    return get_dynamodb_resource().Table(f"{TABLE_PREFIX}{name}")


def retry_on_clock_skew(fn):
    """Decorator: re-run `fn` once if AWS rejects the signature because
    of clock drift. The first failure tells us AWS's current UTC, we
    install a global offset (see `_set_clock_skew_from_error`), and the
    retry signs with the corrected time."""
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ClientError as exc:
            if _maybe_handle_clock_skew(exc):
                return fn(*args, **kwargs)
            raise
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


def create_tables(verbose: bool = True) -> dict[str, str]:
    """Create all tables if they don't exist. Returns status per table."""
    client = get_dynamodb_client()
    statuses: dict[str, str] = {}

    for name, config in TABLES.items():
        full_name = f"{TABLE_PREFIX}{name}"
        try:
            client.describe_table(TableName=full_name)
            statuses[full_name] = "exists"
            if verbose:
                print(f"  [skip] {full_name} already exists")
            continue
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ResourceNotFoundException":
                raise

        params: dict[str, Any] = {"TableName": full_name, **config}
        client.create_table(**params)
        statuses[full_name] = "creating"
        if verbose:
            print(f"  [create] {full_name} ...")

    waiter = client.get_waiter("table_exists")
    for name in TABLES:
        full_name = f"{TABLE_PREFIX}{name}"
        if statuses[full_name] == "creating":
            waiter.wait(TableName=full_name, WaiterConfig={"Delay": 2, "MaxAttempts": 60})
            if verbose:
                print(f"  [ready] {full_name}")
            statuses[full_name] = "created"
    return statuses


def drop_all_tables(confirm: bool = False) -> None:
    if not confirm:
        raise RuntimeError("Pass confirm=True to drop tables")
    client = get_dynamodb_client()
    for name in TABLES:
        full_name = f"{TABLE_PREFIX}{name}"
        try:
            client.delete_table(TableName=full_name)
            print(f"  [drop] {full_name}")
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ResourceNotFoundException":
                raise


def to_dynamodb_safe(value: Any) -> Any:
    """Convert Python values to DynamoDB-safe types (floats -> Decimal, NaN -> None)."""
    if value is None:
        return None
    if isinstance(value, float):
        if value != value:  # NaN check
            return None
        return Decimal(str(round(value, 6)))
    if isinstance(value, dict):
        return {k: to_dynamodb_safe(v) for k, v in value.items() if to_dynamodb_safe(v) is not None}
    if isinstance(value, (list, tuple)):
        return [to_dynamodb_safe(v) for v in value]
    if isinstance(value, str) and value.strip() == "":
        return None
    return value


def batch_write(table_name: str, items: Iterable[dict]) -> int:
    """Batch write items to a table. Returns total count written."""
    table = get_table(table_name)
    count = 0
    with table.batch_writer(overwrite_by_pkeys=None) as writer:
        for item in items:
            cleaned = to_dynamodb_safe(item)
            cleaned = {k: v for k, v in cleaned.items() if v is not None}
            writer.put_item(Item=cleaned)
            count += 1
    return count


def count_table(name: str) -> int:
    """Return a live row count.

    DynamoDB's `describe_table` ItemCount is only refreshed every ~6 hours so
    it stays at 0 right after fresh inserts. We use a paginated scan with
    Select=COUNT instead — fast (no item attributes returned) and accurate.
    """
    client = get_dynamodb_client()
    paginator = client.get_paginator("scan")
    full = f"{TABLE_PREFIX}{name}"
    total = 0
    try:
        for page in paginator.paginate(TableName=full, Select="COUNT"):
            total += page.get("Count", 0)
    except ClientError:
        return 0
    return total


if __name__ == "__main__":
    print(f"Creating Spandan DynamoDB tables in region: {REGION}")
    create_tables()
    print("Done.")
