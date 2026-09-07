"""Unit: HTTP 200 with no answer is a failure, not an empty summary.

On a reasoning model this is the likeliest way a call fails, and the least
visible. Reasoning tokens are completion tokens and are emitted BEFORE the
answer, so when the budget runs out the money is spent, `content` comes back
empty, and every status field says success. Measured against
`meta/muse-spark-1.3-contributor`:

    max_tokens=1200  ->  completion_tokens=1200, reasoning_tokens=1197,
                         content='', finish_reason='length', HTTP 200

A caller that trusts the 200 writes a blank summary for a real day, and nothing
anywhere says why. A day with NO summary and a loud log is strictly better: the
backlog probe already looks for exactly that shape.

This is the same family as the outage that started this week -- the difference
between "it produced nothing" and "it was never asked" has to stay visible.
"""
import importlib
import json
import pytest

OPENROUTER = "https://openrouter.ai/api/v1"


class _Resp:
    def __init__(self, payload, status=200):
        self.status = status
        self.data = json.dumps(payload).encode()


def _load(monkeypatch, base_url=OPENROUTER):
    """Reload IN PLACE, never pop-and-reimport.

    `importlib.reload` mutates the same module object, so every module that did
    `import llm_utils` at its own import time still sees the reloaded one.
    Replacing sys.modules with a NEW object instead splits the identity:
    lambda_ask_agent keeps the old module, this test patches the new one, and
    twelve unrelated ask tests fail -- but only in CI, because the breakage
    depends on filename order and this file sorts before test_ask_*. That
    invisible contract has bitten this repo once already.
    """
    monkeypatch.setenv("LLM_PROVIDER", "qwen")
    monkeypatch.setenv("QWEN_API_KEY", "k")
    monkeypatch.setenv("QWEN_BASE_URL", base_url)
    monkeypatch.setenv("QWEN_MODEL", "meta/muse-spark-1.3-contributor")
    import llm_utils
    importlib.reload(llm_utils)
    monkeypatch.setattr(llm_utils, "_post_with_retry",
                        lambda *a, **k: (_Resp(_load.payload), None))
    return llm_utils


def _answered(content, finish="stop", reasoning=40, completion=60):
    return {
        "choices": [{"finish_reason": finish, "message": {"content": content}}],
        "usage": {"completion_tokens": completion,
                  "completion_tokens_details": {"reasoning_tokens": reasoning}},
    }


@pytest.fixture(autouse=True)
def _restore_the_module():
    """A module reloaded under monkeypatched env keeps what it read after the
    patch is undone -- same fixture as test_reasoning_effort.py, same reason."""
    yield
    import llm_utils
    importlib.reload(llm_utils)


def test_the_budget_spent_thinking_is_reported_as_an_error(monkeypatch):
    _load.payload = _answered("", finish="length", reasoning=1197, completion=1200)
    mod = _load(monkeypatch)
    text, err = mod.call_llm("summarise this day", max_tokens=1200)
    assert text is None, "an empty answer must not reach the caller as content"
    assert err and "empty" in err.lower()


def test_whitespace_is_not_an_answer(monkeypatch):
    """`'\\n'` parses as truthy and would sail straight through a `if content:`
    check, then land in extract_json() as a parse failure one layer away from
    the thing that actually went wrong."""
    _load.payload = _answered("   \n  ")
    mod = _load(monkeypatch)
    text, err = mod.call_llm("summarise this day", max_tokens=4096)
    assert text is None and err


def test_null_content_is_not_an_answer(monkeypatch):
    """The field is present and null when reasoning ran and produced nothing --
    that is the literal shape this vendor returns, so it must not be read as
    'the key is missing' and fall into the shape-error branch."""
    _load.payload = _answered(None, finish="length", reasoning=397, completion=400)
    mod = _load(monkeypatch)
    text, err = mod.call_llm("summarise this day", max_tokens=400)
    assert text is None and err and "empty" in err.lower()


def test_a_real_answer_still_comes_back_unchanged(monkeypatch):
    """The guard must not eat the normal path."""
    _load.payload = _answered("Level 2 progress photographed.")
    mod = _load(monkeypatch)
    text, err = mod.call_llm("summarise this day", max_tokens=4096)
    assert err is None
    assert text == "Level 2 progress photographed."


def test_an_answer_of_a_single_character_is_still_an_answer(monkeypatch):
    """A guard that trims too much is its own bug: "0" and "{}" are legitimate
    model output, and a truthiness test would drop the first of them."""
    for body in ("0", "{}"):
        _load.payload = _answered(body)
        mod = _load(monkeypatch)
        text, err = mod.call_llm("x", max_tokens=100)
        assert err is None and text == body
