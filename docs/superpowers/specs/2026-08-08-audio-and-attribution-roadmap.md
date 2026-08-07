# Audio quality and speaker attribution — what to do next, in order

**Date:** 2026-08-08
**Status:** Roadmap. Supersedes the priorities implied by
`2026-08-07-speaker-attribution-measurement-design.md`, which was written before the
provider testing and is now largely overtaken (§P4).
**Evidence:** `Dropbox/temp/fieldsight-audio/` — a 4:49 clip of real site conversation
(UCPK2, 2026-08-07 15:22–15:27), hand-labelled ground truth, and raw results from four
ASR providers.

## The one-paragraph version

Speaker attribution is broken today, but not for the reason the earlier specs assumed.
The chunk boundaries are a real problem and a single whole-session call removes them — but
underneath that sits something cheaper and larger: **the audio is recorded so quietly that
transcription is not reproducible at all**. Fixing loudness costs one ffmpeg filter, cuts
run-to-run variation from 4.7× to 1.13×, and eliminates a failure mode that silently drops
minutes of a recording. Everything else should queue behind it, because until it lands,
no comparison between providers or architectures is measuring what it claims to measure.

## Priorities

### P0 — Loudness normalisation before ASR

**Why first:** biggest measured effect, lowest cost, independent of every other decision.

The recording averages −39.7 dBFS with a median second at −46. Seven identical
transcription requests on the raw file returned 173–815 words, and **two of the seven
began at 01:57**, silently discarding the first two minutes — no error, no log line. With
`acompressor` + `loudnorm` applied first, three runs returned 415/410/465 words, all
starting at 13 s, and all three found three speakers (no other treatment managed that).

```
acompressor=threshold=-30dB:ratio=4:attack=20:release=250:makeup=8,
loudnorm=I=-16:TP=-1.5:LRA=11
```

**Where:** in the VAD lambda, which already runs ffmpeg — applied when writing
`audio_segments/`, **not** to `users/.../audio/`. The raw upload stays untouched as
evidence.

**Not noise reduction.** Pre-ASR noise suppression is known here to cost accuracy; that
still holds. NS discards information, gain and compression redistribute loudness. Do not
let one justify or condemn the other.

**Watch for:** whether compression lifts site noise (machinery, wind) enough to create new
hallucinations. Measure on the same clip before and after.

### P1a — `TRANSCRIPT_TEXT_LIMIT` truncates long sessions

`lambda_extract_session.py:62,255` caps the prompt at 60,000 characters. A real 2-hour
session renders to 128,427 — **47% reaches the model**, ~387 of 838 turn lines. The
authoritative extraction of a long meeting covers only its first half, silently.

This is CLAUDE.md BUG-15 recurring in a different lambda; meeting-minutes already uses
120,000 for the same reason. Raising the cap is not automatically right — output tokens
and wall time scale with it on a function with a livelock history — so decide between
raising the limit and chunking the extraction, but decide.

### P1b — Device announcements are transcribed as speech

Five turns in one 70-minute session are another device's recording announcements
("Recording started", "recording stopped", "Please stop recording") transcribed as human
speech and given speaker labels. In the 5-minute window, `spk_2` and `spk_3` are largely
machine audio, so the artifact's `speaker_count: 4` counts at least one device as a
person.

**This gets worse exactly as the product gets better:** multi-device grouping means every
device announces start and stop, and every nearby device records it. Filter these before
they reach extraction — the phrases are fixed and short.

### P1c — The final extraction pass can run before the session is fully transcribed

Today's session ran to chunk c0150; the authoritative extraction covers c0000–c0129 from
95 transcripts. Media finished uploading at 15:35:23, the finalize request was written at
15:33:56, and the final pass ran at 15:36:22 — before the tail was transcribed. The
overtake-and-rerun mechanism did not fire, so **the last ten minutes are absent from the
authoritative record**, including the discussion of steel and stair reinforcement.

Diagnose why the live-overtakes-final path did not trigger before changing anything.

### P2 — Whole-session diarization, and the provider it runs on

`2026-07-23-session-continuity-design.md` (plus the 2026-08-07 overlap addendum) is the
right architecture and its premise is now evidenced rather than argued: one call over the
whole audio removes label resetting **by construction**. AWS Transcribe restarts speaker
labels every ~30 s, so one person occupies eight label instances in five minutes and is
both `spk_0` and `spk_1` inside single chunks.

But **the provider matters at least as much as the stitching**:

| | verdict |
|---|---|
| AWS Transcribe (prod today) | labels reset per chunk; "chemical side" → "mechanical side"; "spider lab" → "Before I left Oh" |
| **ElevenLabs `scribe_v2`** | the only viable option measured — stable ids across the file, ~2× the words, no invented languages |
| `qwen-audio-3.0-asr-flash-filetrans` | diarizes, but collapses to one speaker in 2 of 3 runs and invents 4–5 Chinese sentences per run over English audio, which normalisation does not fix |
| `fun-asr` | 11 sentences for 4:49, Chinese mojibake. Reject |

Sequence: P0 first (otherwise the comparison is noise), then re-score providers against
the ground truth with n≥3, then decide the switch, then build the stitch.

Also unresolved before any switch: ElevenLabs quota economics. Fifteen five-minute runs
exhausted a 10,000-credit allowance during this evaluation.

### P3 — Device-side gain, and microphone placement

The only thing the backend **cannot** repair. The distant speakers are captured at about
**5.3 bits of the available 16** (38 LSB RMS at −58.7 dBFS); normalising later amplifies
the quantisation noise with the signal. Capture is `AudioSource.MIC` with no audio effects
attached (`AudioRecorder.kt:48`, `SegmentRecorder.kt:115`).

It must be AGC or compression, **not gain**: the peak is already −2.1 dBFS with zero
clipped samples, while the median sits 44 dB down. Adding 25 dB of gain would destroy the
loud 4%.

Deliberately after P0 because it is **irreversible** — it bakes into the stored recording
— and because the backend change is testable and revertible. Belongs to the GrandTime
session.

What no setting fixes: a chest-mounted mic is ~20 cm from the wearer and 2–5 m from
everyone else. Inverse-square alone is 20–30 dB, and the two people the diarizer merges
away are precisely the two furthest from the mic. That is placement, not software.

### P4 — Reduce or withdraw the speaker-attribution spec

`2026-08-07-speaker-attribution-measurement-design.md` proposed having the reasoning pass
re-unify a person's identity across per-call labels. Ground truth from the real recording
undermines its central assumption:

- A labelled turn is **not one speaker**. Three of eighteen labelled turns contain two
  sources — one contains a device announcement followed by a person. The spec's
  `person → labels` shape cannot express this.
- Fragmentation happens **inside a single 30-second call**, not only across boundaries, so
  it is not the chunking artefact the spec treats it as.
- With a whole-file call the scattering does not occur at all, so the reassembly the spec
  offers has little left to do.

What remains is telling apart quiet and distant voices — acoustic, not textual. Reasoning
over text cannot help. Rewrite the spec around that or drop it; do not implement it as
written.

## Carried over, unrelated to audio

**Upload freeze/thaw Phase 1** is complete and unmerged: GrandTime PR #8 (478 tests) and
pipeline PR #274 (1898 tests, ships inert). Needs real-device verification before merge —
Room v5 migration over an existing install, a forced 403 freezing without a retry storm,
a frozen record staying frozen across an account switch, and a redeploy thawing by build
mismatch. Phases 2 and 3 wait on that.

## What was deployed during this work

PR #273 merged to `main` and deployed to prod on 2026-08-07 (verified by downloading the
deployed zip, not by trusting the workflow): the participants-are-speakers prompt fix, the
`speaker_count` field, self-responsible resolution, transcript-view fix, and the
skipped-request logging. The merge itself produced **zero workflow runs** — GitHub was
degraded — and the deploy had to be dispatched manually.
