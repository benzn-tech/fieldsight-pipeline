"""Tests for scripts/continuity_eval/score.py (Task 11): every bar in spec S7's "Metrics and
bars" table is its own function, each tested at its exact pass/fail/insufficient boundary
with tiny hand-built fixtures -- no run directory, no gold file, no AWS, per Task 11's brief
Step 1. A smaller set of glue tests (`scored_assignments`/`build_summary`) exercises the real
run-directory wiring on a hand-built directory in the same shape `run.py` writes.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import carry_forward

from scripts.continuity_eval import label, score


# =========================================================================================
# is_claim_correct -- the rule behind bars 1 and 2 (merge/split)
# =========================================================================================

def test_is_claim_correct_plain_match():
    assert score.is_claim_correct("A1", {"A1"}) is True
    assert score.is_claim_correct("A2", {"A1"}) is False


def test_is_claim_correct_merge_any_of_several_gold_counterparts_is_correct():
    # A new item gold recognises as a MERGE of two prior items: either alias is correct.
    gold_counterparts = {"A1", "A2"}
    assert score.is_claim_correct("A1", gold_counterparts) is True
    assert score.is_claim_correct("A2", gold_counterparts) is True
    assert score.is_claim_correct("A3", gold_counterparts) is False


def test_is_claim_correct_split_both_halves_score_correct_against_the_same_prior_alias():
    # A prior item SPLIT into two new items: each new item's gold counterpart set is
    # independently {"A1"} -- no special-case code needed, just two separate memberships.
    gold_for_half_one = {"A1"}
    gold_for_half_two = {"A1"}
    assert score.is_claim_correct("A1", gold_for_half_one) is True
    assert score.is_claim_correct("A1", gold_for_half_two) is True


# =========================================================================================
# Bar: wrong carries (0 of N -> pass; 1 -> fail)
# =========================================================================================

def test_wrong_carries_zero_of_n_passes():
    rows = [{"session": "s1", "correct": True}] * 5
    result = score.score_wrong_carries(rows)
    assert result == {"accepted": 5, "wrong": 0,
                       "by_session": {"s1": {"accepted": 5, "wrong": 0}}, "verdict": "pass"}


def test_wrong_carries_one_wrong_fails():
    rows = [{"session": "s1", "correct": True}, {"session": "s1", "correct": False}]
    result = score.score_wrong_carries(rows)
    assert result["wrong"] == 1
    assert result["verdict"] == "fail"


def test_wrong_carries_no_accepted_claims_is_insufficient():
    assert score.score_wrong_carries([])["verdict"] == "insufficient"


def test_wrong_carries_reported_per_session_not_pooled():
    rows = [{"session": "s1", "correct": False}, {"session": "s2", "correct": True}]
    result = score.score_wrong_carries(rows)
    assert result["by_session"]["s1"] == {"accepted": 1, "wrong": 1}
    assert result["by_session"]["s2"] == {"accepted": 1, "wrong": 0}
    assert result["verdict"] == "fail"          # any wrong carry anywhere fails the bar


# =========================================================================================
# Bar: hard-negative wrong carries (separate reporting, minimum-30 insufficient rule)
# =========================================================================================

def test_hard_negative_below_30_pairs_is_insufficient_even_with_zero_wrong():
    rows = [{"session": "s1", "wrongly_carried": False}] * 29
    assert score.score_hard_negative_wrong_carries(rows)["verdict"] == "insufficient"


def test_hard_negative_exactly_30_pairs_zero_wrong_passes():
    rows = [{"session": "s1", "wrongly_carried": False}] * 30
    result = score.score_hard_negative_wrong_carries(rows)
    assert result["pairs"] == 30
    assert result["verdict"] == "pass"


def test_hard_negative_30_pairs_one_wrong_fails():
    rows = [{"session": "s1", "wrongly_carried": False}] * 29 \
        + [{"session": "s1", "wrongly_carried": True}]
    result = score.score_hard_negative_wrong_carries(rows)
    assert result["wrong"] == 1
    assert result["verdict"] == "fail"


def test_hard_negative_reported_separately_from_the_general_wrong_carries_bar():
    # The same session's data feeding both bars independently -- hard-negative wrong carries
    # must not be folded into (or diluted by) the general accepted-claims population.
    general = [{"session": "s1", "correct": False}]     # 1 wrong overall -> fail
    hard_neg = [{"session": "s1", "wrongly_carried": False}] * 30   # but clean hard negatives
    assert score.score_wrong_carries(general)["verdict"] == "fail"
    assert score.score_hard_negative_wrong_carries(hard_neg)["verdict"] == "pass"


# =========================================================================================
# Bar: carry recall vs text-only recall
# =========================================================================================

def test_text_only_recall_uses_the_real_carry_forward_match():
    prior = [{"id": "p1", "text": "Fix the leaking pump", "human_touched": False}]
    new = [{"id": "n1", "text": "Fix the leaking pump", "human_touched": False}]
    gold_pairs = [("p1", "n1")]
    # Sanity: carry_forward.match itself pairs these (exact normalised text).
    pairs, _orphans = carry_forward.match(prior, new)
    assert pairs == [("p1", "n1", "exact")]
    assert score.text_only_recall(prior, new, gold_pairs) == 1.0


def test_text_only_recall_misses_a_reworded_pair_below_the_fuzzy_floor():
    prior = [{"id": "p1", "text": "Fix the leaking pump in the plant room", "human_touched": False}]
    new = [{"id": "n1", "text": "Pump plant room fixed", "human_touched": False}]
    gold_pairs = [("p1", "n1")]
    assert score.text_only_recall(prior, new, gold_pairs) == 0.0


def test_text_only_recall_none_with_no_gold_positive_pairs():
    assert score.text_only_recall([], [], []) is None


def test_carry_recall_from_scored_assignments():
    rows = [{"gold_positive": True, "carried": True},
            {"gold_positive": True, "carried": False},
            {"gold_positive": False, "carried": False}]     # negatives don't count
    assert score.carry_recall(rows) == 0.5


def test_carry_recall_none_with_no_gold_positives():
    assert score.carry_recall([{"gold_positive": False, "carried": False}]) is None


def test_score_carry_recall_bar_at_exactly_80_percent_passes():
    result = score.score_carry_recall(0.80, text_only_recall_value=0.5)
    assert result["verdict"] == "pass"
    assert result["text_only_recall"] == 0.5


def test_score_carry_recall_bar_just_below_80_percent_fails():
    result = score.score_carry_recall(0.799, text_only_recall_value=0.5)
    assert result["verdict"] == "fail"


def test_score_carry_recall_insufficient_with_no_gold_positive_pairs():
    result = score.score_carry_recall(None, None)
    assert result["verdict"] == "insufficient"


# =========================================================================================
# Bar: admission drift (tolerance test, pass at exactly equal, fail above)
# =========================================================================================

def test_admission_drift_passes_at_exactly_equal():
    metric = {"s1": 1.0, "s2": 3.0}       # mean 2.0
    tolerance = {"s1": 2.0, "s2": 2.0}    # mean 2.0
    result = score.score_admission_drift(metric, tolerance)
    assert result["metric"] == result["tolerance"] == 2.0
    assert result["verdict"] == "pass"


def test_admission_drift_fails_above_tolerance():
    metric = {"s1": 2.1}
    tolerance = {"s1": 2.0}
    assert score.score_admission_drift(metric, tolerance)["verdict"] == "fail"


def test_admission_drift_passes_below_tolerance():
    metric = {"s1": 1.9}
    tolerance = {"s1": 2.0}
    assert score.score_admission_drift(metric, tolerance)["verdict"] == "pass"


def test_admission_drift_insufficient_with_no_sessions():
    assert score.score_admission_drift({}, {})["verdict"] == "insufficient"


# =========================================================================================
# Bar: transcript support (block-only unsupported share <= baseline-only + 5pp)
# =========================================================================================

def test_transcript_support_passes_at_exactly_5pp_over():
    # baseline-only: 10% unsupported; block-only: 15% unsupported -- exactly +5pp.
    result = score.score_transcript_support(
        block_only_unsupported=15, block_only_total=100,
        baseline_only_unsupported=10, baseline_only_total=100)
    assert result["block_only_share"] == 0.15
    assert result["baseline_only_share"] == 0.10
    assert result["verdict"] == "pass"


def test_transcript_support_fails_just_over_5pp():
    result = score.score_transcript_support(
        block_only_unsupported=16, block_only_total=100,
        baseline_only_unsupported=10, baseline_only_total=100)
    assert result["verdict"] == "fail"


def test_transcript_support_insufficient_with_no_baseline_only_items():
    result = score.score_transcript_support(
        block_only_unsupported=1, block_only_total=10,
        baseline_only_unsupported=0, baseline_only_total=0)
    assert result["verdict"] == "insufficient"


# =========================================================================================
# Bar: added final-pass latency (p90 <= 20s at n >= 30; below 30 is insufficient)
# =========================================================================================

def test_latency_below_30_samples_is_insufficient_regardless_of_the_numbers():
    fast = [1] * 29
    assert score.score_latency_p90(fast)["verdict"] == "insufficient"


def test_latency_p90_at_exactly_20s_with_30_samples_passes():
    # Nearest-rank p90 of 30 samples is the 27th-smallest (rank = ceil(0.9*30) = 27, 1-indexed
    # = index 26). 26 values below the bar, then the bar itself starting at that rank.
    latencies = [1000] * 26 + [20_000] * 4
    result = score.score_latency_p90(latencies)
    assert result["n"] == 30
    assert result["p90_ms"] == 20_000
    assert result["verdict"] == "pass"


def test_latency_p90_just_over_20s_fails():
    latencies = [1000] * 26 + [20_001] * 4
    result = score.score_latency_p90(latencies)
    assert result["p90_ms"] == 20_001
    assert result["verdict"] == "fail"


# =========================================================================================
# Bar: agent-owner agreement (below 90% -> agent labels not usable alone)
# =========================================================================================

def test_agreement_at_exactly_90_percent_is_usable_alone():
    pairs = [({"A1"}, {"A1"})] * 90 + [({"A1"}, set())] * 10
    result = score.score_agent_owner_agreement(pairs)
    assert result["agreement"] == 0.90
    assert result["usable_alone"] is True
    assert result["verdict"] == "pass"
    assert "note" not in result


def test_agreement_just_below_90_percent_is_not_usable_alone():
    pairs = [({"A1"}, {"A1"})] * 89 + [({"A1"}, set())] * 11
    result = score.score_agent_owner_agreement(pairs)
    assert result["agreement"] == 0.89
    assert result["usable_alone"] is False
    assert result["verdict"] == "fail"
    assert result["note"] == "agent labels are not usable alone"


def test_agreement_no_sample_is_insufficient():
    assert score.score_agent_owner_agreement([])["verdict"] == "insufficient"


def test_agreement_counts_a_none_answer_as_equal_only_to_another_none():
    pairs = [(set(), set())]
    assert score.score_agent_owner_agreement(pairs)["agreement"] == 1.0


# =========================================================================================
# overall_verdict
# =========================================================================================

def test_overall_verdict_all_pass_is_pass():
    assert score.overall_verdict(["pass", "pass", "pass"]) == "pass"


def test_overall_verdict_any_fail_beats_insufficient():
    assert score.overall_verdict(["pass", "insufficient", "fail"]) == "fail"


def test_overall_verdict_insufficient_with_no_fail():
    assert score.overall_verdict(["pass", "insufficient", "pass"]) == "insufficient"


# =========================================================================================
# Reported, not gated
# =========================================================================================

def test_label_rate_none_with_no_gold_positives():
    assert score.label_rate([]) is None


def test_label_rate_counts_any_claim_accepted_or_rejected():
    rows = [{"gold_positive": True, "claim_alias": "A1"},
            {"gold_positive": True, "claim_alias": None},
            {"gold_positive": False, "claim_alias": None}]
    assert score.label_rate(rows) == 0.5


def test_pre_guard_claim_precision_counts_every_claim_not_just_accepted():
    rows = [{"correct": True}, {"correct": False}, {"correct": True}]
    assert score.pre_guard_claim_precision(rows) == 2 / 3


def test_false_rejects_per_guard_groups_by_guard_name():
    rows = [{"guard": "echo", "correct": True}, {"guard": "echo", "correct": False},
            {"guard": "kind", "correct": True}, {"guard": None, "correct": True}]
    assert score.false_rejects_per_guard(rows) == {"echo": 1, "kind": 1}


def test_hallucinated_alias_rate():
    claims = [{"guard": "existence"}, {"guard": "existence"}, {"guard": "echo"}, {"guard": None}]
    assert score.hallucinated_alias_rate(claims) == 0.5


def test_cap_hits_counts_cells_at_or_over_the_cap():
    rows = [{"session": "s1", "list_name": "findings", "count": 40},
            {"session": "s1", "list_name": "action_items", "count": 39}]
    assert score.cap_hits(rows, cap=40) == 1


def test_added_prompt_size_is_chars_and_an_estimate():
    result = score.added_prompt_size("x" * 400)
    assert result["chars"] == 400
    assert result["estimated_tokens"] == 100
    assert "estimate" in result["note"]


# =========================================================================================
# Glue: real run-directory shape -> scored_assignments -> build_summary
# =========================================================================================

def _child(text, item_id=None):
    d = {"action": text}
    if item_id is not None:
        d["item_id"] = item_id
    return d


def _write_step(run_dir, shape, arm, rep, session, step_idx, *, topics, claims=None, void=False):
    d = Path(run_dir) / "runs" / shape / arm / str(rep) / session
    d.mkdir(parents=True, exist_ok=True)
    record = {"prompt_has_block": arm == "with_block" and step_idx > 0, "prior_count": 0,
              "extraction": {"topics": topics}, "claims": claims or [], "void": void,
              "error": None, "latency_ms": 1000, "n_segments": 3, "frac": 1.0}
    (d / f"{step_idx}.json").write_text(json.dumps(record), encoding="utf-8")


def test_scored_assignments_joins_run_files_with_gold_and_scores_the_claim(tmp_path):
    prior_id = str(uuid.uuid4())
    session = "user__2026-01-01__base"
    claim = {"alias": "A1", "prior_item_id": prior_id, "new_item_id": prior_id,
              "outcome": "accepted", "guard": None, "list_name": "action_items"}
    _write_step(tmp_path, "a", "with_block", 1, session, 0,
                topics=[{"action_items": [_child("Fix the pump", prior_id)]}])
    _write_step(tmp_path, "a", "with_block", 1, session, 1,
                topics=[{"action_items": [_child("Pump fixed today", prior_id)]}],
                claims=[claim])
    [assignment] = list(label.iter_assignments(tmp_path))
    gold_done = tmp_path / "gold.jsonl"
    label._write_jsonl(gold_done, [
        {"assignment_id": assignment["assignment_id"], "counterpart": "A1",
         "supported": True, "labeller": "owner"}])
    [row] = score.scored_assignments(tmp_path, gold_done)
    assert row["gold_positive"] is True
    assert row["carried"] is True                # item_id already equals the gold counterpart
    assert row["claim_outcome"] == "accepted"
    assert score.is_claim_correct(row["claim_alias"], row["gold_counterparts"]) is True


def test_scored_assignments_marks_a_wrong_carry(tmp_path):
    # Model claims "A1" but gold says this new item is actually unrelated ("none").
    prior_id = str(uuid.uuid4())
    session = "user__2026-01-01__base"
    claim = {"alias": "A1", "prior_item_id": prior_id, "new_item_id": prior_id,
              "outcome": "accepted", "guard": None, "list_name": "action_items"}
    _write_step(tmp_path, "a", "with_block", 1, session, 0,
                topics=[{"action_items": [_child("Fix the pump", prior_id)]}])
    _write_step(tmp_path, "a", "with_block", 1, session, 1,
                topics=[{"action_items": [_child("Unrelated new job", prior_id)]}],
                claims=[claim])
    [assignment] = list(label.iter_assignments(tmp_path))
    gold_done = tmp_path / "gold.jsonl"
    label._write_jsonl(gold_done, [
        {"assignment_id": assignment["assignment_id"], "counterpart": "none",
         "supported": True, "labeller": "owner"}])
    [row] = score.scored_assignments(tmp_path, gold_done)
    accepted = score.accepted_claims_rows([row])
    assert accepted == [{"session": session, "correct": False}]
    assert score.score_wrong_carries(accepted)["verdict"] == "fail"


def test_build_summary_writes_a_verdict_and_one_entry_per_bar(tmp_path):
    tmp_path = tmp_path / "continuity_eval_runs"  # build_summary refuses to write elsewhere
    prior_id = str(uuid.uuid4())
    session = "user__2026-01-01__base"
    claim = {"alias": "A1", "prior_item_id": prior_id, "new_item_id": prior_id,
              "outcome": "accepted", "guard": None, "list_name": "action_items"}
    _write_step(tmp_path, "a", "with_block", 1, session, 0,
                topics=[{"action_items": [_child("Fix the pump", prior_id)]}])
    _write_step(tmp_path, "a", "with_block", 1, session, 1,
                topics=[{"action_items": [_child("Pump fixed today", prior_id)]}],
                claims=[claim])
    [assignment] = list(label.iter_assignments(tmp_path))
    gold_done = tmp_path / "gold.jsonl"
    label._write_jsonl(gold_done, [
        {"assignment_id": assignment["assignment_id"], "counterpart": "A1",
         "supported": True, "labeller": "owner"}])
    out_path = tmp_path / "results" / "summary.json"
    summary = score.build_summary(tmp_path, gold_done, out_path)
    assert out_path.exists()
    on_disk = json.loads(out_path.read_text(encoding="utf-8"))
    assert on_disk == summary
    assert summary["verdict"] in ("pass", "fail", "insufficient")
    for bar_name in ("wrong_carries", "hard_negative_wrong_carries", "carry_recall",
                      "admission_drift", "transcript_support", "latency_p90",
                      "agent_owner_agreement"):
        assert "verdict" in summary["bars"][bar_name]
    assert summary["bars"]["wrong_carries"]["verdict"] == "pass"     # the one claim was correct


def _finding(text, item_id=None):
    d = {"observation": text}
    if item_id is not None:
        d["item_id"] = item_id
    return d


# =========================================================================================
# Fix round 1: no names in summary.json (Critical 1)
# =========================================================================================

def test_session_refs_are_stable_sorted_opaque_ids():
    refs = score.session_refs(["zzz", "aaa", "mmm"])
    assert refs == {"aaa": "s01", "mmm": "s02", "zzz": "s03"}


def test_summary_json_contains_no_raw_session_id_or_folder_name(tmp_path):
    tmp_path = tmp_path / "continuity_eval_runs"  # build_summary refuses to write elsewhere
    prior_id = str(uuid.uuid4())
    session = "workername__2026-01-01__base"      # user_folder ("workername") is a person's name
    claim = {"alias": "A1", "prior_item_id": prior_id, "new_item_id": prior_id,
              "outcome": "accepted", "guard": None, "list_name": "action_items"}
    _write_step(tmp_path, "a", "with_block", 1, session, 0,
                topics=[{"action_items": [_child("Fix the pump", prior_id)]}])
    _write_step(tmp_path, "a", "with_block", 1, session, 1,
                topics=[{"action_items": [_child("Pump fixed today", prior_id)]}],
                claims=[claim])
    [assignment] = list(label.iter_assignments(tmp_path))
    gold_done = tmp_path / "gold.jsonl"
    label._write_jsonl(gold_done, [
        {"assignment_id": assignment["assignment_id"], "counterpart": "A1",
         "supported": True, "labeller": "owner"}])
    out_path = tmp_path / "results" / "summary.json"
    score.build_summary(tmp_path, gold_done, out_path)
    raw_text = out_path.read_text(encoding="utf-8")
    assert "workername" not in raw_text
    assert session not in raw_text


def test_build_summary_writes_the_id_to_ref_map_only_into_the_run_dir(tmp_path):
    tmp_path = tmp_path / "continuity_eval_runs"  # build_summary refuses to write elsewhere
    prior_id = str(uuid.uuid4())
    session = "user__2026-01-01__base"
    claim = {"alias": "A1", "prior_item_id": prior_id, "new_item_id": prior_id,
              "outcome": "accepted", "guard": None, "list_name": "action_items"}
    _write_step(tmp_path, "a", "with_block", 1, session, 0,
                topics=[{"action_items": [_child("Fix the pump", prior_id)]}])
    _write_step(tmp_path, "a", "with_block", 1, session, 1,
                topics=[{"action_items": [_child("Pump fixed today", prior_id)]}],
                claims=[claim])
    [assignment] = list(label.iter_assignments(tmp_path))
    gold_done = tmp_path / "gold.jsonl"
    label._write_jsonl(gold_done, [
        {"assignment_id": assignment["assignment_id"], "counterpart": "A1",
         "supported": True, "labeller": "owner"}])
    score.build_summary(tmp_path, gold_done, tmp_path / "results" / "summary.json")
    refs_path = tmp_path / "session_refs.json"
    assert refs_path.exists()
    assert json.loads(refs_path.read_text(encoding="utf-8")) == {session: "s01"}


# =========================================================================================
# Fix round 1: the real guard reaches scoring (Critical 2)
# =========================================================================================

def test_rejected_claim_guards_flow_through_export_import_build_summary(tmp_path):
    tmp_path = tmp_path / "continuity_eval_runs"  # export/import_labels/build_summary refuse
    prior_id = str(uuid.uuid4())
    session = "user__2026-01-01__base"
    echo_new_id = str(uuid.uuid4())
    existence_new_id = str(uuid.uuid4())
    # The action-item claim (alias A1) was rejected by the echo guard even though gold agrees
    # it IS the same work -- a genuine false reject.
    echo_claim = {"alias": "A1", "prior_item_id": prior_id, "new_item_id": echo_new_id,
                   "outcome": "rejected", "guard": "echo", "list_name": "action_items"}
    # Alias F9 never existed in the offered prior list at all -- the existence guard.
    existence_claim = {"alias": "F9", "prior_item_id": None, "new_item_id": existence_new_id,
                         "outcome": "rejected", "guard": "existence", "list_name": "findings"}
    _write_step(tmp_path, "a", "with_block", 1, session, 0,
                topics=[{"action_items": [_child("Fix the pump", prior_id)]}])
    _write_step(tmp_path, "a", "with_block", 1, session, 1,
                topics=[{"action_items": [_child("Pump task rework", echo_new_id)],
                          "findings": [_finding("Random leak noticed", existence_new_id)]}],
                claims=[echo_claim, existence_claim])

    assignments = list(label.iter_assignments(tmp_path))
    by_list = {a["list_name"]: a for a in assignments}
    assert by_list["action_items"]["model_claim_guard"] == "echo"
    assert by_list["findings"]["model_claim_guard"] == "existence"

    export_result = label.export(tmp_path, tmp_path / "todo")
    agent_lines = list(label._read_jsonl(export_result["agent_path"]))
    assert agent_lines and not any("model_claim_guard" in line for line in agent_lines)

    agent_done = tmp_path / "agent_done.jsonl"
    label._write_jsonl(agent_done, [
        {"assignment_id": by_list["action_items"]["assignment_id"], "counterpart": "A1",
         "supported": True, "labeller": "agent"},
        {"assignment_id": by_list["findings"]["assignment_id"], "counterpart": "none",
         "supported": True, "labeller": "agent"},
    ])
    merged = tmp_path / "assignments_done.jsonl"
    label.import_labels([agent_done], merged)

    summary = score.build_summary(tmp_path, merged, tmp_path / "results" / "summary.json")
    assert summary["reported"]["false_rejects_per_guard"] == {"echo": 1}
    assert summary["reported"]["hallucinated_alias_rate"] == 0.5


def test_scored_claims_rows_uses_the_real_guard_not_the_coarse_outcome():
    rows = [{"claim_alias": "A1", "claim_guard": "echo", "gold_counterparts": {"A1"}},
            {"claim_alias": "A2", "claim_guard": None, "gold_counterparts": {"A2"}}]
    scored = score.scored_claims_rows(rows)
    assert scored == [{"guard": "echo", "correct": True}, {"guard": None, "correct": True}]


# =========================================================================================
# Fix round 1 (Ruling C5): hard negatives elicited per assignment, not a corpus-wide flag
# =========================================================================================

def test_hard_negative_rows_builds_one_pair_per_flagged_alias():
    rows = [{"session": "s1", "hard_negatives": ["A2", "A3"], "claim_alias": "A2",
              "claim_outcome": "accepted"},
            {"session": "s1", "hard_negatives": [], "claim_alias": None,
              "claim_outcome": None}]
    pairs = score.hard_negative_rows(rows)
    assert len(pairs) == 2                      # one row per (new item, hard-negative alias)
    wrongly = sorted(p["wrongly_carried"] for p in pairs)
    assert wrongly == [False, True]              # claimed onto A2 (wrong); A3 untouched


def test_hard_negative_rows_uses_session_ref_when_given():
    rows = [{"session": "user__2026-01-01__base", "hard_negatives": ["A2"],
              "claim_alias": None, "claim_outcome": None}]
    refs = {"user__2026-01-01__base": "s01"}
    [pair] = score.hard_negative_rows(rows, session_ref=refs)
    assert pair["session"] == "s01"


def test_scored_assignments_reads_hard_negatives_from_gold_and_scores_the_pair(tmp_path):
    prior_id = str(uuid.uuid4())         # will be aliased "A1"
    other_prior_id = str(uuid.uuid4())   # will be aliased "A2"
    session = "user__2026-01-01__base"
    claim = {"alias": "A2", "prior_item_id": other_prior_id, "new_item_id": other_prior_id,
              "outcome": "accepted", "guard": None, "list_name": "action_items"}
    _write_step(tmp_path, "a", "with_block", 1, session, 0,
                topics=[{"action_items": [
                    _child("Fix the leaking valve in room 12", prior_id),
                    _child("Fix the leaking valve in room 14", other_prior_id)]}])
    _write_step(tmp_path, "a", "with_block", 1, session, 1,
                topics=[{"action_items": [_child("Valve in room 12 fixed", other_prior_id)]}],
                claims=[claim])
    [assignment] = list(label.iter_assignments(tmp_path))
    gold_done = tmp_path / "gold.jsonl"
    label._write_jsonl(gold_done, [
        {"assignment_id": assignment["assignment_id"], "counterpart": "A1", "supported": True,
         "labeller": "owner", "hard_negatives": ["A2"]}])
    [row] = score.scored_assignments(tmp_path, gold_done)
    assert row["hard_negatives"] == ["A2"]
    hn_rows = score.hard_negative_rows([row])
    # The model claimed "A2" (wrong -- gold's real counterpart is "A1"), and gold had
    # separately flagged "A2" as a hard negative for this new item: a wrong carry onto it.
    assert hn_rows == [{"session": session, "wrongly_carried": True}]
