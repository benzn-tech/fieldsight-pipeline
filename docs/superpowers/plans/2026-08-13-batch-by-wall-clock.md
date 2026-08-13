# Implementation plan — batching by wall clock (greedy window, gap-bridging)

**Date:** 2026-08-13 · **Spec:** `docs/superpowers/specs/2026-08-13-batch-by-wall-clock.md`
**Verdict of the adversarial review: GO WITH CHANGES.** This plan IS the spec plus the
required changes from that review; where the two disagree, this plan wins. The changes are
recorded in §0 so nobody re-derives the spec's original wording.
**Branch:** `feat/batch-by-wall-clock` off `origin/develop`.
**Prod impact:** none (`BATCH_TRANSCRIPTION=false` on prod, verified live 2026-08-13).
**TEST impact:** real — `BATCH_TRANSCRIPTION=true` on test, so any merged phase that the
transcriber actually calls changes TEST behaviour on the next develop deploy. Each phase
below states whether merging it alone changes TEST.

Run tests exactly as the repo does:

```
export UV_LINK_MODE=copy
export AWS_ACCESS_KEY_ID=x AWS_SECRET_ACCESS_KEY=x AWS_DEFAULT_REGION=ap-southeast-2
uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit -q
```

2569 pass at branch point. Strict TDD: every phase writes its RED test first and watches it
fail before implementation. After any fix that pins writer↔reader agreement, **revert the
writer's half and confirm the test goes red** — this feature has already lost a fix and its
tests to a whole-file rewrite with CI green throughout.

---

## 0. Decision record — what the review changed and why (do not re-litigate without new data)

### 0.1 Two clocks, and each one gets exactly one job

The spec's sentence "a batch seals when `now >= window_end + BATCH_SEAL_GRACE_SEC`" mixes
clocks: `window_end` derives from the chunk filename (DEVICE local clock, naive NZ time)
while `now` is server epoch. Compared directly they are ~12–13 h apart; even converted, a
backlogged offline session (uploads hours late — the spec's own words, and the measured
thundering-herd reality) would find `window_end` long past and **seal on first arrival with
zero effective grace**, permanently excluding its sisters uploading seconds later.

Rule as implemented:

- **Membership** is decided on device base times only ever compared to each other
  (deltas between filenames — same clock, offset cancels). Never compared to `time.time()`.
- **Readiness** (when to seal) is decided on `registered_at` (server epoch), exactly as
  `pending_runs` does today: a window seals when its newest member's registration is older
  than `BATCH_SEAL_GRACE_SEC`. "Quiet for grace" — correct for live capture AND for a
  backlog herd that arrives in ten seconds.

### 0.2 A sealed batch's members are consumed, and the planner must know it

"The first surviving chunk not already in a batch" is currently undefined: the ledger
records members and seal claims, but nothing marks a member as *belonging to* a sealed
batch, and `pending_runs` plans over ALL registered members every time. Today the seal key
`SEAL#{first_index}` hides this most of the time — but a late-arriving EARLIER chunk shifts
the run's first index and the collision protection evaporates:

> members {4,5} seal after the deadline as `SEAL#0004`. Chunk 3 arrives an hour later.
> `plan_batches({3,4,5})` → `[3,4,5]`, claim `SEAL#0003` succeeds, and chunks 4 and 5 are
> **sealed, transcribed and billed a second time**, their turns duplicated in the session.

That is a live latent bug in the shipped consecutive-index rule, and greedy time anchoring
makes it routine (any late chunk earlier than an open anchor moves the anchor). The grace
period does not fix it; it only lowers the frequency. So:

- Sealing writes `sealed_into` onto every member row (same flow as the claim), and the
  planner excludes consumed members. A chunk arriving after the batch covering its
  time/index range sealed **forms its own batch** (spec's stated intent, now enforceable).
- The seal record keeps `SEAL#{first_index}` as its key — with consumed-exclusion the first
  index of a window is stable, so the existing claim race stays sound.

### 0.3 The common case must not wait 150 s (early seal)

Today a complete 4-run seals on arrival of its 4th chunk
(`test_four_consecutive_chunks_seal_immediately`). The spec's wall-clock rule as written
seals only at `window_end + grace`, i.e. it adds the 150 s it complains about **to all 383
common-case batches** while removing it from 26 singletons. Restored rule: a window also
seals immediately when nothing more can join it —

- its member indices are contiguous (no interior hole a late chunk could fill), AND
- the next index has registered with a base time at/past the window end (exclusive
  boundary: a chunk at `anchor + BATCH_WINDOW_SEC` starts the next batch), OR the count cap
  is hit.

A window with an interior index hole always waits for grace: the hole may be a VAD drop
(never coming — no event distinguishes it) or a late upload (coming), and grace is the only
arbiter. That asymmetry is the design, not an accident.

### 0.4 One-member windows are not batches (singleton bypass)

Evaluated as a competitor to the whole change: "never create batches of one" alone is ~20
lines, kills all 50 pure-loss singleton artifacts, adds zero coordinate-system or
hallucination risk — but it does NOT deliver the actual justification (a conversation
interrupted by one silent chunk still splits into two requests with unrelated speaker
labels, in 37 % of sessions), and it cannot avoid the grace wait (you must wait to know the
chunk stays alone). So it is **adopted as part of this change, not instead of it**: a window
that seals with one member is transcribed directly — no `_bn1` WAV, no map, no rebase, seal
record `status: bypassed`, and the member is re-driven through the normal per-chunk path.
The spec's own table says 26 singletons remain under the greedy window; after this, zero.

### 0.5 The map is embedded in the transcript, not fetched by every reader

The spec's §5 ("`_rebase_batch_turns` moves into all four call sites, each new site needs
IAM to read `audio_segments/*`") multiplies exactly the failure mode that has broken this
feature silently three times — a reader-side `except`-and-log fallback behind a missing IAM
grant. And the spec's call-site table is wrong:

| consumer | spec says | actually |
|---|---|---|
| `lambda_extract_session` | calls `normalize_transcript`, rebases | correct |
| `lambda_ask_agent` (line 232) | calls, doesn't rebase | correct |
| `lambda_ingest` (line 463) | calls, doesn't rebase | correct |
| `lambda_org_api` viewer | "calls `normalize_transcript`" | **does not** — own arithmetic (`file_time_sec + seg_start`, ~5153–5190) **plus a file-window prefilter (5138) that uses the `_to` token and under-spans a gap batch, wrongly excluding whole files from a topic window** |
| `lambda_meeting_minutes` (line 294) | **absent from the spec** | calls `normalize_transcript`, drives the minutes timeline |
| `lambda_report_generator` (997–1058) | absent | filename arithmetic, file-level timestamp only — batch start is right by construction; accepted degrade, documented |

Design instead: **the transcriber embeds the batch map into the transcript JSON it writes**
(top-level key `fieldsight_batch_map`), and a pure shared function rebases from the embedded
map. Readers then need **no S3 read and no IAM at all** — the map travels inside the object
every reader already holds. The sidecar under `audio_segments/` stays (it is written before
the WAV and remains the recovery artifact + the fallback for pre-change batch transcripts).

Why this is possible: the ElevenLabs path is synchronous — `lambda_transcribe` writes the
transcript JSON itself, and it already reads `audio_segments/*` (it read the batch WAV in
the same invocation), so fetching the sidecar to embed it adds **no new grant** (verified,
not assumed — Phase 7). The AWS Transcribe provider writes its own output
(`OutputBucketName`/`OutputKey`, lambda_transcribe.py:240) with no write hook, so
**batching + `ASR_PROVIDER=transcribe` is a refused combination**: loud log, per-chunk
passthrough, same shape as the existing whole-chunk guard. Test runs EL; this costs nothing
today and fails loud instead of silent if the provider ever flips back.

Rebase-at-write (rewriting `word.start` so filename arithmetic comes out absolute) was
argued and rejected: it would fix every consumer with zero reader changes, but it breaks
`start_sec`/`end_sec` as in-file offsets (the evidence/playback paths seek the batch WAV
with them — extract_session keeps them batch-relative on purpose), and it has no write hook
on the Transcribe provider either. Embedding gets the same zero-IAM property without
corrupting the offset contract.

### 0.6 Gap seams are expected to measure zero

`measure_trim` across non-adjacent chunks compares audio that shares no ring-buffer copy:
the correct result is 0, but today's code would record `trim_measured: false` and log the
"could not read / unmeasured seam" warning — polluting both the trim-distribution telemetry
and the alarm signal (a wall of `false` is supposed to mean the byte comparison is broken).
Members gain `"seam": "adjacent" | "gap" | "first"`; gap seams skip measurement entirely,
trim 0, and are excluded from the unmeasured-seam alarm.

### 0.7 Env vars: renamed with a live check, window added, cap kept

- Verified: `BATCH_MAX_CHUNKS` and `BATCH_SEAL_DEADLINE_SEC` are **code-default-only** —
  neither exists in `src/template.yaml` (only `BatchTranscription` is wired, lines 538/974/2086).
  So the rename to `BATCH_SEAL_GRACE_SEC` touches code and tests only. Guard anyway
  against the unwired-toggle trap: code reads `BATCH_SEAL_GRACE_SEC`, falls back to
  `BATCH_SEAL_DEADLINE_SEC`, then `150` — and Phase 7 reads the two deployed functions'
  live env to confirm nobody ever set the old name by hand.
- `BATCH_WINDOW_SEC` (default `120`) joins as a code default, same posture as the cap.
- `BATCH_MAX_CHUNKS=4` stays as a **safety cap only**. Demotion is safe for the filename
  (`_bn{K}` takes any K) and `is_batch_key` (regex `\d+`). Known limitation, documented not
  fixed here: a device emitting much shorter chunks hits the count cap before the window
  and quietly reverts to count-shaped batches; if `segmentSeconds` ever changes on a real
  device, revisit with a duration cap (`≤ 300 s` kept audio — the EL 8-min parallel-split
  boundary from the 08-11 plan is the hard ceiling).

### 0.8 Hallucination risk (§6) — what the spec understates, and the mechanical check

The bridged gap splices non-contiguous audio; §6 covers fabricated bridging clauses but
misses that **the provider can emit a single turn that spans the splice**, fusing utterances
up to two minutes apart into one turn whose text straddles the gap — after rebasing, that
turn's `abs_start`/`abs_end` are individually correct but the interval covers time when
nobody spoke, and photo binding / claim provenance quote-matching operate on that interval.
EL is also specifically known to pad toward fluent sentences. And the spec's verification
("read the turns either side, once") is a single manual spot-check — the verified-the-wrong-
thing shape. Replaced by a mechanical flag (Phase 6): at rebase time, any turn whose span
crosses a member boundary with an absolute-time discontinuity is marked `crosses_gap: true`
and counted in a log metric; the manual read is then OF the flagged turns, on TEST, with
a known-gap session.

### 0.9 Unchanged, verified against the code (not assumed)

- `batch_stitch.resolve_abs_time` is already correct for non-contiguous members: each
  member carries its own `abs_start`, `batch_offset_sec` is the running sum of kept
  durations (contiguous in the concatenated file by construction), and the within-member
  clamp stops any spill into the gap. **The map format needs no change** — the spec's claim
  holds. Pinned by a new test anyway (Phase 1), because it is now load-bearing.
- Forbidden territory untouched: VAD threshold/merge rules, normalisation order,
  `DROP_SILENT_CHUNKS`, mics. `tests/unit/test_vad_tuning_rationale.py` must stay green
  and unedited.

---

## Phases

Dependency order: 1 → 2 → 3 → 4; 5 is independent of 2–4 (it fixes a defect that exists on
TEST **today** — batched transcripts are already mis-timed in every consumer except
extract); 6 needs 5; 7 is verification, after 4 and 5 are deployed.

---

### Phase 1 — the window planner and the map pin (`batch_stitch`) [INERT — nothing calls it yet; safe to merge alone]

**RED first**, `tests/unit/test_batch_stitch.py` (extend):

1. `test_a_vad_dropped_chunk_does_not_split_the_window` — `plan_windows(members, window_sec=120, cap=4, consumed=set())`
   with members `{4: t+0, 6: t+60, 7: t+90}` (5 dropped) asserts **one** window `[4, 6, 7]`.
   Fails now: `plan_windows` does not exist.
2. `test_a_chunk_at_the_window_boundary_starts_the_next_batch` — member at `anchor+120.0`
   exactly is NOT in the anchor's window (exclusive end; four 30 s chunks stay one batch,
   a fifth at +120 does not make a 150 s request).
3. `test_the_window_anchors_on_its_first_member_not_on_a_grid` — members at
   `t+110, t+140, t+170` → one window (the spec's 480-vs-470 grid argument, pinned).
4. `test_a_consumed_chunk_is_invisible_to_the_planner` — `consumed={4, 5}` with members
   `{3, 4, 5}` → the only window is `[3]`. This is the §0.2 double-billing pin at the pure
   layer.
5. `test_the_count_cap_still_binds_when_chunks_are_short` — 12 members 10 s apart,
   `cap=4` → windows of 4.
6. `test_the_map_resolves_correctly_across_a_bridged_gap` — build a map whose members are
   `abs_start` `T+0` (kept 28 s) and `T+60` (kept 30 s); assert `resolve_abs_time` at
   `t=27.9 → T+27.9…`, at `t=28.0 → T+60+trim`, at `t=57.9` clamps inside member 2 —
   i.e. no resolved time ever lands inside the 60→…gap. (Pins §0.9; should pass on
   current code — if it fails, STOP: the spec's "map needs no change" claim is wrong and
   the design decision reopens.)

**Change:** add `plan_windows(members: dict[int, float-or-datetime], window_sec, cap,
consumed)` beside `plan_batches` (which stays, untouched, until Phase 4 removes its last
caller — its tests keep passing meanwhile). Pure module rules unchanged: no boto3, no env.

**Verified by:** the suite. No deploy, no IAM.

---

### Phase 2 — ledger: consumed members, server-clock readiness, early seal [INERT — nothing calls it yet]

**RED first**, `tests/unit/test_batch_ledger.py` (extend):

1. `test_a_late_chunk_never_rebuilds_a_batch_that_already_sealed` — register 4, 5; seal the
   window (mark members consumed); register 3; assert the next planning pass yields only a
   window `[3]` and the seal claim for it is `SEAL#0003` **with members `[3]` only**.
   Fails now (yields `[3,4,5]` — the §0.2 bug, demonstrated red before it is fixed).
2. `test_readiness_is_judged_on_registration_time_not_the_device_clock` — a member whose
   filename base time is 10 hours ago but whose `registered_at` is `now` is NOT ready
   until `grace` has passed. Fails against any implementation of the spec's literal
   `window_end + grace` sentence; this is the clock-mixing pin.
3. `test_a_full_window_with_its_successor_registered_seals_without_waiting` — contiguous
   `[0,1,2,3]` in-window, `4` registered at `anchor+120` → ready at `now`, no grace.
4. `test_a_window_with_an_interior_hole_always_waits_for_grace` — `[4, 6]` (5 absent) with
   7 registered outside the window → NOT ready until quiet-for-grace, because 5 may still
   arrive (grace is the only arbiter of VAD-drop vs late — §0.3).
5. `test_sealing_marks_every_member_consumed` — after `mark_sealed`, member rows carry
   `sealed_into` and `list_unconsumed_members` excludes them.

**Change:** `batch_ledger` gains `mark_members_consumed(table, session_id, indices, batch_first_index)`
(called from the seal flow between map-write and `mark_sealed`), a `sealed_into` attribute
read back by a new `pending_windows(rows, now, grace_sec, window_sec, cap)` that replaces
`pending_runs`'s role (old function stays until Phase 4 flips the callers; its own tests
untouched until then, deleted in Phase 4 **individually by name, never by file rewrite**).

**Verified by:** the suite (fake table double, as today). No deploy, no IAM.

---

### Phase 3 — sealer: gap seams and the singleton bypass (`batch_seal`) [INERT — nothing calls the new paths yet]

**RED first**, `tests/unit/test_batch_seal.py` (extend; keep driving the real `batch_seal`
against a dict-backed fake S3 — the writer side of the S3-double shape):

1. `test_a_gap_seam_is_not_measured_and_not_reported_as_unmeasured` — seal `[4, 6]`; assert
   `measure_trim` is never called for the 4→6 seam, the member records
   `{"seam": "gap", "trimmed_head_sec": 0.0, "trim_measured": true}` and no warning log.
   Fails now (unconditional `measure_trim` for `pos > 0`, records `false`).
2. `test_a_window_of_one_is_bypassed_not_sealed` — seal a singleton run; assert **no**
   `_bn` WAV and no map are written, the seal record has `status: bypassed`, the member is
   marked consumed, and the re-drive hook was invoked with the member's unit key.
3. `test_a_bypassed_member_is_transcribed_when_its_event_comes_back` — (lands in Phase 4's
   test file, listed here because the bypass mechanism spans both) — see Phase 4 RED 3.

**Change:** `seal_batch` takes the member list with seam kinds from the planner; singleton
runs short-circuit into `bypass_singleton(...)`: mark consumed + `status: bypassed`, then
re-drive the unit through the normal path via `s3.copy_object` onto its own key (the fresh
S3 event re-enters `lambda_transcribe`; no transcribe client enters the sweep's import
graph, which is why copy-to-self is the mechanism and not a direct transcription call).

**Verified by:** the suite. No deploy, no IAM.

---

### Phase 4 — flip the transcriber and the sweep to windows [CHANGES TEST ON MERGE — the flag is already on there]

**RED first**, `tests/unit/test_lambda_transcribe_batching.py` (extend) and
`tests/unit/test_finalize_batch_seal.py` (extend):

1. `test_the_window_not_the_count_decides_membership` — events for c4, c6, c7 (c5 never
   arrives), then grace elapses: exactly **one** batch WAV containing 4, 6, 7. Fails now
   (two batches, `[4]` and `[6,7]` — the spec's motivating case, red before green).
2. `test_a_consumed_member_arriving_again_is_not_rebatched` — duplicate S3 delivery of a
   member that is `sealed_into` a batch → no registration, no new window, and NOT
   transcribed per-chunk either (it is in a batch; transcribing it too is double billing).
3. `test_a_bypassed_member_event_falls_through_to_normal_transcription` — the copy-to-self
   event for a bypassed singleton: `_maybe_batch` sees `sealed_into == "direct"` and
   returns False → the provider call happens, exactly once.
4. `test_the_grace_env_var_falls_back_to_the_old_name` — with only
   `BATCH_SEAL_DEADLINE_SEC=99` in the env, the module-level grace is 99 (§0.7 rename
   guard).
5. Sweep: `test_the_tail_seal_still_uses_deadline_zero_under_the_window_rule` — at session
   close the open window seals regardless of grace (pins the twice-broken line in
   `_seal_tail_batches` against this refactor erasing it a third time).

**Change:** `_maybe_batch` and `_seal_tail_batches` route through
`pending_windows`/`plan_windows`; new module envs `BATCH_WINDOW_SEC` (120) and
`BATCH_SEAL_GRACE_SEC` (fallback chain per §0.7). Delete `plan_batches`/`pending_runs` and
their tests **by named, single-anchor edits** (Windows CRLF repo: single-line Edit anchors,
never `git add -A`, never a whole-file write).

**Merge note:** merging deploys the behaviour change to TEST. Merge alone, on a morning
someone will watch that day's sessions, not in a stack with 5.

**Verified against live state (not the deploy record):**
`aws lambda get-function-configuration` on `fieldsight-test-transcribe` and the finalize
sweep function — confirm `BATCH_TRANSCRIPTION=true` is still what's live, confirm nobody
ever hand-set `BATCH_SEAL_DEADLINE_SEC`/`BATCH_MAX_CHUNKS` on the live env (the fallback
must not silently change an operator's tuned value), and after the first real session with
a VAD-dropped interior chunk: the S3 listing shows one bridging `_bn` WAV, **zero `_bn1`
objects anywhere in the prefix**, and no member transcripts for consumed members.

---

### Phase 5 — the map travels inside the transcript; every absolute-time consumer rebases [reader half INERT; writer half CHANGES TEST ON MERGE]

The defect this fixes is live on TEST **today**: ask_agent, ingest, meeting_minutes and the
org_api viewer already render batched turns on filename arithmetic, quietly early by the
trimmed overlap — and after Phase 4, early by up to the bridged gap. This phase does not
depend on Phases 2–4 and may land first.

**5a — readers first (inert until a writer embeds; safe to merge alone).**

New pure function in `batch_stitch` (or `transcript_utils` — it must stay import-clean):
`rebase_turns_from_embedded_map(normalized, transcript_data, filename)` — reads
`transcript_data["fieldsight_batch_map"]`, applies `resolve_abs_time` turn-by-turn exactly
as `_rebase_batch_turns` does, returns `normalized` untouched when the key is absent.
No S3 client anywhere in its signature. **This is what kills the IAM failure mode: a reader
with no S3 read cannot silently lose one.**

**RED first** — one test per consumer, each driving the REAL writer against the REAL reader
through one dict-backed S3 double (the shape that caught the `transcripts/` vs
`audio_segments/` drift; never a hand-built map asserted at the reader's key):

1. `tests/unit/test_batch_map_travels.py::test_the_reader_gets_its_times_from_what_the_writer_actually_wrote`
   — run `batch_seal.seal_batch` against the fake S3, feed its sidecar through the Phase 5b
   embed step, hand the resulting transcript JSON to `rebase_turns_from_embedded_map`;
   assert every turn's `abs_start` equals the per-chunk truth built from the same synthetic
   timeline, within one sample period. **Then revert the embed step locally and watch this
   test go red before proceeding** (the whole-file-rewrite lesson, made a step).
2. `test_lambda_ask_agent.py::test_ask_agent_rebases_batched_turns_from_the_embedded_map`
3. `test_lambda_ingest.py::test_ingest_rebases_batched_turns_from_the_embedded_map`
4. `test_meeting_minutes.py::test_meeting_minutes_rebases_batched_turns_from_the_embedded_map`
   (the consumer the spec's table omitted)
5. `test_lambda_org_api_media.py::test_the_viewer_prefilter_spans_a_gap_batch` — a batch
   transcript whose embedded map ends 90 s after `base + (to − off)`: the file must NOT be
   excluded from a topic window that covers only the post-gap minute
   (`_org_transcript_file_end_sec` derives the end from the embedded map for batch keys),
   and `test_the_viewer_rebases_batched_segment_times` for the `file_time_sec + seg_start`
   arithmetic at ~5153.
6. `test_lambda_extract_session.py::test_extract_prefers_the_embedded_map_and_falls_back_to_the_sidecar`
   — embedded map wins; a pre-change batch transcript (no embedded key) still rebases via
   the existing sidecar fetch, which stays.

`lambda_report_generator`: no change — file-level timestamps only, batch start is correct
by filename; the accepted degrade is recorded here and nowhere else needs to know.

**5b — writer (changes TEST on merge).**

In `lambda_transcribe`'s synchronous (ElevenLabs) write path: when the source key
`is_batch_key`, fetch the sidecar (`batch_stitch.map_key_for_audio` — same one-function
key derivation the writer used) and embed it as `fieldsight_batch_map` before the
`transcripts/` put. **A missing sidecar here raises** — the map is written before the WAV
by seal order, so absence is a real fault; this is the fail-loud site the feature has
lacked, not another warn-and-continue. When `ASR_PROVIDER == 'transcribe'` and
`BATCH_TRANSCRIPTION` is on: loud log + per-chunk passthrough (no batching), pinned by
`test_batching_refuses_the_async_transcribe_provider`.

**IAM:** the transcribe function already reads `audio_segments/*` (it reads the batch WAV
in the same invocation), so the sidecar fetch **should** need no new grant — Phase 7 proves
it with `simulate-principal-policy` instead of trusting this sentence. **No other function
gains or needs any S3 permission in this entire plan.**

**Merge order:** 5a first (inert — no transcript carries the key yet, extract's sidecar
path still covers TEST), then 5b. Never 5b alone before 5a for extract's fallback reason —
old artifacts must keep working.

---

### Phase 6 — splice-crossing turns are flagged, not eyeballed [needs 5; INERT until batched material flows]

**RED first:**

1. `test_batch_map_travels.py::test_a_turn_that_spans_the_splice_is_flagged` — a turn whose
   `start_sec`/`end_sec` straddle a member boundary where the members' absolute times are
   discontinuous (gap > 1 s) comes back with `crosses_gap: true`; a turn crossing an
   adjacent (trim-only) boundary does not.
2. `test_a_flagged_turn_count_is_logged_per_batch` — the rebase step logs
   `batch_splice_turns=<n>` once per batched transcript (0 included — the positive
   evidence that the check ran, per the every-guard-needs-a-success-signal rule).

**Change:** inside `rebase_turns_from_embedded_map`. Downstream consumers ignore the field;
photo binding and claim provenance MAY later choose to treat flagged turns specially — out
of scope here, the flag just makes the risk visible.

**Verified on TEST:** CloudWatch Logs Insights over a week of batched sessions — the
distribution of `batch_splice_turns`, and a manual read of every flagged turn's text on one
known-gap session (the spec's §6 check, now aimed at exactly the suspect turns instead of
sampled blind).

---

### Phase 7 — live verification, TEST [no code; gates nothing but the "done" claim]

Every item reads the deployed stack, never the template or the workflow file:

1. **Env, both functions:** `aws lambda get-function-configuration` on
   `fieldsight-test-transcribe` and the finalize sweep — `BATCH_TRANSCRIPTION=true`,
   grace/window values as intended, old `BATCH_SEAL_DEADLINE_SEC` absent (or, if present,
   §0.7's fallback honoured it — check which value is live before calling anything tuned).
   Prod: `BATCH_TRANSCRIPTION` absent/false — re-verify, don't cite the 08-13 check.
2. **IAM, simulated not read:** `aws iam simulate-principal-policy` against the deployed
   transcribe role — `s3:GetObject` on
   `arn:...:{bucket}/audio_segments/*/_*_batch_map.json`-shaped keys and on the batch WAV
   key shape; against the sweep role — `s3:GetObject` on `users/*/audio/*` (the trim reads;
   this is the grant whose absence caused all three silent breakages — it gets re-proven,
   not remembered). Remember the ListBucket lesson: a 403 on a key that exists is
   indistinguishable from missing unless simulated.
3. **One real session with a VAD-dropped interior chunk** (near-guaranteed by §2's 37 %;
   confirm via the `_vad_metadata.json` sidecars): one bridging `_bn` WAV whose map has a
   `"seam": "gap"` member, zero `_bn1` objects, zero member transcripts for consumed
   members, transcript JSON carries `fieldsight_batch_map`, and the viewer/ask/minutes
   render the post-gap turns at the post-gap wall-clock time (compare against the member
   chunk filenames by hand, one turn is enough — but the RIGHT turn: one after the gap).
4. **Positive evidence for every guard:** Logs Insights counts over the day —
   `batch: sealed` == maps written == transcripts with embedded maps;
   `batch_splice_turns` lines == batched transcripts; zero "keeping filename arithmetic"
   warnings for post-change artifacts. A guard line with no matching success line anywhere
   is Phase 5b's raise waiting to be found — find it here, not in a customer email.

---

## Standing constraints

- `tests/unit/test_vad_tuning_rationale.py` stays green and unedited; if any step here
  seems to need a VAD/normalisation/`DROP_SILENT_CHUNKS` change, stop — those are measured
  dead ends.
- Windows worktree: single-line Edit anchors; never `git add -A`; never a whole-file
  rewrite of any `src/` module (this feature has already lost a shipped fix that way).
- Deleting the old planner tests in Phase 4 happens by named test, in the same commit as
  the code they pinned, with the new pins already green — a deleted guard and its fix must
  never share a commit with an unrelated rewrite.
- No phase touches prod configuration; Phase 7's prod check is read-only.
