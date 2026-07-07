"""
Tests for src/dashscope_utils.py — Phase 4d, Task 2 (TDD).

Mirrors claude_utils.py's urllib3 HTTP pattern (see src/claude_utils.py and
lambda_report_generator.py :410-441) and the monkeypatch style of
tests/unit/test_lambda_extract_session.py: module-level env-derived
constants are monkeypatched directly on the module object, and
urllib3.PoolManager.request is monkeypatched at the class level (embed()
constructs a fresh PoolManager() internally, so patching the instance
wouldn't reach it) so no test ever makes a real network call.
"""
import json

import pytest

du = pytest.importorskip("dashscope_utils", reason="requires urllib3 (installed in CI)")


class _FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self.data = json.dumps(payload).encode("utf-8")


def _embedding_payload(batch, offset=0):
    return {"data": [
        {"index": i, "embedding": [float(offset + i)] * 4}
        for i in range(len(batch))
    ]}


@pytest.fixture(autouse=True)
def dashscope_key(monkeypatch):
    monkeypatch.setattr(du, "DASHSCOPE_API_KEY", "test-key")


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    # Backoff sleeps would otherwise slow the suite down for no reason --
    # every retry test cares about attempt COUNT, not real wall-clock delay.
    monkeypatch.setattr(du.time, "sleep", lambda seconds: None)


def test_missing_key_raises(monkeypatch):
    monkeypatch.setattr(du, "DASHSCOPE_API_KEY", "")
    with pytest.raises(RuntimeError, match="DASHSCOPE_API_KEY not set"):
        du.embed(["hello"])


def test_batches_of_10(monkeypatch):
    calls = []

    def fake_request(self, method, url, body=None, headers=None, timeout=None):
        payload = json.loads(body)
        calls.append(payload["input"])
        assert method == "POST"
        assert url.endswith("/embeddings")
        assert headers["Authorization"] == "Bearer test-key"
        assert payload["encoding_format"] == "float"
        return _FakeResponse(200, _embedding_payload(payload["input"]))

    monkeypatch.setattr(du.urllib3.PoolManager, "request", fake_request)

    texts = [f"text-{i}" for i in range(14)]
    vectors = du.embed(texts)

    assert len(calls) == 2
    assert len(calls[0]) == 10
    assert len(calls[1]) == 4
    assert all(len(c) <= 10 for c in calls)
    assert len(vectors) == 14


def test_vectors_returned_in_input_order(monkeypatch):
    def fake_request(self, method, url, body=None, headers=None, timeout=None):
        payload = json.loads(body)
        # Server returns entries out of order -- embed() must sort by index,
        # not trust response array order.
        items = [{"index": i, "embedding": [float(i)]} for i in range(len(payload["input"]))]
        items.reverse()
        return _FakeResponse(200, {"data": items})

    monkeypatch.setattr(du.urllib3.PoolManager, "request", fake_request)

    vectors = du.embed(["a", "b", "c"])

    assert vectors == [[0.0], [1.0], [2.0]]


def test_retry_on_429(monkeypatch):
    attempts = {"n": 0}

    def fake_request(self, method, url, body=None, headers=None, timeout=None):
        attempts["n"] += 1
        payload = json.loads(body)
        if attempts["n"] == 1:
            return _FakeResponse(429, {"error": {"message": "rate limited"}})
        return _FakeResponse(200, _embedding_payload(payload["input"]))

    monkeypatch.setattr(du.urllib3.PoolManager, "request", fake_request)

    vectors = du.embed(["hello"])

    assert attempts["n"] == 2
    assert vectors == [[0.0] * 4]


def test_retry_on_503_then_500_then_success(monkeypatch):
    statuses = iter([503, 500, 200])
    attempts = {"n": 0}

    def fake_request(self, method, url, body=None, headers=None, timeout=None):
        attempts["n"] += 1
        status = next(statuses)
        payload = json.loads(body)
        if status == 200:
            return _FakeResponse(200, _embedding_payload(payload["input"]))
        return _FakeResponse(status, {"error": {"message": "transient"}})

    monkeypatch.setattr(du.urllib3.PoolManager, "request", fake_request)

    vectors = du.embed(["hello"])

    assert attempts["n"] == 3
    assert vectors == [[0.0] * 4]


def test_exhausted_retries_raises(monkeypatch):
    attempts = {"n": 0}

    def fake_request(self, method, url, body=None, headers=None, timeout=None):
        attempts["n"] += 1
        return _FakeResponse(500, {"error": {"message": "server error"}})

    monkeypatch.setattr(du.urllib3.PoolManager, "request", fake_request)

    with pytest.raises(RuntimeError):
        du.embed(["hello"])

    assert attempts["n"] == 4  # up to 4 attempts, then raise


def test_non_retryable_error_raises_immediately(monkeypatch):
    attempts = {"n": 0}

    def fake_request(self, method, url, body=None, headers=None, timeout=None):
        attempts["n"] += 1
        return _FakeResponse(400, {"error": {"message": "bad request"}})

    monkeypatch.setattr(du.urllib3.PoolManager, "request", fake_request)

    with pytest.raises(RuntimeError):
        du.embed(["hello"])

    assert attempts["n"] == 1  # HTTP 400 is not in the retry set -- no retry


def test_empty_texts_returns_empty_without_request(monkeypatch):
    def fail_if_called(self, *a, **k):
        raise AssertionError("must not call DashScope with an empty text list")

    monkeypatch.setattr(du.urllib3.PoolManager, "request", fail_if_called)

    assert du.embed([]) == []
