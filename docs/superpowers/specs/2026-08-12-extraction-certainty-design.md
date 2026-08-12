# One instruction line, measured

**Repo:** `fieldsight-pipeline`. One line in the extraction prompt. No schema change, no
migration, no flag.

**v4.** v1 proposed a rule about epistemic stance. v2 proposed mechanical gating. v3 proposed
per-finding citations plus a migration. **All three are wrong, and v3 is wrong in a way that
would have made things worse.** What follows is what the measurements actually support.

## The incident

A finding on the daily timeline read *"It **was confirmed** that the pour has not yet taken
place."* Nobody confirmed anything: the topic's whole content was one question plus four words
ElevenLabs produced from wind noise (`Oops. No. Not yet.` — 0/3 runs on the raw audio, 1/3 on the
normalised).

## The measurement that decides it

`qwen3.7-max`, non-thinking, `response_format: json_object`, identical task and schema in every
arm, three runs each. Only the instruction differs.

| arm | added to the prompt | findings produced |
|---|---|---|
| control | — | **3/3 the fabricated assertion** |
| `evid_only` | an `evidence` field per finding | **3/3 the assertion, with a citation** |
| `perm_only` | *"If no line states an observation, do NOT invent one — findings may be an empty array."* | **3/3 zero findings** |
| both | both | 3/3 zero findings |

**The escape hatch is what works. The citation field does nothing on its own.**

### v3 would have made this worse, and the measurement shows how

Every `evid_only` run cited the same string: `"When will the concrete pour? Oops. No. Not yet."`
That is ~9 tokens — **above `EVIDENCE_FLOOR_TOKENS = 5`** — and verbatim in the transcript. So
`check_quote` returns **`verified`**.

v3's whole premise was that per-finding citations would mark this claim as thinly supported. The
model does not cite the narrow fragment; it cites the whole line. **The design would have stamped
the fabricated assertion `verified` and shipped it with a receipt** — more trustworthy-looking
than the unmarked version it replaced.

`check_quote` measures existence and specificity. It never measures whether the quote *supports*
the claim, and v3 explicitly carved entailment out of scope — so it had no mechanism left to
catch its own motivating case.

### And the regression arm says the cost is acceptable

Real session content (brackets, building wrap, timber, scaffolding), three runs each:

| | findings |
|---|---|
| control | 8, 8, 8 |
| with the escape hatch | 5, 7, 8 |

The count drops. **Reading them shows what moved rather than vanished:** the only item missing
from a lower-count run was *"Scaffolding requires inspection prior to Monday"* — and across three
runs it appeared as an **action item every time** (once as both). The transcript says *"We should
check the scaffolding before Monday"*, which is an instruction to do something. Filing it as an
action rather than an observation is the better answer, not a loss.

**This is one item on one session.** It is the evidence there is, and it is not enough to call the
regression risk closed — see acceptance.

## The change

One line, in `_instructions_block`'s findings section:

> If no line in the transcript states an observation, do NOT invent one — `findings` may be an
> empty array.

### Why it is a line and not a rule about wording

The diagnosis in v3 was also wrong in its wording: the schema does **not** require an observation —
`lambda_extract_session.py:813` already says findings "may be empty arrays". The pressure comes
from instruction 4, **"capture EVERY notable observation/issue"** (`:782`), which has no floor. The
schema permits silence; the instruction does not. That is why one line in the instruction, rather
than a schema change, is the whole fix.

The evidence that the model is not otherwise careless: with the same schema, `summary` still
hedged 2/3 and `questions` was filled correctly 3/3. Hedging survives everywhere except the field
the instruction pressures.

## What this does not do

- **It does not detect the ASR fabrication.** `Oops. No. Not yet.` is in the transcript. What
  changes is that a topic with nothing observable in it now yields no observation.
- **It does not fix `summary`**, which asserted 1/3. The finding was the field that mattered
  (3/3, and it is what `/live-items` shows), but the summary is untouched and unmeasured here.
- **It does not touch grouped meetings' verification gap** — see below.

## Two things found along the way, filed separately

**`questions` and `decisions` are write-only.** `item_writer` persists neither; `chunking`
embeds neither. The model fills `questions` correctly 3/3 into a field with no readers. Wire them
up or drop them — but do not "fix" anything by writing into them.

**Group extractions persist unverified citations today.** `build_group_prompt` reuses
`_instructions_block()` verbatim, so with `EMIT_EVIDENCE=true` the group prompt asks for evidence,
but `verify_evidence` returns early for `TIER_GROUP` **before** the child-strip loop
(`:326-329`) — so `_evidence_payload` writes `{"status": None, "quotes": [...]}` into Aurora.
That is the exact defect the child-strip exists to prevent, on the path where meetings matter
most. It is live on test today. **This is a bug, not a design gap, and it is independent of
everything above.**

## Acceptance

**A prompt change cannot be pinned by a unit test** — the code path is identical and every
existing test passes either way. The instrument is the harness, and the harness must import
`assemble_session_turns`, `build_extraction_prompt` and the real payload builder rather than
re-deriving them, or it measures a request prod never sends.

Required before shipping:

1. **≥5 unrelated historical sessions**, ≥3 runs per arm, **both thinking modes** — prod runs
   non-thinking for the live pass and thinking for the final (`:1479-1480`), so pinning one leaves
   the other unmeasured.
2. **Both languages.** The prompt is shared, and this repo has shipped two ASCII-normalisation
   bugs that erased CJK entirely.
3. **Read the findings, do not count them.** The scaffolding case is exactly why: the count fell
   and nothing was lost. For each finding present in control and absent in the treatment arm,
   decide by hand whether it moved (`action_items`) or vanished.
4. **Never re-extract through prod.** `extract_session` writes the real `extraction_key`, which
   fires item-writer's delete-then-insert and mutates a real customer's Aurora topics.

## Rollout

No flag. A one-line instruction change with no schema, no migration, no new field and no new
consumer has nothing to roll back except itself, and `EMIT_EVIDENCE` — which this no longer
depends on — stays off.

That is a deliberate reversal of v3's rollout section: a toggle is worth its cost when the change
has a persistence or contract surface. This one has neither, and this repo has shipped a toggle
that could only ever take its default while reporting success the whole way.
