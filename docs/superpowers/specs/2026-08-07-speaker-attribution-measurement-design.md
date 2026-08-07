# Speaker attribution: can reasoning re-unify what per-call diarization split?

**Date:** 2026-08-07
**Revision:** 3 — see §11 (what rev 1 got wrong) and §12 (what rev 2 got wrong).
**Branch:** `docs/speaker-attribution-spec` (off `origin/develop`)
**Status:** Design — awaiting review
**Relates to:** `2026-07-23-session-continuity-design.md` (Spec 1, design-only, unimplemented)

## 1. The question this answers

Not "how fragmented are speaker labels" — that turned out to be arithmetic, not an
open question (§2). The real one:

> **Can the thinking-mode extraction pass re-unify a person's identity across
> per-call speaker labels, and name them, well enough to be worth relying on?**

If yes, Spec 1's audio-level stitch is an optimisation rather than a prerequisite.
If no, Spec 1 is the only way forward and naming should wait for it.

Nothing built here is consumed. No report, no action item, no participant list, no
search index, no UI. The output exists to answer the question above and nothing else.

## 2. What was measured, and why it changed the question

Measured on real production data, 2026-08-07.

**`speaker_count` does not mean what its name suggests.** It is
`len({t['speaker'] for t in turns})` (`lambda_extract_session.py:692`) over turns
pooled from every transcript file in the session. Each ~30s chunk is a **separate
transcription call**, and every call labels independently, from
its own `spk_0` (one Transcribe job / one ElevenLabs call per VAD region). The labels are therefore **raw strings that collide
across calls**: Alice-as-`spk_0` in chunk 1 and Bob-as-`spk_0` in chunk 2 are one
entry in that set. It is capped by the per-call `MAX_SPEAKERS` (default 5,
`template.yaml:715`) no matter how long the recording. **It is neither an upper nor a
lower bound on the number of people.**

**Real session: `Sam_Yu / 2026-08-03 / sid622a0e7f…`** — 262 transcription calls.
Distinct speaker labels *within a single call*:

| labels in one call | 0 | 1 | 2 | 3 | 4 | 5 |
|---|---|---|---|---|---|---|
| calls | 1 | 44 | **169** | 42 | 5 | 1 |

217 of 262 calls heard two or more voices. The session's `speaker_count` would read
**5**; the number of label *instances* is roughly **500**; the number of actual
people is probably two or three.

So fragmentation is not in doubt and does not need measuring: with per-call
diarization and 30s chunks, **a person is re-labelled in every chunk they speak in,
by construction**. Spec 1's premise is established. What is genuinely unknown is
whether a reasoning pass can put the pieces back together from context alone.

**Ground truth from multi-device groups does not exist.** Measured, not assumed:
`meeting_session` has 15 rows in prod and 28 in test, with `group_id` non-null on
**zero** of them. The grouping feature is wired end to end but has never been used on
a real meeting. §6 does not depend on it.

## 3. Where the work rides, and why not the transcript

`lambda_extract_session` already runs twice per session: **live** (thinking off, 90s
throttle, during recording) and **final** (thinking on, unthrottled, at session close,
authoritative). The final pass already receives the transcript as `[time] speaker:
text` lines (`:254`) and already writes an authoritative artifact. A `speakers` block
on that artifact costs one prompt section and no new pipeline.

**We do not write to `transcripts/`:**

1. It is an S3 event source for extract-session, so rewriting an object there re-fires
   extraction (CLAUDE.md BUG-13). The live/final passes already have an overtake
   relationship whose termination argument assumes coverage only grows; a third writer
   invalidates it.
2. It has one *orchestrator* today. On the prod path the object is written by the AWS
   Transcribe service itself (`lambda_transcribe.py:405-413`, bucket policy
   `template.yaml:1247-1249`); the Lambda writes directly only on the ElevenLabs path
   (`:383`). Either way, a second author loses values silently the moment ASR re-runs —
   and an ASR provider switch is in flight, so re-runs are not hypothetical.
3. It is evidence. Measured ASR hallucination on mixed Chinese/English runs about 4%,
   and the failure mode is fluent, complete, fabricated text. "What was heard" and
   "what we concluded it meant" must stay separable or nothing can be audited.

## 4. Prerequisite: labels must be sayable

The prompt currently renders every turn as `[09:14:22] spk_1: …`, so `spk_1` in chunk
7 and `spk_1` in chunk 40 are indistinguishable **to the model as well as to us**. It
cannot express "these two are the same person" because it cannot refer to them
separately, and it cannot avoid conflating them either.

The qualification must happen in **`assemble_deduped_turns`**
(`lambda_extract_session.py:430-447`), not in `build_extraction_prompt`. By the time
the latter runs (`:253-254`) the turns have been flattened and sorted by `abs_start`
and **provenance is already gone** — a turn dict carries only
`speaker/text/start_sec/end_sec/abs_*` (`transcript_utils.py:382-391`), and the
flatten loop keeps nothing from `normalized['filename']`. `filename` is in scope one
function earlier; that is where the tag must be attached.

**The qualifier is the transcript FILE, not the chunk index.** One transcription call
= one transcript file, and a chunk can yield several: `build_segment_filename`
(`lambda_vad.py:588-599`) emits one `_off{a}_to{b}` file **per VAD region**, all
sharing the same `_c{NNNN}`. The measured session shows it — 262 files from 260 chunk
indices. Qualifying by chunk index alone would let two calls inside chunk 7 both
render as `c0007/spk_0`, reintroducing exactly the collision this section exists to
remove. Use `c{NNNN}@{off}` (or a per-file ordinal); rare is not the same as absent,
and "rare collisions are fine" is what made revision 1 wrong.

This is a rendering change only; `speaker_turns`, `speaker_count`, and every stored
field are untouched. Without it the experiment cannot be run at all.

## 5. What the final pass emits

```json
"speakers": {
  "resolved": [
    { "person": "Daniel",
      "labels": ["c0007@0.0/spk_1", "c0008@0.0/spk_0", "c0011@12.5/spk_1"],
      "confidence": "high",
      "basis": "addressed",
      "evidence": [{ "at": "09:14:22", "by": "c0007@0.0/spk_0",
                     "quote": "Hey Daniel, can you check the level 3 doors" }] }
  ],
  "unresolved_labels": ["c0019@0.0/spk_2"]
}
```

The unit is a **person**, carrying the qualified labels believed to be them. That
inverts the original design, which keyed on label and asked for a name — ill-posed
once labels are known to collide, and it made re-unification inexpressible.

- `basis` ∈ `self_introduced | addressed | referred_to | role_only`. Separate from
  `confidence` because the *kind* of evidence is what we want to correlate with
  accuracy; a self-introduction and a third-party reference are not equally
  trustworthy, and only data can say by how much.
- `evidence` quotes the transcript with its timestamp. It makes **precision** cheap to
  check — one citation, one verdict. It does **not** make re-unification cheap; §6
  defines a separate sampling procedure for that, because a citation validates a name
  at one moment and says nothing about the rest of the label set.
- `unresolved_labels` is required, not optional. A model that names everything looks
  better than one that admits doubt. It is bounded by §6's session-length rule rather
  than by a cap: a cap would put most labels in neither list and leave coverage with
  no denominator at all.

**`speaker_count` is not touched**, and the existing `speaker_count == 1` gate in
item-writer (`lambda_item_writer.py:359`) is unchanged. Note that gate is *correct
despite* the collision: a union of exactly one label string implies no call ever heard
a second voice, which is precisely what it claims. It stays out of scope.

**Dropped from revision 1:** `merges` and `effective_speaker_count`. Merge proposals
over a colliding label space were ill-posed, and a fragmentation ratio computed from
the model's own proposals was circular — the model grading the problem it was asked to
solve. §2 answers the fragmentation question directly from the transcripts instead.

## 6. How the claims get checked

The model states claims with citations; a human marks each **right / wrong / can't
tell**. That is minutes of work per session, not hours, and it is the only reason the
`evidence` field exists.

**Precision** — of the people claimed, how many are right. **Checking one citation
measures exactly this and nothing else.** Reported per `basis` and per `confidence`,
so we learn whether the model's stated confidence carries information.

**Re-unification** — are the labels attached to a claimed person actually that person
throughout. This is the number the spec exists for, and **a citation cannot show it**:
"Hey Daniel…" validates the name at one moment and says nothing about the other 119
labels in Daniel's set. An earlier draft claimed the whole check was "minutes of work";
that was true of precision only, and stating it of re-unification hid the fact that no
procedure had been defined for it. The procedure:

> For each claimed person, sample **k = 8** of their labels uniformly at random. For
> each sampled label, read the transcript window around it; where the text alone is
> ambiguous, listen to the audio at that timestamp. Mark same-person / different-person
> / can't-tell. A claim **passes** only if 8/8 are same-person or can't-tell, with at
> least 5 positively same-person.

k=8 is chosen so that a set with 20% contamination fails about 83% of the time, which
is enough to tell "works" from "does not" without approaching the cost of exhaustive
labelling. It is pre-registered here so it cannot be relaxed after seeing results.

A correct name over a contaminated label set is a **worse** outcome than no name and
counts as failure, not partial credit.

**Coverage** — what fraction of labels reaching the model ended up attributed. The
denominator is the labels **actually rendered into the prompt**, not the labels present
in S3. Two things remove labels before the model sees them: `_dedup_turn_boundaries`
drops fully-overlapping turns (`lambda_extract_session.py:171-201`), and
`TRANSCRIPT_TEXT_LIMIT` truncates (below). Counting against S3 would score truncation
as a model failure.

**Sample: sessions that fit inside `TRANSCRIPT_TEXT_LIMIT` (60,000 chars,
`lambda_extract_session.py:62,255`).** This is not a preference, it is a hard
constraint discovered by measurement: the `Sam_Yu/2026-08-03` session renders to
**128,427 characters, so only 47% of it ever reaches the model** — roughly 387 of 838
turn lines. Every metric above would be measuring truncation, not reasoning. Sessions
of roughly 45 minutes or less fit; the candidate filter must check rendered length, not
just duration.

Candidates are chosen by the filter that found that session — calls whose *internal*
label count is ≥2. That is **evidence of, not proof of, a second voice**: per-call
diarization over-splits single speakers routinely, so expect phantom speakers and count
correctly merging them as a success rather than treating their presence as an error.

At least 3 such sessions, and they must be ones a human can identify the voices in. A
recording nobody remembers cannot be checked at any price.

**Selection bias, stated up front:** only new-app chunk sessions are measurable.
Legacy whole-file (RealPTT) recordings never get a final pass at all —
`extraction_requests/` are written only for `sid` sessions and `_request_final_rerun`
bails on non-`sid` bases (`lambda_extract_session.py:722-724`).

### 6.1 What would count as an answer

Pre-registered, so the numbers cannot be read to suit a conclusion later:

- **Re-unification correct on most claims, precision high at `confidence: high`** →
  reasoning can stand in for whole-session diarization; Spec 1 becomes an
  optimisation, and naming can proceed on the current labels.
- **Re-unification unreliable while precision looks fine** → the model is naming
  voices it cannot actually track. This is the dangerous outcome: it produces
  confident, checkable-looking, wrong attributions. Spec 1 first, and naming waits.
- **Precision poor at `confidence: high`** → the reasoning pass is not ready to feed
  anything user-visible, whatever the diarization does.

## 7. Error handling — including the one that can take topics down

The final pass runs thinking mode, which sends **no `response_format`**
(`llm_utils.py:144-150`) and relies on `extract_json`. If the whole document fails to
parse, `extract_json` returns None and the caller raises (`:643-645`), the S3 event
retries a 170s+ thinking call, **and the topics go down with it**.

Verbatim transcript quotes are exactly what breaks JSON escaping — embedded quotes,
newlines, CJK punctuation.

**There is no output cap to lean on.** The thinking path deliberately sends no
`max_tokens` ("No max_tokens cap so the answer isn't truncated after the … chain of
thought", `llm_utils.py:146-152`), so the `max_tokens` computed at `:633` is ignored on
exactly the pass this feature runs on. Prompt-stated caps are requests, enforced by
nothing. The only hard bounds are `LLM_HTTP_TIMEOUT=540` and the 600 s Lambda timeout —
and reaching either is a failed call, which raises, which retries another 170 s+
thinking call, which is the outcome this section exists to prevent. An earlier draft
called prompt caps "generation-time limits"; they are not limits.

So the protection has to be structural, and there are two honest options:

- **Bound the input.** §6 already restricts the experiment to sessions inside
  `TRANSCRIPT_TEXT_LIMIT`, which bounds the label count and therefore the plausible
  output size. This is sufficient for the experiment and costs nothing.
- **Separate the call.** If `speakers` is ever wanted on unbounded sessions, it must be
  its own LLM call rather than extra keys in the extraction JSON — so that a quote which
  breaks the document loses only the attribution, not the topics. Prompt caps stay as
  guidance, but the isolation is what does the work.

The experiment takes the first. Anything beyond the experiment must take the second.

- A `speakers` key that is absent, malformed, or the wrong shape is dropped. That
  covers bad values inside valid JSON; it does **not** cover a document that fails to
  parse, which is why the two options above exist.
- Nothing here can change `speaker_count`, any existing field, or any consumer's
  behaviour. No consumer validates the artifact schema — item-writer reads named keys
  and the only shape guard is on `topics` (`lambda_extract_session.py:650-655`) — so an
  unknown `speakers` key passes through untouched. That is the property that makes this
  safe to ship ungated.

**Provider caveat:** the thinking on/off split between live and final is qwen-path
only. `_call_anthropic` ignores `enable_thinking` (`llm_utils.py:79-81, 107`), so under
`LlmProvider=anthropic` there is no quality difference between the tiers and §8's
final-only argument weakens accordingly.

## 8. Only the final pass emits it

`live` does not emit `speakers`: thinking is off there, so its attribution would be
worse, and the artifact would not distinguish the two — though `tier` on the payload
(`:685`) does say which pass wrote it, and §6 uses that.

One hazard follows. A live pass **can** overtake a final one when it covers strictly
more transcripts (`_supersedes` `:553-587`, `overtook_final` `:669-678`), and that live
extraction carries no `speakers` block, erasing the final's. The existing code
self-heals: on overtaking a final it writes an `extraction_requests/` artifact asking
for another final pass (`:707-736`).

Consequences that must be honoured: §6 samples **final-tier artifacts only**, and a
closed session sitting at live tier is **pending**, not a failure — counting an
erased-then-restored block as a miss would manufacture a defect rate out of a working
self-heal. If the re-run request itself fails the block stays absent; that shows up as
`tier == "live"` on a closed session and is reported as its own count.

## 9. Rollout

**Phase 1 — label qualification (§4) + the `speakers` prompt section and block.** Unit
tests pin that labels are rendered qualified, that the constraint is present in the
prompt, and that an absent/malformed/oversized `speakers` value degrades to absent
rather than raising.

**Phase 2 — run it on identified sessions.** Requires recordings whose voices a human
can name; §6 says why. Produces the check sheet.

**Phase 3 — decide.** The numbers choose between Spec 1 first, naming first, or
neither. This spec pre-commits to none of them.

## 10. Out of scope

- Any consumer: `action_items.responsible`, `participants`, the transcript view, RAG,
  search — all untouched.
- The `speaker_count == 1` gate (correct as it stands, §5).
- Human confirmation UI; writing to `name_aliases`.
- Voiceprint enrolment, cross-session identity.
- Audio-level session stitching and whole-session diarization — that is Spec 1, and
  this spec exists partly to decide whether to build it.
- Changing the ASR provider or `MAX_SPEAKERS`.

## 11. What revision 1 got wrong

- It said `speaker_count` was "an upper bound on the number of people". It is neither
  bound — it is the union of per-call label vocabularies, capped at 5 (§2).
- It proposed measuring fragmentation and using that number to decide Spec 1's fate.
  With colliding labels the measure returns ≈1.0 regardless of the truth, so it would
  have **killed Spec 1 for exactly the wrong reason**. Fragmentation is now answered
  directly from the transcripts, and the question moved to re-unification.
- It keyed `resolved` on label → name, which cannot express re-unification. Now keyed
  on person → labels.
- It built its ground truth on multi-device groups. There are none, and the feature has
  never been used on a real meeting — measured, not assumed.
- It claimed the artifact carries no marker of which pass wrote it. It carries `tier`.
- It said `transcripts/` has exactly one writer. On the prod path the writer is the
  AWS Transcribe service; the Lambda is the orchestrator.
- It treated JSON-level failure as covered by "malformed values are ignored". A quote
  that breaks the document takes the topics down with it (§7).

## 12. What revision 2 got wrong (fixed in revision 3)

- **§4 pointed at the wrong function.** Provenance is gone by the time
  `build_extraction_prompt` runs; the tag must be attached in `assemble_deduped_turns`.
- **§4 qualified by chunk index.** A chunk can produce several transcription calls (one
  per VAD region — 262 files from 260 chunks in the measured session), so `c0007/spk_0`
  could still name two different calls. The qualifier is the file.
- **§6 claimed the whole check was minutes of work.** True of precision, false of
  re-unification, and no procedure existed for the latter — the number the spec says it
  exists for had no way of being produced. Now a pre-registered k=8 sample per claim.
- **§6 counted coverage against S3.** Truncation and turn-dedup remove labels before the
  model sees them; the denominator is the rendered prompt.
- **§6 ignored `TRANSCRIPT_TEXT_LIMIT`.** The session it cites as the exemplar is
  128,427 rendered characters against a 60,000 limit — **47% of it reaches the model**.
  Every metric would have measured truncation. The experiment is now scoped to sessions
  that fit.
- **§7 described caps that do not exist.** Thinking mode sends no `max_tokens`; prompt
  caps enforce nothing. Protection is now structural: bound the input, or isolate the
  call.
- **§6 called ≥2 internal labels "hard evidence" of a second voice.** Per-call
  diarization over-splits single speakers; it is evidence, not proof.
