# The homogeneity threshold: why my measurement does not support moving it

**Status:** analysis, **conclusion withdrawn**. No constant changes.
**Date:** 2026-08-17

> **This document first proposed 0.35 → 0.70 and presented a measurement to justify it.**
> Adversarial review found three structural flaws, each verified independently before this
> rewrite. The proposal is withdrawn. What follows is what the data actually supports, which
> is less, and what a measurement that would support it has to do differently.

---

## The three flaws, in the order they matter

### 1. The "independent confirmation" was the same statistic, thresholded

I discarded the highest one-voice window (0.957) on the grounds that clustering had
*independently* found two voices in it. `frame_statistics` derives `clusters` from
`cluster_turns`, which is complete linkage — and complete linkage's merge criterion **is** the
max over all pairs, so it breaks exactly when that max exceeds `DEFAULT_CLUSTER_TAU = 0.85`.

Verified over 500 random frame sets: `clusters == 1` is **identical** to
`pair_max <= 0.85`. Zero counter-examples.

So the removal was not corroborated by anything. It was "discard the largest value, using a
cut on the very statistic being calibrated" — the sample-fitting this document claimed to be
avoiding — and the one-voice ceiling of 0.777 that the whole argument rests on is a product
of that removal.

### 2. The two classes were sampled with systematically different frame counts

`pair_max` is a maximum over **pairs**, and the number of pairs grows quadratically with
frames. The two sets did not have the same number:

| set | n | frames per window |
|---|---|---|
| one voice (user-attested) | 13 | **3 in all 13** → 3 pairs |
| one voice (label-selected) | 11 | mostly 4 |
| two voices | 16 | **4 in 15 of 16** → 6 pairs |

A max over six draws is larger than a max over three whatever is speaking. The apparent
separation — one-voice ceiling 0.777, two-voice floor 0.727 — is inflated by the sampling
design in exactly the direction that made it look like a threshold existed.

### 3. The one-voice class is one voice, not two

The two sources were `Ben_UCPK` and `Ben_UCPK2`, whose emails are
`benlin.chch+ucpk@gmail.com` and `benlin.chch+ucpk2@gmail.com` — **one person, two accounts**.
"23 windows, two people, four dates" was wrong on the part that mattered: a one-voice class
containing one speaker says nothing about how the statistic behaves on anyone else.

---

## A consequence I had not modelled at all

Raising the threshold does not only admit enrolment windows. `_admit_harvest` is gated on the
anchor passing this same guard, so while the guard refuses everything, **harvest has never
run on real audio**. Relaxing the threshold switches it on, and each correction may then store
up to `ENROL_MAX_SAMPLES = 6` further samples — machine-selected cluster members, screened by
the same relaxed guard.

The cost of a wrong acceptance is therefore up to seven permanent samples, not one. My
verification plan exercised none of that.

---

## What the data does still support

- **0.35 is very likely far too strict.** Every one-voice window measured — under any
  sampling — sits between 0.36 and 0.78, and the limit is 0.35. Enrolment fails 100 % of the
  time, and that is not explained by the audio holding two people.
- **The alternatives are not better.** `centroid_max` and `centroid_mean` matched `pair_max`
  window for window. There is no case for changing the statistic.
- **No number is justified yet.** Not 0.70, not 0.727, not anything: three of the inputs to
  that arithmetic are broken.

---

## What the guard does and why enrolment is blocked

Before a window becomes an enrolment sample, it is cut into 5-second frames, each embedded,
and refused if any two frames disagree by more than `DEFAULT_MAX_FRAME_SPREAD = 0.35`. A
window holding two people would poison a profile, and a poisoned profile cannot be cleaned —
only the contributing sample deleted, after somebody notices, which nothing prompts them to
do.

The guard currently refuses **every** window of real site audio. No voiceprint can be created.

---

## The measurement, as it was actually taken

Kept because the numbers are real and the next attempt should not re-derive them — but read
with the three flaws above, which apply to every row.

**One voice — 23 windows, one person (two accounts), four dates.** 13 from a session the user
states they recorded alone (`Ben_UCPK2`, 2026-08-12, all exactly 3 frames); 10 selected
because the transcriber emitted a single speaker label (`Ben_UCPK`, 2026-07-30 / 08-06 /
08-07), after discarding one window — a discard that flaw 1 shows was circular.

**Two voices — 16 windows** where two labels demonstrably alternate (`Ben_UCPK2`, 2026-08-13),
15 of them 4 frames.

```
one voice   0.360 … 0.663  0.715  0.730  0.777
two voices                 0.727  0.733  0.746 … 1.031
```

| statistic | one-voice max | two-voice min | one-voice pass rate at the two-voice floor |
|---|---|---|---|
| `pair_max` (current) | 0.777 | 0.727 | 21 / 23 |
| `pair_median` | 0.713 | 0.575 | 19 / 23 |
| `centroid_max` | 0.350 | 0.296 | 21 / 23 |
| `centroid_mean` | 0.267 | 0.225 | 21 / 23 |

**The classes do not separate**: the one-voice maximum (0.777) is above the two-voice minimum
(0.727), so three of the 39 windows sit on the wrong side of any single line. An earlier draft
of this document announced that they *did* separate. They do not, by the criterion the
comparison script itself uses, and the near-miss it reports is the artifact described in
flaw 2.

A further boundary error worth recording: `window_is_homogeneous` accepts on `spread <=
max_spread`, so a limit set at exactly the two-voice minimum **accepts** that window. A
zero-accept limit is the largest value strictly below it, not the minimum itself.

---

## What a measurement that supports a threshold has to do

**Hold the frame count fixed.** Every window in both classes must yield the same number of
frames, or normalise the statistic by pair count. Otherwise the comparison measures window
length.

**Label the classes without using the statistic.** Speaker count must come from something
outside this pipeline — a person listening, or a session somebody attests to — never from
`clusters`, which is `pair_max` in disguise, and never from the provider's diarisation alone,
which this repository has recorded getting speaker counts wrong.

**More than one speaker in the one-voice class.** At least three people, ideally on more than
one device. A threshold fitted to one person's voice is fitted to that person.

**Exercise harvest in the verification.** A threshold change switches on a path that has
never run in production; a plan that checks only "an enrolment succeeds" checks a fraction of
what changed.

**Report the frame count with every window.** `op: "spread"` already returns it. Not
reporting it is how flaw 2 survived a whole evening of measuring.

---

## What I am doing instead

Nothing to the constant. The measurement stands as a record of what was tried and why it does
not conclude, and `scripts/compare_homogeneity_statistics.py` keeps working — it is the
sampling and the labels that were wrong, not the machinery.

Enrolment stays blocked. That is the honest state: a guard calibrated on read speech is
refusing site audio, I can see that it is probably wrong, and I cannot yet say what right is.

---

## Verification of this document's own claims

| claim | how it was checked |
|---|---|
| `clusters` is not independent of `pair_max` | 500 random frame sets, zero counter-examples |
| frame counts differ between classes | `frames` is returned per window and was tallied |
| the one-voice class is one speaker | the two folders' email addresses |
| harvest has never run | it is gated on the anchor passing a guard that refuses everything |
