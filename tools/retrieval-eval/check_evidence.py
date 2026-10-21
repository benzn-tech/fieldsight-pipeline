#!/usr/bin/env python3
"""Does each gold item's evidence still exist in report_chunks?

Read-only (SELECT through the RDS Data API). For every answerable item, fetches all
chunks of the pinned author/site on the item's date and applies the item's
`match_any` regexes in Python (the same matching run.py scores with), so a gold
item whose words are no longer in any chunk -- re-chunked, re-transcribed or
deleted since the questions were written -- is listed instead of being counted as
a retrieval miss.

`--family report|session` restricts the chunks to that index family with
production's own `repositories.search_sql.family_predicate` (exported at
EVAL_SRC_REF, default origin/main), so `never_retrieved` in the Ask v2 mode can be
told from "not indexed in that family" (spec §5).

    python tools/retrieval-eval/check_evidence.py --gold tools/retrieval-eval/gold/v1.jsonl
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

CLUSTER = ("arn:aws:rds:ap-southeast-2:509194952652:cluster:"
           "fieldsight-db-test-dbcluster-hywiixu8ihi9")
SECRET = ("arn:aws:secretsmanager:ap-southeast-2:509194952652:secret:"
          "rds!cluster-1757a281-ee31-460d-b56e-950817921010-Ansbey")
PAGE = 100


def chunk_sql(family_predicate):
    """One page of the author's chunks on one day; with `family_predicate` (production's
    function) also that family, its parameter bound as :source_family."""
    sql = ("SELECT c.chunk_type, c.chunk_text FROM report_chunks c WHERE "
           "c.site_id = CAST(:s AS uuid) AND c.user_id = CAST(:u AS uuid) "
           "AND c.report_date = CAST(:d AS date) ")
    if family_predicate is not None:
        pred = family_predicate("c")
        if "%(source_family)s::text" not in pred:
            raise SystemExit("family_predicate no longer binds %(source_family)s::text")
        pred = pred.replace("%(source_family)s::text", "CAST(:source_family AS text)")
        pred = pred.replace("%%", "%")
        if "%(" in pred:
            raise SystemExit("unrewritten parameter left in family_predicate")
        sql += "AND " + pred + " "
    return sql + "ORDER BY c.id LIMIT %d OFFSET :o" % PAGE


def chunk_params(site, author, date, offset, family):
    params = [{"name": "s", "value": {"stringValue": site}},
              {"name": "u", "value": {"stringValue": author}},
              {"name": "d", "value": {"stringValue": date}},
              {"name": "o", "value": {"longValue": offset}}]
    if family is not None:
        params.append({"name": "source_family", "value": {"stringValue": family}})
    return params


def _family_predicate():
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from askv2.srcref import export_src
    src, _ = export_src(os.environ.get("EVAL_SRC_REF", "origin/main"))
    sys.path.insert(0, str(src))
    from repositories.search_sql import family_predicate
    return family_predicate


def main(argv=None):
    import boto3
    ap = argparse.ArgumentParser()
    ap.add_argument("--gold", required=True)
    ap.add_argument("--database", default="fieldsight")
    ap.add_argument("--site", default="4326dbc7-14a2-4251-8381-9d0067595a85")
    ap.add_argument("--author", default="f531eb99-d79c-4e6c-bac1-ff944b802349")
    ap.add_argument("--family", choices=["report", "session"], default=None,
                    help="only chunks of this index family (production's family_predicate)")
    args = ap.parse_args(argv)

    rds = boto3.client("rds-data", region_name="ap-southeast-2")
    sql = chunk_sql(_family_predicate() if args.family else None)

    def query(params):
        r = rds.execute_statement(resourceArn=CLUSTER, secretArn=SECRET, database=args.database,
                                  sql=sql, parameters=params, formatRecordsAs="JSON")
        return json.loads(r.get("formattedRecords") or "[]")

    items = [json.loads(line) for line in Path(args.gold).read_text(encoding="utf-8").splitlines()
             if line.strip()]
    chunks_by_date = {}
    for date in sorted({i["date"] for i in items if i.get("expect") == "answerable"}):
        rows, offset = [], 0
        while True:
            page = query(chunk_params(args.site, args.author, date, offset, args.family))
            rows += page
            if len(page) < PAGE:
                break
            offset += PAGE
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
    print(json.dumps({"family": args.family, "answerable": len(report), "missing": missing,
                      "chunks_per_date": {d: len(r) for d, r in chunks_by_date.items()},
                      "items": report}, indent=1))


if __name__ == "__main__":
    main()
