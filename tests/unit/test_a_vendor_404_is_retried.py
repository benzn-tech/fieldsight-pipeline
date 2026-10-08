"""OpenRouter's 404 "Provider returned error" is a vendor fault and is retried;
any other 404 (a model id that does not exist) is not.

On TEST (2026-10-06) every report call from the Lambda got that 404 for about
fifteen minutes while the same request from elsewhere went through.
"""
import os

import pytest

llm_utils = pytest.importorskip("llm_utils")

PROVIDER_404 = b'{"error":{"message":"Provider returned error","code":404}}'
NO_MODEL_404 = b'{"error":{"message":"No endpoints found for meta/nope","code":404}}'


class Resp:
    def __init__(self, status, data=b'{"choices":[{"message":{"content":"hi"}}]}'):
        self.status, self.data = status, data


class Pool:
    def __init__(self, *responses):
        self.queue, self.calls = list(responses), 0

    def request(self, method, url, body=None, headers=None, timeout=None):
        self.calls += 1
        return self.queue.pop(0) if self.queue else Resp(200)


@pytest.fixture
def slept(monkeypatch):
    waits = []
    monkeypatch.setattr(llm_utils.time, "sleep", lambda s: waits.append(s))
    return waits


def test_THE_a_provider_error_404_is_retried_with_a_longer_wait(monkeypatch, slept):
    pool = Pool(Resp(404, PROVIDER_404), Resp(404, PROVIDER_404), Resp(200))
    monkeypatch.setattr(llm_utils.urllib3, "PoolManager", lambda *a, **k: pool)
    resp, err = llm_utils._post_with_retry("u", b"{}", {})
    assert err is None and resp.status == 200 and pool.calls == 3
    assert slept == [llm_utils.PROVIDER_ERROR_WAIT_SECONDS, llm_utils.PROVIDER_ERROR_WAIT_SECONDS * 2]


def test_a_404_for_a_model_that_does_not_exist_is_final(monkeypatch, slept):
    pool = Pool(Resp(404, NO_MODEL_404))
    monkeypatch.setattr(llm_utils.urllib3, "PoolManager", lambda *a, **k: pool)
    resp, err = llm_utils._post_with_retry("u", b"{}", {})
    assert pool.calls == 1 and resp.status == 404 and slept == []


def test_the_wait_never_outlives_the_callers_deadline(monkeypatch, slept):
    pool = Pool(Resp(404, PROVIDER_404), Resp(404, PROVIDER_404))
    monkeypatch.setattr(llm_utils.urllib3, "PoolManager", lambda *a, **k: pool)
    resp, err = llm_utils._post_with_retry("u", b"{}", {}, deadline=15)
    assert resp is None and "Provider returned error" in err and slept == []


def test_the_report_worker_may_think_up_to_32000_tokens_and_wait_for_them():
    t = open(os.path.join(os.path.dirname(__file__), "..", "..", "src", "template.yaml"),
             encoding="utf-8").read()
    start = t.index("\n  SessionReportFunction:")
    block = t[start:t.index("\n  AskAgentFunction:", start)]
    assert "LLM_REASONING_HEADROOM: '24000'" in block, "8,000 answer + 24,000 thinking"
    assert "LLM_HTTP_TIMEOUT: '480'" in block, "32,000 tokens at ~76/s is ~420s"
    assert "GENERATION_BUDGET_SECONDS: '600'" in block
