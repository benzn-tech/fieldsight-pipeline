"""Unit tests for the offline shadow-eval Jev client (Track A, Task 2).

Fake transport only -- monkeypatches `urllib.request.urlopen` so nothing here
reaches a network. See `.superpowers/sdd/2026-09-24-track-a-jev-shadow-eval/`
for the task brief this file implements.
"""
import io
import json
import urllib.error

import pytest

client = pytest.importorskip("systemone_client")


class FakeHTTPResponse:
    """Minimal stand-in for what `urlopen` returns: a readable, a status, headers."""

    def __init__(self, status, payload, headers=None):
        self.status = status
        self._body = (payload if isinstance(payload, bytes)
                      else json.dumps(payload).encode("utf-8"))
        self.headers = headers or {}

    def read(self):
        return self._body

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeTransport:
    """Records every request and pops queued responses/exceptions in order."""

    def __init__(self, *responses):
        self.queue = list(responses)
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append({
            "url": req.full_url,
            "method": req.get_method(),
            "headers": dict(req.header_items()),
            "body": json.loads(req.data.decode("utf-8")),
            "timeout": timeout,
        })
        nxt = self.queue.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def _install(monkeypatch, *responses):
    transport = FakeTransport(*responses)
    monkeypatch.setattr(client.urllib.request, "urlopen", transport)
    return transport


def _http_error(status, payload, headers=None):
    fp = io.BytesIO(payload if isinstance(payload, bytes)
                     else json.dumps(payload).encode("utf-8"))
    return urllib.error.HTTPError(
        url="https://openrouter.ai/api/alpha/decisions",
        code=status, msg="error", hdrs=headers or {}, fp=fp,
    )


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("DECISIONS_API_KEY", "test-key")


def _decisions_payload(answers=None, model="~typesafe/jev-latest",
                        usage=None):
    return {
        "model": model,
        "answers": answers if answers is not None else {
            "is_urgent": {"type": "noul", "noul": 0.73},
        },
        "usage": usage if usage is not None else {"prompt_tokens": 120,
                                                   "completion_tokens": 8},
    }


def test_request_body_carries_model_state_and_questions_verbatim(monkeypatch, caplog):
    transport = _install(monkeypatch, FakeHTTPResponse(200, _decisions_payload()))
    state = {"topic": "safety walk", "notes": "loose railing on level 3"}
    questions = {"is_urgent": {"type": "noul", "prompt": "Is this urgent?"}}

    with caplog.at_level("INFO"):
        result = client.ask(state, questions, caller="unit-test")

    assert len(transport.requests) == 1
    body = transport.requests[0]["body"]
    assert body["model"] == "~typesafe/jev-latest"
    assert body["state"] == state
    assert body["questions"] == questions
    assert result["answers"]["is_urgent"] == {"noul": 0.73}
    assert isinstance(result["latency_ms"], int)


def test_noul_answer_is_a_float_in_0_1(monkeypatch):
    _install(monkeypatch, FakeHTTPResponse(200, _decisions_payload(
        answers={"q": {"type": "noul", "noul": 0.42}})))
    result = client.ask("state text", {"q": {"type": "noul"}})
    noul = result["answers"]["q"]["noul"]
    assert isinstance(noul, float)
    assert 0.0 <= noul <= 1.0


def test_choice_answer_probabilities_sum_to_one_and_choice_is_in_options(monkeypatch):
    _install(monkeypatch, FakeHTTPResponse(200, _decisions_payload(answers={
        "severity": {
            "type": "choice",
            "choice": "high",
            "probabilities": {"low": 0.1, "medium": 0.2, "high": 0.7},
            "confidence": 0.81,
        },
    })))
    questions = {"severity": {"type": "choice", "options": ["low", "medium", "high"]}}
    result = client.ask("state text", questions)
    answer = result["answers"]["severity"]
    assert answer["choice"] in questions["severity"]["options"]
    total = sum(answer["probabilities"].values())
    assert abs(total - 1.0) < 1e-6
    assert answer["confidence"] == 0.81


def test_429_retries_once_then_raises(monkeypatch):
    transport = _install(
        monkeypatch,
        _http_error(429, {"error": "rate limited"}, headers={"Retry-After": "0"}),
        _http_error(429, {"error": "rate limited"}, headers={"Retry-After": "0"}),
    )
    with pytest.raises(client.SystemOneError):
        client.ask("state text", {"q": {"type": "noul"}})
    assert len(transport.requests) == 2


def test_oversize_state_raises_before_any_request(monkeypatch):
    transport = _install(monkeypatch, FakeHTTPResponse(200, _decisions_payload()))
    huge_state = "x" * (20_000 * 4 + 1000)
    with pytest.raises(client.SystemOneError):
        client.ask(huge_state, {"q": {"type": "noul"}})
    assert len(transport.requests) == 0


def test_missing_api_key_raises_naming_the_env_var(monkeypatch):
    monkeypatch.delenv("DECISIONS_API_KEY", raising=False)
    with pytest.raises(client.SystemOneError) as exc_info:
        client.ask("state text", {"q": {"type": "noul"}})
    assert "DECISIONS_API_KEY" in str(exc_info.value)


def test_413_raises_with_estimated_token_count_in_message(monkeypatch):
    _install(monkeypatch, _http_error(413, {"error": "payload too large"}))
    state = "y" * 4000
    questions = {"q": {"type": "noul"}}
    expected = client._estimate_tokens(state, questions)
    with pytest.raises(client.SystemOneError) as exc_info:
        client.ask(state, questions)
    assert str(expected) in str(exc_info.value)


def test_empty_answers_map_is_treated_as_failure(monkeypatch):
    _install(monkeypatch, FakeHTTPResponse(200, _decisions_payload(answers={})))
    with pytest.raises(client.SystemOneError):
        client.ask("state text", {"q": {"type": "noul"}})


def test_usage_logged_with_openrouter_decisions_provider(monkeypatch, caplog):
    _install(monkeypatch, FakeHTTPResponse(200, _decisions_payload()))
    with caplog.at_level("INFO"):
        client.ask("state text", {"q": {"type": "noul"}}, caller="unit-test")
    lines = [r.getMessage() for r in caplog.records]
    usage_lines = [l for l in lines if l.startswith("LLM_USAGE")]
    assert usage_lines, "expected one LLM_USAGE log line"
    assert "provider=openrouter-decisions" in usage_lines[0]
    assert "caller=unit-test" in usage_lines[0]


def test_usage_logged_with_typesafe_provider_for_direct_url(monkeypatch):
    monkeypatch.setenv("DECISIONS_URL", "https://api.typesafe.ai/v1/systemone")
    logged = {}
    monkeypatch.setattr(client, "log_usage", lambda *a, **k: logged.update(kwargs=k, args=a))
    _install(monkeypatch, FakeHTTPResponse(200, _decisions_payload()))
    client.ask("state text", {"q": {"type": "noul"}}, caller="unit-test")
    assert logged["args"][0] == "typesafe"


def test_decisions_model_env_var_is_used_when_no_kwarg_given(monkeypatch):
    monkeypatch.setenv("DECISIONS_MODEL", "~typesafe/jev-pinned")
    transport = _install(monkeypatch, FakeHTTPResponse(200, _decisions_payload()))
    client.ask("state text", {"q": {"type": "noul"}})
    assert transport.requests[0]["body"]["model"] == "~typesafe/jev-pinned"


def test_model_kwarg_beats_decisions_model_env_var(monkeypatch):
    monkeypatch.setenv("DECISIONS_MODEL", "~typesafe/jev-pinned")
    transport = _install(monkeypatch, FakeHTTPResponse(200, _decisions_payload()))
    client.ask("state text", {"q": {"type": "noul"}}, model="~typesafe/jev-explicit")
    assert transport.requests[0]["body"]["model"] == "~typesafe/jev-explicit"


def test_never_logs_the_state(monkeypatch, caplog):
    with caplog.at_level("DEBUG"):
        _install(monkeypatch, FakeHTTPResponse(200, _decisions_payload()))
        client.ask("very secret construction site notes", {"q": {"type": "noul"}})
    all_text = " ".join(r.getMessage() for r in caplog.records)
    assert "very secret construction site notes" not in all_text
