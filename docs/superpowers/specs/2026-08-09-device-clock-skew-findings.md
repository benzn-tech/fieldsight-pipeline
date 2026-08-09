# Two devices, one meeting — how far apart are their clocks?

**Date:** 2026-08-09
**Why this was measured:** ElevenLabs Scribe v2 supports multichannel transcription — up to
5 channels, each transcribed independently, each word tagged with `channel_index`. For a
multi-device recording that is a very attractive shape: each device is worn by a different
person, so **channel index would *be* speaker identity** — no voiceprints, no clustering, no
thresholds, no biometric data, and none of the acoustic difficulty that makes embeddings
unreliable on this audio.

It only works if the devices' audio can be put on one timeline. Devices share no clock
(BUG-37), so the question is how far apart they actually are.

**Material:** 2026-08-07, one meeting recorded by two devices at different distances —
`Ben_UCPK` `sid39ad6c92` (129 chunks, 14:17:20–15:19:09) and `Ben_UCPK2` `sid61be49d5`
(151 chunks, 14:17:00–15:27:02). About 62 minutes of overlap.

## Method

Both devices timestamp each chunk from their own system clock. Take a wall-clock instant
both claim to cover, extract the same window from each by its own filename timestamp, and
cross-correlate. If the clocks agreed the peak would sit at zero — acoustic propagation over
2–5 m is 6–15 ms, far below what matters here. Whatever the peak is, is the skew.

Levels differ by tens of dB (different distances), so each signal is normalised before
correlating — this measures timing, not amplitude.

**Two mistakes worth recording, because both produced confident wrong numbers:**

1. **Taking the largest *absolute* correlation** picks negative troughs. The same acoustic
   event on two microphones correlates *positively*; searching `argmax(|c|)` returned a
   −0.15 "peak" that was meaningless. Search the positive peak only.
2. **Windows filesystems are case-insensitive.** `ffmpeg -i A2.wav … a2.wav` reads and
   writes the *same file* and destroys it. The first "later" measurement was computed on an
   78-byte truncated file. Always give the output a distinct name, and check durations
   before trusting a result.

A single window is not evidence either. Every number below is the median of 6–7 overlapping
windows, with the spread reported.

## Result

| wall clock | skew | windows | spread |
|---|---|---|---|
| 14:18:26 | **+918 ms** | 3 of 6 strong windows agree exactly | rest were weak-peak noise |
| 14:38:58 | **+915 ms** | 6/6 | 10 ms |
| 14:54:53 | **+754 ms** | 7/7 | 213 ms |
| 15:10:45 | **+748 ms** | 6/7 | 23 ms |

**This is a step, not a drift.** Two plateaus — about 917 ms for the first 20 minutes, about
750 ms for the last 16 — with a ~165 ms jump somewhere between 14:39 and 14:55. A crystal
drifting would move continuously; something discrete happened. Candidates: a clock
correction on one device, or a dropped/duplicated audio buffer.

**The second candidate is worth chasing on its own.** If a device silently loses ~165 ms of
audio mid-recording, then every word timestamp after that point is wrong relative to the
chunk's claimed start — which affects far more than multi-device alignment. It would mean
the absolute time on a claim is quietly off by that much for the rest of the session.

## What this means for multichannel

**Naive alignment by filename timestamp will not work.** The skew is 0.75–0.92 s, roughly
25–30× the ~100 ms that would have made it safe. Building an N-channel file that way would
produce confident, wrong speaker turns — worse than no attribution at all, because it looks
authoritative.

**Measured alignment does work, and is cheap.** Cross-correlating one chunk pair took
milliseconds and both plateaus were stable to 10–23 ms. Since the offset steps mid-session,
alignment has to be computed **per chunk pair, not once per session** — about 150
correlations for an hour-long meeting, all local, no ASR cost.

So the path is open, with a prerequisite that is real work but not research.

## What is still unknown

- **Channel bleed.** Every device hears everyone, just at different levels; the docs say each
  channel should hold one speaker. Bleed is also the opportunity — the same utterance on
  every channel means **the loudest channel identifies the speaker**, which is standard
  multi-microphone attribution and far more robust than embeddings on −54 dBFS audio. With
  `multichannel_output_style=combined` every word already carries `channel_index`, which is
  exactly the input such a rule needs. Not yet measured.
- **Whether the step recurs**, how often, and whether it is a clock correction or lost audio.
  Two sessions is not a sample.
- **The 5-channel ceiling** rules this out for meetings with more than five devices.

## Reproducing

`aws s3 cp` a chunk from each device covering the same wall-clock window, trim each by its
own filename offset so both nominally start at the same instant, normalise, cross-correlate
over ±3 s, take the positive peak, and repeat over several windows. Report the median and
the spread; a spread in the hundreds of milliseconds means the windows disagree and the
number should not be used.
