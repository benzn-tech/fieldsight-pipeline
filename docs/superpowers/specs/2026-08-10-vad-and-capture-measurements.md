# Why the VAD and capture settings are what they are

**Date:** 2026-08-10 · **Status:** measurement record, no code change proposed
**Read this before retuning `VAD_THRESHOLD`, `MERGE_GAP`, `MIN_SPEECH_DURATION`,
`NORMALISE_AUDIO`, `DROP_SILENT_CHUNKS`, or before costing a microphone array.**

Four plausible improvements were measured today. **All four failed**, each for a different
and non-obvious reason. The settings currently in production are the ones that survived.
The point of this document is that the next person to have any of these ideas can see the
data instead of re-running the day.

---

## The problem that started it

A speaker 4 m and 6 m from a chest-worn device produces chunks the pipeline discards
entirely: VAD finds no speech, and `DROP_SILENT_CHUNKS=true` drops them without a
Transcribe job. Measured on a purpose-recorded distance ladder (one continuous take, one
room, the same four sentences read at 0 / 0.5 / 2 / 4 / 6 m).

---

## 1. Level does not encode distance beyond half a metre

Speech level, gated to speech frames, on the untouched chunks:

| position | level | step |
|---|---|---|
| worn (0 m) | −27.0 dBFS | — |
| 0.5 m | −33.8 | **−19.6 dB** (worn → 0.5 m, measured separately at −33.2 vs −52.8 on gated frames) |
| 2 m | −41.8 | −3.4 |
| 4 m | −43.2 | −2.9 |
| 6 m | −49.8 | −2.1 |

**One step is worth more than the other three combined.** Past roughly half a metre the
microphone is hearing mostly the reverberant field, whose strength is nearly uniform in a
room, so level stops tracking distance.

Consequences, both confirmed separately:

- **"Loudest turn = the wearer" does not work.** On a real two-person conversation with
  per-turn ground truth from the wearer: 12.6 dB median separation but complete range
  overlap, best achievable accuracy 77% *with the threshold fitted on the same data*. In
  **two of four chunks the person at 2 m was louder than the wearer.** Even the relative
  rule is a coin flip.
- **The earlier explanation for that failure was wrong.** It was attributed to the rear
  microphone pointing at the wearer's body (`orientation 0,0,−1`). A worn-versus-placed
  control measured the wearer **19.6 dB louder** than a talker at 0.5 m — proximity wins
  easily. The real cause is within-speaker variance in conversation (head turns, effort)
  swamping a mean difference that only exists at the first step.

---

## 2. The VAD parameters are already at their optimum — measured, not assumed

45 configurations swept (`threshold` × `merge_gap` × `min_duration`) using the **shipping**
Silero model pulled from `s3://…/models/silero_vad.onnx` and `merge_close_segments` copied
verbatim from `lambda_vad.py`. Scored against material with known answers: the 4 m and 6 m
blocks must produce segments, and four stretches of walking silence must produce none.

**Three configurations passed. All three are `threshold=0.15, min_duration=1.0` — what
production runs.** Every threshold at or below 0.10 started emitting segments on silence.

The reason there is no headroom:

| segment | max probability | windows ≥ 0.15 |
|---|---|---|
| 6 m, real speech | 0.817 | **3.8%** |
| walking, nobody speaking | 0.735 | **9.6%** |

**Silence scores higher than distant speech.** Any threshold low enough to admit 6 m admits
walking noise first. The knob has no discrimination left on this hardware.

Note also that the retry threshold is `VAD_THRESHOLD / 2`, derived, not the old hardcoded
0.25 — which had become *stricter* than the primary once both environments moved to 0.2 and
could therefore never succeed.

---

## 3. Enhancing before the VAD gate: passes the gate, fabricates the content

The one software lever left was to move the existing filter chain (`acompressor` 4:1 +
`loudnorm`, currently applied *after* the gate) to *before* it.

It works, spectacularly, on the gate:

| chunk | raw max prob | segments | enhanced max prob | segments |
|---|---|---|---|---|
| 2 m | 0.603 | 1 | 0.994 | 3 |
| **4 m** | **0.276** | **0 (dropped)** | **0.964** | **2** |
| **6 m** | **0.200** | **0 (dropped)** | **0.977** | **2** |

And then the transcripts are invented. What was said at every distance:
*今天要去 Mitre 10 买木头和螺丝，明天早上八点在工地碰面*.

| | 4 m | 6 m |
|---|---|---|
| raw | 「今天要去**拜访特码，你和我一起。听说他爸在他那边**」 | "Today our team Mitre 10 **buying bulk of the loose**" |
| enhanced | 「今天要去**买的特卖，那个都是。因此我买了一张机票**」 | "The team headed to Mitre 10 **by Bunnings or the West**" |

**Both wrong, differently wrong.** Enhancement raised the volume; it did not tell the VAD
*which eight seconds of the thirty are speech*. The gate then said "speech everywhere", and
`TRANSCRIBE_WHOLE_CHUNK=true` sent all thirty seconds — mostly footsteps and room — to ASR,
which turned them into fluent sentences.

**Rejected. It converts missing data into false data, and the pipeline downstream cannot
tell the difference.** Missing is recoverable; fabricated is not.

The same argument rejects `DROP_SILENT_CHUNKS=false`, which sends exactly that raw chunk.

### What DOES survive: cut segments transcribe fine

Cut to speech boundaries and sent on their own, the same 6 m audio came back nearly
correct — Mitre 10, 八点, 工地碰面 all present, 5/5 checkpoints at every distance. **The
problem was never the acoustics or the ASR. It is that nothing in the pipeline can find the
speech boundaries in a chunk the VAD has already judged empty.**

---

## 4. A microphone array is not worth buying

The board does give two genuinely independent channels — confirmed three ways: inter-channel
delay of ±4–5 samples at 16 kHz (0.28 ms ≈ **9.6 cm**, a real spacing), the delay *and* the
level difference both flipping sign when the requested microphone flips, and correlation of
0.89 rather than 1.0.

It buys almost nothing:

| distance | single channel SNR | two-mic delay-and-sum | gain |
|---|---|---|---|
| 0 m worn | +13.8 dB | +13.6 | −0.1 |
| 0.5 m | +7.0 | +7.4 | +0.4 |
| 2 m | −1.0 | +1.4 | +2.4 |
| 4 m | −2.4 | −2.1 | +0.3 |
| 6 m | −9.0 | −6.4 | +2.6 |

**−0.1 to +2.6 dB, with no pattern.** The reason is one number:

> **the noise's own inter-channel correlation is 0.939**

Beamforming works by cancelling noise that is *incoherent* between microphones. This
device's dominant noise is clothing friction **on the device itself** — a near-field source
a few centimetres from both microphones, so it is almost perfectly coherent and summing
cannot touch it. Measured previously: speech at −38.0 dBFS against friction at −34.6 dBFS,
a negative SNR before distance is even considered.

Extrapolating `10·log10(N)`: 4 mics = 6.0 dB, 6 mics = 7.8 dB. **6 m needs 9 dB just to
reach zero.** An array does not close it.

And beyond ~4 m the array has nothing to steer by: correlation falls to 0.41–0.43 and the
best lag collapses to 0 or ±1 samples, because the direct path is buried in reverberation
and reverberation has no consistent delay.

**One earlier objection was overstated and should be dropped:** the "15 dB part-to-part
microphone mismatch" is variation *between boards*. Within this unit the two channels differ
by **3.3–3.8 dB**, which is fine for array work. The array fails for the reasons above, not
that one.

Also: **do stereo at 16 kHz, never 44.1 kHz.** The 44.1 kHz stereo take returned channels
correlating 0.160 and 14.8 dB apart against ~0.89 / ~3.8 dB for all three 16 kHz takes —
the same "reports healthy, does not do the work" shape this board showed for NS/AGC at
44.1 kHz.

---

## 4b. Which microphone — and a wrong conclusion that looked convincing

**Measured 2026-08-11, worn throughout, with a second person reading at 1 / 3 / 6 m.**

§4 above reported, in passing, that the two channels differed by 7.1 dB and 7.4 dB of SNR at
2 m and 6 m, and suggested `setPreferredDevice` might buy more than a whole microphone array
for the cost of one line. **That was wrong, and it was wrong because the channels were
labelled by assumption.**

### The labelling error

Block E's take reported `routedDeviceAddress = bottom`, and the analysis took that to mean
"channel L is the bottom mic". **A routing report names the primary device; it says nothing
about channel order in a two-channel capture.** Block D had already produced the warning —
the inter-channel level difference *flipped sign* between a FRONT and a BACK request, so the
mapping is not fixed — and that observation was written down and then not applied to the very
table it invalidated.

Block W settles it physically. The wearer taps beside each microphone in turn:

| tapped | median L−R |
|---|---|
| bottom edge | +2.8 dB (channels comparable) |
| **back of the case** | **+17.9 dB** (range +15.2 … +27.2) |

Tapping beside the back microphone drove L to full scale while R stayed at 1.4–5.7k. **L is
the back microphone; R is the bottom one** — the opposite of what §4's table assumed. The
bottom taps do not discriminate (structure-borne through the case reaches both), so the
identification rests on the back taps alone, which is enough.

The advantage therefore belongs to the **bottom** microphone — which is what the platform
already routes to by default, and what production already records. **It was never an
available gain; it was a gain already being taken.**

### What the worn measurement actually shows

SNR is against the **friction** floor, which is the noise that limits this device, not room
tone:

| | back mic | **bottom mic** | bottom advantage |
|---|---|---|---|
| wearer speaking | +4.9 dB | **+14.6 dB** | **+9.6** |
| other @ 1 m | −8.5 | **+0.3** | **+8.8** |
| other @ 3 m | −10.8 | **−0.9** | **+9.9** |
| other @ 6 m | −10.1 | **−7.1** | +3.0 |

And the reason, which is the useful part:

| | back mic | bottom mic | difference |
|---|---|---|---|
| room quiet | −33.3 | −50.2 | **back is 16.9 dB noisier** |
| friction, no speech | −34.8 | −44.3 | **back is 9.5 dB noisier** |
| speech, any distance | — | — | within ±0.7 dB |

**Both microphones hear the speech equally well. The back one is buried under its own
noise.** It faces the wearer's body (`orientation 0,0,−1`), which does nothing for pickup and
is extremely efficient at collecting clothing and body movement.

At 6 m the bottom advantage narrows to +3.0 dB because the speech has fallen to the noise on
both channels — consistent with 6 m being outside this device's range regardless of
microphone.

### What this changes

**Selecting a microphone is closed** — production is already on the better one.

But the noise source is now *located*: it is the body-facing side, 16.9 dB above the other
channel in a quiet room. That points at **mechanical decoupling and wear position** rather
than a windscreen, and it turns "reduce friction by 6 dB" from a vague goal into a specific
surface to work on.

## 5. Two things that are safe, and one that is not

**Safe — enhancement does not damage diarization.** The worry that compression would flatten
the near/far level cue and cost speaker separation was measured on a real two-person
conversation with crossing and overlapping speech: raw 282 words / 3 labels / 22 turns
versus enhanced 277 / 3 / 22. **Identical turn structure.** The current order (normalise
*after* the gate) is not hurting anything.

**Safe — ElevenLabs does not invent words on pure noise.** Four stretches of walking silence
returned `[pause]`, `[clicking]`, `[background noise]`, and one that turned out to contain a
real spoken word. Zero fabricated words. This is materially different from AWS Transcribe,
which invented 10.7% of words from silence — the finding that originally justified
`DROP_SILENT_CHUNKS=true`. **That justification no longer applies to the provider we run.**

**Not safe — EL *does* fabricate on raw chunks that are mostly non-speech.** §3 above. The
distinction is not "silence versus noise", it is **"a clip that is mostly speech" versus "a
30-second chunk with a few seconds of distant speech in it"**. The first is fine; the second
invents.

---

## 6. What is actually left

Only one lever changes the input quality by a useful amount:

> **worn +13.8 dB versus 6 m −9.0 dB = a 22.8 dB difference.**

That is more than double what a six-microphone array could give, and it is already built:
group merge is implemented and sitting behind `ENABLE_GROUP_MERGE=false`. **A distant talker
wearing their own device stops being a distant talker.**

Second, cheaper than any of this: **reduce the friction noise** (a windscreen, mechanical
decoupling, a different wear position). Six decibels off a −34.6 dBFS noise floor is worth
the same as adding four microphones, at two orders of magnitude less cost, and it improves
every distance rather than only the far ones.

Third, nearly free and honest: **record that a dropped chunk was dropped**. The sidecar
already survives the drop. A report that says "there was conversation here, too far to
transcribe" is worth more than one that silently omits it — and it is the only way anyone
downstream can tell *silent* from *lost*.

---

## Reproducing any of this

The recordings are on the device probe (`AudioProbeActivity`, blocks D and E on the
GrandTime branch `feat/dual-mic-probe`) and the analysis scripts are alongside it
(`tools/dual_mic_analysis.py`). The distance-ladder audio and A/B enhancement pairs are in
`Dropbox/fieldsight-vad-check/2026-08-10-*`.

The parameter sweep is pinned as a unit test — see
`tests/unit/test_vad_tuning_rationale.py`. If you change a VAD default, that test tells you
which measurement you are contradicting.
