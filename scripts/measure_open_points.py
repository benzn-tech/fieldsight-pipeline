#!/usr/bin/env python3
"""What does the open-point gate actually fire on, in a real meeting?

NOT a pass/fail check, and it must not become one. It answers two questions that
a unit test cannot, because a unit test only sees the sentences someone thought
to write down:

  --transcripts  how often the marker gate fires on real speech, and on what.
                 This is where 我觉得 was found to be a stance marker rather than
                 an uncertainty one: it produced three of five hits on the
                 2026-08-27 session and none of the three was an open point.
  --briefs       how many candidates the model produced, how many survived
                 admission, and how many were resolved.

There are two briefs in existence, so any number this prints describes two
sessions. It exists so the numbers are re-derivable rather than remembered, and
so the rates can be watched as briefs accumulate.

    AWS_PROFILE=fieldsight-deployer python scripts/measure_open_points.py --transcripts
    AWS_PROFILE=fieldsight-deployer python scripts/measure_open_points.py --briefs
"""
import argparse
import json
import re
import sys

sys.path.insert(0, "src")
import open_points as op   # noqa: E402

BUCKET = "fieldsight-data-test-509194952652"
_SENT = re.compile(r"(?<=[.!?。！？])\s+")


def _s3():
    import boto3
    return boto3.client("s3", region_name="ap-southeast-2")


def _keys(s3, prefix):
    return [o["Key"] for page in s3.get_paginator("list_objects_v2")
            .paginate(Bucket=BUCKET, Prefix=prefix)
            for o in page.get("Contents", [])]


def transcripts(s3, prefix):
    """Fire rate on real speech. A high rate is a warning, not a success: the
    gate is meant to be rare, and a marker that fires on 5% of a meeting is
    admitting a turn of phrase rather than an uncertainty."""
    keys = [k for k in _keys(s3, prefix) if k.endswith(".json")]
    if not keys:
        print(f"no transcripts under {prefix}")
        return 2
    text = []
    for k in keys:
        d = json.loads(s3.get_object(Bucket=BUCKET, Key=k)["Body"].read())
        for t in (d.get("results") or {}).get("transcripts") or []:
            if t.get("transcript"):
                text.append(t["transcript"])
    sents = [s.strip() for s in _SENT.split(" ".join(text)) if s.strip()]
    hits = [s for s in sents if op.has_uncertainty_marker(s)]
    print(f"{len(keys)} file(s), {len(sents)} sentences, "
          f"gate fires on {len(hits)} ({100 * len(hits) / max(len(sents), 1):.1f}%)")
    for h in hits:
        print("  -", h[:180])
    return 0


def briefs(s3):
    keys = [k for k in _keys(s3, "session_brief/") if k.endswith(".json")]
    if not keys:
        print("no briefs -- nothing to measure")
        return 2
    for k in keys:
        b = json.loads(s3.get_object(Bucket=BUCKET, Key=k)["Body"].read())
        st = b.get("stats") or {}
        if "open_points_admitted" not in st:
            print(f"  {k}: written before open points existed -- skipped")
            continue
        rej = st.get("open_points_rejected") or {}
        print(f"  {'/'.join(k.split('/')[1:3])}: admitted={st['open_points_admitted']} "
              f"resolved={st.get('open_points_resolved', 0)} rejected={rej or '{}'}")
        for p in b.get("open_points") or []:
            mark = "resolved" if p.get("resolution") else "open"
            print(f"      [{p.get('kind')}/{mark}] {(p.get('claim') or '')[:90]}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--transcripts", action="store_true")
    ap.add_argument("--briefs", action="store_true")
    ap.add_argument("--prefix", default="transcripts/Ben_UCPK2/2026-08-27/")
    a = ap.parse_args()
    if not (a.transcripts or a.briefs):
        ap.error("pick --transcripts or --briefs")
    s3 = _s3()
    rc = 0
    if a.transcripts:
        rc |= transcripts(s3, a.prefix)
    if a.briefs:
        rc |= briefs(s3)
    return rc


if __name__ == "__main__":
    sys.exit(main())
