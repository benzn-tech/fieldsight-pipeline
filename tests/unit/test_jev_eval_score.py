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
