# The matching pool: how it is aggregated, how large it may be, and what defines it

Status: spec, 2026-08-13. Rewritten after adversarial review; what the review changed is
recorded at the end rather than quietly folded in.

**The first draft asked only "how many profiles can a pool hold". The review found a
defect one layer below that question, and it is now the first thing this spec addresses:
`decide_name` cannot correctly confirm anyone who has more than one enrolled sample.**

## 1. The aggregation gap — this blocks everything else

`profiles_for_matching` returns **one row per sample**, not per person
(`repositories/voiceprints.py:79-89` JOINs `speaker_voiceprint_samples` and selects
`s.embedding`). `decide_name` takes a flat `{name: score}` mapping and computes
`margin = best - ranked[1][1]` (`voiceprint_utils.py:92-100`). There is no notion of
"these two scores belong to the same person" anywhere, and **there are no callers** — the
aggregation rule has simply never been written.

The consequence is not hypothetical. Phase 0 enrolled **six** profiles for five people:
Ben has an English and a Chinese profile, roughly 0.08 apart, and the results document
records that twice the Chinese one scored nearest of all. Fed to `decide_name` as two
independent keys, Ben's own second profile becomes his runner-up, the margin is ~0.08,
which is below the 0.15 threshold, and the answer is **`tentative`** — a person losing to
himself.

So **Phase 0's 31/32 is nearest-profile accuracy, not what this decision rule would
confirm.** Any experiment that reports "confirmed" must define the aggregation first or its
K=0 baseline is measuring the bug.

The rule has to be chosen, not assumed. Two candidates:

- **Max over a person's samples.** `score(person) = max(cosine(turn, s) for s in samples)`.
  Simple, and it is what "nearest profile" already implicitly did.
- **Mean of the top-k samples.** More robust to one bad enrolment, at the cost of diluting
  a genuinely matching sample.

**Recommended: max, and decided in `voiceprint_utils` rather than in each caller** — a rule
that lives in two callers is a rule that will diverge. `decide_name` should take
`{person_key: [scores…]}` or a pre-aggregated mapping produced by a function next to it, so
the aggregation is testable on its own and Phase 4/5 cannot each invent one.

An open question this raises, and it is a product question: **is a person's Chinese profile
the same person or a different one?** Same person for naming; possibly separate for the
"same voice again" grouping. It has to be answered before enrolment can create a second
profile for anyone.

## 2. Pool size — the original question, now correctly framed

Every turn is scored against every row `profiles_for_matching` returns, so the pool — not
the number of people in the room — is what the decision rule must survive. Two readings of
"we can identify 20 people" were in play; only the second is what the code does:

- *"a conversation of ≤20 people is attributable"* — not measured, and not what pool size
  means;
- *"the pool holds ≤20 people"* — this is the quantity that governs.

Phase 0 measured, on **six profiles / five voices** and 32 turns:

| | |
|---|---|
| same-person similarity | median **+0.463**, range **0.104 – 0.639** |
| cross-person similarity | median **+0.034**, range **−0.114 – 0.205** |
| turns attributed correctly (nearest profile) | **31 / 32** |

The medians are far apart; the ranges overlap. The first draft argued from that overlap
that the runner-up in a 100-profile pool "lands near +0.2". **That inference is withdrawn.**
The +0.205 maximum comes from ~142 heavily correlated scores drawn from only **five distinct
voices**, with non-wearer same-person evidence at n=3–4 turns each. A 100-profile pool
samples twenty times more voices than were ever measured, and a single genuinely
similar-sounding distractor moves the maximum arbitrarily. Five voices cannot tell you the
upper tail of a population of a hundred.

The mechanism is still real and still worth stating, without the fabricated number:
`second_best` is the maximum over the rest of the pool, the maximum of more draws is larger
in expectation, and the margin rule therefore gets harder to clear as the pool grows. **How
much harder is unknown and is what the experiment is for.**

One more correction to the first draft: it cited +0.104 as "the weakest true score", but
that is the 2.1 s turn the duration floor now excludes.

**Measured 2026-08-13, and it changes the outlook.** The rebuilt harness reports the
weakest **floor-eligible** same-person score as **+0.411** (n=23), against a cross-person
maximum of **+0.205**. After the duration floor, on this material, the two distributions
**do not overlap at all** — they are 0.2 apart, and the overlap the first draft argued from
was entirely produced by turns the shipped rule already refuses.

That is a far healthier picture than the spec assumed, and it does **not** settle the pool
question: five voices still cannot tell you where a hundred would put the runner-up. What it
does mean is that the headroom may be much larger than feared, and the experiment is worth
running to find the ceiling rather than to confirm a problem.

The same run gives the baseline the pool sweep needs: 6 profiles / 5 people →
nearest-profile **32/32**, `confirmed` **23 (72%)**, `tentative` **0**, `unknown` **9 (28%,
all below the duration floor)**, wrong-confident **0**.

## 3. The experiment — runnable, but not cheap

The first draft called this "cheap, because the harness exists". Three of its four premises
were wrong.

**The harness is `scripts/speaker_session_eval.py`**, not `speaker_phase0.py` — its
docstring says so and the results document credits it. `speaker_phase0.py segment` splits on
deliberate 4-second silence separators and cannot produce the 32 conversational turns.

**The material is partly gone and partly in the wrong format.** Verified:

| what | state |
|---|---|
| script ground truth | `scripts/fixtures/2026-08-11-blockv-scripts.json`, 4.9 KB — **present** |
| `fieldsight-vad-check/2026-08-11-blockV-script/GROUND_TRUTH.txt` | **a blank template**, never filled — 3.7 KB of underscores. Not a loss: the ground truth is the script, matched by line text |
| session audio | **present in prod S3**, 6 objects each for `318601b2…` and `b49627d7…` — must be re-downloaded |
| enrolments | `Dropbox/AI/Field_Sight/diarization/voiceprint/*.mp3` — **six files**, and both scripts read 16-bit PCM WAV only, so they need converting |

**The model is not a blocker.** `speaker_phase0.py` auto-downloads SpeechBrain's ECAPA
(~80 MB) from HuggingFace on first use under `uv run --with torch --with speechbrain`. It ran
on 2026-08-11, so the environment has worked once. Network required, nothing else. It is
**not** a Phase 3 deliverable — Phase 3 exports an ONNX copy for Lambda, which this
experiment does not need.

**The harness must be modified**, which the first draft did not say. It reports
nearest-profile accuracy and separability, and computes neither of the two outputs this spec
calls the point:

- the three-state `confirmed`/`tentative`/`unknown` split (needs `decide_name`, which the
  eval script does not call);
- wrong-confident count.

Two hazards inside it must be removed at the same time: predictions are matched by
**three-letter name prefix**, and any profile starting with `ben` is hard-coded to merge into
the wearer (`speaker_session_eval.py:145,152,156`). Distractor names colliding on a prefix
would silently corrupt the accuracy figures — a distractor called `benny` would be scored as
the wearer.

**Distractors from real material: attempted 2026-08-13, and it produced none.** Adding them
needs no code change (the eval globs `enrol/*.wav`), and the review estimated source (1)
would yield "about a dozen voices". It yielded **zero**.

Method and result, so nobody repeats it: eight candidate windows were pulled from six other
prod accounts (Sam_Yu, Neil_Blunden, James_Alcock, Jack_Gibson, Jarley_Trainor — one more,
David_Barillaro, is 8 kHz legacy audio and was excluded outright, the model needs 16 kHz).
Each was screened with `window_is_homogeneous`, the guard the enrolment path already uses
for this exact purpose. **All eight were rejected.** A site recording holds the wearer plus
whoever is nearby; a stretch that is provably one voice is not something these recordings
readily contain.

The control run matters as much: four of the six real enrolments **passed** the same screen,
so the screen discriminates rather than refusing everything.

**Consequence for this experiment: the pool-size curve cannot currently be measured on real
material.** The options are (a) public-corpus distractors, in which case *every* point above
6 is corpus-flattered rather than only the large ones, or (b) fix enrolment quality first
(§6) and try again. Neither is free, and the first draft's "cheap, because the harness
exists" was wrong in every particular.

### Method

Sweep the pool over {6, 12, 20, 50, 100} profiles, holding the 32 ground-truth turns fixed.
Report per pool size:

- accuracy (confirmed **and** correct);
- the three-state split — and note that a curve plotting only accuracy can look flat while
  the feature stops answering;
- **wrong-confident count**, tracked separately as the one that matters;
- the weakest floor-eligible same-person score (§2).

**Pre-registered predictions**, so the result cannot be read backwards:

1. `confirmed` share falls monotonically as the pool grows.
2. Wrong-confident stays at or near zero for **enrolled** speakers.

Prediction 2 is a **hypothesis, not a mechanism** — the first draft asserted it as fact and
that was wrong. Nothing in `decide_name` orders the failure modes: one impostor scoring 0.15
clear of everything else produces a confident wrong answer directly. If prediction 2 fails,
the finding is that **0.15 is not a safe margin**, and that matters more than the accuracy
curve.

### The case the method does not cover, named rather than hidden

Every one of the 32 turns belongs to an enrolled speaker. The failure that scoping trades
against is the **opposite** one: a speaker who is *not in the pool* — a visitor, a
subcontractor, someone from another site. There is no true profile to beat, so the nearest
impostor wins by default, and only the margin stands between that and a confident wrong
name. Tightening the pool makes this case **more** common, not less.

Measuring it needs a held-out speaker: enrol four of the five, score the fifth's turns, and
count how often the pool names them anyway. That is a second experiment on the same
material and it should run before any scoping decision is taken, because it measures the
cost side of the trade the first experiment only measures the benefit side of.

## 4. Scoping — and the hole in option B

**A. Company-wide (today).** One pool per company; degrades fastest.

**B. Site-scoped.** No schema change is needed — `speaker_voiceprints` has no site column
(`0038:22-34`), but `memberships(user_id, site_id)` exists (`0002_core_relational.sql:31-38`)
and a join reaches it.

**But the join only reaches profiles that have a `user_id`, and `user_id` is nullable by
design** — 0038's own comment says an unnamed recurring voice may hold a profile before
anyone names it, which is what makes "the same person again" visible. Under a naive
site-scoped join **every unnamed profile silently disappears from the pool**, killing the
feature the nullable column was created for. Admin/gm wearers may also hold no `memberships`
row at all.

So B carries a semantic decision that must be made explicitly: unnamed profiles are either
(a) always in the pool regardless of scope, (b) scoped by the site of the sessions they were
observed in — which needs a column or a join through samples' `s3_key` — or (c) excluded,
and the "same voice again" feature is dropped. **This is exactly the empty-list-means-no-filter
shape this codebase has been bitten by before**, and it must not be settled by whichever
query someone writes first.

**C. Session-participant-scoped.** Tightest and circular: attribution is what tells you who
is present.

**D. B with A as fallback**, capping the fallback result at `tentative`.

**Recommended: defer A-vs-B until both experiments have run**, and make one change now:
`profiles_for_matching` gains a scope argument. Verified free today — grep confirms only
tests reference it — and expensive once Phase 4 and Phase 5 both call it.

## 5. Consent, as the product has decided it

The owner's decision, recorded so it is not re-litigated as a defect: **ship first,
formalise later.** The mechanism stays, the responsibility moves.

- `profiles_for_matching` keeps `consent_at IS NOT NULL` (`voiceprints.py:85`, pinned by
  tests). **Do not remove that filter** — a filter is far easier to keep than to reinstate.
- **No code sets `consent_at` today. There is no function that creates a
  `speaker_voiceprints` row at all.** The first draft described operator-asserted consent as
  though it existed; it is a Phase 4 design intent and nothing more. When it is built,
  `consent_at` becomes an *operator's attestation* rather than a *subject's consent record* —
  deliberately weaker, and the weakening is the content of the decision.
- Standing risks, unchanged by that decision: a voiceprint is biometric information under NZ
  law; the subject is the person recorded, not the wearer or the employer; the vectors live in
  ap-southeast-2 (Sydney), outside New Zealand. Legal specifics are the owner's to confirm —
  **this document is not advice on them.**
- The risk that is engineering's: a confident wrong name is worse than a missing one, and an
  identity layer makes wrong names systematic rather than occasional. That is what the margin,
  the duration floor and the three-state output exist for — and §3's second experiment is how
  we find out whether they are enough.

*(A provenance correction: `Theo → Phil` was an ASR mishearing of a spoken name, not a
speaker-attribution failure. It is a neighbouring failure class, not evidence for this one,
and the first draft used it as though it were.)*

## What the review changed

Recorded because the corrections are the useful part:

1. **Found the aggregation gap** (§1) — a defect below the question the spec was asking, and
   the reason Phase 0's 31/32 does not transfer to `decide_name`.
2. Withdrew the "+0.2 upper tail" inference as unsupported by five voices.
3. Corrected five profiles → six, and named `speaker_session_eval.py` as the real harness.
4. Established what the material actually is: script fixture present, ground-truth sheet
   blank, session audio recoverable from prod, enrolments in the wrong format.
5. Demoted "tentative before wrong" from mechanism to pre-registered prediction.
6. Added the not-in-pool speaker case — the dominant confident-wrong route, and the one
   tighter scoping makes worse.
7. Exposed the `user_id IS NULL` hole in site scoping, and the prefix-match and `ben`-merge
   hazards inside the harness.

---

## 6. Enrolment quality — found while trying to run §3, and it outranks §2

Two of the six Phase 0 enrolments fail the homogeneity guard: `ben` (max frame spread
0.511) and `ben_chinese` (0.441), against a threshold of 0.35. The other four pass at
0.166–0.268. Since Ben is the wearer — the person who speaks most in every session — this
looked like the guard refusing the one profile that matters most.

**It is not the guard, and it is not the audio level.** Two wrong explanations were
eliminated by measurement before the right one was found:

**Rejected — "the recording is too quiet."** Ben's frames sit at −37 to −44 dBFS against
Zoe's −21 to −26, which matches this product's documented recording-level problem exactly.
So: amplify and re-measure. `ben_chinese` gained 7.1× (−42 → −25 dBFS, **louder than Zoe**)
and its spread moved from **0.441 to 0.441** — not one decimal place. Ben's likewise,
0.511 → 0.511.

The reason is in `cosine`'s own docstring: it is **loudness-invariant on purpose**, because
across 0–6 m the level moves ~20 dB and a score that tracked it would be measuring the
microphone. **The whole chain is immune to gain.** Normalisation cannot repair any
voiceprint metric, and any future proposal to try it can be answered with this measurement.

**Rejected — "the threshold is wrong for near-field speech."** A spec to make the guard
distinguish "too quiet" from "two voices" was drafted and abandoned; it rested on the
explanation above.

**The actual cause, from a second engine rather than more inference** — transcribing the
enrolments (the T2 step in this project's own free-methods-then-T1-then-T2-then-ears order):

- `ben.wav` is **the blockV script being performed**: *"Morning, Zoe. Let's start with the
  level three slab. Is your rebar finished?"* / *"Almost. It's the side top opening trim bar
  not down."* / *"Right. I'll raise an RFI…"* — **one person playing several parts**;
- `ben_chinese.wav` is a Chinese statement that ends *"Finish, finish, finish。结束，结束，
  结束。"* — **switching language mid-recording**;
- Zoe's is a flat self-introduction, which is what an enrolment should be.

All three transcribe as a single `spk_0`, so no second person is present. **The guard was
reporting something true — acoustic properties changing sharply — and attributing it to the
wrong cause.**

### What follows

- **Phase 4's enrolment flow needs instruction copy, not a code change**: an enrolment must
  be *natural continuous speech*, not a performance, and must not change language partway.
  Zoe's recording is the model.
- **Ben's two enrolments are not usable as voiceprint samples.** This also explains the Phase
  0 note that his Chinese profile twice scored nearest of all: that profile is a blend, not a
  stable rendering of one voice.
- **It weakens the example in the aggregation change (#412), not the change.** `decide_name`
  genuinely needs per-person aggregation. But the 0.08 gap between Ben's two profiles is the
  distance between two unfit recordings, not the natural distance between one person's two
  languages, and it should not be quoted as the latter.
- The same guard is what blocked every real distractor in §3. Better enrolment material would
  unblock both.
