"""
match_request.py — shared S3 artifact emitter (Programme<->Item feedback,
Task 4).

Mirrors lambda_embed_report's vector-sidecar put_object idiom (see that
module's `embed_report`): a small, dependency-free helper both
lambda_item_writer (session extractions) and lambda_ingest (nightly
reports) call AFTER their `with get_connection()` block commits, so the
Aurora `topics.id` values referenced in the artifact are durable before the
non-VPC MatcherFunction (Task 3, S3-triggered on this prefix) can act on
them.

Writes match_requests/{site_id}/{report_date}/{sha256(source_s3_key)[:16]}
.json — the key is deterministic (hash of the writer's own source key, not
a random uuid/timestamp), so re-processing the same extraction/report
overwrites the same artifact instead of piling up duplicates -- the same
source-key idempotency Phase 4a/4b topics/chunks already rely on.
"""
import hashlib
import json


def emit(s3, bucket, site_id, report_date, source_s3_key, topics):
    """Write a match_requests/ artifact for a batch of freshly-written
    topics. Returns the S3 key written, or None (no S3 call made) when
    `topics` is empty -- callers should skip calling this entirely on a
    zero-write/skip, but emit is itself safe/idempotent either way."""
    if not topics:
        return None

    key = (f"match_requests/{site_id}/{report_date}/"
           f"{hashlib.sha256(source_s3_key.encode('utf-8')).hexdigest()[:16]}.json")
    body = {
        "site_id": str(site_id),
        "report_date": str(report_date),
        "source_s3_key": source_s3_key,
        "topics": topics,
    }
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(body),
        ContentType="application/json",
    )
    return key
