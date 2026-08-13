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
that is the 2.1 s turn the duration floor now excludes. **The weakest floor-eligible
same-person score is not reported in either document** and should be extracted when the
experiment runs — it is the number the margin actually has to clear.

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

**Distractors cannot honestly reach K=95.** Adding them needs no code change (the eval globs
`enrol/*.wav`), but source (1) — other real sessions on the same device — is limited by a
~20-device fleet and a small user base, and each distractor needs a clean single-voice
stretch hand-cut from unlabelled conversation. Realistically source (1) yields around a
dozen voices. **The 50 and 100 points will be almost entirely public-corpus voices**, which
are cleaner than site audio and therefore easier to beat. Those points must be labelled
corpus-flattered in the result, or the curve is optimistic exactly where it matters.

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
