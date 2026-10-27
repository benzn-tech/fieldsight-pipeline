"""Report bench, Task 12: select, keep rule, frozen.v1.json, later phases."""
import importlib.util
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BENCH_DIR = os.path.join(REPO, "tools", "report-bench")


def _load(name):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, os.path.join(BENCH_DIR, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


sel = _load("rb_select")
run = _load("rb_run")
common = _load("rb_common")
SESS = ["s1", "s2", "s3", "s4"]


def _cell(recalls, failed=(), f=0.1, misread=0.0):
    """recalls: per session -> value (3 identical reps)."""
    sessions = {s: {"reps": [{"action_recall": v, "misread": misread}] * 3,
                    "failed": s in failed} for s, v in recalls.items()}
    return {"sessions": sessions, "failed_sessions": len(failed),
            "mean": {"action_recall": sum(recalls.values()) / len(recalls), "misread": misread},
            "noise_floor": {"action_recall": f}}


def _scores(**models):
    return {f"X|X-cur|{m}": c for m, c in models.items()}


def test_trigger_a_two_failed_sessions():
    s = _scores(muse=_cell(dict.fromkeys(SESS, 0.8), failed=("s1", "s2")),
                deepseek=_cell(dict.fromkeys(SESS, 0.8)), gemini=_cell(dict.fromkeys(SESS, 0.8)))
    u = sel.underperforming(s, "X")
    assert u["muse"]["triggers"] == ["a"] and u["deepseek"]["triggers"] == []


def test_trigger_b_needs_two_sessions():
    good = dict.fromkeys(SESS, 0.8)
    low2 = dict(good, s1=0.5, s2=0.5)
    low1 = dict(good, s1=0.5)
    s = _scores(muse=_cell(low2), deepseek=_cell(good), gemini=_cell(good), mimo=_cell(low1))
    u = sel.underperforming(s, "X")
    assert u["muse"]["triggers"] == ["b"]
    assert u["mimo"]["triggers"] == []


def test_misread_never_triggers():
    good = dict.fromkeys(SESS, 0.8)
    s = _scores(muse=_cell(good, misread=9.0), deepseek=_cell(good), gemini=_cell(good))
    assert sel.underperforming(s, "X")["muse"]["triggers"] == []


def test_best_round0_prefers_fewer_failures_then_recall():
    s = {"X|X-0901|muse": _cell(dict.fromkeys(SESS, 0.9), failed=("s1",)),
         "X|X-cur|muse": _cell(dict.fromkeys(SESS, 0.7)),
         "X|X-0917|muse": _cell(dict.fromkeys(SESS, 0.8))}
    assert sel.best_round0(s, "X", "muse") == "X|X-0917|muse"


def test_keep_edit():
    inc = _cell(dict.fromkeys(SESS, 0.5), f=0.25)
    exactly = _cell(dict(dict.fromkeys(SESS, 0.5), s1=0.75))
    assert sel.keep_edit(inc, exactly, "action_recall") is False
    more = _cell(dict(dict.fromkeys(SESS, 0.5), s1=0.76))
    assert sel.keep_edit(inc, more, "action_recall") is True
    worse = _cell(dict(dict.fromkeys(SESS, 0.5), s1=0.76, s2=0.24))
    assert sel.keep_edit(inc, worse, "action_recall") is False
    newly_failed = _cell(dict(dict.fromkeys(SESS, 0.5), s1=0.9), failed=("s3",))
    assert sel.keep_edit(inc, newly_failed, "action_recall") is False


def test_frozen_entries(tmp_path):
    cands = common.read_json(os.path.join(BENCH_DIR, "candidates.json"))
    spec = {"base": "X-cur", "why": "test", "replace": [["NEVER-PRESENT", "x"]]}
    with pytest.raises(ValueError):
        sel.frozen_entry("X", "deepseek", "X-deepseek-e1", {}, 1, cands, {"X-deepseek-e1": spec})
    with open(os.path.join(BENCH_DIR, "prompts", "X-cur.txt"), encoding="utf-8") as f:
        first_line = f.read().splitlines()[0]
    spec["replace"] = [[first_line, first_line + " (edited)"]]
    e1 = sel.frozen_entry("X", "deepseek", "X-deepseek-e1", {"m": 1}, 1, cands,
                          {"X-deepseek-e1": spec})
    e0 = sel.frozen_entry("X", "sonnet", "X-cur", {"m": 1}, 0, cands)
    path = sel.write_frozen([e0, e1], str(tmp_path / "frozen.v1.json"))
    fr = common.read_json(path)
    assert all(e["evidential"] is False for e in fr["entries"])
    assert common.sha256_text(fr["entries"][1]["prompt_text"]) == fr["entries"][1]["prompt_sha256"]
    assert fr["entries"][0]["prompt_sha256"] == cands["X-cur"]["fixed_sha256"]
    assert fr["entries"][0]["params"]["thinking"] == {"type": "adaptive"}
    assert "messages" not in fr["entries"][0]["params"]


def _index(keys):
    return {"X-cur": {k: {"sha256": "h", "task": "X", "n": 3, "chars": 100} for k in keys}}


def test_heldout_plan_charges_heldout_pool(tmp_path):
    fr = tmp_path / "frozen.json"
    common.write_json(str(fr), {"entries": [{"task": "X", "candidate": "X-cur", "model": "muse"},
                                            {"task": "X", "candidate": "X-cur",
                                             "model": "sonnet"}]})
    cells = sel.cells_for_phase("heldout", str(tmp_path), frozen_path=str(fr))
    manifest = {"s1": {"role": "set"}, "h1": {"role": "heldout"}, "h2": {"role": "heldout"},
                "R": {}}
    calls = run.plan("heldout", ["X"], _index(["s1", "h1", "h2"]), manifest, cells=cells)
    assert len(calls) == 2 * 2 * 3
    assert {c["session"] for c in calls} == {"h1", "h2"}
    assert all(c["pool"] == "report/heldout" for c in calls)


def test_rounds12_only_underperforming_pairs(tmp_path):
    out = str(tmp_path)
    common.write_json(os.path.join(out, "underperforming.json"),
                      {"X": {"deepseek": {"triggers": ["a"]}, "muse": {"triggers": []}}})
    edits = {"X-deepseek-e1": {"base": "X-cur", "replace": [], "task": "X", "model": "deepseek"},
             "X-muse-e1": {"base": "X-cur", "replace": [], "task": "X", "model": "muse"}}
    cells = sel.cells_for_phase("rounds12", out, edits=edits)
    assert cells == {("X", "X-deepseek-e1", "deepseek")}
    idx = _index(["s1"])
    idx["X-deepseek-e1"] = idx["X-cur"]
    calls = run.plan("rounds12", ["X"], idx, None, cells=cells)
    assert len(calls) == 3 and all(c["pool"] == "report/rounds12" for c in calls)


def test_add_edit_candidates(tmp_path):
    out = str(tmp_path)
    common.write_json(os.path.join(out, "prompts", "index.json"), _index(["s1"]))
    common.write_text(os.path.join(out, "prompts", "X-cur", "s1.txt"), "say EOD here")
    edits = {"X-deepseek-e1": {"base": "X-cur", "replace": [["EOD", "end of day"]],
                               "task": "X", "model": "deepseek"}}
    idx = sel.add_edit_candidates(out, edits)
    with open(os.path.join(out, "prompts", "X-deepseek-e1", "s1.txt"), encoding="utf-8") as f:
        assert f.read() == "say end of day here"
    assert idx["X-deepseek-e1"]["s1"]["sha256"] == common.sha256_text("say end of day here")


def test_heldout_adds_the_t_input_cell(tmp_path):
    fr = tmp_path / "frozen.json"
    common.write_json(str(fr), {"entries": [{"task": "T", "candidate": "T-cur",
                                             "model": "sonnet"}]})
    cells = sel.cells_for_phase("heldout", str(tmp_path), frozen_path=str(fr))
    assert ("X", "X-cur", "muse") in cells
