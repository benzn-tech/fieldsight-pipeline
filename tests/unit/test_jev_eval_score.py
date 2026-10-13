"""Tests for the Jev shadow-eval scorer (Track A, Task 7).

`score_set(rows_by_arm_run, threshold_policy)` turns Task 6's result rows
into per-arm metrics. Label sets are small (26-27 rows today), so every
metric must say what n it was computed on and every undefined metric must
be `None` with a reason rather than 0 or NaN (controller ruling #7) --
except `coverage_at_p95`, which is 0 when no threshold reaches 0.95
precision (brief Step 4).

Row contract (controller ruling #1): `{"id", "label", "arm", "run", "score",
"answers", "question_hash", "latency_ms", "tokens", "error"}`. Rows with
`error` set or `score` None are `n_failed`, excluded from every metric.
"""
from __future__ import annotations

import hashlib

import pytest

from scripts.jev_eval import score as score_module
from scripts.jev_eval.score import (
    JevScoreError,
    control_check,
    coverage_at_p95,
    ece10,
    brier_score,
    run_agreement,
    score_set,
    split_half,
)


def _row(id_, label, score, run=1, arm="broad", error=None, answers=None, tokens=10):
    return {
        "id": id_,
        "label": label,
        "arm": arm,
        "run": run,
        "score": score,
        "answers": answers,
        "question_hash": "deadbeef",
        "latency_ms": 100,
        "tokens": tokens,
        "error": error,
    }


# ---------------------------------------------------------------------------
# Brier score
# ---------------------------------------------------------------------------

def test_brier_constant_half_predictor_is_quarter():
    rows = [_row(f"a{i}", label, 0.5) for i, label in enumerate(["yes", "no", "yes", "no"])]
    assert brier_score(rows) == pytest.approx(0.25)


def test_brier_none_when_no_rows():
    assert brier_score([]) is None


# ---------------------------------------------------------------------------
# ECE (10-bin)
# ---------------------------------------------------------------------------

def test_ece10_perfect_calibration_is_near_zero():
    # Within each of 10 bins, the fraction of "yes" equals the bin's mean score.
    rows = []
    i = 0
    for center in (0.05, 0.15, 0.25, 0.35, 0.45, 0.55, 0.65, 0.75, 0.85, 0.95):
        for _ in range(8):
            rows.append(_row(f"c{i}", "yes", center))
            i += 1
        for _ in range(2):
            rows.append(_row(f"c{i}", "no", center))
            i += 1
    # Each bin: 8/10 yes -> empirical accuracy 0.8, but confidence == center which
    # varies; use bins near 0.8 confidence for a truly tight check instead.
    tight_rows = []
    i = 0
    for _ in range(80):
        tight_rows.append(_row(f"t{i}", "yes", 0.8))
        i += 1
    for _ in range(20):
        tight_rows.append(_row(f"t{i}", "no", 0.8))
        i += 1
    result = ece10(tight_rows)
    assert result["value"] == pytest.approx(0.0, abs=1e-9)


def test_ece10_none_when_no_rows():
    assert ece10([])["value"] is None
    assert "reason" in ece10([])


# ---------------------------------------------------------------------------
# coverage_at_p95
# ---------------------------------------------------------------------------

def test_coverage_at_p95_zero_when_no_threshold_reaches_precision():
    # Scores and labels are anti-correlated: highest scores are wrong.
    rows = [
        _row("a", "no", 0.9),
        _row("b", "no", 0.8),
        _row("c", "yes", 0.7),
        _row("d", "no", 0.6),
        _row("e", "yes", 0.5),
    ]
    coverage, threshold = coverage_at_p95(rows)
    assert coverage == 0
    assert threshold is None


def test_coverage_at_p95_picks_largest_qualifying_set():
    rows = (
        [_row(f"y{i}", "yes", 0.9) for i in range(19)]
        + [_row("n0", "no", 0.9)]
        + [_row("n1", "no", 0.5), _row("n2", "no", 0.5)]
    )
    # threshold=0.9 accepts 20 rows (19 yes, 1 no): precision 19/20=0.95 -> qualifies.
    # threshold=0.5 accepts all 22 rows (19 yes, 3 no): precision 19/22=0.864 -> fails.
    # So the largest qualifying set is the 20 at threshold 0.9, not all 22.
    coverage, threshold = coverage_at_p95(rows)
    assert coverage == pytest.approx(20 / 22)
    assert threshold == pytest.approx(0.9)


# ---------------------------------------------------------------------------
# split_half
# ---------------------------------------------------------------------------

def test_split_half_disjoint_and_covers_all_ids():
    ids = [f"row-{i}" for i in range(27)]
    half_a, half_b = split_half(ids)
    assert half_a & half_b == set()
    assert half_a | half_b == set(ids)
    assert len(half_a) > 0 and len(half_b) > 0


def test_split_half_is_deterministic():
    ids = [f"row-{i}" for i in range(10)]
    a1, b1 = split_half(ids)
    a2, b2 = split_half(ids)
    assert a1 == a2 and b1 == b2


# ---------------------------------------------------------------------------
# run_agreement
# ---------------------------------------------------------------------------

def test_run_agreement_all_same_side_of_threshold():
    run1 = [_row("a", "yes", 0.9, run=1), _row("b", "no", 0.1, run=1)]
    run2 = [_row("a", "yes", 0.85, run=2), _row("b", "no", 0.05, run=2)]
    result = run_agreement(run1, run2, threshold=0.5)
    assert result["value"] == pytest.approx(1.0)
    assert result["mean_abs_score_diff"] == pytest.approx((0.05 + 0.05) / 2)


def test_run_agreement_none_when_no_overlap():
    run1 = [_row("a", "yes", 0.9, run=1)]
    run2 = [_row("b", "no", 0.1, run=2)]
    result = run_agreement(run1, run2, threshold=0.5)
    assert result["value"] is None
    assert "reason" in result


# ---------------------------------------------------------------------------
# control_check
# ---------------------------------------------------------------------------

def test_control_check_passes_when_margin_met():
    real_rows = [_row(f"r{i}", "yes", 0.9) for i in range(5)]
    control_rows = [_row(f"c{i}", "yes", 0.5) for i in range(5)]
    result = control_check(real_rows, control_rows)
    assert result["result"] == "pass"
    assert result["real_mean"] == pytest.approx(0.9)
    assert result["control_mean"] == pytest.approx(0.5)
    assert result["n_real"] == 5
    assert result["n_control"] == 5


def test_control_check_fails_when_margin_not_met():
    real_rows = [_row(f"r{i}", "yes", 0.6) for i in range(5)]
    control_rows = [_row(f"c{i}", "yes", 0.55) for i in range(5)]
    result = control_check(real_rows, control_rows)
    assert result["result"] == "fail"


def test_control_check_unreachable_when_no_yes_rows():
    real_rows = [_row("r0", "no", 0.6)]
    control_rows = [_row("c0", "no", 0.5)]
    result = control_check(real_rows, control_rows)
    assert result["result"] == "unreachable"


def test_control_check_unreachable_when_no_control_rows():
    real_rows = [_row("r0", "yes", 0.6)]
    result = control_check(real_rows, [])
    assert result["result"] == "unreachable"


# ---------------------------------------------------------------------------
# score_set: end-to-end shape and behaviour
# ---------------------------------------------------------------------------

def _make_synthetic_rows(arm, n=27, correct_bias=0.9, seed=0):
    """Rows whose score correlates with label, deterministic across calls."""
    rows_run1, rows_run2 = [], []
    for i in range(n):
        row_id = f"{arm}-{i}"
        label = "yes" if i % 2 == 0 else "no"
        base = correct_bias if label == "yes" else (1 - correct_bias)
        score1 = min(1.0, max(0.0, base))
        score2 = min(1.0, max(0.0, base + (0.01 if i % 3 == 0 else -0.01)))
        answers = {
            "same_work_item": {"noul": 1.0 if label == "yes" else 0.0},
            "task_named": {"noul": 1.0 if label == "yes" else 0.0},
            "same_trade": {"noul": 0.5},
        }
        rows_run1.append(_row(row_id, label, score1, run=1, arm=arm, answers=answers))
        rows_run2.append(_row(row_id, label, score2, run=2, arm=arm, answers=answers))
    return rows_run1, rows_run2


def test_score_set_basic_shape_with_fixed_threshold():
    run1, run2 = _make_synthetic_rows("baseline")
    rows_by_arm_run = {"baseline": {1: run1, 2: run2}}
    result = score_set(rows_by_arm_run, {"baseline": 0.5})
    metrics = result["baseline"]
    for key in (
        "accuracy", "precision", "recall", "brier", "ece10",
        "run_agreement", "coverage_at_p95", "threshold_at_p95", "n",
    ):
        assert key in metrics, f"missing {key}"
    assert metrics["n"] == 27
    assert metrics["n_failed"] == 0
    assert metrics["accuracy"] == pytest.approx(1.0)
    assert metrics["fixed_threshold"]["threshold"] == 0.5
    assert "split_half" in metrics


def test_score_set_excludes_failed_rows_from_n():
    run1, run2 = _make_synthetic_rows("baseline", n=10)
    run1.append(_row("fail-1", "yes", None, run=1, arm="baseline", error="timeout"))
    run1.append(_row("fail-2", "yes", 0.9, run=1, arm="baseline", error="bad json"))
    rows_by_arm_run = {"baseline": {1: run1, 2: run2}}
    result = score_set(rows_by_arm_run, {"baseline": 0.5})
    assert result["baseline"]["n"] == 10
    assert result["baseline"]["n_failed"] == 2


def test_score_set_fit_policy_uses_split_half():
    run1, run2 = _make_synthetic_rows("broad")
    rows_by_arm_run = {"broad": {1: run1, 2: run2}}
    result = score_set(rows_by_arm_run, {"broad": "fit"})
    metrics = result["broad"]
    assert metrics["fixed_threshold"] is None
    split_half_result = metrics["split_half"]
    assert "a_to_b" in split_half_result and "b_to_a" in split_half_result
    for direction in ("a_to_b", "b_to_a"):
        d = split_half_result[direction]
        assert d["n_fit"] > 0
        assert d["n_score"] > 0
        # fit and score sets must be disjoint ids
    # accuracy should still be computed off *some* threshold
    assert metrics["accuracy"] is not None


def test_score_set_defaults_missing_arm_policy_to_fit():
    run1, run2 = _make_synthetic_rows("decomposed")
    rows_by_arm_run = {"decomposed": {1: run1, 2: run2}}
    result = score_set(rows_by_arm_run, {})
    assert result["decomposed"]["fixed_threshold"] is None


def test_score_set_decomposed_reports_fitted_weights_and_v0():
    run1, run2 = _make_synthetic_rows("decomposed", n=26)
    rows_by_arm_run = {"decomposed": {1: run1, 2: run2}}
    result = score_set(rows_by_arm_run, {"decomposed": "fit"})
    decomposed_fit = result["decomposed"]["decomposed_fit"]
    assert "a_to_b" in decomposed_fit and "b_to_a" in decomposed_fit
    for direction in ("a_to_b", "b_to_a"):
        assert "weights" in decomposed_fit[direction]
        assert isinstance(decomposed_fit[direction]["weights"], dict)
    assert "v0_unfitted" in decomposed_fit
    assert decomposed_fit["v0_unfitted"]["brier"] is not None


def test_score_set_control_checks_present_for_broad_and_decomposed():
    broad_run1, broad_run2 = _make_synthetic_rows("broad", n=10)
    control_run1 = [
        _row(f"cb-{i}", "yes", 0.2, run=1, arm="control_broad")
        for i in range(5)
    ] + [
        _row(f"cb-{i}", "no", 0.2, run=1, arm="control_broad")
        for i in range(5, 10)
    ]
    rows_by_arm_run = {
        "broad": {1: broad_run1, 2: broad_run2},
        "control_broad": {1: control_run1, 2: control_run1},
    }
    result = score_set(rows_by_arm_run, {})
    checks = result["_control_checks"]
    assert "broad" in checks
    assert checks["broad"]["result"] in ("pass", "fail", "unreachable")


def test_score_set_raises_on_unknown_threshold_policy_type():
    run1, run2 = _make_synthetic_rows("baseline", n=5)
    rows_by_arm_run = {"baseline": {1: run1, 2: run2}}
    with pytest.raises(JevScoreError):
        score_set(rows_by_arm_run, {"baseline": "not-a-real-policy"})


# ---------------------------------------------------------------------------
# Fix round 1, finding #1: coverage_at_p95 must be held-out, not in-sample.
# ---------------------------------------------------------------------------

def test_coverage_at_p95_split_reports_lower_than_in_sample():
    """A case engineered so the in-sample (fit-and-score-on-everything)
    coverage figure looks better than what either split-half direction
    actually delivers out-of-sample -- the exact gap ruling #1 exists to
    surface instead of hide."""
    rows_a = (
        [_row(f"a-yes-{i}", "yes", 0.9) for i in range(10)]
        + [_row("a-no-0", "no", 0.5)]
    )
    rows_b = (
        [_row(f"b-yes-{i}", "yes", 0.6) for i in range(10)]
        + [_row("b-no-0", "no", 0.55)]
    )

    # Directions must partition disjoint ids.
    ids_a = {r["id"] for r in rows_a}
    ids_b = {r["id"] for r in rows_b}
    assert ids_a & ids_b == set()

    split_result = score_module._coverage_at_p95_split(rows_a, rows_b)

    # Fit on A (precision 1.0 in-sample) applied to B: nothing in B reaches
    # it -> zero coverage on the held-out half. Fix wave 5 (clause 3): the
    # threshold sits in the gap below the lowest accepted positive, midway
    # between no@0.5 and yes@0.9.
    assert split_result["a_to_b"]["threshold"] == pytest.approx(0.7)
    assert split_result["a_to_b"]["coverage_on_held_out"] == 0

    # Fit on B (midway between no@0.55 and yes@0.6) applied to A: the 10
    # yes@0.9 rows clear it, the one no@0.5 does not -> perfect precision.
    assert split_result["b_to_a"]["threshold"] == pytest.approx(0.575)
    assert split_result["b_to_a"]["coverage_on_held_out"] == pytest.approx(10 / 11)

    pooled_coverage = split_result["pooled"]["coverage"]

    in_sample_coverage, _ = coverage_at_p95(rows_a + rows_b)

    # In-sample fits and scores on the SAME 22 rows, so it can pick a
    # threshold (0.55: 21 of 22 rows, 20 "yes" -> precision 20/21 = 0.952)
    # that happens to cover both score bands at once -- a choice no
    # split-half direction gets to make against unseen data.
    assert in_sample_coverage == pytest.approx(21 / 22)
    assert pooled_coverage < in_sample_coverage


# ---------------------------------------------------------------------------
# Fix round 1, finding #2: run_agreement under "fit" must use each half's
# OUT-OF-SAMPLE threshold, not one threshold applied to every id.
# ---------------------------------------------------------------------------

def test_run_agreement_split_uses_out_of_sample_threshold_per_half():
    half_a_ids = {"a1"}
    half_b_ids = {"b1"}
    threshold_split = {
        "a_to_b": {"threshold": 0.5},  # fit on A, used to score B
        "b_to_a": {"threshold": 0.8},  # fit on B, used to score A
    }
    # a1 (half A) must be scored with threshold_split["b_to_a"] (0.8):
    # run1=0.6 (<0.8), run2=0.9 (>=0.8) -> different sides -> disagree.
    # If the buggy behaviour (a single a_to_b threshold=0.5 for all ids)
    # were used instead, both would read >=0.5 -> agree.
    run1 = [_row("a1", "yes", 0.6, run=1), _row("b1", "yes", 0.4, run=1)]
    run2 = [_row("a1", "yes", 0.9, run=2), _row("b1", "yes", 0.6, run=2)]
    # b1 (half B) must be scored with threshold_split["a_to_b"] (0.5):
    # run1=0.4 (<0.5), run2=0.6 (>=0.5) -> different sides -> disagree.

    result = score_module._run_agreement_split(run1, run2, half_a_ids, half_b_ids, threshold_split)
    assert result["value"] == pytest.approx(0.0)
    assert result["n"] == 2


# ---------------------------------------------------------------------------
# Fix round 1, finding #3: a fit half with only one label class must not
# yield a silent, degenerate threshold.
# ---------------------------------------------------------------------------

def test_fit_threshold_split_single_class_fit_half_returns_none_with_reason():
    all_no = [_row(f"no{i}", "no", 0.4) for i in range(5)]
    mixed = [_row(f"m{i}", "yes" if i % 2 == 0 else "no", 0.5 + i * 0.01) for i in range(6)]

    result = score_module._fit_threshold_split(all_no, mixed)
    direction = result["a_to_b"]  # fit on all_no (single class)
    assert direction["threshold"] is None
    assert direction["accuracy"] is None
    assert direction["precision"] is None
    assert direction["recall"] is None
    assert "reason" in direction


def test_p95_direction_single_class_fit_half_returns_none_with_reason():
    all_no = [_row(f"no{i}", "no", 0.4) for i in range(5)]
    mixed = [_row(f"m{i}", "yes" if i % 2 == 0 else "no", 0.5 + i * 0.01) for i in range(6)]

    split_result = score_module._coverage_at_p95_split(all_no, mixed)
    direction = split_result["a_to_b"]  # fit on all_no (single class)
    assert direction["threshold"] is None
    assert direction["coverage_on_held_out"] == 0
    assert "reason" in direction


def test_decomposed_direction_single_class_fit_half_returns_none_with_reason():
    def _make(id_, label, val):
        return _row(
            id_, label, 0.5, answers={
                "same_work_item": {"noul": val},
                "task_named": {"noul": val},
                "same_trade": {"noul": 0.5},
            },
        )

    all_no = [_make(f"no{i}", "no", 0.0) for i in range(5)]
    mixed = [_make(f"m{i}", "yes" if i % 2 == 0 else "no", 1.0 if i % 2 == 0 else 0.0) for i in range(6)]

    result = score_module._fit_decomposed_direction(all_no, mixed)
    assert result["p95"]["threshold"] is None
    assert "reason" in result["p95"]
    assert result["weights"] == {}


# ---------------------------------------------------------------------------
# Fix wave 1: amended decision rule (2026-09-28 findings-doc amendment).
# TDD case (b): a near-constant, base-rate-only model is exactly the case
# the ORIGINAL rule passed (Monte-Carlo: 63% at n=26) -- it must now come
# back "not_adopted" under the amended rule (precision floor / coverage-diff
# CI / control all reject a model with no real discrimination).
# ---------------------------------------------------------------------------

def _constant_rows(arm, n_per_class=25, base_score=0.5, jitter=0.0, id_prefix="row"):
    """`n_per_class` yes rows and `n_per_class` no rows, all scored at
    (near-)`base_score` regardless of label -- a model with no real
    discrimination between the classes. `id_prefix` defaults to a shared
    scheme so different arms scoring the SAME underlying rows share ids
    (required for the paired coverage/Brier comparisons)."""
    rows_run1, rows_run2 = [], []
    i = 0
    for label in ("yes", "no"):
        for k in range(n_per_class):
            row_id = f"{id_prefix}-{label}-{k}"
            # Deterministic tiny jitter so scores aren't bit-identical, but
            # carries no information about the label.
            delta = jitter * (1 if (i % 2 == 0) else -1)
            score = base_score + delta
            answers = {
                "same_work_item": {"noul": 0.5},
                "task_named": {"noul": 0.5},
                "same_trade": {"noul": 0.5},
            }
            rows_run1.append(_row(row_id, label, score, run=1, arm=arm, answers=answers))
            rows_run2.append(_row(row_id, label, score, run=2, arm=arm, answers=answers))
            i += 1
    return rows_run1, rows_run2


def test_verdict_near_constant_model_is_not_adopted():
    base_r1, base_r2 = _constant_rows("baseline", base_score=0.5, jitter=0.001)
    dec_r1, dec_r2 = _constant_rows("decomposed", base_score=0.5, jitter=0.001)
    ctrl_r1, ctrl_r2 = _constant_rows("control_decomposed", base_score=0.48, jitter=0.001)

    rows_by_arm_run = {
        "baseline": {1: base_r1, 2: base_r2},
        "decomposed": {1: dec_r1, 2: dec_r2},
        "control_decomposed": {1: ctrl_r1, 2: ctrl_r2},
    }
    scores = score_module.score_set(rows_by_arm_run, {"baseline": "fit"})
    result = score_module.verdict(scores)

    assert result["verdict"] == "not_adopted"
    assert result["reasons"], "not_adopted must carry at least one reason"


# ---------------------------------------------------------------------------
# TDD case (a): an informative, well-calibrated decomposed model clearly
# beats a weak (coin-flip) baseline -> replace.
# ---------------------------------------------------------------------------

def _informative_decomposed_rows(arm, n_per_class=25, base_flip_rate=0.0, seed=0, id_prefix="row"):
    """Rows whose noul sub-answers (and hence the fitted composite) strongly
    separate the classes, and whose stored `score` mirrors that separation
    -- deterministic across calls. Shares `id_prefix`'s scheme with
    `_constant_rows` so different arms scoring the SAME underlying rows
    share ids (required for the paired coverage/Brier comparisons)."""
    rows_run1, rows_run2 = [], []
    i = 0
    for label in ("yes", "no"):
        for k in range(n_per_class):
            row_id = f"{id_prefix}-{label}-{k}"
            val = 1.0 if label == "yes" else 0.0
            score = 0.92 if label == "yes" else 0.05
            answers = {
                "same_work_item": {"noul": val},
                "task_named": {"noul": val},
                "same_trade": {"noul": 0.5},
            }
            rows_run1.append(_row(row_id, label, score, run=1, arm=arm, answers=answers))
            # Run 2: tiny, label-preserving jitter -- same decisions, near-
            # identical answers (not a provider cache, but stable).
            score2 = min(1.0, max(0.0, score + (0.01 if i % 2 == 0 else -0.01)))
            rows_run2.append(_row(row_id, label, score2, run=2, arm=arm, answers=answers))
            i += 1
    return rows_run1, rows_run2


def test_verdict_informative_model_beats_weak_baseline_is_replace():
    # Fix wave 4, A1: the precision floor is now the Clopper-Pearson LOWER
    # bound of held-out precision (>= 0.90), which needs ~28 accepted rows
    # all correct -- n_per_class=25 (the pre-wave-4 size) no longer clears
    # it even with perfect separation, so this uses 40 per class.
    base_r1, base_r2 = _constant_rows("baseline", n_per_class=40, base_score=0.5, jitter=0.001)
    dec_r1, dec_r2 = _informative_decomposed_rows("decomposed", n_per_class=40)
    # Control state must NOT reproduce the real signal -- constant, close to
    # the overall base rate, well under the real arm's mean on yes rows.
    ctrl_r1, ctrl_r2 = _constant_rows(
        "control_decomposed", n_per_class=40, base_score=0.3, jitter=0.001)

    rows_by_arm_run = {
        "baseline": {1: base_r1, 2: base_r2},
        "decomposed": {1: dec_r1, 2: dec_r2},
        "control_decomposed": {1: ctrl_r1, 2: ctrl_r2},
    }
    scores = score_module.score_set(rows_by_arm_run, {"baseline": "fit"})
    result = score_module.verdict(scores)

    assert result["verdict"] == "replace", result["reasons"]


# ---------------------------------------------------------------------------
# TDD case (c): control fails -> not_adopted even when the numeric gates
# (coverage, precision floor, Brier, stability) all pass.
# ---------------------------------------------------------------------------

def test_verdict_control_fail_is_not_adopted_even_with_good_numerics():
    base_r1, base_r2 = _constant_rows("baseline", base_score=0.5, jitter=0.001)
    dec_r1, dec_r2 = _informative_decomposed_rows("decomposed")
    # Control reproduces the SAME separation as the real arm -- the model is
    # reading something present even in the donor-substituted state, so the
    # margin (real_mean - control_mean on label=='yes' rows) fails.
    ctrl_r1, ctrl_r2 = _informative_decomposed_rows("control_decomposed")

    rows_by_arm_run = {
        "baseline": {1: base_r1, 2: base_r2},
        "decomposed": {1: dec_r1, 2: dec_r2},
        "control_decomposed": {1: ctrl_r1, 2: ctrl_r2},
    }
    scores = score_module.score_set(rows_by_arm_run, {"baseline": "fit"})
    assert scores["_control_checks"]["decomposed"]["result"] == "fail"

    result = score_module.verdict(scores)
    assert result["verdict"] == "not_adopted"
    assert any("control" in reason for reason in result["reasons"])


# ---------------------------------------------------------------------------
# TDD case (d): fewer than 20 rows of one class -> descriptive_only.
# ---------------------------------------------------------------------------

def test_verdict_descriptive_only_below_eligibility_floor():
    # Only 10 of each class -- under the 20-per-class eligibility floor.
    base_r1, base_r2 = _constant_rows("baseline", n_per_class=10, base_score=0.5)
    dec_r1, dec_r2 = _informative_decomposed_rows("decomposed", n_per_class=10)

    rows_by_arm_run = {
        "baseline": {1: base_r1, 2: base_r2},
        "decomposed": {1: dec_r1, 2: dec_r2},
    }
    scores = score_module.score_set(rows_by_arm_run, {"baseline": "fit"})
    result = score_module.verdict(scores)

    assert result["verdict"] == "descriptive_only"
    assert "20" in result["reasons"][0]


# ---------------------------------------------------------------------------
# TDD case (e): the bootstrap is deterministic under the fixed seed.
# ---------------------------------------------------------------------------

def test_bootstrap_coverage_diff_is_deterministic():
    dec_r1, _ = _informative_decomposed_rows("decomposed")
    base_r1, _ = _constant_rows("baseline", base_score=0.5, jitter=0.001)
    rows_by_arm_run = {
        "baseline": {1: base_r1, 2: base_r1},
        "decomposed": {1: dec_r1, 2: dec_r1},
    }
    scores = score_module.score_set(rows_by_arm_run, {"baseline": "fit"})
    jev_records = {r["id"]: r for r in scores["decomposed"]["held_out_records"]}
    base_records = {r["id"]: r for r in scores["baseline"]["held_out_records"]}

    first = score_module._bootstrap_coverage_diff(jev_records, base_records)
    second = score_module._bootstrap_coverage_diff(jev_records, base_records)
    assert first == second


# ---------------------------------------------------------------------------
# TDD case (f): stability counts flips at the p95 threshold, not the
# accuracy-maximising threshold.
# ---------------------------------------------------------------------------

def test_stability_at_p95_uses_p95_threshold_not_accuracy_threshold():
    half_a_ids = {"a1"}
    half_b_ids = {"b1"}
    # p95 threshold (fit on the OTHER half) is much higher than any
    # accuracy-maximising threshold would be -- a1's run1/run2 scores
    # straddle the p95 threshold (0.8) but would agree under a lower
    # accuracy threshold (e.g. 0.5).
    p95_thresholds = {"a_to_b": 0.5, "b_to_a": 0.8}
    run1 = [_row("a1", "yes", 0.6, run=1), _row("b1", "yes", 0.4, run=1)]
    run2 = [_row("a1", "yes", 0.9, run=2), _row("b1", "yes", 0.6, run=2)]

    result = score_module.stability_at_p95(run1, run2, half_a_ids, half_b_ids, p95_thresholds)
    # a1 (half A) is judged by threshold_split["b_to_a"] = 0.8:
    #   run1=0.6 (<0.8), run2=0.9 (>=0.8) -> flip.
    # b1 (half B) is judged by threshold_split["a_to_b"] = 0.5:
    #   run1=0.4 (<0.5), run2=0.6 (>=0.5) -> flip.
    assert result["flips"] == 2
    assert result["n"] == 2
    assert result["pass"] is False  # 2 flips > max(1, 5% of 2) = 1


# ---------------------------------------------------------------------------
# TDD case (g): the decomposed arm's gates all read the held-out refit
# probability -- never the row's own stored v0 composite `score`.
# ---------------------------------------------------------------------------

def test_decomposed_held_out_records_use_refit_probability_not_v0_score():
    dec_r1, dec_r2 = _informative_decomposed_rows("decomposed")
    # Sabotage every row's stored v0 `score` so it disagrees with the label
    # (and with the noul answers the refit is actually fit on).
    for row in dec_r1 + dec_r2:
        row["score"] = 0.999 if row["label"] == "no" else 0.001

    rows_by_arm_run = {"decomposed": {1: dec_r1, 2: dec_r2}}
    scores = score_module.score_set(rows_by_arm_run, {})
    records = {r["id"]: r for r in scores["decomposed"]["held_out_records"]}

    # The refit reads the (uncorrupted) noul answers, which still separate
    # the classes cleanly -- so held-out scores must still track the label,
    # not the sabotaged v0 `score` (which would show the opposite pattern).
    yes_scores = [rec["score"] for rec in records.values() if rec["label"] == "yes"]
    no_scores = [rec["score"] for rec in records.values() if rec["label"] == "no"]
    assert min(yes_scores) > max(no_scores)


# ---------------------------------------------------------------------------
# Fix wave 4, A1: the precision floor is the Clopper-Pearson LOWER bound of
# held-out precision, not the raw point estimate.
# ---------------------------------------------------------------------------

def test_clopper_pearson_lower_28_of_28_matches_known_value():
    # 28/28 -> lower bound ~= 0.899 at one-sided 95% (brief's own pinned value).
    lower = score_module.clopper_pearson_lower(28, 28)
    assert lower == pytest.approx(0.899, abs=0.001)


def test_clopper_pearson_lower_zero_successes_is_zero():
    assert score_module.clopper_pearson_lower(0, 10) == 0.0


def test_clopper_pearson_lower_zero_trials_is_undefined():
    assert score_module.clopper_pearson_lower(0, 0) is None


def test_clopper_pearson_lower_below_28_of_28_does_not_meet_090_floor():
    # 25/25 perfect precision still does not clear the 0.90 floor -- this is
    # exactly why the earlier n=25-per-class "informative" test needed to
    # grow to n=40 (see test_verdict_informative_model_beats_weak_baseline_is_replace).
    lower = score_module.clopper_pearson_lower(25, 25)
    assert lower < score_module.PRECISION_FLOOR


def test_floored_coverage_uses_clopper_pearson_not_raw_precision():
    # 25/25 accepted, all correct: raw precision is 1.0, but the CP lower
    # bound (~0.887) is below the 0.90 floor -- coverage must count as 0.
    records = {
        f"id{i}": {"id": f"id{i}", "label": "yes", "score": 0.9, "decision": True}
        for i in range(25)
    }
    ids = list(records)
    coverage, precision, floor_met, lower_bound = score_module._floored_coverage(records, ids)
    assert precision == 1.0
    assert lower_bound < score_module.PRECISION_FLOOR
    assert floor_met is False
    assert coverage == 0.0


# ---------------------------------------------------------------------------
# Fix wave 4, A2: augment now needs the SAME coverage-difference CI
# lower-bound-positive condition replace does -- a point estimate of exactly
# 0 (which the old "coverage_point >= 0" check happily accepted) must not
# reach augment when the CI itself straddles/touches zero.
# ---------------------------------------------------------------------------

def _identical_records(n=30, label="yes", decision=True, score=0.9):
    return {
        f"id{i}": {"id": f"id{i}", "label": label, "score": score, "decision": decision}
        for i in range(n)
    }


def test_augment_requires_ci_lower_bound_positive_not_just_point_nonneg():
    # jev and baseline share IDENTICAL held-out records (same decisions, same
    # labels) -- every bootstrap resample computes the same coverage for
    # both, so point_estimate and ci_90 are both exactly 0. The precision
    # floor is cleared (30/30 -> CP lower ~0.905 >= 0.90).
    records = _identical_records(n=30)
    scores = {
        "decomposed": {
            "n_yes": 30, "n_no": 30,
            "held_out_records": list(records.values()),
            "stability": {"pass": True},
        },
        "baseline": {
            "n_yes": 30, "n_no": 30,
            "held_out_records": list(records.values()),
            "brier_calibrated_records": list(records.values()),
        },
        "_control_checks": {"decomposed": {"result": "pass"}},
    }
    result = score_module._compute_arm_verdict("decomposed", scores)
    assert result["coverage_diff"]["point_estimate"] == pytest.approx(0.0)
    assert result["coverage_diff"]["ci_90"] == [pytest.approx(0.0), pytest.approx(0.0)]
    assert result["verdict"] == "not_adopted"


# ---------------------------------------------------------------------------
# Fix wave 4, A3: the control check reads the SAME score the other gates
# read for that arm -- for `decomposed`, the control arm's sub-answers are
# scored through the REAL arm's fitted cross-fit composite weights, never
# the control arm's own raw v0 `score`.
# ---------------------------------------------------------------------------

def test_decomposed_control_records_scored_via_real_arms_fitted_weights():
    weights_a_to_b = {"intercept": -10.0, "f": 20.0}  # fit on real half A, scores half B
    weights_b_to_a = {"intercept": 10.0, "f": -20.0}  # fit on real half B, scores half A
    half_a_ids = {"a1"}
    half_b_ids = {"b1"}
    control_rows = [
        _row("a1", "yes", 0.9, answers={"f": {"noul": 0.0}}),
        _row("b1", "yes", 0.9, answers={"f": {"noul": 1.0}}),
    ]
    records = score_module._decomposed_control_records(
        control_rows, half_a_ids, half_b_ids, weights_a_to_b, weights_b_to_a)
    by_id = {r["id"]: r for r in records}

    # a1 (half A) is scored via weights_b_to_a: 10 + (-20)*0.0 = 10 -> sigmoid ~= 1.
    assert by_id["a1"]["score"] == pytest.approx(1.0, abs=1e-3)
    # b1 (half B) is scored via weights_a_to_b: -10 + 20*1.0 = 10 -> sigmoid ~= 1.
    assert by_id["b1"]["score"] == pytest.approx(1.0, abs=1e-3)
    # Neither reads the row's own raw v0 `score` (0.9) directly.
    assert by_id["a1"]["score"] != 0.9
    assert by_id["b1"]["score"] != 0.9


def test_score_set_control_check_decomposed_ignores_sabotaged_raw_v0_score():
    dec_r1, dec_r2 = _informative_decomposed_rows(
        "decomposed", n_per_class=15, id_prefix="ctrlfix")

    # A proper control state: NO signal in the noul sub-answers (constant),
    # but the raw v0 `score` is sabotaged to look strongly label-separated
    # anyway -- proving the control check no longer reads it.
    ctrl_r1 = []
    for row in dec_r1:
        ctrl_row = dict(row)
        ctrl_row["arm"] = "control_decomposed"
        ctrl_row["answers"] = {
            "same_work_item": {"noul": 0.5}, "task_named": {"noul": 0.5},
            "same_trade": {"noul": 0.5},
        }
        ctrl_row["score"] = 0.95 if row["label"] == "yes" else 0.05
        ctrl_r1.append(ctrl_row)
    ctrl_r2 = [dict(r) for r in ctrl_r1]
    for r in ctrl_r2:
        r["run"] = 2

    rows_by_arm_run = {
        "decomposed": {1: dec_r1, 2: dec_r2},
        "control_decomposed": {1: ctrl_r1, 2: ctrl_r2},
    }
    scores = score_module.score_set(rows_by_arm_run, {})
    control = scores["_control_checks"]["decomposed"]
    assert control["control_mean"] is not None
    # Must NOT read the sabotaged raw mean (0.95) -- the fitted composite,
    # applied to a constant/uninformative feature, produces a value far from it.
    assert control["control_mean"] < 0.8


# ---------------------------------------------------------------------------
# Fix wave 4, A4: a failed decomposed fit half keeps its held-out rows (with
# decision=False), never drops them from the paired comparison.
# ---------------------------------------------------------------------------

def test_decomposed_direction_failed_fit_keeps_held_out_records():
    def _make(id_, label, val):
        return _row(
            id_, label, 0.42, answers={
                "same_work_item": {"noul": val},
                "task_named": {"noul": val},
                "same_trade": {"noul": 0.5},
            },
        )

    all_no = [_make(f"no{i}", "no", 0.0) for i in range(5)]
    mixed = [_make(f"m{i}", "yes" if i % 2 == 0 else "no", 1.0 if i % 2 == 0 else 0.0)
             for i in range(6)]

    result = score_module._fit_decomposed_direction(all_no, mixed)
    records = result["_records"]
    assert len(records) == len(mixed)
    assert {r["id"] for r in records} == {r["id"] for r in mixed}
    assert all(r["decision"] is False for r in records)
    # Falls back to the row's own stored v0 score (there is no fitted
    # composite to score it with).
    assert all(r["score"] == 0.42 for r in records)


# ---------------------------------------------------------------------------
# Fix wave 4, A5: a baseline score that is not a probability is Platt-scaled
# (1-feature cross-fit logistic) before the paired Brier comparison.
# ---------------------------------------------------------------------------

def test_baseline_brier_calibrated_records_are_platt_scaled_not_raw_score():
    base_rows = []
    for i in range(20):
        base_rows.append(_row(f"row-yes-{i}", "yes", 10.0, run=1, arm="baseline"))
        base_rows.append(_row(f"row-no-{i}", "no", -10.0, run=1, arm="baseline"))

    rows_by_arm_run = {"baseline": {1: base_rows, 2: base_rows}}
    scores = score_module.score_set(rows_by_arm_run, {})
    records = scores["baseline"]["brier_calibrated_records"]
    assert records
    for rec in records:
        assert 0.0 <= rec["score"] <= 1.0  # a real probability, not the raw +-10
        if rec["label"] == "yes":
            assert rec["score"] > 0.9
        else:
            assert rec["score"] < 0.1


# ---------------------------------------------------------------------------
# Fix wave 4, A8: Monte-Carlo sweep over a fixed set of seeds at n=130
# (65 per class) -- a control that passes, a near-constant model and a
# random model must never reach augment/replace; an informative model
# reaches replace in the majority of seeds. `n_reps` is reduced from
# production (2000) to keep this file's runtime under ~60s -- never reduced
# in `score.py` itself.
# ---------------------------------------------------------------------------

import random as _random  # noqa: E402

MC_SEEDS = range(5)
MC_N_REPS = 100
MC_N_PER_CLASS = 65  # n=130 total, per the brief


def _mc_constant_rows(arm, seed, n_per_class, base_score, id_prefix):
    rng = _random.Random(seed)
    rows1, rows2 = [], []
    for label in ("yes", "no"):
        for k in range(n_per_class):
            row_id = f"{id_prefix}-{label}-{k}"
            answers = {"same_work_item": {"noul": 0.5}, "task_named": {"noul": 0.5},
                       "same_trade": {"noul": 0.5}}
            score1 = min(1.0, max(0.0, base_score + rng.uniform(-0.05, 0.05)))
            score2 = min(1.0, max(0.0, base_score + rng.uniform(-0.05, 0.05)))
            rows1.append(_row(row_id, label, score1, run=1, arm=arm, answers=answers))
            rows2.append(_row(row_id, label, score2, run=2, arm=arm, answers=answers))
    return rows1, rows2


def _mc_random_rows(arm, seed, n_per_class, id_prefix):
    rng = _random.Random(seed)
    rows1, rows2 = [], []
    for label in ("yes", "no"):
        for k in range(n_per_class):
            row_id = f"{id_prefix}-{label}-{k}"
            s1, s2 = rng.random(), rng.random()
            answers1 = {"same_work_item": {"noul": s1}, "task_named": {"noul": s1},
                        "same_trade": {"noul": 0.5}}
            answers2 = {"same_work_item": {"noul": s2}, "task_named": {"noul": s2},
                        "same_trade": {"noul": 0.5}}
            rows1.append(_row(row_id, label, s1, run=1, arm=arm, answers=answers1))
            rows2.append(_row(row_id, label, s2, run=2, arm=arm, answers=answers2))
    return rows1, rows2


# ---------------------------------------------------------------------------
# Fix wave 5, item 5: Monte-Carlo tests that bite. Wave 4's versions never
# had a passing control (margin ~0 in every seed), so "never augment/replace"
# held for the wrong reason, and the "informative" model was a perfect,
# seed-independent 0/1 separator. Here:
#   - near-constant and random models are judged with a control that DOES
#     pass (asserted), so the precision floor / coverage CI / stability are
#     the only things that can stop them. An uninformative model cannot pass
#     a real control through the cross-fit refit (its weights are ~0), so
#     the pass is injected -- the adversarial case the floor must survive;
#   - a realistic strong model (noisy Beta-distributed sub-answers, jittered
#     run 2, a control whose answers look like the "no" class) reaches
#     replace in a majority of seeds at n=130, base rate 0.5, through the
#     real pipeline end to end.
# ---------------------------------------------------------------------------

import numpy as _np  # noqa: E402

MC5_SEEDS = range(6)
MC5_N_REPS = 300


def _mc5_rows(arm, model, labels, rng, as_control=False):
    rows1, rows2 = [], []
    for k, label in enumerate(labels):
        positive = label == "yes" and not as_control
        if model == "near_constant":
            f = [0.5 + rng.uniform(-0.02, 0.02) for _ in range(3)]
            g = [0.5 + rng.uniform(-0.02, 0.02) for _ in range(3)]
        elif model == "random_stable":
            f = [rng.random() for _ in range(3)]
            g = list(f)
        elif model == "random_unstable":
            f = [rng.random() for _ in range(3)]
            g = [rng.random() for _ in range(3)]
        elif model == "strong":
            f = [rng.beta(20, 2) if positive else rng.beta(2, 20) for _ in range(2)]
            f.append(rng.random())
            g = [float(_np.clip(v + rng.normal(0, 0.03), 0, 1)) for v in f]
        else:  # pragma: no cover
            raise AssertionError(model)
        f = [float(v) for v in f]
        g = [float(v) for v in g]

        def _answers(v):
            return {"same_work_item": {"noul": v[0]}, "task_named": {"noul": v[1]},
                    "same_trade": {"noul": v[2]}}
        rows1.append(_row(f"r{k}", label, float(_np.mean(f)), run=1, arm=arm, answers=_answers(f)))
        rows2.append(_row(f"r{k}", label, float(_np.mean(g)), run=2, arm=arm, answers=_answers(g)))
    return rows1, rows2


def _mc5_scores(model, seed, n=130, base_rate=0.5, force_control_pass=False):
    n_yes = round(n * base_rate)
    labels = ["yes"] * n_yes + ["no"] * (n - n_yes)
    rng = _np.random.default_rng(1000 * seed + 7)
    dec = _mc5_rows("decomposed", model, labels, rng)
    ctrl = _mc5_rows("control_decomposed", model, labels, rng, as_control=True)
    base = [_row(f"r{k}", label, 0.0, run=1, arm="baseline") for k, label in enumerate(labels)]
    scores = score_module.score_set({
        "baseline": {1: base, 2: base},
        "decomposed": {1: dec[0], 2: dec[1]},
        "control_decomposed": {1: ctrl[0], 2: ctrl[1]},
    }, {})
    if force_control_pass:
        scores["_control_checks"]["decomposed"] = {
            "result": "pass", "real_mean": 0.9, "control_mean": 0.1, "margin": 0.8,
            "n_real": n_yes, "n_control": n_yes,
        }
    return scores


@pytest.mark.parametrize("model", ["near_constant", "random_stable", "random_unstable"])
def test_mc_uninformative_model_never_augments_even_with_a_passing_control(model):
    for seed in MC5_SEEDS:
        scores = _mc5_scores(model, seed, force_control_pass=True)
        assert scores["_control_checks"]["decomposed"]["result"] == "pass"
        result = score_module.verdict(scores, n_reps=MC5_N_REPS, seed=seed)
        assert result["verdict"] not in ("augment", "replace"), (model, seed, result["reasons"])


def test_mc_realistic_strong_model_reaches_replace_in_majority_of_seeds():
    verdicts = []
    for seed in MC5_SEEDS:
        scores = _mc5_scores("strong", seed)
        # The control is computed through the real pipeline, not injected.
        assert scores["_control_checks"]["decomposed"]["result"] == "pass"
        verdicts.append(score_module.verdict(scores, n_reps=MC5_N_REPS, seed=seed)["verdict"])
    n_replace = sum(1 for v in verdicts if v == "replace")
    assert n_replace > len(MC5_SEEDS) / 2, verdicts


# ---------------------------------------------------------------------------
# Fix wave 5, item 3: the Clopper-Pearson floor applies to the POINT
# estimate only; inside each bootstrap rep the raw held-out precision >= 0.90
# is the floor. Applying CP inside every rep double-counted uncertainty: a
# model at 64/65 accepted correct (CP lower 0.929) lost the coverage CI
# because the reps that resampled 3+ false positives fell below the CP bound
# and counted as 0 coverage.
# ---------------------------------------------------------------------------

def _records(n_accept_yes, n_accept_no, n_reject_yes, n_reject_no, prefix="r"):
    recs = {}
    i = 0
    for label, decision, count in (("yes", True, n_accept_yes), ("no", True, n_accept_no),
                                   ("yes", False, n_reject_yes), ("no", False, n_reject_no)):
        for _ in range(count):
            recs[f"{prefix}{i}"] = {"id": f"{prefix}{i}", "label": label, "score": 0.5,
                                    "decision": decision}
            i += 1
    return recs


def test_bootstrap_reps_use_raw_precision_floor_not_clopper_pearson():
    jev = _records(64, 1, 1, 64)
    base = {k: {**v, "decision": False} for k, v in jev.items()}
    result = score_module._bootstrap_coverage_diff(jev, base, n_reps=2000, seed=1)
    assert result["jev_precision_floor_met"] is True
    assert result["ci_90"][0] > 0, result["ci_90"]


def test_point_estimate_still_uses_clopper_pearson():
    # 25/25 accepted, all correct: raw precision 1.0 but CP lower 0.887 < 0.90.
    jev = _records(25, 0, 40, 65)
    base = {k: {**v, "decision": False} for k, v in jev.items()}
    result = score_module._bootstrap_coverage_diff(jev, base, n_reps=200, seed=1)
    assert result["jev_precision_floor_met"] is False
    assert result["jev_coverage"] == 0.0


# ---------------------------------------------------------------------------
# Fix wave 5, item 4: Platt scaling standardises the baseline score on the
# fit half before the 1-feature logistic fit, so the calibrated Brier does
# not depend on the raw score's range (wave 4's ridge penalty on the raw
# score crushed a 0..0.1-range baseline to the base rate: 0.253 vs 0.117).
# ---------------------------------------------------------------------------

def _platt_brier(scale, seed=3):
    rng = _np.random.default_rng(seed)
    rows = []
    for k in range(130):
        label = "yes" if k % 2 == 0 else "no"
        x = scale * (0.35 + 0.4 * (label == "yes") + rng.normal(0, 0.2))
        rows.append(_row(f"p{k}", label, float(max(x, 0.0)), run=1, arm="baseline"))
    scores = score_module.score_set({"baseline": {1: rows, 2: rows}}, {})
    recs = scores["baseline"]["brier_calibrated_records"]
    assert len(recs) == 130
    return float(_np.mean([(r["score"] - (r["label"] == "yes")) ** 2 for r in recs]))


def test_platt_calibrated_brier_is_invariant_to_raw_score_scale():
    briers = [_platt_brier(scale) for scale in (0.1, 0.4, 1.0)]
    assert max(briers) - min(briers) < 0.005, briers
    # And it is a real calibration of an informative score, not the base rate.
    assert max(briers) < 0.15, briers


# ---------------------------------------------------------------------------
# Fix wave 5, item 7 (minors): a failed decomposed direction's v0 fallback
# rows never enter the paired Brier (counted instead), and the control check
# compares the SAME row set on both sides.
# ---------------------------------------------------------------------------

def test_v0_fallback_records_excluded_from_paired_brier_and_counted():
    jev = _records(40, 0, 0, 40, prefix="x")
    for i, key in enumerate(sorted(jev)):
        jev[key]["score"] = 0.9 if jev[key]["label"] == "yes" else 0.1
        if i < 10:
            jev[key]["fallback"] = True
    base = {k: {"id": k, "label": v["label"], "score": 0.5} for k, v in jev.items()}
    brier = score_module._bootstrap_brier_diff(jev, base, n_reps=50, seed=0)
    assert brier["n"] == 70
    assert brier["n_excluded_fallback"] == 10


def test_failed_decomposed_direction_marks_its_records_as_fallback():
    def _make(id_, label, val):
        return _row(id_, label, 0.42, arm="decomposed", answers={
            "same_work_item": {"noul": val}, "task_named": {"noul": val},
            "same_trade": {"noul": 0.5}})
    all_no = [_make(f"no{i}", "no", 0.0) for i in range(5)]
    mixed = [_make(f"m{i}", "yes" if i % 2 == 0 else "no", 1.0 if i % 2 == 0 else 0.0)
             for i in range(6)]
    records = score_module._fit_decomposed_direction(all_no, mixed)["_records"]
    assert records and all(r.get("fallback") is True for r in records)


def test_control_check_decomposed_uses_identical_row_sets_when_a_direction_fails():
    # Every half-A id is "yes", so the fit on half A is single-class and
    # fails; the real side must not keep half-B v0 fallback rows the control
    # side cannot be scored on.
    ids = [f"c{i}" for i in range(80)]
    half_a, half_b = score_module.split_half(ids)
    dec_r1, ctrl_r1 = [], []
    for i, id_ in enumerate(ids):
        label = "yes" if (id_ in half_a or i % 2 == 0) else "no"
        v = 1.0 if label == "yes" else 0.0
        ans = {"same_work_item": {"noul": v}, "task_named": {"noul": v},
               "same_trade": {"noul": 0.5}}
        dec_r1.append(_row(id_, label, 0.99, arm="decomposed", answers=ans))
        cans = {"same_work_item": {"noul": 0.0}, "task_named": {"noul": 0.0},
                "same_trade": {"noul": 0.5}}
        ctrl_r1.append(_row(id_, label, 0.99, arm="control_decomposed", answers=cans))
    scores = score_module.score_set({
        "decomposed": {1: dec_r1, 2: dec_r1},
        "control_decomposed": {1: ctrl_r1, 2: ctrl_r1},
    }, {})
    control = scores["_control_checks"]["decomposed"]
    assert control["n_real"] == control["n_control"], control


# ---------------------------------------------------------------------------
# Fix wave 5, item 3 (clause 3): the operating point is not placed ON a
# negative's score when the negatives it accepts buy no positive coverage.
# The lowest threshold with fit-half precision >= 0.95 sits on the highest
# accepted negative -- inside the negative cluster for a sharply separated
# model, where run-2 jitter flips held-out negatives across it (a sharper
# model failed stability MORE: 0/20 seeds reached replace at Beta(200,2)).
# The threshold now moves up to the lowest fit-half POSITIVE at or above
# that point and sits at the midpoint of the gap below it -- the same
# fit-half positives accepted, fewer or equal negatives.
# ---------------------------------------------------------------------------

def test_operating_point_sits_in_the_gap_for_a_separated_fit_half():
    negatives = [0.01 + 0.001 * i for i in range(32)]
    positives = [0.90 + 0.002 * i for i in range(33)]
    scores = negatives + positives
    labels = [False] * len(negatives) + [True] * len(positives)
    threshold, precision = score_module._lowest_threshold_at_target_precision(scores, labels)
    assert max(negatives) < threshold < min(positives)
    assert threshold == pytest.approx((max(negatives) + min(positives)) / 2)
    assert precision == 1.0


def test_operating_point_keeps_negatives_that_sit_between_positives():
    # A negative ABOVE the lowest accepted positive does buy coverage (the
    # positives below it); it stays accepted, precision still >= 0.95.
    scores = [0.1, 0.2, 0.5] + [0.4] + [0.6 + 0.01 * i for i in range(30)]
    labels = [False, False, False] + [True] + [True] * 30
    threshold, precision = score_module._lowest_threshold_at_target_precision(scores, labels)
    assert 0.2 < threshold <= 0.4
    assert precision == pytest.approx(31 / 32)


def test_sharply_separated_model_passes_stability():
    rng = _np.random.default_rng(0)
    labels = ["yes"] * 65 + ["no"] * 65
    dec1, dec2 = [], []
    for k, label in enumerate(labels):
        pos = label == "yes"
        f = [rng.beta(200, 2) if pos else rng.beta(2, 200) for _ in range(2)] + [rng.random()]
        g = [float(_np.clip(v + rng.normal(0, 0.03), 0, 1)) for v in f]

        def _a(v):
            return {"same_work_item": {"noul": float(v[0])}, "task_named": {"noul": float(v[1])},
                    "same_trade": {"noul": float(v[2])}}
        dec1.append(_row(f"r{k}", label, float(_np.mean(f)), run=1, arm="decomposed", answers=_a(f)))
        dec2.append(_row(f"r{k}", label, float(_np.mean(g)), run=2, arm="decomposed", answers=_a(g)))
    scores = score_module.score_set({"decomposed": {1: dec1, 2: dec2}}, {})
    assert scores["decomposed"]["stability"]["pass"] is True, scores["decomposed"]["stability"]
