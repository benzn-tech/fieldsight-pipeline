"""scripts/action_pack_eval metrics on the SYNTHETIC example fixtures (spec §8, §14 S16).

Every expected number below is worked by hand from scripts/action_pack_eval/examples/
(gold.json, run_a.json, annotations_a.jsonl, prices.json); the comments show the working.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts.action_pack_eval import annotations as ann_mod
from scripts.action_pack_eval import gold as gold_mod
from scripts.action_pack_eval import metrics
from scripts.action_pack_eval import runs as runs_mod

EX = Path(__file__).resolve().parents[2] / "scripts" / "action_pack_eval" / "examples"
S = "00000000-0000-4000-8000-0000000000"


@pytest.fixture()
def ex():
    gold = gold_mod.load_gold(EX / "gold.json")
    run = runs_mod.load_run(EX / "run_a.json")
    labels = ann_mod.load_annotations(EX / "annotations_a.jsonl")
    return gold, run, labels


def test_fabrication_raw_vs_post_verifier(ex):
    gold, run, labels = ex
    out = metrics.score_fabrication(run, labels)
    # Stored elements: b1 2 lines + 3 actions, b2 1 + 3, b3 3 + 1 = 13 (raw, incl. the two
    # stored unsupported actions). "no": b1 conclusion:1, b1 action:3, b3 conclusion:0.
    assert out["raw"] == {"elements": 13, "labelled": 13, "unlabelled": 0, "no": 3,
                          "unclear": 1, "rate": pytest.approx(3 / 13),
                          "rate_unclear_as_no": pytest.approx(4 / 13)}
    # Shown = everything but verifier == unsupported -> 11; "no": b1 conclusion:1, b3 c:0.
    assert out["shown"] == {"elements": 11, "labelled": 11, "unlabelled": 0, "no": 2,
                            "unclear": 1, "rate": pytest.approx(2 / 11),
                            "rate_unclear_as_no": pytest.approx(3 / 11)}
    # Counted, not labelled: b1 conclusion_unsupported 1 + drop_target_missing 1.
    assert out["dropped_by_verifier"] == 2
    assert out["raw_upper_bound_rate"] == pytest.approx((3 + 2) / (13 + 2))
    assert out["verdict"] == metrics.FAIL          # 18.2% > 5%


def test_fabrication_unlabelled_shown_element_is_insufficient(ex):
    gold, run, labels = ex
    labels = [r for r in labels if not (r["pack_id"] == S + "b3" and r["element"] == "action:1")]
    out = metrics.score_fabrication(run, labels, max_rate=1.0)
    assert out["shown"]["unlabelled"] == 1
    assert out["verdict"] == metrics.INSUFFICIENT


def test_fabrication_passes_at_threshold():
    run = {"packs": [{"gold_id": "G", "pack": {"id": "p", "state": "ready", "conclusion": [
        {"text": f"line {i}", "refs": ["R1"], "confidence": "supported"} for i in range(20)],
        "actions": [], "sources": []}}]}
    labels = [{"pack_id": "p", "element": f"conclusion:{i}", "annotator": "a",
               "supported_by_refs": "no" if i == 0 else "yes", "is_standards_claim": False}
              for i in range(20)]
    assert metrics.score_fabrication(run, labels)["verdict"] == metrics.PASS   # 1/20 = 5%


def test_wrong_target(ex):
    gold, run, labels = ex
    out = metrics.score_wrong_target(gold, run, labels)
    # Shown close/update: b1 action:1 (S1 ok), b2 action:1 (S4 must_not_touch), b2 action:2
    # (S3, task 3 ok). Raw adds b1 action:3 (S9 must_not_touch) and b2 action:3 (S2 ok).
    assert out["shown"] == {"targeted": 3, "wrong": 1, "rate": pytest.approx(1 / 3)}
    assert out["raw"] == {"targeted": 5, "wrong": 2, "rate": pytest.approx(2 / 5)}
    # Run order, then element order.
    assert out["wrong"] == [{"pack_id": S + "b1", "element": "action:3",
                             "reason": "must_not_touch", "shown": False},
                            {"pack_id": S + "b2", "element": "action:1",
                             "reason": "must_not_touch", "shown": True}]
    assert out["verdict"] == metrics.FAIL


def test_wrong_target_annotator_matched_expectation_on_another_target():
    gold = [{"id": "G", "site": "s", "instruction": "i", "scenario": "other",
             "expected_actions": [{"id": "e1", "type": "close_item",
                                   "target_stable_id": "aaa"}],
             "must_not_touch": [], "expected_uncertain": []}]
    run = {"packs": [{"gold_id": "G", "pack": {"id": "p", "state": "ready", "conclusion": [],
                                               "sources": [], "actions": [
        {"seq": 1, "type": "close_item", "target_stable_id": "bbb", "target_task_no": None,
         "refs": ["R1"], "verifier": "supported"}]}}]}
    labels = [{"pack_id": "p", "element": "action:1", "annotator": "a",
               "supported_by_refs": "yes", "is_standards_claim": False,
               "matches_expected": "e1"}]
    out = metrics.score_wrong_target(gold, run, labels)
    assert out["shown"] == {"targeted": 1, "wrong": 1, "rate": 1.0}
    assert out["wrong"][0]["reason"] == "mismatched_expectation"
    # ... and a matched-by-annotator wrong target never counts as recall.
    assert metrics.score_recall(gold, run, labels)["shown"]["matched"] == 0


def test_wrong_target_with_no_targeted_action_is_insufficient():
    gold = [{"id": "G", "site": "s", "instruction": "i", "scenario": "other",
             "expected_actions": [], "must_not_touch": [], "expected_uncertain": []}]
    run = {"packs": [{"gold_id": "G", "pack": {"id": "p", "state": "ready", "conclusion": [],
                                               "actions": [], "sources": []}}]}
    assert metrics.score_wrong_target(gold, run, [])["verdict"] == metrics.INSUFFICIENT


def test_recall(ex):
    gold, run, labels = ex
    out = metrics.score_recall(gold, run, labels)
    # 5 expected. Shown: G1 e1 (close S1), G1 e2 (create, annotator e2), G2 e2 (update by
    # task_no 3), G3 e1 (create, annotator e1) = 4. G2 e1 (close S2) only by the stored
    # unsupported b2 action:3 -> raw only.
    assert out["shown"] == {"expected": 5, "matched": 4, "rate": pytest.approx(0.8)}
    assert out["raw"] == {"expected": 5, "matched": 5, "rate": pytest.approx(1.0)}
    assert out["missed"] == [{"gold_id": "G2", "expected_id": "e1"}]
    assert out["verdict"] == metrics.PASS


def test_recall_counts_a_missing_or_failed_pack_as_missed(ex):
    gold, run, labels = ex
    run = {"packs": [p for p in run["packs"] if p["gold_id"] != "G1"]}
    out = metrics.score_recall(gold, run, labels)
    assert out["shown"] == {"expected": 5, "matched": 2, "rate": pytest.approx(0.4)}
    assert out["verdict"] == metrics.FAIL


def test_uncertain_honesty(ex):
    gold, run, labels = ex
    out = metrics.score_uncertain_honesty(gold, run, labels)
    # b3: conclusion:0 standards claim on R1 (records) marked supported -> dishonest;
    # conclusion:1 cites D1 (provenance documents) -> not in the denominator;
    # conclusion:2 on R2 marked uncertain -> honest.
    assert out["claims_without_document"] == 2
    assert out["marked_uncertain"] == 1
    assert out["rate"] == pytest.approx(0.5)
    assert out["expected_uncertain"] == {"expected": 1, "covered": 1}


def test_latency_p50_p90(ex):
    gold, run, labels = ex
    out = metrics.score_latency(run, max_p90_s=300, min_n=1)
    # created_at -> updated_at: 120, 240, 400 s; nearest rank p50 = 2nd, p90 = 3rd.
    assert out["end_to_end"] == {"n": 3, "p50_s": 240.0, "p90_s": 400.0}
    # run_trace.ms.total: 110, 230, 390 s.
    assert out["worker"] == {"n": 3, "p50_s": 230.0, "p90_s": 390.0}
    assert out["sources"] == {"timestamps": 3, "run_trace": 0, "timeout": 0, "none": 0,
                              "no_pack": 0, "unfinished": 0}
    assert out["verdict"] == metrics.FAIL


def test_latency_falls_back_to_worker_time_and_needs_min_n():
    run = {"packs": [{"gold_id": "G", "pack": {"id": "p", "state": "ready",
                                               "run_trace": {"ms": {"total": 60000}}}}]}
    out = metrics.score_latency(run, max_p90_s=300, min_n=1)
    assert out["sources"] == {"timestamps": 0, "run_trace": 1, "timeout": 0, "none": 0,
                              "no_pack": 0, "unfinished": 0}
    assert out["gate_p90_s"] == 60.0 and out["verdict"] == metrics.PASS
    assert metrics.score_latency(run, max_p90_s=300, min_n=20)["verdict"] == metrics.INSUFFICIENT


def test_cost_uses_the_price_file(ex):
    gold, run, labels = ex
    prices = metrics.load_prices(EX / "prices.json")
    out = metrics.score_cost(run, prices)
    # b1 10k tokens @ model-w-served 0.01/1k = 0.10; b2 20k @ default 0.02 = 0.40;
    # b3 5k @ default 0.02 = 0.10.
    assert out["tokens_total"] == 35000
    assert out["cost_total"] == pytest.approx(0.60)
    assert out["cost_mean"] == pytest.approx(0.20)
    assert out["currency"] == "USD"
    assert out["packs_unpriced"] == 0 and out["packs_without_tokens"] == 0


def test_cost_without_a_price_file_is_tokens_only(ex):
    gold, run, labels = ex
    out = metrics.score_cost(run, None)
    assert out["tokens_total"] == 35000 and out["cost_total"] is None
    assert out["packs_unpriced"] == 3


def test_cost_has_no_default_rate_unless_the_file_names_one(ex):
    gold, run, labels = ex
    out = metrics.score_cost(run, {"currency": "USD", "per_1k_tokens": {"model-w-served": 0.01}})
    assert out["packs_unpriced"] == 2
    assert out["cost_total"] == pytest.approx(0.10)


def test_percent_agreement_and_kappa_on_a_known_table():
    # Classic 2x2: yes/yes 20, yes/no 5, no/yes 10, no/no 15 -> po .70, pe .50, kappa .40.
    pairs = ([("yes", "yes")] * 20 + [("yes", "no")] * 5 + [("no", "yes")] * 10
             + [("no", "no")] * 15)
    assert metrics.percent_agreement(pairs) == pytest.approx(0.70)
    assert metrics.cohen_kappa(pairs) == pytest.approx(0.40)


def test_kappa_undefined_when_both_annotators_use_one_category():
    assert metrics.cohen_kappa([("yes", "yes")] * 4) is None
    assert metrics.percent_agreement([]) is None


def test_agreement_between_two_annotation_files():
    first = [{"pack_id": "p", "element": f"conclusion:{i}", "annotator": "a",
              "supported_by_refs": v, "is_standards_claim": False}
             for i, v in enumerate(["yes", "yes", "no", "unclear"])]
    second = [{"pack_id": "p", "element": f"conclusion:{i}", "annotator": "b",
               "supported_by_refs": v, "is_standards_claim": i == 3}
              for i, v in enumerate(["yes", "no", "no", "unclear"])]
    out = metrics.agreement(first, second)
    assert out["n"] == 4
    assert out["supported_by_refs"]["percent"] == pytest.approx(0.75)
    # po .75; marginals a: yes2 no1 unclear1, b: yes1 no2 unclear1 ->
    # pe = (2*1 + 1*2 + 1*1)/16 = 5/16; kappa = (.75 - .3125)/(1 - .3125) = .63636...
    assert out["supported_by_refs"]["kappa"] == pytest.approx((0.75 - 5 / 16) / (11 / 16))
    assert out["is_standards_claim"]["percent"] == pytest.approx(0.75)


def test_thresholds_defaults_and_override(tmp_path):
    # latency_min_n None = ceil(0.8 x gold size), resolved per report.
    assert metrics.DEFAULT_THRESHOLDS == {"fabrication_shown_max": 0.05,
                                          "wrong_target_max": 0, "recall_min": 0.60,
                                          "latency_p90_max_s": 300, "latency_min_n": None}
    assert metrics.resolve_thresholds(metrics.DEFAULT_THRESHOLDS, 20)["latency_min_n"] == 16
    assert metrics.resolve_thresholds(metrics.DEFAULT_THRESHOLDS, 21)["latency_min_n"] == 17
    assert metrics.resolve_thresholds(dict(metrics.DEFAULT_THRESHOLDS, latency_min_n=5),
                                      21)["latency_min_n"] == 5
    f = tmp_path / "t.json"
    f.write_text('{"recall_min": 0.7}', encoding="utf-8")
    assert metrics.load_thresholds(f)["recall_min"] == 0.7
    f.write_text('{"recall_minimum": 0.7}', encoding="utf-8")
    with pytest.raises(SystemExit):
        metrics.load_thresholds(f)


# ---------------------------------------------------------------- fix round 2

def _g(expected, must_not_touch=()):
    return [{"id": "G", "site": "s", "instruction": "i", "scenario": "other",
             "expected_actions": expected, "must_not_touch": list(must_not_touch),
             "expected_uncertain": []}]


def _r(actions):
    return {"packs": [{"gold_id": "G", "pack": {"id": "p", "state": "ready", "conclusion": [],
                                               "sources": [], "actions": actions}}]}


def test_recall_is_one_to_one_one_update_cannot_fill_two_expectations():
    # Two update expectations name the same item two ways (stable id, task number); one
    # proposed update on it fulfils one of them, never both.
    gold = _g([{"id": "e1", "type": "update_item", "target_stable_id": "X"},
               {"id": "e2", "type": "update_item", "target_task_no": 7}])
    run = _r([{"seq": 1, "type": "update_item", "target_stable_id": "X", "target_task_no": 7,
               "refs": ["R1"], "verifier": "supported"}])
    out = metrics.score_recall(gold, run, [])
    assert out["shown"] == {"expected": 2, "matched": 1, "rate": pytest.approx(0.5)}
    assert out["raw"]["matched"] == 1


def test_recall_honours_the_annotators_match_first():
    # The annotator says action 1 fulfils e2; e1 is then free for action 2.
    gold = _g([{"id": "e1", "type": "update_item", "target_stable_id": "X"},
               {"id": "e2", "type": "update_item", "target_task_no": 7}])
    run = _r([{"seq": 1, "type": "update_item", "target_stable_id": "X", "target_task_no": 7,
               "refs": ["R1"], "verifier": "supported"},
              {"seq": 2, "type": "update_item", "target_stable_id": "X", "target_task_no": 7,
               "refs": ["R1"], "verifier": "supported"}])
    labels = [{"pack_id": "p", "element": "action:1", "annotator": "a",
               "supported_by_refs": "yes", "is_standards_claim": False,
               "matches_expected": "e2"}]
    out = metrics.score_recall(gold, run, labels)
    assert out["shown"]["matched"] == 2
    assert out["matches"] == {"G/e2": "p/action:1", "G/e1": "p/action:2"}


def test_a_wrong_target_action_never_also_counts_for_recall():
    # e1 expects a close on Y; e3 a close on X. An action closing X that the annotator
    # matched to e1 is a wrong target, and does NOT also fulfil e3.
    gold = _g([{"id": "e1", "type": "close_item", "target_stable_id": "Y"},
               {"id": "e3", "type": "close_item", "target_stable_id": "X"}])
    run = _r([{"seq": 1, "type": "close_item", "target_stable_id": "X",
               "target_task_no": None, "refs": ["R1"], "verifier": "supported"}])
    labels = [{"pack_id": "p", "element": "action:1", "annotator": "a",
               "supported_by_refs": "yes", "is_standards_claim": False,
               "matches_expected": "e1"}]
    assert metrics.score_wrong_target(gold, run, labels)["shown"]["wrong"] == 1
    out = metrics.score_recall(gold, run, labels)
    assert out["shown"]["matched"] == 0 and out["raw"]["matched"] == 0


def test_a_must_not_touch_action_never_counts_for_recall():
    gold = _g([{"id": "e1", "type": "close_item", "target_stable_id": "X"}],
              must_not_touch=["X"])
    run = _r([{"seq": 1, "type": "close_item", "target_stable_id": "X",
               "target_task_no": None, "refs": ["R1"], "verifier": "supported"}])
    assert metrics.score_recall(gold, run, [])["shown"]["matched"] == 0


def test_latency_counts_rows_without_a_pack_and_unfinished_packs():
    run = {"packs": [
        {"gold_id": "G1", "pack": {"id": "p", "state": "ready",
                                   "created_at": "2026-11-01T00:00:00+00:00",
                                   "updated_at": "2026-11-01T00:01:00+00:00"}},
        {"gold_id": "G2", "pack": None, "error": "post_500", "http_status": 500},
        {"gold_id": "G3", "pack": None, "error": "get_404"},
        {"gold_id": "G4", "pack": None, "error": "site_unresolved"},
        {"gold_id": "G5", "pack": {"id": "q", "state": "cancelled"}}]}
    out = metrics.score_latency(run, max_p90_s=300, min_n=1)
    assert out["sources"]["no_pack"] == 3 and out["sources"]["unfinished"] == 1
    assert out["gate_n"] == 1
