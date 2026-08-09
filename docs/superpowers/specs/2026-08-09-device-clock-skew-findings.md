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

**Naive alignment by filename timestamp will not work.** The skew is 0.75–0.92 s —
**7.5–9× the ~100 ms** that would have made it safe. (An earlier version of this line said
"25–30×", which is simply wrong arithmetic; the same error was copied into
`multichannel_probe.py` and has been corrected there too.) Building an N-channel file that way would
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
- **The swing ranged 16–42 dB and crossed zero in two windows of three** — which is the
  structure "loudest channel is the speaker" needs, and a stronger signal than embeddings get
  from −54 dBFS audio. Stating it as "36–42 dB and changes sign" cherry-picks two of the three
  rows in the table directly above: the 14:38 window swings 15.9 dB and **never** crosses
  zero. An independent rerun over eight pairs found swings of 2.8–55.5 dB with device A louder
  in 0–37% of windows, so **there are multi-minute stretches where the sign does not flip at
  all**.
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
correlation of 1.00** — the carried-forward overlap is byte-identical.

**Be precise about what that does and does not establish.** The check correlates a
byte-copied buffer against itself, so a 1.00 peak at +0 ms shows the filename gap matches the
carried PCM amount at the filenames' one-second label resolution. It rules out gradual
drift — the flat plateaus bound any sample-rate difference at ≲20 ppm — but it is
**structurally blind to a constant sub-second labelling offset**, which would produce exactly
the same 1.00 at +0 ms. So "both devices are internally honest" is stronger than the evidence:
nothing slipped *gradually*, and nothing lost audio at these boundaries.

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

The restart also **coincides with** the "165 ms step", and the temporal bracketing is tight —
an independent rerun places the step between c0069 (~14:49) and c0085 (~14:57), and the
restart is at 14:50:59. The likely mechanism is that restarting re-samples the phase of the
chunk-start labelling. **That mechanism is inferred, not verified in device code**, so this is
"strongly associated with the restart" rather than "solved". What is settled either way: it
was not gradual drift, which is why extrapolating a drift rate from two points was wrong.

### How often, and why it matters more than three seconds

Across 336 chunks (two devices, 2026-08-07 and 08-08), ten are shorter than 30 s. **Nine are
legitimate** — the final chunk of a session, or a session only one or two chunks long.
`c0072` is the only short chunk in the *middle* of a session: **1 in 336, about 0.3%.**

⚠️ **A short mid-session chunk is not always a fault.** The app has a pause feature, and
pausing truncates the current chunk exactly as stopping does. The audit's own output shows
short chunks sitting next to flagged pauses. So "one device restarts far more than the other"
may substantially be *one user pausing more*, and the two cannot be separated from S3 alone.

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
over ±3 s, take the positive peak, and repeat over several windows.

Report the median **and** the spread, but do not use the spread as a pass/fail gate — an
earlier version of this section said a spread "in the hundreds of milliseconds" invalidates
the number, and then the table above uses a row whose spread is 213 ms. A rerun found pairs
with spreads of 1.8–2.9 **seconds** whose medians were nonetheless exactly right (+912, +752,
+750). **The median across windows is the robust quantity; the spread flags outlier windows,
not a bad median.** What genuinely invalidates an estimate is too few windows producing a
peak at all, which is what the estimator already refuses on.
