"""keyframe_request.py -- post-commit S3 artifact emitter (video-keyframe plan).

Mirrors match_request.py: item-writer (in-VPC) calls emit() AFTER its
connection block commits, so the topics.id values in the artifact are durable
before the S3-triggered KeyframeFunction acts on them. Deterministic key
(sha256 of the extraction key) -> re-processing overwrites, never piles up.
In-VPC item-writer cannot lambda:Invoke (BUG-36) -- this artifact IS the
trigger.
"""
import hashlib
import json


def emit(s3, bucket, user_folder, date, session_base, extraction_key, topics):
    """topics: [{'topic_id': str, 'time_range': str}] -- already gate-filtered
    (>=2-minute windows only). Empty -> no S3 call, returns None. Returns the
    S3 key written otherwise."""
    if not topics:
        return None
    key = (f"keyframe_requests/{user_folder}/{date}/"
           f"{hashlib.sha256(extraction_key.encode('utf-8')).hexdigest()[:16]}.json")
    s3.put_object(
        Bucket=bucket, Key=key,
        Body=json.dumps({
            "user_folder": user_folder,
            "date": date,
            "session_base": session_base,
            "extraction_key": extraction_key,
            "topics": topics,
        }),
        ContentType="application/json",
    )
    return key
