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

## Channel bleed — measured, and the first answer was wrong

The docs say each channel should contain one speaker. Ours cannot: every device hears
everyone. The question is whether that matters, and the answer turns on whether **the louder
channel identifies who is talking**.

Measured by aligning the pair with the skew above and comparing per-500 ms RMS. **The
decisive statistic is the difference's spread and sign, not its mean** — a near-constant
difference means both microphones hear the same room at a fixed ratio and channel index says
nothing; a difference that swings and changes sign means different people dominate at
different moments.

| window | mean A−B | sd | min | max | swing |
|---|---|---|---|---|---|
| 14:18 | −7.5 dB | 8.1 | −29.3 | **+13.0** | 42.3 dB |
| 14:38 | −11.0 dB | 3.9 | −18.6 | −2.7 | 15.9 dB |
| 15:10 | −0.3 dB | 8.0 | −14.6 | **+22.2** | 36.8 dB |

**The 14:38 window alone says the idea is dead** — 44 consecutive windows, the sign never
flips, `Ben_UCPK` is quieter in every one of them. That reads as a fixed hardware gain
difference, and there is a known one: two microphone part variants on the same board differ
by 15 dB.

**Two more windows overturn it.** In both, `Ben_UCPK` is at times **13–22 dB louder** than
the other device. 14:38 was almost certainly a stretch where only someone near the other
wearer was talking. One window was not a sample.

What the three together say:

- **There is no fixed offset to calibrate away.** The mean moves from −11 dB to −0.3 dB
  within the same meeting, so any comparison has to be relative and adaptive, not a constant.
- **The swing is 36–42 dB and changes sign** — which is the structure "loudest channel is the
  speaker" needs, and it is a much stronger signal than embeddings get from −54 dBFS audio.
- **It is not proven.** The swing could equally come from a wearer moving, handling noise, or
  non-speech events. Establishing it needs ground truth on who spoke when *inside the
  overlap*; the existing hand-labelled set (15:22–15:27) falls after `Ben_UCPK` stops at
  15:19, so it cannot be used for this.

## What is still unknown

- **Whether the level swing actually tracks speaker turns.** Measured above, but not proven
  against ground truth — see the bleed section. This is the single question the whole
  multichannel path rests on.
- **The 5-channel ceiling** rules this out for meetings with more than five devices.

## The step, solved — and it is a recording gap, not a clock

"Clock correction or lost audio" is answerable **on one device at a time**, which the
cross-device comparison could never do. Consecutive chunks overlap by
`30 s − (gap between filename timestamps)`, because the device carries ~2 s of PCM forward.
Correlate the tail of chunk N against the head of N+1: if a device's audio agrees with its
own filenames, the peak sits exactly at the expected overlap.

Across 34 boundaries on `Ben_UCPK2` and 22 on `Ben_UCPK`, **every one measured +0 ms at a
correlation of 1.00** — the carried-forward overlap is byte-identical and both devices are
internally honest. No clock drifted; nothing slipped gradually.

**One boundary on `Ben_UCPK` is not like the others:**

| chunk | start | gap | bytes |
|---|---|---|---|
| c0071 | 14:50:31 | 28 s | 960044 |
| **c0072** | **14:50:59** | 28 s | **160044 — 5 s, not 30** |
| c0073 | 14:51:07 | **8 s** | 960044 |
| c0074 | 14:51:37 | **30 s** | 960044 |

`c0072` holds five seconds. It covers 14:50:59–14:51:04; `c0073` begins at 14:51:07.
**About three seconds of the meeting were never recorded**, and `c0073→c0074` has no 2 s
carry-forward either — the signature of the recorder stopping and restarting.

That also explains the "165 ms step": the restart left the device's chunk-start labelling
offset from where it had been, and the before/after measurements straddled it. **It was never
a clock property**, which is why extrapolating a drift rate from two points was wrong.

### How often, and why it matters more than three seconds

Across 336 chunks (two devices, 2026-08-07 and 08-08), ten are shorter than 30 s. **Nine are
legitimate** — the final chunk of a session, or a session only one or two chunks long.
`c0072` is the only short chunk in the *middle* of a session: **1 in 336, about 0.3%.**

Rare, but the shape is the problem, not the rate:

- three seconds of a meeting are simply absent, and
- **nothing anywhere notices.** The short chunk is transcribed normally, the extraction reads
  it normally, and the report is silently missing whatever was said in those seconds.

### Detecting it costs nothing

The VAD sidecar already records `total_duration_sec`. A chunk that is materially short **and
is not the session's last** is exactly this event — a query over data we already write, with
no new instrumentation.

The stronger check is a **gap**: chunk N's start plus its duration falling short of chunk
N+1's start. That distinguishes the case that loses audio from a short final chunk, which is
normal and must never warn. Neither check exists today.

## Reproducing

`aws s3 cp` a chunk from each device covering the same wall-clock window, trim each by its
own filename offset so both nominally start at the same instant, normalise, cross-correlate
over ±3 s, take the positive peak, and repeat over several windows. Report the median and
the spread; a spread in the hundreds of milliseconds means the windows disagree and the
number should not be used.
