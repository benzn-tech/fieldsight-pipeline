# A voice the library does not know: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the 09-10 hole (a stranger confirmed by margin alone) by landing, in dependency order, the three changes the spec says only work together: mean pooling in `aggregate_scores`, a per-company rejection floor calibrated from `source='correction'` rows only, and a margin that scales with the candidate pool size. Multi-occasion enrolment is mostly an operational practice per the spec (§3.2) — the one piece of code it needs (enrolling from individually-homogeneous chunks, discarding the ones that fail the guard) is already what `add_sample`/`window_is_homogeneous` do today, so that task documents the practice and adds one thing: `enrol_sample` accepting more than one occasion in a single call is *not* required by the spec, and this plan does not invent it.

**Architecture:** `decide_name` (`src/voiceprint_utils.py`) is the single decision point every caller (`lambda_speaker_embed._match`, `lambda_voiceprint_writer._match_names`) funnels through. It takes a flat `{person_key: score}` mapping built by `aggregate_scores` from `profiles_for_matching`'s per-sample rows. Task 1 changes the reduction inside `aggregate_scores` from max to mean — pure arithmetic, no new inputs. Task 2 adds a new table (`speaker_voiceprint_company_floors`, migration `0060`), a recompute path, and a fourth check inside `decide_name` that can only demote `confirmed` to `tentative`, never promote. Task 3 changes `decide_name`'s margin from a constant to a pool-size-aware lookup, threaded through from `aggregate_scores`'s output size. Task 4 is documentation-plus-verification of the multi-occasion enrolment practice against the existing homogeneity guard, with no change to `add_sample`'s contract. Task 5 is the mean-pooling cutover housekeeping the floor's own risk table demands. Task 6 runs the full suite and opens the PR.

**Tech Stack:** Python 3.12, pure numpy in `voiceprint_utils.py` (no torch/speechbrain/onnxruntime — every Lambda imports this module), psycopg for `repositories/voiceprints.py` (in-VPC only), pytest with hand-written `FakeConn`/`FakeCursor` doubles (no real Postgres, no pgvector).

**Spec:** `docs/superpowers/specs/2026-09-20-a-voice-the-library-does-not-know.md`

## Global Constraints

- **One row per enrolment contribution stays.** `speaker_voiceprint_samples` never stores an averaged vector — withdrawal depends on removing exactly one contribution's effect (migration 0038; spec §2.4, §6 Rejected Alternatives). Mean pooling is a match-time reduction over currently-unwithdrawn rows, computed fresh on every match. No task in this plan touches the samples table's write shape.
- **Consent gates unchanged.** `profiles_for_matching`'s `consent_at IS NOT NULL` and `status <> 'withdrawn'` filters are not touched by any task here. The floor is an additional check layered on top of, never instead of, those filters.
- **The floor is never a constant in source.** No task may hardcode a percentile value or a minimum-sample-count value as the shipped behaviour beyond a clearly-labelled placeholder awaiting real multi-company data, matching the module docstring's existing stance on `DEFAULT_MIN_MARGIN`.
- **Calibration population is human assertions only.** Only `speaker_turn_names` rows with `source='correction'` may enter the floor's calibration set. `source='correction_propagation'`, `source='voiceprint_match'`, and `source='label_inheritance'` are excluded by name in the SQL, and a test must prove a `voiceprint_match` row cannot enter the calibration set (spec §1.3, §6 "Calibrate the floor from the system's own confirmed matches").
- Development artefacts (code comments, commit messages, docs) in English.
- Commit messages end with:
  ```
  Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
  ```
- Windows repo: never `git add -A`; add files by path.
- Test harness (run from the worktree root `C:/Users/camil/fswork/specs-2026-09-20`, Git Bash):
  ```bash
  export UV_LINK_MODE=copy AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_DEFAULT_REGION=ap-southeast-2
  uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest <paths> -q
  ```

## File Structure

- Modify `src/voiceprint_utils.py` — `aggregate_scores` (mean pooling, Task 1); `decide_name` (floor check, Task 2; pool-size-aware margin, Task 3); module docstring and constants.
- Modify `tests/unit/test_voiceprint_utils.py` — new/changed tests beside the existing `aggregate_scores`/`decide_name` blocks.
- Create `src/migrations/0060_speaker_voiceprint_company_floors.sql` — the floor table (Task 2).
- Modify `src/repositories/voiceprints.py` — new `company_floor`, `recompute_company_floor` functions (Task 2); no change to `profiles_for_matching`'s existing filters.
- Modify `tests/unit/test_voiceprints_repo.py` — tests for the new repo functions, including the circularity-guard test.
- Modify `src/lambda_voiceprint_writer.py` — a new scheduled entry point (or existing dispatcher branch) that calls `recompute_company_floor` per company (Task 2); `_match_names`/`_profiles` pass the floor and pool size through to `decide_name` (Task 2, Task 3).
- Modify `src/lambda_speaker_embed.py` — `_match` passes `floor` and pool size through to `decide_name` (Task 2, Task 3).
- Modify `tests/unit/test_lambda_speaker_embed.py`, `tests/unit/test_lambda_voiceprint_writer.py` (or equivalent existing writer test file — confirmed to exist as part of Task 2 Step 1) — wiring tests.
- Modify `src/template.yaml` — a scheduled rule for the floor recompute job (Task 2), if the codebase's existing recompute jobs (e.g. programme derived documents) are wired that way — confirmed by reading the existing pattern before writing this file.
- No new file for Task 4 (multi-occasion enrolment) beyond a docstring addition to `src/repositories/voiceprints.py` (`add_sample`) recording the operational practice, per spec §3.2's own framing that this is additive, not a new code path.
- Modify `src/voiceprint_utils.py` module docstring (Task 5) — record the mean-pooling cutover.

---

### Task 1: Mean pooling in `aggregate_scores`

**Files:**
- Modify: `src/voiceprint_utils.py:68-93` (`aggregate_scores`)
- Test: `tests/unit/test_voiceprint_utils.py`

**Interfaces:**
- `aggregate_scores(rows) -> dict` — signature unchanged. `rows` is `[{"person_key": ..., "score": ...}, ...]`.
- No caller signature changes in this task. `decide_name` is untouched here.

This task alone is **not safe to ship as the whole fix**: per the spec's own measured table (§2.2), mean pooling without the floor (Task 2) still lets 09-10's worst false positive sit above 0 (Mike/Leo stay negative on the numbers quoted, but the spec is explicit that the floor is what turns the wider ECAPA gap into an actual decision rule — §2.4's closing line: "the combination that first makes a usable absolute threshold window... possible (see §1, which is what turns that window into a decision rule rather than an observation)"). Land it first because it is the smallest, most independently verifiable change, and because its own docstring rationale (protecting against one bad sample) is exactly what needs a pinned "before" behaviour before it flips.

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_voiceprint_utils.py`, directly after `test_an_empty_row_set_is_unknown_rather_than_a_crash` (existing max-pooling tests `test_two_samples_of_one_person_collapse_to_their_best` and `test_a_person_with_two_profiles_no_longer_beats_himself` must be REPLACED, not kept beside these — they assert the max behaviour by name and would otherwise contradict the new tests):

```python
# ----------------------------------------------------------
# Mean, not max (2026-09-20). A profile pooled across recording conditions (a clean
# read-aloud sample plus a site-condition sample) spikes under max whenever one sample's
# ACOUSTIC CONDITIONS match a stranger's turn, not because the person is present -- measured
# on 09-10, where a pooled-MAX profile put a person who was never in the room top of the
# list at +0.054/+0.055. Mean absorbs that spike into an average across conditions.
# ----------------------------------------------------------


def test_two_samples_of_one_person_collapse_to_their_mean_not_their_max():
    rows = [
        {"person_key": "ben", "score": 0.31},
        {"person_key": "ben", "score": 0.43},
        {"person_key": "zoe", "score": 0.08},
    ]
    assert vp.aggregate_scores(rows) == pytest.approx({"ben": 0.37, "zoe": 0.08})


def test_a_stray_high_sample_no_longer_wins_on_its_own():
    """The exact failure mode mean pooling exists to remove: one sample scoring high for
    reasons that have nothing to do with the turn's speaker (matching acoustic conditions,
    not matching voice) used to carry the whole profile under max. Under mean it is pulled
    back toward the profile's other, more representative samples."""
    rows = [
        {"person_key": "ben", "score": 0.05},
        {"person_key": "ben", "score": 0.06},
        {"person_key": "ben", "score": 0.62},  # a site-condition spike, not a real match
        {"person_key": "mike", "score": 0.10},
    ]
    scores = vp.aggregate_scores(rows)
    assert scores["ben"] == pytest.approx((0.05 + 0.06 + 0.62) / 3)
    assert scores["ben"] < 0.62, (
        "max pooling would have reported 0.62 here -- the whole point of this change is "
        "that a single spiking sample no longer speaks for the profile")


def test_a_person_with_two_profiles_no_longer_beats_himself_under_mean():
    """The Phase 0 case restated under mean pooling. Aggregation still has to happen before
    the margin means anything -- mean pooling does not remove the need for `person_key`
    grouping, it only changes what happens once rows are grouped."""
    rows = [
        {"person_key": "ben", "score": 0.425},
        {"person_key": "ben", "score": 0.505},
        {"person_key": "zoe", "score": 0.078},
    ]
    decision = vp.decide_name(vp.aggregate_scores(rows), duration_s=5.0)
    assert decision.status == "confirmed"
    assert decision.name == "ben"
    assert decision.margin == pytest.approx((0.425 + 0.505) / 2 - 0.078)
```

Delete `test_two_samples_of_one_person_collapse_to_their_best` (it asserts `{"ben": 0.43, ...}`, the max) — keep `test_a_person_with_two_profiles_no_longer_beats_himself` only if its numbers still pass under mean; check by hand first: `(0.425+0.505)/2 = 0.465`, margin over zoe's 0.078 is 0.387, still `>= DEFAULT_MIN_MARGIN` (0.15), so it still passes unmodified — leave it in place alongside the new `_under_mean` variant rather than deleting it, since it now additionally documents that the outcome (confirmed) survived the pooling change even though the margin number changed. Update its docstring comment to no longer say "with its measured numbers rather than invented ones" implying max, since the invariant it pins (aggregation collapses Ben's two profiles before margin) is pooling-independent.

`test_profiles_without_a_user_do_not_collide` (single-sample-per-key case) is unaffected by max→mean (mean of one value equals that value) and needs no change — leave it in place; it also serves as a control that this task did not perturb the single-sample path.

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit/test_voiceprint_utils.py -q -k "collapse_to_their_mean or stray_high_sample or under_mean"
```
Expected: FAIL — `aggregate_scores` still takes the max, so `test_two_samples_of_one_person_collapse_to_their_mean_not_their_max` sees `{"ben": 0.43, ...}` instead of `{"ben": 0.37, ...}`, and `test_a_stray_high_sample_no_longer_wins_on_its_own` sees `scores["ben"] == 0.62`.

- [ ] **Step 3: Implement**

In `src/voiceprint_utils.py`, replace the `aggregate_scores` body (lines 87-93) and its docstring's "Max, not mean" section:

```python
def aggregate_scores(rows) -> dict:
    """One score per PERSON, from one row per SAMPLE.

    `profiles_for_matching` JOINs `speaker_voiceprint_samples`, so a person with three
    enrolments arrives as three rows. `decide_name` compares a flat mapping and cannot see
    that two of its entries are the same voice, so ungrouped rows make a person their own
    runner-up: Phase 0's Ben holds an English and a Chinese profile ~0.08 apart, which is
    below the 0.15 margin, so he would be reported `tentative` against himself.

    That is also why Phase 0's 31 of 32 is a NEAREST-PROFILE figure and not what this rule
    confirms. Aggregation has to happen before the margin means anything.

    **Mean, not max (2026-09-20).** Max was what "nearest profile" did implicitly, and it
    protects a person's one bad enrolment sample from dragging down every future match — but
    it is exactly the mechanism that turned pooled multi-occasion enrolment into a liability:
    adding a site-condition sample to a profile gives max a sample most likely to spike
    against a stranger's turn recorded in the SAME acoustic conditions, not because the
    person is present. Measured on 2026-09-10 (nobody in the enrolment library attended):
    pooled max put a stranger's nearest profile top of the list at +0.054/+0.055; pooled
    mean kept the true negatives negative. The cost mean re-introduces — one bad enrolment
    sample now always contributes its bad score, rather than only being ignored when a
    better sample exists — is accepted deliberately: it costs a missed confirmation
    (`tentative`, not a wrong name), the cheaper of this design's two error directions
    (module docstring above, "a wrong confident name costs much more than a missing one").

    `person_key` is supplied by the caller and must be an identity, never a display name:
    two people share a first name in this data already.
    """
    sums: dict = {}
    counts: dict = {}
    for row in rows or []:
        key = row["person_key"]
        score = float(row["score"])
        sums[key] = sums.get(key, 0.0) + score
        counts[key] = counts.get(key, 0) + 1
    return {key: sums[key] / counts[key] for key in sums}
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit/test_voiceprint_utils.py -q
```
Expected: all pass, including the full pre-existing `decide_name`/clustering/frame-statistics blocks — this task must not perturb anything below the aggregation tests.

- [ ] **Step 5: Prove the mean-pooling test can go red**

Temporarily revert the body of `aggregate_scores` to the max version (`if key not in out or score > out[key]: out[key] = score`, returning `out`). Run Step 2's command again. Expected: `test_two_samples_of_one_person_collapse_to_their_mean_not_their_max` and `test_a_stray_high_sample_no_longer_wins_on_its_own` FAIL. Restore the mean implementation and re-run: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/voiceprint_utils.py tests/unit/test_voiceprint_utils.py
git commit -m "$(cat <<'EOF'
Aggregate a person's sample scores by mean, not max

Max protected a person's one bad enrolment sample from dragging down future
matches, but it is exactly the mechanism that turns pooled multi-occasion
enrolment into a liability: a site-condition sample spikes against a
stranger's turn recorded in the same acoustic conditions, not because the
person is present. Measured on 2026-09-10 (nobody enrolled attended): pooled
max put a stranger top of the list at +0.054/+0.055; pooled mean kept the
true negatives negative.

This is arithmetic only -- aggregate_scores still takes one row per sample
and returns one score per person, and no caller's signature changes. It is
not sufficient alone: the spec (2026-09-20-a-voice-the-library-does-not-know)
is explicit that mean pooling only becomes a decision rule once the
per-company floor (next commit) is layered on top.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: The per-company rejection floor — storage, recompute, and the `decide_name` check

**This is the task the whole spec turns on.** Everything here is inert-by-default until a company crosses the minimum `source='correction'` sample count (spec §1.5): with no floor row, `decide_name` behaves exactly as today. The circularity guard — that a `voiceprint_match` row can never enter the calibration set — is the single most important property this task has to prove, because getting it wrong silently defeats the whole feature (spec §6, "Calibrate the floor from the system's own confirmed matches").

**Files:**
- Create: `src/migrations/0060_speaker_voiceprint_company_floors.sql`
- Modify: `src/repositories/voiceprints.py` — new `company_floor`, `recompute_company_floor` functions
- Modify: `src/voiceprint_utils.py` — `decide_name` gains a floor check
- Modify: `src/lambda_speaker_embed.py` — `_match` passes `floor` through
- Modify: `src/lambda_voiceprint_writer.py` — `_match_names`/`_profiles` pass `floor` through; a new dispatcher branch calling `recompute_company_floor`
- Test: `tests/unit/test_voiceprints_repo.py`, `tests/unit/test_voiceprint_utils.py`, `tests/unit/test_lambda_speaker_embed.py`

**Interfaces:**
- `repositories.voiceprints.recompute_company_floor(conn, company_id, percentile=DEFAULT_FLOOR_PERCENTILE, min_samples=DEFAULT_FLOOR_MIN_SAMPLES) -> dict | None` — computes and upserts one row into `speaker_voiceprint_company_floors`; returns the row, or `None` (and writes nothing) if the company has fewer than `min_samples` qualifying rows.
- `repositories.voiceprints.company_floor(conn, company_id) -> float | None` — reads the stored floor, or `None` if no row exists (below minimum, or never computed).
- `voiceprint_utils.decide_name(scores, duration_s, min_turn_s=..., min_margin=..., floor=None) -> Decision` — new keyword-only-by-convention `floor` parameter, default `None` (no floor, today's behaviour unchanged).

- [ ] **Step 0: Read before writing — the recompute pattern, the dispatch shape, and the scheduled-function precedent**

Read `src/lambda_org_api.py`'s `_write_snapshot` (~line 5492, "Regenerate programmes/{site_id}/programme.json from Aurora") as the existing precedent for "derived/materialized state, recomputed by a job rather than at request time" that the spec (§1.3) points to. Read `src/lambda_voiceprint_writer.py`'s top-level dispatcher (the function that routes on an event key to `_enrol`, `_propagation`, `_match_names`, `_profiles`, `_rebind`, etc. — find it by searching for where `_profiles` and `_match_names` are called from) to see the existing branching shape a new `_recompute_floors` branch must match. Record the dispatcher's function name and line in the report.

`template.yaml` already has at least nine `Schedule:`-triggered functions — this is not a judgment call about whether a precedent exists, it is a choice of which one to model. Read these before writing anything:

- `ExtractionBacklogFunction` (`rate(1 hour)`) — **this is the template to copy.** It recomputes something derived (whether a session was transcribed but never extracted) on a schedule and emits a CloudWatch metric, which is the same shape as "recompute a company's floor and record when it last ran." Copy its `Events: BacklogSweep: Type: Schedule` block, its `State: !If [ShouldEnableSchedules, ENABLED, DISABLED]`, and its narrowly-scoped `Policies` block (S3 list/get on specific prefixes, `cloudwatch:PutMetricData` scoped by namespace) as the starting point for the new function's IAM.
- `FinalizeSweepFunction` (`rate(1 minute)`) — read its comments for what a tight cadence costs (it exists in `OVERLAP_ACCEPTED` in `tests/unit/test_template_timeout_invariants.py` because it is allowed to outlive its own interval, for a documented reason tied to Aurora auto-pause). The floor recompute has no such reason and must NOT need this exception.
- `RecordingSegmentsFunction` (`rate(5 minutes)`) — read it for `ReservedConcurrentExecutions: 2`, this repo's pattern for capping a sweep's burst. Not needed here (see Step 14's cadence reasoning) but read it so the choice not to cap concurrency is a decision, not an oversight.
- `NonWorkExpiryFunction` (`rate(1 hour)`), `VoiceReaperFunction` (`rate(6 hours)`), `DeviceReportFunction` (`cron(0 16 * * ? *)`), `OrchestratorFunction` (`cron(0/15 …)`) — read for cadence range only; the actual cadence choice and its justification are in Step 14, not here.

- [ ] **Step 1: Write the failing repository tests**

Add to `tests/unit/test_voiceprints_repo.py`, after the `confirmations_count` block:

```python
# ----------------------------------------------------------
# The per-company rejection floor (2026-09-20 spec). Calibrated ONLY from
# source='correction' rows -- a human clicking a name onto a turn, never the system's own
# output. The alternative (calibrate from confirmed matches) is circular: on 2026-09-10 the
# existing chain CONFIRMED a stranger at best=0.445, and a distribution containing 0.445
# has a low percentile at or below 0.445, so the floor could never reject the very error it
# exists to catch. This is the guard against that, and it is the point of the whole task.
# ----------------------------------------------------------


def test_recompute_reads_only_source_correction_scores():
    """The circularity guard. A voiceprint_match row, a correction_propagation row and a
    label_inheritance row must all be invisible to the query that builds the floor -- only
    a human-asserted correction may set the bar a machine guess is later held to."""
    conn = FakeConn([[{"n": 1}], [{"score": 0.30}]])
    voiceprints.recompute_company_floor(conn, CO, min_samples=1)
    read_sql = conn.calls[0]["sql"]
    assert "source = 'correction'" in read_sql
    assert "voiceprint_match" not in read_sql
    assert "correction_propagation" not in read_sql
    assert "label_inheritance" not in read_sql


def test_a_voiceprint_match_row_cannot_enter_the_calibration_set():
    """Direct proof, not just an SQL-text assertion: a company whose ONLY qualifying rows
    are voiceprint_match scores has TOO FEW source='correction' rows and gets no floor at
    all -- never a floor built from the matches. The count query itself is scoped to
    source = 'correction', so a company with five voiceprint_match rows and zero
    corrections reports a count of zero."""
    conn = FakeConn([[{"n": 0}]])
    result = voiceprints.recompute_company_floor(conn, CO, min_samples=1)
    assert result is None
    count_sql = conn.calls[0]["sql"]
    assert "source = 'correction'" in count_sql


def test_below_the_minimum_sample_count_no_floor_is_written():
    """Spec 1.5: a young company, or a company whose steady state is mostly matches and few
    corrections, gets no floor rather than a global fallback or a refusal to confirm
    anything -- both worse per the spec's own reasoning."""
    conn = FakeConn([[{"n": 3}]])
    result = voiceprints.recompute_company_floor(conn, CO, min_samples=10)
    assert result is None
    assert not any(c["sql"].startswith("INSERT") or "UPSERT" in c["sql"].upper()
                   for c in conn.calls), "no row should be written below the minimum"


def test_the_floor_is_a_low_percentile_not_the_minimum_or_the_mean():
    """A single low outlier in the corrected history must not veto the company's future
    matches -- that is what the minimum ('own minimum') alternative was rejected for."""
    scores = [0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65]
    conn = FakeConn([[{"n": len(scores)}], [{"score": s} for s in scores]])
    result = voiceprints.recompute_company_floor(conn, CO, min_samples=1, percentile=5)
    assert result is not None
    assert result["floor"] < min(scores) + 0.05, "sanity: low percentile sits near the low end"
    assert result["floor"] > min(scores) - 1e-9 or len(scores) < 20, (
        "with fewer than 20 points a 5th percentile interpolates near the minimum -- this "
        "assertion documents that rather than asserting an exact numpy percentile formula")


def test_company_floor_reads_the_stored_value():
    conn = FakeConn([[{"floor": 0.31, "sample_count": 12}]])
    assert voiceprints.company_floor(conn, CO) == pytest.approx(0.31)


def test_company_floor_is_none_when_no_row_exists():
    conn = FakeConn([[]])
    assert voiceprints.company_floor(conn, CO) is None


def test_recompute_requires_company_id():
    with pytest.raises(ValueError):
        voiceprints.recompute_company_floor(FakeConn(), None)


def test_company_floor_requires_company_id():
    with pytest.raises(ValueError):
        voiceprints.company_floor(FakeConn(), None)
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit/test_voiceprints_repo.py -q -k floor
```
Expected: `AttributeError: module 'repositories.voiceprints' has no attribute 'recompute_company_floor'` (and `company_floor`).

- [ ] **Step 3: Confirm 0060 is still free, then write the migration**

A second plan written in parallel against this same worktree (`docs/superpowers/plans/2026-09-20-a-literal-token-is-findable.md`) also allocated a migration number for `report_chunks`. Do not assume the number written into this plan survives until execution — run:

```bash
ls src/migrations | sort | tail -5
```

If `0060` is not present, use it. If it is already taken (the other plan landed first, or a third branch landed in between), take the next free integer instead, name the number you actually used in this step's commit message (Step 8 below), and update every reference to it in this file's own remaining text as you go — the filename, the `CREATE TABLE`/`INSERT` migration file itself, and Step 8's `git add`.

Create `src/migrations/0060_speaker_voiceprint_company_floors.sql` (or the confirmed-free number from the check above), matching 0058's comment-heavy style:

```sql
-- A rejection floor under decide_name's margin, calibrated per company from that
-- company's own human-asserted corrections -- never authored as a number in source
-- (docs/superpowers/specs/2026-09-20-a-voice-the-library-does-not-know.md, S1).
--
-- decide_name's margin check alone confirms whoever is LEAST wrong among the enrolled
-- profiles, even when nobody enrolled was in the room: measured on 2026-09-10, a
-- stranger was confirmed at best=0.445, clear of the runner-up by 0.268 -- comfortably
-- past the 0.15 margin. A floor asks a second question the margin cannot: is the
-- winning score itself high enough to mean anything, for THIS company's own history.
--
-- One row per company, recomputed by a scheduled job rather than at match time -- the
-- floor must not move mid-decision because one turn happened to land during
-- recomputation (matching the existing pattern for other derived/materialized state,
-- e.g. programmes/{site_id}/programme.json regenerated from Aurora).
--
-- sample_count is the count of source='correction' speaker_turn_names rows the floor
-- was built from, kept beside the floor rather than only in a log line: it is what lets
-- decide_name's caller (and an operator watching this table) tell "no floor because too
-- little evidence" apart from "no floor because nobody ran the job yet", and it is the
-- standing metric the spec's own risk table asks to be watched (S7, last row).
--
-- No row at all, rather than a row with floor=NULL, for a company below the minimum: a
-- present NULL and an absent row would both have to be treated as "no floor" by every
-- reader, and only one of them is enforced by "SELECT ... WHERE company_id = %s
-- returning nothing" -- a NULL floor is a value a future bug could compare against.
CREATE TABLE IF NOT EXISTS speaker_voiceprint_company_floors (
    company_id    uuid PRIMARY KEY,
    floor         double precision NOT NULL,
    sample_count  integer NOT NULL,
    computed_at   timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE speaker_voiceprint_company_floors IS
  'One row per company: a low percentile of that company''s own source=''correction'' '
  'decide_name "best" scores, recomputed by a scheduled job. No row = below the minimum '
  'sample count to calibrate responsibly; decide_name then applies no floor, exactly as '
  'every company does today.';
COMMENT ON COLUMN speaker_voiceprint_company_floors.sample_count IS
  'How many source=''correction'' rows the floor was built from. Tracked so a company '
  'stuck below the minimum for longer than expected is visible directly from this table, '
  'with no new instrumentation.';
```

- [ ] **Step 4: Implement the repository functions**

In `src/repositories/voiceprints.py`, add near `confirmations_count`:

```python
#: Tuning knobs, not settings anyone should trust yet (spec S1.3, S1.5). Six enrolled
#: voices across four dates cannot fit a percentile or a minimum sample count without
#: repeating the exact overfitting mistake DEFAULT_MIN_MARGIN's docstring warns against.
#: Both are here, named, so the eventual calibration against real per-company correction
#: volume changes one number in one place rather than a magic literal buried in a query.
DEFAULT_FLOOR_PERCENTILE = 5
DEFAULT_FLOOR_MIN_SAMPLES = 20


def recompute_company_floor(conn, company_id, percentile=DEFAULT_FLOOR_PERCENTILE,
                            min_samples=DEFAULT_FLOOR_MIN_SAMPLES) -> dict | None:
    """Rebuild one company's rejection floor from its OWN human-asserted corrections.

    **Only `source='correction'` rows enter this.** The alternative -- calibrating from
    everything `decide_name` confirms -- is circular and already named as the mistake to
    avoid: on 2026-09-10 the existing margin-only chain confirmed a stranger at
    `best=0.445`, and a distribution that already contains 0.445 has a low percentile at
    or below 0.445, so the floor could never again reject that exact class of error. Every
    wrong confirmation would lower the bar for the next one. `source='correction_
    propagation'` (one human assertion spread by the model's own similarity judgment),
    `source='voiceprint_match'` (the system's own guess), and `source='label_inheritance'`
    (inherited from a transcriber label, not independently asserted) are excluded for the
    same reason, one level removed each.

    `best` here is the score `decide_name` ranked first for the turn a correction landed
    on -- `speaker_turn_names.score`, stamped at write time by whichever caller recorded
    the correction. A turn with no stored score (an older row, or one written before
    scoring existed) cannot be measured and is excluded rather than treated as zero.

    Returns `None` and writes nothing below `min_samples` qualifying rows -- a floor
    calibrated from too few points is noise, and no floor (today's margin-only behaviour)
    is safer than a wrong one (spec S1.5).
    """
    _require_company(company_id)
    cur = conn.cursor(row_factory=dict_row)
    count_row = cur.execute(
        "SELECT count(*) AS n FROM speaker_turn_names "
        "WHERE company_id = %s AND source = 'correction' "
        "  AND score IS NOT NULL AND superseded_at IS NULL",
        (company_id,)).fetchone()
    n = int((count_row or {}).get("n") or 0)
    if n < min_samples:
        return None
    rows = cur.execute(
        "SELECT score FROM speaker_turn_names "
        "WHERE company_id = %s AND source = 'correction' "
        "  AND score IS NOT NULL AND superseded_at IS NULL",
        (company_id,)).fetchall()
    import numpy as np
    scores = np.array([float(r["score"]) for r in rows], dtype=np.float64)
    floor = float(np.percentile(scores, percentile))
    cur.execute(
        "INSERT INTO speaker_voiceprint_company_floors "
        "(company_id, floor, sample_count, computed_at) "
        "VALUES (%s, %s, %s, now()) "
        "ON CONFLICT (company_id) DO UPDATE "
        "  SET floor = EXCLUDED.floor, sample_count = EXCLUDED.sample_count, "
        "      computed_at = now()",
        (company_id, floor, n))
    return {"company_id": company_id, "floor": floor, "sample_count": n}


def company_floor(conn, company_id) -> float | None:
    """This company's current rejection floor, or None if it has none.

    None covers two cases the caller must treat identically: nobody has run the recompute
    job yet, and the company has too few `source='correction'` rows to calibrate
    responsibly (spec S1.5). Both mean "apply no floor", never a borrowed default.
    """
    _require_company(company_id)
    row = conn.cursor(row_factory=dict_row).execute(
        "SELECT floor FROM speaker_voiceprint_company_floors WHERE company_id = %s",
        (company_id,)).fetchone()
    return float(row["floor"]) if row else None
```

- [ ] **Step 5: Run the repository tests to verify they pass**

```bash
uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit/test_voiceprints_repo.py -q
```
Expected: all pass, including every pre-existing test in this file (confirm `confirmations_count`, `add_sample`, `profiles_for_matching`, `withdraw` tests are untouched).

- [ ] **Step 6: Prove the circularity guard test can go red**

Temporarily widen the `WHERE` clauses in both queries inside `recompute_company_floor` to drop `AND source = 'correction'`. Run:
```bash
uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit/test_voiceprints_repo.py -q -k "voiceprint_match_row_cannot or reads_only_source_correction"
```
Expected: both FAIL. Restore the filter and re-run: pass.

- [ ] **Step 7: Write the failing `decide_name` floor tests**

Add to `tests/unit/test_voiceprint_utils.py`, after `test_the_margin_is_configurable_but_its_default_is_not_the_fitted_cut`:

```python
# ----------------------------------------------------------
# The rejection floor (2026-09-20 spec S1). A NEW, FINAL check, after the margin, before
# returning confirmed. It can only ever demote confirmed -> tentative, never produce
# unknown by itself (duration and "no profiles" already own that path) and never promote.
# Absent (floor=None, today's default) it is a no-op -- every existing test above this
# block must keep passing unchanged.
# ----------------------------------------------------------


def test_a_would_be_confirmation_below_the_floor_is_downgraded_to_tentative():
    """The 09-10 shape: a clear margin, but the winning score itself is not plausible for
    this company's own corrected history."""
    d = vp.decide_name({"Mike": 0.445, "Leo": 0.177}, duration_s=6.0, floor=0.50)
    assert d.status == "tentative"
    assert d.name == "Mike", "the lean is still shown -- a demotion is not a withholding"
    assert "floor" in d.reason.lower()


def test_a_would_be_confirmation_above_the_floor_stays_confirmed():
    d = vp.decide_name({"Ben": 0.65, "Zoe": 0.20}, duration_s=6.0, floor=0.50)
    assert d.status == "confirmed" and d.name == "Ben"


def test_no_floor_is_a_no_op_exactly_as_today():
    """Every company runs margin-only until it calibrates one (S1.5). floor=None, the
    default, must reproduce every pre-floor test in this file with no other change."""
    d = vp.decide_name({"Ben": 0.48, "Zoe": 0.08, "Mike": 0.07}, duration_s=6.0)
    assert d.status == "confirmed" and d.name == "Ben"


def test_the_floor_never_turns_a_tentative_result_into_a_confirmation():
    """The floor is a final, demotion-only check. It has nothing to promote from a
    tentative margin outcome, and this pins that it never tries."""
    d = vp.decide_name({"Ben": 0.30, "Zoe": 0.26}, duration_s=8.0, floor=0.0)
    assert d.status == "tentative", (
        "a floor of 0.0 (trivially cleared) must not rescue a result the margin already "
        "downgraded")


def test_the_floor_never_fires_on_a_single_profile_result():
    """Single-profile decisions are already tentative (no runner-up to beat) and stay
    tentative -- the floor has nothing to demote and must not raise or change the reason
    in a way that hides the real one."""
    d = vp.decide_name({"Ben": 0.9}, duration_s=10.0, floor=0.99)
    assert d.status == "tentative" and d.name == "Ben"
    assert "runner-up" in d.reason.lower()


def test_the_floor_never_produces_unknown():
    """Failing the floor is a lean, not a refusal -- unknown stays owned by duration and
    'no profiles', per S1.4."""
    d = vp.decide_name({"Mike": 0.10, "Leo": -0.05}, duration_s=6.0, floor=0.9)
    assert d.status != "unknown"
```

- [ ] **Step 8: Run to verify they fail**

```bash
uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit/test_voiceprint_utils.py -q -k floor
```
Expected: `TypeError: decide_name() got an unexpected keyword argument 'floor'`.

- [ ] **Step 9: Implement the floor check in `decide_name`**

In `src/voiceprint_utils.py`, change the signature and add the check after the existing margin branch:

```python
def decide_name(scores, duration_s: float,
                min_turn_s: float = DEFAULT_MIN_TURN_S,
                min_margin: float = DEFAULT_MIN_MARGIN,
                floor: float | None = None) -> Decision:
    """Who this turn belongs to, or an honest refusal.

    `scores` maps a profile name to its similarity with this turn. The order of the checks
    is the point: duration first, so that no score — however emphatic — can name a turn too
    short to carry the evidence; margin second, nearest-profile with a required gap; the
    per-company floor last, and only as a DEMOTION.

    `floor` is this company's own calibrated rejection floor (`repositories.voiceprints.
    company_floor`), a low percentile of that company's `source='correction'` scores, or
    `None` when the company has not calibrated one yet (spec S1.5) — `None` is a no-op,
    reproducing today's margin-only behaviour exactly. It never promotes: a turn the margin
    already sent to `tentative` (or `unknown`) is untouched by this check, because the
    floor answers "is the winner's score itself plausible", which only matters once
    something has already tried to be a winner.
    """
    if duration_s is None or duration_s < min_turn_s:
        return Decision("unknown", None, None,
                        f"too short to attribute ({duration_s}s < {min_turn_s}s); the one "
                        f"Phase 0 miss was a 2.1s turn and it scored its own speaker lowest")
    if not scores:
        return Decision("unknown", None, None, "no enrolled profiles to compare against")

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    best_name, best = ranked[0]
    if len(ranked) == 1:
        # Nothing to be better than. Confirming here would be confirming on an absolute
        # score, which is exactly what the overlapping distributions forbid.
        return Decision("tentative", best_name, None,
                        "only one enrolled profile, so there is no runner-up to beat")

    margin = best - ranked[1][1]
    if margin < min_margin:
        return Decision("tentative", best_name, margin,
                        f"only {margin:.3f} clear of {ranked[1][0]}; below the {min_margin} "
                        f"margin this is a lean, not an identification")

    # The floor: a company-calibrated final check, and a DEMOTION only. A turn with no
    # enrolled speaker present still produces a winner — the least-dissimilar profile in
    # the list — and a wide margin over the runner-up does not mean that winner is a
    # plausible match, only that it is less implausible than the rest (spec S1.1: measured
    # 2026-09-10, best=0.445, margin=0.268, confirmed by margin alone).
    if floor is not None and best < floor:
        return Decision("tentative", best_name, margin,
                        f"clear of the runner-up by {margin:.3f}, but {best:.3f} is below "
                        f"this company's calibrated floor of {floor:.3f} — a wide margin "
                        f"over weak candidates is not the same as a plausible match")
    return Decision("confirmed", best_name, margin,
                    f"clear of the runner-up by {margin:.3f}")
```

- [ ] **Step 10: Run the full `voiceprint_utils` suite**

```bash
uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit/test_voiceprint_utils.py -q
```
Expected: all pass — including every existing `decide_name` test with no `floor` argument, which must fall through the new `floor is not None` guard unchanged.

- [ ] **Step 11: Prove the floor tests can go red**

Temporarily change `if floor is not None and best < floor:` to `if False:`. Run Step 8's command. Expected: `test_a_would_be_confirmation_below_the_floor_is_downgraded_to_tentative` FAILS. Restore and re-run: pass.

- [ ] **Step 12: Wire `floor` through the two match callers**

In `src/lambda_speaker_embed.py`, `_match` (~line 617): the profiles list arrives from the writer's `_profiles` response. Add a `company_floor` key to that response (Step 13) and thread it through:

```python
    profiles = event.get("profiles") or []
    floor = event.get("company_floor")
    by_key = {}
    for p in profiles:
        by_key.setdefault(p["person_key"], p)
    ...
        d = vp.decide_name(vp.aggregate_scores(rows), duration_s=duration, floor=floor)
```

In `src/lambda_voiceprint_writer.py`, `_profiles` (~line 282): add the company's floor to the returned payload, reading it once per invocation rather than once per turn:

```python
def _profiles(event):
    company_id = _require(event, "company_id")
    with get_connection() as conn:
        rows = profiles_for_matching(conn, company_id, site_id=event.get("site_id"))
        floor = company_floor(conn, company_id)
    return {"profiles": [{"person_key": str(r["id"]),
                          "display_name": r["display_name"],
                          "status": r["status"],
                          "embedding": r["embedding"]} for r in rows],
            "company_floor": floor}
```

Add `company_floor` to this file's import from `repositories.voiceprints` (find the existing `from repositories.voiceprints import ...` line near the top and extend it).

`_match_names` does not call `decide_name` itself — it records whatever `_match` already decided — so it needs no change; confirm this by reading it again before concluding the wiring is complete, and note in the report whether any other caller of `decide_name` exists (grep `decide_name(` across `src/`) that this step missed.

- [ ] **Step 13: Write and run the wiring test**

In `tests/unit/test_lambda_speaker_embed.py`, find the existing test(s) for `_match` (grep `def _match` usage / `speaker_embed._match` or the public handler name it's invoked through) and add, alongside them:

```python
def test_match_passes_the_companys_floor_into_decide_name(monkeypatch):
    """The floor arrives on the event (from the writer's _profiles response) and must
    reach decide_name, or a calibrated floor sits in the database doing nothing."""
    captured = {}
    real_decide_name = vp.decide_name

    def spy(scores, duration_s, **kwargs):
        captured["floor"] = kwargs.get("floor")
        return real_decide_name(scores, duration_s, **kwargs)

    monkeypatch.setattr(vp, "decide_name", spy)
    monkeypatch.setattr(speaker_embed, "_window_audio",
                        lambda *a, **k: ("key", "clip", 16000))
    monkeypatch.setattr(speaker_embed, "embed_audio", lambda clip, sr: [0.1] * 192)
    event = {
        "user_folder": "f", "date": "2026-09-20", "session": "s",
        "company_floor": 0.42,
        "profiles": [{"person_key": "p1", "embedding": [0.1] * 192, "status": "confirmed"}],
        "turns": [{"start_sec": 0.0, "end_sec": 5.0, "source_filename": "a.wav"}],
    }
    speaker_embed._match(event)
    assert captured["floor"] == 0.42
```

Read the file first to match its actual fixture/monkeypatch conventions (module alias for `lambda_speaker_embed`, how `_window_audio`/`embed_audio` are already stubbed elsewhere in that file) before writing this — adjust names to match rather than inventing new ones.

```bash
uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit/test_lambda_speaker_embed.py -q
```
Expected: fails before Step 12's edit (no `floor` kwarg reaches `decide_name`, or `event.get("company_floor")` is never read), passes after.

- [ ] **Step 14: Add the recompute dispatcher branch**

Using Step 0's findings, add a `_recompute_floors(event)` function to `src/lambda_voiceprint_writer.py` beside `_profiles`/`_match_names`, following the file's existing dispatch shape exactly (same `_require`/`get_connection()` pattern):

```python
def _recompute_floors(event):
    """Rebuild one company's rejection floor from its own human-asserted corrections.

    Scheduled, not triggered by a match or a correction — the floor must not move
    mid-decision because one turn happened to land during recomputation (spec S1.3).
    """
    company_id = _require(event, "company_id")
    with get_connection() as conn:
        result = recompute_company_floor(conn, company_id)
    logger.info("floor recompute for %s: %s", company_id,
                result or "below minimum sample count, no floor written")
    return result or {"company_id": company_id, "floor": None}
```

Add `recompute_company_floor` to the existing `from repositories.voiceprints import ...` line. Wire it into the dispatcher found in Step 0 under a new event key (e.g. `"action": "recompute_floors"` or whatever discriminator the existing dispatcher uses — match its actual shape).

**Wire the schedule now, modelled on `ExtractionBacklogFunction`.** This is not deferred: `template.yaml` already carries nine `Schedule:`-triggered functions and the pattern Step 0 read is a direct fit. Add a new `AWS::Serverless::Function` resource, `FloorRecomputeFunction`, alongside `ExtractionBacklogFunction`:

```yaml
  FloorRecomputeFunction:
    Type: AWS::Serverless::Function
    Properties:
      FunctionName: !Sub ["${P}-floor-recompute", {P: !FindInMap [StageConfig, !Ref Stage, Prefix]}]
      CodeUri: src/
      Handler: lambda_voiceprint_writer.lambda_handler
      Timeout: 120
      MemorySize: 256
      VpcConfig: !Ref VpcConfigForDb   # in-VPC: this half owns the Postgres connection
      Environment:
        Variables:
          S3_BUCKET: !Ref DataBucketName
      Policies:
        - VPCAccessPolicy: {}
        - Version: '2012-10-17'
          Statement:
            - Effect: Allow
              Action: [secretsmanager:GetSecretValue]
              Resource: !Ref DbSecretArn   # match whatever existing voiceprint-writer function uses today
      Events:
        FloorSweep:
          Type: Schedule
          Properties:
            Schedule: rate(6 hours)
            Description: Recompute every company's speaker-voiceprint rejection floor from its own human corrections.
            State: !If [ShouldEnableSchedules, ENABLED, DISABLED]
```

Read the existing `VoiceprintWriterFunction`'s (or whatever the current resource is actually named — confirm by grepping `Handler: lambda_voiceprint_writer` in `template.yaml`) `VpcConfig`/`Policies`/`Environment` block first and copy its actual DB-connection wiring verbatim rather than the placeholder names above, which are illustrative only.

**Cadence: `rate(6 hours)`, not `ExtractionBacklogFunction`'s hourly.** The floor only moves when new `source='correction'` rows accumulate, and corrections are rare by construction (spec §1.5: "a company can run for a long time, accumulate plenty of matched turns, and still have too few corrected ones to calibrate a floor"). Recomputing hourly would run 24 times a day against data that, for most companies, changed zero or one time in that window — all cost, no freshness gained, for a check whose own worst-case failure mode (running once more before a correction lands) is "the floor is a few hours stale during a demotion-only safety check," not a user-facing outage. `VoiceReaperFunction`'s `rate(6 hours)` is the closer precedent by cadence even though `ExtractionBacklogFunction` is the closer precedent by *shape* (recompute + no reserved concurrency) — this plan copies the shape from one and the cadence reasoning from the other, and says so here rather than picking one silently.

**Timeout: 120 seconds.** `Timeout` must be strictly less than the schedule interval (21,600 s for `rate(6 hours)`), enforced by `tests/unit/test_template_timeout_invariants.py::test_a_scheduled_function_is_shorter_than_its_interval` — 120 s clears that by three orders of magnitude, matches `ExtractionBacklogFunction`'s own `Timeout: 120`, and is generous for a per-company `count(*)`/`percentile` query with no LLM call and no S3 read in the loop. No `ReservedConcurrentExecutions` cap: unlike `RecordingSegmentsFunction`'s 5-minute dirty-day sweep, a 6-hour cadence with a 120 s timeout cannot overlap itself under any realistic company count, so `FinalizeSweepFunction`'s reason for needing `OVERLAP_ACCEPTED` does not apply here and this function should not be added to that dict.

If the loop over companies needs its own event fan-out (one `_recompute_floors` invocation per company, versus one invocation iterating every company), decide that here by reading whether `ExtractionBacklogFunction`'s handler loops over companies internally or is invoked once per company by something else, and match whichever shape it uses — do not invent a third fan-out mechanism.

- [ ] **Step 15: Confirm the deploy role can create this resource**

Adding a new `AWS::Serverless::Function` (plus its `Events: Schedule` and any new `Policies` statement) is exactly the class of change this repo has a documented, repeated trap for: a new CFN resource fails the *whole stack* with `CREATE_FAILED` and rolls back everything in the same deploy if the deploy role cannot create it. Check before pushing, not after:

```bash
export MSYS_NO_PATHCONV=1
aws iam simulate-principal-policy \
  --policy-source-arn arn:aws:iam::509194952652:role/github-actions-fieldsight-deploy \
  --action-names lambda:CreateFunction lambda:UpdateFunctionConfiguration logs:CreateLogGroup events:PutRule events:PutTargets \
  --resource-arns \
    arn:aws:lambda:ap-southeast-2:509194952652:function:fieldsight-test-floor-recompute \
    arn:aws:logs:ap-southeast-2:509194952652:log-group:/aws/lambda/fieldsight-test-floor-recompute \
  --query 'EvaluationResults[].[EvalActionName,EvalResourceName,EvalDecision]' --output text
```

Expected: `allowed` on every row. Substitute the function name actually chosen in Step 14 if it differs from `fieldsight-test-floor-recompute`. Anything other than `allowed` means fix the IAM on `github-actions-fieldsight-deploy` (or the new function's own execution role, checked the same way with `lambda:CreateFunction`'s implicit role-creation/`iam:PassRole` if the function's role is new rather than reused) before this task's commit — never after a rollback. If the voiceprint writer's existing function already reuses an execution role this new function will also use, note that reuse in the report; it changes what needs checking here (no new role to create, but the schedule rule and log group still need creating).

**Already run by the controller on 2026-09-20 — every row `allowed`**, against
`…:function:fieldsight-prod-floor-recompute` and
`…:rule/fieldsight-prod-FloorRecompute`:
`lambda:CreateFunction`, `lambda:UpdateFunctionConfiguration`, `lambda:AddPermission`,
`lambda:TagResource`, `events:PutRule`, `events:PutTargets`, `events:DescribeRule`.
The grant is `lambda:*` on `arn:aws:lambda:ap-southeast-2:509194952652:function:fieldsight-*`
and the EventBridge statement covers `rule/*` account-wide, so **any name beginning
`fieldsight-` is already covered**. Re-run it for the name you actually choose; the
result above is evidence, not a licence to skip the check.

> **Read the decision, not just the word.** Running this same simulate WITHOUT
> `--resource-arns` returns `implicitDeny` for all seven actions, because the policy
> grants `lambda:*` only on `function:fieldsight-*` while an omitted resource defaults
> to `*`. That looks exactly like a missing permission on a role that demonstrably
> deploys this stack every day. If a result contradicts something you know to be true,
> suspect the measurement before you suspect the system.

- [ ] **Step 16: Run the timeout-invariant test explicitly**

```bash
uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit/test_template_timeout_invariants.py -q
```
Expected: pass, including `test_a_scheduled_function_is_shorter_than_its_interval` for the new `FloorRecomputeFunction` alongside every existing scheduled function. This is a direct check, not incidental: this task's whole IAM/schedule addition is worthless if it trips the one test this repo already has for exactly this class of mistake.

- [ ] **Step 17: Write and run a dispatcher-level test**

Add a test (matching whatever existing dispatcher tests look like in `tests/unit/test_lambda_voiceprint_writer.py` or the equivalent file found in Step 0) that calls the writer's public entry point with the new event shape and asserts `recompute_company_floor` was called with the right `company_id`. Run:

```bash
uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit/test_lambda_voiceprint_writer.py -q
```
Expected: full pass, including every pre-existing test in that file (this touches the shared dispatcher, so a regression here is exactly the "half-changed state" this plan's sequencing exists to avoid).

- [ ] **Step 18: Run the full pre-existing voiceprint suite**

```bash
uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit/test_voiceprint_utils.py tests/unit/test_voiceprints_repo.py tests/unit/test_lambda_speaker_embed.py tests/unit/test_lambda_voiceprint_writer.py tests/unit/test_homogeneity_limit_override.py tests/unit/test_enrol_narrows_to_a_judgeable_window.py tests/unit/test_turn_name_overlay.py -q
```
Expected: all pass. This is the "not only the new tests" check the plan-level instructions require for any task touching a decision path.

- [ ] **Step 19: Commit**

If Step 3 found `0060` already taken and used the next free integer instead, substitute that number for every `0060` below, including the filename in `git add`.

```bash
git add src/migrations/0060_speaker_voiceprint_company_floors.sql src/repositories/voiceprints.py src/voiceprint_utils.py src/lambda_speaker_embed.py src/lambda_voiceprint_writer.py src/template.yaml tests/unit/test_voiceprints_repo.py tests/unit/test_voiceprint_utils.py tests/unit/test_lambda_speaker_embed.py tests/unit/test_lambda_voiceprint_writer.py
git commit -m "$(cat <<'EOF'
Add a per-company rejection floor, calibrated from human corrections only

decide_name's margin check confirms whoever is least wrong among enrolled
profiles even when nobody enrolled was in the room -- measured 2026-09-10, a
stranger confirmed at best=0.445, margin 0.268 clear of the runner-up. This
adds a final, demotion-only check: a would-be confirmation whose winning
score sits below this company's own calibrated floor is downgraded to
tentative instead.

The floor is derived per company from a low percentile of that company's
speaker_turn_names rows with source='correction' -- human assertions only.
Calibrating from decide_name's own confirmed output was considered and
rejected: it is circular (a wrong confirmation joins the set that would have
to reject it) and the exclusion of voiceprint_match, correction_propagation
and label_inheritance rows is proven by test, not just documented.

No floor (a missing row in speaker_voiceprint_company_floors) is a no-op --
every company runs margin-only, exactly as today, until it crosses the
minimum source='correction' sample count. The floor is computed by a
FloorRecomputeFunction on rate(6 hours) -- corrections accumulate slowly, so
hourly (ExtractionBacklogFunction's cadence, otherwise copied for shape)
would recompute against unchanged data most runs -- never at match time, so
it cannot move mid-decision.

Migration numbered 0060 (0059 was taken by the parallel report_chunks
full-text-index plan in this same worktree; confirmed free immediately
before this commit -- see Step 3).

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Margin scaling with the candidate pool

**Not safe alone, and not safe without Task 2 either**: a shrinking margin at scale without a floor underneath it would only make the 09-10 failure mode *more* likely to clear, not less (the spec's own reasoning in §4 is explicit that this is about not letting `DEFAULT_MIN_MARGIN` become an *impossible* bar at scale, not about loosening it below what the floor from Task 2 will catch). This task only ships the mechanism the spec asks for (§4): pool size threaded into the margin lookup, with today's fixed margin as the fallback below a size threshold. No curve is fit — the spec is explicit that fitting one now would repeat the +0.262 overfitting mistake.

**Files:**
- Modify: `src/voiceprint_utils.py` — `decide_name` gains pool-size-aware margin lookup
- Test: `tests/unit/test_voiceprint_utils.py`

**Interfaces:**
- `voiceprint_utils.effective_margin(pool_size, base_margin=DEFAULT_MIN_MARGIN, scale_threshold=DEFAULT_MARGIN_SCALE_THRESHOLD) -> float` — new pure function; below the threshold returns `base_margin` unchanged (today's behaviour byte-for-byte).
- `decide_name(scores, duration_s, ..., floor=None)` — `min_margin` continues to be accepted as an explicit override (existing callers/tests that pass `min_margin=0.9` etc. must keep working); when not explicitly overridden by the caller, `decide_name` derives it via `effective_margin(len(scores), ...)` rather than always using the bare constant.

- [ ] **Step 1: Write the failing tests**

Add to `tests/voiceprint_utils.py`... (append to `tests/unit/test_voiceprint_utils.py`, after the floor block from Task 2):

```python
# ----------------------------------------------------------
# Margin scaling with the candidate pool (2026-09-20 spec S4). profiles_for_matching
# returns every consented profile for a company, unlimited -- decide_name's runner-up is
# drawn from a POOL whose size, not the number of people actually in the room, is what the
# margin has to survive. A fixed margin comfortable at 6 profiles becomes a harder bar at
# 60 for reasons that have nothing to do with matching getting worse.
#
# No curve is fit here -- six enrolled voices across four dates cannot support one without
# repeating the +0.262 overfitting mistake this module's own docstring already warns
# against. What ships is the SHAPE: a pool-size-aware lookup, with today's fixed margin as
# the fallback below a size threshold, exactly as the floor falls back to "no floor" below
# its own minimum (S1.5) -- the same shape, twice, for the same reason.
# ----------------------------------------------------------


def test_below_the_scale_threshold_the_margin_is_unchanged():
    """The pool sizes every existing test in this file was written against (2-3 profiles)
    must reproduce today's DEFAULT_MIN_MARGIN exactly -- this task must not silently
    change any decision already pinned above."""
    assert vp.effective_margin(pool_size=3) == vp.DEFAULT_MIN_MARGIN
    assert vp.effective_margin(pool_size=vp.DEFAULT_MARGIN_SCALE_THRESHOLD) == vp.DEFAULT_MIN_MARGIN


def test_an_explicit_min_margin_override_is_never_replaced_by_the_scaled_value():
    """Existing callers pass min_margin explicitly (e.g. the fitted-cut test). An explicit
    override is a caller's deliberate choice and the scaling mechanism must not second-guess
    it -- only the DEFAULT is pool-size-aware."""
    d = vp.decide_name({"Ben": 0.5, "Zoe": 0.2}, duration_s=6.0, min_margin=0.9)
    assert d.status == "tentative"


def test_decide_name_still_defaults_correctly_at_small_pool_sizes():
    """Every pre-existing decide_name test above this block used 1-3 profiles and no
    min_margin override -- this pins that Task 3 did not move their outcomes."""
    d = vp.decide_name({"Ben": 0.48, "Zoe": 0.08, "Mike": 0.07}, duration_s=6.0)
    assert d.status == "confirmed" and d.name == "Ben"


def test_a_large_pool_uses_a_wider_effective_margin_by_default():
    """The mechanism, not a fitted number: a pool past the threshold must not silently keep
    using the same constant a 6-profile company gets, or DEFAULT_MIN_MARGIN would already be
    "the curve" in disguise."""
    small_margin = vp.effective_margin(pool_size=3)
    large_margin = vp.effective_margin(pool_size=vp.DEFAULT_MARGIN_SCALE_THRESHOLD * 5)
    assert large_margin >= small_margin
    assert large_margin > vp.DEFAULT_MIN_MARGIN, (
        "a pool well past the threshold that still gets exactly today's constant means "
        "nothing about scale actually changed the bar")


def test_decide_name_uses_pool_size_for_its_default_margin():
    """A margin that would confirm at a small pool size may no longer clear at a large one,
    with nothing else about the scores changed -- the mechanism reaching decide_name, not
    just existing as a standalone function."""
    scores_small_pool = {"Ben": 0.40, "Zoe": 0.24}   # margin 0.16, clears 0.15
    d_small = vp.decide_name(scores_small_pool, duration_s=6.0)
    assert d_small.status == "confirmed"

    huge_pool = {"Ben": 0.40, "Zoe": 0.24}
    huge_pool.update({f"stranger_{i}": 0.10 for i in range(vp.DEFAULT_MARGIN_SCALE_THRESHOLD * 5)})
    d_large = vp.decide_name(huge_pool, duration_s=6.0)
    assert d_large.status == "tentative", (
        "the same 0.16 margin over the SAME runner-up must be judged against a wider "
        "effective margin once the pool is large, or pool size never actually mattered")
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit/test_voiceprint_utils.py -q -k "margin and pool or scale_threshold or effective_margin"
```
Expected: `AttributeError: module 'voiceprint_utils' has no attribute 'effective_margin'` / no attribute `DEFAULT_MARGIN_SCALE_THRESHOLD`.

- [ ] **Step 3: Implement**

In `src/voiceprint_utils.py`, add near `DEFAULT_MIN_MARGIN`:

```python
# How many candidate profiles `profiles_for_matching` can return before the runner-up's
# expected closeness to the winner starts moving mostly because the pool grew, not because
# matching got worse. Like DEFAULT_FLOOR_MIN_SAMPLES on the other side of this codebase,
# this is the SHAPE of a fallback boundary, not a fitted number: six enrolled voices across
# four dates cannot support fitting a scaling curve (spec S4), so below this threshold the
# margin is exactly DEFAULT_MIN_MARGIN, unchanged.
DEFAULT_MARGIN_SCALE_THRESHOLD = 10

# How much wider the margin grows per profile once the pool exceeds the threshold. NOT
# measured against real multi-company data -- a placeholder shape (linear, small slope)
# that keeps the margin from becoming impossible at very large pools while explicitly
# leaving the curve itself for whoever calibrates against live confirmed-match volume
# across many companies, per spec S4's own refusal to invent one.
DEFAULT_MARGIN_SCALE_STEP = 0.01


def effective_margin(pool_size: int, base_margin: float = DEFAULT_MIN_MARGIN,
                     scale_threshold: int = DEFAULT_MARGIN_SCALE_THRESHOLD,
                     scale_step: float = DEFAULT_MARGIN_SCALE_STEP) -> float:
    """The margin `decide_name` should require, given how many candidates it was drawn from.

    `profiles_for_matching` returns every consented profile for a company, unlimited — the
    runner-up decide_name compares against is the maximum over however many rows that is,
    so the expected gap between winner and runner-up shrinks as the pool grows for reasons
    that have nothing to do with matching quality (docstring at `profiles_for_matching`,
    "the size of this result, not the number of people in the room, is what the margin has
    to survive"). Below `scale_threshold` this returns `base_margin` unchanged — every
    company today, and every company that narrows its matching by `site_id`, stays exactly
    where it is.
    """
    if pool_size <= scale_threshold:
        return base_margin
    return base_margin + scale_step * (pool_size - scale_threshold)
```

Change `decide_name`'s signature and margin lookup:

```python
def decide_name(scores, duration_s: float,
                min_turn_s: float = DEFAULT_MIN_TURN_S,
                min_margin: float | None = None,
                floor: float | None = None) -> Decision:
    """...
    `min_margin`, when given explicitly, overrides the pool-size-aware default entirely —
    an explicit choice by the caller is never second-guessed by the scaling mechanism.
    Left as `None` (the default), the margin required is `effective_margin(len(scores))`:
    unchanged for the small pools every company runs today, wider once a company's
    consented, non-withdrawn profile count grows past `DEFAULT_MARGIN_SCALE_THRESHOLD`.
    """
    if duration_s is None or duration_s < min_turn_s:
        ...  # unchanged
    if not scores:
        ...  # unchanged

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    best_name, best = ranked[0]
    if len(ranked) == 1:
        ...  # unchanged

    required_margin = min_margin if min_margin is not None else effective_margin(len(scores))
    margin = best - ranked[1][1]
    if margin < required_margin:
        return Decision("tentative", best_name, margin,
                        f"only {margin:.3f} clear of {ranked[1][0]}; below the "
                        f"{required_margin:.3f} margin this is a lean, not an identification")

    if floor is not None and best < floor:
        ...  # unchanged, from Task 2
    return Decision("confirmed", best_name, margin,
                    f"clear of the runner-up by {margin:.3f}")
```

Note the default change from `DEFAULT_MIN_MARGIN` to `None` on `min_margin` — grep every existing caller of `decide_name` (`grep -rn "decide_name(" src/`) before finishing this step and confirm none relies on the old default being exactly `DEFAULT_MIN_MARGIN` when they pass no `min_margin` at all; they should not, since `effective_margin` returns exactly `DEFAULT_MIN_MARGIN` for any pool at or below the threshold, but record the grep result in the report.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit/test_voiceprint_utils.py -q
```
Expected: all pass, including every test from Task 1 and Task 2's steps in this file.

- [ ] **Step 5: Prove the scaling test can go red**

Temporarily hardcode `required_margin = min_margin if min_margin is not None else DEFAULT_MIN_MARGIN` (i.e. remove the `effective_margin` call). Run:
```bash
uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit/test_voiceprint_utils.py -q -k "uses_pool_size_for_its_default_margin or large_pool_uses"
```
Expected: FAIL. Restore and re-run: pass.

- [ ] **Step 6: `site_id` narrowing note**

Read `src/repositories/voiceprints.py`'s `profiles_for_matching` `site_id` parameter and its three-arm SQL (already implemented, per its own docstring, "opt-in"). The spec (§4) asks that site-narrowed matching become the *default* mode once a company has enough enrolled people that the pool would meaningfully move a scaled margin. This plan does NOT change who calls `profiles_for_matching` with a `site_id` — that is an org-api/writer call-site decision about which mode to run in, gated by the same pool-size logic, and changing it touches the matching Lambda's request shape rather than the pure arithmetic this task is scoped to. Record in the report: which call site(s) currently omit `site_id` (grep `profiles_for_matching(` across `src/`), and flag this as follow-up work outside this plan's scope rather than silently doing nothing about it.

- [ ] **Step 7: Run the full pre-existing voiceprint suite**

```bash
uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit/test_voiceprint_utils.py tests/unit/test_voiceprints_repo.py tests/unit/test_lambda_speaker_embed.py tests/unit/test_lambda_voiceprint_writer.py -q
```
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add src/voiceprint_utils.py tests/unit/test_voiceprint_utils.py
git commit -m "$(cat <<'EOF'
Scale decide_name's margin with the candidate pool it was drawn from

profiles_for_matching returns every consented profile for a company,
unlimited -- decide_name's runner-up is the maximum over however many rows
that is, so a fixed 0.15 margin comfortable at 6 profiles mechanically
becomes a harder bar at 60, for reasons that have nothing to do with
matching quality.

effective_margin(pool_size) returns DEFAULT_MIN_MARGIN unchanged below
DEFAULT_MARGIN_SCALE_THRESHOLD (10) -- every company running today's
matching stays exactly where it is -- and grows linearly past it. The slope
is an explicit placeholder, not a fitted curve: six enrolled voices across
four dates cannot support fitting one without repeating the +0.262
overfitting mistake this module already rejected once. An explicit
min_margin argument still overrides the default entirely.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Multi-occasion enrolment — the practice, and the one guard check the spec actually asks for

**The spec is explicit (§3.2) that this is additive to the existing one-row-per-contribution design and needs no new storage shape** — `add_sample`/`upsert_profile` already support adding a contribution to an existing profile from a different recording session. What the spec asks this codebase to change is not a code path but a *source of enrolment samples* (turns already confirmed under the decision chain, per §3.2's "practical source") and a discipline about the existing homogeneity guard (§3.3: enrol from the chunks that pass `window_is_homogeneous`, discard the ones that do not — never loosen `DEFAULT_MAX_FRAME_SPREAD`). This plan does not invent a batch-enrolment API, a "multi-occasion" flag, or any new function, because the spec does not ask for one and doing so would be scope invented past what was specified.

**This task is safe only once Tasks 1-3 have landed**: per the spec's own reasoning (§ intro), multi-occasion enrolment without mean pooling and the floor makes the false-positive problem *worse* (pooled MAX turned a stranger into the top scorer on 09-10). Sequencing it last is not incidental.

**Files:**
- Modify: `src/repositories/voiceprints.py` — docstring addition to `add_sample` recording the practice and the guard discipline; no behaviour change.
- Test: none new — Step 2 re-runs `window_is_homogeneous`'s existing tests as the proof that the guard the practice depends on is unchanged.

- [ ] **Step 1: Document the practice, without touching behaviour**

In `src/repositories/voiceprints.py`, extend `add_sample`'s docstring (do not change its signature or body — the spec requires none):

```python
    """Record one enrolment contribution.

    One row per event rather than an averaged vector per person: §6's withdrawal needs each
    contribution individually removable, and an average cannot be un-poisoned.

    **Multi-occasion enrolment (2026-09-20 spec §3) is this function, called more than
    once, from more than one recording session.** A profile built from a single recording
    condition carries that condition's channel and room characteristics baked into the
    vector — the cross-session measurement found enrolments from one session do not
    recognise the same speaker in a different session's audio. There is no separate API:
    a clean read-aloud sample and a site-condition sample are both just calls to this
    function, distinguished only by `s3_key`/`window` pointing at different recordings and
    by `source` (both are `'correction'` when a human vouched for the window, whatever
    recording it came from).

    A site-condition sample cannot be manufactured; the practical source is a turn already
    `confirmed` for this profile under `decide_name`, with high margin and duration well
    over `DEFAULT_MIN_TURN_S`, offered back to this function as an ordinary enrolment. This
    is consent-compatible only when the underlying recording already carries consent for
    voiceprint use from that person — this function's own agreement guard and
    `upsert_profile`'s consent preconditions are not relaxed for this path.

    The homogeneity guard (`window_is_homogeneous`, called by the embedder before this
    function ever sees a window) is not loosened to admit a noisy-but-real target-
    environment chunk — it cannot tell "more background noise than its neighbours" from
    "two voices", and both look like a wider spread. The correct practice, not a code
    change: enrol from the chunks of a target-environment recording that pass the guard,
    and accept losing the ones that do not. A multi-occasion profile needs one homogeneous
    window from the new condition, not every chunk of it.

    `admitted_max_spread` is the homogeneity limit this window got past, and it is stored
    only when it was NOT the compiled-in default. NULL therefore means "the ordinary guard",
    and the non-NULL rows are exactly the ones worth re-examining if the loosened limit turns
    out to have been too loose — which is the whole reason the limit is settable.

    `correction_ref` and `created_by` are what make a bad enrolment traceable to everything
    it justified. They are optional in the signature and should not be: they are only
    optional because a future enrolment path may have no correction behind it.

    The guard lives HERE and not in a caller because there are two enrolment paths — the
    one folded into a propagation and the standalone one — and a rule that only one of them
    runs is a rule the other quietly does without. See `_agreement` for why it compares an
    order rather than testing a threshold.
    """
```

- [ ] **Step 2: Run the existing homogeneity and agreement tests unchanged, as proof nothing here needed a code change**

```bash
uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit/test_voiceprint_utils.py tests/unit/test_voiceprints_repo.py tests/unit/test_homogeneity_limit_override.py tests/unit/test_enrol_narrows_to_a_judgeable_window.py -q
```
Expected: all pass, byte-for-byte the same as before this task — this is the check that "multi-occasion enrolment" really did cost zero new source lines of behaviour, matching the spec's own claim.

- [ ] **Step 3: Confirm there is no step to skip here by grepping for a batch-enrol shape**

Run `grep -rn "def enrol\|def add_sample\|multi.occasion\|multi_occasion" src/` and confirm no other function already partially implements a multi-sample enrolment call that this task should have extended instead. Record the grep output in the report.

- [ ] **Step 4: Commit**

```bash
git add src/repositories/voiceprints.py
git commit -m "$(cat <<'EOF'
Document multi-occasion enrolment as a practice, not a new code path

The 2026-09-20 spec (S3) is explicit that a profile built from more than one
recording condition needs no new storage shape: add_sample already supports
adding a contribution to an existing profile, and a site-condition sample is
enrolled exactly the way any other sample is. This documents that on
add_sample's own docstring -- where the practice's source (a turn already
confirmed under decide_name) and its one real constraint (enrol from the
homogeneous chunks a target-environment recording produces, never loosen the
frame-spread guard to admit a noisy one) actually live -- rather than
inventing an API the spec does not ask for.

No behaviour changes. The existing homogeneity and agreement test suites are
re-run unchanged as proof of that.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: The mean-pooling cutover — discard pre-cutover corrections from the floor

**Why this cannot be folded into Task 2.** The floor's risk table (spec §7, last-but-one row) names a specific hazard: a `source='correction'` row's stored `best` score was computed under whichever aggregation (max, before Task 1's commit; mean, after it) was live when that correction was written. Mixing the two in one calibration set describes neither distribution correctly, because mean pooling systematically shifts `best` (Task 1's own measured table). This task lands the discard-and-rebuild the spec specifies, and it can only be written correctly once both Task 1 (so there is a "before"/"after" to discard between) and Task 2 (so there is a floor calibration query to filter) exist.

**Files:**
- Modify: `src/repositories/voiceprints.py` — `recompute_company_floor` gains a cutover timestamp filter.
- Test: `tests/unit/test_voiceprints_repo.py`.

**Interfaces:**
- `recompute_company_floor(conn, company_id, percentile=..., min_samples=..., since=MEAN_POOLING_CUTOVER)` — new `since` parameter, defaulting to a module-level cutover marker.

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_voiceprints_repo.py`:

```python
def test_recompute_excludes_corrections_from_before_the_mean_pooling_cutover():
    """Spec S7: a correction's stored `best` score was computed under whichever pooling was
    live when it was written. Mixing pre-cutover (max-pooled) and post-cutover (mean-pooled)
    scores in one calibration set describes neither distribution -- discard and rebuild from
    post-cutover corrections only."""
    conn = FakeConn([[{"n": 1}], [{"score": 0.30}]])
    voiceprints.recompute_company_floor(conn, CO, min_samples=1)
    count_sql = conn.calls[0]["sql"]
    assert "created_at >=" in count_sql or "created_at > " in count_sql, (
        "the calibration query must exclude rows written before the mean-pooling cutover")


def test_the_cutover_can_be_overridden_for_a_future_pooling_change():
    """The next arithmetic change to aggregate_scores will need the same discard-and-rebuild
    -- `since` must be a parameter, not a hardcoded date, or this becomes a one-time hack
    that has to be reinvented."""
    import datetime
    custom = datetime.datetime(2027, 1, 1, tzinfo=datetime.timezone.utc)
    conn = FakeConn([[{"n": 1}], [{"score": 0.30}]])
    voiceprints.recompute_company_floor(conn, CO, min_samples=1, since=custom)
    assert custom in conn.calls[0]["params"]
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit/test_voiceprints_repo.py -q -k cutover
```
Expected: FAIL — `recompute_company_floor` takes no `since` argument yet and its SQL has no `created_at` filter.

- [ ] **Step 3: Implement**

In `src/repositories/voiceprints.py`, add a module-level cutover constant near `DEFAULT_FLOOR_PERCENTILE` and thread it through both queries in `recompute_company_floor`:

```python
import datetime

#: When aggregate_scores switched from max to mean pooling (Task 1 commit, this plan).
#: Every source='correction' row written before this carries a `best` score computed
#: under the OLD pooling -- mixing it with post-cutover scores in one calibration set
#: would describe neither distribution, since mean pooling systematically shifts `best`
#: (spec S7). recompute_company_floor discards anything before this and rebuilds from
#: post-cutover corrections only; a company with too few of those simply returns to the
#: S1.5 no-floor fallback until enough accumulate -- a safe, already-specified state, not
#: a new one invented for this cutover.
MEAN_POOLING_CUTOVER = datetime.datetime(2026, 9, 20, tzinfo=datetime.timezone.utc)
```

```python
def recompute_company_floor(conn, company_id, percentile=DEFAULT_FLOOR_PERCENTILE,
                            min_samples=DEFAULT_FLOOR_MIN_SAMPLES,
                            since=MEAN_POOLING_CUTOVER) -> dict | None:
    """... (existing docstring, plus:)

    `since` excludes any `source='correction'` row written before it. Re-scoring an old
    correction under the new pooling would require re-running match arithmetic against
    embeddings that may since have been withdrawn — a clean discard-and-rebuild is cheaper
    and costs only a longer stay in the no-floor fallback (S1.5), which is already the safe
    state for a company with too little evidence.
    """
    _require_company(company_id)
    cur = conn.cursor(row_factory=dict_row)
    count_row = cur.execute(
        "SELECT count(*) AS n FROM speaker_turn_names "
        "WHERE company_id = %s AND source = 'correction' "
        "  AND score IS NOT NULL AND superseded_at IS NULL "
        "  AND created_at >= %s",
        (company_id, since)).fetchone()
    n = int((count_row or {}).get("n") or 0)
    if n < min_samples:
        return None
    rows = cur.execute(
        "SELECT score FROM speaker_turn_names "
        "WHERE company_id = %s AND source = 'correction' "
        "  AND score IS NOT NULL AND superseded_at IS NULL "
        "  AND created_at >= %s",
        (company_id, since)).fetchall()
    import numpy as np
    scores = np.array([float(r["score"]) for r in rows], dtype=np.float64)
    floor = float(np.percentile(scores, percentile))
    cur.execute(
        "INSERT INTO speaker_voiceprint_company_floors "
        "(company_id, floor, sample_count, computed_at) "
        "VALUES (%s, %s, %s, now()) "
        "ON CONFLICT (company_id) DO UPDATE "
        "  SET floor = EXCLUDED.floor, sample_count = EXCLUDED.sample_count, "
        "      computed_at = now()",
        (company_id, floor, n))
    return {"company_id": company_id, "floor": floor, "sample_count": n}
```

Note `speaker_turn_names.created_at` must exist for this filter to compile — confirm it does by reading migration 0038 (or wherever `speaker_turn_names` was created) before this step; every other query in this file already selects `created_at` from that table (`live_turn_names`), so it is expected to be present, but confirm rather than assume.

- [ ] **Step 4: Run to verify the new tests pass, and the whole floor suite still does**

```bash
uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit/test_voiceprints_repo.py -q
```
Expected: all pass, including every Task 2 floor test — they call `recompute_company_floor` without `since`, so they must still pass using the default cutover constant against `FakeConn`'s fixed result lists (the fake does not evaluate SQL, so the added `created_at >= %s` clause and parameter do not change what rows are "returned" in the test doubles — confirm this holds, since it is why Task 2's tests need no edits here).

- [ ] **Step 5: Prove it can go red**

Temporarily drop `"  AND created_at >= %s"` from both queries (and the corresponding `since` from the params tuples). Run Step 2's command. Expected: both cutover tests FAIL. Restore and re-run: pass.

- [ ] **Step 6: Commit**

```bash
git add src/repositories/voiceprints.py tests/unit/test_voiceprints_repo.py
git commit -m "$(cat <<'EOF'
Exclude pre-mean-pooling corrections from the floor's calibration set

A source='correction' row's stored best score was computed under whichever
pooling was live when it was written. Mixing pre-cutover (max-pooled) and
post-cutover (mean-pooled) scores in one calibration set describes neither
distribution, since mean pooling systematically shifts best (spec S7,
S2.2's measured table). recompute_company_floor now discards anything
written before the mean-pooling cutover and rebuilds from post-cutover
corrections only; a company with too few of those returns to the S1.5
no-floor fallback until enough accumulate, which is already a safe,
specified state rather than a new one invented for this cutover.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Full suite, PR into `develop`

**Files:** none modified (unless the suite finds a regression, fixed in the task that caused it).

- [ ] **Step 1: Full unit suite**

```bash
export UV_LINK_MODE=copy AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_DEFAULT_REGION=ap-southeast-2
uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit -q
```
Expected: all pass except known pre-existing skips. Record pass/skip counts in the report.

- [ ] **Step 2: Confirm migration numbering has not collided**

```bash
ls src/migrations | sort | tail -5
```
Confirm `0060_speaker_voiceprint_company_floors.sql` is the only new file and no other branch has since claimed `0060` (rerun this check immediately before opening the PR, not only when this plan was written, in case `develop` moved).

- [ ] **Step 3: Push and open the PR (controller decision point)**

```bash
git push -u origin <branch>
gh pr create --base develop --title "A rejection floor, mean pooling, and margin scaling for speaker voiceprints" --body-file <body file>
```

Body: what was wrong (09-10 — a stranger confirmed by margin alone at best=0.445), the three changes and why they only work together (mean pooling removes the pooled-max spike; the per-company floor catches a wide margin over implausible candidates; margin scaling keeps the bar reachable as the pool grows), the circularity guard and its test, the mean-pooling cutover, and the verification note below. End the body with:
```
🤖 Generated with [Claude Code](https://claude.com/claude-code)
```

- [ ] **Step 4: What can and cannot be verified before merge**

Record explicitly in the PR body and in the report: **prod currently runs `SPEAKER_IDENTITY_MODE=shadow` with zero stored voiceprints.** This means:

- **Can be verified pre-merge:** every unit-level claim in this plan (aggregation arithmetic, the floor's circularity exclusion, the margin-scaling shape, `decide_name`'s check ordering) — all of it is pure-function/fake-conn testable and none of it depends on live data.
- **Cannot be verified pre-merge, and must wait for a company's first real enrolments:** whether the floor's default percentile/minimum-sample placeholders (`DEFAULT_FLOOR_PERCENTILE=5`, `DEFAULT_FLOOR_MIN_SAMPLES=20`) or the margin-scaling placeholders (`DEFAULT_MARGIN_SCALE_THRESHOLD=10`, `DEFAULT_MARGIN_SCALE_STEP=0.01`) are the right numbers for any real company — the spec is explicit these are shapes, not fitted values, and zero stored voiceprints means there is no `source='correction'` history anywhere to check them against. Until a company enrols people and accumulates corrections, `company_floor` will return `None` for every company and `decide_name` will run exactly as it does today (margin-only) in shadow mode — which is the correct, already-specified fallback, not a bug to chase before merging.
- Merging into `develop` deploys TEST and is the owner's call, per this repository's standing practice; this plan does not merge.

---

## Deferred (named, not silently dropped)

- **Site-narrowed matching becoming the default at scale** (spec §4, second bullet) is a call-site decision in org-api/the writer about which mode to invoke `profiles_for_matching` in, not an arithmetic change to `voiceprint_utils.py` — flagged in Task 3 Step 6 as follow-up, not implemented here.
- **The floor's percentile, the minimum sample count, and the margin-scaling curve's slope** are explicitly left as placeholders per the spec's own refusal to fit them from six enrolled voices (§1.3, §1.5, §4, §5). This plan ships the mechanism only.
- **Chunk-level re-measurement of the 2026-09-17 recording against the 0.35 spread guard** (spec §5) is a data-analysis task on existing recordings, not a code change, and is out of scope for an implementation plan.
- **The scheduled trigger for `_recompute_floors`** is wired in Task 2 Step 14 as `FloorRecomputeFunction` on `rate(6 hours)`, modelled on `ExtractionBacklogFunction`'s shape — not deferred, since this repo already has nine `Schedule:`-triggered functions to copy from.
