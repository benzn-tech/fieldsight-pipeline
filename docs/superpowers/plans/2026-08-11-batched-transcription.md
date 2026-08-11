# Implementation plan — batched transcription (2 min per request, not 30 s)

**Date:** 2026-08-11 · **Spec:** `docs/superpowers/specs/2026-08-11-batched-transcription.md`
(read its "Review corrections" section — two of its factual claims were corrected in review)
**Branch:** `feat/batched-transcription` off `origin/develop`.
**Discipline:** strict TDD — every phase writes its tests FIRST and watches them fail before
any implementation. Unit tests use monkeypatched boto3 clients / fake table objects, never
real AWS, never real Postgres (this feature touches no Aurora table; if any phase grows a DB
touch, use the FakeConn/FakeCursor double from `tests/unit/test_action_items_repo.py`).

Run tests exactly as the repo does:

```
export UV_LINK_MODE=copy
export AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_DEFAULT_REGION=ap-southeast-2
uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit -q
```

---

## 0. Decision record (settled during adversarial review — do not re-litigate without new data)

### Where batching lives: **inside `lambda_transcribe`**, with a seal hook in `lambda_finalize_claim`.

Not a new Lambda, and not `lambda_vad`. The trigger topology decides it:

- The data bucket is managed **outside the stack**; S3 notifications are configured **by
  hand** (`src/template.yaml`, TranscribeFunction NOTE: "S3 event trigger must be configured
  manually"). A new Lambda means manual prod bucket surgery + deploy-role IAM
  (`github-actions-fieldsight-deploy`, the CREATE_FAILED-rollback trap) + a new concurrency
  consumer. All three are known bite-points for zero architectural gain.
- `lambda_transcribe` already receives **exactly one S3 event per chunk unit**. The arrival
  of the chunk that completes a batch IS the natural batch trigger — no polling, no timer
  for the common case.
- `lambda_vad` must stay per-chunk (spec requirement: online silence gating), it cannot see
  later chunks, it is the 3 GB / py3.12-layer function, and it also serves legacy whole
  files and video. Wrong home.

### The batch artifact is a normal `audio_segments/` WAV

The batcher writes the concatenated audio back to
`audio_segments/{user}/{date}/{batch_base}.wav`. The **existing, manually-wired S3 trigger
then transcribes it through the unchanged per-object path** — which makes batching
provider-agnostic for free (AWS Transcribe and ElevenLabs both just see one object), keeps
the ledger write, and keeps `_segment_key_for()` (evidence → audio resolution:
`transcripts/{base}.json → audio_segments/{base}.wav`) working with zero change.

The cost of this choice is one new silent-failure edge that MUST be pinned by tests: if
`lambda_transcribe` fails to recognise a batch object as "transcribe me" (vs a member chunk
as "register me"), two minutes of audio are silently never transcribed and the session just
looks quieter. Phase 3's tests exist for exactly this, in both directions.

### Batches contain only consecutive, same-date chunks — gaps are never inside a batch

The spec's "a missing index becomes an explicit gap in the map" is **cut** as
over-engineering that creates a silent failure: every consumer except `extract_session`
(report generator, meeting minutes, transcript viewer, ingest) resolves time by **filename
arithmetic** and will never read the map, so a gap spliced into a batch shifts their
timestamps by ~28 s per missing chunk with nothing looking broken. Instead:

- a batch covers a run of **consecutive chunk indices** with **identical date tokens**;
- a missing / not-yet-arrived / VAD-dropped index **seals the batch early** at the gap;
- the late or solo chunk becomes its own (possibly 1-chunk) batch when it arrives;
- sealed batches are never reopened (as the spec already says).

With that rule, filename arithmetic stays *approximately* right for every legacy consumer
(error = device cadence jitter minus trim error, sub-second in practice), and the map is a
**correction for the extract path**, not the sole line of defence. The timeline cannot drift
by a chunk-length because a gap simply ends the batch.

### The trim is measured on the RAW uploads, applied to the normalised units

This is the review's most important correction. The spec's per-seam byte-identical
comparison is specified against the batcher's inputs (`audio_segments/` units), where it
**cannot work**:

- `NORMALISE_AUDIO` (on in test today; a repo-variable flip away on prod) applies
  `acompressor + loudnorm` **per chunk**. loudnorm's gain is time-varying and depends on the
  whole chunk, so the same source bytes at the tail of chunk N and the head of chunk N+1
  produce **different output bytes**. Byte-identity measures 0 at every seam.
- Even unnormalised, `extract_audio_ffmpeg` re-encodes; for any non-16k/mono source the
  resampler's warm-up state differs at a file head, so byte identity is not guaranteed.
- Combined with the spec's own (correct!) rule "measured zero → trim nothing", the result is
  that **turning normalisation on silently disables all trimming**: no error, overlap
  transcribed twice, the 6.7 % saving and the stutter fix evaporate. A textbook example of
  the silent-failure class the spec set out to hunt.

Therefore: the byte-identical run is measured on the **raw device uploads**
(`users/{user}/audio/{date}/{chunk}.…` — untouched by design, "it is the evidence"), where
the device's PcmRingBuffer copy IS byte-identical. The measured duration (seconds, from the
raw WAV's own sample rate) is converted to a **sample count at 16 kHz** and that many
samples are cut from the head of the normalised unit. This is sound because
`normalise_for_asr` **refuses** any normalised output whose sample count differs from the
extraction (`lambda_vad.py` — "VAD offsets index the original array"), so raw-derived
sample offsets align exactly with the `audio_segments/` units. Everything else the spec
says about the trim stands: 2.0 s ceiling, log the measured value per seam, zero → trim
nothing and keep the audio.

### Every chunk's head is trimmed against its predecessor — including the first chunk of a batch

The spec counts "three seams to zero" but the seam between batch N and batch N+1 still
exists, and its overlap would be transcribed twice (and its ~6.7 % figure only holds if all
four overlaps per two minutes are trimmed). So the trim rule is uniform: **the head of
every chunk except the session's chunk 0 is trimmed against its predecessor chunk,
regardless of batch membership**. The batch's `_off` token records the first chunk's head
trim, so `compute_segment_base_time` (filename arithmetic) lands on the true first sample.
`_dedup_turn_boundaries` stays in place untouched as the safety net for anything that slips
through (mixed-mode sessions, trim measured 0).

### Whole-chunk mode is a precondition, enforced at runtime

Batching concatenates *chunks*; if `TRANSCRIBE_WHOLE_CHUNK=false` (legacy per-VAD-segment
units) the inputs are speech islands with fictional seams. The transcribe function gets the
`TRANSCRIBE_WHOLE_CHUNK` env too and, when batching is on but whole-chunk is off, logs
loudly and **passes through per-chunk** (never guesses). Note the sidecar
`_vad_metadata.json` cannot be used for this decision at arrival time — VAD writes segments
BEFORE metadata, so the .wav event can outrun the sidecar (race).

---

## 1. Artifact contracts (fixed before any code)

### Batch WAV key

```
audio_segments/{user}/{date}/{device}_{date}_{HH-MM-SS}_sid{32hex}_c{NNNN}_bn{K}_off{T}_to{E}_srcwav.wav
```

- `{HH-MM-SS}`, `_c{NNNN}`: the **first member chunk's** timestamp and index.
- `_bn{K}`: member count (K=1..4). This token is the batch marker `lambda_transcribe` keys
  on, and it is invisible to every existing parser (verified: `_SESSION_ID_RE` matches —
  `sid` is followed by `_`; `_CHUNK_INDEX_RE` and `chunk_stitch.CHUNK_TOKENS_RE` match —
  `c{NNNN}` is followed by `_`; `session_base_from_key` → `sid{...}` so
  `gather_session_segments` finds the batch transcript; `extract_base_time_from_filename`
  reads the first chunk's clock).
- `_off{T}`: the first chunk's measured head-trim in seconds (0.0 for session chunk 0), so
  filename arithmetic starts at the true first sample. `_to{E}` = `T` + batch audio
  duration.

### Batch map sidecar (authoritative time map)

`audio_segments/{user}/{date}/{batch_base}_batch_map.json` (`.json` → skipped by
`get_media_format`, so it never triggers transcription):

```json
{
  "schema": 1,
  "session_id": "…32hex…",
  "members": [
    {"chunk_index": 4, "chunk_key": "users/…/….wav",
     "abs_start": "2026-08-11T14:18:47",          // chunk filename clock
     "batch_offset_sec": 0.0,                      // where this chunk's KEPT audio starts in the batch
     "trimmed_head_sec": 1.5,                      // measured, 0.0 if none / chunk 0
     "trim_measured": true,                        // false = measurement returned 0 unexpectedly
     "kept_duration_sec": 28.5}
  ],
  "sealed_by": "arrival | idle_sweep | session_close | gap | date_boundary",
  "created_at": "…"
}
```

Resolution rule (the ONLY one, spec-mandated: never re-derive): find the member with the
largest `batch_offset_sec <= t`, absolute = `abs_start + trimmed_head_sec +
(t − batch_offset_sec)`.

### Batch ledger state (DynamoDB, existing `TRANSCRIPT_TABLE` — transcribe already holds `DynamoDBCrudPolicy`)

- Member registration: `PK=BATCH#{session_id}`, `SK=CHUNK#{index:04d}` — conditional put
  (`attribute_not_exists(SK)`) so duplicate S3 deliveries are no-ops.
- Seal record: `SK=SEAL#{first:04d}` with `status` — conditional put wins the race between
  arrival-sealing and sweep-sealing; the loser walks away. Write the batch WAV + map
  **before** the seal record flips to `sealed` is wrong (double-emit on crash between);
  order is: conditional-claim `sealing` → write map → write WAV (the WAV event is what
  triggers transcription, so it goes last) → update `sealed`. A crash mid-way leaves a
  `sealing` claim the sweep re-drives after a timeout.

No TTL assumption on the table; rows are tiny and PK-scoped. (If cleanup is wanted later it
is an independent change.)

---

## 2. Phases

Phases 1–5 are **inert on prod AND test** (new code behind `BATCH_TRANSCRIPTION`, default
`'false'` in code, template, and both workflows). Phase 6 changes **test** behaviour.
Phase 7 (prod) is a separate decision, not part of this plan's default path.

---

### Phase 1 — pure batching module `src/batch_stitch.py` [INERT]

New file, pure Python (no boto3/env at import — same rule as `chunk_stitch.py`), bundled
automatically (CodeUri is `src/`).

**Tests first** — `tests/unit/test_batch_stitch.py`:

1. `measure_overlap(raw_tail_bytes, raw_head_bytes, rate, ceiling_sec=2.0)`
   - synthetic PCM pair with a known 2.0 s identical tail → returns 2.0 s worth of samples;
   - a 1.5 s pair → 1.5 s; a 0-overlap pair → 0; overlap longer than ceiling → ceiling;
   - odd byte counts / different rates handled (rate from each WAV header, compare at the
     raw rate, return seconds).
2. `plan_batches(present_indices, max_size=4)` → runs of consecutive indices, split at
   gaps, capped at 4 (e.g. `{4,5,7,8,9,10,11}` → `[4,5], [7,8,9,10], [11]`).
3. `build_batch_name(first_member_stem, count, head_trim, duration)` → the key contract in
   §1, and **round-trips through the real parsers**: assert
   `transcript_utils.extract_session_id_from_filename`,
   `extract_chunk_index_from_filename`, `extract_base_time_from_filename`,
   `compute_segment_base_time` (== chunk start + head trim), and
   `chunk_stitch.parse_chunk_key` all read it correctly, and that
   `lambda_extract_session.session_base_from_key` groups it with its session. This test is
   the guard against finding 4 above breaking silently.
4. `is_batch_key(key)` — `_bn{K}` detection; false for every member/legacy/fallback shape
   in the fixtures (include a device named `x_bn2_y` style adversarial stem).
5. `build_map(members)` / `resolve_abs_time(map, t)`:
   - four members, known trims → every resolved time equals the per-chunk path's
     `chunk_start + t_in_chunk` within one sample period (spec verification №1);
   - unequal chunk lengths; a 1-member batch; `t` exactly on a boundary; `t` beyond the
     last member (clamps to last member, never extrapolates into a nonexistent chunk).
6. Zero-measured seam: map records `trim_measured: false`, `trimmed_head_sec: 0.0`, and
   the concatenation keeps all audio.
7. WAV concatenation helper: header-parsed 16 kHz mono 16-bit in, single well-formed WAV
   out, byte-exact sample accounting (`kept_duration_sec` sums to output length).

**Then implement.** Rollback: delete the file — nothing imports it yet.

---

### Phase 2 — batch ledger state machine `src/batch_ledger.py` [INERT]

Pure functions over an injected table object (fake in tests — mirror the existing
`test_device_heartbeat.py` fake-table style; no real DynamoDB).

**Tests first** — `tests/unit/test_batch_ledger.py`:

1. `register_chunk(table, session_id, index, key)` — conditional put; second call with the
   same index returns `already_present` and writes nothing (duplicate S3 delivery).
2. `claim_seal(table, session_id, first_index, members)` — conditional; a second claimant
   loses cleanly (returns None). A stale `sealing` claim older than `SEAL_RETRY_SECONDS`
   can be re-claimed (crash re-drive).
3. `pending_runs(rows, now, deadline_sec)` — which runs are complete (4 consecutive) and
   which are past deadline (seal short). Chunk 0 present alone and young → nothing sealed.
4. A VAD-dropped index never arrives: run `{0,1,2}` with 3 dropped → sealed as `[0,1,2]` at
   deadline, and `{4,5}` later forms its own batch — the dropped index behaves exactly like
   a gap (per §0; the map never contains it).

**Then implement.** Rollback: delete the file.

---

### Phase 3 — `lambda_transcribe` integration, behind the flag [INERT]

Touches `src/lambda_transcribe.py` only. New module-level envs:
`BATCH_TRANSCRIPTION` (default `'false'`), `BATCH_MAX_CHUNKS` (default `'4'`),
`BATCH_SEAL_DEADLINE_SEC` (default `'150'`), plus reading `TRANSCRIBE_WHOLE_CHUNK`.

Behaviour when `BATCH_TRANSCRIPTION=true` and the key carries `sid`+`c` tokens and is NOT a
batch object: register the member (Phase 2), check whether it completes a run of
`BATCH_MAX_CHUNKS` consecutive members **or** pushes any older run past the deadline; if
so, seal: fetch members' raw uploads for trim measurement (raw key derived from the unit
key's stem — `users/{user}/{audio}/{date}/{stem}.{ext}`; if the raw object is missing or
unreadable, trim nothing and log — never block the batch), fetch the member units, trim,
concatenate, write map then batch WAV. **Do not transcribe the member.**
Batch objects (`is_batch_key`) and every other key (legacy, no-sid, `.json`) flow through
the existing path unchanged. `BATCH_TRANSCRIPTION=false` → the function is byte-for-byte
today's behaviour.

**Tests first** — `tests/unit/test_lambda_transcribe_batching.py` (monkeypatch `s3`,
`transcribe`, `elevenlabs_utils`, and the ledger module on the imported handler module,
exactly like `test_lambda_transcribe_elevenlabs.py` does):

1. Flag off → a chunk-unit event transcribes exactly as today (regression pin: assert the
   provider call happened and no ledger/batch writes).
2. Flag on, member arrives, run incomplete → registered, **no transcription call, no batch
   WAV** — and the result entry says `batched_pending` so the log is auditable.
3. Flag on, 4th consecutive member arrives → batch WAV + map written to the right keys;
   member units were fetched; the batch WAV is NOT transcribed in this invocation (the S3
   event will do it).
4. A batch-WAV event arrives → transcribed through the normal path (both providers —
   parametrize `ASR_PROVIDER`), ledger record written for the Transcribe path, output
   transcript key is `transcripts/{user}/{date}/{batch_base}.json`.
5. **The mis-detection pins (silent-failure guard, both directions):** a batch key must
   never be registered as a member; a member key must never be transcribed while the flag
   is on and whole-chunk is on. Assert on the fakes' call lists, not on log text.
6. Legacy key (no sid) with flag on → transcribed normally (batching never applies).
7. `TRANSCRIBE_WHOLE_CHUNK=false` + flag on → loud log + per-chunk passthrough (the
   runtime guard from §0).
8. Duplicate S3 delivery of the same member → second invocation is a no-op
   (conditional-write behaviour surfaced through the fake ledger).
9. Trim measurement returned 0 for a seam → batch still emitted, nothing trimmed at that
   seam, map says `trim_measured: false`.
10. EL failure mid-seal (RuntimeError) → seal claim is left re-drivable; no half-batch
    transcript exists (the WAV write is last).

**Then implement.** Rollback: `BATCH_TRANSCRIPTION` off (env default) — and because the
default is `'false'` in all three segments, merging this phase changes nothing anywhere.

---

### Phase 4 — tail sealing in `lambda_finalize_claim` [INERT]

The last 1–3 chunks of a session have no 4th arrival and possibly no further arrivals at
all. The sweep (`FinalizeSweepFunction`, rate(1 min), already infers idle close) gains a
flag-gated step: for a session it is about to close (and on each sweep pass for sessions
with pending runs past `BATCH_SEAL_DEADLINE_SEC`), seal pending runs **before** the final
extraction request is written. Sealing here = the same Phase 1/2 code writing map + WAV;
the S3 event transcribes it; the existing grew-rerun / final-pass coverage recheck
(`_rerun_if_the_session_grew`, `test_final_pass_coverage_recheck`) is the net for the
transcription completing after the final request — this ordering is what prevents the
review's finding 3 (final extraction silently missing the last ≤2 minutes).

**Tests first** — `tests/unit/test_finalize_batch_seal.py`:

1. Flag off → sweep behaviour byte-for-byte unchanged (pin against today's calls).
2. Flag on, session closing with a pending 2-chunk run → seal happens BEFORE the final
   request object is written (assert call order on the fakes).
3. Flag on, no pending runs → no batch writes.
4. Seal race: transcribe sealed it between sweep's read and claim → sweep's claim loses
   and it does not double-write (fake ledger returns claim-lost).

`FinalizeSweepFunction` needs the batching envs + S3 read on `users/*` raw audio for trim
measurement (it deploys from the same `src/`, so the modules are already in its bundle).
Check its template Policies cover `users/*` read — if a policy statement must be added,
verify with `simulate-principal-policy` after deploy, per the repo's IAM trap rule.

**Then implement.** Rollback: same flag.

---

### Phase 5 — map consumption in `lambda_extract_session` [INERT]

In `assemble_session_turns`, after `normalize_transcript` of a transcript whose filename
`is_batch_key(...)`, fetch `{batch_base}_batch_map.json` and re-base each turn's
`abs_start`/`abs_end` via `batch_stitch.resolve_abs_time` (turn-level; `start_sec`/
`end_sec` stay batch-relative — they are the in-file audio offsets the evidence/playback
path needs, and `source_filename` already points at the batch WAV). Map missing →
warn loudly and keep filename arithmetic (bounded error by §0's no-gaps rule; never drop
the transcript).

**Tests first** — extend `tests/unit/test_lambda_extract_session.py` (or a new
`test_extract_batch_map.py`):

1. A batch transcript + map with known trims → every turn's `abs_start` equals what the
   per-chunk path would produce for the same words (build both fixtures from the same
   synthetic timeline; within one sample period — spec verification №1).
2. Map absent → filename-arithmetic times, warning logged, extraction proceeds.
3. **Mixed-mode session** (flag flipped mid-session): chunks 0–3 per-chunk transcripts,
   4–7 one batch transcript → `gather_session_segments` finds all of them, ordering is
   correct, `_dedup_turn_boundaries` removes any residual seam duplicate (this is the
   rollback-safety test: turning the flag OFF mid-stream must degrade to today's
   behaviour, not corrupt a session).
4. Evidence resolution: `_segment_key_for` on the batch transcript filename yields the
   batch WAV key (already true by construction; pin it anyway).
5. Group path: two member sessions, one batched one not → `assemble_group_turns` output
   shape unchanged (batching is per-session; the group merge must not care).

**Then implement.** Rollback: consumption is keyed on `is_batch_key`; with the flag off no
batch artifacts are produced and this code is dead.

---

### Phase 6 — the switch, wired in all three segments, then test activation [wiring INERT · activation changes TEST]

**6a. Wiring (inert).** One commit, three files + one test:

1. `src/template.yaml` — Parameter:
   ```yaml
   BatchTranscription:
     Type: String
     Default: 'false'
     AllowedValues: ['true', 'false']
     Description: >-
       Whether lambda_transcribe accumulates up to BatchMaxChunks consecutive chunks of
       one session into a single ASR request (spec 2026-08-11). Default false: deploys
       inert. Requires TranscribeWholeChunk=true; the function refuses to batch
       per-VAD-segment units. Declared here so rollback survives a deploy.
   ```
   Env wiring: `BATCH_TRANSCRIPTION: !Ref BatchTranscription` AND
   `TRANSCRIBE_WHOLE_CHUNK: !Ref TranscribeWholeChunk` on **TranscribeFunction**, and
   `BATCH_TRANSCRIPTION` on **FinalizeSweepFunction**. (`BatchMaxChunks` /
   `BatchSealDeadlineSec` stay code defaults for now — adding them as Parameters later is
   the evidence-tunables pattern.)
2. `.github/workflows/deploy.yml`:
   `"BatchTranscription=${{ vars.TEST_BATCH_TRANSCRIPTION || 'false' }}" \`
3. `.github/workflows/deploy-prod.yml`:
   `"BatchTranscription=${{ vars.PROD_BATCH_TRANSCRIPTION || 'false' }}" \`
4. **Pinning tests FIRST** (they go red before the wiring lands, by design):
   - The existing sweep `test_every_boolean_toggle_is_reachable_from_a_repo_variable` in
     `tests/unit/test_template_workflow_parameter_wiring.py` picks the new boolean up
     automatically — adding the Parameter without both workflow lines turns CI red with no
     new code. Verify this by adding the Parameter first and watching that test fail.
   - Add an explicit named pin (same file), mirroring
     `test_the_whole_chunk_mode_is_wired_in_both_environments`:
     `test_the_batch_switch_is_wired_in_both_environments` — both workflows pass
     `BatchTranscription`, and **every function that reads `BATCH_TRANSCRIPTION` is given
     it** (`!Ref` present in both `TranscribeFunction` and `FinalizeSweepFunction` blocks —
     the "middle segment" check, via `_function_block`).
   - Pin the precondition: `TranscribeFunction` receives `TRANSCRIBE_WHOLE_CHUNK`.

**6b. Test activation (changes TEST behaviour — do this alone, watch it).**
Set repo variable `TEST_BATCH_TRANSCRIPTION=true`, push/redeploy `develop`, then verify in
this order (spec's verification list, made executable):

1. **Read the deployed function's env** — `aws lambda get-function-configuration
   --function-name fieldsight-test-transcribe` shows `BATCH_TRANSCRIPTION=true` (the
   unwired-toggle check; repo variables existing is not evidence).
2. Record one real multi-chunk session on a test device. Confirm in S3:
   `_bn{K}` WAVs + `_batch_map.json` sidecars; member chunk units present but with **no**
   member transcripts; batch transcripts under `transcripts/`.
3. **Same audio, both paths** (spec verification №3): re-run the same session's member
   units through the per-chunk path (copy them to a scratch prefix or flip the flag and
   re-drive), compare word counts and first/last-word absolute times per turn. More words
   at seams = trim not happening (check the seam logs — expect this if the raw-upload
   fetch failed); fewer words = **stop-ship**, flip the flag off.
4. CloudWatch: the per-seam measured-trim log line distribution settles 1.5 vs 2.0 s (the
   spec wants this as a side effect; it is also the health signal for finding 1 — a wall
   of zeros means the raw-byte comparison is broken for this device's upload format).
5. Latency/cost on the real path (spec verification №5) and: the finalize email for a
   batched session contains the session's **final minutes** (finding 3's end-to-end check).
6. Tier-1 live summary cadence: confirm the ~2 min update rhythm is acceptable; if not,
   `BATCH_SEAL_DEADLINE_SEC` is the knob (smaller = earlier partial seals).

**Rollback for 6b:** set `TEST_BATCH_TRANSCRIPTION=false` (or delete the variable) and
redeploy. In-flight pending members simply get transcribed per-chunk on their next event /
sweep pass is not needed — already-registered-but-unsealed members were never transcribed,
so ALSO re-drive them: the sweep's flag-off path must be checked to leave nothing stranded.
Phase 5 test 3 (mixed-mode) is the guard that sessions straddling the flip stay coherent.

---

### Phase 7 — prod [NOT in this plan's default path]

A separate decision with its own session: set `PROD_BATCH_TRANSCRIPTION=true`, deploy via
the approval-gated `deploy-prod.yml`, repeat 6b's verification (env read first) on a prod
device. Rollback identical: variable off, redeploy, mixed-mode session degrades safely.
Until then every phase above leaves prod byte-for-byte unchanged (defaults `'false'` in
code, template, and workflow fallback).

---

## 3. Standing constraints (do not violate while implementing)

- **Never touch VAD defaults or ordering** — `tests/unit/test_vad_tuning_rationale.py`
  pins `MIN_SPEECH_DURATION=1.0`, `MERGE_GAP=2.0`, the derived retry threshold
  (`VAD_THRESHOLD / 2`, `lambda_vad.py:955`), normalise-after-gate ordering, and
  `DROP_SILENT_CHUNKS=true`. The 2026-08-10 measurement spec rules out threshold retuning,
  pre-gate enhancement, `DROP_SILENT_CHUNKS=false`, mic switching, and a mic array. This
  plan needs none of them; if an implementation step seems to, stop.
- The VAD retry interacts with batching only as "the chunk unit exists or it does not" —
  batching consumes VAD's output and must not model its thresholds.
- Windows worktree: single-line Edit anchors, never `git add -A`.
- Do not assume 30 s chunks anywhere (`segmentSeconds` is a device setting); cap total
  batch duration at 6 min (`4 × 90 s` headroom) so a large-chunk device cannot push a
  batch past ElevenLabs' 8-min internal parallel-split boundary, whose diarization
  behaviour across splits is unmeasured.
- New IAM statements → `simulate-principal-policy` after deploy, never trust the template
  read; a 403 on a missing raw upload must log-and-continue, not silently skip the trim
  forever (the S3 403-vs-404 lesson: verify the batcher's raw-audio read permission
  explicitly on both stacks during 6b).
