"""A caller with a deadline had no way to say so.

`/ask` is served through API Gateway REST, which terminates the integration at
29 seconds. Underneath it, `call_llm` accepts no timeout and `_post_with_retry`
makes up to `MAX_ATTEMPTS` attempts at `HTTP_TIMEOUT` each with 1+2+4s of
backoff. On AskAgentFunction that is 45s per attempt, so the ladder's ceiling is
about 187 seconds, cut only by the Lambda's own 60s timeout -- itself already
past the gateway.

What a reader sees today when one vendor attempt is slow: the browser waits out
its 35s budget, the gateway 504s at 29s, the Lambda keeps burning to 60s, and
the answer that eventually exists is thrown away. Nothing logs a deadline
because there is no deadline.

This is the shape `corroboration_client` was written to avoid, and its docstring
lists these exact two reasons for existing. The difference is that this module
is shared by nine callers, so the fix has to be inert for every one of them that
does not ask for it.

The rule these pin:

  * a caller that says nothing gets exactly today's behaviour, byte for byte
  * a caller with a deadline never has an attempt STARTED that cannot finish
    inside it -- checked before the attempt, because a 45s attempt begun at t=20
    cannot be recalled
  * the deadline bounds the backoff too, since sleeping past it is the same
    failure wearing a different hat
  * running out of time is reported as running out of time, not as a network
    error, because the two need different fixes
"""
import time

import pytest

llm_utils = pytest.importorskip("llm_utils")


class FakeResponse:
    def __init__(self, status=200, data=b'{"choices":[{"message":{"content":"hi"}}]}'):
        self.status = status
        self.data = data


class FakePool:
    """Records each attempt with the timeout it was given."""

    def __init__(self, *responses, elapsed=0.0):
        self.queue = list(responses)
        self.calls = []
        self.elapsed = elapsed
        self.clock = 0.0

    def request(self, method, url, body=None, headers=None, timeout=None):
        self.calls.append({"timeout": timeout})
        self.clock += self.elapsed
        nxt = self.queue.pop(0) if self.queue else FakeResponse()
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Backoff is real seconds; the tests are about the decisions around it."""
    monkeypatch.setattr(llm_utils.time, "sleep", lambda s: None)


def _install(monkeypatch, pool):
    monkeypatch.setattr(llm_utils.urllib3, "PoolManager", lambda *a, **k: pool)
    return pool


# --------------------------------------------- the nine callers that say nothing

def test_a_caller_without_a_deadline_gets_the_ladder_it_has_today(monkeypatch):
    """Eight of the nine callers are background pipelines where a slow answer is
    fine. Bounding them by default would be a behaviour change nobody asked for,
    delivered to every one of them at once."""
    pool = _install(monkeypatch, FakePool(FakeResponse(503), FakeResponse(503),
                                          FakeResponse(503), FakeResponse(200)))
    resp, err = llm_utils._post_with_retry("u", "{}", {})
    assert len(pool.calls) == llm_utils.MAX_ATTEMPTS
    assert err is None and resp.status == 200


def test_without_a_deadline_the_request_timeout_is_the_module_default(monkeypatch):
    pool = _install(monkeypatch, FakePool(FakeResponse(200)))
    llm_utils._post_with_retry("u", "{}", {})
    assert pool.calls[0]["timeout"] == llm_utils.HTTP_TIMEOUT


# ------------------------------------------------- the caller that has a deadline

def test_an_attempt_is_never_started_that_cannot_finish_in_time(monkeypatch):
    """Checked BEFORE the attempt. A 45-second request begun with 5 seconds left
    cannot be recalled, and the caller it belongs to is already gone."""
    pool = _install(monkeypatch, FakePool(FakeResponse(503), FakeResponse(200)))
    ticks = iter([0.0, 0.0, 100.0, 100.0, 100.0, 100.0])
    resp, err = llm_utils._post_with_retry("u", "{}", {}, deadline=10.0,
                                           clock=lambda: next(ticks))
    assert len(pool.calls) == 1, "it started an attempt past the deadline"
    assert resp is None and err is not None


def test_the_request_timeout_shrinks_to_what_is_actually_left(monkeypatch):
    """45 seconds of patience inside a 12-second budget is not patience, it is a
    guarantee that the caller is gone before the answer arrives."""
    pool = _install(monkeypatch, FakePool(FakeResponse(200)))
    ticks = iter([0.0, 0.0, 0.0, 0.0])
    llm_utils._post_with_retry("u", "{}", {}, deadline=12.0,
                               clock=lambda: next(ticks))
    assert pool.calls[0]["timeout"] <= 12.0


def test_a_deadline_longer_than_the_default_does_not_extend_it(monkeypatch):
    """The module default exists to lose the race against the Lambda timeout.
    A deadline may only ever tighten."""
    pool = _install(monkeypatch, FakePool(FakeResponse(200)))
    ticks = iter([0.0, 0.0, 0.0, 0.0])
    llm_utils._post_with_retry("u", "{}", {}, deadline=999.0,
                               clock=lambda: next(ticks))
    assert pool.calls[0]["timeout"] == llm_utils.HTTP_TIMEOUT


def test_the_backoff_may_not_sleep_past_the_deadline(monkeypatch):
    """Sleeping through the budget is the same failure as a slow attempt, and it
    is the easier one to leave in by accident because nothing is on the wire."""
    slept = []
    monkeypatch.setattr(llm_utils.time, "sleep", lambda s: slept.append(s))
    _install(monkeypatch, FakePool(FakeResponse(503), FakeResponse(200)))
    ticks = iter([0.0, 0.0, 3.0, 3.0, 3.0, 3.0])
    llm_utils._post_with_retry("u", "{}", {}, deadline=3.5,
                               clock=lambda: next(ticks))
    assert sum(slept) <= 3.5


def test_running_out_of_time_says_so(monkeypatch):
    """`ConnectionError` and `we ran out of budget` need different fixes. An
    error that names the wrong one sends the reader at the vendor."""
    _install(monkeypatch, FakePool(FakeResponse(503), FakeResponse(200)))
    ticks = iter([0.0, 0.0, 100.0, 100.0, 100.0, 100.0])
    _, err = llm_utils._post_with_retry("u", "{}", {}, deadline=10.0,
                                        clock=lambda: next(ticks))
    assert "deadline" in err.lower() or "out of time" in err.lower(), err


def test_a_deadline_already_spent_makes_no_request_at_all(monkeypatch):
    """Spending the caller's remaining budget on an attempt that cannot land is
    worse than reporting that the budget ran out."""
    pool = _install(monkeypatch, FakePool(FakeResponse(200)))
    ticks = iter([0.0, 100.0, 100.0, 100.0])
    resp, err = llm_utils._post_with_retry("u", "{}", {}, deadline=1.0,
                                           clock=lambda: next(ticks))
    assert pool.calls == []
    assert resp is None and err is not None


# --------------------------------------------------------- it reaches call_llm

def test_call_llm_passes_a_deadline_down(monkeypatch):
    """The parameter is useless if only the private helper accepts it."""
    seen = {}

    def fake_post(url, body, headers, deadline=None, clock=time.monotonic):
        seen["deadline"] = deadline
        return FakeResponse(200), None

    monkeypatch.setattr(llm_utils, "_post_with_retry", fake_post)
    monkeypatch.setattr(llm_utils, "LLM_PROVIDER", "qwen")
    monkeypatch.setattr(llm_utils, "QWEN_API_KEY", "k")
    llm_utils.call_llm("p", deadline=9.0)
    assert seen["deadline"] == 9.0


def test_call_llm_without_a_deadline_passes_none(monkeypatch):
    seen = {}

    def fake_post(url, body, headers, deadline=None, clock=time.monotonic):
        seen["deadline"] = deadline
        return FakeResponse(200), None

    monkeypatch.setattr(llm_utils, "_post_with_retry", fake_post)
    monkeypatch.setattr(llm_utils, "LLM_PROVIDER", "qwen")
    monkeypatch.setattr(llm_utils, "QWEN_API_KEY", "k")
    llm_utils.call_llm("p")
    assert seen["deadline"] is None
