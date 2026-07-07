"""
Tests for src/lambda_embed_report.py — Phase 4d, Task 2 (TDD).

Style mirrors tests/unit/test_lambda_ingest.py (FakeS3 object-store double,
module-level monkeypatch of the cached _s3_client global) and
tests/unit/test_lambda_extract_session.py (dummy AWS env vars so an eager
boto3.client('s3') at import time never blows up on a missing credential
provider).

lambda_embed_report reuses lambda_ingest._load_turns(user_folder, date) for
transcript loading (same module-level function, same S3 bucket in
production -- both Lambdas' S3_BUCKET env var points at the one ingest
bucket). In tests that means BOTH modules' _s3_client/S3_BUCKET must be
wired to the same FakeS3 instance -- see the `wired` fixture below.

CRITICAL: this file's cross-check tests import lambda_ingest directly and
assert lambda_embed_report's hash expression produces a key that
lambda_ingest.embed_from_sidecar actually looks up successfully. If the two
sides' hash expressions (sha256 of chunk_text[:8000], utf-8) ever diverge,
EVERY vector lookup misses and the whole backfill produces zero chunks --
this is the single most load-bearing contract in Phase 4d.
"""
import hashlib
import io
import json
import os

import pytest

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")

ler = pytest.importorskip("lambda_embed_report", reason="requires psycopg (lambda_ingest import, installed in CI)")
ing = pytest.importorskip("lambda_ingest", reason="requires psycopg (installed in CI)")
du = pytest.importorskip("dashscope_utils", reason="requires urllib3 (installed in CI)")

BUCKET = "test-bucket"
REPORT_KEY = "reports/2026-03-02/Jarley_Trainor/daily_report.json"


class FakeS3:
    """Minimal S3 client double: object store keyed by S3 key, records puts."""

    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.put_calls = []

    def get_object(self, Bucket, Key):
        body = self.objects[Key]
        raw = body.encode("utf-8") if isinstance(body, str) else body
        return {"Body": io.BytesIO(raw)}

    def put_object(self, **kwargs):
        self.put_calls.append(kwargs)
        self.objects[kwargs["Key"]] = kwargs["Body"]
        return {}

    def get_paginator(self, op):
        assert op == "list_objects_v2"
        return _FakePaginator(self.objects)


class _FakePaginator:
    def __init__(self, objects):
        self.objects = objects

    def paginate(self, Bucket, Prefix):
        contents = [{"Key": k} for k in self.objects if k.startswith(Prefix)]
        yield {"Contents": contents}


class _FakeHTTPResponse:
    def __init__(self, status, payload):
        self.status = status
        self.data = json.dumps(payload).encode("utf-8")


def make_report(n_topics=1, **overrides):
    topics = []
    for i in range(n_topics):
        topics.append({
            "topic_id": i,
            "time_range": f"{9 + i:02d}:00 – {9 + i:02d}:05",
            "topic_title": f"Topic {i}",
            "category": "safety",
            "participants": ["Jarley Trainor"],
            "summary": f"Summary text for topic {i}.",
            "key_decisions": [],
            "action_items": [],
            "safety_flags": [],
        })
    report = {
        "report_date": "2026-03-02",
        "user_name": "Jarley Trainor",
        "site": "Test Site",
        "topics": topics,
    }
    report.update(overrides)
    return report


def _wire_s3(monkeypatch, fake_s3, bucket=BUCKET):
    """Point BOTH lambda_embed_report's own client and lambda_ingest's (used
    internally by _load_turns) at the same fake object store/bucket."""
    monkeypatch.setattr(ler, "_s3_client", fake_s3)
    monkeypatch.setattr(ler, "S3_BUCKET", bucket)
    monkeypatch.setattr(ing, "_s3_client", fake_s3)
    monkeypatch.setattr(ing, "S3_BUCKET", bucket)


@pytest.fixture
def wired(monkeypatch):
    fake_s3 = FakeS3({REPORT_KEY: json.dumps(make_report())})
    _wire_s3(monkeypatch, fake_s3)
    monkeypatch.setattr(
        ler.dashscope_utils, "embed",
        lambda texts, dim=None: [[i / 1000.0] * 1024 for i in range(len(texts))],
    )
    return fake_s3


# ---------------------------------------------------------------------------
# S3 event key parsing -- depth-exact reports/{date}/{user}/daily_report.json
# ---------------------------------------------------------------------------

def test_key_parsing_depth_exact(monkeypatch):
    calls = []
    monkeypatch.setattr(
        ler, "embed_report",
        lambda date, user_folder, key: calls.append((date, user_folder, key))
        or {"report": key, "chunks": 1, "vectors": 1},
    )
    # S3 event notifications encode spaces as '+' -- must be unquote_plus'd.
    event = {"Records": [{"s3": {"object": {
        "key": "reports/2026-03-02/Jarley+Trainor/daily_report.json"}}}]}

    result = ler.lambda_handler(event, None)

    assert calls == [("2026-03-02", "Jarley Trainor",
                      "reports/2026-03-02/Jarley Trainor/daily_report.json")]
    assert result == {"results": [{"report": "reports/2026-03-02/Jarley Trainor/daily_report.json",
                                    "chunks": 1, "vectors": 1}]}


def test_key_parsing_skips_wrong_depth_or_prefix(monkeypatch):
    calls = []
    monkeypatch.setattr(ler, "embed_report", lambda *a: calls.append(a))
    event = {"Records": [
        {"s3": {"object": {"key": "reports/2026-03-02/daily_report.json"}}},          # too shallow
        {"s3": {"object": {"key": "reports/2026-03-02/A/B/daily_report.json"}}},      # too deep
        {"s3": {"object": {"key": "reports/2026-03-02/A/summary_report.json"}}},      # wrong suffix
        {"s3": {"object": {"key": "embeddings/2026-03-02/A/vectors.json"}}},          # wrong prefix
    ]}

    result = ler.lambda_handler(event, None)

    assert calls == []
    assert result == {"results": []}


# ---------------------------------------------------------------------------
# CRITICAL cross-check: embed-report's hash key MUST match what
# lambda_ingest.embed_from_sidecar actually looks up.
# ---------------------------------------------------------------------------

def test_hash_matches_ingest_sidecar():
    text = "hello world, this is a chunk of report text under 8000 chars"
    embed_side_key = ler._chunk_hash(text)

    # If embed-report writes {embed_side_key: vec}, lambda_ingest's REAL
    # embed_from_sidecar (not a reimplementation) must find it.
    result = ing.embed_from_sidecar(text, {embed_side_key: [7.0, 8.0]})

    assert result == "[7.0,8.0]"
    assert embed_side_key == hashlib.sha256(text[:8000].encode("utf-8")).hexdigest()


def test_hash_matches_ingest_sidecar_long_text_truncation():
    # Load-bearing edge case: text > 8000 chars must truncate IDENTICALLY on
    # both sides before hashing, or every long-chunk lookup misses silently.
    long_text = "y" * 9000
    embed_side_key = ler._chunk_hash(long_text)

    result = ing.embed_from_sidecar(long_text, {embed_side_key: [3.14]})

    assert result == "[3.14]"
    assert embed_side_key == hashlib.sha256(long_text[:8000].encode("utf-8")).hexdigest()


def test_hash_matches_ingest_sidecar_end_to_end(wired):
    ler.embed_report("2026-03-02", "Jarley_Trainor", REPORT_KEY)

    sidecar = json.loads(wired.objects["embeddings/2026-03-02/Jarley_Trainor/vectors.json"])
    report = make_report()
    expected_chunks = ing.chunk_report(report)
    assert expected_chunks  # sanity: this report does produce chunks

    for c in expected_chunks:
        # Real ingest-side lookup against the REAL written sidecar must hit.
        looked_up = ing.embed_from_sidecar(c["chunk_text"], sidecar)
        assert looked_up is not None


# ---------------------------------------------------------------------------
# Sidecar write contract
# ---------------------------------------------------------------------------

def test_writes_sidecar_contract(wired):
    result = ler.embed_report("2026-03-02", "Jarley_Trainor", REPORT_KEY)

    sidecar_key = "embeddings/2026-03-02/Jarley_Trainor/vectors.json"
    assert wired.put_calls
    put = wired.put_calls[-1]
    assert put["Key"] == sidecar_key
    assert put["Bucket"] == BUCKET

    vectors = json.loads(put["Body"])
    assert isinstance(vectors, dict) and vectors
    for h, vec in vectors.items():
        assert len(h) == 64  # sha256 hex digest length
        assert isinstance(vec, list) and vec

    assert result["report"] == REPORT_KEY
    assert result["vectors"] == len(vectors)
    assert result["chunks"] >= result["vectors"]  # dedup can only shrink, never grow


# ---------------------------------------------------------------------------
# Empty report -> zero chunks -> no sidecar write, no embed call
# ---------------------------------------------------------------------------

def test_empty_report_no_write(monkeypatch):
    key = "reports/2026-03-05/Empty_User/daily_report.json"
    fake_s3 = FakeS3({key: json.dumps(make_report(n_topics=0))})
    _wire_s3(monkeypatch, fake_s3)

    def fail_if_called(*a, **k):
        raise AssertionError("must not call dashscope_utils.embed with zero chunks")

    monkeypatch.setattr(ler.dashscope_utils, "embed", fail_if_called)

    result = ler.embed_report("2026-03-05", "Empty_User", key)

    assert result == {"report": key, "chunks": 0, "vectors": 0}
    assert fake_s3.put_calls == []


# ---------------------------------------------------------------------------
# Dedup identical chunk texts before calling dashscope_utils.embed
# ---------------------------------------------------------------------------

def test_dedupes_identical_texts(monkeypatch):
    report = make_report(n_topics=2)
    # Force topic 1's rendered chunk_text to be byte-identical to topic 0's
    # (same title/time_range/summary) -- only topic_id differs, which never
    # enters chunk_text.
    report["topics"][1] = dict(report["topics"][0])
    report["topics"][1]["topic_id"] = 1

    key = "reports/2026-03-06/Dup_User/daily_report.json"
    fake_s3 = FakeS3({key: json.dumps(report)})
    _wire_s3(monkeypatch, fake_s3)

    embed_calls = []

    def fake_embed(texts, dim=None):
        embed_calls.append(list(texts))
        return [[i / 1000.0] * 1024 for i in range(len(texts))]

    monkeypatch.setattr(ler.dashscope_utils, "embed", fake_embed)

    result = ler.embed_report("2026-03-06", "Dup_User", key)

    assert result["chunks"] == 2       # both topic chunks still counted
    assert result["vectors"] == 1      # but only 1 unique text
    assert len(embed_calls) == 1
    assert len(embed_calls[0]) == 1


# ---------------------------------------------------------------------------
# Batch >10 unique texts -> DashScope HTTP layer never sees more than 10 per
# request (exercises the REAL dashscope_utils.embed batching end-to-end, not
# a stand-in mock, since batching happens inside dashscope_utils not here).
# ---------------------------------------------------------------------------

def test_batch_over_10(monkeypatch):
    report = make_report(n_topics=14)  # 14 topics -> 14 unique topic chunks
    key = "reports/2026-03-07/Many_User/daily_report.json"
    fake_s3 = FakeS3({key: json.dumps(report)})
    _wire_s3(monkeypatch, fake_s3)
    monkeypatch.setattr(du, "DASHSCOPE_API_KEY", "test-key")

    request_batches = []

    def fake_request(self, method, url, body=None, headers=None, timeout=None):
        payload = json.loads(body)
        request_batches.append(payload["input"])
        data = [{"index": i, "embedding": [0.0] * 1024} for i in range(len(payload["input"]))]
        return _FakeHTTPResponse(200, {"data": data})

    monkeypatch.setattr(du.urllib3.PoolManager, "request", fake_request)

    result = ler.embed_report("2026-03-07", "Many_User", key)

    assert result["chunks"] == 14
    assert result["vectors"] == 14
    assert len(request_batches) == 2  # 10 + 4
    assert all(len(b) <= 10 for b in request_batches)


# ---------------------------------------------------------------------------
# Idempotent overwrite -- rerunning writes to the same key, never a new one
# ---------------------------------------------------------------------------

def test_idempotent_overwrite(wired):
    ler.embed_report("2026-03-02", "Jarley_Trainor", REPORT_KEY)
    ler.embed_report("2026-03-02", "Jarley_Trainor", REPORT_KEY)

    sidecar_keys = [k for k in wired.objects if k.startswith("embeddings/")]
    assert sidecar_keys == ["embeddings/2026-03-02/Jarley_Trainor/vectors.json"]
    assert len(wired.put_calls) == 2
    assert wired.put_calls[0]["Key"] == wired.put_calls[1]["Key"]
