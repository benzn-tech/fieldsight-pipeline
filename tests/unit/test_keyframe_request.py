"""Tests for src/keyframe_request.py (mirrors tests/unit/test_match_request.py style).

Deterministic post-commit S3 artifact emitter: item-writer (in-VPC, no
lambda:Invoke -- BUG-36) writes this after its topic commit so the durable
topic ids in the payload are visible before the S3-triggered KeyframeFunction
acts on them.
"""
import hashlib
import json

import keyframe_request


class FakeS3:
    def __init__(self):
        self.puts = []

    def put_object(self, **kw):
        self.puts.append(kw)


EXTRACTION_KEY = "extractions/Ben_UCPK/2026-07-23/Benl1_2026-07-23_10-15-34.json"


def test_emit_writes_deterministic_key_and_shape():
    s3 = FakeS3()
    topics = [{"topic_id": "aaaa-1", "time_range": "10:15 – 10:20"}]
    key = keyframe_request.emit(s3, "bkt", "Ben_UCPK", "2026-07-23",
                                "Benl1_2026-07-23_10-15-34", EXTRACTION_KEY, topics)
    digest = hashlib.sha256(EXTRACTION_KEY.encode()).hexdigest()[:16]
    assert key == f"keyframe_requests/Ben_UCPK/2026-07-23/{digest}.json"
    assert s3.puts[0]["Key"] == key
    assert s3.puts[0]["Bucket"] == "bkt"
    assert s3.puts[0]["ContentType"] == "application/json"
    body = json.loads(s3.puts[0]["Body"])
    assert body == {
        "user_folder": "Ben_UCPK",
        "date": "2026-07-23",
        "session_base": "Benl1_2026-07-23_10-15-34",
        "extraction_key": EXTRACTION_KEY,
        "topics": topics,
    }


def test_emit_deterministic_overwrite_on_reprocess():
    s3 = FakeS3()
    topics = [{"topic_id": "aaaa-1", "time_range": "10:15 – 10:20"}]
    k1 = keyframe_request.emit(s3, "bkt", "Ben_UCPK", "2026-07-23",
                               "Benl1_2026-07-23_10-15-34", EXTRACTION_KEY, topics)
    k2 = keyframe_request.emit(s3, "bkt", "Ben_UCPK", "2026-07-23",
                               "Benl1_2026-07-23_10-15-34", EXTRACTION_KEY, topics)
    assert k1 == k2  # keyed on the extraction key, not a uuid/timestamp


def test_emit_skips_empty_topics():
    s3 = FakeS3()
    assert keyframe_request.emit(s3, "bkt", "u", "d", "s", EXTRACTION_KEY, []) is None
    assert s3.puts == []
