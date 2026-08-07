# Session Continuity: chunk→session assembly + whole-session diarization

**Date:** 2026-07-23
**Branch:** `feat/session-continuity` (based on `origin/develop`)
**Status:** Design — awaiting review
**Depends on:** the ElevenLabs ASR path from PR #116 (`feat/alt-llm-asr-qwen-scribe`)

## 1. Goal

Give every recording **session** a single diarization pass so that speaker labels
(`speaker_0`, `speaker_1`, …) are **consistent across the whole session**, and every
word carries a **correct absolute wall-clock time**. This is the foundation that a
later spec (Spec 2: speaker naming) builds on — you cannot map `speaker_N → real
name` reliably until `speaker_N` means the same person for the whole session.

This spec delivers **anonymous but session-consistent** speaker labels + correct
time. **Naming is explicitly out of scope here.**

## 2. Background / the problem

Two facts about the current pipeline combine into the problem:

1. **Media is uploaded in ~30-second chunks** (for timeliness — raw media must land
   in S3 quickly, not wait for a session to finish). A real activity (a 2-hour
   inspection, or a meeting) spans **many** chunk files.
2. The current pipeline runs **VAD per chunk → one transcription call per VAD
   segment**. Speaker labels are only consistent **within a single transcription
   call**, so `speaker_0` in one segment has no relation to `speaker_0` in another.

Net effect: across a session, the same person is labelled inconsistently, so
per-turn attribution across a multi-person inspection or meeting is impossible.
Downstream today sidesteps this by assuming *filename device = the one speaker* —
true for single-person PTT clips, false for multi-person recordings.

**The core tension** (already resolved with the user): whole-session diarization
needs the *whole session*, which conflicts with per-chunk timeliness. Resolution:
raw media keeps landing immediately (timeliness preserved); the **attributed
transcript is produced shortly after the session ends** (session-close + a short
inactivity window, target ~15 min). Near-real-time/live attribution is **not**
required.

## 3. Scope

**In scope:**
- **Session assembly** — group the stream of chunk files into sessions and detect
  when a session has ended.
- **Session-level VAD-stitch** — VAD each chunk, concatenate the speech across all
  of a session's chunks into **one** silence-stripped file, plus a
  concat-time↔real-time map.
- **Whole-session diarization** — one ElevenLabs call per session (reusing the
  PR #116 ElevenLabs path), producing session-consistent `speaker_N`.
- **Timestamp restoration** — rewrite every word's time back to **real wall-clock**
  before the transcript is written, preserving absolute-time correctness.
- A **feature toggle** so the new path ships default-off and rolls out test-first.

**Out of scope (later specs / not now):**
- **Speaker naming** (`speaker_N → real name` via self-intro / device anchor /
  content) — **Spec 2**.
- **Voiceprint enrollment / cross-session recognition** — separate future project.
- **Automatic meeting-vs-inspection classification** — deliberately dropped
  (unreliable and circular; the always-on approach makes it unnecessary).

## 4. Guiding principles

- **Timeliness of raw media is untouched.** Chunks still land in S3 immediately.
  Only the transcript is deferred to session close.
- **Absolute-time correctness is the headline invariant.** The project's most
  painful historical bugs were timestamp bugs (CLAUDE.md BUG-09, BUG-11). The
  stitch/restore step must produce word times identical to what the old path
  would compute — this is the #1 acceptance criterion.
- **Default-safe, toggled, test-first.** A `SESSION_CONTINUITY` toggle defaults to
  the current per-segment path; the new path is validated on the test stack before
  any prod cutover; rollback is one env-var flip. Mirrors the provider-toggle
  pattern from PR #116.
- **Downstream stays stable.** The output stays AWS-Transcribe-shaped JSON that
  `transcript_utils` already consumes; the change is "one transcript per session"
  instead of "one per segment," with times already absolute.

## 5. Architecture / data flow

```
Device uploads ~3-min chunks continuously
  → S3 users/{user}/audio|video/{date}/{device}_{date}_{time}.ext   (immediate; unchanged)
       │  S3 ObjectCreated event
       ▼
  [1] Session Assembly  (new)
       - append chunk to the session record for (user, device) in a DynamoDB session table
       - a chunk belongs to the current open session if it starts within
         SESSION_GAP_MINUTES of the previous chunk's end; otherwise it opens a new session
       ▼
  [2] Session Close Sweeper  (new, scheduled)
       - periodically scan open sessions; a session whose last chunk ended more than
         SESSION_GAP_MINUTES ago (or that exceeds SESSION_MAX_MINUTES) is marked CLOSED
       - a CLOSED session triggers assembly (async invoke of [3])
       ▼
  [3] Session VAD-Stitch  (VAD lambda, extended)
       - for each chunk in the session (time-ordered): VAD → speech regions
       - concatenate all speech regions across all chunks into ONE stitched WAV
       - emit a stitch map: ordered [(concat_start, concat_end, real_walltime_start)]
       - write stitched WAV + map to a session prefix
       ▼
  [4] Whole-session diarize  (ElevenLabs path from PR #116, triggered on the stitched file)
       - one transcribe_segment() call over the stitched session file, diarize=true
       - adapt_to_transcribe_json → AWS-Transcribe-shaped JSON with session-consistent speaker_N
       ▼
  [5] Timestamp Restore
       - for each word: map its concat-time back to real wall-clock via the stitch map;
         rewrite start_time/end_time as seconds-from-session-start (real, silence-inclusive)
       - write the session transcript to transcripts/{user}/{date}/{session_base}.json
       ▼
  Downstream (report-generator / meeting-minutes / extract-session / ingest): unchanged consumers
```

When `SESSION_CONTINUITY=off`, steps [1][2][3-as-session][5] are bypassed and the
pipeline behaves exactly as PR #116 (per-chunk VAD segments → per-segment
transcribe). The two paths are mutually exclusive per stack.

## 6. Components

### 6.1 Session table (DynamoDB)

A new table (or a partition of the existing ledger) tracks sessions.

- **PK:** `SESSION#{user}#{device}` ; **SK:** `{session_start_iso}`
- Attributes: `session_id`, `user`, `device`, `date`, `chunk_keys` (ordered list),
  `first_ts`, `last_ts` (wall-clock of last chunk's end), `status`
  (`open|closed|stitching|transcribing|done|failed`), `stitched_key`,
  `transcript_key`, `updated_at`.
- Idempotency: appending the same chunk key twice is a no-op; state transitions are
  guarded (only `open→closed`, `closed→stitching`, …).

### 6.2 Session assignment rule

On each chunk `ObjectCreated`:
- Parse `{device}_{date}_{time}` from the filename → chunk start wall-clock; chunk
  end = start + chunk duration **taken from the decoded media**. The nominal value is
  30 s (OI-1, measured), but 6 of 260 regions in the measured session were not 30 s, so
  the nominal is a last resort and never the basis for the overlap arithmetic (§6.4a).
- Find the open session for `(user, device)`. If `chunk.start − session.last_ts ≤
  SESSION_GAP_MINUTES`, append; else close the old session (if any) and open a new
  one starting at this chunk.

This directly handles the user's scenario: inspection (2 h of contiguous chunks) →
device off → 1-hour gap → meeting. The 1-hour gap ≫ `SESSION_GAP_MINUTES`, so the
meeting opens a **separate** session; the two are never stitched together.

### 6.3 Session close sweeper (scheduled)

A scheduled lambda (aligned with the existing ~15-min orchestrator sweep cadence)
scans `status=open` sessions and closes any whose `last_ts` is older than
`SESSION_GAP_MINUTES`, or whose span exceeds `SESSION_MAX_MINUTES`
(force-close safety for a device left running). Closing sets `status=closed` and
async-invokes the VAD-stitch step. Using a sweeper (not a per-chunk timer) matches
existing infra and avoids per-chunk scheduling machinery.

### 6.4 Session VAD-stitch (extends the VAD lambda)

- Input: a closed session's ordered chunk keys.
- For each chunk: download → extract 16 kHz mono (existing ffmpeg path) → Silero VAD
  (existing, threshold 0.4 → 0.25 → full-audio fallback per BUG-07) → speech regions
  as `(offset_start, offset_end)` within the chunk.
- **Stitch:** concatenate the speech-region PCM across all chunks, in session time
  order, into one WAV. Use numpy buffer ops, never per-sample Python loops
  (CLAUDE.md BUG-04/BUG-06), and `len(arr)==0` not truthiness (BUG-05).
- **Stitch map:** as regions are appended, record
  `(concat_start_sec, concat_end_sec, real_start_sec)` where `real_start_sec` is the
  region's seconds-from-session-start = `(chunk.start_walltime − session.first_ts) +
  region.offset_start`. The map is the sole bridge from stitched-time back to real
  time.
- Output: `sessions/{user}/{date}/{session_base}.wav` + `..._stitchmap.json`.

### 6.4a Remove the device's chunk overlap BEFORE stitching

> **Addendum 2026-08-07.** This section did not exist in the original spec, which was
> written on 2026-07-23 — before the mobile chunk-session contract shipped. §6.4 as
> written concatenates every chunk's speech regions, and those regions now contain
> **deliberately duplicated audio**. Stitching them unchanged records each seam
> sentence twice.

The device carries the tail of chunk N into the head of chunk N+1
(`AudioSegmentation.PcmRingBuffer` — "a sentence crossing a boundary appears whole in
both"), so a boundary sentence is never cut in half. The overlap length is
**deliberately not on the wire**, so the backend cannot read it; `chunk_stitch.py`
recovers it at the **text** level by finding the longest repeated word-run at a seam.

**Measured on production data** (`Sam_Yu / 2026-08-03 / sid622a0e7f…`, 260 chunks):

| quantity | value | frequency |
|---|---|---|
| VAD region duration (`to − off`) | 30.0 s | 254 / 260 |
| consecutive chunk start gap | 28.0 s | 249 / 259 |
| **implied audio overlap** | **2.0 s** | |

Note this also settles OI-1: chunk duration is **30 s, not the "nominal 3 min"** the
original assumed, and chunk filenames do carry a reliable per-chunk start wall-clock.

If the overlap is not removed from the audio, three things follow, and the third is
the one that breaks the spec's own headline invariant:

1. The session transcript repeats every seam sentence. `chunk_stitch` will not clean
   this up — it runs on the per-chunk path, not on a whole-session transcript.
2. Diarization hears the same two seconds twice. Identity usually survives that; turn
   boundaries do not.
3. The restored timeline **loses monotonicity**: two stitched positions carry the same
   real time. Note precisely what is and is not broken — the duplicated words get the
   *correct* time, twice. The times are right; the content is doubled. (An earlier
   draft of this addendum said the stitch map "stops being a function". It does not —
   concat→real is still single-valued. The damage is duplication and non-monotonic
   restored output, not wrong timestamps.)

**Removal, at each seam, in this order:**

1. **Estimate** from the filenames, using **wall-clock endpoints, not durations**:

   ```
   overlap = (start_N + to_N) − (start_{N+1} + off_{N+1})
   ```

   where `start` is the chunk's own start from the filename and `off`/`to` bound the
   VAD region inside it. The duration-based form (`prev_duration − chunk_gap`) is
   correct **only when both regions start at offset 0**. Whenever VAD trims a chunk's
   head, that form yields a spurious negative and declares a real overlap
   "not contiguous". It happened to work on the measured session because 254 of 260
   regions were full-chunk — which is exactly the kind of accident that ships.

   Clamp to `[0, MAX_OVERLAP_S]`. **`MAX_OVERLAP_S = 10.0`**, matching the device
   contract that `chunk_stitch.py:27-30` already documents ("capped at 10s upstream").
   An earlier draft used 3.0 s, which would have left every seam of a 5 s-overlap
   device build partially duplicated — while that same draft argued the ring-buffer
   size can change without a backend deploy.

2. **Estimate ≤ 0 means no overlap: stop here, do not correlate.** A genuine 30 s gap
   or the 806 s pause below must not be handed to a correlator that will find a
   spurious peak in repeated speech or machinery hum and trim seconds of unique audio.

3. **Refine** by cross-correlating the last `2 × estimate` of the running stream
   against the head of the incoming region, taking the lag that maximises correlation.
   Filename timestamps are whole seconds, so the estimate alone carries up to ±1 s of
   error against a 2 s overlap — half of it. This is well-posed and usually exact:
   the audio path is WAV PCM decoded through the same ffmpeg→16 kHz pipeline both
   times, so the carried ring-buffer samples are **byte-identical in both chunks**.
   (Video chunks re-encoded as AAC lose byte-identity to encoder priming, but not
   correlation.) The array is already numpy (BUG-04/BUG-06), and correlating ~10⁵
   samples is negligible beside the decode already happening.

4. **Trim the full estimate when correlation is weak.** Do NOT prefer under-trimming.
   A weak peak at a seam the timestamps say *does* overlap means silence in the
   overlap — where trimming the whole estimate deletes nothing audible. The opposite
   default is actively unsafe here: there is **no downstream net on this path**. The
   existing `_dedup_turn_boundaries` fires only when adjacent turns overlap in *time*
   (`lambda_extract_session.py:171-201`), and after step 5 a short trim produces
   *sequential* times, so it never triggers. Under-trimming therefore ships duplicated
   seam text **and** times shifted late by the residual — quietly breaking the §4
   invariant this addendum exists to protect.

5. **Record the trim in the stitch map.** The map entry for a trimmed region starts at
   its post-trim real time, not its nominal one.

6. **Assert the invariant instead of trusting it.** Because there is no net, the
   session path needs its own seam check: after restore, no two adjacent turns may
   overlap in time, and the parity test in §10 asserts on the *text*.

**Do not simply hard-code 2.0 s.** The measurement above is one session on one device
build; the ring buffer's size is a mobile implementation detail that can change
without any backend deploy, which is precisely why it is not on the wire.

### 6.5 Whole-session diarize (reuse PR #116)

The stitched WAV lands under a session prefix whose `ObjectCreated` triggers the
ElevenLabs path (`elevenlabs_utils.transcribe_segment`, `diarize=true`,
`num_speakers` from `MAX_SPEAKERS`). Because it is **one call over the whole
session**, `speaker_id` is consistent across the session. `adapt_to_transcribe_json`
produces the Transcribe-shaped dict as today — but times in it are **stitched-time**
and must be restored (6.6) before it is written as the session transcript.

### 6.6 Timestamp restore + output contract

- For each word `(concat_start, concat_end)` from the diarized result, find the
  stitch-map segment it falls in and compute
  `real_start = seg.real_start + (concat_start − seg.concat_start)` (same for end).
- **Output contract:** write the session transcript as AWS-Transcribe-shaped JSON to
  `transcripts/{user}/{date}/{session_base}.json`, where each word's `start_time` /
  `end_time` are **seconds from session start (real, silence-inclusive)** and the
  filename encodes the session start wall-clock. `transcript_utils` then computes
  absolute time as `session_base_time + word.start_time` with **offset 0 and no
  `_off` suffix** — the existing model, minimally used. Speaker labels are the
  session-consistent `spk_N`.

This keeps `transcript_utils` and every downstream consumer working unchanged;
`extract_session`'s existing "group by session" step becomes trivial (already one
file per session).

## 7. Configuration

New env / parameters (defaults preserve current behavior):

- `SESSION_CONTINUITY` — `off` (default) | `on`. Off = PR #116 per-segment path.
- `SESSION_GAP_MINUTES` — inactivity gap that closes a session (default `15`).
- `SESSION_MAX_MINUTES` — force-close safety cap (default e.g. `180`).
- `SESSION_TABLE` — DynamoDB session table name.
- `SESSION_PREFIX` — S3 prefix for stitched files + maps (default `sessions/`).

Test stack sets `SESSION_CONTINUITY=on` (dogfood); prod defaults `off` until
validated, flipped via a `PROD_*` repo variable — same mechanism as the provider
toggles.

## 8. Blast radius / changes to existing pipeline

| Area | Change |
|---|---|
| **new** session assembly + sweeper lambdas + DynamoDB session table | new |
| **VAD lambda** | gains a session-stitch mode (multi-chunk concat + stitch map); per-chunk mode unchanged when toggle off |
| **ElevenLabs transcribe path (PR #116)** | trigger source becomes the stitched session file; the call itself is unchanged. **Assumes `ASR_PROVIDER=elevenlabs`** (whole-session diarization needs a diarizing ASR) |
| **timestamp model** | session transcript carries real absolute time (offset 0); `_off` mechanism unused on this path |
| **transcript_utils + downstream** | no code change required by the output contract (§6.6); one transcript per session instead of per segment |
| **S3 event wiring** | chunk events → session assembly; stitched-file events → transcribe |

## 9. Edge cases & error handling

- **Out-of-order / late chunk arrival:** assembly orders by filename timestamp, not
  arrival order; a late chunk arriving after close is attached to a new session (or,
  if within gap of a not-yet-swept session, appended) — never silently dropped.
- **Crossing midnight / date rollover:** session keyed by device + contiguous time,
  independent of the `{date}` folder; the session's `date` is its first chunk's date.
- **Very long / abandoned session:** `SESSION_MAX_MINUTES` force-closes so a
  left-on device can't create an unbounded stitched file (also bounds Lambda memory
  per BUG-04).
- **Single-speaker session (typical PTT/touring):** diarization returns one speaker;
  fully valid, no naming needed here.
- **Silent / no-speech chunk:** contributes nothing to the stitch (VAD full-audio
  fallback still applies per chunk per BUG-07 to avoid silent drops).
- **Empty session (all silence):** produces no transcript, logged, session marked
  `done` with no output.
- **Diarize failure:** session marked `failed`, retried by a later sweep; raw chunks
  and the stitched file are retained so nothing is lost.
- **Idempotency:** re-processing a closed session overwrites the same
  `session_base` outputs deterministically.
- **Discontinuous chunks inside one session (addendum 2026-08-07):** consecutive
  chunk indices do **not** imply consecutive time. The measured session has one seam
  where `c{N}` and `c{N+1}` are **806 seconds apart** — a 13-minute pause mid-session,
  with the index sequence unbroken. Four further seams show a 30 s gap against a 30 s
  region, i.e. **no overlap at all**. So the overlap estimator in §6.4a must derive
  per seam and clamp, never assume a constant; and the stitch map must never infer a
  region's real time from its index.
- **Overlap larger than a whole region:** a short VAD region (3 s was observed) can be
  entirely inside the previous chunk's overlap. Trimming must be able to drop a region
  completely rather than produce a negative length.

## 10. Testing

- **Absolute-time parity (headline):** for a recording processed both ways, each
  word's computed **absolute** time on the new session path must equal the old
  per-segment path within a small tolerance. This is the gate that protects against
  BUG-09/BUG-11 regressions.
- **Stitch-map round-trip (unit):** given synthetic chunks with known speech
  regions, the stitch map restores every region's real time exactly.
- **Session assignment (unit):** contiguous chunks → one session; a gap >
  `SESSION_GAP_MINUTES` → two sessions (the inspection-then-meeting scenario);
  `SESSION_MAX_MINUTES` force-close.
- **Speaker consistency (integration, test stack):** a real multi-chunk recording
  yields one diarized transcript whose speaker labels are stable across chunk
  boundaries (the whole point).
- **Overlap removal (unit, addendum 2026-08-07):** synthetic chunks built with a known
  duplicated tail stitch to a stream containing that audio exactly once; a seam with
  silence falls back to the timestamp estimate; a seam whose chunks are 806 s apart
  trims nothing; a region shorter than the overlap is dropped whole, not negative.
- **Overlap removal (parity):** the session transcript from a real chunked recording
  contains each seam sentence **once**. Assert on the text, not on the sample count —
  a sample-count assertion passes while the words are still doubled.
- **Default-safe:** with `SESSION_CONTINUITY=off`, all existing tests pass unchanged.

## 11. Open items (resolve during implementation)

- ~~**OI-1:** Confirm the exact chunk duration source (media metadata vs nominal 3 min)
  and whether chunk filenames reliably encode start wall-clock for gap detection.~~
  **Answered 2026-08-07 from production data** (§6.4a): chunks are **30 s, not 3 min**;
  filenames do carry a reliable per-chunk start wall-clock, but only to **whole-second**
  granularity — which is why §6.4a refines the overlap by correlation rather than
  trusting the arithmetic. Duration should still come from the decoded media, not from
  the nominal value: 6 of 260 regions were not 30 s.
- **OI-2:** Confirm whether stitched-session diarization on long files (up to
  `SESSION_MAX_MINUTES`) stays within ElevenLabs limits / cost expectations
  (scribe_v2 auto-splits ≥8 min internally but returns one reconciled result).
- **OI-3:** Decide reuse-vs-new for the session table (extend the existing
  `fieldsight-transcripts` ledger vs a dedicated table).
- **OI-4:** Confirm the sweeper cadence and whether to piggyback on the existing
  orchestrator schedule or add a dedicated schedule.
