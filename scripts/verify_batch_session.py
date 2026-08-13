#!/usr/bin/env python3
"""Check that one batched session came out right — against S3, not against the deploy record.

    uv run --with boto3 python scripts/verify_batch_session.py <session_id> [--env test]

Phase 7 item 3 of docs/superpowers/plans/2026-08-13-batch-by-wall-clock.md, written as a
script rather than a checklist because the manual version is the shape that has produced
five separate "I verified the wrong thing" findings in this project: it samples, it reads
the easy artifact, and it stops at the first thing that looks fine.

What it asserts, and why each one exists:

* **zero `_bn1` objects** — a window of one is meant to be handed back to the per-chunk
  path. A `_bn1` means the bypass did not run.
* **every uploaded chunk is accounted for** — in a batch, bypassed, or transcribed alone.
  A chunk in none of those is audio that vanished with no error anywhere, which is the
  failure mode the consumed-member bug produced.
* **no chunk appears in two batches** — the double-billing shape.
* **every batch transcript carries `fieldsight_batch_map`** — without it four consumers
  fall back to filename arithmetic, wrong by up to the bridged gap, silently.
* **a bridged gap is reported, not assumed** — the run says whether this session actually
  exercised the gap path. A green run on a session with no gap proves nothing about it, and
  saying so is the point.
"""
import argparse
import collections
import json
import re
import sys

import boto3

BUCKETS = {"test": "fieldsight-data-test-509194952652",
           "prod": "fieldsight-data-509194952652"}
CHUNK_RE = re.compile(r"_sid([0-9a-f]{32})_c(\d{4})")
BATCH_RE = re.compile(r"_sid([0-9a-f]{32})_c(\d{4})_bn(\d+)_")


def _keys(s3, bucket, prefix):
    out = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        out.extend(o["Key"] for o in page.get("Contents", []))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("session_id")
    ap.add_argument("--env", default="test", choices=sorted(BUCKETS))
    # The embedded map only exists in transcripts written after the transcriber learned to
    # embed it. Without a cutoff this script reports every pre-change artifact as a failure
    # forever, which trains its reader to skim past failures -- the opposite of the job.
    # UPDATE THIS if the embed is ever redeployed from scratch.
    ap.add_argument("--embed-since", default="2026-08-13T00:30:51+00:00",
                    help="UTC ISO time the transcriber began embedding the batch map")
    args = ap.parse_args()
    sid, bucket = args.session_id, BUCKETS[args.env]
    s3 = boto3.client("s3", region_name="ap-southeast-2")

    uploaded, units, batches, transcripts = set(), set(), {}, set()
    for key in _keys(s3, bucket, "users/"):
        m = CHUNK_RE.search(key)
        if m and m.group(1) == sid and key.endswith(".wav"):
            uploaded.add(int(m.group(2)))
    for key in _keys(s3, bucket, "audio_segments/"):
        if sid not in key or not key.endswith(".wav"):
            continue
        b = BATCH_RE.search(key)
        if b:
            batches[key] = (int(b.group(2)), int(b.group(3)))
        else:
            m = CHUNK_RE.search(key)
            if m:
                units.add(int(m.group(2)))
    for key in _keys(s3, bucket, "transcripts/"):
        if sid in key and key.endswith(".json"):
            transcripts.add(key)

    print(f"session {sid[:8]} on {args.env}")
    print(f"  uploaded chunks   {len(uploaded)}")
    print(f"  surviving units   {len(units)}   (VAD dropped {len(uploaded - units)})")
    print(f"  batch objects     {len(batches)}")
    print(f"  transcripts       {len(transcripts)}")

    problems, notes = [], []

    bn1 = [k for k, (_, n) in batches.items() if n == 1]
    if bn1:
        problems.append(f"{len(bn1)} `_bn1` object(s) - the singleton bypass did not run: "
                        + ", ".join(k.rsplit('/', 1)[-1] for k in bn1[:3]))

    covered, gapped = collections.Counter(), []
    for key in batches:
        map_key = key.rsplit(".", 1)[0] + "_batch_map.json"
        try:
            doc = json.loads(s3.get_object(Bucket=bucket, Key=map_key)["Body"].read())
        except Exception as e:
            problems.append(f"no readable map beside {key.rsplit('/', 1)[-1]} ({e.__class__.__name__})")
            continue
        for m in doc["members"]:
            covered[int(m["chunk_index"])] += 1
        if any(m.get("seam") == "gap" for m in doc["members"]):
            gapped.append(key.rsplit("/", 1)[-1])

    doubled = sorted(i for i, n in covered.items() if n > 1)
    if doubled:
        problems.append(f"chunks in more than one batch - transcribed and billed twice: {doubled}")

    transcribed_alone = set()
    for key in transcripts:
        if "_bn" in key:
            continue
        m = CHUNK_RE.search(key)
        if m:
            transcribed_alone.add(int(m.group(2)))
    unaccounted = sorted(units - set(covered) - transcribed_alone)
    if unaccounted:
        problems.append(f"chunks in NO batch, no bypass and no transcript — audio lost "
                        f"with no error anywhere: {unaccounted}")

    from datetime import datetime
    cutoff = datetime.fromisoformat(args.embed_since)
    stale = 0
    for key in transcripts:
        if "_bn" not in key:
            continue
        head = s3.head_object(Bucket=bucket, Key=key)
        doc = json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
        if "fieldsight_batch_map" in doc:
            continue
        if head["LastModified"] < cutoff:
            stale += 1                      # written before the embed existed
            continue
        problems.append(f"batch transcript without an embedded map - every consumer "
                        f"falls back to filename arithmetic: {key.rsplit('/', 1)[-1]}")
    if stale:
        notes.append(f"{stale} batch transcript(s) predate the embed ({args.embed_since}) "
                     f"and are expected to have no map; they say nothing either way")

    if gapped:
        notes.append(f"{len(gapped)} batch(es) bridge a VAD-dropped chunk: {', '.join(gapped[:2])}")
    else:
        notes.append("NO batch in this session bridges a gap - so this run says nothing "
                     "about the gap path. It needs a session where VAD dropped an interior "
                     "chunk (37% of real sessions).")

    print()
    for n in notes:
        print(f"  note: {n}")
    for p in problems:
        print(f"  FAIL: {p}")
    print("\n" + ("PASS" if not problems else f"{len(problems)} PROBLEM(S)"))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
