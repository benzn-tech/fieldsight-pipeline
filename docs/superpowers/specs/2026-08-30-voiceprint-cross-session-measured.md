# Enrolling a voice once and recognising it later: measured on real site audio, 2026-08-30

**Result: on this material there is no cross-session signal to build on.** Turns from the same
session cluster at cosine ~0.5. Turns from *different* sessions of the same wearer score
0.12–0.45 — the range a stranger occupies. An enrolment taken in one meeting therefore scores
a later meeting's turns about as highly as it scores anybody's.

This does not contradict Phase 0's 31-of-32
(`2026-08-11-speaker-phase0-results.md`). Phase 0 scored turns against enrolments **cut from
the same recording session**. Every number it produced is a within-session number, and the
within-session number here reproduces it. What had never been measured is the thing the
product promises, which is the across-session one.

## What was run

Four real `Ben_UCPK2` prod sessions spread across a month — 2026-07-31, 08-11, 08-13, 08-27 —
capped at 14 chunks each. Turns were rebuilt from the shipped transcripts (consecutive items
with one `speaker_label`, gap < 1.2 s), kept at >= 3 s (`DEFAULT_MIN_TURN_S`), and embedded
with **the deployed model**, `models/ecapa_tdnn.onnx` pulled from the prod bucket, through a
local copy of `embed_audio` including its 45 s piecewise-average path. 119 turns.

Read-only throughout: nothing was written to any bucket, database, or profile.

One defect in the harness is worth recording because it is silent and this repository's
transcripts invite it. Chunk filenames carry **each chunk's own timestamp**, so the batch
transcript's stem does not name chunk *c+1*. Deriving the later keys from the stem fetched
nothing, and every turn past the first 30 seconds was then cut from audio that was not its
own. Turn count went 85 -> 119 once the keys were listed from the bucket instead of guessed,
and nothing about the wrong version looked wrong.

## Within a session

| session | same label | different label | gap |
|---|---|---|---|
| 2026-08-27 | 0.517 | 0.382 | **+0.135** |
| 2026-08-13 | 0.539 | 0.562 | **-0.023** |

08-13 is the more important row: the transcriber's two speaker labels are, to the embedder,
the same voice. That is consistent with what diarisation has been observed to do here
(`fieldsight-speaker-attribution-findings`), and it means label-derived ground truth cannot be
trusted as a control.

The ~0.5 is not the room. Restricting to pairs from **different transcript batches** — different
audio files, different background — same-label similarity stays 0.48–0.55. Whatever the model
is keying on survives a change of file within the session; it does not survive a change of
session.

## Across sessions

Per (session, label) centroid, >= 15 s of speech each:

```
              07-31   08-11   08-13a  08-13b  08-27a  08-27b
07-31          1.00    0.16    0.20    0.16    0.12    0.45
08-11          0.16    1.00    0.24    0.16    0.25    0.28
08-13a         0.20    0.24    1.00    0.95    0.20    0.25
08-13b         0.16    0.16    0.95    1.00    0.20    0.21
08-27a         0.12    0.25    0.20    0.20    1.00    0.81
08-27b         0.45    0.28    0.25    0.21    0.81    1.00
```

Two readings, and they agree:

* **0.95 and 0.81** between two labels the transcriber called different people, inside one
  session.
* **0.45 at best** between any pair of sessions, most of them 0.12–0.25 — while the wearer
  speaks in all four.

The conclusion does not depend on knowing who is who, which is why it is stated without that
claim. Nobody listened to this audio. If the vectors carried identity, *some* cross-session
pair would have to reach the within-session level, because the same person is present on both
sides of at least one of them. None does.

## What this changes

* **`SPEAKER_IDENTITY_MODE` should stay `off` in prod.** With this separation, turning it on
  puts tentative names that cannot be justified onto a surface customers read. The gate is
  doing its job.
* **The enrolment guard is not the only blocker.** `2026-08-17-homogeneity-threshold-measured.md`
  and `voiceprint-enrolment-must-not-be-a-performance` describe enrolment being *refused*. This
  says that even an accepted enrolment would not be recognised in a later session, so raising
  the admission rate alone cannot deliver the feature.
* **Do not tune the margin against this.** `DEFAULT_MIN_MARGIN` separates candidates within one
  turn; nothing here is evidence about it.

## What is untested and would change the answer

* **Whether the ceiling is the material or the model.** Everything above is one ECAPA export on
  16 kHz device audio recorded at a known-low level (median -36 dBFS,
  `fieldsight-recording-loudness-baseline`). A model change is not indicated by this data, but
  it is not excluded by it either — that is a different experiment.
* **Enrolment from several sessions.** Every profile here is effectively single-session. A
  profile built from windows in three different meetings is exactly the shape that would
  cancel a session-level channel term, and `aggregate_scores` already takes the max over a
  person's samples. This is the one design change the measurement actively suggests.
* **A same-room control.** Two devices in one meeting would separate "session" from "channel",
  which this cannot.

## Reproducing

`scripts/` holds no harness for this yet; the collector and the two analyses were written
against the model and prod S3 directly and are described above in enough detail to rebuild.
Anyone repeating it should list chunk keys from the bucket rather than deriving them, and
should report same-batch and different-batch pairs separately.
