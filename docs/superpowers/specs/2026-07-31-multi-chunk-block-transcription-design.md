# Multi-chunk block transcription (fix per-segment fragmentation)

Date: 2026-07-31
Base branch: `feat/rolling-summary` (off `feat/chunk-stitch` off `develop`)
Status: DESIGN — awaiting user confirmation of parameters before implementation.

## 0. TL;DR

Today the pipeline transcribes **one Transcribe job per VAD segment**. A 30 s device
chunk is split by Silero VAD into up to ~4 short segments, and each is transcribed in
isolation. Measured on a real session this produces three defects: (1) diarization is
meaningless (per-segment `spk_N`, no global speaker identity), (2) language-ID flips on
tiny clips, (3) it is *more expensive* than transcribing larger units because Transcribe
bills a 15 s minimum per request.

Fix: transcribe **multi-chunk blocks** (≈2 min of joined audio) instead of per segment.
Consecutive blocks deliberately **share one whole chunk (~30 s overlap)**. The overlap is
reused three ways — sentence stitching, confidence cross-check, and **speaker-label
chaining** across blocks (approximate global diarization with no separate pass). At
finalize, the whole session is joined and transcribed once for the authoritative report.

## 1. Problem (measured, not assumed)

Real session `sidf83256383cab420e990d764930ffe87b` (Ben_UCPK2, 2026-07-31, ~10 min,
chunks c0000..c0020, 34 transcript segments in `transcripts/…`, prod bucket).

Current flow (verified in code):
- `lambda_vad.py` — VAD each uploaded chunk into speech segments, writes each as a
  separate `audio_segments/…_off{X}_to{Y}_src{ext}.wav` (per-segment).
- `lambda_transcribe.py` — S3-triggered on `audio_segments/*.wav`; **one AWS Transcribe
  job per segment** (job shape confirmed: `jobName`/`speaker_labels`/`language_identification`).
- `chunk_stitch.py` / `assemble_deduped_turns` — word-level dedup of the device's ~2 s
  chunk overlap when assembling turns for the rolling summary + final extraction.

Measured defects:
1. **Diarization is broken.** Each segment's `spk_0/1/2` is local to that job. c0000–c0013
   were all `spk_0`; c0018 alone shows `spk_0/1/2`. Same person → different labels across
   segments; short segments collapse everyone to `spk_0`. A multi-person meeting cannot be
   attributed. This is the user-reported "single person judged as many speakers".
2. **Language-ID flips on tiny segments.** A 1.6 s clip was mis-detected en-US
   ("In the right place in human heart" — actually Chinese). Per-segment language ID on
   1–5 s clips is unreliable; whole-unit context stabilises it.
3. **Cost inversion.** AWS Transcribe bills a **15 s minimum per request**. A 30 s chunk
   split into 4 segments = 4 × 15 s = **60 s billed**; the same chunk as one job = 30 s.
   Per-segment is both worse quality *and* ~2× the cost here.

What already works (keep it):
- **Chunk-boundary continuity is fine.** The device carries ~2 s PCM into the next chunk;
  `chunk_stitch.dedup_overlap` removes the repeat (verified: c0012→c0013 "对，把这个用起来"
  appears in both and is deduped). Word accuracy of Transcribe on zh/en code-switching is
  good — the ASR model is not the problem.

## 2. Goals / non-goals

Goals:
- Coherent transcript units (whole thoughts, not silence-sliced fragments).
- Global, or near-global, speaker identity across a ≥30 min meeting.
- Preserve the ≤2 min confirmation email (Tier-0) and the mid-meeting rolling summary
  (Tier-1). Meetings are ≥30 min, so batch-after-stop is NOT acceptable for either.
- Do not increase (ideally reduce) ASR cost.
- Keep offline resilience: the device store-and-forward chunk contract is unchanged.

Non-goals:
- WebSocket streaming ASR as the primary path (rejected: 30 min on site cellular →
  frequent disconnects; would still need the chunk fallback; sub-second latency is not
  needed for a summary that refreshes ~every 75 s). Streaming stays for SP-Ask / a future
  online-only overlay.
- Replacing the ASR provider (Transcribe accuracy is adequate).
- Any mobile-client change (block assembly is entirely server-side).

## 3. Design

### 3.1 Block assembly (server-side, from existing chunks)

A **block** is K consecutive chunks joined into one audio stream. Consecutive blocks
**share one whole chunk** so a sentence cut at a block boundary is whole in at least one
block:

```
chunks:  c0 c1 c2 c3 c4 c5 c6 c7 c8 c9 …
block A: [c0 c1 c2 c3]
block B:          [c3 c4 c5 c6]        (shares c3 with A)
block C:                   [c6 c7 c8 c9] (shares c6 with B)
```

Blocks share chunk boundaries we already have — no mid-chunk audio cutting. Overlap of a
full ~30 s chunk far exceeds any single spoken sentence (~5–10 s), so continuity is safe.

### 3.2 Intra-block audio join

Within a block, adjacent chunks carry the device's ~2 s overlap (capped 10 s upstream).
Concatenating raw would stutter and double words. Join options:
- v1: trim the known device overlap by time (drop ~first 2 s of each non-first chunk).
- v2 (robust): cross-correlate adjacent chunk audio to find the exact overlap and splice
  at best alignment (handles device-clock jitter).
Start with v1; escalate to v2 only if jitter causes audible seams / duplicate words.

VAD stays as a **cost gate**: a chunk (or block) with no detected speech is skipped
entirely. But a speech-containing chunk is transcribed *whole within its block* rather
than sliced into per-segment jobs — this is where the 15 s-minimum cost inversion is won.

### 3.3 Block-level overlap dedup

The shared chunk is transcribed in both neighbouring blocks → the overlap region yields
the same word run twice. Reuse `chunk_stitch.dedup_overlap` (already content-based,
longest-matching-run, case/punct-insensitive) but at **block granularity**: align block
N's tail against block N+1's head, drop the duplicated prefix, splice. `DEFAULT_MAX_WINDOW`
(=40 words) may need raising for a ~30 s overlap (a chunk can hold ~60–90 words); make it
a parameter derived from overlap size.

### 3.4 Cross-check / confidence reconciliation (the user's "互相对照")

The overlap is transcribed twice. When the two transcriptions of the same audio DISAGREE
(alignment finds a near-match, not exact), that region is low-confidence: keep the side
with higher Transcribe item confidence. Free quality signal; also a health metric (high
disagreement rate ⇒ noisy audio / VAD trouble on that session).

### 3.5 Speaker-label chaining across blocks (approximate global diarization)

Both blocks transcribed the SAME speakers in the shared chunk. So within the overlap we
can build a mapping between block N's local labels and block N+1's local labels — e.g.
"whoever is `spk_0` in A's tail is `spk_2` in B's head" (match by who spoke which of the
aligned overlap words). Chain these per-boundary mappings across the session to assign
**one stable global speaker id** to each local label. This fixes defect #1 *incrementally,
during the meeting*, with no separate whole-session diarization pass.
- Ambiguity (two speakers both present in the overlap, crossed): fall back to "unknown"
  for that boundary rather than guess; the finalize pass (3.7) resolves it globally.

### 3.6 Rolling (Tier-1) — stable freeze + trailing re-transcribe

Keep `lambda_rolling_summary`'s cadence (`MIN_RESUMMARY_INTERVAL_S = 75`). As chunks
arrive, only the **trailing block** (the one still filling) is (re)transcribed; blocks
that have been fully superseded are **frozen** and never re-transcribed. Cost is O(n)
across the meeting, not O(n²). The rolling summary continues to read the assembled,
deduped, now-globally-speaker-tagged stream.

### 3.7 Finalize (Tier-0 report) — whole-session single pass

At stop/grace, join ALL chunks into one audio stream and transcribe **once** with
diarization → the cleanest global speaker identity + maximal context for the report and
extraction. This runs **async, off the ≤2 min critical path**: the confirmation email
still ships immediately from the last rolling summary; the polished report/diarization
follows and updates the stored transcript. Block-chaining (3.5) is "good enough" live; the
whole-session pass is "best" for the deliverable — they do not conflict.

## 4. Parameters (proposed defaults — CONFIRM)

| Param | Default | Rationale |
|---|---|---|
| block size K | 3–4 chunks (~90–120 s) | ≥ diarization needs context; matches 75 s rolling cadence |
| block overlap | 1 chunk (~30 s) | ≫ any single sentence; enables speaker chaining |
| dedup window | ≈ overlap word count (raise from 40) | must span a full overlap chunk |
| intra-block join | v1 time-trim ~2 s | upgrade to cross-correlation only if needed |

Tradeoff to confirm with the user: **bigger block ⇒ better diarization/context but a less
fresh live view + slightly more cost; smaller block ⇒ more real-time.** Recommendation:
~2 min (the rolling summary only refreshes every 75 s, so a larger, more coherent block is
the better feed).

## 5. Cost

Per-segment today: Σ over segments of max(15 s, seg_len). Measured chunk c0000 = 4 segments
= ~60 s billed for 30 s of audio. Per-block: one job per block minus the overlap re-charge.
For K=4, overlap=1: each chunk (except session ends) is transcribed ~1.33× → still far below
the per-segment 15 s-minimum blowup, and language-ID/diarization improve. Net expected:
**lower cost, higher quality.**

## 6. Latency

- Rolling: unchanged 75 s cadence; trailing-block transcription is one job per tick.
- Finalize email: unchanged (ships from rolling; ≤2 min preserved).
- Whole-session pass: ~0.2–0.5× realtime for Transcribe batch ⇒ a 30 min meeting ~6–15 min
  AFTER stop, but off the critical path (report "updates" when ready). If that lag is
  unacceptable, options: (a) reuse the block-chained transcript as the report and skip the
  whole-session pass, or (b) a faster async ASR for this one pass.

## 7. Backend changes (scoped)

- `chunk_stitch.py` (pure): add block assembly (group chunks into overlapping blocks),
  parameterise the dedup window, add speaker-chaining helper. Pure + unit-tested.
- `lambda_vad.py`: add a mode that emits per-CHUNK (or per-block) speech audio for
  transcription instead of per-VAD-segment files — VAD still gates silence, but the unit
  handed to Transcribe is the joined block. (Keep legacy per-segment path behind a flag
  for rollback.)
- `lambda_transcribe.py`: trigger/handle one job per block; carry `session_id` + block
  index tokens on the output key (mirrors the existing `_sid…_c…` convention).
- `lambda_rolling_summary.py`: `assemble_deduped_turns` consumes block transcripts +
  applies global speaker ids; trailing-block-only recompute.
- Finalize (`lambda_finalize_claim` / `lambda_session_finalize`): add the optional
  whole-session pass; email path unchanged.
- No schema change required (transcripts remain S3 JSON; speaker ids are derived).

## 8. Mobile contract impact

**None.** The device already: mints a 32-hex `session_id`, chops ~30 s chunks with ~2 s
overlap, names them `{device}_{date}_{HH-MM-SS}_sid{32hex}_c{NNNN}`, and store-and-forwards.
Block assembly is 100 % server-side. Offline resilience is preserved.

## 9. Risks / open questions

- Speaker chaining fails when the overlap chunk has crossed/ambiguous speakers → mark
  unknown, let finalize resolve. Acceptable for the live view.
- Transcribe batch job startup latency for many blocks — measure; may prefer fewer, larger
  blocks, or a warm path.
- Cross-correlation join (3.2 v2) may be needed sooner than expected if device overlap
  isn't a stable 2 s — verify against real chunk audio.
- Confirm the 15 s-minimum applies to batch Transcribe (measured behavior consistent with
  it; verify billing).

## 10. Rollout

1. Land pure `chunk_stitch` block/chaining functions + unit tests (no pipeline change).
2. Add the block-transcription path behind a flag; keep per-segment for rollback.
3. Verify on `test` with a real ≥30 min device recording (diarization coherent, cost down).
4. Compare block-chained vs whole-session finalize diarization; decide if the whole-session
   pass is required or the chained transcript suffices.
5. Promote to prod via develop→main with the usual approval gate.
