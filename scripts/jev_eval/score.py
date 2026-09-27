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
from typing import Any

import numpy as np

# ---------------------------------------------------------------------------
# Constants (brief's exact values)
# ---------------------------------------------------------------------------

ECE_N_BINS = 10
COVERAGE_PRECISION_TARGET = 0.95
CONTROL_MARGIN = 0.2

# L2 (ridge) penalty for the decomposed-weight logistic regression. With as
# few as ~13 rows per split half, an unregularised fit on any near-separable
# label pattern diverges (weights -> +/-inf); this keeps it bounded. Not
# tuned against real data yet -- revisit once owner labelling grows n.
L2_PENALTY = 1.0
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
    return min(qualifying, key=lambda pair: pair[0])


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

    return {
        "threshold": threshold,
        "precision_on_fit_half": precision_on_fit,
        "coverage_on_held_out": coverage_held,
        "precision_on_held_out": precision_held,
        "n_fit": len(fit_usable),
        "n_held_out": len(score_usable),
        "_pool": (len(accepted), positives),
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
    for direction in (dir_ab, dir_ba):
        n_accepted, n_positive = direction.pop("_pool", (0, 0))
        pooled_accepted += n_accepted
        pooled_positive += n_positive

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

    return {"a_to_b": dir_ab, "b_to_a": dir_ba, "pooled": pooled}


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
    n_score_usable = len(_usable_rows(score_rows))

    class_reason = _class_balance_reason(fit_rows)
    if class_reason:
        return {
            "weights": {}, "n_fit": 0, "n_score": 0, "brier": None,
            "p95": {**_EMPTY_P95, "n_fit": 0, "n_held_out": n_score_usable, "reason": class_reason},
            "reason": class_reason,
        }

    fit_extraction = _extract_noul_features(fit_rows)
    if not fit_extraction[0]:
        reason = "fit half has no rows with usable noul answers"
        return {
            "weights": {}, "n_fit": 0, "n_score": 0, "brier": None,
            "p95": {**_EMPTY_P95, "n_fit": 0, "n_held_out": n_score_usable, "reason": reason},
            "reason": reason,
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
            "n_fit": len(y_fit), "n_score": 0, "brier": None,
            "p95": {
                "threshold": threshold, "precision_on_fit_half": precision_on_fit,
                "coverage_on_held_out": 0, "precision_on_held_out": None,
                "n_fit": len(y_fit), "n_held_out": n_score_usable, "reason": reason,
            },
            "reason": reason,
        }
    _, X_score, y_score, _ = score_extraction
    predicted_score = _predict_logreg(X_score, weights)
    brier = float(np.mean((predicted_score - y_score) ** 2))

    if threshold is None:
        p95 = {
            **_EMPTY_P95, "n_fit": len(y_fit), "n_held_out": len(y_score),
            "reason": "no threshold on fit half reaches 0.95 precision",
        }
    else:
        predicted_list = predicted_score.tolist()
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

    return {
        "weights": dict(zip(["intercept"] + feature_names, weights.tolist())),
        "n_fit": len(y_fit),
        "n_score": len(y_score),
        "brier": brier,
        "p95": p95,
    }


def _fit_decomposed(rows_a: list, rows_b: list) -> dict:
    v0_brier = brier_score(rows_a + rows_b)
    dir_ab = _fit_decomposed_direction(rows_a, rows_b)
    dir_ba = _fit_decomposed_direction(rows_b, rows_a)

    pooled_n = dir_ab["p95"]["n_held_out"] + dir_ba["p95"]["n_held_out"]
    pooled_accepted = 0
    pooled_positive = 0
    for direction in (dir_ab, dir_ba):
        n_accepted, n_positive = direction["p95"].pop("_pool", (0, 0))
        pooled_accepted += n_accepted
        pooled_positive += n_positive

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
    else:
        decomposed_fit = None
        p95_split = _coverage_at_p95_split(rows_a, rows_b)
        p95_a_to_b = p95_split["a_to_b"]
        p95_b_to_a = p95_split["b_to_a"]
        p95_pooled = p95_split["pooled"]

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

    result = {
        "n": len(usable1),
        "n_failed": n_failed,
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
    }
    if decomposed_fit is not None:
        result["decomposed_fit"] = decomposed_fit
    return result


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
            real_rows = rows_by_arm_run[real_arm].get(1, [])
            control_rows = rows_by_arm_run[control_arm].get(1, [])
            control_checks[real_arm] = control_check(real_rows, control_rows)
    result["_control_checks"] = control_checks

    return result
