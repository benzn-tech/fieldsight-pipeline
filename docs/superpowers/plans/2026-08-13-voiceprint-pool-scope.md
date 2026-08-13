# Plan — the matching pool: aggregation, size, scope

Spec: `docs/superpowers/specs/2026-08-13-voiceprint-pool-scope.md`

Ordering is load-bearing. Task 1 is a defect that makes every later measurement meaningless
if it is not fixed first: `decide_name` cannot confirm a person who has more than one
enrolled sample, so any "confirmed" number produced before Task 1 is measuring that bug.

Nothing in this plan changes live behaviour. `voiceprint_utils` and
`repositories/voiceprints` have **no callers** (grep-verified), so every code change here is
inert on both stacks. No template change, no IAM, no migration, no workflow — it cannot
collide with the batching or extraction work running in parallel.

---

## Task 1 — per-person aggregation, in one place

**Files:** `tests/unit/test_voiceprint_utils.py` (extend, FIRST), `src/voiceprint_utils.py`

`profiles_for_matching` returns one row per **sample** (`voiceprints.py:79-89`).
`decide_name` takes a flat `{name: score}` and has no way to know two scores belong to one
person. Phase 0's Ben held two profiles ~0.08 apart, so he would beat himself to
`tentative`.

Add `aggregate_scores(rows) -> {person_key: score}` next to `decide_name`, taking **max**
over a person's samples. Max because it is what "nearest profile" already did implicitly,
and because a mean would dilute a genuinely matching sample against a weak one.

`person_key` is the identity the caller wants named — `user_id` when set, else the profile
`id`, never `display_name` (two people can share a first name; this repo already has two
Jameses).

**Tests, each verified to fail with its own line removed:**
- two samples of one person collapse to one entry, scored at the max;
- a person with two samples no longer becomes their own runner-up — the Phase 0 Ben case,
  written with his measured numbers so the regression is named rather than abstract;
- profiles without `user_id` key on profile `id` and do not collide with each other;
- an empty row set returns `{}` and `decide_name` reports `unknown` (not a crash).

**Open decision to record, not to settle here:** whether one person's Chinese and English
profiles are the same `person_key`. Same person for naming; possibly separate for "same
voice again". Task 1 keys on `user_id`, which makes them the same when both are linked to
one user — sufficient for now, and the place the answer will live when it is needed.

---

## Task 2 — rebuild the experiment material

**No repo files.** Working directory outside the repo; nothing committed but the results.

1. Pull the two Phase 0 sessions from prod S3 — `318601b22d36…` and `b49627d7a6a4…`, 6
   objects each under `users/…/audio/`, verified present.
2. Convert the six enrolments from `Dropbox/AI/Field_Sight/diarization/voiceprint/*.mp3` to
   16-bit PCM WAV — both scripts read WAV only (`read_wav_mono` raises otherwise).
3. Rebuild the `--work` layout `speaker_session_eval.py` expects.

**Ground truth is the script**, `scripts/fixtures/2026-08-11-blockv-scripts.json` (present).
`fieldsight-vad-check/2026-08-11-blockV-script/GROUND_TRUTH.txt` is a blank template that was
never filled in — do not spend time looking for data in it.

**Six enrolments, five voices** (Ben has English and Chinese). Any statement about pool size
must count profiles, not people.

---

## Task 3 — make the harness answer the question

**Files:** `scripts/speaker_session_eval.py`

It reports nearest-profile accuracy and separability. It computes neither output the spec
calls the point, and it carries two hazards that would corrupt a distractor run.

- Call `decide_name` (through Task 1's aggregation) and report the three-state
  **confirmed / tentative / unknown** split.
- Count **wrong-confident** separately — `confirmed` with the wrong name. It is the number
  that decides whether 0.15 is safe.
- Report the **weakest floor-eligible same-person score**. The spec's first draft quoted
  +0.104, which is the 2.1 s turn the duration floor now excludes; the number the margin
  actually has to clear is not recorded anywhere.
- **Remove the three-letter prefix match and the hard-coded `ben`-merge**
  (`:145`, `:152`, `:156`). A distractor named `benny` is currently scored as the wearer,
  silently. Match on the full profile key instead.

Verify the rewrite reproduces Phase 0's 31/32 **nearest-profile** figure before trusting any
new number out of it. It will **not** reproduce 31/32 *confirmed* — that is Task 1's finding,
and seeing the gap is the point.

---

## Task 4 — experiment A: how the pool degrades with size

Sweep pool ∈ {6, 12, 20, 50, 100}, the 32 ground-truth turns fixed.

Distractors, in preference order: other real sessions on the same device first; a public
corpus only to reach the larger points. **Label every point that used corpus voices** — they
are cleaner than site audio and flatter the curve exactly where the answer matters. Source
(1) realistically yields about a dozen voices against a ~20-device fleet, so 50 and 100 will
be mostly corpus.

**Pre-registered, written before the run:**
1. `confirmed` share falls monotonically as the pool grows.
2. Wrong-confident stays at or near zero for enrolled speakers.

Prediction 2 is a hypothesis, not a mechanism — nothing in `decide_name` orders the failure
modes. **If it fails, the finding is that 0.15 is not a safe margin**, and that outranks the
accuracy curve.

---

## Task 5 — experiment B: the speaker who is not in the pool

The case Task 4 cannot see, and the one tighter scoping makes **worse**.

Enrol four of the five voices, score the fifth's turns against that pool, count how often it
is named anyway. Repeat leaving out each voice in turn — five runs, same material.

This measures the cost side of the scoping trade. Task 4 measures only the benefit side, and
deciding scope on Task 4 alone would be deciding on half the evidence.

---

## Task 6 — the scope argument, added before there are callers

**Files:** `tests/unit/test_repositories_voiceprints.py` (extend), `src/repositories/voiceprints.py`

`profiles_for_matching(conn, company_id)` gains a scope parameter. Free today —
grep-verified that only tests reference it — and expensive once Phase 4 and Phase 5 both call
it.

**The decision this task must make explicit rather than encode by accident:** under
site-scoping, what happens to profiles with `user_id IS NULL`? There is no site column on
`speaker_voiceprints` (`0038:22-34`); the reachable join is through
`memberships(user_id, site_id)`, and it only reaches profiles that have a user. Unnamed
profiles — the "same recurring voice before anyone names it" the nullable column exists for —
would **silently vanish** from a site-scoped pool.

Three options, and one must be chosen in writing: always in the pool regardless of scope;
scoped by where their samples were observed (needs a column or a join through `s3_key`); or
excluded, dropping the recurring-voice feature.

This is the empty-list-means-no-filter shape this codebase has already been bitten by. A
test must pin whichever answer is chosen, with the reason.

**A-vs-B itself stays deferred** until Tasks 4 and 5 have run. If the curve shows 100
profiles is fine, company-wide is simpler and should win.

---

## Execution order and gates

```
Task 1 ──► Task 3 ──► Task 4 ──┐
             ▲                 ├──► Task 6 (scope decision)
Task 2 ──────┘         Task 5 ─┘
```

- **Task 1 gates everything downstream.** A "confirmed" number produced before it is
  measuring the aggregation bug.
- Tasks 4 and 5 both gate Task 6's A-vs-B decision. Task 6's *signature* change may land
  earlier; only the choice of default waits.
- Phase 3 of the identity plan (`SpeakerEmbedFunction`) is independent and may proceed in
  parallel — it builds the embedder, this decides what pool it will search.

## What this does not do

- Does not touch `decide_name`'s thresholds. 0.15 and 3.0 s stay as they are; Task 4 may
  produce evidence that 0.15 is wrong, and changing it is a separate decision on that
  evidence.
- Does not build enrolment. No code here creates a `speaker_voiceprints` row — there is still
  no function that does, which is Phase 4's work.
- Does not re-run Phase 0's gate. It passed; this measures a different axis of the same
  result.
