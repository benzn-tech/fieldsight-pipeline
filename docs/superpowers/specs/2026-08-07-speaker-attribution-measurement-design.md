# Speaker attribution: measure the ground before building on it

**Date:** 2026-08-07
**Branch:** `docs/speaker-attribution-spec` (off `origin/develop`)
**Status:** Design — awaiting review
**Relates to:** `2026-07-23-session-continuity-design.md` (Spec 1, design-only, unimplemented)

## 1. What this is, and what it is not

This is **not** Spec 2 (speaker naming). Spec 1 already reserved that name and stated
its precondition plainly:

> you cannot map `speaker_N → real name` reliably until `speaker_N` means the same
> person for the whole session.

This spec is the **measurement that comes before both**. It produces one number
nobody has yet — **how badly are speaker labels fragmented in real recordings** —
and a first, unconsumed read on how well a reasoning pass can name them.

That number is the missing input to a decision already on the table: Spec 1 proposes
audio-level session stitching plus one whole-session diarization call, which is real
engineering and depends on an ASR provider prod is not yet running. Nothing in Spec 1
says how much fragmentation there actually is. This spec answers that, cheaply, from
data already flowing.

**Nothing here is consumed.** No report, no action item, no participant list, no
search index, no UI changes. The output is written and read only by us, on purpose.

## 2. Where the pipeline actually stands

Verified against `origin/develop` at `6b48f94`, not assumed:

| Layer | State |
|---|---|
| Text-level session assembly (dedup the device's ~2s chunk overlap) | **Done** — `chunk_stitch.py`, used by `lambda_extract_session` |
| Audio-level stitch + one whole-session diarization | **Spec only.** PR #150 merged a single `.md`; no implementation exists |
| Session-consistent speaker labels | **Absent.** Labels are consistent only within one transcription call |
| Speaker naming | Absent (Spec 2) |
| ElevenLabs ASR path (Spec 1's dependency) | Present in code; prod runs `AsrProvider=transcribe` |

So today a person speaking across several transcription calls appears as several
unrelated labels, and `speaker_count` on every extraction is an **upper bound on the
number of people**, not a count of them.

## 3. Why the measurement rides the final extraction pass

`lambda_extract_session` already runs twice per session:

- **live** — while recording continues, thinking **off**, 90s throttle
- **final** — at session close, thinking **on**, unthrottled, authoritative

The final pass already receives the transcript as `[time] speaker: text` lines
(`:254`), already writes an authoritative artifact, and already carries
`speaker_count`. Adding a `speakers` block to what it writes costs one prompt section
and zero new pipeline.

**We do not write to `transcripts/`.** Three reasons, each from this codebase:

1. `transcripts/` is an S3 event source for extract-session. Rewriting an object
   there re-fires extraction (CLAUDE.md BUG-13), and the live/final passes already
   have an overtake relationship whose termination argument assumes coverage only
   grows. A third writer invalidates that argument.
2. It has exactly one writer today (`lambda_transcribe`). A second writer loses
   values silently the moment ASR is re-run — and an ASR provider switch is in
   flight, so re-runs are not hypothetical.
3. It is evidence. Measured ASR hallucination on mixed Chinese/English runs about
   4%, and the failure mode is fluent, complete, fabricated text. "What was heard"
   and "what we concluded it meant" must stay separable, or nothing downstream can
   ever be audited.

## 4. What the final pass emits

A new `speakers` object inside the extraction payload:

```json
"speakers": {
  "resolved": [
    { "label": "spk_1",
      "name": "Daniel",
      "confidence": "high",
      "basis": "addressed",
      "evidence": [{ "at": "09:14:22", "by": "spk_0",
                     "quote": "Hey Daniel, can you check the level 3 doors" }],
      "reason": "addressed by name, answered in the next turn" }
  ],
  "merges": [
    { "labels": ["spk_1", "spk_3"], "confidence": "medium",
      "reason": "one continuous subject across a 2s gap; both refer to 'my crew'" }
  ],
  "effective_speaker_count": 2
}
```

- `basis` is one of `self_introduced` | `addressed` | `referred_to` | `role_only`.
  Kept separate from `confidence` because the *kind* of evidence is what we want to
  compare against accuracy — a self-introduction and a third-party reference are not
  equally trustworthy, and only the data can say by how much.
- `evidence` quotes the transcript verbatim with its timestamp. Without it a wrong
  name is unexplainable, and the measurement in §6 becomes hand-grading instead of
  checking a citation.
- `confidence` is the model's own, and §6 treats it as a claim to be calibrated, not
  as a fact.

**`speaker_count` is not touched.** It stays the raw ASR count. `effective_speaker_count`
is the post-merge proposal and is read by nobody. The existing single-speaker gate in
item-writer (`_resolve_self_responsible`, which only fires when `speaker_count == 1`)
is unchanged in every respect.

## 5. Only the final pass emits it — and the hazard that creates

`live` does not emit `speakers`. It runs with thinking off, so its attribution would
be worse, and once written the field carries no marker saying which pass produced it.

This creates one real hazard. A live pass **can** overtake a final one when it covers
strictly more transcripts (`_supersedes` / `overtook_final`), and that live extraction
carries no `speakers` block — erasing the final's. The existing code already handles
the general case: on overtaking a final it writes an `extraction_requests/` artifact
asking for another final pass, so the block returns within one cycle.

Two consequences that must be honoured:

- The measurement in §6 samples **final-tier extractions only**, and a session whose
  latest artifact is live-tier is **pending**, not a failure. Counting an
  erased-then-restored block as a miss would manufacture a defect rate out of a
  working self-heal.
- If the re-run request ever fails, the block stays absent. That is visible as
  `tier == "live"` on a closed session, and §6 reports it as a separate count rather
  than folding it into accuracy.

## 6. The measurement

### 6.1 Ground truth, from multi-device groups

When several devices record one meeting as a group, **each device's wearer is known
from its account**, and in that device's own audio the wearer should be the dominant
voice. A group of N devices therefore yields N labelled `(session, dominant-label →
person)` pairs at no annotation cost.

**This is a dependency, not an assumption.** Phase 0 (§8) queries how many grouped
sessions with two or more devices actually exist. If the answer is zero or near zero,
the fallback is hand-labelling 5–10 real sessions, and the naming half of the
measurement waits for real group data while the fragmentation half proceeds — it
needs no ground truth at all.

### 6.2 What we count

**Fragmentation** — the number that decides Spec 1's fate. Measured **two ways, kept
apart**, because the obvious single measure is circular:

- *Model-estimated*: `speaker_count / effective_speaker_count` from the merge
  proposals. Cheap and available on every session — but it is the model grading the
  problem it was asked to solve, so on its own it proves nothing.
- *Independent*: on a grouped session, the number of devices is a **lower bound on
  the people present**, and it comes from the group, not from the model. Comparing
  `speaker_count` against device count gives a fragmentation floor that no prompt can
  flatter. This is the number to quote when arguing for or against Spec 1.

Where both are available they are reported side by side; a large gap between them is
itself a finding about the merge proposals.

Both are reported as a **distribution, not a mean**. A long tail matters more than
the average: one two-hour inspection split into nine labels is the case Spec 1 exists
for, and it vanishes into an average dominated by short single-speaker clips. This is
the error the BUG-43 write-up ended on — error rates must be grouped by input size,
or long recordings hide inside short ones. Report by recording duration bucket.

**Naming precision** — of the names emitted, how many are right. Reported per
`basis`, and per `confidence`, so we learn whether the model's own confidence means
anything.

**Naming coverage** — of labels that a human could name from the transcript alone,
how many got a name. Deliberately secondary: the project's stated principle is that
a guess printed as a name reads as a fact, so precision is the constraint and
coverage is the thing we trade away.

**Over-merge rate** — proposals that fuse two different people. Reported separately
from under-merge, because they are not symmetric: leaving a person split is a missed
improvement, fusing two people is a false statement about who said what.

### 6.3 What "good" would mean

Stated in advance so the numbers cannot be read to suit a conclusion later:

- Independent fragmentation at or near 1.0 label per person across the distribution
  → Spec 1's audio stitch is not urgent, and naming can proceed on current labels.
- A meaningful tail above 2 in the **independent** measure → Spec 1 is the right next
  investment, and naming should wait for it, exactly as Spec 1 argues. A tail that
  appears only in the model-estimated measure is a claim about the model, not about
  the diarization, and must not be used to justify Spec 1.
- Naming precision below roughly 90% at `confidence: high` → the reasoning pass is
  not ready to feed anything user-visible regardless of what the diarization does.

## 7. Error handling

- The `speakers` block is best-effort. A malformed or absent `speakers` key in the
  model's JSON leaves the field out; it never fails the extraction, which carries the
  topics that people actually depend on.
- Evidence quotes are truncated to a fixed length before storage. An unbounded quote
  is a way for a long transcript to bloat every extraction artifact.
- Nothing in this feature can change `speaker_count`, any existing field, or any
  consumer's behaviour. That is the property that makes it safe to ship without
  gating.

## 8. Rollout

**Phase 0 — check the ground truth exists.** Count grouped sessions with ≥2 devices
in prod. Decides whether §6.1 works as written or falls back to hand-labelling. Needs
no code.

**Phase 1 — emit and land.** Prompt section plus the `speakers` block on the final
pass. Unit tests pin that the constraint is present in the prompt and that a missing
or malformed block degrades to absent rather than raising.

**Phase 2 — measure.** A script that reads final-tier extractions over a date range
and reports §6.2, including the count of closed sessions still sitting at live tier.

**Phase 3 — decide.** The numbers choose between Spec 1 first, naming first, or
neither. This spec deliberately does not pre-commit to any of them.

## 9. Out of scope

- Any consumer. `action_items.responsible`, `participants`, the transcript view,
  RAG, and search are untouched.
- Human confirmation UI, and writing to `name_aliases`.
- Voiceprint enrolment or cross-session identity.
- Audio-level session stitching and whole-session diarization — that is Spec 1, and
  this spec exists partly to decide whether to build it.
- Changing the ASR provider.

## 10. Open questions

- **Does grouped multi-device data exist in prod yet?** Unverified — the AWS session
  expired mid-design. Phase 0 answers it, and §6.1 already carries the fallback.
- **Does the wearer-is-dominant-voice assumption hold?** It is the basis of the free
  ground truth. A device worn by someone who mostly listens would break it. Phase 0
  should sanity-check a couple of groups by hand before the metric is trusted.
