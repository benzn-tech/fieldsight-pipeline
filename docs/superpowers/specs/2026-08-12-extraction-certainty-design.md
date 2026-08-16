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
`lambda_extract_session.py:834` already says findings "may be empty arrays". The pressure comes
from instruction 4, **"capture EVERY notable observation/issue"** (`:801`), which has no floor. The
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

**~~Group extractions persist unverified citations today.~~ FIXED — PR #394, already merged.**
`build_group_prompt` reuses `_instructions_block()` verbatim, so with `EMIT_EVIDENCE=true` the
group prompt asked for evidence, but `verify_evidence` returned early for `TIER_GROUP` **before**
the child-strip loop — so `_evidence_payload` wrote `{"status": None, "quotes": [...]}` into
Aurora, the exact defect the child-strip exists to prevent, on the path where meetings matter most.
`verify_evidence` now strips topic and child citations before returning for a group
(`lambda_extract_session.py:342-348`), covered by `tests/unit/test_group_evidence_strip.py`.

Left here in struck-through form deliberately: v5 asserted this in the present tense **after** the
fix had merged in this same branch's history. A spec that keeps claiming a fixed bug is live sends
the next reader to hunt something that is not there.

## v5 — the acceptance runs

The harness is `scripts/extraction_ab.py`. ~150 calls in four configurations: prod's config, test's
config, the group merge prompt, and a matched-content language pair.

**This section was rewritten after an adversarial review found the first version selective.** The
original table showed five of six sessions and omitted the one where the change looks worst. Both
that session and the corrections are below; the omission is recorded because it is the same failure
mode the rest of this document is about.

### Which model, in which environment

Read from the live deployments, not from the repo — the provider is env-driven, and a measurement
of the wrong model would be worthless:

| | provider / model | temperature | `EMIT_EVIDENCE` |
|---|---|---|---|
| prod | `qwen` / `qwen3.7-max` | unset (provider default) | `false` |
| test | `qwen` / `qwen3.7-max` | **`0`** | **`true`** |

`EMIT_EVIDENCE` changes the *prompt*, not just post-processing, so **the two environments do not
send the same request** — and test is where this lands first. Both were measured. Every result row
now carries provider, model, temperature and the evidence flag; the first run of the harness
recorded none of that, which meant the data could not prove what produced it.

### Findings per run — all six sessions, prod config, non-thinking

Run order as executed, not sorted:

| session | baseline | with the line |
|---|---|---|
| 2026-08-08 `sid754a…` | 6, 4, 8 | 4, 4, 4 |
| 2026-08-12 `sid2c30…` | 3, 3, 7 | 3, 3, 4 |
| 2026-08-11 `sid5321…` | 4, 4, 4 | 3, 4, 4 |
| 2026-08-08 `sidb8bd…` | 2, 3, 4 | 2, 3, 3 |
| 2026-08-10 `sid97f0…` | 3, 3, 3 | 3, 3, 3 |
| 2026-07-31 `sidf832…` | 2, 3, 4 | **1, 2, 3** |

The honest summary is **not** "the spread collapses". On the two verbose sessions it does, markedly:
`sid754a…` goes from 6/4/8 to 4/4/4. On three it is flat. **On `sidf832…` the treatment is lower
across the board and one run returns a single finding** — that is the session the first draft left
out, and it is the one that argues against the change.

Why the 8-finding run shrinks, read rather than counted: it emitted three findings for one
situation — *"Grass seed sown and awaiting germination"*, *"Temporary timer-based irrigation system
installed by Gills"*, *"Newly seeded grass requires twice-daily watering in hot weather"*. The
treatment keeps the irrigation fact as a finding and files the watering requirement as an action,
which is one timeline row instead of three. (The first draft attributed a differently-worded
version of that quote to this run; it came from a different run of the same session.)

Thinking mode moves less than non-thinking everywhere, as expected — `enable_thinking` already
suppresses some of the same over-production.

### The one item worth arguing about

On `sidf832…` baseline produced *"Current Linen licensing model ($10k per 1–2 weeks) is
cost-prohibitive compared to the annual option ($5k)"*. **No non-thinking treatment run reproduces
those figures**, and the surviving action item (*"Linen annual license — procure for Benny"*) carries
no numbers.

The mitigation is real but partial: the figures **do** survive in the thinking arm (2 of 3 runs),
and prod's authoritative `final` pass runs thinking-on — the non-thinking `live` pass is the
provisional one that `final` supersedes. So the durable record keeps it. What degrades is the
in-progress view during recording. That is a genuine cost of this change, not a false positive, and
it is the strongest argument for revert if the timeline looks thin tomorrow.

### The analyser's own limits

`*** VANISHED ***` is a triage label, **not a verdict**. *"Rain has cleaned mud from new asphalt
surface"* was flagged while the treatment says *"Asphalt surface is trafficable and clean after
rain"* — the same fact, scoring 0.375 against a 0.40 threshold. It narrows what a person reads; it
does not decide. Every flagged item was read by hand.

### Test's configuration, measured separately

`EMIT_EVIDENCE=true`, `temperature=0`, non-thinking:

| | baseline | with the line |
|---|---|---|
| `sid754a…` | 6, 7, 7 | 4, 4, 5 |
| `sidf832…` | 2, 3, 3 | 2, 2, 2 |
| `lang_pair_en` | 4, 5, 5 | 5, 5, 5 |
| `lang_pair_zh` | 5, 5, 5 | 5, 5, 5 |

Same direction as prod's config, and notably *stabler* on the verbose session. The evidence
instructions do not interact badly with the new line.

### The group merge prompt — shipped by this change, so measured

`build_group_prompt` reuses `_instructions_block()` verbatim, so this line changes multi-device
merges too. It is not reachable from a session id (it needs a claim artifact), so two fixture
recordings of one meeting stand in for two body-worn recorders. `extract_group` always calls with
`enable_thinking=True`.

| | baseline | with the line |
|---|---|---|
| group (2 devices) | 4, 4, 6 | 5, 6, 6 |

No collapse on the merge path, and content only device 2 heard (*"scaffold tagged out of service"*)
still survives the merge — which is the property the extra recorders exist for.

### Both languages — the bucket could not answer this

The one day in the bucket with any CJK (2026-07-31) is **English speech containing a few Chinese
names — 51 CJK characters total**. Calling that the both-languages arm would have been the
"verified the wrong thing" failure again. Uploading a Chinese transcript was not an option either:
`transcripts/` is the live production trigger for extract-session.

So `--turns-file` feeds turns to the *real* `build_extraction_prompt`, with a matched pair of
**synthetic** fixtures — the same conversation in Chinese and English:

| | baseline | with the line |
|---|---|---|
| `lang_pair_zh`, non-thinking | 5, 5, 5 | 5, 4, 5 |
| `lang_pair_en`, non-thinking | 4, 4, 4 | 4, 5, 5 |
| `lang_pair_zh`, thinking | 4, 4, 4 | 3, 4, 4 |
| `lang_pair_en`, thinking | 4, 4, 4 | 3, 4, 4 |

The two languages behave **identically**, in both thinking modes. Nothing is erased on the CJK side.

**What this fixture cannot catch, stated plainly:** it is 16 scripted turns where every line is a
clean observation or action, author-translated so the pair is informationally identical by
construction. It is the *easiest* case for a line whose real risk is over-suppression on sparse or
ASR-noisy input — which is exactly what the incident was. It does not cover noisy Chinese,
code-mixed speech, or long CJK transcripts against the char-based `TRANSCRIPT_TEXT_LIMIT` (CJK packs
far more content per char). What it legitimately shows: the line does not zero out findings on a
clean Chinese transcript, and does not treat the two languages differently. (Separately and
pre-existing: findings come back in English for Chinese input. This line does not change that.)

### `decisions` / `questions`: measured, and not shipped here

They have no readers — `item_writer` persists neither, `chunking` embeds neither; the
`key_decisions` those modules read belongs to the *report* artifact, a name collision. Across 84
runs `decisions` is non-empty in **81** and `questions` in **22**, so this is real output token cost
on a path with a 47%-truncation history.

Deleting them is not free. All four sessions measured, non-thinking:

| session | baseline | `no_qd` | `no_invent` | both |
|---|---|---|---|---|
| `lang_pair_en` | 4, 4, 4 | 5, 5, 5 | 4, 5, 5 | 5, 5, 5 |
| `lang_pair_zh` | 5, 5, 5 | 5, 5, 5 | 4, 5, 5 | 5, 5, 5 |
| `sid754a…` | 4, 6, 8 | 5, 6, 7 | 4, 4, 4 | 4, 4, 5 |
| `sid2c30…` | 3, 3, 7 | 3, 3, 4 | 3, 3, 4 | 3, 3, 4 |

`no_qd` rises on one, is unchanged on one, has an identical mean on one, and falls on one. It moves
findings **without a consistent direction** — an uncontrolled behavioural change bought for a token
saving. **Not shipped here.** Wiring them up or dropping them stays a separate decision with its own
measurement; what is now established is that "they're unused, just delete them" is false.

### Sample limits

All six real sessions are **one user (`Ben_UCPK2`), one week, all short** — 9–14k prompt chars, and
`truncated=False` in every run. **The truncation and elision path was never exercised**, so
"≥5 unrelated historical sessions" is met in letter more than in spirit. A long multi-hour session
remains unmeasured.

## Acceptance

**A prompt change cannot be pinned by a unit test** — the code path is identical and every
existing test passes either way. The instrument is the harness, and the harness must import
`assemble_session_turns`, `build_extraction_prompt` and the real payload builder rather than
re-deriving them, or it measures a request prod never sends.

Required before shipping:

1. **≥5 unrelated historical sessions**, ≥3 runs per arm, **both thinking modes** — prod runs
   non-thinking for the live pass and thinking for the final (`:1553-1554`), so pinning one leaves
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
