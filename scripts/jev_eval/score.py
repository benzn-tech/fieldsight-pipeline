"""Scoring for the Jev shadow evaluation (Track A, Task 7).

Reads Task 6's result rows and produces per-arm metrics comparing the
third-party ("Jev") decision model against human labels. Label sets are
small today (26-27 rows per set), so every metric here says what n it was
computed on, and every metric that is *undefined* at that n (zero predicted
positives, no threshold reaching 0.95 precision, no overlapping ids between
runs, ...) is reported as `None` with a `reason` -- never silently as 0 or
NaN. The one deliberate exception is `coverage_at_p95`, which the brief
defines as 0 (not None) when no threshold reaches 0.95 precision.

Row contract (controller ruling, Task 6 writes these, this module only
reads them):
    {"id": str, "label": "yes"|"no", "arm": str, "run": int,
     "score": float|None, "answers": dict|None, "question_hash": str,
     "latency_ms": int, "tokens": int|None, "error": str|None}
Arms: baseline, broad, decomposed, control_broad, control_decomposed. A row
with `error` set or `score` None is counted in `n_failed` for its arm and
excluded from every metric -- it is never scored as 0 (ruling #1).

`score_set(rows_by_arm_run, threshold_policy)`:
    `rows_by_arm_run` = {arm: {run: [rows]}}. `threshold_policy` = {arm:
    fixed_threshold_float} | {arm: "fit"}; an arm missing from the dict
    defaults to "fit" (split-half fitted). Per ruling #2, when a fixed
    threshold is given, BOTH the fixed-threshold numbers and the fitted
    numbers are reported (`fixed_threshold` and `split_half` keys); the
    top-level accuracy/precision/recall/threshold_at_p95 use the fixed
    threshold when one is given, else the split-half cross-fit predictions
    (each half scored by the OTHER half's fitted threshold, pooled to cover
    every row without ever fitting and scoring the same rows -- ruling #4).

Metrics needing one score per row (accuracy, precision, recall, brier,
ece10, coverage_at_p95) use run 1 only (ruling #3). `run_agreement` compares
run 1 and run 2 on the same ids, and also reports the mean absolute score
difference between runs -- the noise floor a real effect has to clear.

`coverage_at_p95` (controller ruling, fix round 1) is a dict, not a bare
number: `{"pooled": ..., "a_to_b": ..., "b_to_a": ..., "in_sample": ...}`.
`pooled`/`a_to_b`/`b_to_a` are HELD-OUT: on the fit half, take the lowest
threshold whose precision on the fit half is >= 0.95, then report that
threshold's precision and coverage on the OTHER (held-out) half; `pooled`
combines both directions' held-out predictions (every usable row is
held-out in exactly one direction, so this covers all of them without ever
scoring a row against a threshold fit with that row in it). `in_sample` is
the older, optimistic, same-rows-fit-and-scored number -- kept for
comparison, explicitly labelled so nobody reads it as the gate. Likewise
`run_agreement` under a `"fit"` policy pools each half's agreement using
that half's held-out threshold (fit on the OTHER half), never a single
threshold applied to ids it was fit on.

Split-half (ruling #4): ids are partitioned by the parity of
`int(sha256(id).hexdigest(), 16)` -- deterministic, no RNG, no dependency on
id ordering. Half A and half B are always disjoint and always cover every id
passed in. Each direction (fit on A / score on B, and fit on B / score on A)
is reported separately; nothing is ever fit and scored on the same rows.

Decomposed arm (ruling #5): in addition to the ordinary metrics computed off
the row's stored `score` (the v0 composite from `questions.py`), a logistic
regression is fit on the *noul* sub-answers only (`status_claimed` and other
choice-type sub-answers are excluded) via plain-numpy gradient descent with
an L2 penalty (`L2_PENALTY` below) -- with ~13 rows per half an
unregularised fit on near-separable synthetic-looking label patterns would
blow up. Both the fitted weights and the v0-vs-fitted comparison are
reported under `decomposed_fit`. Per the controller's fix-round-1 ruling,
the p95 threshold for this arm is fit on the FITTED composite's predicted
probabilities on the same fit half the weights came from, and evaluated on
the other half -- `decomposed_fit["a_to_b"]["p95"]` /
`["b_to_a"]["p95"]` / `["p95_pooled"]`, mirroring the non-decomposed
`coverage_at_p95` shape.

Control check (ruling #6): for each Jev arm that has a paired control arm
(`broad`/`control_broad`, `decomposed`/`control_decomposed`), compares the
mean score on label=="yes" rows between the two, pass only if
real_mean - control_mean >= CONTROL_MARGIN (0.2). No yes rows on either side
-> "unreachable", never "pass".
"""
from __future__ import annotations

import hashlib
import math
from functools import lru_cache
from typing import Any

import numpy as np

# ---------------------------------------------------------------------------
# Constants (brief's exact values)
# ---------------------------------------------------------------------------

ECE_N_BINS = 10
COVERAGE_PRECISION_TARGET = 0.95
CONTROL_MARGIN = 0.2

# Amended decision rule (2026-09-28, docs/superpowers/specs/
# 2026-09-28-jev-shadow-eval-findings.md, section 2 addendum). See
# `verdict()` below for the mechanical application of clause 9.
ELIGIBILITY_MIN_PER_CLASS = 20
PRECISION_FLOOR = 0.90
BOOTSTRAP_REPS = 2000
BOOTSTRAP_SEED = 20260928

# L2 (ridge) penalty for the decomposed-weight logistic regression. With as
# few as ~13 rows per split half, an unregularised fit on any near-separable
# label pattern diverges (weights -> +/-inf); this keeps it bounded. Not
# tuned against real data yet -- revisit once owner labelling grows n.
L2_PENALTY = 1.0
# Fix wave 5, item 4: Platt scaling fits on the STANDARDISED baseline score
# (z-scored on the fit half), so its penalty only has to guard against
# complete separation, not tame the raw score's scale -- a small penalty.
PLATT_L2_PENALTY = 0.01
_LOGREG_LR = 0.5
_LOGREG_ITERS = 2000

# Sub-answer keys that are choice-type, not noul, in questions.py's
# decomposed sets -- excluded from the composite-weight regression (ruling
# #5). Kept as an explicit list (not "everything without a 'noul' key")
# because a malformed/missing noul sub-answer for a real noul question
# should still surface, not be silently swept into "must be non-noul".
_NON_NOUL_SUBANSWER_KEYS = {"status_claimed"}


class JevScoreError(ValueError):
    """Raised on a malformed or unsupported call into this module."""


# ---------------------------------------------------------------------------
# Clopper-Pearson (fix wave 4, A1): the precision floor is the one-sided 95%
# LOWER confidence bound of held-out precision, not the raw point estimate --
# a raw "28/28 = 1.0" says nothing about the next row, while its
# Clopper-Pearson lower bound (~0.8985 -- just BELOW 0.90, so the floor
# needs 29/29, or 46 accepted with one error) is a real statement about what
# fraction of future accepted rows are correct. Implemented with numpy/stdlib
# only (no scipy): the lower bound at confidence `1 - alpha` for `successes`
# out of `n` trials is Beta_ppf(alpha; successes, n - successes + 1) when
# successes > 0, else 0.0 exactly (the standard convention: a lower bound of
# zero needs no numerical inversion). `n == 0` is undefined -> None, per the
# brief ("0/0 -> undefined -> floor fails").
#
# The inverse Beta CDF is found by bisection on the regularised incomplete
# beta function `_betainc` (Numerical-Recipes-style continued fraction via
# `_betacf`, using `math.lgamma` for the log-Beta normaliser) -- pinned
# against a known value: 28/28 -> lower bound ~= 0.899 at one-sided 95%.
# `lru_cache` matters here: `_bootstrap_coverage_diff` calls this inside every
# bootstrap rep, and a paired resample of a small held-out set revisits the
# same (successes, n) integer pair many times.
# ---------------------------------------------------------------------------

def _betacf(a: float, b: float, x: float, max_iter: int = 100, eps: float = 1e-8) -> float:
    """Continued-fraction evaluation used by `_betainc` (Numerical Recipes
    `betacf`). Converges quickly for the small integer (a, b) this module
    ever calls it with."""
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < 1e-30:
        d = 1e-30
    d = 1.0 / d
    h = d
    for m in range(1, max_iter + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-30:
            d = 1e-30
        c = 1.0 + aa / c
        if abs(c) < 1e-30:
            c = 1e-30
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-30:
            d = 1e-30
        c = 1.0 + aa / c
        if abs(c) < 1e-30:
            c = 1e-30
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def _betainc(x: float, a: float, b: float) -> float:
    """Regularised incomplete beta function I_x(a, b), x in [0, 1]."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    ln_beta = math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)
    front = math.exp(math.log(x) * a + math.log(1.0 - x) * b - ln_beta)
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def _beta_ppf(p: float, a: float, b: float, tol: float = 1e-9, max_iter: int = 100) -> float:
    """Inverse CDF (quantile) of Beta(a, b) at probability `p`, via
    bisection over `_betainc` -- exact enough for a 0.90 floor comparison,
    and far cheaper per call than a tighter tolerance would be given how
    often `clopper_pearson_lower` is called inside the bootstrap."""
    lo, hi = 0.0, 1.0
    for _ in range(max_iter):
        mid = (lo + hi) / 2.0
        if _betainc(mid, a, b) < p:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol:
            break
    return (lo + hi) / 2.0


@lru_cache(maxsize=None)
def clopper_pearson_lower(successes: int, n: int, confidence: float = 0.95) -> float | None:
    """One-sided Clopper-Pearson LOWER bound of a binomial proportion at
    `confidence` (default 0.95). `None` when `n == 0` (undefined -- the
    precision floor fails on an empty accepted set, per the brief). Cached:
    only ever called with small non-negative integers."""
    if n == 0 or successes < 0 or successes > n:
        return None
    if successes == 0:
        return 0.0
    alpha = 1.0 - confidence
    return _beta_ppf(alpha, successes, n - successes + 1)


# ---------------------------------------------------------------------------
# Row filtering
# ---------------------------------------------------------------------------

def _is_usable(row: dict) -> bool:
    return row.get("error") is None and row.get("score") is not None


def _usable_rows(rows: list) -> list:
    return [r for r in rows if _is_usable(r)]


def _label_bit(row: dict) -> int:
    return 1 if row["label"] == "yes" else 0


def _has_both_classes(usable_rows: list) -> bool:
    saw_yes = any(r["label"] == "yes" for r in usable_rows)
    saw_no = any(r["label"] == "no" for r in usable_rows)
    return saw_yes and saw_no


def _class_balance_reason(rows: list) -> str | None:
    """None if `rows` has usable rows of both labels (safe to fit a
    threshold on); otherwise a reason string. A fit half with only one
    label class produces a degenerate threshold (any value clears the
    "maximise accuracy on this half" bar when everyone shares a label) --
    controller ruling (fix round 1, finding #3): every threshold-dependent
    metric fit on such a half must come back None with this reason, not a
    threshold nobody chose for a real reason."""
    usable = _usable_rows(rows)
    if not usable:
        return "fit half has no usable rows"
    if not _has_both_classes(usable):
        return "fit half has fewer than one row of each label"
    return None


# ---------------------------------------------------------------------------
# Split-half
# ---------------------------------------------------------------------------

def split_half(ids: list) -> tuple:
    """Deterministic split of `ids` into two disjoint sets covering all of
    them, by the parity of sha256(id) interpreted as an integer. No RNG, no
    dependence on input order or length."""
    half_a: set = set()
    half_b: set = set()
    for id_ in ids:
        digest = hashlib.sha256(str(id_).encode("utf-8")).hexdigest()
        if int(digest, 16) % 2 == 0:
            half_a.add(id_)
        else:
            half_b.add(id_)
    return half_a, half_b


# ---------------------------------------------------------------------------
# Brier score
# ---------------------------------------------------------------------------

def brier_score(rows: list) -> float | None:
    usable = _usable_rows(rows)
    if not usable:
        return None
    errors = [(r["score"] - _label_bit(r)) ** 2 for r in usable]
    return float(sum(errors) / len(errors))


# ---------------------------------------------------------------------------
# ECE (10-bin)
# ---------------------------------------------------------------------------

def ece10(rows: list, n_bins: int = ECE_N_BINS) -> dict:
    usable = _usable_rows(rows)
    if not usable:
        return {"value": None, "reason": "no scored rows", "n_bins_used": 0}

    bins = [[] for _ in range(n_bins)]
    for row in usable:
        score = min(max(row["score"], 0.0), 1.0)
        idx = min(int(score * n_bins), n_bins - 1)
        bins[idx].append(row)

    n_total = len(usable)
    weighted_gap = 0.0
    n_bins_used = 0
    for bucket in bins:
        if not bucket:
            continue
        n_bins_used += 1
        confidence = sum(r["score"] for r in bucket) / len(bucket)
        accuracy = sum(_label_bit(r) for r in bucket) / len(bucket)
        weighted_gap += (len(bucket) / n_total) * abs(accuracy - confidence)

    return {"value": float(weighted_gap), "n_bins_used": n_bins_used, "n": n_total}


# ---------------------------------------------------------------------------
# Threshold-based accuracy / precision / recall
# ---------------------------------------------------------------------------

def _metrics_at_threshold(rows: list, threshold: float) -> dict:
    usable = _usable_rows(rows)
    if not usable:
        return {
            "accuracy": None, "precision": None, "recall": None,
            "reason": "no scored rows", "n": 0,
        }

    tp = fp = tn = fn = 0
    for row in usable:
        predicted_positive = row["score"] >= threshold
        actual_positive = row["label"] == "yes"
        if predicted_positive and actual_positive:
            tp += 1
        elif predicted_positive and not actual_positive:
            fp += 1
        elif not predicted_positive and actual_positive:
            fn += 1
        else:
            tn += 1

    n = len(usable)
    accuracy = (tp + tn) / n
    precision = tp / (tp + fp) if (tp + fp) > 0 else None
    recall = tp / (tp + fn) if (tp + fn) > 0 else None

    result = {"accuracy": accuracy, "precision": precision, "recall": recall, "n": n}
    if precision is None:
        result["precision_reason"] = "no predicted positives at this threshold"
    if recall is None:
        result["recall_reason"] = "no actual positives in this set"
    return result


# ---------------------------------------------------------------------------
# coverage_at_p95
# ---------------------------------------------------------------------------

def coverage_at_p95(rows: list) -> tuple:
    """IN-SAMPLE ONLY -- fits the threshold and measures coverage/precision
    on the SAME rows. Kept because it is a cheap sanity number, but it reads
    optimistic (a threshold can always be chosen to fit its own data) and is
    never the gate number reported by `score_set` (that is the held-out
    figure computed by `_coverage_at_p95_split` / `_fit_decomposed`, labelled
    `coverage_at_p95.pooled`). Returns (coverage, threshold); 0 / None when
    no threshold reaches 0.95 precision even in-sample (brief Step 4 -- the
    one metric that is 0, not None, when undefined)."""
    usable = _usable_rows(rows)
    if not usable:
        return 0, None

    n = len(usable)
    candidate_thresholds = sorted({r["score"] for r in usable}, reverse=True)

    best_coverage = 0.0
    best_threshold = None
    for threshold in candidate_thresholds:
        accepted = [r for r in usable if r["score"] >= threshold]
        positives = sum(1 for r in accepted if r["label"] == "yes")
        if not accepted:
            continue
        precision = positives / len(accepted)
        if precision >= COVERAGE_PRECISION_TARGET:
            coverage = len(accepted) / n
            if coverage > best_coverage:
                best_coverage = coverage
                best_threshold = threshold

    if best_threshold is None:
        return 0, None
    return best_coverage, best_threshold


# ---------------------------------------------------------------------------
# coverage_at_p95, split-half / held-out (controller ruling, fix round 1)
#
# On the FIT half: take the LOWEST threshold whose precision on the fit half
# is >= 0.95 (not the one maximising in-sample coverage -- the lowest
# qualifying threshold is the most permissive one that still cleared the bar
# on data it was chosen from). Report that threshold's precision and
# coverage on the HELD-OUT half, in both directions, plus a pooled figure
# (the held-out predictions from both directions combined -- covers every
# usable row exactly once, since each row is held out in exactly one
# direction). If no threshold on the fit half reaches 0.95, that direction's
# coverage is 0 with a reason -- never silently the in-sample number.
# ---------------------------------------------------------------------------

def _lowest_threshold_at_target_precision(
    scores: list, labels: list, target: float = COVERAGE_PRECISION_TARGET
) -> tuple:
    """`labels` is a parallel list of bool (True = positive/"yes"). Returns
    (threshold, precision_at_that_threshold), or (None, None) if `scores` is
    empty, has only one label class, or no threshold reaches `target`."""
    if not scores or not (any(labels) and not all(labels)):
        return None, None
    qualifying = []
    for threshold in sorted(set(scores)):
        accepted_idx = [i for i, s in enumerate(scores) if s >= threshold]
        if not accepted_idx:
            continue
        precision = sum(1 for i in accepted_idx if labels[i]) / len(accepted_idx)
        if precision >= target:
            qualifying.append((threshold, precision))
    if not qualifying:
        return None, None
    lowest, _ = min(qualifying, key=lambda pair: pair[0])

    # Fix wave 5, item 3 (clause 3): do not sit ON a negative that buys no
    # positive coverage. Every fit-half row scored in [lowest, s_p), where
    # s_p is the lowest POSITIVE at or above `lowest`, is a negative:
    # accepting it adds no coverage of positives and puts the boundary
    # inside the negative cluster, where run-2 jitter flips held-out
    # negatives across it (a sharply separated model failed stability MORE
    # than a noisy one). Move up to s_p -- identical positives accepted on
    # the fit half, fewer or equal negatives, so precision only rises -- and
    # place the threshold at the midpoint of the gap below s_p (the
    # max-margin boundary that makes the same fit-half decisions).
    positives_at_or_above = [s for s, lab in zip(scores, labels) if lab and s >= lowest]
    s_p = min(positives_at_or_above)
    below = [s for s in scores if s < s_p]
    threshold = (s_p + max(below)) / 2.0 if below else s_p
    accepted_idx = [i for i, s in enumerate(scores) if s >= threshold]
    precision = sum(1 for i in accepted_idx if labels[i]) / len(accepted_idx)
    return threshold, precision


def _p95_direction(fit_rows: list, score_rows: list) -> dict:
    """Fit the p95 threshold on `fit_rows`, evaluate it on `score_rows`
    (disjoint by id -- the caller partitions via `split_half`). Internal key
    `_pool` (a tuple `(n_accepted, n_positive)`) is used by the caller to
    build the pooled figure across both directions and is stripped before
    the result is returned to `score_set`'s output."""
    reason = _class_balance_reason(fit_rows)
    fit_usable = _usable_rows(fit_rows)
    score_usable = _usable_rows(score_rows)

    if reason:
        return {
            "threshold": None, "precision_on_fit_half": None,
            "coverage_on_held_out": 0, "precision_on_held_out": None,
            "n_fit": len(fit_usable), "n_held_out": len(score_usable),
            "reason": reason,
            "_records": [
                {"id": r["id"], "label": r["label"], "score": r["score"], "decision": False}
                for r in score_usable
            ],
        }

    scores_fit = [r["score"] for r in fit_usable]
    labels_fit = [r["label"] == "yes" for r in fit_usable]
    threshold, precision_on_fit = _lowest_threshold_at_target_precision(scores_fit, labels_fit)

    if threshold is None:
        return {
            "threshold": None, "precision_on_fit_half": None,
            "coverage_on_held_out": 0, "precision_on_held_out": None,
            "n_fit": len(fit_usable), "n_held_out": len(score_usable),
            "reason": "no threshold on fit half reaches 0.95 precision",
            "_records": [
                {"id": r["id"], "label": r["label"], "score": r["score"], "decision": False}
                for r in score_usable
            ],
        }

    accepted = [r for r in score_usable if r["score"] >= threshold]
    if accepted:
        positives = sum(1 for r in accepted if r["label"] == "yes")
        precision_held = positives / len(accepted)
        coverage_held = len(accepted) / len(score_usable) if score_usable else 0.0
    else:
        positives = 0
        precision_held = None
        coverage_held = 0.0

    records = [
        {"id": r["id"], "label": r["label"], "score": r["score"], "decision": r["score"] >= threshold}
        for r in score_usable
    ]

    return {
        "threshold": threshold,
        "precision_on_fit_half": precision_on_fit,
        "coverage_on_held_out": coverage_held,
        "precision_on_held_out": precision_held,
        "n_fit": len(fit_usable),
        "n_held_out": len(score_usable),
        "_pool": (len(accepted), positives),
        "_records": records,
    }


def _coverage_at_p95_split(rows_a: list, rows_b: list) -> dict:
    """Both directions (fit on A / score on B, fit on B / score on A) plus a
    pooled figure combining the held-out predictions from each -- this
    pooled figure, not either direction alone and never the in-sample
    `coverage_at_p95`, is the number `score_set` reports as the gate."""
    dir_ab = _p95_direction(rows_a, rows_b)  # fit on A, held-out = B
    dir_ba = _p95_direction(rows_b, rows_a)  # fit on B, held-out = A

    pooled_n = dir_ab["n_held_out"] + dir_ba["n_held_out"]
    pooled_accepted = 0
    pooled_positive = 0
    pooled_records: list = []
    for direction in (dir_ab, dir_ba):
        n_accepted, n_positive = direction.pop("_pool", (0, 0))
        pooled_accepted += n_accepted
        pooled_positive += n_positive
        pooled_records.extend(direction.pop("_records", []))

    if pooled_n == 0:
        pooled = {"coverage": 0, "precision": None, "n": 0, "reason": "no usable rows"}
    elif pooled_accepted == 0:
        pooled = {
            "coverage": 0, "precision": None, "n": pooled_n,
            "reason": "no threshold reached 0.95 precision on either fit half",
        }
    else:
        pooled = {
            "coverage": pooled_accepted / pooled_n,
            "precision": pooled_positive / pooled_accepted,
            "n": pooled_n,
        }

    return {"a_to_b": dir_ab, "b_to_a": dir_ba, "pooled": pooled, "pooled_records": pooled_records}


# ---------------------------------------------------------------------------
# run_agreement
# ---------------------------------------------------------------------------

def run_agreement(run1_rows: list, run2_rows: list, threshold: float) -> dict:
    usable1 = {r["id"]: r for r in _usable_rows(run1_rows)}
    usable2 = {r["id"]: r for r in _usable_rows(run2_rows)}
    common_ids = sorted(set(usable1) & set(usable2))

    if not common_ids:
        return {
            "value": None, "mean_abs_score_diff": None,
            "reason": "no overlapping usable ids between runs", "n": 0,
        }

    same_side = 0
    abs_diffs = []
    for id_ in common_ids:
        s1, s2 = usable1[id_]["score"], usable2[id_]["score"]
        if (s1 >= threshold) == (s2 >= threshold):
            same_side += 1
        abs_diffs.append(abs(s1 - s2))

    return {
        "value": same_side / len(common_ids),
        "mean_abs_score_diff": sum(abs_diffs) / len(abs_diffs),
        "n": len(common_ids),
    }


def _run_agreement_split(
    run1_rows: list, run2_rows: list, half_a_ids: set, half_b_ids: set, threshold_split: dict
) -> dict:
    """Run agreement under a fitted ("fit") threshold policy, without ever
    scoring a row against a threshold fitted with that row in it (controller
    ruling, fix round 1, finding #2). A half-A id is scored with the
    threshold fit on half B (`threshold_split["b_to_a"]`, i.e. fit on B and
    used to score A); a half-B id uses the threshold fit on half A
    (`threshold_split["a_to_b"]`). Results from both halves are pooled into
    one agreement figure and one noise-floor figure."""
    threshold_for_half_a = threshold_split["b_to_a"]["threshold"]
    threshold_for_half_b = threshold_split["a_to_b"]["threshold"]

    usable1 = {r["id"]: r for r in _usable_rows(run1_rows)}
    usable2 = {r["id"]: r for r in _usable_rows(run2_rows)}
    common_ids = set(usable1) & set(usable2)

    same_side = 0
    abs_diffs = []
    n_used = 0
    for id_ in common_ids:
        if id_ in half_a_ids:
            threshold = threshold_for_half_a
        elif id_ in half_b_ids:
            threshold = threshold_for_half_b
        else:
            continue
        if threshold is None:
            continue
        s1, s2 = usable1[id_]["score"], usable2[id_]["score"]
        if (s1 >= threshold) == (s2 >= threshold):
            same_side += 1
        abs_diffs.append(abs(s1 - s2))
        n_used += 1

    if n_used == 0:
        return {
            "value": None, "mean_abs_score_diff": None,
            "reason": "no common ids had an out-of-sample threshold available", "n": 0,
        }
    return {
        "value": same_side / n_used,
        "mean_abs_score_diff": sum(abs_diffs) / len(abs_diffs),
        "n": n_used,
    }


# ---------------------------------------------------------------------------
# Threshold fitting (split-half, accuracy-maximising -- used for the main
# accuracy/precision/recall numbers and for run_agreement's out-of-sample
# threshold. The p95-targeted split lives above in `_coverage_at_p95_split`.)
# ---------------------------------------------------------------------------

def _fit_best_accuracy_threshold(rows: list) -> float | None:
    """Threshold maximising accuracy on `rows` (the fit half). Candidate
    thresholds are the observed scores themselves -- exhaustive and exact
    for this n. Caller must already have confirmed the fit half has both
    label classes (`_class_balance_reason`) -- with only one class this
    would return a degenerate threshold that happens to clear an
    unconstraining bar (controller ruling, fix round 1, finding #3)."""
    usable = _usable_rows(rows)
    if not usable:
        return None
    candidates = sorted({r["score"] for r in usable})
    best_threshold, best_accuracy = candidates[0], -1.0
    for threshold in candidates:
        metrics = _metrics_at_threshold(usable, threshold)
        if metrics["accuracy"] is not None and metrics["accuracy"] > best_accuracy:
            best_accuracy = metrics["accuracy"]
            best_threshold = threshold
    return best_threshold


def _fit_threshold_split(rows_a: list, rows_b: list) -> dict:
    """Fit on one half, score on the other, both directions. Never fits and
    scores the same rows -- `rows_a`/`rows_b` must already be disjoint by id
    (the caller partitions via `split_half`). A fit half with fewer than one
    row of each label returns `threshold: None` with a reason for the whole
    direction (finding #3) rather than a degenerate threshold."""
    result = {}
    for name, fit_rows, score_rows in (
        ("a_to_b", rows_a, rows_b),
        ("b_to_a", rows_b, rows_a),
    ):
        reason = _class_balance_reason(fit_rows)
        if reason:
            result[name] = {
                "threshold": None, "n_fit": len(_usable_rows(fit_rows)),
                "n_score": len(_usable_rows(score_rows)),
                "accuracy": None, "precision": None, "recall": None,
                "reason": reason,
            }
            continue
        threshold = _fit_best_accuracy_threshold(fit_rows)
        metrics = _metrics_at_threshold(score_rows, threshold)
        result[name] = {
            "threshold": threshold,
            "n_fit": len(_usable_rows(fit_rows)),
            "n_score": metrics["n"],
            "accuracy": metrics["accuracy"],
            "precision": metrics["precision"],
            "recall": metrics["recall"],
        }
    return result


def _cross_fit_predictions(rows_a: list, rows_b: list, split_result: dict) -> list:
    """Pool out-of-sample predictions for every usable row: half A is scored
    by the threshold fit on half B and vice versa, so every row gets a
    prediction from a threshold that never saw it."""
    pooled = []
    threshold_b_to_a = split_result["b_to_a"]["threshold"]  # fit on B, used to score A
    threshold_a_to_b = split_result["a_to_b"]["threshold"]  # fit on A, used to score B
    if threshold_b_to_a is not None:
        pooled.extend(
            {**row, "_predicted": row["score"] >= threshold_b_to_a}
            for row in _usable_rows(rows_a)
        )
    if threshold_a_to_b is not None:
        pooled.extend(
            {**row, "_predicted": row["score"] >= threshold_a_to_b}
            for row in _usable_rows(rows_b)
        )
    return pooled


def _metrics_from_pooled_predictions(pooled: list) -> dict:
    if not pooled:
        return {"accuracy": None, "precision": None, "recall": None, "n": 0,
                "reason": "no split-half predictions available"}
    tp = fp = tn = fn = 0
    for row in pooled:
        predicted_positive = row["_predicted"]
        actual_positive = row["label"] == "yes"
        if predicted_positive and actual_positive:
            tp += 1
        elif predicted_positive and not actual_positive:
            fp += 1
        elif not predicted_positive and actual_positive:
            fn += 1
        else:
            tn += 1
    n = len(pooled)
    return {
        "accuracy": (tp + tn) / n,
        "precision": tp / (tp + fp) if (tp + fp) > 0 else None,
        "recall": tp / (tp + fn) if (tp + fn) > 0 else None,
        "n": n,
    }


# ---------------------------------------------------------------------------
# Platt scaling for the Brier comparison (fix wave 4, A5). A raw arm score
# that is not itself a calibrated probability -- threads' baseline is lexical
# similarity, not P(match) -- makes a squared-error (Brier) comparison
# against a Jev arm's real probability unfair in either direction. This
# fits a 1-feature L2 logistic regression (raw score -> label) on one split
# half and applies it to the other, cross-fit exactly like every other
# threshold/weight in this module -- never fit and scored on the same rows.
# Computed for every arm passed through `_score_one_arm` (cheap, one extra
# 1-D logistic fit per direction); only the BASELINE arm's version is read by
# `_compute_arm_verdict` for the paired Brier condition, per the brief
# ("do this for every set's baseline so the comparison is fair both ways") --
# a Jev arm's own `held_out_records` are already a probability (clause 2) and
# do not need it, but computing it for every arm keeps this function generic
# rather than baseline-specific.
# ---------------------------------------------------------------------------

def _platt_scale_direction(fit_rows: list, score_rows: list) -> dict:
    """Fit a 1-feature L2 logistic regression (raw `score` -> label) on
    `fit_rows`, apply it to `score_rows`. Returns `{"records": [...],
    "reason": str|None}`; `records` is empty with a reason when the fit half
    has fewer than one row of each label (mirrors `_class_balance_reason`
    everywhere else in this module)."""
    reason = _class_balance_reason(fit_rows)
    if reason:
        return {"records": [], "reason": reason}

    fit_usable = _usable_rows(fit_rows)
    score_usable = _usable_rows(score_rows)
    raw_fit = np.array([float(r["score"]) for r in fit_usable])
    # Fix wave 5, item 4: standardise on the FIT half (mean/sd of the fit
    # half only -- never the held-out half) and apply the same transform to
    # the held-out half. Wave 4 fitted the L2 logistic on the raw score, so
    # the penalty dominated any small-range score: an informative baseline
    # on a 0..0.1 scale calibrated to the base rate (Brier 0.253 vs 0.117
    # for the same information on any scale), biasing the paired Brier gate
    # toward Jev. A constant fit half (sd 0) carries no information: every
    # z is 0 and the fit reduces to the intercept (the fit half's base rate).
    mean = float(raw_fit.mean())
    sd = float(raw_fit.std())

    def _z(values: np.ndarray) -> np.ndarray:
        if sd <= 0.0:
            return np.zeros((len(values), 1))
        return ((values - mean) / sd).reshape(-1, 1)

    y_fit = np.array([_label_bit(r) for r in fit_usable], dtype=float)
    weights = _fit_logreg_l2(_z(raw_fit), y_fit, l2=PLATT_L2_PENALTY)

    if not score_usable:
        return {"records": [], "reason": None}
    raw_score = np.array([float(r["score"]) for r in score_usable])
    probs = _predict_logreg(_z(raw_score), weights).tolist()
    records = [
        {"id": r["id"], "label": r["label"], "score": float(p)}
        for r, p in zip(score_usable, probs)
    ]
    return {"records": records, "reason": None}


def _platt_scale_pooled(rows_a: list, rows_b: list) -> list:
    """Pooled Platt-scaled held-out predictions: half B calibrated by half
    A's fit, half A calibrated by half B's fit -- every usable row is
    calibrated by a fit that never saw it, same cross-fit shape as every
    other split-half computation here."""
    dir_ab = _platt_scale_direction(rows_a, rows_b)  # fit on A, score B
    dir_ba = _platt_scale_direction(rows_b, rows_a)  # fit on B, score A
    return dir_ab["records"] + dir_ba["records"]


# ---------------------------------------------------------------------------
# Decomposed composite-weight fitting (logistic regression, ruling #5)
# ---------------------------------------------------------------------------

def _extract_noul_features(rows: list) -> tuple:
    """Return (feature_names, X, y) for the rows that have a usable
    `answers` dict with noul sub-answers. Feature set is the intersection of
    noul keys present across all rows, sorted for determinism. Non-noul
    sub-answers (`_NON_NOUL_SUBANSWER_KEYS`, e.g. `status_claimed`) are
    excluded, per ruling #5."""
    usable = [r for r in _usable_rows(rows) if isinstance(r.get("answers"), dict)]
    if not usable:
        return [], None, None, []

    key_sets = []
    for row in usable:
        keys = {
            key for key, value in row["answers"].items()
            if key not in _NON_NOUL_SUBANSWER_KEYS
            and isinstance(value, dict) and "noul" in value
        }
        key_sets.append(keys)
    feature_names = sorted(set.intersection(*key_sets)) if key_sets else []
    if not feature_names:
        return [], None, None, []

    X = np.array(
        [[float(row["answers"][name]["noul"]) for name in feature_names] for row in usable]
    )
    y = np.array([_label_bit(row) for row in usable], dtype=float)
    return feature_names, X, y, usable


def _fit_logreg_l2(X: np.ndarray, y: np.ndarray, l2: float = L2_PENALTY) -> np.ndarray:
    """Plain-numpy logistic regression by gradient descent with an L2
    (ridge) penalty on the non-intercept weights. Returns weights
    [intercept, w_1, ..., w_d]. Deterministic (zero init, fixed iteration
    count) -- no randomness to seed."""
    n, d = X.shape
    X_aug = np.hstack([np.ones((n, 1)), X])
    weights = np.zeros(d + 1)
    for _ in range(_LOGREG_ITERS):
        z = X_aug @ weights
        p = 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))
        grad = X_aug.T @ (p - y) / n
        grad[1:] += (l2 / n) * weights[1:]
        weights -= _LOGREG_LR * grad
    return weights


def _predict_logreg(X: np.ndarray, weights: np.ndarray) -> np.ndarray:
    n = X.shape[0]
    X_aug = np.hstack([np.ones((n, 1)), X])
    z = X_aug @ weights
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


_EMPTY_P95 = {
    "threshold": None, "precision_on_fit_half": None,
    "coverage_on_held_out": 0, "precision_on_held_out": None,
}


def _fit_decomposed_direction(fit_rows: list, score_rows: list) -> dict:
    """Fits the composite weights AND the p95 threshold on `fit_rows`, both
    evaluated on `score_rows` (controller ruling, fix round 1: "the
    composite weights and the p95 threshold are both fitted on the same fit
    half and evaluated on the other"). A fit half with fewer than one row of
    each label (finding #3) short-circuits every threshold-dependent number
    in this direction, including p95."""
    score_usable_all = _usable_rows(score_rows)
    n_score_usable = len(score_usable_all)

    # Fix wave 4, A4: a failed fit half must not drop its held-out rows from
    # the paired comparison -- it keeps them with decision=False (mirroring
    # `_p95_direction`'s own reason branch) and the row's own stored v0
    # `score` as a fallback (there is no fitted composite to score it with).
    # Fix wave 5, item 7: these records are flagged `fallback` -- they keep
    # the coverage comparison honest (decision=False, never dropped) but
    # their v0 score is not the clause-2 probability, so the paired Brier
    # and the control check exclude them (counted, never silently).
    def _fallback_records() -> list:
        return [
            {"id": r["id"], "label": r["label"], "score": r["score"], "decision": False,
             "fallback": True}
            for r in score_usable_all
        ]

    class_reason = _class_balance_reason(fit_rows)
    if class_reason:
        return {
            "weights": {}, "n_fit": 0, "n_score": n_score_usable, "brier": None,
            "p95": {**_EMPTY_P95, "n_fit": 0, "n_held_out": n_score_usable, "reason": class_reason},
            "reason": class_reason,
            "_records": _fallback_records(),
        }

    fit_extraction = _extract_noul_features(fit_rows)
    if not fit_extraction[0]:
        reason = "fit half has no rows with usable noul answers"
        return {
            "weights": {}, "n_fit": 0, "n_score": n_score_usable, "brier": None,
            "p95": {**_EMPTY_P95, "n_fit": 0, "n_held_out": n_score_usable, "reason": reason},
            "reason": reason,
            "_records": _fallback_records(),
        }
    feature_names, X_fit, y_fit, _ = fit_extraction
    weights = _fit_logreg_l2(X_fit, y_fit)
    fit_predicted = _predict_logreg(X_fit, weights)
    threshold, precision_on_fit = _lowest_threshold_at_target_precision(
        fit_predicted.tolist(), [bool(v) for v in y_fit.tolist()]
    )

    score_extraction = _extract_noul_features(score_rows)
    if not score_extraction[0] or score_extraction[0] != feature_names:
        # Score half must offer the exact same feature set the fit used.
        reason = "score half is missing one or more fitted sub-answer keys"
        return {
            "weights": dict(zip(["intercept"] + feature_names, weights.tolist())),
            "n_fit": len(y_fit), "n_score": n_score_usable, "brier": None,
            "p95": {
                "threshold": threshold, "precision_on_fit_half": precision_on_fit,
                "coverage_on_held_out": 0, "precision_on_held_out": None,
                "n_fit": len(y_fit), "n_held_out": n_score_usable, "reason": reason,
            },
            "reason": reason,
            "_records": _fallback_records(),
        }
    _, X_score, y_score, score_usable_rows = score_extraction
    predicted_score = _predict_logreg(X_score, weights)
    brier = float(np.mean((predicted_score - y_score) ** 2))
    predicted_list = predicted_score.tolist()

    if threshold is None:
        p95 = {
            **_EMPTY_P95, "n_fit": len(y_fit), "n_held_out": len(y_score),
            "reason": "no threshold on fit half reaches 0.95 precision",
        }
        records = [
            {"id": r["id"], "label": r["label"], "score": float(p), "decision": False}
            for r, p in zip(score_usable_rows, predicted_list)
        ]
    else:
        accepted_idx = [i for i, p in enumerate(predicted_list) if p >= threshold]
        if accepted_idx:
            positives = sum(1 for i in accepted_idx if bool(y_score[i]))
            precision_held = positives / len(accepted_idx)
            coverage_held = len(accepted_idx) / len(y_score) if len(y_score) else 0.0
        else:
            positives = 0
            precision_held = None
            coverage_held = 0.0
        p95 = {
            "threshold": threshold,
            "precision_on_fit_half": precision_on_fit,
            "coverage_on_held_out": coverage_held,
            "precision_on_held_out": precision_held,
            "n_fit": len(y_fit),
            "n_held_out": len(y_score),
            "_pool": (len(accepted_idx), positives),
        }
        records = [
            {"id": r["id"], "label": r["label"], "score": float(p), "decision": p >= threshold}
            for r, p in zip(score_usable_rows, predicted_list)
        ]

    return {
        "weights": dict(zip(["intercept"] + feature_names, weights.tolist())),
        "n_fit": len(y_fit),
        "n_score": len(y_score),
        "brier": brier,
        "p95": p95,
        "_records": records,
    }


def _fit_decomposed(rows_a: list, rows_b: list) -> dict:
    v0_brier = brier_score(rows_a + rows_b)
    dir_ab = _fit_decomposed_direction(rows_a, rows_b)
    dir_ba = _fit_decomposed_direction(rows_b, rows_a)

    pooled_n = dir_ab["p95"]["n_held_out"] + dir_ba["p95"]["n_held_out"]
    pooled_accepted = 0
    pooled_positive = 0
    pooled_records: list = []
    for direction in (dir_ab, dir_ba):
        n_accepted, n_positive = direction["p95"].pop("_pool", (0, 0))
        pooled_accepted += n_accepted
        pooled_positive += n_positive
        pooled_records.extend(direction.pop("_records", []))

    if pooled_n == 0:
        p95_pooled = {"coverage": 0, "precision": None, "n": 0, "reason": "no usable rows"}
    elif pooled_accepted == 0:
        p95_pooled = {
            "coverage": 0, "precision": None, "n": pooled_n,
            "reason": "no threshold reached 0.95 precision on either fit half",
        }
    else:
        p95_pooled = {
            "coverage": pooled_accepted / pooled_n,
            "precision": pooled_positive / pooled_accepted,
            "n": pooled_n,
        }

    return {
        "a_to_b": dir_ab,
        "b_to_a": dir_ba,
        "p95_pooled": p95_pooled,
        "pooled_records": pooled_records,
        "v0_unfitted": {
            "brier": v0_brier,
            "n": len(_usable_rows(rows_a + rows_b)),
            "description": "brier of the stored v0 composite score (questions.py), for comparison",
        },
    }


# ---------------------------------------------------------------------------
# Control check (ruling #6)
# ---------------------------------------------------------------------------

def control_check(real_rows: list, control_rows: list) -> dict:
    real_yes = [r for r in _usable_rows(real_rows) if r["label"] == "yes"]
    control_yes = [r for r in _usable_rows(control_rows) if r["label"] == "yes"]

    if not real_yes or not control_yes:
        return {
            "result": "unreachable",
            "reason": "no label=='yes' rows on one side" if (real_yes or control_yes)
                      else "no label=='yes' rows on either side",
            "real_mean": (sum(r["score"] for r in real_yes) / len(real_yes)) if real_yes else None,
            "control_mean": (sum(r["score"] for r in control_yes) / len(control_yes)) if control_yes else None,
            "n_real": len(real_yes),
            "n_control": len(control_yes),
        }

    real_mean = sum(r["score"] for r in real_yes) / len(real_yes)
    control_mean = sum(r["score"] for r in control_yes) / len(control_yes)
    margin = real_mean - control_mean

    return {
        "result": "pass" if margin >= CONTROL_MARGIN else "fail",
        "real_mean": real_mean,
        "control_mean": control_mean,
        "margin": margin,
        "n_real": len(real_yes),
        "n_control": len(control_yes),
    }


# ---------------------------------------------------------------------------
# score_set
# ---------------------------------------------------------------------------

_CONTROL_PAIRS = (("broad", "control_broad"), ("decomposed", "control_decomposed"))


def _score_one_arm(arm: str, runs: dict, policy) -> dict:
    run1 = runs.get(1, [])
    run2 = runs.get(2, [])
    usable1 = _usable_rows(run1)
    n_failed = len(run1) - len(usable1)

    brier = brier_score(usable1)
    ece = ece10(usable1)
    in_sample_coverage, in_sample_threshold = coverage_at_p95(usable1)

    ids = [r["id"] for r in usable1]
    half_a_ids, half_b_ids = split_half(ids)
    rows_a = [r for r in usable1 if r["id"] in half_a_ids]
    rows_b = [r for r in usable1 if r["id"] in half_b_ids]

    threshold_split = _fit_threshold_split(rows_a, rows_b)

    if arm == "decomposed":
        decomposed_fit = _fit_decomposed(rows_a, rows_b)
        p95_a_to_b = decomposed_fit["a_to_b"]["p95"]
        p95_b_to_a = decomposed_fit["b_to_a"]["p95"]
        p95_pooled = decomposed_fit["p95_pooled"]
        held_out_records = decomposed_fit.pop("pooled_records")
        decomposed_weights = {
            "a_to_b": decomposed_fit["a_to_b"].get("weights") or None,
            "b_to_a": decomposed_fit["b_to_a"].get("weights") or None,
        }
    else:
        decomposed_fit = None
        p95_split = _coverage_at_p95_split(rows_a, rows_b)
        p95_a_to_b = p95_split["a_to_b"]
        p95_b_to_a = p95_split["b_to_a"]
        p95_pooled = p95_split["pooled"]
        held_out_records = p95_split["pooled_records"]
        decomposed_weights = None

    coverage_at_p95_result = {
        "pooled": p95_pooled,
        "a_to_b": p95_a_to_b,
        "b_to_a": p95_b_to_a,
        "in_sample": {"coverage": in_sample_coverage, "threshold": in_sample_threshold},
    }
    threshold_at_p95 = {
        "a_to_b": p95_a_to_b.get("threshold"),
        "b_to_a": p95_b_to_a.get("threshold"),
    }

    if isinstance(policy, str):
        if policy != "fit":
            raise JevScoreError(
                f"unsupported threshold_policy string {policy!r} for arm {arm!r}; "
                "expected a number or the literal string 'fit'")
        fixed_metrics = None
        main_threshold = None
        pooled = _cross_fit_predictions(rows_a, rows_b, threshold_split)
        main_metrics = _metrics_from_pooled_predictions(pooled)
    elif isinstance(policy, (int, float)) and not isinstance(policy, bool):
        main_threshold = float(policy)
        main_metrics = _metrics_at_threshold(usable1, main_threshold)
        fixed_metrics = dict(main_metrics)
        fixed_metrics["threshold"] = main_threshold
    else:
        raise JevScoreError(
            f"threshold_policy for arm {arm!r} must be a number or 'fit', got {policy!r}")

    if main_threshold is not None:
        # A fixed threshold is not fit from this data, so there is no
        # held-out concern -- score every common id against it directly.
        run_agree = run_agreement(run1, run2, main_threshold)
    else:
        # "fit" policy: each half must be scored with the OTHER half's
        # threshold (finding #2) -- never the threshold it was fit on.
        run_agree = _run_agreement_split(run1, run2, half_a_ids, half_b_ids, threshold_split)

    # Amended rule clause 7: stability is flips at the SAME held-out p95
    # operating point coverage was judged at -- never the accuracy-maximising
    # threshold `run_agreement`/`_run_agreement_split` above use. For the
    # decomposed arm, run 2's rows must be scored through the SAME fitted
    # composite weights run 1's fit half produced (clause 2/g) -- never
    # run 2's own v0 composite `score`.
    stability = stability_at_p95(
        run1, run2, half_a_ids, half_b_ids, threshold_at_p95,
        decomposed_weights=decomposed_weights,
    )

    n_yes = sum(1 for r in usable1 if r["label"] == "yes")
    n_no = sum(1 for r in usable1 if r["label"] == "no")

    # Fix wave 4, A5: Platt-scaled (calibrated) held-out score for every arm,
    # cross-fit the same way as everything else -- only the baseline arm's
    # version is actually read (by `_compute_arm_verdict`'s paired Brier
    # condition), but computed here for every arm so this stays generic.
    brier_calibrated_records = _platt_scale_pooled(rows_a, rows_b)

    result = {
        "n": len(usable1),
        "n_failed": n_failed,
        "n_yes": n_yes,
        "n_no": n_no,
        "accuracy": main_metrics.get("accuracy"),
        "precision": main_metrics.get("precision"),
        "recall": main_metrics.get("recall"),
        "brier": brier,
        "ece10": ece,
        "run_agreement": run_agree,
        "coverage_at_p95": coverage_at_p95_result,
        "threshold_at_p95": threshold_at_p95,
        "fixed_threshold": fixed_metrics,
        "split_half": threshold_split,
        "held_out_records": held_out_records,
        "brier_calibrated_records": brier_calibrated_records,
        "stability": stability,
    }
    if decomposed_fit is not None:
        result["decomposed_fit"] = decomposed_fit
    return result


def _decomposed_control_records(
    control_rows: list, half_a_ids: set, half_b_ids: set,
    weights_a_to_b: dict | None, weights_b_to_a: dict | None,
) -> list:
    """Fix wave 4, A3: score the CONTROL arm's rows through the SAME
    held-out cross-fit weights the real `decomposed` arm's fit halves
    produced -- a control id in half B is scored with the weights fit on the
    real arm's half A (`weights_a_to_b`), and a control id in half A with the
    weights fit on the real arm's half B (`weights_b_to_a`), mirroring
    exactly how `_fit_decomposed` pools the real arm's own held-out
    predictions. Without this, the control check would compare the real
    arm's cross-fit probability against the control arm's raw, never-refit
    v0 composite score -- two different scores about two different models,
    which is exactly the inconsistency the amended rule's clause 2 exists to
    remove. A control row outside both halves, or missing a fitted feature
    key, is skipped (never fabricated)."""
    usable = _usable_rows(control_rows)
    out = []
    for row in usable:
        if row["id"] in half_a_ids:
            weights = weights_b_to_a
        elif row["id"] in half_b_ids:
            weights = weights_a_to_b
        else:
            continue
        score = _decomposed_row_score(row, weights)
        if score is None:
            continue
        out.append({"id": row["id"], "label": row["label"], "score": score})
    return out


def score_set(rows_by_arm_run: dict, threshold_policy: dict | None = None) -> dict:
    """See module docstring. `threshold_policy` maps arm -> fixed threshold
    (number) or "fit"; an arm not present defaults to "fit"."""
    threshold_policy = threshold_policy or {}
    result: dict = {}

    for arm, runs in rows_by_arm_run.items():
        policy = threshold_policy.get(arm, "fit")
        result[arm] = _score_one_arm(arm, runs, policy)

    control_checks = {}
    for real_arm, control_arm in _CONTROL_PAIRS:
        if real_arm in rows_by_arm_run and control_arm in rows_by_arm_run:
            # Fix wave 4, A3: the control check reads the SAME score every
            # other gate reads for that arm (clause 2) -- the real arm's own
            # cross-fit held-out records, and, for `decomposed`, the control
            # arm's sub-answers run through that SAME fit (never the control
            # arm's raw v0 composite `score`). `broad` needs no such
            # transform: its held-out records already ARE the raw
            # probability (clause 2 reads it directly), so the control side
            # stays its own raw run-1 rows, unchanged from before.
            real_records = result[real_arm].get("held_out_records", [])
            if real_arm == "decomposed":
                control_run1 = rows_by_arm_run[control_arm].get(1, [])
                real_run1_usable = _usable_rows(rows_by_arm_run[real_arm].get(1, []))
                half_a_ids, half_b_ids = split_half([r["id"] for r in real_run1_usable])
                decomposed_fit = result[real_arm].get("decomposed_fit") or {}
                weights_a_to_b = decomposed_fit.get("a_to_b", {}).get("weights") or None
                weights_b_to_a = decomposed_fit.get("b_to_a", {}).get("weights") or None
                control_records = _decomposed_control_records(
                    control_run1, half_a_ids, half_b_ids, weights_a_to_b, weights_b_to_a)
            else:
                control_records = _usable_rows(rows_by_arm_run[control_arm].get(1, []))
            # Fix wave 5, item 7: both sides are judged on the SAME ids --
            # never a real side that still carries a failed direction's v0
            # fallback rows the control side could not be scored on.
            real_usable = [r for r in real_records if not r.get("fallback")]
            shared_ids = {r["id"] for r in real_usable} & {r["id"] for r in control_records}
            real_paired = [r for r in real_usable if r["id"] in shared_ids]
            control = control_check(
                real_paired, [r for r in control_records if r["id"] in shared_ids])
            control["n_excluded_unpaired"] = len(real_records) - len(real_paired)
            control_checks[real_arm] = control
    result["_control_checks"] = control_checks

    return result


# ---------------------------------------------------------------------------
# Amended decision rule (2026-09-28 findings-doc amendment, owner-approved
# before any result exists -- see wave1-brief.md). Original wording is kept,
# superseded, in the findings doc; this module computes the OPERATIVE rule.
#
# Clause 2 (one score per row -- the cross-fit held-out probability):
#   - `broad`/`baseline`: the row's own `score` (already a probability /
#     the deployed gate's stored output) -- never re-fit.
#   - `decomposed`: the L2-logistic refit's held-out prediction, i.e. each
#     half's rows scored by the OTHER half's fitted weights (`_fit_decomposed`
#     already computes exactly this; `_score_one_arm` pools it into
#     `held_out_records`).
# `held_out_records` (`_score_one_arm`'s output) is therefore already the
# clause-2 score for every arm, in the shape `{"id", "label", "score",
# "decision"}` -- `decision` is that row's accept/reject at the SAME p95
# operating point `coverage_at_p95` was judged at (clause 3), or `False`
# when no fit-half threshold reached 0.95 precision.
# ---------------------------------------------------------------------------


def _decomposed_row_score(row: dict, weights_dict: dict | None) -> float | None:
    """Apply a fitted decomposed-composite `weights_dict` (as stored in
    `decomposed_fit[direction]["weights"]`, an ordered {"intercept": ...,
    <feature>: ...} map) to `row`'s own noul sub-answers. Returns `None` if
    there are no weights to apply, or `row` is missing one of the fitted
    feature keys -- never a fabricated score."""
    if not weights_dict:
        return None
    feature_names = [key for key in weights_dict if key != "intercept"]
    answers = row.get("answers")
    if not isinstance(answers, dict):
        return None
    try:
        x = np.array([[float(answers[name]["noul"]) for name in feature_names]])
    except (KeyError, TypeError):
        return None
    weights = np.array([weights_dict["intercept"]] + [weights_dict[name] for name in feature_names])
    return float(_predict_logreg(x, weights)[0])


def stability_at_p95(
    run1_rows: list, run2_rows: list, half_a_ids: set, half_b_ids: set,
    p95_thresholds: dict, decomposed_weights: dict | None = None,
) -> dict:
    """Amended rule clause 7 (replaces `run_agreement >= 0.95`): run 1 vs
    run 2 decisions at the SAME held-out p95 operating point coverage was
    judged at -- never the accuracy-maximising threshold `run_agreement`/
    `_run_agreement_split` use. `p95_thresholds` = `{"a_to_b": ..., "b_to_a":
    ...}` (a half-A id is judged by the threshold fit on B, i.e.
    `p95_thresholds["b_to_a"]`, and vice versa -- mirrors `_run_agreement_split`).

    For the decomposed arm, `decomposed_weights` (same shape, each value the
    `weights` dict that direction's fit produced, or `None` if that
    direction had no fit) must be passed so run 2's rows are scored through
    the SAME fitted composite run 1's fit half produced -- never run 2's own
    v0 composite `score` (clause 2/g). For every other arm, `decomposed_
    weights=None` and both runs' raw `score` fields are compared directly.

    Also reports `identical_answer_fraction` -- the fraction of common ids
    whose `answers` were byte-identical across runs (a provider cache would
    make this 1.0, which is itself a finding, per the brief)."""
    usable1 = {r["id"]: r for r in _usable_rows(run1_rows)}
    usable2 = {r["id"]: r for r in _usable_rows(run2_rows)}
    common_ids = sorted(set(usable1) & set(usable2), key=str)

    if not common_ids:
        return {
            "flips": None, "n": 0, "allowed_flips": None, "pass": None,
            "identical_answer_fraction": None,
            "reason": "no overlapping usable ids between runs",
        }

    flips = 0
    n_scored = 0
    identical = 0
    for id_ in common_ids:
        if id_ in half_a_ids:
            threshold = p95_thresholds.get("b_to_a")  # fit on B, scores A
            weights = (decomposed_weights or {}).get("b_to_a") if decomposed_weights is not None else None
        elif id_ in half_b_ids:
            threshold = p95_thresholds.get("a_to_b")  # fit on A, scores B
            weights = (decomposed_weights or {}).get("a_to_b") if decomposed_weights is not None else None
        else:
            continue
        if threshold is None:
            continue

        r1, r2 = usable1[id_], usable2[id_]
        if decomposed_weights is not None:
            score1 = _decomposed_row_score(r1, weights)
            score2 = _decomposed_row_score(r2, weights)
            if score1 is None or score2 is None:
                continue
        else:
            score1, score2 = r1["score"], r2["score"]

        decision1 = score1 >= threshold
        decision2 = score2 >= threshold
        if decision1 != decision2:
            flips += 1
        n_scored += 1
        if r1.get("answers") == r2.get("answers"):
            identical += 1

    if n_scored == 0:
        return {
            "flips": None, "n": 0, "allowed_flips": None, "pass": None,
            "identical_answer_fraction": None,
            "reason": "no common ids had an out-of-sample p95 threshold available",
        }

    allowed = max(1, math.floor(0.05 * n_scored))
    return {
        "flips": flips,
        "n": n_scored,
        "allowed_flips": allowed,
        "pass": flips <= allowed,
        "identical_answer_fraction": identical / n_scored,
    }


# ---------------------------------------------------------------------------
# Clause 1 (eligibility, replaces clause f): >= 20 usable rows of EACH class,
# for BOTH the Jev arm being judged and the baseline it is compared against
# -- a set is not eligible for a verdict just because one side clears the
# bar while the other is thin.
# ---------------------------------------------------------------------------

def _eligibility(jev_arm_result: dict, baseline_arm_result: dict) -> dict:
    counts = {
        "jev_n_yes": jev_arm_result.get("n_yes", 0),
        "jev_n_no": jev_arm_result.get("n_no", 0),
        "baseline_n_yes": baseline_arm_result.get("n_yes", 0),
        "baseline_n_no": baseline_arm_result.get("n_no", 0),
    }
    eligible = all(v >= ELIGIBILITY_MIN_PER_CLASS for v in counts.values())
    reason = None
    if not eligible:
        detail = ", ".join(f"{key}={value}" for key, value in counts.items())
        reason = (
            f"fewer than {ELIGIBILITY_MIN_PER_CLASS} usable rows of some class ({detail})"
        )
    return {"eligible": eligible, "reason": reason, **counts}


# ---------------------------------------------------------------------------
# Clauses 4/5: precision floor + seeded paired bootstrap over held-out rows.
# ---------------------------------------------------------------------------

def _coverage_and_precision(records: dict, ids: list) -> tuple:
    accepted = 0
    positive = 0
    total = 0
    for id_ in ids:
        rec = records[id_]
        total += 1
        if rec["decision"]:
            accepted += 1
            if rec["label"] == "yes":
                positive += 1
    if total == 0:
        return 0.0, None
    coverage = accepted / total
    precision = (positive / accepted) if accepted > 0 else None
    return coverage, precision


def _floored_coverage(records: dict, ids: list, use_clopper_pearson: bool = True) -> tuple:
    """(coverage-or-0, precision, floor_met, precision_lower_bound) -- clause 4.

    On the POINT estimate (`use_clopper_pearson=True`, fix wave 4 A1) the
    floor is the one-sided 95% Clopper-Pearson LOWER bound of held-out
    precision (29/29 gives 0.902 and passes; 28/28 gives 0.8985 and fails;
    with one error 46 accepted rows are needed). Inside each bootstrap rep
    (`use_clopper_pearson=False`, fix wave 5 item 3) the floor is the RAW
    resampled precision >= 0.90: the bootstrap already carries the sampling
    uncertainty, and applying the CP bound inside every rep as well counted
    it twice -- a 64/65-correct model lost its coverage CI because the reps
    that resampled 3 false positives fell below the CP bound. `precision`
    (raw point estimate) is always returned. Below the floor -- including 0
    accepted rows -- the arm's coverage counts as 0."""
    coverage, precision = _coverage_and_precision(records, ids)
    accepted = sum(1 for id_ in ids if records[id_]["decision"])
    positive = sum(
        1 for id_ in ids if records[id_]["decision"] and records[id_]["label"] == "yes"
    )
    if not use_clopper_pearson:
        floor_met = precision is not None and precision >= PRECISION_FLOOR
        return (coverage if floor_met else 0.0), precision, floor_met, None
    lower_bound = clopper_pearson_lower(positive, accepted) if accepted > 0 else None
    floor_met = lower_bound is not None and lower_bound >= PRECISION_FLOOR
    return (coverage if floor_met else 0.0), precision, floor_met, lower_bound


def _bootstrap_coverage_diff(
    jev_records: dict, baseline_records: dict,
    n_reps: int = BOOTSTRAP_REPS, seed: int = BOOTSTRAP_SEED,
) -> dict:
    """Clause 5: a seeded paired bootstrap over row ids (2000 reps, seed
    fixed and recorded) of `coverage_Jev - coverage_baseline`, resampling the
    pooled held-out predictions and recomputing coverage AND the precision
    floor (clause 4) inside every rep -- an arm whose resampled precision
    falls below 0.90 in a given rep contributes 0 coverage for that rep, not
    its raw (possibly noise-inflated) coverage number."""
    jev_records = {k: v for k, v in jev_records.items() if v is not None}
    baseline_records = {k: v for k, v in baseline_records.items() if v is not None}
    common_ids = sorted(set(jev_records) & set(baseline_records), key=str)

    if not common_ids:
        return {
            "point_estimate": None, "ci_90": [None, None], "n": 0,
            "n_reps": n_reps, "seed": seed,
            "jev_precision_floor_met": False, "baseline_precision_floor_met": False,
            "reason": "no overlapping held-out ids between the two arms",
        }

    jev_point_cov, jev_precision, jev_floor_met, jev_lower = _floored_coverage(
        jev_records, common_ids)
    base_point_cov, base_precision, base_floor_met, base_lower = _floored_coverage(
        baseline_records, common_ids)
    point_estimate = jev_point_cov - base_point_cov

    rng = np.random.default_rng(seed)
    n = len(common_ids)
    ids_arr = np.array(common_ids, dtype=object)
    diffs = np.empty(n_reps)
    for i in range(n_reps):
        sample_ids = ids_arr[rng.integers(0, n, size=n)].tolist()
        jc, _, _, _ = _floored_coverage(jev_records, sample_ids, use_clopper_pearson=False)
        bc, _, _, _ = _floored_coverage(baseline_records, sample_ids, use_clopper_pearson=False)
        diffs[i] = jc - bc
    lo, hi = np.percentile(diffs, [5.0, 95.0])

    return {
        "point_estimate": float(point_estimate),
        "ci_90": [float(lo), float(hi)],
        "n": n, "n_reps": n_reps, "seed": seed,
        "jev_coverage": float(jev_point_cov), "baseline_coverage": float(base_point_cov),
        "jev_precision": jev_precision, "baseline_precision": base_precision,
        "jev_precision_lower_bound": jev_lower, "baseline_precision_lower_bound": base_lower,
        "jev_precision_floor_met": jev_floor_met,
        "baseline_precision_floor_met": base_floor_met,
    }


# ---------------------------------------------------------------------------
# Clause 6: paired Brier (replaces ECE10 <= 0.10).
# ---------------------------------------------------------------------------

def _bootstrap_brier_diff(
    jev_records: dict, baseline_records: dict,
    n_reps: int = BOOTSTRAP_REPS, seed: int = BOOTSTRAP_SEED,
) -> dict:
    """Clause 6: paired Brier_Jev - Brier_baseline on the SAME held-out rows
    (never each arm's full, differently-failed set), point estimate plus the
    bootstrap 90% CI of the difference -- same seeded paired resampling as
    the coverage-difference bootstrap."""
    jev_records = {k: v for k, v in jev_records.items() if v is not None}
    baseline_records = {k: v for k, v in baseline_records.items() if v is not None}
    # Fix wave 5, item 7: a failed decomposed direction's v0 fallback rows
    # are not the clause-2 probability -- excluded from the Brier, counted.
    fallback_ids = {k for k, v in jev_records.items() if v.get("fallback")}
    jev_records = {k: v for k, v in jev_records.items() if k not in fallback_ids}
    n_excluded_fallback = len(fallback_ids & set(baseline_records))
    common_ids = sorted(set(jev_records) & set(baseline_records), key=str)

    if not common_ids:
        return {
            "point_estimate": None, "ci_90": [None, None], "n": 0,
            "n_reps": n_reps, "seed": seed, "n_excluded_fallback": n_excluded_fallback,
            "reason": "no overlapping held-out ids between the two arms",
        }

    def _label_bit_of(rec):
        return 1.0 if rec["label"] == "yes" else 0.0

    jev_brier = np.array([(jev_records[i]["score"] - _label_bit_of(jev_records[i])) ** 2 for i in common_ids])
    base_brier = np.array(
        [(baseline_records[i]["score"] - _label_bit_of(baseline_records[i])) ** 2 for i in common_ids]
    )
    point_estimate = float(np.mean(jev_brier) - np.mean(base_brier))

    rng = np.random.default_rng(seed)
    n = len(common_ids)
    diffs = np.empty(n_reps)
    for i in range(n_reps):
        idx = rng.integers(0, n, size=n)
        diffs[i] = np.mean(jev_brier[idx]) - np.mean(base_brier[idx])
    lo, hi = np.percentile(diffs, [5.0, 95.0])

    return {
        "point_estimate": point_estimate,
        "ci_90": [float(lo), float(hi)],
        "n": n, "n_reps": n_reps, "seed": seed,
        "jev_brier": float(np.mean(jev_brier)), "baseline_brier": float(np.mean(base_brier)),
        "n_excluded_fallback": n_excluded_fallback,
    }


# ---------------------------------------------------------------------------
# Clause 9: the verdict, applied mechanically.
# ---------------------------------------------------------------------------

def _compute_arm_verdict(
    jev_arm_key: str, scores: dict,
    n_reps: int = BOOTSTRAP_REPS, seed: int = BOOTSTRAP_SEED,
) -> dict:
    """Runs clauses 1-9 for one Jev arm (`jev_arm_key`) against `baseline`.
    Used for `decomposed` (the rule's subject -- drives `verdict()`'s
    top-level result) and, per the brief, for `broad` too (reported
    alongside using the same machinery, but never overriding the top-level
    verdict, which is `decomposed`'s alone). `n_reps`/`seed` are passed
    through to both bootstraps -- production always uses the module defaults;
    tests may pass a smaller `n_reps` to keep a Monte-Carlo sweep over many
    seeds fast (fix wave 4, A8), never by reducing it in this module itself."""
    if jev_arm_key not in scores or "baseline" not in scores:
        return {
            "verdict": "descriptive_only",
            "reasons": [f"missing {jev_arm_key!r} or 'baseline' arm in scores"],
            "eligibility": None, "control": None,
            "coverage_diff": None, "brier": None, "stability": None,
        }

    jev = scores[jev_arm_key]
    baseline = scores["baseline"]
    reasons: list = []

    eligibility = _eligibility(jev, baseline)
    if not eligibility["eligible"]:
        return {
            "verdict": "descriptive_only",
            "reasons": [eligibility["reason"]],
            "eligibility": eligibility, "control": None,
            "coverage_diff": None, "brier": None, "stability": None,
        }

    control = scores.get("_control_checks", {}).get(jev_arm_key)
    control_result = control["result"] if control else "unreachable"
    control_pass = control_result == "pass"
    if not control_pass:
        reasons.append(f"control check for {jev_arm_key!r}: {control_result!r} (need 'pass')")

    jev_records = {rec["id"]: rec for rec in jev.get("held_out_records", [])}
    base_records = {rec["id"]: rec for rec in baseline.get("held_out_records", [])}
    # Fix wave 4, A5: the baseline side of the paired Brier comparison reads
    # its Platt-scaled (cross-fit calibrated) score, not its raw stored
    # score -- a raw score that is not itself a probability (e.g. threads'
    # lexical similarity) makes squared-error-against-the-label an unfair
    # comparison against a Jev arm's real probability. The Jev arm's own
    # `held_out_records` are already a probability (clause 2) and need no
    # such transform.
    base_brier_records = {
        rec["id"]: rec for rec in baseline.get("brier_calibrated_records", [])
    }

    coverage_diff = _bootstrap_coverage_diff(jev_records, base_records, n_reps=n_reps, seed=seed)
    # Clause 4 gates the JEV arm's own operating point -- an accepted
    # decision must actually meet the deployed precision bar to be worth
    # replacing/augmenting anything with. The baseline's floor is not a
    # verdict condition in its own right: when the baseline misses it, its
    # coverage is correctly counted as 0 inside the diff (clause 4/5), and
    # the coverage-difference CI (not a second floor check) is what decides
    # whether that makes Jev look better for a real reason or by noise.
    precision_floor_met = bool(coverage_diff.get("jev_precision_floor_met"))
    if not precision_floor_met:
        reasons.append(
            f"held-out precision floor (>= {PRECISION_FLOOR}) not met by {jev_arm_key!r}"
        )

    brier = _bootstrap_brier_diff(jev_records, base_brier_records, n_reps=n_reps, seed=seed)
    brier_pass = brier.get("point_estimate") is not None and brier["point_estimate"] <= 0
    if brier.get("point_estimate") is None:
        reasons.append("paired Brier undefined: no overlapping held-out ids between the arms")

    stability = jev.get("stability", {}) or {}
    stability_pass = stability.get("pass") is True
    if not stability_pass:
        reasons.append("stability check failed or undefined (flips exceed the allowance)")

    # Fix wave 4, A2: augment now needs the SAME coverage-difference CI
    # lower-bound-positive condition replace does (not merely a nonnegative
    # point estimate, which a noisy/uninformative model clears far too
    # easily) -- replace = augment's conditions + the paired Brier condition.
    ci_lower = (coverage_diff.get("ci_90") or [None, None])[0]
    coverage_ci_lower_positive = ci_lower is not None and ci_lower > 0

    augment_conditions_met = (
        control_pass and precision_floor_met and coverage_ci_lower_positive and stability_pass
    )
    if augment_conditions_met and brier_pass:
        result = "replace"
    elif augment_conditions_met:
        result = "augment"
        reasons.append("paired Brier condition failed (allowed for augment)")
    else:
        result = "not_adopted"
        coverage_point = coverage_diff.get("point_estimate")
        if (control_pass and precision_floor_met and stability_pass
                and coverage_point is not None and not coverage_ci_lower_positive):
            reasons.append(
                "coverage-difference 90% CI lower bound is not > 0 "
                f"(point_estimate={coverage_point}, ci_90={coverage_diff.get('ci_90')})"
            )

    return {
        "verdict": result,
        "reasons": reasons,
        "eligibility": eligibility,
        "control": control,
        "coverage_diff": coverage_diff,
        "brier": brier,
        "stability": stability,
    }


def verdict(
    scores: dict, n_reps: int = BOOTSTRAP_REPS, seed: int = BOOTSTRAP_SEED,
) -> dict:
    """Clause 9, applied mechanically. `scores` is `score_set(...)`'s own
    output. The Jev arm judged is `decomposed` (the rule's subject); `broad`
    is computed the same way and reported alongside under
    `inputs["broad"]`, per the brief, but never changes the top-level
    `verdict`/`reasons`, which are `decomposed`'s alone. `n_reps`/`seed`
    default to the production bootstrap settings; pass a smaller `n_reps`
    only from a test that needs to sweep many seeds quickly (fix wave 4,
    A8) -- production call sites never override this."""
    decomposed_inputs = _compute_arm_verdict("decomposed", scores, n_reps=n_reps, seed=seed)
    broad_inputs = (
        _compute_arm_verdict("broad", scores, n_reps=n_reps, seed=seed)
        if "broad" in scores else None
    )
    return {
        "verdict": decomposed_inputs["verdict"],
        "reasons": decomposed_inputs["reasons"],
        "inputs": {"decomposed": decomposed_inputs, "broad": broad_inputs},
    }
