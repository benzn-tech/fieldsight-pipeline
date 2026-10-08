#!/usr/bin/env python3
"""Manage the ASR keyterm table (src/asr_vocabulary.py). Self-contained: needs only
boto3, so it can be uploaded to AWS CloudShell and run against prod as is.

    python asr_vocab.py --stage test list [--status candidate]
    python asr_vocab.py --stage test seed --file custom_vocabulary_construction_nz.txt
    python asr_vocab.py --stage test add Hirepool --heard "Hiab" --heard "hair pool" \
        --date 2026-10-08 --folder Ben_Lin_test2 --session sid5cc1... [--status candidate]
    python asr_vocab.py --stage test retire Hiab --note "pulled Hirepool to Hiab"

`add` on an existing term keeps its status unless --status is given, and appends
what it was heard as and where. Every write stamps added_by/updated_at.
"""
import argparse
import datetime as dt
import getpass
import os
import sys

STATUSES = ("active", "candidate", "retired")
MAX_TERM_LEN = 49                       # scribe_v2 requires each term < 50 chars


def table_name(stage):
    return "fieldsight-%s-asr-vocabulary" % stage


def file_terms(path):
    """The Phrase column, as src/elevenlabs_utils.load_keyterms reads it."""
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            cols = line.split("\t")
            if cols[:2] == ["Phrase", "SoundsLike"]:
                continue
            if cols[0].strip():
                out.append(cols[0].strip()[:MAX_TERM_LEN])
    return out


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _who():
    return os.environ.get("ASR_VOCAB_BY") or getpass.getuser()


def seed(table, terms, by=None, now=None):
    """Each file term as an active row; a term already in the table is left alone,
    so a re-seed never undoes a retire or overwrites what someone wrote."""
    from botocore.exceptions import ClientError
    added = 0
    for term in terms:
        try:
            table.put_item(Item={"term": term, "status": "active", "category": "seed",
                                 "note": "from custom_vocabulary_construction_nz.txt",
                                 "added_by": by or _who(), "added_at": now or _now(),
                                 "updated_at": now or _now()},
                           ConditionExpression="attribute_not_exists(term)")
            added += 1
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
    return added


def add(table, term, *, status=None, heard=(), evidence=None, note=None, category=None,
        by=None, now=None):
    term = term.strip()[:MAX_TERM_LEN]
    if not term:
        raise ValueError("empty term")
    if status is not None and status not in STATUSES:
        raise ValueError("status must be one of %s" % (STATUSES,))
    item = table.get_item(Key={"term": term}).get("Item") or {
        "term": term, "status": status or "active", "added_by": by or _who(),
        "added_at": now or _now(), "misheard_as": [], "evidence": []}
    if status:
        item["status"] = status
    for h in heard:
        if h and h not in item.setdefault("misheard_as", []):
            item["misheard_as"].append(h)
    if evidence:
        item.setdefault("evidence", []).append({k: v for k, v in evidence.items() if v})
    if note:
        item["note"] = note
    if category:
        item["category"] = category
    item["updated_at"] = now or _now()
    item["updated_by"] = by or _who()
    table.put_item(Item=item)
    return item


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--stage", choices=("test", "prod"), required=True)
    ap.add_argument("--profile", default=None, help="AWS profile (omit in CloudShell)")
    ap.add_argument("--region", default="ap-southeast-2")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list")
    ls.add_argument("--status", choices=STATUSES)
    sd = sub.add_parser("seed")
    sd.add_argument("--file", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                                                   "src", "config",
                                                   "custom_vocabulary_construction_nz.txt"))
    ad = sub.add_parser("add")
    ad.add_argument("term")
    ad.add_argument("--status", choices=STATUSES)
    ad.add_argument("--heard", action="append", default=[])
    ad.add_argument("--date"); ad.add_argument("--folder"); ad.add_argument("--session")
    ad.add_argument("--note"); ad.add_argument("--category")
    rt = sub.add_parser("retire")
    rt.add_argument("term")
    rt.add_argument("--note")
    a = ap.parse_args(argv)

    import boto3
    session = boto3.Session(profile_name=a.profile, region_name=a.region)
    table = session.resource("dynamodb").Table(table_name(a.stage))

    if a.cmd == "list":
        rows, kw = [], {}
        while True:
            page = table.scan(**kw)
            rows += page.get("Items") or []
            if "LastEvaluatedKey" not in page:
                break
            kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        rows = [r for r in rows if not a.status or r.get("status") == a.status]
        for r in sorted(rows, key=lambda r: (r.get("status", ""), r["term"].lower())):
            heard = ", ".join(r.get("misheard_as") or [])
            print("%-9s %-32s %s" % (r.get("status"), r["term"], ("heard as: " + heard) if heard else ""))
        print("%d rows" % len(rows), file=sys.stderr)
    elif a.cmd == "seed":
        terms = file_terms(a.file)
        print("seeded %d of %d file terms (existing rows untouched)" % (seed(table, terms), len(terms)))
    elif a.cmd == "add":
        item = add(table, a.term, status=a.status, heard=a.heard, note=a.note, category=a.category,
                   evidence={"date": a.date, "folder": a.folder, "session": a.session,
                             "heard": ", ".join(a.heard)} if (a.date or a.session) else None)
        print("%s: %s" % (item["term"], item["status"]))
    elif a.cmd == "retire":
        item = add(table, a.term, status="retired", note=a.note)
        print("%s: retired" % item["term"])


if __name__ == "__main__":
    main()
