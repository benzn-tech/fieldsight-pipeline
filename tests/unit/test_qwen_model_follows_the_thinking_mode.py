"""The model a qwen call reaches is a property of the CALL, not of the deploy.

Measured 2026-09-07, five runs per configuration, on one site sentence carrying
an explicit date: `qwen3.7-max` and `qwen3.6-flash` put Friday in the `due`
field 4/5 with thinking off, `qwen3.8-flash` managed 0/5 with thinking off --
in both output modes, so it is not `response_format` -- and 4/5 with thinking
on. Five of the eight qwen call sites in this repo run non-thinking
deliberately, for latency, so a blanket model bump degrades them silently:
valid JSON, no error, no log line, a missing field.

Why this needs a test rather than a comment: the model is a CloudFormation
stack parameter and thinking is a per-function env var that one caller
(`lambda_extract_session`'s live pass) overrides per call. The two values never
meet anywhere a test could see them until `_call_qwen`. A per-function model
parameter cannot separate the modes INSIDE extract-session, which is why the
split lives in the call and not the template.

See docs/superpowers/specs/2026-09-07-qwen38-flash-thinking-dependency.md
"""
import importlib
import json

import pytest

DASHSCOPE = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"


@pytest.fixture(autouse=True)
def _restore_the_module():
    """This file reloads `llm_utils` under chosen env; put it back.

    Filename order became an invisible contract in this repo once already,
    because a module reloaded under monkeypatched env keeps what it read after
    the patch is undone.
    """
    yield
    import llm_utils
    importlib.reload(llm_utils)


def _load(monkeypatch, model, nonthinking, thinking_env="false"):
    monkeypatch.setenv("QWEN_BASE_URL", DASHSCOPE)
    monkeypatch.setenv("QWEN_API_KEY", "k")
    monkeypatch.setenv("LLM_PROVIDER", "qwen")
    monkeypatch.setenv("QWEN_MODEL", model)
    monkeypatch.setenv("QWEN_MODEL_NONTHINKING", nonthinking)
    monkeypatch.setenv("QWEN_ENABLE_THINKING", thinking_env)
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


# --------------------------------------------------------------------------
# The rule
# --------------------------------------------------------------------------

@pytest.mark.parametrize("thinking,expected", [(True, "thinky"), (False, "fasty")])
def test_the_model_sent_follows_the_mode_of_the_call(monkeypatch, thinking, expected):
    mod = _load(monkeypatch, "thinky", "fasty")
    sent = _capture(mod, monkeypatch)
    mod.call_llm("hi", max_tokens=100, force_json=True, enable_thinking=thinking)
    assert sent["body"]["model"] == expected


def test_one_function_running_both_modes_reaches_both_models(monkeypatch):
    """extract-session's live pass forces thinking off and its final pass forces
    it on, inside a single deploy of a single function. This is the case no
    per-function stack parameter can express, and the reason the split is here."""
    mod = _load(monkeypatch, "thinky", "fasty", thinking_env="true")
    sent = _capture(mod, monkeypatch)
    mod.call_llm("live", max_tokens=100, force_json=True, enable_thinking=False)
    assert sent["body"]["model"] == "fasty"
    mod.call_llm("final", max_tokens=100, force_json=True, enable_thinking=True)
    assert sent["body"]["model"] == "thinky"


def test_the_env_default_decides_when_the_call_does_not(monkeypatch):
    """`enable_thinking=None` means "whatever this function is configured for",
    and the model has to follow that resolution too -- not the raw env var."""
    mod = _load(monkeypatch, "thinky", "fasty", thinking_env="true")
    sent = _capture(mod, monkeypatch)
    mod.call_llm("hi", max_tokens=100, force_json=True)
    assert sent["body"]["model"] == "thinky"


# --------------------------------------------------------------------------
# Nothing changes for a deploy that does not set it
# --------------------------------------------------------------------------

@pytest.mark.parametrize("unset", ["", "   ", "inherit"])
@pytest.mark.parametrize("thinking", [True, False])
def test_unset_means_one_model_for_both_modes(monkeypatch, unset, thinking):
    """The parameter defaults to the `inherit` sentinel, so every existing deploy
    keeps its exact behaviour. Empty and whitespace mean the same thing, because
    the value crosses a shell, a CLI override and CloudFormation on its way
    here and any of them can hand over a blank."""
    mod = _load(monkeypatch, "only-one", unset)
    sent = _capture(mod, monkeypatch)
    mod.call_llm("hi", max_tokens=100, force_json=True, enable_thinking=thinking)
    assert sent["body"]["model"] == "only-one"


# --------------------------------------------------------------------------
# The label a reader sees
# --------------------------------------------------------------------------

def test_inheritance_follows_a_changed_model_rather_than_a_snapshot(monkeypatch):
    """`inherit` is resolved when the model is read, not when the module loaded.

    Taking the snapshot at import looks equivalent and is not: anything that
    changes QWEN_MODEL afterwards -- a test patching it, a caller
    reconfiguring -- leaves the non-thinking half pointing at the value from
    load time, and the two disagree with nothing to show it. Three unrelated
    tests in this suite went red on exactly that.
    """
    mod = _load(monkeypatch, "original", "inherit")
    sent = _capture(mod, monkeypatch)
    monkeypatch.setattr(mod, "QWEN_MODEL", "changed-later")
    mod.call_llm("hi", max_tokens=100, force_json=True, enable_thinking=False)
    assert sent["body"]["model"] == "changed-later"


def test_the_reported_model_is_the_one_that_answered(monkeypatch):
    """Ask labels its answers with `active_model()`. This repo has already
    shipped a version that named a model which had not written the answer; a
    second model per deploy is a second chance to do it."""
    mod = _load(monkeypatch, "thinky", "fasty", thinking_env="false")
    assert mod.active_model() == "fasty"
    assert mod.active_model(enable_thinking=True) == "thinky"


# --------------------------------------------------------------------------
# A new qwen function cannot be added without it
# --------------------------------------------------------------------------

def test_every_function_with_a_qwen_model_also_carries_the_non_thinking_one():
    """The env key is what a Lambda actually reads. A function given QWEN_MODEL
    and not QWEN_MODEL_NONTHINKING silently pins both modes to one model, and
    nothing at runtime would say so."""
    import pathlib
    tpl = pathlib.Path(__file__).resolve().parents[2] / "src" / "template.yaml"
    lines = tpl.read_text(encoding="utf-8").splitlines()
    have, missing = 0, []
    for i, line in enumerate(lines):
        if line.strip().startswith("QWEN_MODEL: !Ref"):
            have += 1
            window = lines[max(0, i - 1):i + 2]
            if not any("QWEN_MODEL_NONTHINKING: !Ref QwenModelNonThinking" in w
                       for w in window):
                missing.append(f"line {i + 1}: {line.strip()}")
    assert have >= 7, f"expected the known qwen functions, found {have}"
    assert not missing, "QWEN_MODEL without QWEN_MODEL_NONTHINKING:\n" + "\n".join(missing)
