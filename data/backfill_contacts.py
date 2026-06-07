"""Backfill synthesized email + phone for donors that pre-date the schema.

Idempotent — only updates donors missing those attributes.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import db
from data.load_dataset import _gen_demo_email, _gen_demo_phone


def main() -> None:
    table = db.get_table("donors")
    paginator = db.get_dynamodb_client().get_paginator("scan")
    total = updated = skipped = 0
    for page in paginator.paginate(TableName=table.name):
        for item in page.get("Items", []):
            total += 1
            uid = list(item.get("user_id", {}).values())[0] if item.get("user_id") else None
            if not uid:
                skipped += 1
                continue
            # ddb client returns {"S": "..."} format, use resource API instead
            res = table.get_item(Key={"user_id": uid}).get("Item")
            if not res:
                skipped += 1
                continue
            email = res.get("email")
            phone = res.get("phone")
            name = res.get("name") or "Donor"
            if email and phone:
                continue
            new_email = email or _gen_demo_email(uid, name)
            new_phone = phone or _gen_demo_phone(uid)
            table.update_item(
                Key={"user_id": uid},
                UpdateExpression="SET email = if_not_exists(email, :e), phone = if_not_exists(phone, :p)",
                ExpressionAttributeValues={":e": new_email, ":p": new_phone},
            )
            updated += 1
            if updated % 200 == 0:
                print(f"  ... updated {updated} donors so far")

    print(f"Done. total scanned={total}  updated={updated}  skipped(no uid)={skipped}")


if __name__ == "__main__":
    main()
