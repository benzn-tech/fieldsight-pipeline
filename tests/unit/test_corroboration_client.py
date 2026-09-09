"""The client the corroboration steps use.

Spec: docs/superpowers/specs/2026-09-08-corroboration-off-anthropic-design.md
(supersedes the vendor half of the 2026-08-31 design)

The four reasons this module exists instead of `llm_utils` are each asserted
here, because "we wrote a second client" is only worth the duplication if the
second one actually behaves differently. Three of the four are silent failures
in the shared client -- it would return a plausible answer with the search
results missing -- so a test that only checks the happy path would pass against
the code this module was written to avoid.

This file was Anthropic-shaped until 2026-09-09. What changed is the vendor and
the wire format; what did NOT change is every rule about honesty, and those
tests are carried over deliberately rather than rewritten from scratch, so a
reader can see the guarantees survived the port.
"""
import json

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
    monkeypatch.setenv("CORROBORATION_API_KEY", "test-key")


def _install(monkeypatch, pool):
    monkeypatch.setattr(client.urllib3, "PoolManager", lambda *a, **k: pool)
    return pool


def _body(text="hi", annotations=None, finish_reason="stop"):
    message = {"content": text}
    if annotations is not None:
        message["annotations"] = annotations
    return {"choices": [{"message": message, "finish_reason": finish_reason}]}


def _cite(url, title):
    return {"type": "url_citation",
            "url_citation": {"url": url, "title": title,
                             "start_index": 0, "end_index": 10}}


SEARCH_ANNOTATIONS = [
    _cite("https://naylorlove.co.nz/about", "naylorlove.co.nz"),
    _cite("https://example.com/nz-builders", "example.com"),
]


# ------------------------------------------------ reason 4: results are not dropped

def test_search_results_survive_the_parse():
    """`llm_utils` keeps only the text. Every source would vanish silently and
    a plausible answer would still come back -- indistinguishable from the web
    having nothing to say."""
    reply = client._parse(_body("Naylor Love is a NZ construction company.",
                                annotations=SEARCH_ANNOTATIONS))
    assert reply.search_results == [
        client.SearchResult("https://naylorlove.co.nz/about", "naylorlove.co.nz"),
        client.SearchResult("https://example.com/nz-builders", "example.com"),
    ]
    assert reply.text == "Naylor Love is a NZ construction company."


def test_citations_survive_the_parse():
    reply = client._parse(_body(annotations=SEARCH_ANNOTATIONS))
    assert len(reply.citations) == 2


def test_a_search_that_produced_nothing_is_not_a_search(monkeypatch):
    """The muse-spark shape, and the reason `searched` is derived from results
    rather than from prose. Measured 2026-09-08: HTTP 200, 1200 tokens, zero
    annotations, and text reading "I'll search the web to verify... Initial
    results support the claim." A model's account of its own tool use is not
    evidence."""
    reply = client._parse(_body("I'll search the web to verify this. "
                                "Initial results support the claim."))
    assert reply.searched is False
    assert reply.search_results == []


def test_an_annotation_without_a_url_is_not_counted_as_a_source():
    """`searched` is what the caller trusts. Padding it with entries that cite
    nothing would defeat the guard it exists for."""
    reply = client._parse(_body(annotations=[{"type": "url_citation",
                                              "url_citation": {"title": "x"}}]))
    assert reply.searched is False
    assert reply.search_results == []


def test_a_successful_search_says_it_searched():
    reply = client._parse(_body(annotations=SEARCH_ANNOTATIONS))
    assert reply.searched is True
    assert reply.search_error is None


def test_a_malformed_body_does_not_raise():
    """A shape nobody expected must cost the cards, never the answer."""
    for junk in ({}, {"choices": []}, {"choices": [None]},
                 {"choices": [{"message": None}]},
                 {"choices": [{"message": {"annotations": "not a list"}}]}):
        reply = client._parse(junk)
        assert reply.search_results == []


# --------------------------------------------- reason 1: the caller owns the clock

def test_the_callers_timeout_reaches_the_request(monkeypatch):
    pool = _install(monkeypatch, FakePool(FakeResponse(200, _body())))
    client.call("q", timeout=11.5)
    assert pool.calls[0]["timeout"] == 11.5, "the module constant won again"


def test_a_timeout_too_small_to_use_spends_nothing(monkeypatch):
    """Below the floor there is no time for anything but a timeout, and burning
    the remaining budget on a doomed attempt is worse than saying so."""
    pool = _install(monkeypatch, FakePool(FakeResponse(200, _body())))
    reply = client.call("q", timeout=0.5)
    assert not reply.ok
    assert pool.calls == [], "it made the request anyway"


# ------------------------------------------------ reason 2: at most one retry

def test_no_retry_unless_the_budget_was_stated(monkeypatch):
    """`llm_utils` retries four times. A caller that says nothing about its
    budget gets exactly one attempt here -- silence must not authorise spending
    the deadline twice."""
    pool = _install(monkeypatch, FakePool(FakeResponse(503, {}), FakeResponse(200, _body())))
    reply = client.call("q", timeout=8)
    assert len(pool.calls) == 1
    assert not reply.ok


def test_one_retry_when_the_budget_covers_a_whole_second_attempt(monkeypatch):
    pool = _install(monkeypatch, FakePool(FakeResponse(503, {}), FakeResponse(200, _body())))
    reply = client.call("q", timeout=5, retry_budget=20)
    assert len(pool.calls) == 2
    assert reply.ok and reply.text == "hi"


def test_the_retry_is_never_a_third_attempt(monkeypatch):
    pool = _install(monkeypatch, FakePool(FakeResponse(503, {}), FakeResponse(503, {}),
                                          FakeResponse(200, _body())))
    reply = client.call("q", timeout=5, retry_budget=999)
    assert len(pool.calls) == 2
    assert not reply.ok


def test_a_budget_that_does_not_cover_a_second_attempt_buys_no_retry(monkeypatch):
    """`retry_budget` is what remains AFTER this attempt. 6 seconds does not fit
    another 5-second attempt plus the floor, so the retry must not be taken."""
    pool = _install(monkeypatch, FakePool(FakeResponse(503, {}), FakeResponse(200, _body())))
    client.call("q", timeout=5, retry_budget=6)
    assert len(pool.calls) == 1


def test_a_client_error_is_not_retried(monkeypatch):
    pool = _install(monkeypatch, FakePool(FakeResponse(400, {"error": {"message": "bad plugin"}}),
                                          FakeResponse(200, _body())))
    reply = client.call("q", timeout=5, retry_budget=99)
    assert len(pool.calls) == 1
    assert reply.error == "bad plugin"


def test_a_connection_failure_is_reported_not_raised(monkeypatch):
    _install(monkeypatch, FakePool(RuntimeError("connection reset")))
    reply = client.call("q", timeout=5)
    assert not reply.ok and "connection reset" in reply.error


# ------------------------------------------ reason 3: it can actually search

def test_the_web_plugin_is_sent_when_asked(monkeypatch):
    pool = _install(monkeypatch, FakePool(FakeResponse(200, _body(annotations=SEARCH_ANNOTATIONS))))
    client.call("q", timeout=13, web=True)
    assert pool.calls[0]["body"]["plugins"] == client.WEB_PLUGIN


def test_nothing_searches_unless_it_asked_to(monkeypatch):
    """A classification step that quietly searched would spend the search step's
    budget a second time, inside a hard stop that has no room for it."""
    pool = _install(monkeypatch, FakePool(FakeResponse(200, _body())))
    client.call("q", timeout=8)
    assert "plugins" not in pool.calls[0]["body"]


def test_the_measured_pairing_is_the_one_that_ships():
    """Vendor and request form are one decision, and measurement settled both.

    n=3 per configuration, 2026-09-08: the plugin form answered in 10.3-11.4 s
    with 4-7 sources; the `:online` suffix -- same model, same question -- was
    over budget on all three runs; muse-spark returned zero sources while
    asserting it had searched. Changing either half without re-measuring puts
    the feature back somewhere it was already shown not to work.
    """
    assert client.WEB_PLUGIN == [{"id": "web"}]
    assert client.DEFAULT_MODEL == "google/gemini-3.8-flash"
    assert "openrouter.ai" in client.API_URL


# ---------------------------------------------- the model-behaviour choices, pinned

def test_effort_is_low_by_default_and_lives_under_reasoning(monkeypatch):
    """Measured on the search prompt: effort low spent 0 reasoning tokens in
    9.4 s, effort high spent 711 in 13.6 s -- past the search budget. The
    default is latency, not cost."""
    pool = _install(monkeypatch, FakePool(FakeResponse(200, _body())))
    client.call("q", timeout=13, web=True)
    assert pool.calls[0]["body"]["reasoning"] == {"effort": "low"}


def test_effort_can_be_raised_by_a_caller_that_means_to(monkeypatch):
    pool = _install(monkeypatch, FakePool(FakeResponse(200, _body())))
    client.call("q", timeout=13, effort="high")
    assert pool.calls[0]["body"]["reasoning"] == {"effort": "high"}


# ------------------------------------------------------ the property, not a case

def test_the_client_is_one_vendor_on_every_stack():
    """TEST and prod point their shared chat client at different models. If this
    module could route through that machinery, the environment where the feature
    gets tested would exercise a different model from prod and the tests would
    mean nothing about what ships."""
    import inspect
    source = inspect.getsource(client)
    # The module docstring names the models it compared, and that comparison is
    # the reason the module reads as it does -- scanning it would forbid the
    # documentation rather than the behaviour. The code below it is what matters.
    code = source.split('"""', 2)[-1]
    for forbidden in ("llm_utils", "dashscope", "DASHSCOPE", "LLM_PROVIDER",
                      "QWEN_API_KEY", "anthropic", "elevenlabs"):
        assert forbidden not in code, f"the client can reach {forbidden}"
    assert "openrouter.ai" in code


def test_the_key_is_its_own_and_not_the_chat_one(monkeypatch):
    """`QWEN_API_KEY` on this function resolves to the DashScope key on any
    stack still pointed there, and one vendor's credential 401s at another."""
    import inspect
    code = inspect.getsource(client).split('"""', 2)[-1]
    assert "CORROBORATION_API_KEY" in code


def test_a_missing_key_costs_the_cards_and_not_the_answer(monkeypatch):
    monkeypatch.delenv("CORROBORATION_API_KEY", raising=False)
    pool = _install(monkeypatch, FakePool(FakeResponse(200, _body())))
    reply = client.call("q", timeout=13)
    assert not reply.ok and pool.calls == []


def test_the_key_is_sent_as_a_bearer_token(monkeypatch):
    pool = _install(monkeypatch, FakePool(FakeResponse(200, _body())))
    client.call("q", timeout=13)
    assert pool.calls[0]["headers"]["Authorization"] == "Bearer test-key"


def test_an_empty_completion_is_an_error_and_not_an_empty_finding(monkeypatch):
    """HTTP 200 with nothing in it. Measured 2026-09-08: this model with no
    plugin configured returns `completion_tokens: 0` and an empty string. Read
    as findings that becomes "the web said nothing", which is a claim about the
    world where the truth is a claim about our configuration."""
    _install(monkeypatch, FakePool(FakeResponse(200, _body(""))))
    reply = client.call("q", timeout=13)
    assert not reply.ok and reply.error == "empty completion"


def test_whitespace_is_not_content(monkeypatch):
    _install(monkeypatch, FakePool(FakeResponse(200, _body("   \n "))))
    reply = client.call("q", timeout=13)
    assert not reply.ok


def test_the_api_key_is_never_returned_in_the_reply(monkeypatch):
    """`Reply` ends up in logs. `__repr__` is where a secret leaves a process."""
    _install(monkeypatch, FakePool(FakeResponse(401, {"error": {"message": "bad key"}})))
    reply = client.call("q", timeout=13)
    assert "test-key" not in repr(reply)
