"""Tests for the locator re-test's scoring stages and label files (design 2026-11-12, C5/C6):
`score locator --stage tune|heldout --wording wN` over runs/locator_v2/<wording>/ and
labels/v2/<wording>/. Synthetic run files only: no AWS, no model call, no labeller. All text
is synthetic.
"""
from __future__ import annotations

import copy
import hashlib
from pathlib import Path

import pytest

from tests.unit.test_item_support_eval_locator_score import (
    FORBIDDEN, OTHER, PRIMARY, _four, _gold_for, _it, _keys_deep, _rec, _session, _turns)

from scripts.item_support_eval import label, score, store

P, F, I = score.PASS, score.FAIL, score.INSUFFICIENT
RO, PO, REP = score.REPORTED_ONLY, score.PENDING_OWNER, score.REPORTED


# =========================================================================================
# Builders: re-test run files record wording, primary_model and reasoning_effort
# =========================================================================================

def _v2(rec, wording="w1", primary=PRIMARY, effort="high"):
    return dict(rec, wording=wording, primary_model=primary, reasoning_effort=effort)


def _b1_set(n_sessions, items_per, split="fit", wording="w1", n_turns=40):
    """Re-test tune runs: B1 only."""
    sessions, runs = [], {}
    for k in range(n_sessions):
        ref = f"{split[0]}{k:03d}"
        s = _session(ref, split, n_turns)
        sessions.append(s)
        runs[ref] = {"B1": _v2(_rec(ref, "B", 1, [_it(i) for i in range(items_per)],
                                    s["turns"]), wording)}
    return sessions, runs


def _four_v2(ref, turns, items, wording="w1", **kw):
    return {k: _v2(r, wording) for k, r in _four(ref, turns, items, **kw).items()}


def _held_set(n_sessions, items_per, wording="w1", **kw):
    sessions, runs = [], {}
    for k in range(n_sessions):
        ref = f"h{k:03d}"
        s = _session(ref, "heldout")
        sessions.append(s)
        runs[ref] = _four_v2(ref, s["turns"], [_it(i) for i in range(items_per)], wording, **kw)
    return sessions, runs


def _wrong(sessions, runs, n):
    """The first n linked B1 item_refs (to be given a wrong gold turn)."""
    refs = []
    for s in sessions:
        for row in score.run_items(runs[s["ref"]]["B1"]):
            if score.item_status(row) == "linked":
                refs.append(row["item_ref"])
    return set(refs[:n])


# =========================================================================================
# The tune decision (section 4 step 2)
# =========================================================================================

@pytest.mark.parametrize("wrong,cov,want", [
    (0, 1.0, "continue"), (5, 0.60, "continue"), (6, 0.60, "ask_owner"),
    (9, 0.60, "ask_owner"), (10, 0.99, "stop"), (0, 0.5999, "stop"), (9, 0.59, "stop")])
def test_tune_decision_boundaries(wrong, cov, want):
    assert score.tune_decision(wrong, cov) == want


def test_tune_decision_waits_for_labels_and_needs_items():
    assert score.tune_decision(0, 1.0, missing_gold=1) == score.PENDING_LABELS
    assert score.tune_decision(0, None) == I


# =========================================================================================
# score_locator_tune
# =========================================================================================

def test_tune_counts_wrong_links_and_decides():
    sessions, runs = _b1_set(2, 30)
    for n, want in ((0, "continue"), (5, "continue"), (6, "ask_owner"), (9, "ask_owner"),
                    (10, "stop")):
        gold = _gold_for(sessions, runs, wrong=_wrong(sessions, runs, n))
        out = score.score_locator_tune(sessions, runs, gold=gold)
        b3 = out["bars"]["link_precision"]
        assert (b3["wrong"], b3["linked"], out["decision"]) == (n, 60, want), n
        assert b3["cp_upper"] == pytest.approx(score.cp_upper(n, 60))
        assert out["bars"]["coverage"]["coverage"] == 1.0
    assert out["stage"] == "tune" and "stability" not in out["bars"]
    assert out["admission_early"] is None


def test_tune_low_coverage_stops_and_lists_refusals():
    s = _session("f000", "fit")
    items = [_it(i) for i in range(5)] + [_it(5, "ambiguous_stamp"), _it(6, "ambiguous_stamp"),
                                         _it(7, "no_locator"), _it(8, "outside_topic"),
                                         _it(9, "not_shown")]
    runs = {"f000": {"B1": _v2(_rec("f000", "B", 1, items, s["turns"]))}}
    out = score.score_locator_tune([s], runs, gold=_gold_for([s], runs))
    assert out["bars"]["coverage"]["coverage"] == pytest.approx(0.5)
    assert out["decision"] == "stop" and out["bars"]["link_precision"]["wrong"] == 0
    assert out["refusals"] == {"ambiguous_stamp": 2, "not_shown": 1, "outside_topic": 1}
    assert out["statuses"]["no_locator"] == 1


def test_tune_missing_gold_is_pending_labels():
    sessions, runs = _b1_set(1, 10)
    gold = _gold_for(sessions, runs)
    gold.pop(next(iter(gold)))
    out = score.score_locator_tune(sessions, runs, gold=gold)
    assert out["decision"] == score.PENDING_LABELS
    assert out["bars"]["link_precision"]["missing_gold"] == 1


def test_tune_eligibility_is_b1_only_and_uses_the_recorded_primary():
    sessions, runs = _b1_set(6, 10)
    runs["f001"]["B1"]["error"] = "call_failed: synthetic"
    runs["f002"]["B1"]["model"] = None
    runs["f003"]["B1"]["model"] = OTHER                 # a fallback answered
    runs["f004"]["B1"].pop("primary_model")
    del runs["f005"]["B1"]                              # never run
    out = score.score_locator_tune(sessions, runs, gold=_gold_for(sessions[:1], runs))
    c = out["sessions"]
    assert (c["eligible"], c["errored_excluded"], c["model_unknown_excluded"],
            c["fallback_excluded"], c["primary_unknown_excluded"], c["missing_excluded"]) \
        == (1, 1, 1, 1, 1, 1)
    assert out["bars"]["link_precision"]["linked"] == 10
    assert c["reasoning_effort"] == ["high"]
    # The recorded primary decides, not a mode over runs: every B1 answered by OTHER whose
    # file records OTHER as the deployed primary is eligible.
    sessions, runs = _b1_set(2, 5)
    for four in runs.values():
        four["B1"].update(model=OTHER, primary_model=OTHER)
    assert score.score_locator_tune(sessions, runs, gold={})["sessions"]["eligible"] == 2


def test_tune_twin_flags_are_reported():
    s = _session("f000", "fit")
    s["turns"][0]["text"] = "fix the scaffold on level 3"
    items = [_it(0, text="fix the scaffold on level 2")] + [_it(i) for i in range(1, 5)]
    runs = {"f000": {"B1": _v2(_rec("f000", "B", 1, items, s["turns"]))}}
    gold = _gold_for([s], runs)
    out = score.score_locator_tune([s], runs, gold=gold)
    assert out["bars"]["twin_hard_negatives"]["flags"] == 1
    assert out["bars"]["twin_hard_negatives"]["verdict"] == I
    out = score.score_locator_tune([s], runs, gold=gold,
                                   flag_decisions={"f000:0:action_items:0|0": True})
    assert out["bars"]["twin_hard_negatives"]["verdict"] == F


def test_status_suggestion_at_diagnostic():
    s = _session("f000", "fit")
    rec = _v2(_rec("f000", "B", 1, [_it(i) for i in range(4)], s["turns"]))
    raw = rec["_raw_said_at"]
    raw[0]["status_at"] = raw[0]["said_at"]           # copied into said_at
    raw[1]["status_at"] = "23:59:59"                  # a different stamp
    raw[2]["status_at"] = None                        # a suggestion without a stamp
    out = score.score_locator_tune([s], {"f000": {"B1": rec}},
                                   gold=_gold_for([s], {"f000": {"B1": rec}}))
    d = out["status_suggestion_at"]
    assert (d["links"], d["with_status_at"], d["equal"]) == (4, 2, 1)
    assert d["share_of_links"] == pytest.approx(0.25)
    assert d["share_of_with_status_at"] == pytest.approx(0.5)
    assert d["verdict"] == REP


def test_status_at_falls_back_to_a_surviving_suggestion():
    s = _session("f000", "fit")
    rec = _v2(_rec("f000", "B", 1, [_it(0)], s["turns"]))
    child = rec["topics"][0]["action_items"][0]
    child["status_suggestion"] = {"status": "done", "at": rec["_raw_said_at"][0]["said_at"]}
    row = score.run_items(rec)[0]
    assert row["status_at"] == child["status_suggestion"]["at"]


def test_admission_early_reads_first_test_a_runs_and_labels_the_effort():
    sessions, runs = _b1_set(2, 10)
    a_runs = {s["ref"]: {k: v for k, v in _four(s["ref"], s["turns"],
                                                 [_it(i) for i in range(10)]).items()
                         if k in ("A1", "A2")} for s in sessions}
    out = score.score_locator_tune(sessions, runs, gold=_gold_for(sessions, runs),
                                   a_runs=a_runs)
    ae = out["admission_early"]
    assert ae["sessions"] == 2 and ae["verdict"] == REP
    assert ae["share"]["A1_B1"] == 1.0 and ae["share"]["A1_A2_first_test"] == 1.0
    assert ae["item_delta"] == 0.0
    assert ae["effort"]["B1"] == ["high"]
    assert "provider default" in ae["effort"]["A"]
    assert "different reasoning effort" in ae["effort"]["note"]
    # B1 drops 3 items in one session: the share and the delta move.
    runs["f000"]["B1"] = _v2(_rec("f000", "B", 1, [_it(i) for i in range(7)],
                                  sessions[0]["turns"]))
    ae = score.score_locator_tune(sessions, runs, gold=_gold_for(sessions, runs),
                                  a_runs=a_runs)["admission_early"]
    assert ae["matched"]["A1_B1"] == 17
    assert ae["item_delta"] == pytest.approx((17 / 2 - 10) / 10)
    # A session whose A1 errored is left out.
    a_runs["f001"]["A1"]["error"] = "x"
    assert score.score_locator_tune(sessions, runs, gold={}, a_runs=a_runs)[
        "admission_early"]["sessions"] == 1


# =========================================================================================
# score_locator_heldout
# =========================================================================================

def _owner_all(gold, n=5):
    todo = [{"item_ref": r} for r in list(gold)[:n]]
    return todo, {t["item_ref"]: gold[t["item_ref"]] for t in todo}


def test_heldout_passes_with_owner_and_reports_bar6_only():
    sessions, runs = _held_set(3, 25)
    gold = _gold_for(sessions, runs)
    todo, owner = _owner_all(gold)
    out = score.score_locator_heldout(sessions, runs, gold=gold, owner_gold=owner,
                                      owner_todo=todo)
    b = out["bars"]
    assert out["verdict"] == P
    for k in ("admission", "link_precision", "twin_hard_negatives", "coverage", "stability",
              "agent_owner_agreement"):
        assert b[k]["verdict"] == P, k
    assert b["inferred_precision"]["verdict"] == RO
    assert b["stability_several_aware"]["share"] == 1.0
    assert out["sessions"]["eligible"] == 3 and out["sessions"]["primary_models"] == [PRIMARY]
    out = score.score_locator_heldout(sessions, runs, gold=gold, owner_todo=todo)
    assert out["verdict"] == PO


def test_heldout_bar6_never_gates_even_with_29_no_locator_items():
    s = _session("h000", "heldout")
    items = [_it(i) for i in range(70)] + [_it(70 + i, "no_locator") for i in range(29)]
    s["turns"] = _turns(100)
    runs = {"h000": _four_v2("h000", s["turns"], items)}
    gold = _gold_for([s], runs)
    for r in list(gold)[70:73]:
        gold[r] = {"label": "turn", "turns": [0]}      # no_locator items a turn supports
    out = score.score_locator_heldout([s], runs, gold=gold)
    b6 = out["bars"]["inferred_precision"]
    assert b6["no_locator"] == 29 and b6["computed_verdict"] == F
    assert b6["verdict"] == RO and b6["inferred_on"] is False


def test_heldout_bar7_strict_gate_with_several_aware_report():
    s = _session("h000", "heldout")
    b1 = [_it(i) for i in range(10)]
    b2 = [_it(i) for i in range(8)] + [_it(8, turn=21), _it(9, turn=20)]
    runs = {"h000": _four_v2("h000", s["turns"], b1, b2_items=b2)}
    gold = _gold_for([s], runs)
    refs = [r["item_ref"] for r in score.run_items(runs["h000"]["B1"])]
    gold[refs[8]] = {"label": "several", "turns": [8, 21]}
    out = score.score_locator_heldout([s], runs, gold=gold)
    strict, aware = out["bars"]["stability"], out["bars"]["stability_several_aware"]
    assert (strict["pairs"], strict["same_turn"], strict["verdict"]) == (10, 8, F)
    assert (aware["pairs"], aware["same_or_both_in_several"]) == (10, 9)
    assert aware["share"] == pytest.approx(0.9) and aware["verdict"] == REP
    assert out["verdict"] == F


def test_heldout_excludes_a_session_by_its_recorded_primary():
    sessions, runs = _held_set(2, 10)
    runs["h001"]["B2"]["model"] = OTHER
    out = score.score_locator_heldout(sessions, runs, gold=_gold_for(sessions, runs))
    assert out["sessions"]["eligible"] == 1 and out["sessions"]["fallback_excluded"] == 1


def test_heldout_without_eligible_sessions_is_insufficient():
    sessions, runs = _held_set(1, 10)
    runs["h000"]["A2"]["error"] = "x"
    out = score.score_locator_heldout(sessions, runs, gold={})
    assert out["verdict"] == I


def test_heldout_status_at_and_side_set():
    sessions, runs = _held_set(1, 4)
    runs["h000"]["B1"]["_raw_said_at"][0]["status_at"] = \
        runs["h000"]["B1"]["_raw_said_at"][0]["said_at"]
    side = [{"arm": "A", "rep": 1, "status_suggestions": {"offered": 2, "kept": 2}},
            {"arm": "B", "rep": 1, "status_suggestions": {"offered": 2, "kept": 1}}]
    out = score.score_locator_heldout(sessions, runs, gold=_gold_for(sessions, runs),
                                      side_runs=side)
    assert out["status_suggestion_at"]["equal"] == 1
    assert out["side_set"]["kept_drops_under_b"] is True


# =========================================================================================
# Files: namespaces, labels/v2, provenance
# =========================================================================================

def _write_v2(tmp_path, sessions, runs, wording="w1", old_runs=None):
    root = tmp_path / "item_support_eval"
    data, runs_dir, labels = root / "data", root / "runs", root / "labels"
    manifest = []
    for s in sessions:
        rec = dict(s, turns=[store.encode_turn(t) for t in s["turns"]])
        store.write_json(Path(data, "sessions", f"{s['ref']}.json"), rec)
        manifest.append({"ref": s["ref"], "split": s["split"]})
    store.write_json(Path(data, "manifest.json"), {"sessions": manifest})
    digest = store.manifest_digest(data)
    for ref, four in runs.items():
        for key, rec in four.items():
            store.write_json(store.locator_run_path(key[0], int(key[1]), ref,
                                                    runs_dir=runs_dir, wording=wording),
                             dict(rec, manifest_digest=digest))
    for ref, four in (old_runs or {}).items():
        for key, rec in four.items():
            store.write_json(store.locator_run_path(key[0], int(key[1]), ref,
                                                    runs_dir=runs_dir),
                             dict(rec, manifest_digest=digest))
    return data, runs_dir, labels


def _answer(labels_v2, gold):
    store.write_jsonl(Path(labels_v2, "done.locator.agent.jsonl"),
                      [{"item_ref": k, **v} for k, v in gold.items()])


def test_label_export_v2_writes_under_labels_v2_wording(tmp_path):
    sessions, runs = _b1_set(2, 4)
    data, runs_dir, labels = _write_v2(tmp_path, sessions, runs)
    out = label.main(["locator-export", "--wording", "w1", "--data", str(data), "--runs",
                      str(runs_dir), "--labels", str(labels)])
    assert out["agent"] == 8
    v2 = Path(labels, "v2", "w1", "todo.locator.agent.jsonl")
    rows = store.read_jsonl(v2)
    assert len(rows) == 8
    for r in rows:
        assert not FORBIDDEN & set(_keys_deep(r))
        assert r["run_sha"] == score.file_sha(store.locator_run_path(
            "B", 1, r["item_ref"].split(":")[0], runs_dir=runs_dir, wording="w1"))
    assert not Path(labels, "todo.locator.agent.jsonl").exists()
    assert not Path(labels, "v2", "w2").exists()


def test_blind_label_instruction_is_unchanged():
    assert hashlib.sha256(label.BLIND_LABEL_INSTRUCTION.encode("utf-8")).hexdigest() \
        == "3b8598f3a6550019d2d5a2bd6c21c96dda868127cebf9ce8336da7c61030f77b"


def test_score_tune_from_files_writes_the_v2_summary_only(tmp_path, capsys):
    sessions, runs = _b1_set(2, 30)
    a_old = {s["ref"]: {k: v for k, v in _four(s["ref"], s["turns"],
                                                [_it(i) for i in range(30)]).items()
                        if k in ("A1", "A2")} for s in sessions}
    data, runs_dir, labels = _write_v2(tmp_path, sessions, runs, old_runs=a_old)
    label.export_locator(runs_dir, data_dir=data, labels_dir=labels, wording="w1")
    gold = _gold_for(sessions, runs, wrong=_wrong(sessions, runs, 6))
    _answer(Path(labels, "v2", "w1"), gold)
    out = score.main(["locator", "--stage", "tune", "--wording", "w1", "--data", str(data),
                      "--runs", str(runs_dir), "--labels", str(labels)])
    assert out["decision"] == "ask_owner" and out["wording"] == "w1"
    assert out["admission_early"]["sessions"] == 2
    path = Path(runs_dir, "locator_v2", "w1", "summary.tune.json")
    assert path.exists()
    assert not Path(runs_dir, "locator", "summary.tune.json").exists()
    text = path.read_text(encoding="utf-8") + capsys.readouterr().out
    assert "zeta" not in text and "f000" not in text


def test_score_heldout_from_files(tmp_path):
    sessions, runs = _held_set(2, 30, wording="w2")
    data, runs_dir, labels = _write_v2(tmp_path, sessions, runs, wording="w2")
    label.export_locator(runs_dir, data_dir=data, labels_dir=labels, wording="w2")
    owner = label.export_locator_owner(runs_dir, data_dir=data, labels_dir=labels,
                                       wording="w2")
    assert owner["owner"] == 40
    v2 = Path(labels, "v2", "w2")
    gold = _gold_for(sessions, runs)
    _answer(v2, gold)
    out = score.locator_summary("heldout", data_dir=data, runs_dir=runs_dir,
                                labels_dir=labels, wording="w2")
    assert out["verdict"] == PO and out["bars"]["link_precision"]["verdict"] == P
    todo = store.read_jsonl(v2 / "todo.owner.locator.jsonl")
    store.write_jsonl(v2 / "done.owner.locator.jsonl",
                      [{"item_ref": t["item_ref"], **gold[t["item_ref"]]} for t in todo])
    out = score.locator_summary("heldout", data_dir=data, runs_dir=runs_dir,
                                labels_dir=labels, wording="w2")
    assert out["bars"]["agent_owner_agreement"]["verdict"] == P
    assert Path(runs_dir, "locator_v2", "w2", "summary.heldout.json").exists()


def test_owner_sample_v2_uses_recorded_eligibility(tmp_path):
    sessions, runs = _held_set(2, 30)
    runs["h001"]["A1"]["model"] = OTHER               # a fallback: excluded from the sample
    data, runs_dir, labels = _write_v2(tmp_path, sessions, runs)
    label.export_locator_owner(runs_dir, data_dir=data, labels_dir=labels, wording="w1")
    rows = store.read_jsonl(Path(labels, "v2", "w1", "todo.owner.locator.jsonl"))
    assert rows and {r["item_ref"].split(":")[0] for r in rows} == {"h000"}


def test_locator_flags_v2_waits_for_the_v2_todo(tmp_path):
    s = _session("f000", "fit")
    s["turns"][0]["text"] = "fix the scaffold on level 3"
    items = [_it(0, text="fix the scaffold on level 2")] + [_it(i) for i in range(1, 5)]
    runs = {"f000": {"B1": _v2(_rec("f000", "B", 1, items, s["turns"]))}}
    data, runs_dir, labels = _write_v2(tmp_path, [s], runs)
    with pytest.raises(SystemExit, match="not exported"):
        label.export_locator_flags(runs_dir, data_dir=data, labels_dir=labels, wording="w1")
    label.export_locator(runs_dir, data_dir=data, labels_dir=labels, wording="w1")
    _answer(Path(labels, "v2", "w1"), _gold_for([s], runs))
    out = label.export_locator_flags(runs_dir, data_dir=data, labels_dir=labels, wording="w1")
    assert out["flags"] == 1
    assert Path(labels, "v2", "w1", "flags.owner.locator.jsonl").exists()
    assert not Path(labels, "flags.owner.locator.jsonl").exists()


def test_stage_and_wording_must_agree(tmp_path):
    sessions, runs = _b1_set(1, 5)
    data, runs_dir, labels = _write_v2(tmp_path, sessions, runs)
    with pytest.raises(SystemExit, match="needs --wording"):
        score.locator_summary("tune", data_dir=data, runs_dir=runs_dir, labels_dir=labels)
    with pytest.raises(SystemExit, match="first test"):
        score.locator_summary("fit", data_dir=data, runs_dir=runs_dir, labels_dir=labels,
                              wording="w1")


def test_a_run_recording_another_wording_is_refused(tmp_path):
    sessions, runs = _b1_set(1, 5)
    runs["f000"]["B1"]["wording"] = "w2"
    data, runs_dir, labels = _write_v2(tmp_path, sessions, runs, wording="w1")
    with pytest.raises(SystemExit, match="records wording"):
        score.locator_summary("tune", data_dir=data, runs_dir=runs_dir, labels_dir=labels,
                              wording="w1")


def test_v2_labels_on_another_b1_run_are_refused(tmp_path):
    sessions, runs = _b1_set(1, 5)
    data, runs_dir, labels = _write_v2(tmp_path, sessions, runs)
    label.export_locator(runs_dir, data_dir=data, labels_dir=labels, wording="w1")
    p = store.locator_run_path("B", 1, "f000", runs_dir=runs_dir, wording="w1")
    rec = store.read_json(p)
    rec["latency_s"] = 99.0
    store.write_json(p, rec)
    with pytest.raises(SystemExit, match="B1 run"):
        score.locator_summary("tune", data_dir=data, runs_dir=runs_dir, labels_dir=labels,
                              wording="w1")


def test_first_test_stages_are_unchanged_by_a_v2_namespace(tmp_path):
    """A fit summary over runs/locator/ is identical with and without locator_v2 beside it."""
    fit_s, fit_r = [], {}
    for k in range(2):
        s = _session(f"f{k:03d}", "fit")
        fit_s.append(s)
        fit_r[s["ref"]] = _four(s["ref"], s["turns"], [_it(i) for i in range(30)])
    data, runs_dir, labels = _write_v2(tmp_path, fit_s, {}, old_runs=fit_r)
    label.export_locator(runs_dir, data_dir=data, labels_dir=labels)
    store.write_jsonl(Path(labels, "done.locator.agent.jsonl"),
                      [{"item_ref": k, **v} for k, v in _gold_for(fit_s, fit_r).items()])
    before = score.locator_summary("fit", data_dir=data, runs_dir=runs_dir, labels_dir=labels)
    v2 = {ref: {"B1": _v2(copy.deepcopy(four["B1"]))} for ref, four in fit_r.items()}
    digest = store.manifest_digest(data)
    for ref, four in v2.items():
        store.write_json(store.locator_run_path("B", 1, ref, runs_dir=runs_dir, wording="w1"),
                         dict(four["B1"], manifest_digest=digest, latency_s=1.0))
    after = score.locator_summary("fit", data_dir=data, runs_dir=runs_dir, labels_dir=labels)
    assert before == after and before["fit_gate"]["verdict"] == P
