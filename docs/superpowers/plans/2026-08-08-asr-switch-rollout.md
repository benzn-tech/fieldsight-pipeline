# Rollout plan — ElevenLabs on prod, and the work it exposes

Findings and evidence: `docs/superpowers/specs/2026-08-07-asr-hallucination-and-vad-findings.md`

## Direction

Two decisions were taken on 2026-08-07 and everything below follows from them:

1. **A chunk with no detected speech is dropped, never transcribed.** The old
   fallback fabricated 10.7% of a meeting's words.
2. **prod moves from AWS Transcribe to ElevenLabs scribe_v2.** ElevenLabs
   captured 48% more real words on the hardest audio, and reports noise as noise
   instead of inventing sentences for it.

The direction underneath both: **the pipeline should say "I did not hear
anything" rather than produce plausible text.** Silence is a valid answer;
fabricated content is not, because nothing downstream can tell it apart.

`VAD_THRESHOLD` **stays at 0.2**. Measured, not assumed — see the sweep table.

---

## P0 — must land before the next site test

### P0-1 · Merge PR #279 and deploy to test
`fix/vad-retry-threshold`, CI green. Carries the retry fix, the drop, the
`DropSilentChunks` parameter and both workflow wirings.

**Verify on test, do not assume:**
- new `_vad_metadata.json` files carry `vad_result: no_speech_dropped` with an empty `segments` list
- no `audio_segments/` object is written for those chunks
- a chunk **with** speech still transcribes end to end

### P0-2 · Deploy prod (needs the user's click on the approval gate)
`PROD_ASR_PROVIDER=elevenlabs` **is already set** and takes effect on the next
prod deploy — it is not live yet.

**Both changes land together**, which is intentional: dropping silence without
switching the engine leaves Transcribe fabricating on everything that *does*
pass VAD.

**Confirm after deploy:**
- `fieldsight-prod-transcribe` env shows `ASR_PROVIDER=elevenlabs`
- a fresh prod transcript JSON has ElevenLabs shape (`results.transcripts` +
  `items`, **no** `audio_segments` / `language_identification`)
- the transcript viewer still renders — `speaker_turns_from_items` (PR #269/#272,
  already on main) is what covers the missing `audio_segments`

### P0-3 · Session touch no longer depends on speech
The one regression the drop introduces. `session_activity` opens/touches
`meeting_session` from `transcripts/` arrivals; with silence dropped, a quiet
stretch stops touching, and prod runs `INFER_IDLE_CLOSE=true` → premature close
→ confirmation email sent mid-meeting.

Headroom measured at 3.5 min against a 15-min `SESSION_GAP`, so this is **not**
same-night urgent — but it is the highest-priority real defect created here.
Give the touch stream a source that exists even when nobody speaks: the VAD
metadata sidecar, or the upload path.

---

## P1 — accuracy the switch does not fix

### P1-1 · Filter our own voice prompts out of transcripts
"Recording started" appears in transcripts, and therefore in minutes and action
items. Every recording is affected, not just these chunks.

### P1-2 · Make transcripts checkable against their audio
ElevenLabs still smooths unclear passages into fluent sentences (2 substantive
cases in 12). **Switching engines reduces fabrication; it does not end it.** The
durable defence is that a person can hear what a claim came from — surface the
audio next to the transcript and the minutes.

### P1-3 · Measure the switch on ordinary audio
Every conclusion here rests on the 12 hardest chunks of one meeting. Before
treating "ElevenLabs is better" as settled, compare on normal-volume chunks —
including cost and latency, not only word count.

**Update 2026-08-08 — this recording cannot supply the comparison.** Measured
with `ffmpeg volumedetect` over 18 speech chunks sampled evenly across the
session (free, no ASR calls):

| | mean dBFS |
|---|---|
| loudest | −26.3 |
| upper quartile | −33.2 |
| **median** | **−36.0** |
| lower quartile | −38.7 |
| quietest | −44.4 |

13 of 18 sit within 6 dB of the median. Normal speech capture sits around −20
to −12 dBFS, so **the entire session is quiet — there is no ordinary-volume
material in it at all.** `c0075`, one of the "12 hardest", is 7.5 dB below the
median: not an outlier, just the ordinary case for this recording.

Two consequences:

1. **The "12 hardest" framing overstated how unrepresentative they were.** They
   are the tail of a distribution that is uniformly quiet, not a separate
   category. The ElevenLabs result is therefore *more* generalisable to this
   recording than the framing suggested — and says nothing about a normal one.
2. **P1-3 needs new audio, not new analysis.** The first normal-level recording
   is the material; nothing in the existing bucket can answer the question.

**Do not run the paid comparison before prod's first real ElevenLabs
transcription succeeds.** Prod switched providers and has never run on it, the
previous evaluation exhausted a 10,000-credit allowance, and a quota failure
would present exactly as "the backend changes broke transcription". Confirm
prod, check the remaining allowance, then measure — on 30-second clips.

---

## P2 — worth doing, not blocking

- **Re-check the "37 fabricated" figure with a third engine.** Engine silence is
  evidence, not proof: on `c0132` two engines heard nothing and three agreed on
  16 words. The figure is an upper bound.
- **Move the ElevenLabs key out of plaintext Lambda env.** Readable by anyone
  with `lambda:GetFunctionConfiguration`. Pre-existing, but prod now depends on it.
- **Reconsider `VAD_THRESHOLD` 0.2 → 0.15** *only after* the ElevenLabs switch is
  proven. Loosening admits ~6 noise chunks per 135 to recover 3 real ones — safe
  with an engine that stays silent on noise, unsafe with one that fabricates.
- **Delete the A/B scratch data** under `s3://fieldsight-data-509194952652/ab-test/`.

---

## Rollback

| to undo | do this |
|---|---|
| the engine switch | `gh variable set PROD_ASR_PROVIDER --body transcribe` + redeploy |
| the silence drop | `gh variable set PROD_DROP_SILENT_CHUNKS --body false` + redeploy |

Both are repo variables, so neither needs a code change or a PR.
