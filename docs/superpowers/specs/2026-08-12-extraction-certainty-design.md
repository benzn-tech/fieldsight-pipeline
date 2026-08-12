# Evidence status has no consequences

**Repo:** `fieldsight-pipeline`. Behaviour of an already-built feature, plus one small gate.
**v2 — the first draft was reworked after review and after measurement.** Three of its claims
were wrong and are corrected below, because the reasons they were wrong are the design.

## What started this

A finding on the daily timeline read:

> *"It **was confirmed** that the pour has not yet taken place or is not yet ready."*

Nobody confirmed anything. The topic's entire substance was one question and four words that
ElevenLabs produced from wind and metal noise (`Oops. No. Not yet.` — 0 of 3 runs on the raw
audio, 1 of 3 on the normalised audio).

## Three corrections to the first draft

### C1 — "Turning on EMIT_EVIDENCE would not have helped" is FALSE, and it was free to check

`token_count("Oops. No. Not yet.")` is **4**, below `EVIDENCE_FLOOR_TOKENS = 5`, so
`check_quote` returns **`weak`** (`evidence_match.py:159,188`). `STATUS_ORDER = ["weak",
"verified_fuzzy", "verified"]` and `roll_up` takes the **worst** (`:344-367`), so one good
citation plus that fragment still rolls up `weak`. And `weak` is **persisted** —
`_evidence_payload` → `upsert_topic(evidence=...)` (`lambda_item_writer.py:341-351,695`).

**The existing machinery already detects the exact shape of this failure**: a claim standing on
a four-word fragment. The floor exists for that reason — "a quote of two or three words proves
nothing" (`lambda_extract_session.py:404`).

What is missing is not detection. **It is that the detection has no consequence.** `EMIT_EVIDENCE`
is `false` on prod, and even with it on, a topic that rolls up `weak` is written with exactly the
same confident wording as one that rolls up `verified`. Nothing reads the status back.

### C2 — The second motivating incident is already fixed, on this same branch

The agent's own spoken answer becoming a site fact is cut by `agent_turn_filter` (this branch,
commits `eb22877`/`e451206`): the turns are removed before the prompt is built. It cannot
motivate this change, and — importantly — an A/B that re-extracts the 2026-08-12 session would
have its delta dominated by that filter, not by anything proposed here.

### C3 — Routing content into `questions` would have hidden it, not fixed it

The first draft proposed sending unanswered questions to the `questions` field. Two things kill
that:

- **Nothing reads it.** `questions` appears once in `src/` — its own schema definition
  (`lambda_extract_session.py:652`). `item_writer` persists title/category/summary/action_items/
  participants/findings/evidence and **not** questions; `chunking._topic_text` embeds
  participants/summary/key_decisions/action_items and **not** questions. It is write-only.
- **The model already does it.** Measured below: 3 of 3 runs filed the question correctly.

So the model has been filing questions correctly all along, into a field with no consumers, and
the proposal would have moved *more* content there — turning a visible wrong answer into an
invisible one. The acceptance criterion ("the question appears in `questions`") would have
verified a write nobody loads.

**Filed separately:** `questions` and `decisions` are produced every run and consumed by nothing.
Either wire them up or drop them from the schema; leaving them is a standing invitation to
"fix" something by writing into a void.

## What the measurement actually says

Same input as the incident, `qwen3.7-max`, non-thinking, production-shaped schema, three runs:

| field | result |
|---|---|
| `questions` | ✅ 3/3 correct — "When will the concrete pour take place?" |
| `summary` | 2/3 correctly hedged ("inquired about… and indicated"); **1/3 asserts** ("with confirmation that") |
| `findings.observation` | ❌ **3/3 flatly assertive** — "Concrete pour has not yet taken place as of 12:10:52" |

And asked plainly, with no schema, the same model hedges correctly 3/3 ("the speaker asked …
then corrected themselves").

**So the model is not careless about certainty. The schema demand is.** `findings: capture EVERY
notable observation/issue` has no floor: given a topic whose whole content is a question plus a
false start, the model must still produce an observation, so it manufactures one and states it as
fact. The damage concentrates in exactly the field that gets embedded and displayed.

## Also measured: extraction is not reproducible

`llm_utils._call_qwen` sends **no `temperature` and no `seed`** (`:141-162`), so the provider
default applies. Three identical prompts produced three different summaries.

Two consequences, and the second is bigger than this spec:

1. **Any A/B needs N runs per arm**, or a pinned temperature. One run per arm measures sampling.
2. **Production extraction is non-reproducible.** The same transcript extracted twice yields
   different findings and wording — and this pipeline extracts more than once by design (live
   pass, final pass, backfills, reruns). Worth its own decision; not resolved here.

## Design

Two layers, deliberately split by what each is good at.

### Layer 1 — mechanical detection, no judgment

Turn `EMIT_EVIDENCE` on, and give the status a consequence. The mechanism is already built and
is mechanical end to end: the model may only *point* at a quote; whether that quote exists is
decided by string containment inside a 60s window, an ellipsis-splice test, then a 0.80 fuzzy
floor, plus a 5-token specificity floor counted per writing system (so a fully specific Chinese
quote is not capped at `weak` by whitespace counting). Same input, same output, whatever the
sampler does.

**The consequence:** a topic whose evidence rolls up `weak` or `absent` must not present its
findings as settled fact. Minimum viable form — annotate, do not delete:

- the topic carries its evidence status where the UI and the report can see it;
- a finding on `weak`/`absent` evidence is marked as unsupported rather than rendered as a
  statement of fact.

**And a floor on emitting findings at all.** `capture EVERY notable observation` is what
manufactures an observation out of a false start. A topic whose material cannot support one
should be allowed to return an empty `findings` array — the schema already permits it
("may be empty arrays").

### Layer 2 — model judgment, only on what Layer 1 flags

"Does this quote actually support this claim?" is an entailment question. A regex cannot do it,
and in Chinese a phrase list is hopeless — this repo has shipped two ASCII-normalisation bugs
that erased CJK entirely.

But it runs **only on the small set Layer 1 marks weak/absent**, and its output **annotates**.
Reasons, all specific rather than a preference for simplicity:

- **Circularity.** The tendency that produced "It was confirmed" is the same one that would judge
  it. Measured: the assertive finding is 3/3, not an occasional slip.
- **Non-determinism.** A gate that fires 1/3 of the time is noise, not a gate. Confining the model
  to annotation means the jitter cannot swing the main path.
- **Concurrency.** Extraction already carries a 600s timeout and has caused a livelock that lost
  customer uploads (account concurrency is charged by wall-clock, BUG-43). One extra LLM call per
  topic goes the wrong way; one per flagged topic does not.

## Why not a prompt rule (the first draft's proposal)

It fails its own motivating case. The rule was "no settled language unless the transcript
contains someone stating it as settled" — and in the transcript **the model saw**, someone did:
`Oops. No. Not yet.` is, to the model, an answer. The rule removes the words "it was confirmed"
and leaves the false claim.

No epistemic-stance instruction can separate a real "no, not yet" from a hallucinated one. That
defence belongs upstream (the normalisation-amplifies-noise finding) and in thin-support gating —
not in prompt wording.

Supporting precedent: every prompt contract in this repo has needed a code-side check anyway —
the model "may volunteer evidence inside children **despite the instruction**"
(`lambda_extract_session.py:337-343`), `work_class` is sanitised post-hoc
(`item_writer.py:664-676`), `declared_site` gets a fuzzy post-check.

## Acceptance

**Mechanical layer — testable normally.** Unit tests over `roll_up`/`check_quote` already exist;
add: a topic whose only citation is below the token floor rolls up `weak`; a `weak` topic's
findings are marked unsupported; an empty `findings` array survives the schema and item-writer.

**Never re-extract through prod to evaluate.** `extract_session` writes the real
`extraction_key`, which fires item-writer's delete-then-insert and **mutates a real customer's
Aurora topics** (`lambda_item_writer.py:635`). Any A/B needs a local harness: download
transcripts → build the prompt → call qwen directly → compare. That harness does not exist and
must be budgeted.

**If Layer 2 is built**, it needs ≥3 runs per arm with `enable_thinking` pinned (live and final
passes use opposite modes), and the metric cannot be topic/action/finding **counts** — a wording
change leaves counts identical. Compare the wording of findings on `weak` evidence specifically.

## Rollout

Behind an env toggle, wired through the workflow **and** the template Parameter — not just the
code. This repo has shipped a toggle that could only ever take its default and reported success
the whole way.

(The first draft said this matched `FILTER_AUDIO_EVENT_TAGS` shipping off. It ships **on**
(`lambda_extract_session.py:251`). `EMIT_EVIDENCE` is the one that ships off.)
