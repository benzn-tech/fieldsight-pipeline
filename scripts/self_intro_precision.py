"""Precision gate for self-introduction suggestions: run the detector over real transcripts.

The plan ships the feature only if at least 80% of what it suggests is a genuine
first-person introduction with the right name. This lists every hit over a date range so
a person can mark each one; it proves nothing on its own.

  python scripts/self_intro_precision.py <bucket> <since YYYY-MM-DD> <out.json>

Reads `transcripts/<folder>/<date>/*.json`, normalises each file with the same function the
extraction uses, and runs `self_introduction.find` over its turns.
"""
import json
import sys

import boto3

sys.path.insert(0, "src")
import self_introduction  # noqa: E402
import transcript_utils  # noqa: E402

bucket, since, out = sys.argv[1], sys.argv[2], sys.argv[3]
s3 = boto3.client("s3", region_name="ap-southeast-2")

hits, files = [], 0
for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix="transcripts/"):
    for obj in page.get("Contents", []):
        key = obj["Key"]
        parts = key.split("/")
        if len(parts) != 4 or not key.endswith(".json") or parts[2] < since:
            continue
        try:
            data = json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
            turns = transcript_utils.normalize_transcript(data, parts[3]).get("speaker_turns") or []
        except Exception as exc:  # a malformed file is a skip, not a crash
            print("skip", key, exc)
            continue
        files += 1
        for t in turns:
            t.setdefault("source_filename", parts[3])
        for h in self_introduction.find(turns):
            hits.append(dict(h, key=key))

json.dump({"files": files, "hits": hits}, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(f"files={files} hits={len(hits)}")
