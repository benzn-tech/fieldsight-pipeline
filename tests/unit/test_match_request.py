"""
Tests for src/match_request.py — Programme<->Item feedback, Task 4.

Shared S3 artifact emitter: mirrors lambda_embed_report's vector-sidecar
put_object idiom. A fake S3 client records put_object calls; assertions
check the deterministic key shape (idempotent overwrite on reprocess) and
the JSON body contract the non-VPC MatcherFunction (Task 3) reads off
match_requests/.
"""
import hashlib
import json

import pytest

mr = pytest.importorskip("match_request")


class FakeS3:
    def __init__(self):
        self.put_calls = []

    def put_object(self, **kwargs):
        self.put_calls.append(kwargs)


SOURCE_KEY = "extractions/Jarley_Trainor/2026-07-06/Benl1_2026-07-06_10-00-00.json"


def make_topic(**overrides):
    topic = {
        "topic_id": "topic-uuid-0",
        "title": "Safety Briefing",
        "summary": "Discussed PPE requirements.",
        "user_id": "user-1",
        "action_items": [{"text": "Order more hard hats"}],
    }
    topic.update(overrides)
    return topic


def test_emit_key_shape():
    s3 = FakeS3()
    expected_hash = hashlib.sha256(SOURCE_KEY.encode("utf-8")).hexdigest()[:16]

    key = mr.emit(s3, "bucket", "site-1", "2026-07-06", SOURCE_KEY, [make_topic()])

    assert len(expected_hash) == 16
    assert key == f"match_requests/site-1/2026-07-06/{expected_hash}.json"
    assert s3.put_calls[0]["Key"] == key
    # deterministic — same inputs, same key (idempotent overwrite on reprocess)
    key2 = mr.emit(s3, "bucket", "site-1", "2026-07-06", SOURCE_KEY, [make_topic()])
    assert key2 == key


def test_emit_empty_topics_returns_none():
    s3 = FakeS3()

    key = mr.emit(s3, "bucket", "site-1", "2026-07-06", SOURCE_KEY, [])

    assert key is None
    assert s3.put_calls == []


def test_emit_body_shape():
    s3 = FakeS3()
    topics = [make_topic()]

    key = mr.emit(s3, "my-bucket", "site-1", "2026-07-06", SOURCE_KEY, topics)

    assert len(s3.put_calls) == 1
    call = s3.put_calls[0]
    assert call["Bucket"] == "my-bucket"
    assert call["Key"] == key
    assert call["ContentType"] == "application/json"
    body = json.loads(call["Body"])
    assert body == {
        "site_id": "site-1",
        "report_date": "2026-07-06",
        "source_s3_key": SOURCE_KEY,
        "topics": topics,
    }
