# Transcribe two minutes at a time, not thirty seconds at a time

> **SUPERSEDED IN PART, 2026-08-13.** The batching RULE described here — four
> consecutive chunk indices, split at every gap — was replaced by a 120-second
> wall-clock window in `2026-08-13-batch-by-wall-clock.md`. `plan_batches` and
> `pending_runs` no longer exist. Measured reason: a chunk VAD dropped ended the
> batch, which shattered 31% of real sessions into short runs (one 46-chunk session
> produced [1,1,1,2,1,1]). Everything else here — the map, the filename contract,
> the ledger's two races, the tail seal — still stands.

**Status:** spec · 2026-08-11
**Scope:** a batching stage between `lambda_vad` and the transcriber. VAD stays per-chunk.
No mobile change.

## What is wrong today

The device rolls audio into ~30 s chunks. `lambda_vad` runs on each chunk as it lands and
emits one or more units into `audio_segments/`, and every one of those units becomes its own
transcription request. A two-minute stretch of a meeting is therefore transcribed as four
independent problems that share nothing.

Three costs follow, and only the third is about money.

**Speaker labels do not survive the call boundary.** `speaker_label` is assigned per request,
so the same person is `spk_0` in one call and `spk_1` in the next. Nothing downstream can
undo this, because the turn boundaries themselves come from the provider's labels — the
merge that hides a person happens before we see the data. Batching does not fix speaker
attribution (fragmentation was measured *inside* a single 30 s call: one person appearing as
both `spk_0` and `spk_1`), but it cuts the label space by 4× and removes three of the four
places per two minutes where a stable identity is re-rolled from scratch.

**Every seam is a place where real speech gets deleted.** The device seeds each new chunk
with the tail of the previous one, so consecutive chunks transcribe the same words twice and
something has to remove the duplicate. That something is `chunk_stitch.dedup_overlap` /
`_dedup_turn_boundaries`, and it is where the ASCII-normalisation bug silently deleted up to
40 words of real speech at *every* seam (PR #314; `chunk_stitch`'s window is 40 words, the
turn-boundary path runs 12). Fewer seams is fewer chances. Batching takes the three seams
*inside* a two-minute stretch to zero. The seam *between* batches remains, so the head of
each batch's first chunk must be trimmed against its predecessor exactly like any other —
otherwise the saving below does not hold either. `_dedup_turn_boundaries` stays in place as
the residual net; this reduces how often it has to be right, it does not retire it.

**It is not even cheaper to do it in pieces.** Measured against ElevenLabs:

| request | wall clock |
|---|---|
| 30 s of audio | 2.32 s |
| 120 s of audio | 6.42 s |
| 4 × 30 s | 9.3 s |

Batching is ~30% *faster* end to end, and trimming the duplicated overlap before sending
removes ~6.7% of the audio we currently pay to transcribe twice.

## What this changes

`lambda_vad` is untouched: it must stay online, cheap and per-chunk, because it is what
decides whether a chunk contains speech at all and that decision is needed immediately.

A batching stage accumulates the audio of up to **four consecutive chunks of one session**,
concatenates it into one object, and issues one transcription request for it.

## The part that will break, and how

### Three coordinate systems, and only one of them is real

- **absolute wall-clock** — what every consumer needs, and the only one that is real
- **chunk-relative** — what `audio_segments/…_off{start}_to{end}_…` encodes today
- **batch-relative** — what the provider returns: `word.start` counted from the first sample
  of the concatenated file

Today the conversion is a three-term sum that `transcript_utils` already owns:
base time from the filename + VAD offset from the filename + `word.start`. With a batch,
`word.start` is measured from an origin that exists nowhere in any filename, and the offset
from that origin to a given chunk depends on **how much overlap was trimmed from every
earlier seam in the same batch**.

So the batch artifact must carry an explicit **position → absolute time map**, written at
concatenation time and stored alongside the audio: for each member chunk, its index, its
absolute start, its byte offset in the concatenated file, and the number of samples trimmed
from its head. Every word's absolute time is resolved by looking up the segment its offset
falls in and adding, never by re-deriving the arithmetic at read time.

This must not be a computed convenience. The failure mode of getting it wrong is that every
timestamp in the meeting is shifted by a second or two — reports still read fine, photos
still bind to something, the daily summary is still fluent. Nothing looks broken. This is the
same shape as the site-attribution bug, where wrong data was indistinguishable from right
data until someone searched for themselves and found nothing.

### The overlap length is not a constant — measure it

The trim is the dangerous edit. Too little and the transcript stutters; too much and real
speech is deleted with no trace, which is exactly what PR #314 was.

The sources disagreed about how long the overlap is:

| source | says |
|---|---|
| `AudioSegmentation.overlapBytesFor(2)` (mobile, `CaptureManager` audio + video paths) | 2.0 s |
| `lambda_extract_session._dedup_turn_boundaries` docstring | "~2s of PCM" |
| observed chunk start times in real material (`14-18-47 → 14-19-15 → 14-19-43`) | 28 s cadence, consistent with a 30 s file and a 2.0 s overlap |
| a measurement recorded on 2026-08-10 | "exactly 1.50 s, byte-identical" |

**Settled by running it: 2.0 s.** On the TEST stack, 2026-08-12, four real device chunks
were batched and all three seams measured **exactly 2.000 s with `trim_measured: true`** —
a byte comparison of the raw uploads, not an inference. The 1.50 s outlier is not
reproducible on this device's audio. Nothing below changes: the trim stays measured rather
than configured, because the value being knowable today does not make it constant, and the
`segmentSeconds` device setting can move it.

**Therefore the trim length is measured per seam, not configured.** Compare the tail of
chunk *N* with the head of chunk *N+1* at PCM level, take the longest byte-identical run up
to a **2.0 s ceiling**, trim exactly that, and log the measured value with the seam. If the
measured run is zero, trim nothing — an unexpected zero means the assumption is wrong, and
the safe response to a broken assumption is to keep the audio and say so, not to cut
blind. The distribution of those logged values settles the 1.50 vs 2.0 question with
evidence, as a side effect of running.

**Measure it on the raw upload, never on the `audio_segments/` unit.** `NORMALISE_AUDIO`
runs `acompressor` + single-pass `loudnorm` per chunk, and both have time-varying gain: the
same source samples at a seam come out as *different bytes* in the two chunks, so a
byte-identical comparison on normalised audio finds nothing. Combined with the rule directly
above — measured zero, trim nothing — **turning normalisation on would silently switch all
trimming off**, with no error, the overlap transcribed twice, and the saving quietly gone.
That is the same silent-failure shape this section exists to prevent, hiding inside the
mechanism meant to prevent it. So the measurement runs on `users/…/audio/`, which is
untouched by design and where the device's ring-buffer copy really is byte-identical; the
result is converted to 16 kHz sample counts and applied to the normalised unit. That transfer
is sound because `normalise_for_asr` refuses any output whose sample count differs from its
input, so the two are the same length by construction.

`segmentSeconds` is a device *setting*, so the chunk length is not guaranteed to be 30 s
either. Nothing in the batching stage may assume a fixed chunk duration.

### A batch is not always four chunks

- **A chunk is missing.** Prod is still losing uploads (0.9% of chunk-session recordings;
  `UPLOAD_VERIFY_MODE` ships in `observe` first, so the hole is measured but not yet
  closed). A gap must never be spliced shut — that silently moves every later word earlier
  by 28 s. Nor may the gap merely be *recorded in the map*: only extract-session would read
  the map, while reports, minutes, the transcript viewer and ingest all resolve time by
  filename arithmetic and would be a full chunk wrong past the gap. **So a batch never spans
  a gap at all** — a missing index, a VAD-dropped index, or a date-folder boundary seals the
  batch early. The map then only has to carry per-member trims, which is the whole of what it
  is trusted for.
- **A chunk is late.** Uploads can arrive hours after the recording, and the freeze/thaw work
  makes that more likely, not less. A batch is sealed on a deadline; a chunk that arrives
  after its batch was sealed is transcribed on its own rather than reopening it. Reopening a
  sealed batch means re-issuing a paid request and re-writing artifacts other things have
  already read.
- **A session ends mid-batch.** The last batch is 1–3 chunks and must not wait for a fourth
  that will never come. Sealed by session close, or by the idle timeout the finalize path
  already infers.
- **A chunk was dropped as silent.** `DROP_SILENT_CHUNKS` removes chunks before this stage.
  A dropped chunk is a gap like any other, and seals the batch for the same reason.

### It must be switchable, in all three segments

`BATCH_TRANSCRIPTION` (repo variable → workflow `--parameter-overrides` → template
Parameter). A switch that exists only in code takes its default forever and raises no error —
`FILTER_AUDIO_EVENT_TAGS` shipped that way once, and `TRANSCRIBE_WHOLE_CHUNK` was hard-coded
in the template with no Parameter at all. Both are fully wired now, and the boolean-Parameter
sweep in `test_template_workflow_parameter_wiring.py` auto-pins any new boolean switch, so
omitting the workflow line for `BATCH_TRANSCRIPTION` turns CI red by itself. Rollback here
means "go back to per-chunk requests", and it has to actually work.

Note that `TRANSCRIBE_WHOLE_CHUNK` already sends the whole 30 s chunk rather than the VAD
segments within it (~70% of the audio on the measured session). Batching composes with it:
whole-chunk decides *what within a chunk* is sent, batching decides *how many chunks per
request*. They must be tested together, because whole-chunk is what makes a batch a clean
2 minutes rather than a ragged concatenation of speech islands.

## How this is verified

1. **The map, not the audio, is the unit under test.** Given four chunks with known absolute
   start times and known overlaps, every word's resolved absolute time must equal the time
   the per-chunk path produces for the same word, within one sample period. Include a batch
   with a missing chunk and a batch with an unequal chunk length.
2. **Trim measurement on real bytes.** A synthetic pair with a known 2.0 s identical tail,
   one with 1.5 s, one with 0. Assert the measured value, and assert nothing is trimmed in
   the zero case.
3. **Same audio, both paths.** One real session transcribed per-chunk and batched; compare
   word counts and the absolute time of the first and last word of every turn. A batched run
   that produces *more* words at the seams is the overlap not being trimmed; *fewer* is
   speech being deleted, and that is a stop-ship.
4. **Read the deployed function's env** to confirm `BATCH_TRANSCRIPTION` is actually present
   with the intended value. The switch existing in the repo is not evidence it reached
   Lambda.
5. **Cost and latency on the real path**, not the benchmark: the numbers above came from
   direct API calls, and prod adds S3 reads, concatenation and a larger payload.

## What this does not do

It does not fix speaker attribution. Fragmentation happens inside a single call, a turn can
contain two people, and device announcements are still transcribed as a participant. Batching
reduces the number of times identity is re-rolled; it does not make identity work. That is
the speaker-identity track, and its Phase 0 gate is a separate, still-unrecorded session.

## Review corrections (2026-08-11 adversarial review — see the plan for full reasoning)

1. **`TRANSCRIBE_WHOLE_CHUNK` is no longer hard-coded.** On this branch it is a full
   three-segment switch: template Parameter (`src/template.yaml` ~505, env ref ~989), both
   workflows pass it (`TEST_/PROD_TRANSCRIBE_WHOLE_CHUNK || 'true'`), pinned by
   `test_template_workflow_parameter_wiring.py::test_the_whole_chunk_mode_is_wired…`. Same
   for `FILTER_AUDIO_EVENT_TAGS`. The cautionary tale is history, already fixed; the
   boolean-Parameter sweep in that test file will auto-pin `BATCH_TRANSCRIPTION` too.
2. **The per-seam byte-identical trim cannot be measured on `audio_segments/` units.**
   `NORMALISE_AUDIO` applies per-chunk `acompressor+loudnorm` (time-varying gain), so the
   same source bytes at a seam produce different output bytes in the two chunks; combined
   with this spec's own "zero → trim nothing" rule, enabling normalisation silently
   disables all trimming. Measure on the raw uploads under `users/…/audio/` (untouched by
   design; the device ring-buffer copy IS byte-identical there), convert to 16 kHz sample
   counts, apply to the normalised units — valid because `normalise_for_asr` refuses any
   output whose sample count differs from the extraction.
3. **Seams do not go "to zero"** unless the head of a batch's *first* chunk is also
   trimmed against its predecessor (the inter-batch seam); the ~6.7 % figure assumes that.
   `_dedup_turn_boundaries` must stay as the residual net either way.
4. **Gaps encoded inside the map are themselves a silent-failure path**: every consumer
   except extract (reports, minutes, viewer, ingest) resolves time by filename arithmetic
   and never reads the map, so a gap inside a batch shifts their timestamps ~28 s. The plan
   replaces map-encoded gaps with "never batch across a gap / VAD-dropped index / date
   boundary — seal early"; the map keeps only per-member trims.
5. Minor: the conversion is a three-term sum (base + VAD offset + word.start); the "40
   characters at every seam" window is `chunk_stitch.DEFAULT_MAX_WINDOW` (words), while the
   turn-boundary path runs `max_window=12`; a VAD-dropped chunk leaves a
   `_vad_metadata.json` sidecar the batcher could read, though the plan seals at gaps
   instead. Unverifiable from this repo (flagged, not contradicted): the EL latency table,
   6.7 %, 0.9 %, `overlapBytesFor(2)`, the 1.50 s measurement, the 28 s cadence.

---

## It ran (TEST, 2026-08-12)

Phase 6b, driven with real material rather than a new recording: the six chunks of the
2026-08-11 Block V session A (three people, two of them at 5 m) were uploaded to the test
bucket twice under fresh session ids — once with the flag off, once on — so both paths saw
byte-identical audio on the same stack.

**The check that could have stopped this: no speech was lost.**

| | words |
|---|---|
| per-chunk baseline, c0000–c0003 | 219 |
| …of which fall in the 2 s each chunk repeats from the one before | 17 |
| baseline with the duplication removed | **202** |
| batched | **205** |

`+3`, i.e. the safe direction. The rule was "more words at the seams means the trim is not
happening; **fewer** means stop-ship". Three extra words out of 202 is inside run-to-run
ASR variation, and some of them are plausibly words that used to be cut in half by a
boundary.

**The batch object and its map came out as designed:** one
`_bn4_off0.0_to114.0_srcwav.wav` — 120 s of chunks minus 6 s of overlap — plus its sidecar,
and **zero member transcripts**. The members were accumulated, not transcribed.

**Speaker labels moved in the right direction**, which was the point of the exercise:

| | labels | extraction |
|---|---|---|
| per-chunk (4 chunks) | `spk_0`, `spk_1` | 1 topic, `speaker_count=2` |
| batched | `spk_0`, `spk_1`, `spk_2` | 2 topics, `speaker_count=3` |

Three people were in the room. The batched extraction also separated out the missing
scaffold handrail as its own topic; the per-chunk one did not. **More labels is not the same
as correct labels** — the same day's voiceprint work showed this transcriber's labels
scramble their contents — but 2→3 with three people present, and a safety item surfacing
that had been buried, is the shape the design predicted.

### What this run did not exercise

- **The tail seal (phase 4).** `c0004` was registered and left unsealed: in a real session
  the finalize sweep seals it at close, but these chunks were uploaded straight to S3 with
  no `meeting_session` row, so the sweep could not see them. A limitation of the method,
  not a defect found.
- **Latency and cost on the real path** were not separately timed.
- **`c0005` was dropped by VAD as silent**, on test exactly as it was on prod — so the
  comparison covers c0000–c0004 and the batch covers c0000–c0003.

TEST is left with batching on. Prod is untouched; phase 7 remains a separate decision.
