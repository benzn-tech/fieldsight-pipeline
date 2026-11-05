# Action pack quality fixes — design

Date: 2026-11-05. Status: rev 2.
- Rev 1 was reviewed by Fable: approve with changes.
- Rev 2 folds in all 13 required changes, the two decisions, and the optional changes the
  coordinator adopted. §17 lists what changed from rev 1.
- An implementation plan follows this spec.

Builds on: `docs/superpowers/specs/2026-11-01-background-action-agent-design.md` (PR #1207,
"the 11-01 spec") and its plan `docs/superpowers/plans/2026-11-01-background-action-agent.md`
(implementation #1210). Section numbers like "§4.4" or "S6" refer to the 11-01 spec.

The eval that measured the defects ran on TEST against a 24-instruction gold set (gold v1).
The harness is on a local-only branch, so observations taken from it are marked "(eval
branch)". Gold instructions are referred to by id only (C1–C5 close, U1–U5 update, K1–K4
create, S1–S4 standards, R1–R4 refuse/delegate, M1–M2 mixed). This document names no people,
sites or quotes from the gold set.

Decisions marked **my call** were made by the main agent on the owner's standing instruction
to choose the long-term reasonable option. **[decided, review]** marks a decision made in
the rev 1 review. The owner can overturn any of them.

---

## 1. Why

### 1.1 What the eval measured

The §8 gates are: fabrication ≤ 5% of shown elements, wrong target = 0, recall ≥ 60% of the
27 expected actions (so at least 17 of 27), and p90 ≤ 300 s. Ten configurations ran 24
instructions each. Final labels were set after a blind second-annotator review.

- **No configuration passes all four gates.** The two best both fail on recall only:

  | Writer → verifier | Fabrication (shown) | Wrong target | Recall (shown) | p90 |
  |---|---|---|---|---|
  | Opus 5.5 → Muse | 1.9% | 0 | 13/27 | 114 s |
  | Muse → Opus 5.5 (rerun) | 3.8% | 0 | 13/27 | 258 s |

- **The chosen configuration is not stable on fabrication either.** Across its two runs and
  the two Muse → Sonnet runs, shown fabrication was 7.2%, 3.8%, 8.2% and 7.1%. Wrong target
  was 1, 0, 1 and 1.
- **Run-to-run noise.** Between reruns, 5 of 24 instructions flip between the writer
  refusing and the writer acting. Recall moves 7–11 points, which is 2–3 expectations.
- **Model choice going forward.** The writer is `meta/muse-spark-1.3-contributor` (OpenRouter)
  and the verifier is `claude-opus-5-5` (Claude API only, never OpenRouter). That costs about
  $1.3 per 24 packs, with p90 around 255 s. A Muse writer at reasoning=high is being measured
  now, so the writer's reasoning level stays a config knob (§8).

All eleven defects are in the pipeline: they appeared with every writer/verifier pair.

### 1.2 Where the missing recall and the fabrications are

**Recall.** These expectations were missed in **all four** Muse-writer runs (shown):

| Gold ids | Count | Defect |
|---|---|---|
| M1/e1, M1/e3, M1/e4, M2/e4, M2/e5 | 5 | Mixed instructions lose their sub-asks (#4) |
| S2/e1, S4/e1, S4/e2 | 3 | A standards answer does not become a `note` (#5) |
| R3/e1 | 1 | A delegated missing due date is refused or set to today+N (#3) |

C3/e1 and U4/e1 were missed in 3 of the 4 runs, and S3/e1 and C2/e1 in 2. No defect below is
attributed to C3, C2 or U4 (Q6).

**Fabrication.** In the Muse-writer runs, almost every element labelled "not supported"
belongs to one of these four groups:

| Pattern | Per run | Defect |
|---|---|---|
| M1: `update_item` on `status` and/or `priority` of items nobody asked about | 5–6 | #1 |
| S3/S4: `update_item status` on an item the user did not mean | 1–2 | #6 |
| U2 (and one M2 run): a deadline anchored to today, not to the date it was said | 0–1 | #2 |
| C4: a close or note aimed at the newer look-alike item instead of the named one | 0–1 | #6 |

**The levers.**
- **Change 1 is the fabrication lever.** If it removes the first two groups and Change 4 the
  third, raw writer fabrication in those runs falls from 7–9% to roughly 1–3%.
- **The verifier stays a second line.** In the rerun of the chosen configuration it removed
  half of the raw fabrications (8 raw, 4 shown) and the only raw wrong target. That is why
  its hiding policy does not change in this round (§7).

**Value-aware recall will lower today's baseline.** U2's action was labelled "not
supported" (it had the wrong date), but it still counted as a recall match in 3 of the 4 runs,
because recall does not check values today. Under value-aware recall (§10.1), the chosen
configuration's baseline is about 12–13 of 27, not 13–15.

### 1.3 Goals

- **Q1.** The chosen configuration passes all four gates (§1.4).
- **Q2.** Rules that keep the writer from fabricating are **enforced in code** wherever a
  rule can be checked without a model. A prompt sentence is not a guard. An element that
  breaks such a rule is dropped or demoted, and the drop is counted.
- **Q3.** Every sub-ask of an instruction is accounted for: done, partly done, or not done
  with a reason.
- **Q4.** The verifier gets the inputs it lacks: the user's stated values and the dates of the
  evidence. It parses answers tolerantly and records which model judged each element. Its
  hiding policy is unchanged in this round; a switch prepares the later move to flagging
  (§7, §12).
- **Q5.** The writer's model, reasoning, provider pin and fallbacks are configured
  independently of the Ask judge. TEST and prod spend from separate OpenRouter credentials.

### 1.4 Success definition and iteration loop

**Success.** On **gold v2** (§10.3), the chosen configuration passes all four gates in **two
consecutive runs**:

- Labels come from the blind second-annotator process (§10.5).
- Recall is value-aware (§10.1).
- Fabrication counts every shown element.

A single passing run is not success, because the noise floor (§1.1) is bigger than the
margin in most single-run comparisons.

**Iteration loop.** Once every PR in §13 is on TEST:

1. **Measure.** Do one full v2 run. Read the gates, the per-pack `scope_gate` summary and the
   sub-task coverage.
2. **One tuning round.** Make prompt wording, threshold or knob changes only (for example
   `TARGET_MATCH_MIN`, `ACTION_MAX_ROUNDS`, `ACTION_WRITER_REASONING`). Add no new mechanism.
3. **Two consecutive runs.** Both must pass.

If step 3 fails, stop and report the gap with its traces. A second tuning round against the
same 24 instructions would be overfitting.

### 1.5 Non-goals

- No new action types, no auto-apply, no outbound sends: G3 and A5 of the 11-01 spec stand.
- **No change to applicability.** `unsupported` stays non-applicable, and so does a check
  the verifier could not complete. The §4.4 rule stands in this round (§7.2).
- No change to the S5 web-query isolation. Web queries are still written in Plan from the
  instruction only.
- No change to apply semantics (§5 of the 11-01 spec).
- No UI work in this repo. The UI session gets a contract hand-off (§9.3).
- No attempt to fix C2, C3 or U4 by guesswork (Q6).
- No gold-set expansion beyond the v2 edits in §10.3 (Q10).
- Not in this round: the deterministic round-1 web search, the cross-element consistency
  check, the switch from hiding to flagging, and the sub-task repair call (§12).

---

## 2. Changes in impact order

| # | Change | Defects | Gate it moves | Gold ids it should recover or protect | PRs (§13) |
|---|---|---|---|---|---|
| 1 | Change scope and field provenance, enforced in code | #1, #3, #6 | Fabrication, wrong target (+ recall via R3) | M1 and S3/S4 fabrications; C4, S4 wrong target; R3/e1 | PR-3, PR-4 |
| 2 | Sub-tasks: the planner splits, the writer accounts | #4 | Recall | M1/e1, M1/e3, M1/e4, M2/e4, M2/e5 | PR-3, PR-5 |
| 3 | Standards answers as notes; notes survive the schema | #5, #9 | Recall | S2/e1, S3/e1, S4/e1, S4/e2 | PR-6 |
| 4 | Dates and the item view: said-on dates, `deadline_text`, raised-on date | #2, #8 (+#3 data) | Recall (value-aware), fabrication | U1/e1, U2/e1, R3/e1 | PR-2 |
| 5 | Verifier inputs and records (hiding policy unchanged) | #7 | Fabrication (better judgements), recall (fewer `not_checked`) | — | PR-2 (ref dates), PR-7 |
| 6 | Writer and verifier configuration; separate OpenRouter keys | #10, #11 | p90 (reasoning knob, round cap) | n/a (enabler) | PR-1, owner step |

### Expected recall

The value-aware baseline is about 12–13 of 27. Conservative gains per change:

| Change | Expected gain | Why |
|---|---|---|
| 2 | +2 | of 5; no repair call |
| 3 | +1–2 | of 4; the note rule only; S3/S4 depend on the model rounds choosing their searches, which they did on the Muse arms |
| 1 and 4 together | +1–2 | R3, and U2 with the right value |
| 5 | +0–1 | Fewer checks lost to parse failures |

**Central estimate: about 16–18 of 27.** The gate needs 17, so the central case is at the
gate, not above it. That is why the iteration loop (§1.4) and its single tuning round exist.

**Downside.** Change 1 drops or demotes every named close or update whose target does not
resolve. If target resolution falls short of the floor in §3.3, the correct closes and updates
on C1–C3, U1–U4 and M2 are at risk: about 2–4 expectations, so 13–15 of 27, a fail. For this
reason the resolution floor is a blocking gate, measured **before** the scope gate enforces
anything (§3.3, PR-3).

---

## 3. Change 1 — change scope and field provenance (defects #1, #3, #6)

### 3.1 Current behaviour

**Writer prompt.** `action_pack_stages.WRITE_PROMPT` lets the writer target any item in the
evidence. It asks for "at most one action per item", but only the code layer below enforces
that.

**`validate_draft` (`action_pack_stages`)** checks:
- the action keys and the type enum;
- at least one citable ref;
- the rationale;
- the shape of `proposed` per type, through `_proposed` (including the priority values apply
  accepts);
- that a close/update target is a uuid present in `run.items`;
- that the status transition is legal (`legal_change`);
- the `MAX_ACTIONS` cap.

For conclusion lines it checks the text, the refs and the confidence.

**`action_pack_verify._code_check_action`** adds:
- every ref is in the registry;
- the target is on the pack's site (`drop_target_off_site`);
- **at most one close/update per target** (`drop_duplicate_target`). This layer owns the
  one-action-per-item rule;
- near-duplicate creates are dropped (exact) or capped at weak (fuzzy).

**What none of these checks.**
- *Why* a field gets its value. An `update_item` that sets `priority: high` on an item the
  instruction never mentioned, citing only that item's own row, passes. So does an invented
  deadline.
- *Which* item the user meant. There is no notion of a named target. An item named by text is
  matched by the writer alone, so C4's newer look-alike is indistinguishable from the named
  item in code.
- `legal_change` gates only the `status` field. A `deadline` or `priority` change on a done
  or closed item passes it.

**Delegated missing due dates (R3).** The writer either refuses (draft RFI) or picks today+N.
It cannot see when the item was raised (§6.1).

### 3.2 The change

#### 3.2.1 The planner states the scope

The Plan schema (`PLAN_PROMPT`, `_PLAN_KEYS`, `validate_plan`) gains these keys.

| Key | Shape | Meaning |
|---|---|---|
| `change_scope` | `"named"` or `"tidy"` | `tidy` only when the instruction delegates bulk changes to open items ("tidy up the open items", "close whatever is finished"). |
| `targets` | `[{"id": "t1", "task_no": int or null, "phrase": str or null}]`, ≤ 10 | Items the instruction names, by task number or a short phrase (≤ 200 chars). |
| `stated_values` | `[{"target": "t1" or null, "field": "deadline"/"responsible"/"priority"/"status", "value": str}]`, ≤ 10 | Values the user gave. A deadline is ISO, resolved against today (the instruction is spoken today). |
| `delegations` | `[{"target": "t1" or null, "field": "deadline"}]`, ≤ 10 | The user delegated a missing value ("if there is no due date, set one"). v1 supports `deadline` only. |
| `sub_tasks` | §4.2 | |

**Tolerance.** `validate_plan` is strict today (exact key set). The new keys are tolerated:
- An absent new key is treated as empty.
- A malformed new key is replaced by empty and counted (`plan_key_defaulted__<key>`) instead
  of rejecting the plan.
- A missing or invalid `change_scope` is `named`.
- `run_trace.plan_scope` records `named` or `tidy`, so the eval can count tidy plans.

The planner still sees no evidence (S5). It can only resolve what the instruction says.

#### 3.2.2 Gather resolves the targets in code

**Lookup calls.** Round 1 adds one items-tool call per planner target, with at most 4 such
calls:
- `task_no` for a numbered target;
- `query` = the phrase for a text target.

Each call passes an **explicit status list covering every status**: `open`, `in_progress`,
`blocked`, `done`, `closed`. Otherwise an item the user named that is already done would
resolve to a newer open look-alike, which is C4's failure reproduced in code.

**These are separate invokes.** `round1` sends them alongside its own calls (day overviews,
the site's open items, documents). They are not inside the existing round-1 batch, and they
have their own time budget (§11).

**Matching**, in this order:

1. **Task number.** A `task_no` that matches resolves the target. A numbered target never
   falls through to text matching.
2. **Phrase containment.** Normalise both the phrase and each candidate item text with the
   same rules the near-duplicate code uses (`todo_collapse.normalise`, then
   `carry_forward._key`). Tokenise: words for Latin text, single characters for CJK runs.
   Drop a short fixed stop-word list.

   The score is the share of the phrase's tokens found in the item's tokens. It is computed
   over the phrase query's results **plus** every item in `run.items`, because the ILIKE
   query misses reworded text.
3. **Threshold.** `TARGET_MATCH_MIN` is 0.8 to start. It is tuned by the PR-3 gate (§3.3)
   and may be changed once in the iteration loop (§1.4).

**Why not `near_duplicate`.** Rev 1 used it as the fallback, but it cannot fire here.
SequenceMatcher at ≥ 0.90 compares whole texts, so it can never match a short phrase against
a full item text.

**Each target then resolves to one of:**

| Result | When |
|---|---|
| `resolved(stable_id)` | A `task_no` matches; or exactly one item scores ≥ `TARGET_MATCH_MIN` and no other item scores within 0.15 of it. |
| `ambiguous([stable_id, …])` | Two or more items score ≥ `TARGET_MATCH_MIN` within 0.15 of the best. |
| `none` | No item scores ≥ `TARGET_MATCH_MIN`, or the lookup ran out of time. |

**What the writer and the trace see.**
- The writer's render shows each target with its result and the matched item's status. For
  example: `t1: AMBIGUOUS — R4, R9: write a note, change neither` or
  `t2: R7 (done) — already done`.
- `run_trace.target_resolution` records `{"t1": "<stable_id>" | "ambiguous" | "none"}`: ids
  and codes only, and counts-shaped (a uuid passes `_TRACE_CODE`).

#### 3.2.3 The writer declares where each field comes from

Every `close_item` and `update_item` gains two keys:

- `target_ref`: a planner target id, or null when unnamed.
- `field_sources`: one entry per proposed field:
  `{"<field>": {"source": "user" | "evidence" | "assumed_default", "refs": [..], "said_on": "YYYY-MM-DD" or null}}`.

`create_item` gains `field_sources` for its optional `deadline`, `responsible` and `priority`.
The text still needs refs, as it does today. `WRITE_PROMPT` states the rules in §3.2.4 and
adds: "change only items the instruction names, unless it asks you to tidy open items. For
anything else worth changing, write a note."

#### 3.2.4 A code gate after `validate_draft`

The gate is new, in `action_pack_stages`. Every rejection is counted in
`run_trace.scope_gate` under the codes below.

**Rule 1: target scope.**
- Every close or update target must have a status in `OPEN_STATUSES` (open, in_progress,
  blocked), whatever the scope and whatever the field. `legal_change` gates only `status`,
  so this rule is what stops a deadline or priority change on a done item. A named target
  that is done or closed gets a note saying it is already done (`demoted_target_closed`).
- Under `change_scope = "named"`, a close or update whose target is not the resolved
  `stable_id` of a planner target is dropped (`drop_unasked_target`).
- Under `tidy`, any open item on the pack's site may be targeted, but rule 3 still applies to
  every field.

**Rule 2: named target mismatch.**
- A close or update whose `target_ref` names a target that resolved to a **different**
  `stable_id` is dropped (`drop_named_mismatch`). This is C4's look-alike.
- A close or update on an `ambiguous` target is **demoted** to a `note` (`demoted_ambiguous`).
  The note's text is the rationale plus "matches more than one item: R4, R9", and its refs are
  kept.
- A close or update for a named target that resolved `none` is demoted to a `note` the same
  way (`demoted_target_none`). It is never dropped silently.

**Rule 3: field provenance.** Each proposed field must pass its source's rule.
- A field that fails is removed from `proposed` (`field_unsourced__<field>`).
- An update left with no fields is dropped (`drop_no_sourced_fields`).

| Source | Rule |
|---|---|
| `user` | A `stated_values` entry must exist for that target and field (or a global one). **The code sets the value to the planner's stated value.** If the writer's value differs, it is counted (`user_value_overridden__<field>`), and the field is kept. |
| `evidence` | At least one ref of kind `topic`, `chunk` or `document`. The target's own `item` ref does not count, because an item row cannot justify changing itself. The one exception is a `deadline` taken from the item's own `deadline_text` (§6.2). Web refs never set a project field (A4). |
| `assumed_default` | Only for `deadline`; only when a delegation covers the target; only when the item has neither `deadline` nor `deadline_text`. **The code sets the value** to the item's `raised_on` (§6.1), whatever the writer proposed. The action carries the `date_assumed` marker (shown as the existing `weak`/"uncertain" badge plus the reason in `verifier_detail`, §7.2). |

**Same-day default.** When `raised_on` is the day of the instruction, the default is still
`raised_on`, which is today. Nothing is added, and the action is still marked assumed. The
rule is the owner's: the raised date, never today+N.

Closing follows the same rule. A `status` of `done`/`closed` needs a stated value or evidence
that the work is finished.

**Rule 4: deadline sanity.**
- An `evidence` deadline must carry `said_on` equal to the date shown on one of its refs
  (`field_said_on_mismatch` otherwise).
- If the deadline is before that date, the field is removed (`field_deadline_before_source`).

**Owner rule for delegated missing due dates (#3).** When the user delegates a missing due
date, the default is the date the item was raised, marked as assumed. Code enforces this
through `assumed_default`.

### 3.3 Acceptance

**Unit tests** (deterministic):
- An M1-shaped draft under `named`: status/priority updates on unnamed items are all dropped.
- A C4-shaped draft: the look-alike `stable_id` is dropped.
- A done named target: the action becomes a note.
- An ambiguous phrase and a `none` phrase: both become notes.
- A `user` field with a different writer value: the stated value wins, and the override is
  counted.
- An R3-shaped delegation: the deadline is replaced by `raised_on`, including when
  `raised_on` is today.
- An absent or malformed new plan key, and a missing `change_scope`: the plan is still
  accepted (scope `named`, override counted).
- Phrase containment: a short phrase matches the full item text; two items equally close are
  `ambiguous`; CJK text matches.

**Blocking gate: target resolution (PR-3, measured before PR-4 enforces anything).**
- **What.** PR-3 records `run_trace.target_resolution` without enforcing it. Run C1–C5,
  U1–U5, R3 and M2 three times each on TEST. Score each planner target against gold:
  - the gold `target_stable_id` of the expectation it names;
  - for C4/C5/U5, which have no expectation, that it does not resolve to a `must_not_touch`
    item.
- **Floor.**
  - ≥ 90% of expectation targets resolved to the gold `stable_id`, pooled over the three runs.
  - **Zero** resolved to a wrong `stable_id`; `ambiguous` and `none` count as not resolved,
    not wrong.
  - No `must_not_touch` resolution on C4/C5/U5.
- **On a miss.** Tune `TARGET_MATCH_MIN`, the tokeniser or the planner phrase rule, and
  re-measure. PR-4 does not merge until the floor is met.

**Targeted runs after PR-4.** Run M1, S3, S4, C4, U2 and R3, three runs each on TEST:
- no shown `update_item` on an M1 `must_not_touch` item;
- zero wrong targets on C4 and S4;
- R3/e1 matched with the right value (its `raised_on`) in at least 2 of 3 runs.

**Full gold v2:** shown fabrication ≤ 5% and wrong target = 0 in both gate runs (§1.4).

### 3.4 Risks

- **Lost recall from over-strict sourcing.** A correct update whose evidence the writer
  cites badly is removed. Mitigation: `scope_gate` counts by code and appears per pack in the
  eval report (§10.6), so it is visible which removed fields were right. A field-level
  removal keeps the rest of the action.
- **Unresolved text-named targets.** These become notes, and the close or update recall is
  lost. Mitigation: the blocking floor above.
- **Planner mis-scoping.** A `tidy` instruction read as `named` drops legitimate closes, and
  M2 is the gold case for this. Mitigation: `change_scope` is shown in the reading,
  `plan_scope` is in the trace, and M2 is a targeted case.
- **`stated_values` is model-extracted user text.** It is compared or copied as a value,
  never followed as an instruction. It is the caller's own instruction, so there is no new
  trust boundary.

---

## 4. Change 2 — sub-tasks (defect #4)

### 4.1 Current behaviour

- The Plan stage returns a single `reading` (`PLAN_PROMPT`).
- `WRITE_PROMPT` gets the reading and the instruction, with no structure.
- Nothing checks that every ask in the instruction was handled. M2 (five asks) gets 3 of 5 in
  every run, and M1 (four) gets 1 of 4. Nothing in the pack tells the user what was skipped.

### 4.2 The change

**Plan.** The plan gains `sub_tasks`, with at most 8 entries:

```json
[{"id": "s1", "ask": "short paraphrase, ≤ 200 chars, instruction language",
  "kind": "close"|"update"|"create"|"review"|"standards"|"draft"|"other",
  "targets": ["t1", ...]}]
```

- A single-ask instruction has one sub-task.
- The reading lists the sub-tasks.
- An absent key is treated as empty (§3.2.1). With no sub-tasks, nothing is accounted.

**Write.** The writer output gains `sub_tasks`:

```json
[{"id": "s1", "status": "done"|"partly"|"not_done",
  "actions": [indices], "conclusion": [indices],
  "reason_code": "no_evidence"|"not_found"|"ambiguous_target"|"out_of_scope"|"already_done"|"other",
  "reason": "one sentence, ≤ 300 chars"}]
```

The prompt requires one entry per planner sub-task, and at least one action or conclusion
index for `done` and `partly`.

**Code checks** (in `validate_draft` and the scope gate):
- Every planner sub-task appears exactly once. Unknown ids are ignored and counted.
- Indices are remapped through the drops and demotions of §3.2.4.
- A `done` or `partly` whose actions were all dropped becomes `not_done`, with reason code
  `dropped_by_checks`.
- A planner sub-task the writer left out becomes `not_done`, with reason code `not_answered`.

**No repair call. [decided, review]** A missing sub-task is reported, not retried. The
deferred design is in §12.

**Stored** on a new `action_packs.sub_tasks` jsonb (§9). GET returns it. Action references
are stored as action `seq`s after renumbering.

### 4.3 Acceptance

- Unit tests:
  - a missing sub-task becomes `not_answered`;
  - a `done` sub-task whose actions were dropped becomes `not_done`;
  - a plan without `sub_tasks` stores `null`.
- Targeted M1 and M2, three runs each:
  - every planner sub-task is present in the stored pack;
  - M2 ≥ 4 of 5 and M1 ≥ 2 of 4 in at least 2 of 3 runs (value-aware).
- p90 stays ≤ 300 s in both full gate runs (§11).

### 4.4 Risks

- **Over-splitting.** A planner that splits one ask into three makes the writer pad.
  Mitigation: the cap of 8, and `already_done`/`out_of_scope` are acceptable answers.
- **Without a repair call, a sub-task the writer skips is only reported, not done.** The
  pack says so; that is the honest floor for this round.

---

## 5. Change 3 — standards answers as notes; notes survive the schema (defects #5, #9)

### 5.1 Current behaviour

**Web search is model-chosen only.** `round1` runs day overviews, items and documents, never
web. `web_search` is reachable only when a model round picks it (`_tool_catalog`,
`validate_round`, `_web`). What the runs showed (eval branch):

- On the Opus-writer arm, S1–S4 planned 1–3 queries each and none was searched.
- On the Muse arms, S3/S4 searched every planned query.
- S1/S2 (standard in the library) correctly planned none.

For the chosen Muse writer the searches ran. A deterministic web step is deferred (§12).

**The writer has no rule for the form of a standards answer.** In the Muse arms:
- S2 (in the library) became `create_item`s;
- S3/S4 became `draft_rfi` or an `update_item` (counted under #6);
- the expected `note` appeared only sometimes.

**Note drops.**
- `_DRAFT_FIELDS["note"]` requires `proposed` to be exactly `{"text"}`, with text ≤
  `CREATE_TEXT_MAX_CHARS` (2,000). A longer answer, or any extra key, drops the whole
  action.
- `validate_draft` also drops an action that carries any unexpected top-level key.
- `run_trace.write_dropped` already has reason buckets (`refs`, `targets`, `transitions`,
  `priority`, `conclusion`, and `actions` for every other schema failure). What it lacks is
  the **action type**, so a dropped note cannot be told apart from any other type.
- In the Muse arms writer-schema drops were 0. They were 2–7 per run on other writers.

### 5.2 The change

**Standards answers are notes.** `WRITE_PROMPT` adds this rule. When the instruction asks
what a standard, code or procedure requires, or whether something complies, the answer is a
`note` action:
- `proposed.text` is the answer;
- the clause, page or heading goes in when the evidence gives it;
- refs: the `D` refs, or the `W` answer and its sources.

Two other types are allowed only in narrow cases:
- `create_item`: only when the instruction asks for a task, or the evidence shows work still
  to do.
- `draft_rfi`: only when the instruction asks for an RFI, or the answer needs a party outside
  the team.

Several short notes are allowed, one per question.

**Web-only notes.**
- They stay capped at `weak` (A4), and the UI's existing "web source, not your project's
  documents" label applies.
- `verifier_detail.reason_code` records `web_only`, or `not_archived` when no page was
  snapshotted.
- D6 is unchanged: an unverified `web_answer` is never citable.

**Notes survive the schema.**
- `note.proposed.text` accepts up to `BODY_MAX_CHARS` (4,000).
- An unknown key inside `proposed` or on the action is removed and counted
  (`extra_key__<type>`) instead of dropping the element. Known keys (`target_ref`,
  `field_sources`, sub-task indices) are part of the schema now.
- `run_trace.write_dropped` keys become `<type>__<reason>`, for example
  `note__text_too_long`, `update_item__targets`, `create_item__proposed`. Conclusion drops
  keep `conclusion__<reason>`. The harness already sums `write_dropped.*`.

### 5.3 Acceptance

- Unit tests:
  - a note with a 3,000-character text and one stray key is stored, with the key counted;
  - `write_dropped` keys carry the type.
- Targeted S1–S4, three runs each:
  - S2, S3 and S4 each produce at least one shown `note` that matches their expectation in at
    least 2 of 3 runs;
  - S1 still yields its `create_item`.

### 5.4 Risks

- **S3/S4 depend on the round model choosing the planned searches.** It did on every Muse
  run, but nothing guarantees it. If the targeted runs show a skipped search,
  §12's deterministic web step is the next change.

---

## 6. Change 4 — dates and the item view (defects #2, #8, data for #3)

### 6.1 Current behaviour

**`deadline_text` is hidden at every layer, not only in the writer's render.**
- `repositories/topics.py:_scoped_items` does not select `a.deadline_text`.
- `ask_items.ITEM_FIELDS` has no such key.
- `action_pack_stages._line` (the item branch) renders status, deadline, responsible and
  priority only.
- So an item whose deadline exists only as text (migration 0011: the date column cannot
  hold "end of next week") reads as having no deadline. That is U1.

**The writer cannot see when an item was raised.**
- `_scoped_items` returns `t.report_date`, but `_line` does not render it.
- That `report_date` is the **newest** live row's topic date, because the query uses
  `DISTINCT ON (stable_id)` ordered by `created_at DESC`. For a carried item it is the latest
  re-extraction date, not the date it was first raised.

**Record lines carry a date, but nothing tells the writer to use it.**
- Topic and chunk lines go through `ask_orchestrator._item_line`, which is shared with Ask.
  It renders `date time` with no weekday.
- The writer prompt shows `Today (New Zealand)` once, and has no rule that a relative
  expression resolves against the date it was said. So the writer anchors to today (U2 every
  run; R3; M1).

**The planner cannot anchor spoken dates.** It never sees evidence (S5). It can only resolve
the instruction's own relative dates, which are correctly relative to today.

**The verifier cannot catch a wrong anchor.** `action_pack_verify.build_prompt` renders each
ref as `ref | kind | text`, with no date.

### 6.2 The change

**Items tool.**
- `_scoped_items` adds `a.deadline_text` and `raised_on`.
- `raised_on` is the earliest `report_date` across **every** `action_items` row sharing the
  `stable_id`, whatever its visibility or supersession. It is a date of the same item on the
  same site, so it exposes no content. Builder check: the subquery's cost inside the 20 s
  rag-search hop. With a supporting index this is cheap; `action_task_numbers.created_at`
  is not a substitute, because it is wrong for backfilled numbers.
- `ask_items.ITEM_FIELDS` adds both keys.
- `SNAPSHOT_FIELDS` does not change: neither key can be patched.

**Render (Gather and Write).**
- The agent gets **its own** topic/chunk line renderer in `action_pack_stages`. It prints
  `said_on YYYY-MM-DD (Wkd)` in place of the bare date. `ask_orchestrator._item_line` is not
  touched: Ask shares it.
- Item lines (`_line`) add `deadline_text "<text>"` (when set) and
  `raised_on YYYY-MM-DD (Wkd)`.
- The prompt header says `Today (New Zealand): YYYY-MM-DD (Weekday)` and adds "today is not
  the date the records were said".

**Write rules.**
- A relative date in a record resolves against that record's `said_on`.
- An item's `deadline_text` resolves against its `raised_on`. That is the one case where the
  item's own ref may source `deadline` (§3.2.4 rule 3).
- The writer fills `field_sources.deadline.said_on`, and code checks it (§3.2.4 rule 4).

**Verify.**
- `build_prompt` renders each ref's date and weekday. For a deadline it also renders the
  claim's `said_on`.
- A deadline that does not follow from its anchor is labelled `unsupported` with reason code
  `date_anchor` or `contradicted`.

**No regex date parsing.** Spoken dates are the model's job. Code only checks that the anchor
the writer names is a real ref date.

### 6.3 Acceptance

- Unit tests:
  - `deadline_text` and `raised_on` reach the item line;
  - `raised_on` is the earliest row's date for a carried item, including a superseded or
    hidden earlier row;
  - Ask's `_item_line` output is byte-identical before and after.
- Targeted U1, U2 and R3, three runs each: each expectation is matched **with the right
  value** (value-aware) in at least 2 of 3 runs.

### 6.4 Risks

- **The `raised_on` subquery slows the items hop.** Mitigation: the builder measures it and
  adds an index if needed.
- **Weekday text in the render costs tokens.** About 6 characters per line, which is
  negligible.

---

## 7. Change 5 — verifier inputs and records (defect #7)

### 7.1 Current behaviour

**Labels** (`action_pack_verify.verify`) are `supported`, `weak` and `unsupported`.
- An `unsupported` action is stored but never applicable
  (`action_pack_view.applicable_candidate`), and the eval does not count it as shown.
- An `unsupported` conclusion line is **dropped before storage**. The §4.4 promise that it is
  "kept in the trace for the eval" holds only as a count.

**Hiding also happens without a judgement.** `_check` turns each of these into `unsupported`:
- a call that fails;
- an answer `parse_label` rejects on the last candidate;
- a check not back by the deadline.

**`parse_label` is strict.** It accepts exactly `{"label": X}`, optionally fenced. Prose
around it, a leading verdict word, or an extra key such as `reason` all fail and move to the
next candidate. Sonnet's answers fell to the fallback 18–30 times per run.

**No per-check record.** The trace has a per-pack `fallback_answers` count, and
`models.verifier` is the most-used model. Which model judged which element is not recorded.

**Measured.**
- Muse as verifier hid 30–50% of correct actions: the Opus → Muse raw recall was 19, shown 13.
- Sonnet blocked almost nothing.
- Opus blocked about 36–60% of bad actions and hid about 3–15% of good ones.
- For the chosen configuration, the verifier hid 0–2 correct expectations per run. It also
  removed half the raw fabrications and the only raw wrong target in the rerun.

**The verifier lacks context.** It does not see the instruction or the values the user
stated, so a value the user gave reads as unsupported. It does not see ref dates (§6.1).

### 7.2 The change

**Decision: the hiding policy stays in this round. [decided, review]**
- `unsupported` stays non-applicable and not shown.
- A check the verifier could not complete stays `unsupported` too, with reason code
  `not_checked`.
- Rev 1's proposal to make verifier-unsupported actions individually applicable is
  **rejected for this round**, for two reasons:
  1. On the chosen configuration the verifier removes half the raw fabrications and the
     only wrong target. Change 1 has not yet been measured to replace that.
  2. A verifier outage (every candidate failing or timing out) must never make everything
     applicable.
- The move to flagging is prepared (the switch below), but only flipped under the
  conditions in §12.

**What the verifier is asked.** `VERIFY_PROMPT` answers:

```json
{"label": "supported" | "weak" | "unsupported",
 "reason_code": "partly_stated" | "not_in_evidence" | "value_not_in_evidence" | "date_anchor" | "contradicted" | "other",
 "reason": "one sentence, ≤ 200 chars"}
```

`contradicted` is a reason code: the evidence says something different, not merely nothing.
It is kept so that the later flagging mode can separate "no evidence" from "evidence against".

**New inputs.**
- A **user-values block** in the prompt, fenced as data like everything else: the instruction
  plus Plan's `stated_values`. It carries the rule that a value the user stated for that item
  and field counts as evidence for that field.
- **Ref dates and `said_on`**, from §6.2.

**Relaxed `parse_label`.** These forms are accepted, in order:
1. A JSON object anywhere in the text: whole, fenced, or brace-sliced, through the same logic
   as `action_pack_stages.parse_json_object`. It must have a valid `label`. Extra keys are
   allowed, and `reason_code`/`reason` are read when valid.
2. A leading verdict word. Markdown emphasis, quotes and a `label:`/`verdict:` prefix are
   stripped, and the first word must be one of the labels.

Anything else is `None`: two different labels, a label buried mid-prose, or an invalid JSON
label. Counts: `parse_json`, `parse_relaxed_json`, `parse_leading_word`, `parse_fail`.

**Unsupported conclusion lines are stored, hidden.** They are stored with `"hidden": true`
instead of being dropped, and GET omits them. This makes the raw rate countable from the
pack, not only from trace counts.

**Record the model per check.** Each stored action and conclusion line carries
`verifier_detail`:

```json
{"model": "<requested id>", "served": "<served id>", "attempt": 1,
 "reason_code": "...", "reason": "..."}
```

- `attempt` is 1 for the primary, 2 or more for fallbacks.
- The code caps (`web_only`, `not_archived`, `near_duplicate`) and Change 1's `date_assumed`
  also go in `reason_code` when they set or limit the label.
- `models` gains `verifier_answers: {"<model id>": n}`.
- `run_trace.verify` adds `answered_primary`, `answered_fallback` and `not_checked`.

**The hide switch.** `ACTION_VERIFIER_HIDE_UNSUPPORTED`, default **`on`**.

| Mode | Behaviour |
|---|---|
| `on` (this round) | Today's mapping, with the inputs above. |
| `off` (later, §12) | An `unsupported` action whose reason code is neither `contradicted` nor `not_checked` is stored as `weak`, with its reason shown as a caution. It is individually applicable and excluded from Apply-all. `contradicted` and `not_checked` stay `unsupported`. |

`run_trace.verify_hide_mode` records `on` or `off` for every pack.

**What the `verifier='unsupported'` column means**, since it is mixed by mode:
- With `on`, it covers three cases: not supported, contradicted, and not checked.
- With `off`, it covers only contradicted and not checked.
- `verifier_detail.reason_code` says which. A pack stored before this change has no
  `verifier_detail`, and its `unsupported` means "not supported or not checked".

**"Every check failed"** still fails the pack with reason `verifier`.

**Verifier fallbacks.** S6's family rule only excludes the writer's family (meta). Recommended
TEST setting: `ACTION_VERIFIER_FALLBACKS=claude-sonnet-5-5,google/gemini-3.8-flash`.
- Sonnet comes first because it uses the same API and parses the same way.
- The non-Anthropic entry gives cover when the Claude API is down.
- With hiding still on, a lenient fallback (Sonnet was measured lenient) lets more weak work
  through on the few checks it answers. The relaxed parser should make fallbacks rare (Q3).

### 7.3 Acceptance

- Unit tests:
  - `parse_label` accepts a fenced object with a reason, a prose-wrapped object, and "Weak —
    the record says …"; it rejects "supported or weak";
  - a failed check is `unsupported` + `not_checked`, non-applicable, in both modes;
  - a `contradicted` action stays `unsupported` in both modes;
  - an unsupported conclusion is stored hidden and omitted by GET;
  - `verifier_detail.model` is set on every checked element;
  - `verify_hide_mode` is in the trace;
  - with the switch `off`, a not-in-evidence action is `weak` and applicable.
- Full runs with Opus as primary: verifier fallbacks ≤ 2 per run (Sonnet as primary fell back
  18–30 times).
- A user-stated value (U-type, fixture) is labelled `supported` with the user-values block,
  where today it is unsupported.

### 7.4 Risks

- **No recall gain from the verifier in this round.** Expected, and accepted (§2).
- **The `reason` text is model-written free text in the pack.** It is pack content, like a
  rationale: it is stored and shown, but never logged (logs stay codes and counts).

---

## 8. Change 6 — writer and verifier configuration (defects #10, #11)

### 8.1 Current behaviour

- **There is no writer parameter.** `template.yaml` sets
  `ACTION_WRITER_MODEL: !Ref AskJudgeModel` on `ActionAgentFunction`, so the writer can only
  change together with the Ask judge.
- **The writer's routing is the judge's.** `action_pack_stages.judge_call` calls
  `llm_tools_client.complete_json(judge_routing=True)`:
  - the writer inherits `ASK_JUDGE_FALLBACKS`;
  - `ASK_JUDGE_PROVIDER` and `ASK_JUDGE_REASONING` apply only when
    `chain[0] == ASK_JUDGE_MODEL`.

  So a Muse writer under a DeepSeek judge gets no reasoning setting at all, and the judge's
  fallbacks.
- **The planner and the round model use the same `writer_model()`.**
- **`llm_utils.parse_reasoning(raw, model=...)`** already drops `off` for muse (muse rejects
  `{"enabled": false}`). The judge path passes no model.
- **The verifier settings** come in through `ActionVerifierModel`/`ActionVerifierFallbacks`.
  Both default to empty, and both workflows set them from `TEST_/PROD_ACTION_VERIFIER_*`
  variables (`deploy.yml` around line 196, `deploy-prod.yml` around line 226).
- **One OpenRouter key.** Both workflows pass `secrets.OPENROUTER_API_KEY` as
  `CorroborationApiKey` (the key `llm_tools_client` uses) and as the chat key.
  `deploy-prod.yml`'s job runs under `environment: production`. Today the key is one repo
  secret, so TEST evals spend from the same account balance as prod.

### 8.2 The change

**New template parameters**, wired into `ActionAgentFunction` only:

| Parameter | Env var | Default |
|---|---|---|
| `ActionWriterModel` | `ACTION_WRITER_MODEL` | `meta/muse-spark-1.3-contributor` |
| `ActionWriterReasoning` | `ACTION_WRITER_REASONING` | `''` = the model's default |
| `ActionWriterProvider` | `ACTION_WRITER_PROVIDER` | `''` |
| `ActionWriterFallbacks` | `ACTION_WRITER_FALLBACKS` | `''` = none |
| `ActionVerifierHideUnsupported` | `ACTION_VERIFIER_HIDE_UNSUPPORTED` | `on` (`AllowedValues: on, off`) |
| `ActionMaxRounds` | `ACTION_MAX_ROUNDS` | `8` (today's `MAX_ROUNDS`; read per run, clamped 1–8) |

- `ActionVerifierModel` defaults to `claude-opus-5-5`.
- `ActionVerifierFallbacks` defaults to the §7.2 recommendation.
- `ActionMaxRounds` ships in PR-3, not PR-1.

**Wiring for every new variable**, `ACTION_VERIFIER_HIDE_UNSUPPORTED` included:
- a template parameter;
- the function's env entry;
- `TEST_`/`PROD_` repo-variable overrides in **both** `deploy.yml` and `deploy-prod.yml`,
  with the existing bare-`Key` guard;
- an entry in the env-parity test (`tests/unit/test_action_agent_template.py`).

**Routing.**
- `complete_json` gains explicit routing arguments: the chain, plus the extras for the
  primary entry.
- When `ACTION_WRITER_MODEL` is set, `judge_call` uses **only** the `ACTION_WRITER_*`
  settings. That holds even when it equals `ASK_JUDGE_MODEL`: the judge's pin, reasoning
  and fallbacks then do not apply. Writer settings belong to the writer.
- When `ACTION_WRITER_MODEL` is empty, today's judge routing is kept exactly, for backward
  compatibility.
- Reasoning is parsed with `parse_reasoning(raw, model=<writer>)`, so `off` aimed at muse is
  dropped with a warning.
- Claude ids still go to the Claude API only (`is_claude`).

**Writer fallbacks default to none. My call:**
- A writer outage then fails the pack with reason `model`.
- The alternative is a fallback writer measured at 13% fabrication shipping quietly.
- S6 is unchanged: the verifier family check reads the writer model as actually used.

**PR-1 lands first, and it changes TEST's effective writer.** On its TEST deploy the writer
moves from the Ask judge model (DeepSeek on CoreWeave, with the judge's pin and reasoning) to
Muse with its defaults. Every later TEST measurement assumes this, including the v2 baseline
runs (§10.4). On prod the defaults are inert while `ACTION_PACKS` is off or internal.

**Owner step: separate OpenRouter keys** (secrets; numbered, owner-run).

1. In OpenRouter, create a prod-only key. A separate account is preferred, so balances are
   not shared. Failing that, a TEST key with a monthly credit limit on the shared account
   (Q5).
2. GitHub → repo Settings → Environments → `production` → add the secret
   `OPENROUTER_API_KEY` with the prod key. `deploy-prod.yml`'s job runs under
   `environment: production`, so an environment secret overrides the repo secret of the same
   name. No workflow change is needed.
3. Replace the repo-level `OPENROUTER_API_KEY` with the TEST key (credit-limited).
4. Run `deploy-prod` from the next release PR (the main agent gives the PR and run numbers),
   then `deploy` for TEST.
5. Verify in the OpenRouter dashboard: after one TEST eval run, usage appears only on the
   TEST key.

### 8.3 Acceptance

**Unit tests:**
- With `ACTION_WRITER_MODEL` set, the request carries `ACTION_WRITER_REASONING`. It carries
  no `ASK_JUDGE_*` pin, reasoning or fallbacks.
- With `ACTION_WRITER_MODEL` == `ASK_JUDGE_MODEL` and **no** `ACTION_WRITER_*` extras set,
  the request carries no provider, no reasoning and no fallback chain. The judge's settings
  are not inherited.
- With `ACTION_WRITER_MODEL` empty, the payload is byte-identical to today's.
- Reasoning `off` for muse sends no reasoning key.
- `ACTION_MAX_ROUNDS` out of range clamps.

**The env-parity test** covers all six new variables.

**TEST deploy check:** the parameters appear on the function, and one pack's
`models.writer` is the Muse id.

### 8.4 Risks

- **Template defaults now name models.** On prod, `ACTION_PACKS` gates them (off or
  internal), so a release carrying them is inert until the owner turns packs on.

---

## 9. Data model, store shape and UI contract

### 9.1 Migration (one, additive)

The number is the next free one at build time (0097 or later; 0096 is taken). The migration
is idempotent and has no CHECK changes:

- `ALTER TABLE action_packs ADD COLUMN sub_tasks jsonb` (Change 2);
- `ALTER TABLE action_pack_actions ADD COLUMN verifier_detail jsonb` (Change 5).

It ships with whichever of PR-5 and PR-7 lands first, with both columns. It goes to TEST
through the repo's usual path; the prod migration is an owner step.

### 9.2 Store and GET, including before the migration

**Store.**
- The store task writes the new columns.
- On `42P01` (undefined table) **or** `42703` (undefined column), it retries the **whole
  store transaction** without the new columns, and counts `store_legacy_columns`. This
  follows the pattern noted in `repositories/action_items.py`.
- It never writes a partial pack.

**GET.**
- It selects the new columns only when they exist. On `42703` it re-queries without them; a
  per-process cached flag is fine.
- A missing column reads as `null`.

**Old packs.**
- `sub_tasks` and `verifier_detail` are `null`.
- A conclusion line with no `hidden` field means `hidden: false`.
- `verifier='unsupported'` keeps its old meaning (§7.2).

**Shape checks.** Each new key is added, in the same PR as the key, to:
- `action_pack_view.payload_shape_error` / `_PACK_TYPES`;
- the repository's known-key checks (`repositories/action_packs.py`);
- the store task;
- GET's `shape_pack`.

**What GET returns.** It omits hidden conclusion lines, and returns `verifier_detail` minus
`served`.

**Trace.** `run_trace` stays counts-shaped (`fail_trace_error`). The new trace keys are:
`plan_scope`, `plan_key_defaulted__*`, `target_resolution`, `scope_gate`, `verify_hide_mode`,
`write_finish_length`, and `store_legacy_columns` (the last one at store time).

### 9.3 UI contract (hand-off to the ui session; no UI work here)

- A sub-task checklist under the reading: each `ask`, its status, the linked actions, and
  for `not_done` the reason.
- A `weak` element shows its `verifier_detail.reason` next to the existing "uncertain" badge.
  `date_assumed` reads "deadline assumed: date raised".
- Ambiguity, not-found and already-done notes need no new component.
- Nothing about applicability changes in this round.

---

## 10. Eval and harness changes

The harness lives on a local-only branch, so this section specifies behaviour, not paths.

### 10.1 Value-aware recall

- A match counts only when the action's values agree with the expectation's scored fields.
  Today `fields` is documentation only.
- Gold v2 marks which fields are scored:
  - a close: the status set (`done`/`closed`);
  - an update: each listed field;
  - an assumed default (R3): the raised-on date;
  - alternatives (U2): any listed alternative.
- The report gives `matched_value_ok` and `matched_value_wrong` separately. The gate uses
  `matched_value_ok`.

### 10.2 Type-mismatch warning

- When an annotator's `matches_expected` names an expectation of a different type (for
  example a `draft_rfi` matched to a `note` expectation), the match is still not counted. The
  report now lists it as a warning (gold id, expected id, element), with a count
  `type_mismatch_dropped`.
- Whether some expectations should accept a second type is a gold decision (Q7), not a
  scoring default.

### 10.3 Gold v2 edits

- **C4:** add the newer look-alike item to `must_not_touch`.
- **S2:** its one bundled `note` expectation may be satisfied by several notes together
  (`allow_split: true`). Only for that expectation, the one-to-one rule becomes many-to-one.
- **Scored fields per §10.1.**
- A new gold sha256. v1 results are not comparable, and the configuration is rerun on v2.

### 10.4 Repeats and spread

- Every configuration runs **at least twice**.
- The report gives per-gate mean, min and max, and passes a configuration only when every run
  passes.
- `compare` already computes the floor from two baselines. The baseline for this work is the
  chosen configuration on v2 **after PR-1 and before any other change here**: two runs.
- **Subset runs.** The targeted acceptance runs (§3.3–§7.3) and the PR-3 resolution gate need
  the harness to:
  - submit a subset of gold ids with a repeat count;
  - report per-id hit rates over the repeats;
  - for the resolution gate, score `run_trace.target_resolution` against gold
    `target_stable_id` and `must_not_touch`.

### 10.5 The blind second annotator stays

- Draft/blind agreement was 39%, and the draft was optimistic.
- The two gate runs (§1.4) are labelled in full by a blind second annotator, with
  disagreements reconciled before scoring.
- Exploratory runs may use the sampled blind subset.
- The blind item continues to exclude the verifier label and confidence. It now also
  excludes `verifier_detail` (its `reason` above all) and any caution text.

### 10.6 New report-only metrics

- **Per pack:** a `scope_gate` summary by code, and `target_resolution`.
- Sub-task coverage: planner sub-tasks accounted for, and `not_done` by reason.
- `write_dropped` by type and reason.
- Verifier answers by model and attempt (from `verifier_detail`), `not_checked`, and parse
  forms.
- Fabrication split by the element's verifier label (`supported` vs `weak`), to prepare the
  flagging decision (§12).

---

## 11. Latency budget

The Muse writer's p90 is about 255 s against the 300 s gate, and the Muse arms use all 8
gather rounds. That leaves about 45 s of headroom.

**Added in this round:**

| Addition | Cost | Guard |
|---|---|---|
| Target-resolution items hops (PR-3) | Up to 4 invokes. Run **concurrently** with each other and with round 1's other calls, so the wall cost is one hop (≤ 20 s), not up to 80 s serially. | Their own budget: each hop gets `min(ask_items.MAX_DEADLINE_S, ctx.left("gather") − ROUND_FLOOR_S)`. With under 5 s of that left, the lookup is skipped and the targets resolve `none` (`target_resolve_time`). |
| Larger write output (`field_sources`, `sub_tasks`) | Estimate +10–20 s on Muse, and a truncation risk | See below. |
| Muse reasoning=high (being measured) | Unknown | `ACTION_WRITER_REASONING` |

**Write output size.**
- `WRITE_MAX_TOKENS` is 8,000 today, and the new keys add output. A truncated answer fails
  to parse and costs a full schema-repair call.
- Raise `WRITE_MAX_TOKENS` to 12,000.
- Record `write_finish_length` (count of answers that stopped on the token limit) from the
  finish reason.
- If any targeted run shows it, prefer trimming the writer's rationale and reason caps over
  raising the limit again.

**Round cap.** `ACTION_MAX_ROUNDS` (PR-3, default 8) is the latency knob. If p90 goes above
280 s in any targeted or full run after PR-3, lower it in the iteration loop's tuning round
(§1.4). Q8 tracks the setting for Muse.

---

## 12. After the gates (not in this round)

These are designed but held back. Each waits until the chosen configuration has passed §1.4.

1. **Flag instead of hide.** Flip `ACTION_VERIFIER_HIDE_UNSUPPORTED` to `off`.
   - **When:** only after two consecutive passing v2 runs scored with the flip simulated.
     "Simulated" means the would-be-flagged elements are counted as shown in fabrication;
     §10.6's split gives the data.
   - **What stays non-applicable:** `contradicted` and `not_checked`, always.
2. **Deterministic planned web search in round 1.** Run every planned query concurrently
   with round 1, bounded by `WEB_BUDGET_S`, and count skips by reason. Remove `web_search`
   from the model-round catalog.
   - Its case rests on an eval-branch observation (the Opus-writer arm searched none of its
     planned queries). Muse searched all of them.
   - Take it up if §5.4's risk shows on TEST, or if the writer model changes.
3. **Cross-element consistency check.** One verifier call per pack, in parallel with the
   per-element checks. It reads a compact list of the elements and flags contradicting pairs
   (for example a note denying an event, next to updates that cite it). Both get a caution;
   neither is hidden.
4. **Sub-task repair call. [dropped from this round, review]** One focused write call for
   `not_answered` sub-tasks, guarded by elapsed time ≤ 180 s and write window ≥ 45 s. It is
   reconsidered only if sub-task coverage, not latency, is what blocks the gates.

---

## 13. PRs, in landing order

Each PR goes to `develop` with its tests and is deployed to TEST.

| Order | PR | Content | Depends on | Changes |
|---|---|---|---|---|
| 1 | PR-1 | Config: `ActionWriter*` parameters; `ActionVerifierHideUnsupported`; explicit routing in `complete_json`; template defaults (Muse writer, Opus verifier, fallbacks); workflow variables; env parity. **Changes TEST's effective writer.** | none | 6 |
| — | (harness) | §10 changes, gold v2, then two baseline v2 runs on PR-1's configuration | PR-1 on TEST | eval |
| 2 | PR-2 | Items tool `deadline_text` and `raised_on`; the agent's own dated line renderer with weekdays; today stated separately; prompt date rules; verifier ref dates | none | 4, 5 (dates) |
| 3 | PR-3 | Plan schema v2 (`change_scope`, `targets`, `stated_values`, `delegations`, `sub_tasks`; tolerant); concurrent target resolution with its budget; `run_trace.target_resolution`; `ActionMaxRounds` knob. **No enforcement.** Then the blocking resolution gate (§3.3). | none | 1, 2, 6 |
| 4 | PR-4 | Writer `target_ref`/`field_sources`; the scope gate (`OPEN_STATUSES`, named/tidy, provenance, user-value override, assumed default); `WRITE_MAX_TOKENS` 12,000 and `write_finish_length` | PR-2, PR-3 **and its gate met** | 1 |
| 5 | PR-5 | Writer `sub_tasks`; accounting (no repair call); migration (both columns, if first); store/GET with legacy-column tolerance | PR-3 | 2 |
| 6 | PR-6 | Standards-as-note rule; note schema (4,000 chars, extra keys removed and counted); typed `write_dropped` | none | 3 |
| 7 | PR-7 | Relaxed `parse_label`; user-values block; `verifier_detail` per check; hidden unsupported conclusion lines; reason codes including `contradicted`; hide switch honoured (default on); migration (if first) | PR-3 (stated values) | 5 |

- **Parallel work.** PR-2, PR-6 and PR-7 may be built in parallel with PR-3. PR-4 waits
  for the PR-3 gate. PR-5 and PR-7 share the migration; whichever lands first carries both
  columns.
- **The iteration loop (§1.4)** starts after PR-7 is on TEST.

---

## 14. Rollout

| Step | What | Where | Who |
|---|---|---|---|
| 1 | PR-1 lands; set TEST variables if they differ from the template defaults | TEST | Main agent |
| 2 | Harness §10 changes; gold v2; two baseline runs | Local eval branch, TEST | Main agent |
| 3 | PR-2 to PR-7 land on `develop`, deploy to TEST (`ACTION_PACKS=internal` there already); the migration on TEST | TEST | Main agent |
| 4 | The PR-3 resolution gate; targeted acceptance after each PR (§3.3–§7.3) | TEST | Main agent |
| 5 | The iteration loop and two consecutive full v2 runs with blind labels (§1.4) | TEST | Main agent and blind annotator |
| 6 | OpenRouter key split (§8.2 owner step) | GitHub secrets, OpenRouter | **Owner** |
| 7 | Release PR to `main` (prod stays `ACTION_PACKS` off or internal; template defaults inert; hide switch on) | prod | Main agent opens; prod gate per the usual 15-minute wait |
| 8 | Prod migration for the two columns | prod | **Owner** |
| 9 | Any prod `ACTION_PACKS` change, prod writer/verifier variables, and any later hide-switch flip | prod | **Owner** |

Owner steps carry PR numbers and run links when they are handed over.

---

## 15. Risks (cross-cutting)

- **Noise hides real effects.** One run cannot tell a one-expectation gain from noise.
  Targeted repeats (3 per id), the blocking resolution gate, and the two-run success rule
  are the mitigation.
- **The fixes overfit to 24 instructions.** Most gates here are code rules (scope,
  provenance, schema), not prompt tuning against gold wording. Prompt additions are general
  rules, and none names a gold case. The iteration loop allows one tuning round, not
  open-ended tuning.
- **The central recall estimate sits at the gate.** If it misses, the next levers are in
  order: §12.2 (web), §12.4 (repair call), then §12.1 (flagging). Each needs its own
  measurement.
- **Prompt-only rules stay unenforced.** The standards-as-note rule and the date-anchoring
  rule are partly prompt-only. Their measured effect is the acceptance, not their presence in
  the prompt.

---

## 16. Open questions

1. *(Closed in rev 2: fabrication counts every shown element.)*
2. *(Closed in rev 2: `raised_on` is the earliest row regardless of visibility; same-day
   default is the raised date, §3.2.4.)*
3. **Verifier fallbacks.** `claude-sonnet-5-5`, then `google/gemini-3.8-flash` (§7.2), or a
   different non-Anthropic second entry?
4. **Writer fallback.** None (**my call**: fail the pack), or a measured second writer?
5. **OpenRouter key split.** A separate account, or a credit-limited TEST key on the shared
   account? A limited key caps TEST spend, but both still draw on one balance.
6. **C2/e1, C3/e1 and U4/e1 recurring misses.** These are not in the defect list. Trace them
   (run_trace, `write_dropped`, `scope_gate`, `target_resolution`) on the v2 baseline before
   attributing a cause.
   - U4 is an update of `responsible`. Apply (S10) requires a `members_for_site` display
     name, while `_proposed` only checks length.
   - C2 and C3 may be the measured refuse-vs-act flips.
7. **Second accepted type.** Should a `note` expectation also accept a `draft_rfi` whose body
   carries the same answer (gold decision, §10.2)?
8. **Muse writer settings.** The reasoning level (high is being measured), and the
   `ACTION_MAX_ROUNDS` value.
9. *(Moved with §12.3.)* Should one side of a consistency conflict ever be hidden?
   **My call:** no.
10. **Gold coverage.** v1 warns that the tidy, in-library and not-in-library scenarios are
    below the §8.1 counts (1/3, 2/5, 2/5). Expanding the gold set is a follow-on (gold v3).
    It is not part of this success definition.
11. *(Closed in rev 2, decided in review: the §4.4 reversal is rejected for this round; the
    reasons are in §7.2.)*

---

## 17. Rev 2 changes (from the rev 1 review)

1. **Target resolution (§3.2.2).**
   - Matching is task number, then normalised token containment with its own threshold
     (`TARGET_MATCH_MIN`), over an explicit all-status list.
   - Rev 1's `near_duplicate` fallback is removed: it cannot fire on a short phrase.
   - A blocking resolution-rate floor on C1–C5, U1–U5, R3 and M2 runs before enforcement
     (§3.3).
2. **The §4.4 reversal is rejected for this round.**
   - `unsupported` and `not_checked` stay non-applicable, and the hide switch defaults to
     `on` (§7.2, §1.5, Q11).
   - The input fixes ship: user-values block, ref dates, relaxed parser, `verifier_detail`,
     and hidden conclusion lines stored instead of dropped. `contradicted` stays as a reason
     code.
3. **Close and update targets are restricted to `OPEN_STATUSES` in code.** `change_scope`
   defaults to `named`, and `plan_scope` is traced (§3.2.4 rule 1, §3.2.1).
4. **Latency.** Concurrent target-resolution hops with their own budget;
   `WRITE_MAX_TOKENS` 12,000 and `write_finish_length` (§11).
5. **Legacy columns.** Store retries the whole transaction without the new columns on
   42P01/42703; GET tolerates a missing column; old-pack defaults; `verify_hide_mode` traced;
   the mixed meaning of `verifier='unsupported'` is stated (§9.2, §7.2).
6. **The sub-task repair call is dropped,** and Change 2's expected gain is lowered (§4.2,
   §2).
7. **`user` fields take the planner's stated value,** and the writer's disagreement is
   counted, not dropped (§3.2.4 rule 3).
8. **New Plan keys are tolerated** when absent or malformed (§3.2.1).
9. **PR-1 (config) lands first** and changes TEST's effective writer. An acceptance case
   covers writer == judge model with no writer extras (§8).
10. **The agent has its own topic/chunk renderer;** Ask's `_item_line` is untouched (§6.2).
11. **Expected recall is restated** at about 16–18 central, with the downside, and the
    iteration loop is added (§2, §1.4).
12. **Full wiring for every new variable** (§8.2).
13. **Wording.**
    - What `validate_draft` and `_code_check_action` each check, and which layer owns one
      action per item (§3.1).
    - The resolution hops are separate invokes, not part of the round-1 batch.
    - `write_dropped` already has reason buckets; it lacks the action type.
    - The Opus-arm web observation is marked as from the eval branch.

**Optional changes adopted:**
- The deterministic round-1 web search and the consistency check move to §12.
- `ACTION_MAX_ROUNDS` ships in PR-3.
- `raised_on` is defined regardless of visibility, with the same-day default stated.
- The per-pack `scope_gate` summary is in the eval report.
- The blind annotator is kept blind to caution and `verifier_detail.reason`.

**Beyond the review:**
- The `OPEN_STATUSES` restriction also covers named targets, not only tidy targets. A deadline
  change on a done item that the user named is a note, not an update.

---

## 18. Recon notes: where the code differs from the defect list

Checked against origin/develop `d14c5698` on 2026-11-05.

- **#8.** `deadline_text` is missing from the items SQL (`_scoped_items`) and from
  `ask_items.ITEM_FIELDS`, not only from the writer's render. The raised date is fetched but
  not rendered, and it is the newest row's date.
- **#5.** On the Muse-writer arms the planned searches did run. The skip happened on the
  Opus-writer arm (eval branch). Round 1 never searches the web; only a model round can.
  S2's miss is a type choice, not a gather miss: no web query was planned for it, correctly.
- **#7.**
  - Hiding also comes from failed, unparseable or timed-out checks, not only from verdicts.
  - Unsupported conclusion lines are dropped before storage, not kept.
  - The verifier prompt carries no ref dates.
  - For the chosen configuration, verifier hiding cost 0–2 expectations per run, while it
    removed half the raw fabrications.
- **#9.** Drops are counted in `run_trace.write_dropped` with reason buckets but no action
  type. The Muse arms had 0 writer-schema drops. The likely causes are the exact-`{"text"}`
  note shape, the 2,000-character cap, and the whole-element drop on any extra key.
- **#10/#11.**
  - There is no writer template parameter: `ACTION_WRITER_MODEL` is `!Ref AskJudgeModel`.
  - The writer also inherits `ASK_JUDGE_FALLBACKS`.
  - `parse_reasoning` already handles muse's `off` when given the model.
- **#10 (keys).** An environment-scoped `production` secret overrides the repo secret with no
  workflow change. A second key on the same account still shares the balance.
- **#2.** The planner never sees evidence, so anchoring is the writer's and verifier's job.
- **Recall is not value-aware today.** U2 counted as matched in 3 of 4 Muse runs while
  labelled unsupported.
