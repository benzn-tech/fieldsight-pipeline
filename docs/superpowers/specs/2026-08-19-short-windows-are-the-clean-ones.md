# Measured: the short windows are the clean ones, and the floor was the whole problem

**Date:** 2026-08-19. Read-only, against prod recordings.
**Supersedes the open question in** `2026-08-17-homogeneity-threshold-measured.md`.

---

## The finding

Sampling real prod segments with `op: "spread"` (read-only, scalars only):

| duration band | verdicts | frame spread min / med / max |
|---|---|---|
| 5–10 s (12 sampled of 192) | **10 homogeneous, 2 mixed** | 0.052 / **0.175** / 0.608 |
| 10–30 s (10 sampled of 71) | **10 mixed, 0 homogeneous** | 0.514 / **0.661** / 0.855 |

**Short windows pass the guard comfortably. Long windows fail it, every one.**

Not one verdict was `None`. A 5-second-plus window yields two frames, so it is judgeable — the
unjudgeable band is under 5 seconds, not under 10.

---

## What this overturns

**Three nights were spent on the theory that `DEFAULT_MAX_FRAME_SPREAD = 0.35` is
mis-calibrated.** It measured a real thing — enrolment refuses every window — and drew the
wrong conclusion from it, because every window it looked at was one the ten-second floor had
already selected.

The floor admits only 10–30 s windows. In a conversation, a window that long usually holds
more than one person. **So the only candidates enrolment ever saw were the ones most likely to
be mixed, and the guard was correctly refusing them.** The measurement that "one-voice windows
span 0.36–0.78 against a 0.35 limit" was measuring long windows and calling them one-voice on
the strength of a transcriber label.

The guard was not broken. The selection was.

---

## What it means for the design

`2026-08-18-enrolment-by-set-agreement.md` proposes dropping the floor from 10 s to 3 s. That
is right, and for a better reason than the spec gives: **short utterances are not merely more
numerous, they are cleaner.**

It also makes the change smaller than any draft of that spec proposed:

* **`DEFAULT_MAX_FRAME_SPREAD` does not move.** Not to 0.7, not anywhere. The 5–10 s material
  passes it at a median of 0.175 — half the limit.
* **The guard does not need relaxing to `refuse only on False`.** With a 3 s floor the 3–5 s
  band is unjudgeable and would need that rule, but the 5–10 s band — 192 segments in one
  session against 71 in the whole 10–30 s band — is judged, and judged clean. A 5 s floor
  costs little and keeps the guard's verdict meaningful on every candidate.
* What remains of the change is **one constant**, from `10.0` to `5.0`.

---

## The override is withdrawn

`TEST_VOICEPRINT_MAX_FRAME_SPREAD` was set to `0.7` earlier the same day to unblock enrolment
on TEST. **This measurement makes that actively harmful and it has been deleted.** At 0.7 the
10–30 s windows measured at 0.514–0.661 would all be admitted — they are the mixed ones. The
override would not have unblocked enrolment so much as filled the first profiles with
two-voice samples, permanently.

The knob stays; it is wired, tested and documented. Nothing should be set in it.

---

## What is still not known

* **Ground truth.** These verdicts come from the guard itself. That short windows score 0.175
  is consistent with one speaker and does not prove one speaker. What would settle it is
  listening to a handful of the 10–30 s windows judged mixed and confirming two voices are
  audible — a short listening task against a ranked list, not a research project.
* **Sample size.** 22 windows, one session, one device. A larger sweep across bands and
  sessions is running; this document records the finding, not the last word on it.
* **Level.** Frame dBFS ran −47.8 to −30.1, median −36.6 — the known quiet baseline, and
  comfortably above the −55 speech gate, so nothing here was dropped for silence.
