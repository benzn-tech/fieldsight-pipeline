# Jev shadow evaluation — findings (Track A, Task 8)

**Date written:** 2026-09-28
**Status:** Decision rule pre-registered; no results yet.
**Written on commit:** `9b75d93`
**Rule freeze:** Section 2 (the decision rule and its operational definitions) must not be
edited once the first results row exists in Section 5. Any later change to how a verdict is
computed goes in a dated addendum appended after Section 2, never as an edit in place.

This is Step 1 of Task 8 only: the rule, the label-set inventory, and the privacy conditions,
written before any real Jev API call has been made (Task 6 has not run). Sections 5, 6 and 8
are placeholders, filled after real runs (Task 6/7).

---

## 1. Scope

Covers the sets in scope for Track A's shadow evaluation: `programme_match`, `threads`,
`work_class`. `name_aliases` is tracked for completeness (it has labels) but was not in the
plan's arm list and is not scored under this rule unless a later addendum adds it.

---

## 2. Pre-registered decision rule

**Amended 2026-09-28, before any result exists.** Reason: an independent review found the
original rule runs backwards at these sizes -- see the review's measured evidence below. Owner
decision (2026-09-28): amend the rule now, while no results exist yet, per
`.superpowers/sdd/2026-09-24-track-a-jev-shadow-eval/wave1-brief.md`. The original wording is
kept below as a historical, superseded block; it is not the operative rule.

> **Original wording (superseded 2026-09-28 before any result, reason: independent review)**
>
> Jev (decomposed) **replaces** today's gate on a set only if, on the held-out half:
> coverage_at_p95 ≥ the baseline's coverage_at_p95, AND run_agreement ≥ 0.95, AND ECE10 ≤ 0.10,
> AND the control check passes. Jev **augments** the gate (runs alongside as an extra signal in
> `decision_records`, Track C) if it meets two of the three numeric conditions. Otherwise it is
> **not adopted** for that set and the finding is recorded with the numbers.
> (`docs/superpowers/plans/2026-09-24-track-a-jev-shadow-eval.md`, Task 8 Step 1, verbatim.)

**Why the original wording was unreliable (independent review, Monte-Carlo with `score.py`'s own
functions, at n <= 130):**

- ECE10 <= 0.10 runs backwards: a perfectly calibrated model passes 0.3% at n=26, 25% at n=65; a
  near-constant base-rate model (no real discrimination at all) passes 63% at n=26.
- "Augment = 2 of 3 numeric conditions" is reachable by noise (an uninformative model passes
  ECE + run_agreement together 44% of the time at n=100) and does not require the control check
  to pass at all.
- coverage_at_p95 had no held-out precision floor: held-out precision at the fit-half p95
  threshold has median 0.83, p10 0.00, at n=26 -- "0.95 on the fit half" said nothing reliable
  about held-out precision.
- The coverage point comparison alone is too noisy to read (10-90% spread ~0.2 at n=100; 0 on
  15-23% of draws at n=26); "Jev >= baseline" is trivially true whenever the baseline lands on 0.
- run_agreement was measured at the accuracy-maximising threshold, not the p95 operating point
  the rest of the rule is judged at.
- For the decomposed arm specifically, coverage read the L2 logistic refit's held-out prediction
  while ECE and run_agreement read the stored v0 composite score (not a probability) -- three
  different numbers about three different scores, read as if they were one model.

### The amended (operative) rule

1. **Eligibility (replaces clause f below):** a set gets a verdict only if the scored rows for
   BOTH the Jev arm being judged (`decomposed`) and the `baseline` arm contain at least **20 of
   EACH class**. Below that, the set is descriptive only -- no replace/augment/not-adopted
   verdict is drawn. (Implemented: `score.py`'s `_eligibility`, `ELIGIBILITY_MIN_PER_CLASS = 20`.)
2. **Scores:** every gate for an arm reads ONE score per row -- the cross-fit held-out
   probability (`score.py`'s `held_out_records`, per arm). For `broad` that is `broad_score`
   (already a probability, read directly, never re-fit). For `decomposed` it is the L2-logistic
   refit's held-out prediction: each split half's rows are scored by the OTHER half's fitted
   composite weights (never the row's own stored v0 composite `score`). For `baseline` it is the
   stored/produced baseline score, read directly (the deployed gate is not re-fit here either).
   The v0 composite stays reported (`decomposed_fit.v0_unfitted`) as descriptive only.
3. **Operating point (unchanged):** for each arm and each direction, the threshold is the lowest
   threshold whose precision on the FIT half is >= 0.95.
4. **Precision floor:** the Jev arm's (`decomposed`'s) pooled held-out precision at that
   operating point must be >= 0.90. Below it, that arm's coverage counts as 0 for the verdict --
   this is not a second independent gate on the baseline arm; a baseline that misses its own
   floor is correctly counted as 0 coverage inside the comparison in clause 5, and the
   coverage-difference CI (not a second floor check) decides whether that makes Jev look better
   for a real reason or by noise. (`score.py`: `PRECISION_FLOOR = 0.90`, `_floored_coverage`.)
5. **Coverage comparison:** a seeded paired bootstrap over row ids (2000 reps, seed fixed and
   recorded -- `score.py`'s `BOOTSTRAP_REPS`/`BOOTSTRAP_SEED`) of `coverage_Jev -
   coverage_baseline`. Rather than recomputing the cross-fit inside each rep, it resamples the
   pooled held-out predictions (each row's held-out score and its operating-point decision) and
   recomputes coverage and the precision floor (clause 4) per rep. Reports the point estimate and
   the 90% CI. (`score.py`'s `_bootstrap_coverage_diff`.)
6. **Calibration (replaces ECE10 <= 0.10):** paired Brier, `Brier_Jev <= Brier_baseline`, on the
   SAME held-out rows (point estimate; also reports the bootstrap 90% CI of the difference, same
   seeded paired resampling as clause 5). ECE10 stays reported as descriptive only.
   (`score.py`'s `_bootstrap_brier_diff`.)
7. **Stability (replaces run_agreement >= 0.95):** run 1 vs run 2 decisions at the SAME held-out
   p95 operating point coverage was judged at (never the accuracy-maximising threshold the old
   `run_agreement` metric uses); count flips; pass if flips <= max(1, 5% of n). Also reports the
   fraction of rows whose `answers` were byte-identical across runs (a provider cache would make
   this 1.0, which is itself a finding). (`score.py`'s `stability_at_p95`.)
8. **Control (unchanged mechanics):** mean score on label=="yes" rows, real arm minus its paired
   control arm, pass only if the difference is >= `CONTROL_MARGIN = 0.2`. "Unreachable" or "fail"
   both mean NOT ADOPTED for that set (no augment either).
9. **Verdict**, applied mechanically (`score.py`'s `verdict()`):
   - **REPLACE** if: eligible, control passes, precision floor met, coverage-difference 90% CI
     lower bound > 0, paired Brier condition met, stability passes.
   - **AUGMENT** if: eligible, control passes, precision floor met, coverage-difference point
     estimate >= 0, stability passes (the paired Brier condition may fail).
   - Otherwise **NOT ADOPTED**.
10. Clauses on failed rows (never scored as 0), provenance, and the 20-largest-disagreements
    human read (below, unchanged from the original rule) still apply.

`verdict()`'s Jev arm is `decomposed` (the rule's subject); `broad` is run through the same
machinery and reported alongside for comparison, but never changes the top-level verdict.

Operational definitions (controller, binding for how `scripts/jev_eval/score.py` output is
read):

a. **"held-out"** means `coverage_at_p95["pooled"]` from `score.py`: ids are split by the
   parity of `int(sha256(id).hexdigest(), 16)` into halves A/B; for each direction the p95
   threshold is fit on one half and precision/coverage evaluated on the *other* half; `pooled`
   combines both directions so every usable row is held out exactly once. The rule is judged on
   `pooled`. The per-direction figures (`a_to_b`, `b_to_a`) are reported alongside it for
   inspection. If a direction could not be fitted (e.g. no threshold on the fit half reaches
   0.95 precision, or the fit half has no rows of one class), that direction is reported as
   `None` with its `reason` string from `score.py`, not silently dropped from `pooled`. The
   `in_sample` figure `score.py` also reports is never used for this rule — it is the older,
   optimistic, same-rows-fit-and-scored number, kept only for comparison.

b. **coverage_at_p95 threshold** = the lowest score threshold reaching precision ≥ 0.95 **on
   the fit half** (`COVERAGE_PRECISION_TARGET = 0.95` in `score.py`). Per `score.py`'s
   documented exception, if no threshold on the fit half reaches 0.95 precision, coverage for
   that direction is 0 (not `None`) — this is the one metric where "no threshold works" is a
   real, scorable number rather than an undefined one.

c. **run_agreement** = run 1 vs run 2 of the same arm, but each row is judged by the threshold
   fitted on the *other* half from the one it belongs to (mirrors clause a — never a threshold
   fit on the same ids it scores). `score.py` also reports the mean absolute score difference
   between the two runs; that number is the noise floor a real Jev-vs-baseline gap must clear
   before it is read as anything other than run-to-run jitter (per CLAUDE.md's ASR-eval method
   rule: run the same config twice before reading any difference).

d. **Control check** = mean score on `label == "yes"` rows, real arm minus its paired control
   arm (`broad`/`control_broad`, `decomposed`/`control_decomposed`), pass only if the
   difference is ≥ `CONTROL_MARGIN = 0.2`. "Unreachable" (no yes rows on either side, or no
   control-arm rows at all) is **not** a pass — an unreachable control gets no verdict
   contribution at all, and the rule cannot be evaluated as "replace" or "augment" for that set
   until a real control comparison exists.

e. **Baseline arm thresholds** are the deployed production gates, not re-tuned for this eval:
   - `programme_match`: `lambda_programme_matcher.CONF_MIN` = 0.70
   - `threads`: `thread_match.MIN_SCORE` = 0.25
   - `work_class`: 0.5 on `P(non_work)` (the stored confidence when the stored verdict is
     `non_work`; `1 - confidence` when it is `work`, matching the human label convention where
     "yes" means non-work, per `scripts/jev_eval/baseline.py` and `export_labels.py`).

f. **Superseded 2026-09-28 -- see Section 2's amended clause 1.** Original wording: "n < 30
   labelled rows, or fewer than 5 rows of either class, makes a set descriptive only." The
   operative eligibility rule is now: a set gets a verdict only if the scored rows for BOTH the
   Jev arm being judged and the baseline arm contain at least 20 of EACH class. This is a hard
   gate applied before the rest of the rule is read as a decision, not a note added after the
   fact.

g. **Rows whose call failed** (`error` set or `score` is `None` in the row contract) are
   excluded from every metric and counted separately as `n_failed` for that arm/run. They are
   never scored as 0 and never silently dropped without being counted (`score.py`'s `_is_usable`
   filter; controller ruling #1).

h. **Provenance is mandatory.** Every results row in Section 5 carries provider, model,
   temperature, route, and the exact question-text hash used to produce it. The scores file
   itself carries provenance: which label-export and result files it read, their row counts,
   and the git sha of the harness that produced them. A number that arrives without this
   provenance is not a valid input to the decision rule, regardless of what it says — this
   repeats the failure mode the 2026-08-12 spec's first harness run had (no provider/model/
   temperature/question-hash recorded, so the data could not prove what produced it).

i. **A person reads the 20 largest Jev-vs-baseline disagreements per set** (Task 8 Step 3)
   before any verdict from the amended rule (Section 2) is acted on — i.e. before a set is
   actually switched to "replace" or "augment" in Track C. A number alone, however clean, is not
   a decision input until this read has happened for that set.

---

## 3. Label sets as of 2026-09-28

Source: controller's read-only, rolled-back RDS Data API queries, 2026-09-27.

| Set | Env | Decided | Pending | Class breakdown | Status under the amended eligibility rule (>= 20 of EACH class, both arms) |
|---|---|---|---|---|---|
| `programme_match` | TEST | 0 | 1 | — | Descriptive only (0 rows of either class) |
| `programme_match` | prod | 0 | 1 | — | Descriptive only (0 rows of either class) |
| `threads` | TEST | 0 | 1 | — | Descriptive only (0 rows of either class) |
| `threads` | prod | 26 | — | 5 confirmed / 21 rejected | Descriptive only (confirmed=5 < 20) |
| `work_class` | TEST | 7 | — | 7 `missed_personal` (all one class) | Descriptive only (7 < 20; 0 rows of the other class) |
| `work_class` | prod | 27 | — | 14 `confirm_non_work` + 13 `missed_personal` = 27 "is non-work"; 0 `reject_is_work` | Descriptive only (0 rows of the other class < 20) |
| `name_aliases` | TEST | 0 | — | — | Not scored under this rule (not in plan's arm list); descriptive if used |
| `name_aliases` | prod | 4 | — | 4, all `kind='other'` | Not scored under this rule; also 0 class diversity |

**Note on `work_class`:** every decided label in both environments, under both "confirmed" and
"missed" outcome labels, falls on the "is non-work" side of the binary. There are currently
**zero negative (work) labels** anywhere. This affects precision and recall differently, per
`scripts/jev_eval/score.py`'s `_metrics_at_threshold` (~lines 214-245): recall = tp/(tp+fn) is
well-defined and meaningful on the current labels, since fn only requires actual-positive rows,
which exist. Precision = tp/(tp+fp) is not informative — with every label "yes", fp is
structurally 0, so precision is trivially 1.0 whenever the arm predicts anything positive at
all (and `None` only when it predicts nothing positive). A trivial 1.0 says nothing about the
classifier. `coverage_at_p95`, which is built on precision crossing the 0.95 target, inherits
this: it says nothing meaningful until work-labelled negatives exist. The set stays descriptive
only under the amended eligibility rule either way, regardless of which metrics happen to be
well-defined — this is unchanged by the owner's 2026-09-27 decision (below); it is why that
decision calls for a new owner-labelled batch with both classes rather than treating the
existing labels as sufficient.

**Owner decision, 2026-09-27:** rather than pausing labelling, the owner chose to build a new,
owner-labelled batch (planned as Task 10) with balanced classes. The existing prod labels
above are a descriptive dry run only — not inputs to a replace/augment/not-adopted verdict, per
the amended eligibility rule, and not to be treated as if they were.

---

## 4. Privacy conditions in force

- Only structured event JSON is sent to the Jev API — never raw transcripts.
- Fields sent are allowlisted (per `scripts/jev_eval/export_labels.py` / `questions.py`); no
  field outside that allowlist reaches the request payload.
- Names are masked before export: known `name_aliases` entries, users' own names, and a
  generic `_NAME_WORD` two-token pass over the remaining text.
- Deleted content is excluded at export time (never enters the exported set at all).
- `--dry-run` output is reviewed by the owner before the first real API call is made for any
  set (per the plan and per §4.4 of the assessment spec's data-egress gate).

**Known residual gap:** a single first name that is in neither the `users` table nor
`name_aliases` passes through the masking pipeline unredacted. This is a known, unfixed gap in
the masking coverage, not a defect introduced by this eval — it applies to whatever text is
exported for any set. Recorded here so it is not silently rediscovered during the disagreement
read (Section 6).

---

## 5. Results

*(filled after the first real run)*

---

## 6. Disagreement read

*(filled after the first real run)*

---

## 7. What this did not measure

- **Task admission**: no exported label set exists. The only ground truth is a single
  session's fixture, `tests/fixtures/task_admission_ground_truth.json` (5 true / 3 not / 1
  unresolved) — too small to run under this rule at all; run descriptively only if time
  allows, never as a decision input.
- **`claim_type`**: no labels exist yet anywhere in the repo.
- **`programme_match`**: 0 decided labels in both environments (Section 3); excluded from the
  first owner-labelled batch (Task 10) because candidate generation for this set depends on the
  matcher's embedding gate (≤0.55 distance), which the owner batch is not yet set up to drive.
- **Latency inside a Lambda**: all runs in this eval are made from a laptop/CI runner, not from
  inside the deployed Lambda environment (VPC egress, cold start, concurrency ceiling). Any
  latency number reported is not a production latency estimate.

---

## 8. Recommendation

*(filled after the first real run)*
