"""Operator, local, READ-ONLY: scan the legacy DynamoDB audit table for ACTIONS#
rows and print the org-api invoke payload for the `backfill_legacy_ticks` task
(always apply=false; flip it by hand once the dry-run report has been read).

    AWS_PROFILE=fieldsight-deployer python scripts/export_legacy_ticks.py \
        --table fieldsight-test-audit > payload.json
"""
import argparse
import json
import os
import sys
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr


def _plain(v):
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    return v


def export_rows(table):
    kwargs = {"FilterExpression": Attr("PK").begins_with("ACTIONS#")}
    rows = []
    while True:
        page = table.scan(**kwargs)
        rows.extend({k: _plain(v) for k, v in it.items()} for it in page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            return rows
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--table", required=True)
    ap.add_argument("--region", default=os.environ.get("AWS_REGION", "ap-southeast-2"))
    args = ap.parse_args(argv)
    table = boto3.Session(region_name=args.region).resource("dynamodb").Table(args.table)
    json.dump({"task": "backfill_legacy_ticks", "rows": export_rows(table), "apply": False},
              sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
