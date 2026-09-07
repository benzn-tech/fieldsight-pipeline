"""On an OpenAI-compatible vendor, reasoning is spent out of the answer's budget.

`max_tokens` there is the budget for the WHOLE completion, and reasoning tokens
come out of it first. Measured 2026-09-07 against `meta/muse-spark-1.3-contributor`
on a two-sentence extraction prompt: at `effort=high` with `max_tokens=900` the
model spent **897 of them reasoning**, returned `finish_reason: length` with an
**empty** answer, and was billed for all 900. Every batch caller in this repo asks
for far less than the reasoning alone needs, so the whole Plus family would have
returned empty extractions on every call.

DashScope does not have this problem: its thinking branch drops `max_tokens`
entirely, and its reasoning is carried in a separate `reasoning_content` field.
The two vendors' budgets are not the same object, which is why the allowance is
added only on the non-DashScope path.

The second half matters more than the first. A 200 carrying an empty string is
the worst shape this API has -- the caller sees success and writes an empty
extraction -- and no budget number is large enough to guarantee it never happens.
"""
import importlib
import json

import pytest

DASHSCOPE = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
OPENROUTER = "https://openrouter.ai/api/v1"


@pytest.fixture(autouse=True)
def _restore_the_module():
    yield
    import llm_utils
    importlib.reload(llm_utils)


def _load(monkeypatch, base_url, effort=None, thinking_env="false"):
    monkeypatch.setenv("QWEN_BASE_URL", base_url)
    monkeypatch.setenv("QWEN_API_KEY", "k")
    monkeypatch.setenv("LLM_PROVIDER", "qwen")
    monkeypatch.setenv("QWEN_ENABLE_THINKING", thinking_env)
    if effort is None:
        monkeypatch.delenv("LLM_REASONING_EFFORT", raising=False)
    else:
        monkeypatch.setenv("LLM_REASONING_EFFORT", effort)
    import llm_utils
    return importlib.reload(llm_utils)


def _capture(mod, monkeypatch, body=None, status=200):
    sent = {}
    payload = body if body is not None else {
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}

    class _Resp:
        def __init__(self):
            self.status = status
            self.data = json.dumps(payload).encode()

    def _post(url, b, headers):
        sent["body"] = json.loads(b)
        return _Resp(), None

    monkeypatch.setattr(mod, "_post_with_retry", _post)
    return sent


# --------------------------------------------------------------------------
# The budget
# --------------------------------------------------------------------------

@pytest.mark.parametrize("effort", ["low", "medium", "high", None])
def test_reasoning_gets_its_own_allowance_on_top(monkeypatch, effort):
    """The caller's number stays the ANSWER budget. Folding reasoning into it is
    what produced a billed, empty, `finish_reason: length` response.

    The allowance is flat rather than per-effort. A parallel change measured the
    same wall from the other side and found the spread does not track the level
    the way a tier table implies -- 516 tokens at low, 743 at high, and 1671 in
    JSON mode, which is the highest of the three and is not an effort level at
    all. A single generous number is honest about that; three tuned ones would
    claim a precision the measurements do not support."""
    mod = _load(monkeypatch, OPENROUTER, effort=effort)
    sent = _capture(mod, monkeypatch)
    mod.call_llm("hi", max_tokens=900)
    assert sent["body"]["max_tokens"] == 900 + mod.REASONING_HEADROOM_TOKENS


def test_the_allowance_is_configurable_without_a_deploy(monkeypatch):
    """No measured number survives a new model. This one is an env var so the
    next wall can be moved before it is understood."""
    monkeypatch.setenv("LLM_REASONING_HEADROOM", "1234")
    mod = _load(monkeypatch, OPENROUTER, effort="high")
    sent = _capture(mod, monkeypatch)
    mod.call_llm("hi", max_tokens=900)
    assert sent["body"]["max_tokens"] == 900 + 1234


@pytest.mark.parametrize("effort", ["low", "high"])
def test_dashscope_budget_is_untouched(monkeypatch, effort):
    """DashScope carries reasoning in a separate field and drops max_tokens in
    thinking mode. Adding an allowance there would silently raise the cap on a
    per-token bill for no reason."""
    mod = _load(monkeypatch, DASHSCOPE, effort=effort)
    sent = _capture(mod, monkeypatch)
    mod.call_llm("hi", max_tokens=900, enable_thinking=False)
    assert sent["body"].get("max_tokens") == 900


# --------------------------------------------------------------------------
# The guard that matters more than the budget
# --------------------------------------------------------------------------

@pytest.mark.parametrize("content", ["", "   ", None])
def test_an_empty_answer_is_an_error_not_a_success(monkeypatch, content):
    """A 200 carrying nothing is the worst shape this API has: the caller sees
    success, gets an empty string, and writes an empty extraction. No budget is
    large enough to guarantee this never happens, so it has to be named."""
    mod = _load(monkeypatch, OPENROUTER, effort="high")
    _capture(mod, monkeypatch, body={
        "choices": [{"message": {"content": content}, "finish_reason": "length"}],
        "usage": {"completion_tokens_details": {"reasoning_tokens": 897}},
        "model": "meta/muse-spark-1.3-contributor"})
    text, err = mod.call_llm("hi", max_tokens=900)
    assert text is None
    assert err and "empty" in err.lower()
    assert "length" in err


def test_the_empty_answer_log_says_why(monkeypatch, caplog):
    """`finish_reason` and the reasoning token count are the difference between
    'the model said nothing' and 'the model was cut off mid-thought' -- same
    symptom, different fix."""
    import logging
    mod = _load(monkeypatch, OPENROUTER, effort="high")
    _capture(mod, monkeypatch, body={
        "choices": [{"message": {"content": ""}, "finish_reason": "length"}],
        "usage": {"completion_tokens_details": {"reasoning_tokens": 897}},
        "model": "meta/muse-spark-1.3-contributor"})
    with caplog.at_level(logging.ERROR):
        mod.call_llm("hi", max_tokens=900)
    line = " ".join(r.getMessage() for r in caplog.records)
    assert "897" in line
    assert "length" in line


def test_a_real_answer_still_comes_back(monkeypatch):
    """The guard must not eat the ordinary case."""
    mod = _load(monkeypatch, OPENROUTER, effort="high")
    _capture(mod, monkeypatch, body={
        "choices": [{"message": {"content": '{"items":[]}'}, "finish_reason": "stop"}]})
    text, err = mod.call_llm("hi", max_tokens=900)
    assert err is None
    assert text == '{"items":[]}'
