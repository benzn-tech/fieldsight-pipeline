# Enrolment is blocked, and the homogeneity guard is why — but not for the reason I first wrote

**Status:** spec, revised after adversarial review.
**Date:** 2026-08-17
**Blocks:** enrolment. No voiceprint can currently be created from real site audio.

> **Revision note.** The first draft named a cause ("the frames are not all speech"),
> proposed a fix (gate the frames), and claimed corroboration from the transcriber and from
> our own clustering. The review took all three apart:
>
> * a speech gate can only REMOVE frames, and `frame_spread` is a max over pairs — so it can
>   never turn a refusal into an acceptance. **It does not unblock enrolment.** It would take
>   the score from 5-of-6 refused to 6-of-6.
> * the "two independent sources" were neither independent nor about the same question.
> * the length series shares a start, so each window's frames are a superset of the last
>   one's and the max is non-decreasing **by construction** — and the 20 s→30 s tie inside my
>   own data is a counter-example to the claim I cited it for.
>
> What survives is smaller and worth stating plainly.

---

## What the guard is for

A window holding two people poisons a profile, and a poisoned profile cannot be cleaned —
only the contributing sample deleted. So before a window becomes an enrolment sample,
`window_is_homogeneous` cuts it into 5-second frames, embeds each, and refuses if any two
disagree by more than `DEFAULT_MAX_FRAME_SPREAD = 0.35`.

That is a sound thing to want. This spec is about the fact that it currently refuses
everything.

---

## What is actually established

**1. Enrolment is blocked on real audio.** Six windows from one TEST session, all refused,
smallest spread 0.551 against a 0.35 limit. Every enrolment attempted tonight failed.

**2. Five disjoint 10-second windows of one recording** — same frame count, so the frame
count is held constant:

| window | spread | verdict | per-frame dBFS |
|---|---|---|---|
| 5–15 s | 0.546 | mixed | −38.7, −41.3 |
| 15–25 s | 0.805 | mixed | −35.4, −47.6 |
| 25–35 s | 0.434 | mixed | −47.5, −45.8 |
| 35–45 s | 0.367 | mixed | −25.0, −68.5 |
| **45–55 s** | **0.123** | **homogeneous** | **−67.2, −56.9** |

**3. The one window it accepted is the quietest.** −67 and −57 dBFS against a measured
median of −36 for speech on these devices: roughly 30 dB down, a pause.

That is **one observation with no established mechanism**, and the first draft was wrong to
build on it. The 25–35 s window is also at the noise floor (1.7 dB apart) and was refused, so
"quiet frames agree with each other" is one for and one against.

It is still the observation that matters most, because of which direction it points: the
input the guard exists to refuse is the one input it let through.

---

## What is NOT established

- **Why the speech-bearing windows are refused.** −25 to −47 dBFS frames disagreeing by
  0.37–0.81 is the measurement; the cause is unknown.
- **That the frames not all being speech explains it.** Plausible, unproven, and the fix it
  implies cannot unblock enrolment even if true.
- **That the statistic's growth with window length is a real effect here.** It must be true
  in principle (a max over more pairs), but the series offered as evidence cannot show it:
  the windows share a start, so each frame set is a superset of the last and the max cannot
  decrease. The 20 s→30 s pair added nine comparisons and moved nothing.
- **That the transcriber or our clustering disagrees with the guard.** Neither is a second
  opinion. `TRANSCRIBE_WHOLE_CHUNK` makes a 113.6 s single turn the *configured* output shape
  regardless of speaker count, and `propagation: 11 turns in 1 voices` covers the whole
  session at τ = 0.85 using **whole-turn** embeddings, which `embed_audio` produces by
  averaging 45-second pieces — deliberately smoothing away the very frame-to-frame variation
  `frame_spread` measures.

---

## Why the threshold must not simply be raised

Raising it accepts **more**, and the one thing currently accepted is a near-silent window.
The errors run in both directions at once, and loosening only worsens the direction that
cannot be undone: a wrongly-refused enrolment costs a retry, a wrongly-accepted one poisons a
profile permanently.

`0.35` was measured once, on read speech. It is not obviously wrong for what it was measured
on; it is being asked a question the statistic may not be able to answer on this audio.

---

## The work, in the order the evidence supports

### A. Gate the frames on speech — for correctness, not to unblock

A frame with no speech is not evidence about who is talking, and the guard currently treats
it as if it were. This stops the one failure that cannot be undone.

**It will not unblock enrolment**, and the spec must not pretend otherwise: it can only
remove frames, and removing frames cannot lower a max below the pairs that remain.

Three things the first draft got wrong about how:

- **It goes in `_frames`, nowhere else.** Four call sites build frames independently
  (`op=enrol`, `_admit_harvest`, `_from_request_artifact`, `_spread`). Gating at the enrolment
  sites only would leave `op=spread` ungated — which is precisely how this spec's own live
  check is run, so the check would report "the gate is not gating" while it gated everywhere
  that writes.
- **It cannot go in `voiceprint_utils`.** That module's contract is pure numpy, no ONNX, "not
  even lazily, because every Lambda imports this module" — and `window_is_homogeneous`
  receives embeddings, never audio, so it could not gate anything in principle.
- **The "fewer than two speech frames → None" rule already exists** (`voiceprint_utils.py`,
  `if len(frames) < 2: return None`). Gating before embedding makes a pure-noise window
  arrive as zero or one frame and the existing rule fires. There is nothing to add.

The silero VAD **is** reachable from this function: it already carries `VadLayerArn`, holds
`s3:GetObject` on `models/*`, and both stacks resolve to the same bucket, with 1769 MB and
600 s leaving room for a second small ONNX session.

**And the test fixtures must move first.** Every audio fixture in the speaker-embed suite is
digital zero. Any gate drops 100 % of their frames, so the tests asserting a *successful*
outcome would fail — and the suite could not tell "the gate works" from "the fixtures are
silent". Either the gate is stubbable or the fixtures gain signal, before the gate lands.

### B. Then measure, on gated frames, against labelled windows

Windows known to hold one voice and windows known to hold two. Until that exists, every
number here is inherited rather than chosen.

This is the step that can unblock enrolment, and it needs data that does not exist yet.

### C. Only then, consider the statistic

A max over all pairs grows with the number of pairs. A quantile, or spread against the
window's own centroid, would not. But this is worth revisiting only after gating, since
gating may remove most of the variance that made length matter — and the evidence that
length matters *here* has not been produced.

---

## What must stay true

- **A window that cannot be judged is refused, never accepted.** The `None` path is the one
  clearly correct thing in the current implementation and it is load-bearing at all four
  consumers.
- **The guard stays.** Nothing here argues for removing it; the finding is that it is not
  doing its job in either direction.
- **No threshold moves without a measurement that includes two-voice windows.** A measurement
  made only on one-voice windows can only ever tell us how to accept more.

---

## Verification

| removed | must go red |
|---|---|
| the speech gate in `_frames` | a near-silent window is accepted |
| the gate applied before embedding | gated and ungated frames are compared together |
| fixtures with signal | the suite cannot distinguish a working gate from silent fixtures |

**One live check.** The 45–55 s window of
`ben_ucpk2_2026-08-13_18-10-00_sida2d5c…_c0000_bn4` currently returns `homogeneous` at 0.123
through `op: "spread"`. After A it must return **unjudgeable**. Because `op: "spread"` shares
`_frames` with the writing paths, that check is honest — it was not, in the first draft's
proposal.

---

## Related defect, already fixed

The review found that `embed_audio` discarded everything after its last whole 45-second
piece: a 113.6 s turn was embedded from its first 90 seconds, losing 21 % of the speech,
silently. Fixed in #523. It is not the cause of anything here — `frame_spread` uses `_frames`,
not `embed_audio` — but it was introduced tonight by the fix for a different problem, which is
the same shape of mistake this document is about.
