"""The client the corroboration steps use.

Spec: docs/superpowers/specs/2026-08-31-ask-external-corroboration-design.md §5.4

The four reasons this module exists instead of `llm_utils` are each asserted
here, because "we wrote a second client" is only worth the duplication if the
second one actually behaves differently. Three of the four are silent failures in
the shared client -- it would return a plausible answer with the search results
missing -- so a test that only checks the happy path would pass against the code
this module was written to avoid.
"""
import json
import sys
import types

import pytest

client = pytest.importorskip("corroboration_client")


class FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self.data = (payload if isinstance(payload, bytes)
                     else json.dumps(payload).encode("utf-8"))


class FakePool:
    """Records every request and returns queued responses.

    Nothing here reaches a network; a unit test that could would be a unit test
    that fails on a plane and passes in CI for reasons unrelated to the code.
    """

    def __init__(self, *responses):
        self.queue = list(responses)
        self.calls = []

    def request(self, method, url, body=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url, "timeout": timeout,
                           "headers": headers, "body": json.loads(body)})
        nxt = self.queue.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")


def _install(monkeypatch, pool):
    monkeypatch.setattr(client.urllib3, "PoolManager", lambda *a, **k: pool)
    return pool


def _body(text="", blocks=None, stop_reason="end_turn"):
    return {"content": blocks if blocks is not None else [{"type": "text", "text": text}],
            "stop_reason": stop_reason}


SEARCH_BLOCKS = [
    {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search",
     "input": {"query": "Naylor Love Construction"}},
    {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_1", "content": [
        {"type": "web_search_result", "url": "https://naylorlove.co.nz/about",
         "title": "About - Naylor Love", "page_age": "2026-01-04"},
        {"type": "web_search_result", "url": "https://example.com/nz-builders",
         "title": "NZ builders", "page_age": None},
    ]},
    {"type": "text", "text": "Naylor Love is a New Zealand construction company.",
     "citations": [{"type": "web_search_result_location",
                    "url": "https://naylorlove.co.nz/about",
                    "cited_text": "founded in 1910"}]},
]


# ------------------------------------------------ reason 4: results are not dropped

def test_search_results_survive_the_parse():
    """`llm_utils._call_anthropic` keeps only `type == "text"` blocks, so every
    result and citation would vanish with the answer still looking fine. That is
    the failure this whole module exists to prevent, so it is asserted first."""
    reply = client._parse(_body(blocks=SEARCH_BLOCKS))
    assert reply.ok
    assert reply.searched is True
    assert [r.url for r in reply.search_results] == [
        "https://naylorlove.co.nz/about", "https://example.com/nz-builders"]
    assert reply.search_results[0].title == "About - Naylor Love"
    assert reply.text.startswith("Naylor Love is a New Zealand")


def test_citations_survive_the_parse():
    """A card without its source is an unsourced claim wearing a card's clothes."""
    reply = client._parse(_body(blocks=SEARCH_BLOCKS))
    assert len(reply.citations) == 1
    assert reply.citations[0]["url"] == "https://naylorlove.co.nz/about"


def test_a_search_error_is_not_read_as_an_empty_web():
    """`web_search_tool_result.content` is a LIST on success and a DICT on error,
    and the HTTP status is 200 either way. Iterating the dict yields its keys and
    quietly produces zero results -- which the caller would report as
    `not_found`, i.e. a finding about the world rather than a fault in us."""
    blocks = [
        {"type": "server_tool_use", "id": "s1", "name": "web_search", "input": {}},
        {"type": "web_search_tool_result", "tool_use_id": "s1",
         "content": {"type": "web_search_tool_result_error",
                     "error_code": "max_uses_exceeded"}},
        {"type": "text", "text": "I could not complete the search."},
    ]
    reply = client._parse(_body(blocks=blocks))
    assert reply.search_results == []
    assert reply.searched is True, "the search ran and failed; that is not 'no search'"
    # The load-bearing half. Without this the caller cannot tell a failed search
    # from an empty one: iterating the error dict yields its keys, the item
    # filter drops them, and zero results come back with nothing to say why.
    # A first version of this test asserted only the empty list and stayed green
    # with the guard deleted.
    assert reply.search_error == "max_uses_exceeded"


def test_a_successful_search_carries_no_search_error():
    assert client._parse(_body(blocks=SEARCH_BLOCKS)).search_error is None


def test_a_malformed_body_does_not_raise():
    """The step must degrade to no cards, never to a 500 on the answer."""
    for body in [{}, {"content": None}, {"content": ["not a dict", 7]},
                 {"content": [{"type": "web_search_tool_result", "content": None}]}]:
        assert client._parse(body).search_results == []


# --------------------------------------------------- reason 1: the timeout is ours

def test_the_callers_timeout_reaches_the_request(monkeypatch):
    pool = _install(monkeypatch, FakePool(FakeResponse(200, _body("hi"))))
    client.call("q", timeout=11.5)
    assert pool.calls[0]["timeout"] == 11.5, "the module constant won again"


def test_a_timeout_too_small_to_use_spends_nothing(monkeypatch):
    """Below the floor there is no time for anything but a timeout, and burning
    the remaining budget on a doomed attempt is worse than saying so."""
    pool = _install(monkeypatch, FakePool(FakeResponse(200, _body("hi"))))
    reply = client.call("q", timeout=0.5)
    assert not reply.ok
    assert pool.calls == [], "it made the request anyway"


# ------------------------------------------------ reason 2: at most one retry

def test_no_retry_unless_the_budget_was_stated(monkeypatch):
    """`llm_utils` retries four times. A caller that says nothing about its
    budget gets exactly one attempt here -- silence must not authorise spending
    the deadline twice."""
    pool = _install(monkeypatch, FakePool(FakeResponse(503, {}), FakeResponse(200, _body("hi"))))
    reply = client.call("q", timeout=8)
    assert len(pool.calls) == 1
    assert not reply.ok


def test_one_retry_when_the_budget_covers_a_whole_second_attempt(monkeypatch):
    pool = _install(monkeypatch, FakePool(FakeResponse(503, {}), FakeResponse(200, _body("hi"))))
    reply = client.call("q", timeout=5, retry_budget=20)
    assert len(pool.calls) == 2
    assert reply.ok and reply.text == "hi"


def test_the_retry_is_never_a_third_attempt(monkeypatch):
    pool = _install(monkeypatch, FakePool(FakeResponse(503, {}), FakeResponse(503, {}),
                                          FakeResponse(200, _body("hi"))))
    reply = client.call("q", timeout=5, retry_budget=999)
    assert len(pool.calls) == 2
    assert not reply.ok


def test_a_budget_that_does_not_cover_a_second_attempt_buys_no_retry(monkeypatch):
    """`retry_budget` is what remains AFTER this attempt. 6 seconds does not fit
    another 5-second attempt plus the floor, so the retry must not be taken."""
    pool = _install(monkeypatch, FakePool(FakeResponse(503, {}), FakeResponse(200, _body("x"))))
    client.call("q", timeout=5, retry_budget=6)
    assert len(pool.calls) == 1


def test_a_client_error_is_not_retried(monkeypatch):
    pool = _install(monkeypatch, FakePool(FakeResponse(400, {"error": {"message": "bad tool"}}),
                                          FakeResponse(200, _body("hi"))))
    reply = client.call("q", timeout=5, retry_budget=99)
    assert len(pool.calls) == 1
    assert reply.error == "bad tool"


def test_a_connection_failure_is_reported_not_raised(monkeypatch):
    _install(monkeypatch, FakePool(RuntimeError("connection reset")))
    reply = client.call("q", timeout=5)
    assert not reply.ok and "connection reset" in reply.error


# --------------------------------------------------- reason 3: tools can be sent

def test_the_web_search_tool_is_sent_when_asked(monkeypatch):
    pool = _install(monkeypatch, FakePool(FakeResponse(200, _body(blocks=SEARCH_BLOCKS))))
    client.call("q", timeout=12, tools=[client.WEB_SEARCH_TOOL])
    assert pool.calls[0]["body"]["tools"] == [client.WEB_SEARCH_TOOL]


def test_the_search_tool_is_capped(monkeypatch):
    """Twelve seconds does not hold six searches. The cap is on the tool, where
    the model sees it, not on a loop we do not run."""
    assert client.WEB_SEARCH_TOOL["max_uses"] <= 3


# ---------------------------------------------- the model-behaviour choices, pinned

def test_thinking_is_never_disabled(monkeypatch):
    """With thinking disabled, Claude Opus 5 sometimes writes a tool call into
    its visible text instead of emitting `server_tool_use`. The turn succeeds,
    the search never runs, and the caller reports `not_found`. Latency is bought
    with effort instead."""
    pool = _install(monkeypatch, FakePool(FakeResponse(200, _body("hi"))))
    client.call("q", timeout=12, tools=[client.WEB_SEARCH_TOOL])
    assert "thinking" not in pool.calls[0]["body"]


def test_effort_is_low_by_default_and_lives_in_output_config(monkeypatch):
    pool = _install(monkeypatch, FakePool(FakeResponse(200, _body("hi"))))
    client.call("q", timeout=12)
    assert pool.calls[0]["body"]["output_config"] == {"effort": "low"}


def test_the_model_is_pinned_to_one_that_supports_the_search_tool():
    """The tool version and the model default are one decision: the dynamic-
    filtering search tool needs Opus 4.6+/Sonnet 4.6+, so changing either alone
    yields a 400 at runtime and nowhere else."""
    assert client.WEB_SEARCH_TOOL["type"] == "web_search_20260209"
    assert client.DEFAULT_MODEL.startswith("claude-")


# ------------------------------------------------------ the property, not a case

def test_the_client_is_anthropic_on_every_stack():
    """TEST runs `LLM_PROVIDER=qwen`. If this module could route to DashScope,
    the environment where the feature gets tested would exercise a different
    model from prod, and the tests would mean nothing about what ships."""
    import inspect
    source = inspect.getsource(client)
    # The module docstring names every provider it refuses to route to, and that
    # explanation is the reason the module exists -- scanning it would forbid the
    # documentation rather than the behaviour. The code below it is what matters.
    code = source.split('"""', 2)[-1]
    for forbidden in ("llm_utils", "dashscope", "qwen", "LLM_PROVIDER",
                      "QWEN_API_KEY", "elevenlabs"):
        assert forbidden not in code, f"the client can reach {forbidden}"
    assert "api.anthropic.com" in code


def test_a_missing_key_costs_the_cards_and_not_the_answer(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    pool = _install(monkeypatch, FakePool(FakeResponse(200, _body("hi"))))
    reply = client.call("q", timeout=12)
    assert not reply.ok and pool.calls == []


def test_a_refusal_is_an_error_and_not_an_empty_finding(monkeypatch):
    """HTTP 200, empty content. Read as "the web said nothing" it becomes a
    claim about the world; it is a claim about our request."""
    _install(monkeypatch, FakePool(FakeResponse(200, {"content": [], "stop_reason": "refusal"})))
    reply = client.call("q", timeout=12)
    assert not reply.ok and reply.error == "refused"


def test_the_api_key_is_never_returned_in_the_reply(monkeypatch):
    """`Reply` ends up in logs. `__repr__` is where a secret leaves a process."""
    _install(monkeypatch, FakePool(FakeResponse(401, {"error": {"message": "bad key"}})))
    reply = client.call("q", timeout=12)
    assert "test-key" not in repr(reply)
