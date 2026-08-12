# The schema leaves no way to say "I have no basis for this"

**Repo:** `fieldsight-pipeline`.
**v3.** v1 proposed a prompt rule about epistemic stance; v2 proposed mechanical gating after the
fact. Both were treating the symptom. The measured cause is that the extraction schema **requires**
an observation and offers no way to decline, so the model manufactures one. v3 gives it the slot
and verifies what it puts there.

## The measurement that decides the design

Same input as the incident (`When will the concrete pour? Oops. No. Not yet.` — four of those
words are an ASR fabrication from wind noise), `qwen3.7-max`, three runs each:

| asked | result |
|---|---|
| plainly, no schema | ✅ **3/3 hedged correctly** — "the speaker asked … then corrected themselves" |
| with the production schema | ❌ **3/3 flatly assertive** — "Concrete pour has not yet taken place as of 12:10:52" |

The model is not careless about certainty. `findings: capture EVERY notable observation/issue`
has no floor and no escape hatch, so a topic whose entire content is a question plus a false start
still has to yield an observation — and it gets manufactured, in the one field that is embedded
and displayed.

## The model is already trying to do this, and the code deletes it

```python
# The model may volunteer evidence inside children despite the instruction;
for child_key in ('action_items', 'findings'):
    for child in topic.get(child_key) or []:
        child.pop('evidence', None)
```
(`lambda_extract_session.py:337-343`)

It attaches a citation to individual findings unprompted. We strip it.

**And the reason it is stripped is the reason v3 is shaped the way it is**, not an argument
against: the comment says such a citation "would leave an UNVERIFIED citation in the S3 artifact
for a reader to trust." The problem was never that per-finding citations are unwanted — it is that
nothing checked them. **Naively deleting the `pop` would reintroduce exactly the defect that line
was added to prevent.** The fix is to verify them, then keep them.

## Design

### Order, and why this order

**0. Build the offline harness. Prerequisite, not a phase.**

Nothing below can be evaluated without it:

- `_call_qwen` sends **no `temperature` and no `seed`** (`llm_utils.py:141-162`). Measured: three
  identical prompts, three different summaries. A single A/B run measures sampling, not the change.
- Re-extracting through the deployed lambda writes the real `extraction_key`, which fires
  item-writer's delete-then-insert and **mutates a real customer's Aurora topics**
  (`lambda_item_writer.py:635`). **Evaluation must never run through prod.**

The harness: download a session's transcripts → build the prompt locally → call qwen directly →
compare structured output across N runs and across prompt variants. `enable_thinking` pinned (live
and final passes use opposite modes, so "same model" is otherwise under-specified).

**1. Ask for per-finding evidence, and stop deleting it.**

Add `evidence` to the `findings[]` shape in the extraction schema, alongside the topic-level one
that already exists. Remove the `child.pop` **only together with step 2** — unverified citations
in the artifact is the exact failure that line prevents.

**2. Run the existing checker one level down.**

`check_quote` is already mechanical end to end: string containment inside a 60s window, an
ellipsis-splice test, a 0.80 fuzzy floor, and a 5-token specificity floor counted per writing
system. It needs no changes — only to be called per finding as well as per topic.

This is what makes the model's self-report trustworthy. **Self-reported confidence is not
evidence** — the generator that writes "It was confirmed" will equally write `"support": "stated"`.
But a *citation* is checkable: the model may only point, and whether the quote exists and how
specific it is, is decided by string matching. That distinction is the whole design.

**3. Persist it: `findings.evidence` / `evidence_status`.**

A migration, following `0037_topic_evidence.sql`'s precedent (jsonb on the row, not a child
table). This is no longer "deferred until topic granularity proves too coarse" — with per-finding
citations it is where the result lives.

Topic-level status can then roll up from its findings instead of being a separate assertion.

**4. Let `findings` be empty.**

The schema already permits it ("may be empty arrays"); the instruction does not. With an
evidence slot, "I have no basis" is expressible — a topic made of a question and a false start
should return no finding rather than invent one. The floor is the same 5-token specificity rule,
now applied where the claim is made.

### What this does NOT do

- **It does not judge whether a real quote supports the claim built on it.** A model can attach a
  genuine quote to an unsupported conclusion. That is entailment, it needs a model, and it is
  deliberately out of scope here — run it later over the small set this layer marks weak, and have
  it annotate rather than delete.
- **It does not detect ASR fabrication.** `Oops. No. Not yet.` is in the transcript, so a citation
  to it is genuine. What changes is that a finding standing on four words is *marked* as standing
  on four words. The fabrication itself is the normalisation-amplifies-noise finding, separate.
- **It does not make the report right.** It makes the report say how much it knows.

## Corrections carried forward from v2

- **"Turning EMIT_EVIDENCE on would not have helped" was false**, and free to check.
  `token_count("Oops. No. Not yet.")` is 4, below the 5-token floor, so `check_quote` returns
  `weak`; `roll_up` takes the worst; `weak` is persisted. Detection existed; consequence did not.
- **The agent-answer incident is already fixed on this branch** (`agent_turn_filter`), so it cannot
  motivate this, and an A/B over that session would measure the filter instead.
- **`questions` is write-only** — one occurrence in `src/`, its own schema definition. Not
  persisted by item-writer, not embedded by chunking. The model fills it correctly 3/3 into a
  field with no readers. Same for `decisions`. Filed separately: wire them up or drop them; do not
  "fix" anything by writing into them.

## Costs, stated rather than discovered

- **Every finding now carries a citation.** More output tokens per topic, more `check_quote` calls
  per session. Extraction already runs to a 600s timeout and has caused a livelock that lost
  customer uploads — account concurrency is charged by wall clock (BUG-43). Measure prompt and
  response size in the harness before shipping, not after.
- **It is a prompt change**, so no existing test can pin it: the code path is unchanged and every
  test passes either way. The harness is the only instrument.
- **Artifact size** grows with per-finding citations; the S3 extraction artifact is read by the
  matcher and the rolling summary.

## Acceptance

**Harness, N ≥ 3 runs per arm, thinking pinned, both languages** (the prompt is shared; two
ASCII-normalisation bugs have already erased CJK in this repo):

1. **The incident case**: with per-finding evidence, the fabricated observation either disappears
   or is marked `weak`. Requirement is the marking, not the disappearance — a model that still
   writes it but declares a four-word basis has done the right thing.
2. **Regression sample**, ≥5 unrelated historical sessions: genuine findings keep their citations
   and do not collapse to empty. **An extractor that finds nothing is as useless as one that
   invents.** Compare finding counts *and* the fraction rolling up `verified`.
3. **Cost**: prompt/response tokens and wall-clock per session, before and after.

Counts alone cannot be the metric — a wording change leaves them identical. Compare the wording
of findings whose evidence rolls up `weak`.

## Rollout

Behind an env toggle wired through the workflow **and** the template Parameter — not just the
code. This repo has shipped a toggle that could only take its default and reported success the
whole way. `EMIT_EVIDENCE` is the existing precedent and it ships off.
