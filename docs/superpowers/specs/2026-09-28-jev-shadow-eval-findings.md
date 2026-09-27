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
   stored/produced baseline score, read directly (the deployed gate is not re-fit here either) for
   the coverage/precision gates -- but see clause 6 for the SEPARATE, Platt-scaled version of the
   baseline score the calibration gate reads. The v0 composite stays reported
   (`decomposed_fit.v0_unfitted`) as descriptive only. **Fix wave 4, A3:** the control check (clause
   8) reads this SAME score for both the real and its paired control arm -- for `decomposed`, the
   control arm's sub-answers are run through the real arm's own fitted cross-fit composite weights
   (never the control arm's own raw v0 `score`), closing a gap where the control check compared two
   different kinds of number.
3. **Operating point (unchanged):** for each arm and each direction, the threshold is the lowest
   threshold whose precision on the FIT half is >= 0.95.
4. **Precision floor (fix wave 4, A1 -- amended from the 2026-09-28 wording below):** the Jev arm's
   (`decomposed`'s) pooled held-out precision at that operating point must clear 0.90 as a
   **one-sided 95% Clopper-Pearson LOWER confidence bound**, not the raw point estimate. A raw
   "28/28 = 1.0" says nothing about the next row; its Clopper-Pearson lower bound (~0.899) is a
   real statement about what fraction of future accepted rows are correct, and needs roughly 28
   accepted rows, all correct, to clear 0.90 -- far more than the raw point estimate implied at
   these sizes. `n = 0` accepted rows is undefined, which counts as the floor failing. Below the
   floor, that arm's coverage counts as 0 for the verdict -- this is not a second independent gate
   on the baseline arm; a baseline that misses its own floor is correctly counted as 0 coverage
   inside the comparison in clause 5, and the coverage-difference CI (not a second floor check)
   decides whether that makes Jev look better for a real reason or by noise. (`score.py`:
   `PRECISION_FLOOR = 0.90`, `clopper_pearson_lower`, `_floored_coverage`.)
5. **Coverage comparison:** a seeded paired bootstrap over row ids (2000 reps, seed fixed and
   recorded -- `score.py`'s `BOOTSTRAP_REPS`/`BOOTSTRAP_SEED`) of `coverage_Jev -
   coverage_baseline`. Rather than recomputing the cross-fit inside each rep, it resamples the
   pooled held-out predictions (each row's held-out score and its operating-point decision) and
   recomputes coverage and the precision floor (clause 4) per rep. Reports the point estimate and
   the 90% CI. (`score.py`'s `_bootstrap_coverage_diff`.)
6. **Calibration (replaces ECE10 <= 0.10):** paired Brier, `Brier_Jev <= Brier_baseline`, on the
   SAME held-out rows (point estimate; also reports the bootstrap 90% CI of the difference, same
   seeded paired resampling as clause 5). **Fix wave 4, A5:** the baseline side of this comparison
   reads a Platt-scaled (1-feature, cross-fit) version of the baseline score, for every set --
   a raw baseline score that is not itself a calibrated probability (e.g. threads' lexical
   similarity) makes a squared-error comparison against a Jev arm's real probability unfair; the
   Jev arm's own held-out score is already a probability (clause 2) and needs no such transform.
   ECE10 stays reported as descriptive only. (`score.py`'s `_bootstrap_brier_diff`,
   `_platt_scale_pooled`.)
7. **Stability (replaces run_agreement >= 0.95):** run 1 vs run 2 decisions at the SAME held-out
   p95 operating point coverage was judged at (never the accuracy-maximising threshold the old
   `run_agreement` metric uses); count flips; pass if flips <= `max(1, floor(0.05 * n))` -- a
   **floor**, not a round, and `n` here is the number of rows that HAD an out-of-sample p95
   threshold available to be judged by at all (rows outside both split halves, or whose direction's
   fit failed, are excluded from `n` rather than counted as automatic passes or fails). Also
   reports the fraction of rows whose `answers` were byte-identical across runs (a provider cache
   would make this 1.0, which is itself a finding). (`score.py`'s `stability_at_p95`.)
8. **Control (unchanged mechanics, fix wave 4 A3 changes what score it reads -- see clause 2):**
   mean score on label=="yes" rows, real arm minus its paired control arm, pass only if the
   difference is >= `CONTROL_MARGIN = 0.2`. "Unreachable" or "fail" both mean NOT ADOPTED for that
   set (no augment either).
9. **Verdict**, applied mechanically (`score.py`'s `verdict()`):
   - **REPLACE** if: eligible, control passes, precision floor met, coverage-difference 90% CI
     lower bound > 0, paired Brier condition met, stability passes.
   - **AUGMENT** if: eligible, control passes, precision floor met, coverage-difference 90% CI
     **lower bound > 0** (fix wave 4, A2 -- amended from "point estimate >= 0" below, which a
     noisy/uninformative model cleared far too easily), stability passes (the paired Brier
     condition may fail). In other words: **replace = augment's conditions + the paired Brier
     condition.**
   - Otherwise **NOT ADOPTED**.
10. Clauses on failed rows and provenance (below, unchanged from the original rule) still apply.
    **Fix wave 4, A4:** for the `decomposed` arm specifically, a split half whose composite-weight
    fit fails (a fit half with fewer than one row of each label, no usable noul sub-answers, or a
    score half missing a fitted feature key) now keeps its held-out rows in the paired comparison,
    with `decision = False` and the row's own stored v0 score as a fallback (mirroring how a failed
    `coverage_at_p95` direction already handled this for the non-decomposed arms) -- it is no
    longer silently dropped from the comparison. See clause (a)'s correction below.
11. **Re-join (fix wave 4, A7):** when scoring already-written result rows against the CURRENT
    `{set}.jsonl` (a relabel, or a re-export, may have happened since the results were written), a
    result row whose id no longer exists in the current fixture at all is EXCLUDED from scoring and
    counted, never scored against its stale, no-longer-current label.

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
   0.95 precision, or the fit half has no rows of one class), that direction's held-out rows are
   **kept in the paired comparison with `decision = False`** (fix wave 4, A4, corrects the earlier
   "not silently dropped" wording below to match: for `decomposed` specifically, this now also
   covers a failed composite-weight fit, not only a failed p95 threshold), reported with its
   `reason` string from `score.py` -- not silently dropped from `pooled`. The `in_sample` figure
   `score.py` also reports is never used for this rule — it is the older, optimistic,
   same-rows-fit-and-scored number, kept only for comparison.

b. **coverage_at_p95 threshold** = the lowest score threshold reaching precision ≥ 0.95 **on
   the fit half** (`COVERAGE_PRECISION_TARGET = 0.95` in `score.py`). Per `score.py`'s
   documented exception, if no threshold on the fit half reaches 0.95 precision, coverage for
   that direction is 0 (not `None`) — this is the one metric where "no threshold works" is a
   real, scorable number rather than an undefined one.

c. **run_agreement — superseded 2026-09-28 (fix wave 4), descriptive only.** = run 1 vs run 2 of
   the same arm, but each row is judged by the threshold fitted on the *other* half from the one
   it belongs to (mirrors clause a — never a threshold fit on the same ids it scores). `score.py`
   also reports the mean absolute score difference between the two runs; that number is the noise
   floor a real Jev-vs-baseline gap must clear before it is read as anything other than
   run-to-run jitter (per CLAUDE.md's ASR-eval method rule: run the same config twice before
   reading any difference). **`run_agreement` no longer decides anything in the amended rule** —
   clause 7's `stability_at_p95` (judged at the p95 operating point, not the accuracy-maximising
   threshold this metric uses) replaces it for the verdict. `score.py` still computes and reports
   `run_agreement` for comparison against `stability`, but it is descriptive only from here on.

d. **Control check** = mean score on `label == "yes"` rows, real arm minus its paired control
   arm (`broad`/`control_broad`, `decomposed`/`control_decomposed`), pass only if the
   difference is ≥ `CONTROL_MARGIN = 0.2`. "Unreachable" (no yes rows on either side, or no
   control-arm rows at all) is **not** a pass — an unreachable control gets no verdict
   contribution at all, and the rule cannot be evaluated as "replace" or "augment" for that set
   until a real control comparison exists.

e. **Baseline arm thresholds — descriptive only for the amended rule's coverage/precision/Brier
   gates (fix wave 4).** The deployed production gates, not re-tuned for this eval:
   - `programme_match`: `lambda_programme_matcher.CONF_MIN` = 0.70
   - `threads`: `thread_match.MIN_SCORE` = 0.25
   - `work_class`: 0.5 on `P(non_work)` (the stored confidence when the stored verdict is
     `non_work`; `1 - confidence` when it is `work`, matching the human label convention where
     "yes" means non-work, per `scripts/jev_eval/baseline.py` and `export_labels.py`).

   These fixed thresholds are what `jev_shadow_eval.py` passes as the baseline's
   `threshold_policy` for the baseline arm's own headline accuracy/precision/recall numbers (the
   original rule's intent). **They are NOT what clauses 4-6's coverage/precision-floor/Brier gates
   are computed against** — those read the baseline's split-half FIT p95 threshold and its
   Platt-scaled score (clause 6), exactly like every other arm, because the amended rule needs a
   held-out, cross-fit operating point and probability for the baseline too, not just its
   deployed cutoff. Keeping (e)'s deployed thresholds only for the descriptive headline numbers
   avoids conflating "what the gate runs in prod" with "what this eval's held-out comparison used".

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

**Fix wave 3, I6 — what the exported `threads`/`programme_match` labels can and cannot show:**
every row in both sets above exists ONLY because `topic_thread_suggestions` /
`programme_progress_suggestions` already carries it — i.e. because the deployed gate proposed
that pair/match in the first place and a human then confirmed or rejected it. A topic the gate
never proposed (a real restatement/match it missed) never enters `topic_thread_suggestions` /
`programme_progress_suggestions` at all, so it can never appear in these exported sets no matter
how the human would have labelled it. These two sets therefore measure **"can Jev filter the
gate's false positives out of what the gate already proposed"** — a precision-side question —
**never the gate's recall**: a Jev arm that agrees with every human "no" on these sets says
nothing about how many real restatements/matches the deployed gate silently missed. `sample_batch.py`'s
owner-labelled `threads` batch (I6) partly compensates by also recomputing `baseline.score`/
`baseline.top_hit` under the DEPLOYED gate's own corpus definition (same site, `MAX_GAP_DAYS`
window, `open_items > 0`) for every sampled pair, so a batch row can at least say whether the
deployed gate would have proposed THIS pair as its top hit — but even that batch is drawn from
pairs `find_candidates`/`score_pair` are capable of producing at all, not from restatements the
gate's own eligibility rules (open items on both sides, the gap cap) rule out before scoring ever
starts.

**Fix wave 4, E19 — the recomputed thread baseline is a recomputation, not a replay.**
`deployed_thread_baseline`'s `score`/`top_hit` for an owner-batch row are computed **at export/
sample time, against today's data** — today's `open_items` counts, today's topic visibility (a
topic deleted since the pair was originally proposed drops out of the corpus the recompute uses)
— not the corpus the deployed gate actually saw the day it proposed (or could have proposed) that
pair. They can also differ from the gate's own historical run for reasons that have nothing to do
with drift: `sample_batch.py`'s own sampling caps (`--max-topics-per-site`, default 200; the
threads window, default 120 days) mean the per-site pool the recompute scores against is not
necessarily the full pool the deployed gate would use, and every summary in that pool is truncated
to 1,000 characters (`sql_threads_topics_for_site`) before `thread_match.score_pair` ever sees it,
which can shift the lexical-similarity score `score_pair` itself computes. Concretely: **a
DB-exported `threads` row (`export_labels.py`, from `topic_thread_suggestions`) carries the score
the deployed gate actually stored at decision time — a historical fact.** **An owner-batch row
(`sample_batch.py`) carries the score recomputed today, under today's data and the sampler's own
caps/truncation — an estimate of what the gate would say now, not a record of what it said then.**
`score.py`'s baseline arm reads whichever of these two `baseline.score` a row happens to carry
without distinguishing them, so a set that mixes DB-exported and owner-batch rows is mixing two
different kinds of "baseline score" under one arm; this is accepted for now (both are the best
available baseline number for that row) but should be read with this distinction in mind, not as
one homogeneous measurement. **`top_hit` is recorded on every owner-batch row for a human to read,
but `score.py` never reads it — it plays no part in scoring, the precision floor, the coverage
comparison, or the verdict.**

---

## 4. Privacy conditions in force

- Only structured event JSON is sent to the Jev API — never raw transcripts.
- Fields sent are allowlisted (per `scripts/jev_eval/export_labels.py` / `questions.py`); no
  field outside that allowlist reaches the request payload.
- **Owner decision, 2026-09-28 (fix wave 2):** `work_class` sends ONLY `title` and `category`
  — never `summary`. `work_class` positives are, by construction, the recorder's private
  conversations (health, family), and `summary` is exactly where that private content lives.
  The label review page may still show the summary to the owner locally; it is stripped before
  the state is built for export, so it never reaches this pipeline's output at all.
- Names are masked before export: known `name_aliases` entries (including, as of this wave, a
  grouped first-name/last-name/full-name mapping per user so all three collapse to ONE
  placeholder — the old code masked only the full name and left a bare surname mention
  unmasked), and a generic `_NAME_WORD` two-token pass over the remaining text, bounded by a
  stoplist of sentence-start/construction words (`the`, `level`, `roof`, weekday/month names,
  number words, …) so ordinary phrases like "Roof Framing" or "The Scaffold crew" are no longer
  swallowed whole as a person's name. Emails are masked to `EMAIL` and phone-shaped digit runs
  (7+ digits, allowing spaces/dashes/a leading `+`) to `PHONE`, in every string, with a
  carve-out for this schema's own `YYYY-MM-DD` date fields so dates are not destroyed as
  false-positive phone numbers.
- **`programme_match`'s `task.name` field never runs the generic two-token pass at all**
  (person aliases still apply to it). Task/programme names are exactly the signal the baseline
  sees raw and this eval exists to compare Jev against; the generic pass's non-overlapping
  match behaviour was masking the entire field on short task names (`"Roof Framing Inspection"`
  → `"PERSON_1 Inspection"`), which biases the comparison against Jev rather than protecting
  anyone.
- Company names, site names, and programme task names of the exported companies/sites are all
  protected from the generic masking pass (`name_aliases` rows of `kind="company"`) — before
  this wave only the recorder's own company name was protected this way; subcontractor names,
  site names and task names were being masked as if they were people.
- `--dry-run` now also prints, per set, the mean number of `PERSON_n` placeholders per state and
  the fraction of states whose title (or, for `programme_match`, `task.name`) is made up
  entirely of placeholders — so the owner can see over-masking, not only check for leaks, before
  the first real API call.
- Deleted content is excluded at export time (never enters the exported set at all). **Because
  that exclusion is evaluated once, at export time, the fixtures must be re-exported
  immediately before the real run** — a customer deletion made between export and run would
  otherwise still be sitting in `scripts/fixtures/jev_eval/*.jsonl` and get sent.
- `--dry-run` output is reviewed by the owner before the first real API call is made for any
  set (per the plan and per §4.4 of the assessment spec's data-egress gate).

**Closed this wave (fix wave 2):**
- Generic two-token pass no longer runs on `task.name` (I1.1).
- Company/site/programme-task names are all protected from the generic pass, not just the
  recorder's own company (I1.2).
- Stoplist added for the generic pass to reduce over-masking of construction phrases (I1.3).
- Donor preference (`_words`) in the control functions no longer treats the literal token
  "person" (from a `PERSON_n` placeholder) as a shared word between two masked states (I1.4).
- `--dry-run` reports over-masking stats, not just leak checks (I1.5).
- CJK alias terms match without a `\b` word-boundary anchor, since `\b` never fires between two
  adjacent CJK characters (I4).
- Alias matching is case-insensitive (I4).
- A user's first name, last name and full name now all collapse to ONE placeholder via a shared
  `alias_group`, instead of only the full name being masked and the bare surname leaking (I4).
- Email addresses and phone-shaped digit runs are masked in every string (I4).
- `work_class`'s allowlist dropped `summary` per the owner decision above (I4).

**Known residual gap, unchanged by this wave:** a single first name that is in neither the
`users` table nor `name_aliases` passes through the masking pipeline unredacted (e.g. a visitor
or a subcontractor mentioned once, by first name only, who is not an enrolled user). This is a
known, unfixed gap in the masking coverage, not a defect introduced by this eval — it applies to
whatever text is exported for any set. Recorded here so it is not silently rediscovered during
the disagreement read (Section 6).

**New residual gap, introduced by the stoplist (I1.3):** a real person surnamed after one of the
stoplist words (e.g. a person literally named "Roof") would not be masked by the generic pass —
the stoplist cannot distinguish "Roof Jenkins, a person" from "Roof Framing, a task". This is the
accepted trade named in the brief: a little under-masking of names that collide with stoplist
words, in exchange for a large reduction in over-masking of ordinary construction phrases.

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
