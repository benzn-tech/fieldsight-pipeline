#!/usr/bin/env python3
"""Does each gold item's evidence still exist in report_chunks?

Read-only (SELECT through the RDS Data API). For every answerable item, fetches all
chunks of the pinned author/site on the item's date and applies the item's
`match_any` regexes in Python (the same matching run.py scores with), so a gold
item whose words are no longer in any chunk -- re-chunked, re-transcribed or
deleted since the questions were written -- is listed instead of being counted as
a retrieval miss.

    python tools/retrieval-eval/check_evidence.py --gold tools/retrieval-eval/gold/v1.jsonl
"""
import argparse
import json
import re
from pathlib import Path

import boto3

CLUSTER = ("arn:aws:rds:ap-southeast-2:509194952652:cluster:"
           "fieldsight-db-test-dbcluster-hywiixu8ihi9")
SECRET = ("arn:aws:secretsmanager:ap-southeast-2:509194952652:secret:"
          "rds!cluster-1757a281-ee31-460d-b56e-950817921010-Ansbey")

ap = argparse.ArgumentParser()
ap.add_argument("--gold", required=True)
ap.add_argument("--database", default="fieldsight")
ap.add_argument("--site", default="4326dbc7-14a2-4251-8381-9d0067595a85")
ap.add_argument("--author", default="f531eb99-d79c-4e6c-bac1-ff944b802349")
args = ap.parse_args()

rds = boto3.client("rds-data", region_name="ap-southeast-2")


def query(sql, params):
    r = rds.execute_statement(resourceArn=CLUSTER, secretArn=SECRET, database=args.database,
                              sql=sql, parameters=params, formatRecordsAs="JSON")
    return json.loads(r.get("formattedRecords") or "[]")


items = [json.loads(line) for line in Path(args.gold).read_text(encoding="utf-8").splitlines()
         if line.strip()]
chunks_by_date = {}
for date in sorted({i["date"] for i in items if i.get("expect") == "answerable"}):
    rows, offset = [], 0
    while True:
        page = query(
            "SELECT chunk_type, chunk_text FROM report_chunks WHERE site_id = CAST(:s AS uuid) "
            "AND user_id = CAST(:u AS uuid) AND report_date = CAST(:d AS date) "
            "ORDER BY id LIMIT 100 OFFSET :o",
            [{"name": "s", "value": {"stringValue": args.site}},
             {"name": "u", "value": {"stringValue": args.author}},
             {"name": "d", "value": {"stringValue": date}},
             {"name": "o", "value": {"longValue": offset}}])
        rows += page
        if len(page) < 100:
            break
        offset += 100
    chunks_by_date[date] = rows

missing, report = [], []
for it in items:
    if it.get("expect") != "answerable":
        continue
    pats = [re.compile(p, re.I) for e in it["evidence"] for p in e["match_any"]]
    rows = chunks_by_date[it["date"]]
    hits = [r for r in rows if any(p.search(r["chunk_text"] or "") for p in pats)]
    report.append({"id": it["id"], "date": it["date"], "chunks_on_date": len(rows),
                   "chunks_with_evidence": len(hits)})
    if not hits:
        missing.append(it["id"])
print(json.dumps({"answerable": len(report), "missing": missing,
                  "chunks_per_date": {d: len(r) for d, r in chunks_by_date.items()},
                  "items": report}, indent=1))
