# Speaker attribution: can reasoning re-unify what per-call diarization split?

**Date:** 2026-08-07
**Revision:** 2 — the original asked the wrong question. See §11.
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
transcription call**, and every call starts labelling at `spk_0`
(`transcript_utils.py:589`). The labels are therefore **raw strings that collide
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

`build_extraction_prompt` must therefore qualify each label with its source call
before flattening — `[09:14:22] c0007/spk_1: …`, using the chunk index already present
in the transcript filename (`chunk_stitch.CHUNK_TOKENS_RE`). This is a prompt-rendering
change only; `speaker_turns`, `speaker_count`, and every stored field are untouched.

Without this the experiment cannot be run at all, and no result from it would mean
anything.

## 5. What the final pass emits

```json
"speakers": {
  "resolved": [
    { "person": "Daniel",
      "labels": ["c0007/spk_1", "c0008/spk_0", "c0011/spk_1"],
      "confidence": "high",
      "basis": "addressed",
      "evidence": [{ "at": "09:14:22", "by": "c0007/spk_0",
                     "quote": "Hey Daniel, can you check the level 3 doors" }] }
  ],
  "unresolved_labels": ["c0019/spk_2"]
}
```

The unit is a **person**, carrying the qualified labels believed to be them. That
inverts the original design, which keyed on label and asked for a name — ill-posed
once labels are known to collide, and it made re-unification inexpressible.

- `basis` ∈ `self_introduced | addressed | referred_to | role_only`. Separate from
  `confidence` because the *kind* of evidence is what we want to correlate with
  accuracy; a self-introduction and a third-party reference are not equally
  trustworthy, and only data can say by how much.
- `evidence` quotes the transcript with its timestamp, so a claim can be checked
  against a citation instead of by re-listening to two hours of audio. This is what
  makes §6 cheap.
- `unresolved_labels` is required, not optional. A model that names everything looks
  better than one that admits doubt, and coverage without that admission is
  unreadable.

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

**Precision** — of the people claimed, how many are right. Reported per `basis` and
per `confidence`, so we learn whether the model's stated confidence carries
information. This is the constraint: the project's standing principle is that a guess
printed as a name reads as a fact.

**Re-unification** — of the label sets attached to each claimed person, are they
actually that person throughout. This is the number the whole spec exists for. A
correct name attached to a label set that silently includes someone else is a **worse**
outcome than no name, and must be counted as an error, not a partial credit.

**Coverage** — what fraction of labels ended up attributed at all, with
`unresolved_labels` as the honest denominator.

**Sample:** at least 3 sessions with genuine multi-person audio, chosen by the filter
that found `Sam_Yu/2026-08-03` — calls whose *internal* label count is ≥2, which is
hard evidence of a second voice rather than an artefact of pooling. The sessions must
be ones a human can actually identify the voices in; a two-hour recording nobody
remembers cannot be checked at any price.

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
newlines, CJK punctuation. So:

- The prompt caps each quote (one short sentence) and caps `resolved` and
  `unresolved_labels` counts. Generation-time limits, not storage-time: output tokens
  and wall-time scale with session length while the model is running, on a function
  already sized against a 600s timeout (`template.yaml:1454`).
- A `speakers` key that is absent, malformed, or the wrong shape is dropped. It never
  fails the extraction, which carries the topics people actually depend on.
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
