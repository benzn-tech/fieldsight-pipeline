"""Tests for the locator re-test's arms changes (design 2026-11-12, C3/C4): the
runs/locator_v2/<wording>/ namespace, the --arms subset, the deployed-config mirror (empty vs
unset), the extractor env check and the effort refusal. No AWS, no network, no model call:
fakes stand in for the config read, the env loader and call_llm. All text is synthetic.
"""
from __future__ import annotations

import importlib
import json
import os
import types
from pathlib import Path

import pytest

import llm_utils
from tests.unit.test_item_support_eval_arms import (  # noqa: F401  (les: fake AWS env)
    FakeCall, _dirs, _payload, _write_set, les)

from scripts.item_support_eval import arms, store

# Every key a re-test run may set or remove in os.environ.
_TOUCHED = tuple(arms.MIRRORED_LLM_KEYS) + tuple(arms.EXTRACTOR_ENV) + (
    "LLM_PROVIDER", "CLAUDE_MODEL", "QWEN_MODEL", "QWEN_BASE_URL", "QWEN_MODEL_NONTHINKING",
    "LLM_TEMPERATURE", "ANTHROPIC_API_KEY", "QWEN_API_KEY", "DASHSCOPE_API_KEY")


@pytest.fixture(autouse=True)
def _restore_env_and_llm_utils():
    """arms mutates os.environ and reloads llm_utils; put both back for the rest of the
    suite (monkeypatch alone restores only the keys it set itself)."""
    saved = {k: os.environ.get(k) for k in _TOUCHED}
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        importlib.reload(llm_utils)


def _deployed(**over):
    """A deployed Variables map that agrees with this process's extractor env."""
    env = {k: os.environ[k] for k in arms.EXTRACTOR_ENV if k in os.environ}
    env.update({"LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "sk-synthetic-secret",
                "LLM_REASONING_EFFORT": "high", "LLM_FALLBACK_MODELS": "",
                "LLM_HTTP_TIMEOUT": "120"})
    for k, v in over.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    return env


class FakeReadEnv:
    def __init__(self, variables):
        self.variables = variables
        self.calls = 0

    def __call__(self, function_name, **kw):
        self.calls += 1
        return dict(self.variables)


class FakeLoadEnv:
    """Stands in for load_deployed_llm_env: names the keys it would copy, sets none."""

    def __init__(self):
        self.calls = []

    def __call__(self, function_name, **kw):
        self.calls.append(kw)
        return ["ANTHROPIC_API_KEY", "LLM_PROVIDER"]


def _main(argv, data, runs, *, call=None, read_env=None, load_env=None):
    return arms.main(argv + ["--data", str(data), "--runs", str(runs)],
                     call=call or FakeCall(), read_env=read_env or FakeReadEnv(_deployed()),
                     load_env=load_env or FakeLoadEnv())


def _v2_files(runs, wording="w1"):
    base = Path(runs, "locator_v2", wording)
    return {tuple(p.relative_to(base).parts): store.read_json(p)
            for p in sorted(base.rglob("*.json"))}


# =========================================================================================
# store
# =========================================================================================

def test_store_paths_old_namespace_unchanged_and_v2_per_wording(tmp_path):
    runs = tmp_path / "item_support_eval" / "runs"
    assert store.locator_run_path("B", 1, "r1", runs_dir=runs) == runs / "locator/B/1/r1.json"
    assert store.locator_run_path("B", 1, "r1", side=True, runs_dir=runs) \
        == runs / "locator/side/B/1/r1.json"
    assert store.locator_run_path("B", 1, "r1", runs_dir=runs, wording="w2") \
        == runs / "locator_v2/w2/B/1/r1.json"
    assert store.locator_run_path("A", 2, "r1", side=True, runs_dir=runs, wording="w1") \
        == runs / "locator_v2/w1/side/A/2/r1.json"
    labels = tmp_path / "item_support_eval" / "labels"
    assert store.locator_labels_dir(labels) == labels
    assert store.locator_labels_dir(labels, "w1") == labels / "v2" / "w1"
    with pytest.raises(SystemExit):
        store.locator_run_path("B", 1, "r1", runs_dir=runs, wording="w3")
    with pytest.raises(SystemExit):
        store.locator_labels_dir(labels, "v2")


# =========================================================================================
# --arms / --wording / count / confirm gate
# =========================================================================================

def test_parse_arms():
    assert arms.parse_arms(None) == ("A1", "A2", "B1", "B2")
    assert arms.parse_arms("B1") == ("B1",)
    assert arms.parse_arms("B2, A1") == ("A1", "B2")
    for bad in ("", "B3", "B1,C1", "b1"):
        with pytest.raises(SystemExit):
            arms.parse_arms(bad)


def test_count_honours_arms_and_wording_and_makes_no_call(tmp_path):
    data, runs = _dirs(tmp_path)
    _write_set(data, 3, 2)
    call, read, load = FakeCall(), FakeReadEnv(_deployed()), FakeLoadEnv()
    out = _main(["count", "--stage", "fit", "--wording", "w1", "--arms", "B1"], data, runs,
                call=call, read_env=read, load_env=load)
    assert (out["calls"], out["total"], out["wording"], out["arms"]) == (3, 3, "w1", ["B1"])
    out = _main(["count", "--stage", "heldout", "--wording", "w1"], data, runs)
    assert out["calls"] == 8
    old = _main(["count", "--stage", "fit"], data, runs)
    assert old == {"stage": "fit", "calls": 12, "total": 12, "skipped_existing": 0}
    assert call.calls == [] and read.calls == 0 and load.calls == []


def test_wording_must_be_w1_or_w2(tmp_path):
    data, runs = _dirs(tmp_path)
    _write_set(data, 1, 0)
    with pytest.raises(SystemExit) as e:
        _main(["count", "--stage", "fit", "--wording", "w3"], data, runs)
    assert e.value.code == 2


def test_confirm_count_must_equal_the_restricted_count(tmp_path):
    data, runs = _dirs(tmp_path)
    _write_set(data, 3, 0)
    call, read = FakeCall(), FakeReadEnv(_deployed())
    for n in ("12", "4"):        # the unrestricted count, and a wrong one
        with pytest.raises(SystemExit) as e:
            _main(["run", "--stage", "fit", "--wording", "w1", "--arms", "B1",
                   "--emit-evidence", "false", "--confirm-count", n], data, runs,
                  call=call, read_env=read)
        assert e.value.code == 2
    assert call.calls == [] and read.calls == 0
    assert not Path(runs, "locator_v2").exists()


# =========================================================================================
# run: namespace, record, resume
# =========================================================================================

def test_run_b1_only_writes_under_locator_v2_with_the_deployed_record(tmp_path):
    data, runs = _dirs(tmp_path)
    _write_set(data, 3, 2)
    call, read, load = FakeCall(), FakeReadEnv(_deployed()), FakeLoadEnv()
    out = _main(["run", "--stage", "fit", "--wording", "w1", "--arms", "B1",
                 "--emit-evidence", "false", "--confirm-count", "3"], data, runs,
                call=call, read_env=read, load_env=load)
    assert out["calls"] == 3 and out["errors"] == 0 and out["reasoning_effort"] == "high"
    assert len(call.calls) == 3 and read.calls == 1 and len(load.calls) == 1
    # load_deployed_llm_env copies from the same snapshot this module compared.
    assert load.calls[0]["deployed"] == _deployed()
    files = _v2_files(runs)
    assert sorted(files) == [("B", "1", f"r00{i}.json") for i in range(3)]
    assert not Path(runs, "locator").exists()
    for rec in files.values():
        assert rec["arm"] == "B" and rec["rep"] == 1 and rec["error"] is None
        assert rec["prompt_sha"] == arms.prompt_sha(True)
        assert rec["wording"] == "w1"
        assert rec["reasoning_effort"] == "high"
        assert rec["fallback_models"] == []                  # set but empty: no fallback
        assert rec["primary_model"] == llm_utils.active_model(enable_thinking=True)
        env = rec["deployed_env"]
        assert env["LLM_REASONING_EFFORT"] == "high" and env["LLM_FALLBACK_MODELS"] == ""
        assert env["LLM_HTTP_TIMEOUT"] == "120" and env["LLM_PROVIDER"] == "anthropic"
        assert env["LLM_CHAIN_BUDGET_SECONDS"] is None       # unset on the function
        assert "ANTHROPIC_API_KEY" not in env
        assert "sk-synthetic-secret" not in json.dumps(rec)
    # The next count under the same wording and arms is zero; w2 is a separate namespace.
    assert _main(["count", "--stage", "fit", "--wording", "w1", "--arms", "B1"],
                 data, runs)["calls"] == 0
    assert _main(["count", "--stage", "fit", "--wording", "w2", "--arms", "B1"],
                 data, runs)["calls"] == 3


def test_run_without_wording_keeps_the_first_tests_path_and_record(tmp_path):
    data, runs = _dirs(tmp_path)
    _write_set(data, 1, 0)
    read = FakeReadEnv({})          # would refuse (no effort) if the re-test steps ran
    out = _main(["run", "--stage", "fit", "--arms", "B1", "--emit-evidence", "false",
                 "--confirm-count", "1"], data, runs, read_env=read)
    assert out["calls"] == 1 and read.calls == 0
    rec = store.read_json(Path(runs, "locator", "B", "1", "r000.json"))
    assert "wording" not in rec and "deployed_env" not in rec
    assert not Path(runs, "locator_v2").exists()


def test_side_stage_under_v2(tmp_path):
    data, runs = _dirs(tmp_path)
    _write_set(data, 1, 0, n_side=2)
    out = _main(["run", "--stage", "side", "--wording", "w2", "--emit-evidence", "false",
                 "--confirm-count", "8"], data, runs)
    assert out["calls"] == 8
    files = _v2_files(runs, "w2")
    assert len(files) == 8 and all(k[0] == "side" for k in files)


def test_raw_said_at_keeps_the_childs_status_at_before_the_strip(tmp_path):
    data, runs = _dirs(tmp_path)
    _write_set(data, 1, 0)
    sugg = {"status_suggestion": {"status": "done", "quote": "synthetic", "at": "10:00:20"}}
    call = FakeCall(lambda n, prompt: (json.dumps(_payload("10:00:00", extra=sugg)), None))
    _main(["run", "--stage", "fit", "--wording", "w1", "--arms", "B1",
           "--emit-evidence", "false", "--confirm-count", "1"], data, runs, call=call)
    rec = _v2_files(runs)[("B", "1", "r000.json")]
    assert rec["_raw_said_at"] == [{"item": "0:action_items:0", "said_at": "10:00:00",
                                    "status_at": "10:00:20"}]
    assert rec["status_suggestions_stripped"] == 1


# =========================================================================================
# Deployed config: mirror, effort refusal, extractor env check
# =========================================================================================

def test_mirror_keeps_empty_apart_from_unset():
    env = {"LLM_FALLBACK_MODELS": "stale", "LLM_HTTP_TIMEOUT": "9", "QWEN_ENABLE_THINKING": "x"}
    got = arms.mirror_deployed_keys({"LLM_FALLBACK_MODELS": "", "LLM_REASONING_EFFORT": "high",
                                     "QWEN_ENABLE_THINKING": "true"}, environ=env)
    assert env == {"LLM_FALLBACK_MODELS": "", "LLM_REASONING_EFFORT": "high",
                   "QWEN_ENABLE_THINKING": "true"}
    assert got["LLM_FALLBACK_MODELS"] == "" and got["LLM_HTTP_TIMEOUT"] is None
    assert set(got) == set(arms.MIRRORED_LLM_KEYS)


def test_mirror_then_reload_reaches_llm_utils(tmp_path):
    data, runs = _dirs(tmp_path)
    _write_set(data, 1, 0)
    os.environ["LLM_FALLBACK_MODELS"] = "stale-model"
    os.environ["LLM_REASONING_HEADROOM"] = "1"
    _main(["run", "--stage", "fit", "--wording", "w1", "--arms", "B1", "--emit-evidence",
           "false", "--confirm-count", "1"], data, runs,
          read_env=FakeReadEnv(_deployed(LLM_FALLBACK_MODELS=None,
                                         LLM_REASONING_HEADROOM="1234")))
    assert "LLM_FALLBACK_MODELS" not in os.environ       # missing on the function -> removed
    assert llm_utils.fallback_models() == [
        m.strip() for m in llm_utils.DEFAULT_FALLBACK_MODELS.split(",") if m.strip()]
    assert llm_utils.REASONING_HEADROOM_TOKENS == 1234   # reloaded
    assert llm_utils.LLM_REASONING_EFFORT == "high"
    rec = _v2_files(runs)[("B", "1", "r000.json")]
    assert rec["deployed_env"]["LLM_FALLBACK_MODELS"] is None
    assert rec["fallback_models"] == llm_utils.fallback_models()


@pytest.mark.parametrize("effort", [None, "", "  "])
def test_no_deployed_effort_refuses_before_any_call(tmp_path, effort):
    data, runs = _dirs(tmp_path)
    _write_set(data, 1, 0)
    call, load = FakeCall(), FakeLoadEnv()
    with pytest.raises(SystemExit) as e:
        _main(["run", "--stage", "fit", "--wording", "w1", "--arms", "B1", "--emit-evidence",
               "false", "--confirm-count", "1"], data, runs, call=call, load_env=load,
              read_env=FakeReadEnv(_deployed(LLM_REASONING_EFFORT=effort)))
    assert e.value.code == 2
    assert call.calls == [] and load.calls == []
    assert not Path(runs, "locator_v2").exists()


def test_allow_default_effort_runs_and_records_none(tmp_path):
    data, runs = _dirs(tmp_path)
    _write_set(data, 1, 0)
    _main(["run", "--stage", "fit", "--wording", "w1", "--arms", "B1", "--emit-evidence",
           "false", "--confirm-count", "1", "--allow-default-effort"], data, runs,
          read_env=FakeReadEnv(_deployed(LLM_REASONING_EFFORT=None)))
    rec = _v2_files(runs)[("B", "1", "r000.json")]
    assert rec["reasoning_effort"] is None
    assert rec["deployed_env"]["LLM_REASONING_EFFORT"] is None


@pytest.mark.parametrize("over", [{"TRANSCRIPT_TEXT_LIMIT": "123"},
                                  {"EVIDENCE_FUZZY_THRESHOLD": "0.5"},
                                  {"EVIDENCE_SOMETHING_NEW": "1"},
                                  {"STATUS_EVIDENCE_FLOOR_TOKENS": "9"}])
def test_extractor_env_mismatch_refuses_before_any_call(tmp_path, over):
    data, runs = _dirs(tmp_path)
    _write_set(data, 1, 0)
    call, load = FakeCall(), FakeLoadEnv()
    with pytest.raises(SystemExit) as e:
        _main(["run", "--stage", "fit", "--wording", "w1", "--arms", "B1", "--emit-evidence",
               "false", "--confirm-count", "1"], data, runs, call=call, load_env=load,
              read_env=FakeReadEnv(_deployed(**over)))
    assert e.value.code == 2
    assert call.calls == [] and load.calls == []


def test_extractor_env_mismatches_strings_and_imported_constants():
    # Strings: both unset agree; set vs unset and different values do not.
    assert arms.extractor_env_mismatches({}, environ={}) == []
    got = arms.extractor_env_mismatches({"TRANSCRIPT_TEXT_LIMIT": "300000"}, environ={})
    assert got == [{"key": "TRANSCRIPT_TEXT_LIMIT", "deployed": "300000", "local": None}]
    got = arms.extractor_env_mismatches({}, environ={"EVIDENCE_WINDOW_SEC": "30"})
    assert [m["key"] for m in got] == ["EVIDENCE_WINDOW_SEC"]
    # An imported module's constants are compared with the deployed value parsed its way.
    mod = types.SimpleNamespace(TRANSCRIPT_TEXT_LIMIT=300000, EVIDENCE_WINDOW_SEC=60.0,
                                EVIDENCE_FLOOR_TOKENS=5, EVIDENCE_FUZZY=0.80,
                                STATUS_EVIDENCE_FLOOR_TOKENS=5)
    assert arms.extractor_env_mismatches({}, environ={}, module=mod) == []
    mod.EVIDENCE_FUZZY = 0.7          # imported under another env than the one compared
    got = arms.extractor_env_mismatches({}, environ={}, module=mod)
    assert [m["key"] for m in got] == ["EVIDENCE_FUZZY_THRESHOLD"]


def test_deployed_record_never_keeps_a_secret_value():
    rec = arms.deployed_record({"LLM_PROVIDER": "anthropic", "QWEN_API_KEY": "s1",
                                "DASHSCOPE_API_KEY": "s2"},
                               ["LLM_PROVIDER", "QWEN_API_KEY", "DASHSCOPE_API_KEY"],
                               {"LLM_ANSWER_TOKEN_CEILING": "100"}, "w2")
    assert rec["deployed_env"] == {"LLM_ANSWER_TOKEN_CEILING": "100",
                                   "LLM_PROVIDER": "anthropic"}
    assert rec["wording"] == "w2"


def test_default_load_env_replays_the_snapshot_without_a_second_read():
    # load_deployed_llm_env's allowlist runs on the snapshot; no aws call is made.
    got = arms._default_load_env("fn", profile="p", region="r",
                                 deployed={"LLM_PROVIDER": "anthropic",
                                           "CLAUDE_MODEL": "fake-model",
                                           "LLM_REASONING_EFFORT": "high"})
    assert got == ["CLAUDE_MODEL", "LLM_PROVIDER"]
    assert os.environ["CLAUDE_MODEL"] == "fake-model"


def test_default_read_env_parses_the_cli_output_with_a_fake_run():
    seen = {}

    def run(cmd, **kw):
        seen["cmd"] = cmd
        out = {"Environment": {"Variables": {"LLM_REASONING_EFFORT": "high"}}}
        return types.SimpleNamespace(returncode=0, stdout=json.dumps(out).encode(),
                                     stderr=b"")

    got = arms._default_read_env("fn", profile="p", region="r", run=run)
    assert got == {"LLM_REASONING_EFFORT": "high"}
    assert seen["cmd"][:3] == ["aws", "lambda", "get-function-configuration"]

    def fail(cmd, **kw):
        return types.SimpleNamespace(returncode=255, stdout=b"", stderr=b"denied")

    with pytest.raises(SystemExit):
        arms._default_read_env("fn", profile="p", region="r", run=fail)
