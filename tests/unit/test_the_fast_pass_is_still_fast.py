"""`enable_thinking=False` means fast, even when the deploy asks for high effort.

`call_llm`'s own docstring promises this:

    False - force the fast non-thinking path for THIS call.
    The per-call override exists because one Lambda can need both modes:
    lambda_extract_session runs a fast live pass during recording and a
    thinking-mode final pass once the session closes.

Once `LLM_REASONING_EFFORT` was introduced it read the env FIRST and returned,
so on every deployed function the promise above was unreachable. Nothing failed.
The log line even kept printing `thinking=False` on a call that was reasoning at
high, which is why this survived a night of deploys.

Measured on the deployed live pass with `LLM_REASONING_EFFORT=high`:

    qwen done: muse-spark-1.3-contributor 119.1s prompt=44297
               completion=16379 reasoning=8753    <- thinking=False

119 seconds, on a path that is throttled to run every 90 seconds during
recording. The pass built to be cheap had become slower than its own trigger
interval — BUG-43's shape, arrived at by configuration rather than by code.

The asymmetry is deliberate and is the part worth reading twice:

* `enable_thinking=False` **overrides** the env. The caller is saying "not on
  this call", and no deploy-wide value can know better — it is per-call
  precisely because one function needs both.
* `enable_thinking=True` **does not**. The caller is saying "think"; how hard is
  the deploy's decision, so `LLM_REASONING_EFFORT` still names the level. This
  keeps the operator's "high for everything except ask" intact.
"""
import importlib
import json
import pathlib

import pytest

OPENROUTER = "https://openrouter.ai/api/v1"
ROOT = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _restore_the_module():
    yield
    import llm_utils
    importlib.reload(llm_utils)


def _load(monkeypatch, effort=None, thinking_env="false"):
    monkeypatch.setenv("QWEN_BASE_URL", OPENROUTER)
    monkeypatch.setenv("QWEN_API_KEY", "k")
    monkeypatch.setenv("LLM_PROVIDER", "qwen")
    monkeypatch.setenv("QWEN_ENABLE_THINKING", thinking_env)
    if effort is None:
        monkeypatch.delenv("LLM_REASONING_EFFORT", raising=False)
    else:
        monkeypatch.setenv("LLM_REASONING_EFFORT", effort)
    import llm_utils
    return importlib.reload(llm_utils)


def _capture(mod, monkeypatch):
    sent = {}

    class _Resp:
        status = 200
        data = json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()

    def _post(url, body, headers):
        sent["body"] = json.loads(body)
        return _Resp(), None

    monkeypatch.setattr(mod, "_post_with_retry", _post)
    return sent


# ------------------------------------------------------------------
# The override reaches the wire
# ------------------------------------------------------------------

@pytest.mark.parametrize("effort", ["high", "medium"])
def test_a_fast_call_stays_fast_under_a_high_effort_deploy(monkeypatch, effort):
    """The live extraction pass, exactly as lambda_extract_session issues it."""
    mod = _load(monkeypatch, effort=effort, thinking_env="true")
    sent = _capture(mod, monkeypatch)

    mod.call_llm("hi", max_tokens=900, force_json=True, enable_thinking=False)

    assert sent["body"]["reasoning"] == {"effort": "low"}


def test_a_thinking_call_still_takes_the_deploys_level(monkeypatch):
    """`True` is not the same shape of statement as `False`: the caller wants
    reasoning, and the operator decides how much. Demoting this to a hardcoded
    'high' would silently discard a deploy that chose 'medium'."""
    mod = _load(monkeypatch, effort="medium", thinking_env="false")
    sent = _capture(mod, monkeypatch)

    mod.call_llm("hi", enable_thinking=True)

    assert sent["body"]["reasoning"] == {"effort": "medium"}


def test_callers_that_say_nothing_are_unaffected(monkeypatch):
    """rolling-summary, report-generator, minutes and the matcher pass no
    override. The operator's 'high for everything but ask' must survive this
    change untouched, or the fix trades one silent behaviour change for another."""
    mod = _load(monkeypatch, effort="high", thinking_env="false")
    sent = _capture(mod, monkeypatch)

    mod.call_llm("hi")

    assert sent["body"]["reasoning"] == {"effort": "high"}


def test_the_fast_override_survives_an_unset_effort(monkeypatch):
    """With no env effort at all the boolean mapping already produced 'low'.
    Pinned so the two paths cannot drift into disagreeing about what False means."""
    mod = _load(monkeypatch, effort=None, thinking_env="true")
    sent = _capture(mod, monkeypatch)

    mod.call_llm("hi", enable_thinking=False)

    assert sent["body"]["reasoning"] == {"effort": "low"}


# ------------------------------------------------------------------
# ...and it is visible afterwards
# ------------------------------------------------------------------

def test_the_log_line_names_the_effort_that_travelled(monkeypatch, caplog):
    """This defect was invisible for a night because the only line about the call
    printed `thinking=False` while `effort=high` went on the wire. A log that
    reports the caller's intent instead of the payload cannot catch a layer that
    overrides the intent."""
    import logging
    mod = _load(monkeypatch, effort="high", thinking_env="true")
    _capture(mod, monkeypatch)

    with caplog.at_level(logging.INFO):
        mod.call_llm("hi", enable_thinking=False)

    line = " ".join(r.getMessage() for r in caplog.records if "qwen call" in r.getMessage())
    assert "effort=low" in line, line
    assert "thinking=False" in line


# ------------------------------------------------------------------
# The caller this was written for
# ------------------------------------------------------------------

def test_the_live_extraction_pass_asks_for_the_fast_path():
    """The fix is worthless if the caller stops passing the override. Read the
    source rather than the behaviour: this pins the wiring, and the tests above
    pin what the wiring then does."""
    src = (ROOT / "src" / "lambda_extract_session.py").read_text(encoding="utf-8")
    assert "enable_thinking=final" in src, (
        "the live/final pass distinction is how one Lambda runs both modes")
