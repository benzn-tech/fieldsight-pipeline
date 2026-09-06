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

@pytest.mark.parametrize("unset", ["", "   ", "inherit", "Inherit", "INHERIT"])
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

def _qwen_env_blocks():
    """(model_ref, non_thinking_ref or None) for every function in the template.

    Parsed rather than substring-matched: a commented-out pairing satisfies
    `"X" in line` while CloudFormation ignores it, so a broken template would
    pass. These env blocks are heavily commented, so a fixed +/-1 line window
    also fails a correct template the moment somebody adds a note between the
    two keys.
    """
    import pathlib
    tpl = pathlib.Path(__file__).resolve().parents[2] / "src" / "template.yaml"
    blocks, cur = [], None
    for raw in tpl.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("#") or not line:
            continue
        if line.startswith("QWEN_MODEL: !Ref "):
            if cur is not None:
                blocks.append(cur)
            cur = [line.split("!Ref ", 1)[1].strip(), None]
        elif line.startswith("QWEN_MODEL_NONTHINKING: !Ref ") and cur is not None:
            cur[1] = line.split("!Ref ", 1)[1].strip()
    if cur is not None:
        blocks.append(cur)
    return blocks


def test_every_plus_function_carries_the_non_thinking_parameter():
    """The env key is what a Lambda actually reads. A Plus function given
    QWEN_MODEL and not QWEN_MODEL_NONTHINKING silently pins both modes to one
    model, and nothing at runtime would say so."""
    blocks = _qwen_env_blocks()
    plus = [b for b in blocks if b[0] == "QwenModelPlus"]
    assert len(plus) >= 6, f"expected the known plus functions, found {len(plus)}"
    missing = [b for b in plus if b[1] != "QwenModelPlusNonThinking"]
    assert not missing, f"QwenModelPlus functions without the override: {missing}"


def test_the_ask_path_is_deliberately_left_out():
    """AskAgentFunction is ALWAYS non-thinking, so QwenModelFast is already its
    non-thinking model. Mirroring the override onto it would make QwenModelFast
    dead config and move the latency-bound path onto the heavier model the first
    time somebody set the variable to protect extract-session's live pass --
    silently, since the answer would still be correct, just slower.

    This pins the ABSENCE, which is the half a reviewer forgets to pin."""
    fast = [b for b in _qwen_env_blocks() if b[0] == "QwenModelFast"]
    assert len(fast) == 1, f"expected exactly one Fast function, found {len(fast)}"
    assert fast[0][1] is None, (
        "AskAgentFunction must NOT carry QWEN_MODEL_NONTHINKING -- see the "
        "parameter Description in template.yaml")


def test_no_function_hardcodes_a_qwen_model():
    """A literal `QWEN_MODEL: qwen3.6-flash` would escape both checks above:
    it is neither a Plus nor a Fast reference, so it is invisible to the pairing
    rule while still reaching a model."""
    import pathlib, re
    tpl = pathlib.Path(__file__).resolve().parents[2] / "src" / "template.yaml"
    bad = [ln.strip() for ln in tpl.read_text(encoding="utf-8").splitlines()
           if re.match(r"^\s*QWEN_MODEL:\s*(?!!Ref)\S", ln) and not ln.strip().startswith("#")]
    assert not bad, f"QWEN_MODEL must be a !Ref so the pairing rule can see it: {bad}"


def test_both_workflows_pass_the_parameter():
    """Without this, deleting either workflow line leaves every other test green
    and the repo variable silently inert -- the deploy falls back to the CFN
    default and nobody learns the knob stopped working."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[2] / ".github" / "workflows"
    for name, prefix in (("deploy.yml", "TEST"), ("deploy-prod.yml", "PROD")):
        text = (root / name).read_text(encoding="utf-8")
        assert "QwenModelPlusNonThinking=" in text, f"{name} does not pass the parameter"
        assert f"{prefix}_QWEN_MODEL_PLUS_NONTHINKING" in text, (
            f"{name} does not read its own repo variable")
