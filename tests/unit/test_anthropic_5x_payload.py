"""The Anthropic request for Claude 5.x ids, and only for them (spec 2026-10-31 Part C).

4.x payloads must stay byte for byte what they are today -- the pins in
test_llm_utils.py are the proof of that half. This file pins the other half: a
5.x id sends no temperature (Haiku 5.5 400s on non-default sampling), adds the
reasoning headroom to max_tokens (thinking is on by default and counts against
it), and carries `output_config.effort` only when something asked for one.

HTTP is stubbed at urllib3.PoolManager.request, as in test_llm_utils.py.
"""
import json

import pytest

lu = pytest.importorskip("llm_utils", reason="requires urllib3 (installed in CI)")

SONNET_4 = "claude-sonnet-4-6"
HAIKU_4 = "claude-haiku-4-5-20251001"
SONNET_5 = "claude-sonnet-5-5"
HAIKU_5 = "claude-haiku-5-5"


class _R:
    def __init__(self, status, payload):
        self.status = status
        self.data = json.dumps(payload).encode("utf-8")


def _ok(text="hello", stop="end_turn", blocks=None):
    return _R(200, {"model": SONNET_5, "stop_reason": stop,
                    "content": blocks if blocks is not None else [{"type": "text", "text": text}],
                    "usage": {"input_tokens": 3, "output_tokens": 2}})


@pytest.fixture(autouse=True)
def anthropic(monkeypatch):
    monkeypatch.setattr(lu, "LLM_PROVIDER", "anthropic")
    monkeypatch.setattr(lu, "ANTHROPIC_API_KEY", "sk-ant-x")
    monkeypatch.setattr(lu, "LLM_TEMPERATURE", 0.0)
    monkeypatch.setattr(lu, "LLM_REASONING_EFFORT", "")
    monkeypatch.setattr(lu.time, "sleep", lambda s: None)


def _stub(monkeypatch, responses):
    sent = []
    seq = list(responses)

    def fake(self, method, url, body=None, headers=None, timeout=None):
        sent.append(body)
        return seq.pop(0)

    monkeypatch.setattr(lu.urllib3.PoolManager, "request", fake)
    return sent


# ---- the gate --------------------------------------------------------------

@pytest.mark.parametrize("model,is5", [
    (SONNET_5, True), (HAIKU_5, True), ("claude-opus-5-5", True),
    (SONNET_4, False), (HAIKU_4, False), ("claude-opus-4-1", False),
    ("claude-3-5-sonnet-20241022", False), ("", False),
])
def test_the_5x_gate(model, is5):
    # claude-haiku-4-5-20251001 carries a "-5-" too; only the major after the
    # family name may decide.
    assert lu._is_claude_5x(model) is is5


# ---- 4.x: byte-identical ---------------------------------------------------

@pytest.mark.parametrize("model", [SONNET_4, HAIKU_4])
def test_a_4x_payload_is_exactly_todays(monkeypatch, model):
    monkeypatch.setattr(lu, "CLAUDE_MODEL", model)
    sent = _stub(monkeypatch, [_ok()])
    lu._call_anthropic("p", 100, enable_thinking=False)
    assert sent[0] == json.dumps({"model": model, "max_tokens": 100,
                                  "messages": [{"role": "user", "content": "p"}],
                                  "temperature": 0.0})


def test_a_4x_payload_ignores_the_effort_env(monkeypatch):
    monkeypatch.setattr(lu, "CLAUDE_MODEL", SONNET_4)
    monkeypatch.setattr(lu, "LLM_REASONING_EFFORT", "high")
    sent = _stub(monkeypatch, [_ok()])
    lu._call_anthropic("p", 100)
    assert "output_config" not in json.loads(sent[0])


# ---- 5.x -------------------------------------------------------------------

def test_a_5x_payload_has_no_temperature_and_adds_headroom(monkeypatch):
    monkeypatch.setattr(lu, "CLAUDE_MODEL", HAIKU_5)
    sent = _stub(monkeypatch, [_ok()])
    text, err = lu._call_anthropic("p", 100)
    assert (text, err) == ("hello", None)
    body = json.loads(sent[0])
    assert "temperature" not in body, "Haiku 5.5 rejects non-default sampling"
    assert body["max_tokens"] == 100 + lu.REASONING_HEADROOM_TOKENS
    assert "output_config" not in body, "nothing asked for an effort"
    assert "thinking" not in body


def test_enable_thinking_false_asks_for_low_effort(monkeypatch):
    monkeypatch.setattr(lu, "CLAUDE_MODEL", SONNET_5)
    monkeypatch.setattr(lu, "LLM_REASONING_EFFORT", "high")
    sent = _stub(monkeypatch, [_ok()])
    lu._call_anthropic("p", 100, enable_thinking=False)
    body = json.loads(sent[0])
    assert body["output_config"] == {"effort": "low"}, "the call's fast path beats the deploy"
    assert "thinking" not in body and "budget_tokens" not in sent[0]


@pytest.mark.parametrize("env", ["low", "medium", "high"])
def test_the_deploy_effort_is_sent_when_set(monkeypatch, env):
    monkeypatch.setattr(lu, "CLAUDE_MODEL", SONNET_5)
    monkeypatch.setattr(lu, "LLM_REASONING_EFFORT", env)
    sent = _stub(monkeypatch, [_ok()])
    lu._call_anthropic("p", 100, enable_thinking=True)
    assert json.loads(sent[0])["output_config"] == {"effort": env}


def test_an_unknown_effort_sends_no_field(monkeypatch):
    monkeypatch.setattr(lu, "CLAUDE_MODEL", SONNET_5)
    monkeypatch.setattr(lu, "LLM_REASONING_EFFORT", "turbo")
    sent = _stub(monkeypatch, [_ok()])
    lu._call_anthropic("p", 100)
    assert "output_config" not in json.loads(sent[0])


def test_max_tokens_with_no_text_is_an_error_not_an_empty_answer(monkeypatch):
    monkeypatch.setattr(lu, "CLAUDE_MODEL", SONNET_5)
    _stub(monkeypatch, [_ok(stop="max_tokens",
                            blocks=[{"type": "thinking", "thinking": "..."}])])
    text, err = lu._call_anthropic("p", 100)
    assert text is None
    assert "max_tokens before any text" in err


def test_a_refusal_is_an_error(monkeypatch):
    monkeypatch.setattr(lu, "CLAUDE_MODEL", SONNET_5)
    _stub(monkeypatch, [_ok(text="", stop="refusal")])
    text, err = lu._call_anthropic("p", 100)
    assert text is None and "refusal" in err


def test_thinking_blocks_are_not_the_answer(monkeypatch):
    monkeypatch.setattr(lu, "CLAUDE_MODEL", SONNET_5)
    _stub(monkeypatch, [_ok(blocks=[{"type": "thinking", "thinking": "hmm"},
                                    {"type": "text", "text": "answer"}])])
    assert lu._call_anthropic("p", 100) == ("answer", None)


# ---- call_llm threads enable_thinking through -------------------------------

def test_call_llm_passes_enable_thinking_to_the_anthropic_branch(monkeypatch):
    monkeypatch.setattr(lu, "CLAUDE_MODEL", SONNET_5)
    sent = _stub(monkeypatch, [_ok()])
    lu.call_llm("p", max_tokens=100, enable_thinking=False)
    assert json.loads(sent[0])["output_config"] == {"effort": "low"}


def test_call_llm_keeps_the_old_call_shape_when_enable_thinking_is_none(monkeypatch):
    """A stub with the pre-existing signature must keep working."""
    seen = {}

    def old_shape(prompt, max_tokens, deadline=None, caller="unknown"):
        seen.update(prompt=prompt, max_tokens=max_tokens)
        return "ok", None

    monkeypatch.setattr(lu, "_call_anthropic", old_shape)
    assert lu.call_llm("p", max_tokens=7) == ("ok", None)
    assert seen == {"prompt": "p", "max_tokens": 7}
