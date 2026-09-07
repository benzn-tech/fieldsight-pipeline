"""`reasoning.effort` is a different knob from `enable_thinking`, and must not cross.

DashScope expresses reasoning as a boolean (`enable_thinking`). OpenAI-compatible
vendors take `reasoning: {"effort": low|medium|high}`, where the levels are priced
and latency-differentiated. Sending the wrong one to the wrong vendor is the quiet
failure this file exists to prevent: an unknown field is either rejected loudly or
DROPPED SILENTLY, and a silently dropped "low" means the path chosen for latency
quietly pays for full reasoning while still returning a correct answer.

The split follows the model families, because that is where the requirement lands:
AskAgentFunction reads QwenModelFast and is the latency-bound path, so it gets the
low effort; the six QwenModelPlus functions do batch work and get the high one.
"""
import importlib
import json
import pathlib

import pytest

DASHSCOPE = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
OPENROUTER = "https://openrouter.ai/api/v1"
ROOT = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _restore_the_module():
    """A module reloaded under monkeypatched env keeps what it read after the
    patch is undone. Filename order became an invisible contract in this repo
    once already; put it back."""
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


def _param_default(param):
    """The `Default:` of one template Parameter, by walking its block."""
    text = (ROOT / "src" / "template.yaml").read_text(encoding="utf-8")
    inside = False
    for raw in text.splitlines():
        if raw.startswith("  ") and not raw.startswith("   ") and raw.rstrip().endswith(":"):
            inside = raw.strip() == param + ":"
            continue
        if inside and raw.strip().startswith("Default:"):
            return raw.split("Default:", 1)[1].strip().strip("'").strip('"')
    return None


# --------------------------------------------------------------------------
# The rule
# --------------------------------------------------------------------------

@pytest.mark.parametrize("effort", ["low", "medium", "high"])
def test_effort_reaches_an_openai_compatible_vendor(monkeypatch, effort):
    mod = _load(monkeypatch, OPENROUTER, effort=effort)
    sent = _capture(mod, monkeypatch)
    mod.call_llm("hi", max_tokens=100)
    assert sent["body"]["reasoning"] == {"effort": effort}


@pytest.mark.parametrize("effort", ["low", "high"])
@pytest.mark.parametrize("thinking", [True, False])
def test_effort_never_reaches_dashscope(monkeypatch, effort, thinking):
    """DashScope has no `reasoning` field at all. An unknown key there is at best
    ignored, and 'at best ignored' is exactly how a knob becomes decorative."""
    mod = _load(monkeypatch, DASHSCOPE, effort=effort)
    sent = _capture(mod, monkeypatch)
    mod.call_llm("hi", max_tokens=100, enable_thinking=thinking)
    assert "reasoning" not in sent["body"]
    assert sent["body"]["enable_thinking"] is thinking


@pytest.mark.parametrize("thinking", [True, False])
def test_unset_effort_keeps_the_boolean_every_deploy_had(monkeypatch, thinking):
    """The parameter is new, so an unset value must change no request."""
    mod = _load(monkeypatch, OPENROUTER, effort=None)
    sent = _capture(mod, monkeypatch)
    mod.call_llm("hi", max_tokens=100, enable_thinking=thinking)
    assert sent["body"]["reasoning"] == {"enabled": thinking}


@pytest.mark.parametrize("bad", ["highest", "none", "0", "true"])
def test_an_unknown_effort_falls_back_instead_of_travelling(monkeypatch, bad):
    """A level this vendor does not know is worse than saying nothing: it can be
    rejected, or accepted-and-ignored, and the second is unobservable."""
    mod = _load(monkeypatch, OPENROUTER, effort=bad)
    sent = _capture(mod, monkeypatch)
    mod.call_llm("hi", max_tokens=100, enable_thinking=True)
    assert sent["body"]["reasoning"] == {"enabled": True}


@pytest.mark.parametrize("messy", ["LOW", " high ", "Medium"])
def test_a_real_value_wearing_whitespace_and_capitals_still_works(monkeypatch, messy):
    """What a repo variable typed by a human looks like. The value crosses a
    workflow, a CLI override and CloudFormation, and none of them normalise it."""
    mod = _load(monkeypatch, OPENROUTER, effort=messy)
    sent = _capture(mod, monkeypatch)
    mod.call_llm("hi", max_tokens=100)
    assert sent["body"]["reasoning"] == {"effort": messy.strip().lower()}


# --------------------------------------------------------------------------
# The wiring, which is where this kind of parameter dies
# --------------------------------------------------------------------------

def _qwen_env_blocks():
    """(model_ref, effort_ref or None) per function, parsed rather than
    substring-matched: a commented-out line satisfies `"x" in line` while
    CloudFormation ignores it."""
    text = (ROOT / "src" / "template.yaml").read_text(encoding="utf-8")
    blocks, cur = [], None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#") or not line:
            continue
        if line.startswith("QWEN_MODEL: !Ref "):
            if cur is not None:
                blocks.append(cur)
            cur = [line.split("!Ref ", 1)[1].strip(), None]
        elif line.startswith("LLM_REASONING_EFFORT: !Ref ") and cur is not None:
            cur[1] = line.split("!Ref ", 1)[1].strip()
    if cur is not None:
        blocks.append(cur)
    return blocks


def test_every_function_carries_an_effort_matching_its_model_family():
    """A function given a model and no effort silently keeps the boolean, and
    nothing at runtime would say so."""
    want = {"QwenModelPlus": "LlmReasoningEffortPlus",
            "QwenModelFast": "LlmReasoningEffortFast"}
    blocks = _qwen_env_blocks()
    assert len(blocks) >= 7, f"expected the known qwen functions, found {len(blocks)}"
    wrong = [b for b in blocks if b[1] != want.get(b[0])]
    assert not wrong, f"model/effort family mismatch: {wrong}"


def test_the_latency_path_is_the_one_that_gets_low():
    """The whole requirement in one assertion: ask reads QwenModelFast, and that
    family's default effort is the cheap one. If these ever agree with the Plus
    family, the distinction this parameter exists for is gone."""
    fast, plus = _param_default("LlmReasoningEffortFast"), _param_default("LlmReasoningEffortPlus")
    assert fast == "low", f"the ask path defaults to {fast!r}, not low"
    assert plus == "high", f"the batch path defaults to {plus!r}, not high"
    assert fast != plus


def test_both_workflows_pass_both_efforts():
    """Without this, deleting the workflow lines leaves every other test green and
    the repo variables silently inert -- the deploy falls back to the CFN default
    and nobody learns the knob stopped working."""
    root = ROOT / ".github" / "workflows"
    cases = (("LlmReasoningEffortPlus", "LLM_EFFORT_PLUS", "high"),
             ("LlmReasoningEffortFast", "LLM_EFFORT_FAST", "low"))
    for name, prefix in (("deploy.yml", "TEST"), ("deploy-prod.yml", "PROD")):
        text = (root / name).read_text(encoding="utf-8")
        for param, var, lit in cases:
            assert param + "=" in text, f"{name} does not pass {param}"
            assert prefix + "_" + var in text, f"{name} does not read its own variable"
            assert "|| '" + lit + "'" in text, f"{name}: {param} must fall back to {lit}"


def test_the_workflow_literal_agrees_with_the_template_default():
    """Two places state a default and only the workflow's is ever used -- the CLI
    always passes a value, so CloudFormation never consults its own. Pin them
    equal so the losing one cannot drift into a plausible lie."""
    import re
    root = ROOT / ".github" / "workflows"
    for param in ("LlmReasoningEffortPlus", "LlmReasoningEffortFast"):
        tpl = _param_default(param)
        for name in ("deploy.yml", "deploy-prod.yml"):
            text = (root / name).read_text(encoding="utf-8")
            m = re.search(param + r"=\$\{\{ vars\.\w+ \|\| '([^']+)' \}\}", text)
            assert m, f"{name}: no fallback found for {param}"
            assert m.group(1) == tpl, (
                f"{param}: template says {tpl!r}, {name} deploys {m.group(1)!r}. "
                f"The workflow wins, so the template documents a value nothing uses.")
