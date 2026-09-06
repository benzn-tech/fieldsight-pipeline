"""Ask the DEPLOYED re-bind chain for one session's speaker groups, and say whether
the answer matches what a human confirmed.

The producer (`lambda_item_writer._request_rebind`) only fires on a final extraction
pass, and only for sessions finalized after the field existed. This builds the same
request from the transcripts directly, so a session can be checked without waiting for
one to be recorded.

    export MSYS_NO_PATHCONV=1
    uv run --with boto3 python scripts/rebind_verify.py \
        --bucket fieldsight-data-509194952652 \
        --user-folder Ben_UCPK2 --date 2026-08-27 \
        --session-base sid93396a6ac8434fdf908c25a50cc7e167 \
        --company-id <uuid> --put

Without `--put` it prints the request and writes nothing — always run that first.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import boto3  # noqa: E402
from transcript_utils import normalize_transcript  # noqa: E402


def build_turns(bucket, user_folder, date, session_base):
    """The `speaker_turns` payload item_writer would have sent, built from transcripts.

    Same shape and same vocabulary as `lambda_extract_session`'s artifact: the
    transcript's own `speaker_label`, and times relative to that transcript file — NOT
    absolute. `_window_audio` re-derives the audio coordinates itself.
    """
    s3 = boto3.client("s3")
    prefix = f"transcripts/{user_folder}/{date}/"
    turns = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            filename = key.rsplit("/", 1)[-1]
            if session_base not in filename or not filename.endswith(".json"):
                continue
            data = json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
            normalized = normalize_transcript(data, filename)
            if not normalized:
                print(f"  skipped unnormalizable {filename}", file=sys.stderr)
                continue
            for t in normalized.get("speaker_turns") or []:
                if not t.get("speaker"):
                    continue
                turns.append({"source_filename": filename,
                              "speaker_label": t.get("speaker"),
                              "start_sec": t.get("start_sec"),
                              "end_sec": t.get("end_sec")})
    return turns


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--user-folder", required=True)
    ap.add_argument("--date", required=True)
    ap.add_argument("--session-base", required=True)
    ap.add_argument("--company-id", required=True)
    ap.add_argument("--put", action="store_true",
                    help="actually write the request; omit for a dry run")
    a = ap.parse_args(argv)

    turns = build_turns(a.bucket, a.user_folder, a.date, a.session_base)
    pairs = {(t["source_filename"], t["speaker_label"]) for t in turns}
    calls = {src for src, _ in pairs}
    print(f"{len(turns)} turns, {len(pairs)} (call,label) pairs across {len(calls)} calls")
    if len(pairs) < 2 or len(calls) < 2:
        print("the producer would skip this session — nothing to group")
        return 1

    req = {"op": "rebind", "company_id": a.company_id,
           "session_base": a.session_base, "user_folder": a.user_folder,
           "date": a.date, "turns": turns}
    key = f"voiceprint_requests/{a.company_id}/{a.session_base}/rebind.json"
    if not a.put:
        print(f"DRY RUN — would put s3://{a.bucket}/{key}")
        return 0
    boto3.client("s3").put_object(Bucket=a.bucket, Key=key,
                                  Body=json.dumps(req).encode("utf-8"),
                                  ContentType="application/json")
    print(f"put s3://{a.bucket}/{key}")
    print("now: aws logs tail /aws/lambda/fieldsight-prod-speaker-embed --since 5m --follow")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
