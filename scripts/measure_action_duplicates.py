#!/usr/bin/env python3
"""Every action item in the extraction artifacts, and how often two of them are the same one.

    uv run --with boto3 python scripts/measure_action_duplicates.py [--bucket B] [--json out]

READ-ONLY. Lists and gets objects under `extractions/`; writes nothing to S3 or the database.

Produced the numbers in `docs/superpowers/specs/2026-08-30-todo-version-history.md`, and the
reason it exists is that the rule it measures was proposed before anyone had looked: *same
thread AND near-duplicate text AND same responsible*. Exactly one of the 29 candidate pairs in
production shares a non-empty `responsible`, so as a conjunct that rule is an off switch.

Three things it deliberately does or refuses to do:

* **It does not read the database.** `thread_id` lives on `topics` in Aurora, so the thread
  axis is not available here and pairs are grouped by recorder folder instead. An earlier note
  claiming this measurement needs no database at all was wrong about that, and this is where
  the correction lands.
* **It does not group by the artifact's site.** `declared_site` is null on every action
  measured; grouping by it groups by null, finds no pairs, and reads like a clean result.
* **It reports same-day and cross-day pairs separately, and they must stay separate.** Only a
  cross-day pair is the restatement a version history exists to show. Summing them reports a
  feature as viable on evidence that is mostly duplication.

A same-day pair is NOT the live tier against the final tier: `extraction_key` puts both on one
S3 key so the final pass supersedes the live one in place (`lambda_extract_session.py`), and
this script skips pairs sharing a `session_base` anyway. The sources that remain are a split
session, and group merge — the merged artifact takes its own `grp...` base while the members'
artifacts stay in S3 even after `_delete_member_topics` removes the members' database rows.

That last one means the artifact corpus can hold pairs that never coexist where a user looks,
so a duplicate found here is not by itself a customer-visible defect. Checked separately on
2026-08-30 through the live `GET /api/org/timeline?date=2026-08-10&user=Ben_UCPK2`: the
database returned 35 actions over 12 sessions with `Scaffolding -- inspect before Monday`
present **three times**. Same-day duplication is real in the read model — but that cross-check
is a second measurement, not something this script proves.
"""
import argparse
import collections
import difflib
import itertools
import json
import re
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor

import boto3
from botocore.config import Config

STOP = set("the a an and or but is are was to of in on at it that this for with from as by "
           "be will need needs should".split())

# Two thresholds, not one, and both are deliberately loose. This script measures how much
# material a rule would have to work with; a tight threshold here would answer the question
# by assuming it.
JACCARD = 0.4
RATIO = 0.6


def tokens(s):
    return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower())
            if w not in STOP and len(w) > 2}


def jaccard(a, b):
    return len(a & b) / len(a | b) if a and b else 0.0


def collect(bucket):
    s3 = boto3.client("s3", config=Config(max_pool_connections=32))
    keys, token = [], None
    while True:
        kw = {"Bucket": bucket, "Prefix": "extractions/"}
        if token:
            kw["ContinuationToken"] = token
        page = s3.list_objects_v2(**kw)
        keys += [o["Key"] for o in page.get("Contents", []) if o["Key"].endswith(".json")]
        token = page.get("NextContinuationToken")
        if not token:
            break

    def one(key):
        try:
            doc = json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read()
                             .decode("utf-8"))
        except Exception as exc:                      # noqa: BLE001 - reported, not raised
            return key, None, type(exc).__name__
        rows = []
        for topic in doc.get("topics") or []:
            for a in topic.get("action_items") or []:
                rows.append({"key": key, "folder": doc.get("user_folder"),
                             "date": doc.get("date"), "session": doc.get("session_base"),
                             "tier": doc.get("tier"), "site": doc.get("declared_site"),
                             "topic": topic.get("topic_title"),
                             "action": a.get("action"), "responsible": a.get("responsible"),
                             "deadline": a.get("deadline"), "priority": a.get("priority")})
        return key, rows, None

    rows, errors, empty = [], [], 0
    with ThreadPoolExecutor(max_workers=24) as pool:
        for key, got, err in pool.map(one, keys):
            if err:
                errors.append((key, err))
                continue
            if not got:
                empty += 1
            rows += got
    return {"artifacts": len(keys), "errors": errors, "without_actions": empty, "rows": rows}


def pairs_of(rows):
    """Candidate pairs from DIFFERENT sessions of one recorder, split by same/cross day."""
    same_day, cross_day = [], []
    for i, j in itertools.combinations(range(len(rows)), 2):
        a, b = rows[i], rows[j]
        if a["session"] == b["session"] or a["folder"] != b["folder"]:
            continue
        jac = jaccard(tokens(a["action"]), tokens(b["action"]))
        ratio = difflib.SequenceMatcher(None, (a["action"] or "").lower(),
                                        (b["action"] or "").lower()).ratio()
        if jac < JACCARD and ratio < RATIO:
            continue
        (same_day if a["date"] == b["date"] else cross_day).append(
            {"jaccard": round(jac, 2), "ratio": round(ratio, 2), "a": a, "b": b})
    return same_day, cross_day


def clusters_of(pairs):
    """Connected components over the pairs — the unit the V2 gate is stated in.

    Pair count is the wrong measure and this function exists to stop it being used: a chain
    of one subject seen on four days contributes six pairs, so pair count grows with how long
    a chain runs rather than with how many distinct things there are to learn from. The 12
    cross-day pairs measured on 2026-08-30 are 4 subjects.
    """
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    def key(row):
        return (row["date"], row["session"], row["action"])

    for p in pairs:
        union(key(p["a"]), key(p["b"]))
    groups = {}
    for k in list(parent):
        groups.setdefault(find(k), []).append(k)
    return sorted(groups.values(), key=lambda g: (min(x[0] for x in g), len(g)))


def same_responsible(pair):
    x = (pair["a"]["responsible"] or "").strip().lower()
    y = (pair["b"]["responsible"] or "").strip().lower()
    return bool(x) and x == y


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bucket", default="fieldsight-data-509194952652")
    ap.add_argument("--json", help="also write the collected rows and pairs here")
    args = ap.parse_args()

    got = collect(args.bucket)
    rows = got["rows"]
    print(f"artifacts={got['artifacts']} without-actions={got['without_actions']} "
          f"errors={len(got['errors'])} actions={len(rows)}")
    if got["errors"]:
        print("  errors:", collections.Counter(e for _, e in got["errors"]))

    per = collections.Counter((r["folder"], r["date"], r["session"]) for r in rows)
    if per:
        print(f"sessions with >=1 action: {len(per)} | per session: "
              f"median {statistics.median(per.values())} max {max(per.values())}")
    for field in ("responsible", "deadline", "site"):
        n = sum(1 for r in rows if (str(r[field]).strip() if r[field] else ""))
        print(f"{field:12s} present: {n}/{len(rows)}")
    print("tiers:", dict(collections.Counter(r["tier"] for r in rows)))
    print("folders:", collections.Counter(r["folder"] for r in rows).most_common(8))

    same_day, cross_day = pairs_of(rows)
    same_clusters, cross_clusters = clusters_of(same_day), clusters_of(cross_day)
    print(f"\nnear-duplicate pairs across sessions of one recorder "
          f"(jaccard>={JACCARD} or ratio>={RATIO}):")
    print(f"  same day  : {len(same_day):4d} pairs -> {len(same_clusters):3d} clusters"
          f"   <- one event extracted twice, NOT a version")
    print(f"  cross day : {len(cross_day):4d} pairs -> {len(cross_clusters):3d} clusters"
          f"   <- the only shape a version history is for")
    print(f"  same non-empty responsible: same-day {sum(map(same_responsible, same_day))}, "
          f"cross-day {sum(map(same_responsible, cross_day))}")
    print(f"\n  THE V2 GATE reads the cross-day CLUSTER count above ({len(cross_clusters)}), "
          f"not the pair count.")

    print("\ncross-day clusters in full (this is the corpus a matcher would be tuned on):")
    for group in cross_clusters:
        dates = sorted({x[0] for x in group})
        print(f"  {dates[0]} .. {dates[-1]}  ({len(dates)} days, {len(group)} occurrences)")
        for _, _, action in sorted(group):
            print(f"    {(action or '')[:76]}")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"rows": rows, "same_day": same_day, "cross_day": cross_day}, fh,
                      ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
