# ASR accuracy — what actually moves it, measured (2026-08-29)

**Status:** measurement complete, nothing shipped. Three of the four levers below are
decided by evidence; the fourth (voiceprint re-bind) is a prototype waiting to be built.
**Updated 2026-08-30** with §5, a head-to-head against Qwen-Audio-3.0.

**Material:** one real meeting — `Ben_UCPK2` / `2026-08-27` / `sid93396a6ac8434fdf908c25a50cc7e167`,
11:06:35–11:33 NZ, 26 m 36 s, 16 kHz mono, 56 chunks. Mandarin + English, three people.
**Ground truth for speakers came from the user's ears**, not from any model:
`spk_0 = Benny`, `spk_1 / spk_2 / spk_4 = Ben`, `spk_3 = Isaac`.

Runs compared (all ElevenLabs `scribe_v2`, `num_speakers=5`, same audio bytes):

| id | what | ASR wall | words | coverage | >8 s holes |
|----|------|---------|-------|----------|-----------|
| run 1 | prod batching, 14 calls (13 batches + 1 unbatched tail) | 125 s summed | 6861 | 1436 s | 3 |
| run 2 | whole session, 1 call, 130 keyterms | 87.7 s | 6750 | **1398 s** | **6** |
| run 2′ | **identical config to run 2, re-run** | 86.5 s | 6913 | 1436 s | 4 |
| run 3 | whole session, 135 keyterms (+5 proper nouns) | 85.8 s | 6853 | 1441 s | 5 |

Reproduce with `tools/asr-eval/` (see its README).

---

## The four levers, ranked by measured effect

### 1. Keyterms — the only lever that reliably fixes proper nouns. USE IT.

A per-word allowlist. It fixes exactly the words you put in it and **nothing else**.

| term | run 1 | run 2 | run 2′ | run 3 (+terms) | previously heard as |
|------|-------|-------|--------|----------------|---------------------|
| `Plaud` | 0 | 0 | 0 | **9** | "Pro" — all 9 sites |
| `Southbase` | 0 | 0 | 0 | **10** | "Soundbase" / "SalesBase" / "Sales" |
| `VizField` | 0 | 0 | 0 | **1** (+2 run-on forms) | "VC Field" / "Visual Field" |
| `DeanDre` | 0 | 0 | 0 | 0 → emitted **"DeonDre"** | partial: 1 site, not the given spelling |
| `VisioField` | 0 | 0 | 1 | 0 | almost certainly never said |
| *`Lindis Pass`* (**control, deliberately not added**) | "Lindes Pass" | "Linda's pass" | — | **"where it's passed"** | got *worse* |

**Rules this establishes:**

- **No spillover.** Adding 5 proper nouns does not make the model more careful about proper
  nouns in general. The control term degraded. Treat keyterms as a whitelist you must
  populate deliberately, not a "be more careful" switch.
- **Zero false positives in this round** — but read the scope: only the sites where the five
  target terms appeared were checked one by one. No full-transcript comparison was done.
- **One sentence can need two entries.** At 06:47 (`let's say I work ___, I go to ___`)
  run 1 got the second name right and the first wrong; run 3 got the first right and the
  second wrong. **No run got the whole sentence.** The question is never "which run is
  better", it is "which words are in the list".

**Action:** keep feeding `config/custom_vocabulary_construction_nz.txt`. Confirmed queue
for the next round: `Lindis Pass`, `Naylor Love`. Project names, people, companies,
products — anything a customer would notice being wrong.

**Cost:** none. Same request, same billing. `MAX_KEYTERMS = 1000`, currently at 135.

---

### 2. Speaker identity — fix it with a local voiceprint, NOT with a second ASR pass.

Batching's speaker labels are close to worthless across a session:

| label | frames | purity vs the three real people |
|-------|--------|-------------------------------|
| batched `spk_0` | 2638 | **50.2 %** — Benny 1324 vs Ben 1311, an almost perfect coin flip |
| batched `spk_1` | 1944 | **62.5 %** |

The failure is **between** ASR calls, not inside them: within any one call the two runs
agree 89–100 % on who is speaking. `spk_0` means a different person in 6 of the 14 calls.

**Two different jobs — do not conflate them (this cost a round of confusion):**

| | (A) re-bind the namespace | (B) relabel every turn |
|---|---|---|
| decisions to make | **28** (14 calls × 2 labels) | **354** (one per turn) |
| evidence per decision | all long turns of that label in that call — 4.8–109 s, median ≈40 s | one turn; 226 of them are <3 s |
| judgeable material | **85.8 % by seconds**, 28/28 centroids built | 36 % **by turn count** |
| verdict | **works — purity 55.4 % → 96.3 %** | **not viable** |

*(The "only 36 % of turns are long enough" figure is by turn **count**. It rules out (B)
and says nothing about (A). Always state the denominator.)*

Re-bind result, average-linkage over the 28 centroids — **same 3 groups at thresholds
0.35 / 0.40 / 0.45**, and the grouping independently matches the frame-level flip pattern:

| group | frames | is | purity |
|-------|--------|----|--------|
| G1 | 2531 | Benny | 97.1 % |
| G0 | 1879 | Ben | 96.5 % |
| G2 | 172 | Isaac | 82.6 % |

**Cost comparison — this is the whole argument:**

| approach | extra vendor cost | extra latency after stop | result |
|----------|------------------|--------------------------|--------|
| today (batched ASR) | — | — | 55.4 % purity |
| second whole-session ASR at finalize | **+1 full ASR bill** | +87.7 s | one namespace |
| **local ECAPA re-bind at finalize** | **0** | **≈55 s CPU** (19.7× realtime, parallelisable) | **96.3 %** |

The model is `models/ecapa_tdnn.onnx` (192-d, 83 MB, already in S3, loaded the same way
VAD loads Silero). No per-minute billing.

**Why not spend on realtime ASR instead:** streaming has a *shorter* look-ahead than 2-minute
batching, so labels fragment more, not less. And realtime buys first-token latency, which is
not the bottleneck — of the ≈146 s from stop to summary, **134 s is the extraction LLM and
only 12 s is ASR**.

**Threshold guidance:** never ship a single tuned threshold. What makes a result trustworthy
is a *stable band* — here 0.35–0.45 all give the same answer. Turn-level relabelling has no
such band (same-person p10 = 0.403 vs different-person p90 = 0.417 cross at ≈0.41), which is
why `voiceprint_utils` uses a margin rule and not an absolute cut.

**The channel confound is refuted, not merely unaddressed.** The worry was that ECAPA
clusters *near-mic vs far-mic* rather than *person vs person*. A close-mic phone recording of
Ben scored **0.473** against far-field Ben and **0.058** against Benny — the wearer, the
closest voice in the room. If it were sorting by distance the close recording would have
matched the closest speaker. It did not.

**Also measured:** cross-language does not affect ECAPA — the same person's English and
Mandarin clips score **0.768**, higher than the same-person same-session median of 0.596.
ElevenLabs' diarization *is* affected, but the more likely trigger is a long silence: the two
points where Ben was split carry 18.3 s and 101.8 s gaps, and only one of them is also a
language switch.

**Blocked on, and it is not the maths:** the voiceprint library already exists and is deployed
(`speaker_voiceprints` / `speaker_voiceprint_samples`, migrations 0038 + 0042–0048,
`fieldsight-prod-speaker-embed`, `fieldsight-prod-voiceprint-writer`, `src/voiceprint_utils.py`).
It is empty because (a) there is no consent UI, and a profile with a null `consent_at` is coded
to never match — a voiceprint is biometric data under the NZ Privacy Act; and (b) the
homogeneity guard is still too strict.

> ### Correction, 2026-08-29 (later the same day): (b) was wrong, and it was the expensive
> ### kind of wrong
>
> The library was not empty because the guard is too strict. **The guard has never refused a
> window it was given fairly.** Three separate investigations measured the threshold, and all
> three were measuring the wrong thing:
>
> 1. **`lambda_voiceprint_writer` could not import numpy**, and had not been able to since
>    2026-08-14 (PR #499 added `import voiceprint_utils` to `_agreement` for one dot product;
>    that writer is in-VPC with the psycopg layer and nothing else). The embedder logged
>    `enrolment accepted` and the *storing* lambda then died. "Accepted" is printed by the half
>    that computes; persistence is a different function, and that line said nothing about
>    whether a row existed. Fixed in PR #595.
> 2. **The window handed to the guard was the whole turn.** A correction has to name a turn —
>    the propagation half matches on exact boundaries — and under batching a turn is a whole
>    chunk. So the same two numbers were asked to be a 109 s span for one consumer and a 10 s
>    span for the other. Enrolment now re-tests the tightest contiguous 10 s inside a window it
>    cannot accept whole (PRs #601–#603).
>
> **The first real sample landed at spread 0.231 against the unchanged 0.35 limit.** The
> threshold measured on 78 prod windows was right the whole time; what was wrong was the
> material being fed to it — which is exactly what
> `docs/superpowers/specs/2026-08-17-homogeneity-threshold-measured.md` concluded and what
> three nights of suspecting the number kept re-litigating.
>
> (a) still stands as a rule, but it does not block a subject who has an account: the live row
> carries `consent_basis = confirmed` with the subject's own uuid in `consented_by`, so it is
> not inert. What (a) blocks is the population the feature is actually for — subcontractors
> with no account — which is what `consent_basis = notice | attestation` was added for.
>
> **None of this changes the recommendation below.** The anonymous re-bind is still the
> unblocked path and still the one that produces the 96.3 %. It changes only the reason the
> named library was empty, which matters because "the guard is too strict" invites loosening a
> number that was correct.

**But session-scoped anonymous re-binding needs no consent** — nothing is stored, no
individual is identified. That is a separate, unblocked path, and it is the one that produces
the 96.3 %. **Ship the anonymous re-bind first; let the named library keep waiting for its
consent UI.** Bundling the two is what kept this blocked.

*(One shortcut is closed off: the device belongs to Ben but is often left on the table, so
mic proximity moves during a meeting. "The closest voice is the wearer" does not hold, so
"only enrol the wearer" is not available — identifying anyone means identifying everyone.)*

---

### 3. Whole-session ASR instead of batching — NOT worth it.

- Word-level difference is smaller than it looks: batched vs whole-session sits at
  90.1–90.7 %, and the **noise floor is 5.6 %**, so the real difference is ≈3.8 points.
- Latency gets worse, not better: stop → summary goes from ≈146 s to ≈211 s, because
  batching has already completed 13 of its 14 calls while the meeting is still running.
- It carries a failure mode batching does not: **run 2 silently swallowed ~25 s of real
  conversation** (23:20–23:45, emitting only "Yeah. Yeah. Yeah.") and looked completely
  normal. Coverage 1398 s vs 1436/1436/1441 s for the other three. A single run cannot
  detect this. When one batched call degrades, only that 2 minutes degrades.
- It does not fix diarization anyway: all three whole-session runs produced **5 labels for
  3 people**. Run 3 merged Ben from 3 labels to 2 but split Isaac from 1 to 2 — net zero.
  Runs 2 and 2′ produced *identical* splits, so "Ben gets cut into three" is stable
  behaviour, not jitter.

**Keep batching.**

---

### 4. VAD — already near-transparent on this material. Do not touch.

Raw 1731 s → VAD passed 1680 s (**97.1 %**) → 1596 s actually reached ASR (92.2 % of raw).

The 2.9 % dropped is two whole chunks at the very end (c0056, c0057) judged silent after
everyone had left. **All 56 other chunks have `speech_ratio` exactly 1.00** — there are no
intermediate values, because `TRANSCRIBE_WHOLE_CHUNK=true` / `emit_mode=whole_chunk` makes
the decision "is there any speech in these 30 s", not a within-sentence cut.

A separate 84 s is trimmed at batch seams (~2 s device overlap × 42 joins). That is
**de-duplication, not loss** — the 1731 s contains ~84 s of duplicated audio by design.

Unchanged old finding: pre-normalisation loudness median **−34.3 dBFS** (range −41.1 to
−29.0), against a normal −20…−12. The device still records systematically quiet.

---

### 5. Provider — Qwen-Audio-3.0 is faster and far more deterministic, but loses English. STAY ON ELEVENLABS.

`qwen-audio-3.0-asr-flash-filetrans` on the same 1596 s file, 7 runs (same-config re-run,
a no-vocabulary control, `speaker_count=3`, and three hotword configurations).

| | ElevenLabs `scribe_v2` | Qwen-Audio-3.0 filetrans |
|---|---|---|
| whole-session latency | 85.8 / 86.5 / 87.7 s (mean 86.7) | **47.0–54.3 s (mean 50.7, −41 %)** |
| same-config re-run similarity (**noise floor**) | 94.5 % | **99.2 % — near deterministic** |
| CJK characters | 5147–5176 | **5301** |
| English letters (spaces stripped) | 3982–4554 | **3383 (−22…−26 %)** |
| hotwords | **3 of 5 terms fixed, 0 false positives** | **0 of 5, in 4 configurations** |
| labels for the 3 real people | 5 (Ben×2–3) | 5 (Ben×2, Benny×2) / 3 when `speaker_count=3` |
| label purity vs the three names | 97.1–98.2 % | 90.6–93.1 % |
| **Ben recall via his enrolled voiceprint** | **99.4 % (EL-3), precision 96.4 %** | **62.9 % (sc=5) / 73.7 % (sc=3)** |

**The 99.2 % noise floor is a real advantage** — on Qwen any difference above ~1 % is
signal, where ElevenLabs needs 5.6 % before jitter can be ruled out. Worth remembering for
any future A/B.

**Qwen's hotwords produced no measurable effect in any configuration tested:** inline
`vocabulary` dict at weight 4; precompiled `vocabulary_id` at weight 4; the same at
weight 50 ("super hotword"); and a probe list built from a term Qwen already emits. The
probe is what makes this a controlled result rather than an absence of evidence — the
no-vocabulary run QW-D is the baseline, and every hotword run matches it exactly:

| probe | QW-D (no hotwords) | every hotword run |
|---|---|---|
| `Plaud` / `Southbase` / `VizField` | 0 | 0 |
| `"pro"` (Plaud's wrong form) | 13 | 13 |
| `工地` | 2 | 2 — *already present; the hotword earned nothing* |
| `naylor` | 2 | 2 … **except 0 in the run where it WAS the hotword** |

Adding `Naylor Love` as a weight-50 hotword made a term Qwen was getting right disappear.

**Qwen's one genuine lever ElevenLabs lacks: `speaker_count` is honoured.** Ask for 3 and
you get 3 labels; ElevenLabs returns 5 for `num_speakers=5` regardless. It did not help
here — with `speaker_count=3` the quietest person (Isaac, 47 s) was absorbed into Ben's
label, raising that label's contamination. **Constraining the count to the true number is
not the same as separating better**; it forces the minority speaker into the majority.

**Qwen also fragments English** — `let ' s say`, `n aylor love`, `hab its`, `ag gressive`,
all lowercase, no punctuation. This breaks any word-level string matching downstream. It
broke my own first term count: searching per word for "Naylor" returned 0, and only
concatenating the letters revealed 2 real hits.

**Scope of this conclusion:** one meeting, ~45 % English. Qwen's losses are *entirely* in
the English half — it produced MORE Chinese than ElevenLabs. **On a monolingual Mandarin
session the verdict could invert.** That is the next comparison worth running.

Qwen also reports its own `content_duration_in_milliseconds`: 1358 s of 1596 s = **85.1 %
judged as speech** (and that is its billing basis). Our VAD passes 97.1 %. Different
criteria, not directly comparable, but Qwen is markedly stricter.

---

## Method rules this session established the hard way

1. **Measure the noise floor before reading any difference.** Run 2 vs run 3 differ at 489
   word positions and 93.5 % of those have nothing to do with the added terms — which was
   about to be reported as "keyterms have broad side effects". A fourth run with the
   *identical* config settled it: run 2 vs run 2′ = **94.4 %**, run 2 vs run 3 = **94.3 %**.
   Identical. The collateral was jitter.
2. **One run is one sample.** Four report revisions were built on run 2, which turned out to
   be the worst of the three whole-session runs. Any single-run number is provisional.
3. **State the denominator.** "36 % of turns" and "85.8 % of seconds" describe the same data
   and point to opposite conclusions.
4. **Two runs agreeing is not evidence they are right.** Runs 1 and 2 both said
   "3W Construction"; run 3's lone "Southbase" was the correct one. The agreement of the
   majority nearly got the only correct reading flagged as a keyterm-induced error.
5. **"Which reading sounds more real" is not verification.** "Naylor Love is a real NZ
   contractor, Naola Lab is not" was reasoning from world knowledge, written into a
   deliverable as though checked. It happened to be right; it was still unverified when
   written. Same reasoning, one paragraph earlier, had been wrong. Mark unverified claims
   as unverified.
6. **A control run is what turns "no effect" into a finding.** Qwen's zero hotword hits
   could equally have meant "these terms are unreachable". The no-vocabulary run plus a
   probe term the model already emits is what separated the two — and it also caught a
   wrong reading in the making: the Chinese probe `工地` scored 2 hits and nearly became
   "Chinese hotwords work", until the baseline showed it scored 2 without any hotword.
7. **Word counts are not comparable across engines.** ElevenLabs splits Chinese per
   character, Qwen per word: 6853 vs 3708 "words" for near-identical content. Compare
   characters, and strip spaces before matching terms or a fragmenting engine looks worse
   than it is.
8. **Score against people, not against another model's output.** Purity was first computed
   against the whole-session run's own labels — circular, and it gave 84.9 %. Rescored
   against the three confirmed names it is 96.3 %. The better reference gave the better
   number *and* removed the circularity.

---

## What is decided, and what is still open

**Decided:** keep batching (§3); keep VAD as is (§4); keep feeding keyterms (§1); solve
speaker identity with a local voiceprint re-bind rather than a second ASR pass (§2).

**Open — needs building:** wire the re-bind into finalize and measure its real added latency
(the ≈55 s may hide entirely behind the 123–134 s extraction LLM, but that is a structural
argument, not a measurement).

**Open — needs more sessions:** this is one meeting. The two that would settle the most are a
**monolingual** session (separates "batching hurts" from "language switching hurts") and a
**3+ speaker toolbox meeting** (label collisions get worse with more people, not better).

**Open — needs the user:** whether the extraction items are *true*. Counts are measurable and
correctness is not. Across the three transcripts: topics 8/7/9, actions 13/11/13,
findings 7/6/**4**, questions 3/2/**1**. That findings/questions drop is larger than the gap
between the first two runs and nobody has judged whether it removed weak items or real ones.

## Related

- `docs/superpowers/specs/2026-08-11-speaker-phase0-results.md` — the earlier speaker gate
- `docs/superpowers/specs/2026-08-09-speaker-identity-v2.md` — the design the guards implement
- `src/voiceprint_utils.py` — margin rule, 3 s floor, homogeneity guard
- `tools/asr-eval/` — every script behind the numbers above
