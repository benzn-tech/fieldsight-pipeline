# Action pack quality fixes — design

Date: 2026-11-05. Status: rev 1, for review (then an implementation plan).

Builds on: `docs/superpowers/specs/2026-11-01-background-action-agent-design.md` (PR #1207,
"the 11-01 spec") and its plan `docs/superpowers/plans/2026-11-01-background-action-agent.md`
(implementation #1210). Section numbers like "§4.4" or "S6" refer to the 11-01 spec.

The eval that measured the defects ran on TEST against a 24-instruction gold set (gold v1).
The harness is on a local-only branch. Gold instructions are referred to by id only (C1–C5
close, U1–U5 update, K1–K4 create, S1–S4 standards, R1–R4 refuse/delegate, M1–M2 mixed). This
document names no people, sites or quotes from the gold set.

Decisions marked **my call** were made by the main agent on the owner's standing
instruction to choose the long-term reasonable option. The owner can overturn any of them.

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
attributed to C3, C2 or U4 (open question Q6).

**Fabrication.** In the Muse-writer runs, almost every element labelled "not supported"
belongs to one of these four groups:

| Pattern | Per run | Defect |
|---|---|---|
| M1: `update_item` on `status` and/or `priority` of items nobody asked about | 5–6 | #1 |
| S3/S4: `update_item status` on an item the user did not mean | 1–2 | #6 |
| U2 (and one M2 run): a deadline anchored to today, not to the date it was said | 0–1 | #2 |
| C4: a close or note aimed at the newer look-alike item instead of the named one | 0–1 | #6 |

If Change 1 removes the first two groups and Change 4 the third, raw writer fabrication in
those runs falls from 7–9% to roughly 1–3%. That is low enough to show more of the writer's
output (Change 5) and still pass the 5% gate. Change 5 therefore must not ship before Change 1.

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
- **Q4.** The verifier warns instead of hiding. Only a clear contradiction is hidden. Every
  check records the model that answered it.
- **Q5.** The writer's model, reasoning, provider pin and fallbacks are configured
  independently of the Ask judge. TEST and prod spend from separate OpenRouter credentials.

### 1.4 Success definition

On **gold v2** (§10.3), the chosen configuration passes all four gates in **two consecutive
runs**:

- Labels come from the blind second-annotator process (§10.5).
- Recall is value-aware (§10.1).
- Fabrication counts every shown element, flagged ones included (Q1 under §13).

A single passing run is not success, because the noise floor (§1.1) is bigger than the
margin in most single-run comparisons.

### 1.5 Non-goals

- No new action types, no auto-apply, no outbound sends: G3 and A5 of the 11-01 spec stand.
- No change to the S5 web-query isolation. Web queries are still written in Plan from the
  instruction only.
- No change to apply semantics (§5) apart from the bulk-apply exclusion that already covers
  `weak` actions.
- No UI work in this repo. The UI session gets a contract hand-off (§9.3).
- No attempt to fix C2, C3 or U4 by guesswork (Q6).
- No gold-set expansion beyond the v2 edits in §10.3 (Q10).

---

## 2. Changes in impact order

| # | Change | Defects | Gate it moves | Gold ids it should recover or protect | PRs |
|---|---|---|---|---|---|
| 1 | Change scope and field provenance, enforced in code | #1, #3, #6 | Fabrication, wrong target (+ recall via R3) | M1 and S3/S4 fabrications; C4, S4 wrong target; R3/e1 | PR-2, PR-3 |
| 2 | Sub-tasks: the planner splits, the writer accounts | #4 | Recall | M1/e1, M1/e3, M1/e4, M2/e4, M2/e5 | PR-2, PR-4 |
| 3 | Standards lane: run planned searches, answer as `note`; notes survive the schema | #5, #9 | Recall | S2/e1, S3/e1, S4/e1, S4/e2 | PR-5 |
| 4 | Dates and the item view: said-on dates, `deadline_text`, raised-on date | #2, #8 (+#3 data) | Recall (value-aware), fabrication | U1/e1, U2/e1, R3/e1 | PR-1 |
| 5 | Verifier: flag, don't hide | #7 | Recall (protects the writer's correct actions), wrong target (consistency) | Correct actions hidden by the verifier: 0–2 per run with Opus, 6 with Muse | PR-6, PR-7 |
| 6 | Writer and verifier configuration; separate OpenRouter keys | #10, #11 | p90 (reasoning knob); none directly | n/a (enabler) | PR-8, owner step |

**Expected recall.** Starting from a value-aware baseline of about 12–13 of 27, the
conservative expectation is:

| Change | Expected gain |
|---|---|
| 2 | +3 (of 5) |
| 3 | +2 (of 4) |
| 1 and 4 together | +2 (R3, U2 with the right value) |
| 5 | +0–1 |

That gives about 19–21 of 27, against the 17 the gate needs, with a margin of 2–4 for the
2–3 expectations of measured noise. If Changes 2 and 3 land at the low end, the margin is
gone. That is why §1.4 asks for two consecutive runs.

---

## 3. Change 1 — change scope and field provenance (defects #1, #3, #6)

### 3.1 Current behaviour

- `action_pack_stages.py:WRITE_PROMPT` lets the writer target any item in the evidence. The
  only target rule is "copy its stable_id exactly from an item evidence line".
- `validate_draft` (`action_pack_stages.py`) checks four things only:
  - the target exists in `run.items`;
  - the transition is legal (`legal_change`);
  - field shapes (`_proposed`);
  - the refs are citable.
- Nothing checks *why* a field gets its value. An `update_item` that sets `priority: high` on
  an item the instruction never mentioned, citing only that item's own row, passes every
  check. So does an invented deadline.
- `action_pack_verify._code_check_action` repeats the same structural checks. The model
  check sees one element at a time with only its cited refs, so it cannot know which items the
  instruction meant.
- There is no notion of a *named* target. An item named by text is matched by the writer
  alone. Two open items with near-identical text (C4's newer look-alike) are
  indistinguishable to the code.
- For a delegated missing due date (R3), the writer either refuses (draft RFI) or picks
  today+N. It cannot see when the item was raised (§6.1).

### 3.2 The change

**3.2.1 The planner states the scope.** The Plan schema (`PLAN_PROMPT`, `_PLAN_KEYS`,
`validate_plan`) gains these keys. Validation stays strict, and every key is required (it may
be empty).

| Key | Shape | Meaning |
|---|---|---|
| `change_scope` | `"named"` or `"tidy"` | `tidy` only when the instruction delegates bulk changes to open items ("tidy up the open items", "close whatever is finished"). Everything else is `named`, the default. |
| `targets` | `[{"id": "t1", "task_no": int or null, "phrase": str or null}]`, ≤ 10 | Items the instruction names, by task number or a short phrase (≤ 200 chars). |
| `stated_values` | `[{"target": "t1" or null, "field": "deadline"/"responsible"/"priority"/"status", "value": str}]`, ≤ 10 | Values the user gave. A deadline is ISO, resolved against today (the instruction is spoken today). |
| `delegations` | `[{"target": "t1" or null, "field": "deadline"}]`, ≤ 10 | The user delegated a missing value ("if there is no due date, set one"). v1 supports `deadline` only. |

The planner still sees no evidence (S5). It can only resolve what the instruction says.

**3.2.2 Gather resolves the targets in code.** Round 1 (`round1`) adds one items-tool call
per target:

- with `task_no` for a numbered target;
- with `query` = the phrase for a text target.

There are at most 4 such calls, inside the existing round-1 batch. Each target then resolves
in code to one of:

| Result | When |
|---|---|
| `resolved(stable_id)` | A `task_no` matches, or exactly one open item matches the phrase. |
| `ambiguous([stable_id, …])` | Two or more open items match the phrase, or the phrase's best match has a near-duplicate among open items (`action_pack_verify.near_duplicate` = `fuzzy` or `exact` between the candidates' texts). |
| `none` | Nothing matches. |

The writer's render shows each target with its result, for example
`t1: AMBIGUOUS — R4, R9: write a note, change neither`.

**3.2.3 The writer declares where each field comes from.** Every `close_item` and
`update_item` gains two keys:

- `target_ref`: a planner target id, or null when unnamed.
- `field_sources`: one entry per proposed field:
  `{"<field>": {"source": "user" | "evidence" | "assumed_default", "refs": [..], "said_on": "YYYY-MM-DD" or null}}`.

`create_item` gains `field_sources` for its optional `deadline`, `responsible` and `priority`.
The text needs refs, as it does today. `WRITE_PROMPT` states the rules in §3.2.4 and adds:
"change only items the instruction names, unless it asks you to tidy open items. For anything
else worth changing, write a note."

**3.2.4 A code gate after `validate_draft`** (new, in `action_pack_stages`). Every element it
rejects is counted in `run_trace.scope_gate` under the codes below.

1. **Target scope.**
   - Under `change_scope = "named"`, a close or update whose target is not the resolved
     `stable_id` of a planner target is dropped (`drop_unasked_target`).
   - Under `tidy`, any open item on the pack's site may be targeted, but rule 2 still
     applies to every field.
2. **Named target mismatch.**
   - A close or update whose `target_ref` names a target that resolved to a **different**
     `stable_id` is dropped (`drop_named_mismatch`). This is C4's look-alike.
   - A close or update on an `ambiguous` target is **demoted** to a `note`
     (`demoted_ambiguous`). The note's text is the rationale plus "matches more than one open
     item: R4, R9". Its refs are kept.
3. **Field provenance.** Each proposed field must pass its source's rule. A field that
   fails is removed from `proposed` (`field_unsourced__<field>`). An update left with no
   fields is dropped (`drop_no_sourced_fields`).

   | Source | Rule |
   |---|---|
   | `user` | The value equals a `stated_values` entry for that target and field (or a global one). Dates compare as ISO; priority and status lower-cased; responsible case-folded. |
   | `evidence` | At least one ref of kind `topic`, `chunk` or `document`. The target's own `item` ref does not count, because an item row cannot justify changing itself. The one exception is `deadline` from the item's own `deadline_text` (§6.2). Web refs never set a project field (A4). |
   | `assumed_default` | Only for `deadline`, only when a delegation covers the target, and only when the item has neither `deadline` nor `deadline_text`. **The code sets the value** to the item's `raised_on` (§6.1), whatever the writer proposed, and marks the action with the caution `date_assumed` (§7). |

   A `status` of `done`/`closed` on a `close_item` follows the same rule. Closing needs a
   stated value or evidence that the work is finished.
4. **Deadline sanity.** An `evidence` deadline must carry `said_on` equal to the date shown
   on one of its refs (`field_said_on_mismatch` otherwise). If the deadline is before that
   date, the field is removed (`field_deadline_before_source`).

**Owner rule for delegated missing due dates (#3).** When the user delegates a missing due
date, the default is the date the item was raised, marked as assumed. The writer neither
refuses nor picks today+N. Code enforces this through `assumed_default` (rule 3).

### 3.3 Acceptance

- Unit tests, deterministic, for every gate code above. They include an M1-shaped draft
  (status/priority updates on unnamed items under `named`: all dropped) and a C4-shaped draft
  (look-alike `stable_id`: dropped). They also cover an ambiguous phrase (demoted to a note)
  and an R3-shaped delegation (deadline replaced by `raised_on`, caution set).
- Targeted runs of M1, S3, S4, C4, U2 and R3, three runs each on TEST:
  - no shown `update_item` on an M1 `must_not_touch` item;
  - zero wrong targets on C4 and S4;
  - R3/e1 matched with the right value (its `raised_on`) in at least 2 of 3 runs.
- Full gold v2: shown fabrication ≤ 5% and wrong target = 0 in both gate runs (§1.4).

### 3.4 Risks

- **Lost recall from over-strict sourcing.** A correct update whose evidence the writer
  cites badly is removed. Mitigation: `scope_gate` counts by code, so the eval can see how
  many removed fields were right. A field-level removal keeps the rest of the action.
- **Planner mis-scoping.** A `tidy` instruction read as `named` drops legitimate closes, and
  M2 is the gold case for this. Mitigation: `change_scope` is shown in the reading, and M2 is
  a targeted acceptance case.
- **`stated_values` is model-extracted user text.** It is compared as a value, never
  followed as an instruction. It is the user's own instruction, so it adds no new trust
  boundary (A1 still holds through rule 3).

---

## 4. Change 2 — sub-tasks (defect #4)

### 4.1 Current behaviour

- The Plan stage returns a single `reading` (`PLAN_PROMPT`).
- `WRITE_PROMPT` gets the reading and the instruction, with no structure.
- Nothing checks that every ask in the instruction was handled. M2 (five asks) gets 3 of 5 in
  every run, and M1 (four) gets 1 of 4. Nothing in the pack tells the user what was skipped.

### 4.2 The change

- **Plan.** It gains `sub_tasks`, ≤ 8 entries:
  `[{"id": "s1", "ask": "short paraphrase, ≤ 200 chars, instruction language", "kind": "close"|"update"|"create"|"review"|"standards"|"draft"|"other", "targets": ["t1", ...]}]`.
  - A single-ask instruction has one sub-task.
  - The reading lists the sub-tasks.
- **Write.** The writer output gains `sub_tasks`:
  `[{"id": "s1", "status": "done"|"partly"|"not_done", "actions": [indices], "conclusion": [indices], "reason_code": "no_evidence"|"not_found"|"ambiguous_target"|"out_of_scope"|"already_done"|"budget"|"other", "reason": "one sentence, ≤ 300 chars"}]`.
  The prompt requires one entry per planner sub-task, and at least one action or conclusion
  index for `done` and `partly`.
- **Code checks** (in `validate_draft` and the scope gate):
  - Every planner sub-task appears exactly once. Unknown ids are ignored and counted.
  - Indices are remapped through any drops and demotions from §3.2.4.
  - A `done` or `partly` whose actions were all dropped becomes `not_done`, with reason code
    `dropped_by_checks`.
  - A planner sub-task the writer left out becomes `not_done`, with reason code
    `not_answered`.
- **One repair call, time-guarded.** One focused write call ("produce actions only for
  sub-tasks s2, s4") runs only when **all** of these hold:
  - some sub-task is `not_answered`;
  - the run is ≤ 180 s from its claim;
  - `ctx.left("write")` ≥ 45 s.

  Its elements go through the same validation and gate, and are appended. The trace records
  `subtask_repair` = 1 or 0 and `subtask_repair_skipped_time`.
- **Stored** on a new `action_packs.sub_tasks` jsonb (§9). GET returns it. Action
  references are stored as action `seq`s after renumbering.

### 4.3 Acceptance

- Unit tests:
  - a missing sub-task becomes `not_answered`;
  - a `done` sub-task whose actions were dropped becomes `not_done`;
  - the repair call is skipped outside the time guard.
- Targeted M1 and M2, three runs each:
  - every planner sub-task is present in the stored pack;
  - M2 ≥ 4 of 5 and M1 ≥ 3 of 4 in at least 2 of 3 runs (value-aware).
- p90 stays ≤ 300 s in both full gate runs (§11).

### 4.4 Risks

- **The repair call adds latency** on the very instructions that are already slowest.
  Mitigation: the time guard above, and the repair targets only the missing sub-tasks.
- **Over-splitting.** A planner that splits one ask into three makes the writer pad.
  Mitigation: the cap of 8, and `already_done`/`out_of_scope` are acceptable answers.

---

## 5. Change 3 — standards lane and note survival (defects #5, #9)

### 5.1 Current behaviour

- **Web search is model-chosen only.** `round1` runs day overviews, items and documents,
  never web. `web_search` is reachable only when a model round picks it (`_tool_catalog`,
  `validate_round`, `_web`). Planned queries are therefore searched only if a later round
  model chooses them.
  - On the Opus-writer arm, S1–S4 planned 1–3 queries each and **none** was searched
    (`run_trace.web` empty).
  - On the Muse arms, S3/S4 searched every planned query (`web_searches` = `plan_web_queries`).
  - S1/S2 (standard in the library) correctly planned none.
  - So the skip is structural and model-dependent.
- **The writer has no rule for the form of a standards answer.** In the Muse arms:
  - S2 (in the library) became `create_item`s;
  - S3/S4 became `draft_rfi` or an `update_item` (counted under #6);
  - the expected `note` appeared only sometimes.
- **Note drops.**
  - `_DRAFT_FIELDS["note"]` requires `proposed` to be exactly `{"text"}`, with text ≤
    `CREATE_TEXT_MAX_CHARS` (2,000). A longer answer, or any extra key, drops the whole
    action.
  - `validate_draft` also drops an action that carries any unexpected top-level key.
  - The drops are not silent: `run_trace.write_dropped` counts them. But they fall into one
    bucket, `actions`, with no type or reason, so a lost note looks like any other drop.
  - In the Muse arms writer-schema drops were 0. They were 2–7 per run on other writers.

### 5.2 The change

**Deterministic planned searches.**
- Round 1 runs every planned web query (≤ 3) **concurrently** with its other calls, each
  bounded by `WEB_BUDGET_S`, when `web_on()` and `ctx.left("gather") ≥ MIN_WEB_S`.
- A planned query that does not run is counted by reason:
  - `web_skipped__disabled`;
  - `web_skipped__time`;
  - `web_skipped__docs_answered` (§5.3 Q);
  - `web_skipped__error`.
- `web_search` leaves the model-round catalog: every planned query has already run.
- S5 is unchanged: the queries are still the Plan stage's, run by id.

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

Several short notes are allowed, one per question. A web-only note:
- is capped at `weak` (A4, unchanged);
- carries the caution `web_only`, which the UI shows as "web source, not your project's
  documents";
- carries the caution `not_archived` when no source page was snapshotted (§6 of the 11-01
  spec).

D6 is unchanged: an unverified `web_answer` is still never citable.

**Notes survive the schema.**
- `note.proposed.text` accepts up to `BODY_MAX_CHARS` (4,000).
- An unknown key inside `proposed` or on the action is removed and counted
  (`extra_key__<type>`) instead of dropping the element. Known keys (`target_ref`,
  `field_sources`, the `sub_task` index) are part of the schema now.
- `run_trace.write_dropped` becomes `{"<type>__<reason>": n}`, codes only. Examples:
  `note__text_too_long`, `update_item__target_unknown`, `create_item__no_text`. The harness
  already sums `write_dropped.*`.

### 5.3 Acceptance

- Unit tests:
  - round 1 issues every planned web query, and each skip is counted with its reason;
  - a note with a 3,000-character text and one stray key is stored, with the key counted.
- Targeted S1–S4, three runs each:
  - every planned query is searched or carries a skip reason;
  - S2, S3 and S4 each produce at least one shown `note` that matches their expectation in at
    least 2 of 3 runs;
  - S1 still yields its `create_item`.

### 5.4 Risks

- **Latency.** The web lane runs up to 45 s per query. Concurrency keeps it inside round 1's
  wall time in most runs. §11 has the budget.
- **Web spend on instructions where the documents would do.** Plan already leaves
  `web_queries` empty when records and documents should be enough.
  - **Q (left to the builder):** whether round 1 skips a planned web query when
    `search_project_documents` returned hits for the same standard (`web_skipped__docs_answered`).
  - **My call:** do not skip in v1. The eval cannot yet tell whether a document hit answers
    the question, and the four S ids are the only standards cases.

---

## 6. Change 4 — dates and the item view (defects #2, #8, data for #3)

### 6.1 Current behaviour

- **The item view hides `deadline_text` at every layer, not only in the writer's render.**
  - `repositories/topics.py:_scoped_items` does not select `a.deadline_text`.
  - `ask_items.ITEM_FIELDS` has no such key.
  - `action_pack_stages._line` (the item branch) renders status, deadline, responsible and
    priority only.
  - So an item whose deadline exists only as text (migration 0011: the date column cannot
    hold "end of next week") reads as having no deadline. That is U1.
- **The writer cannot see when an item was raised.**
  - `_scoped_items` returns `t.report_date`, but `_line` does not render it.
  - That `report_date` is the **newest** live row's topic date, because the query uses
    `DISTINCT ON (stable_id)` ordered by `created_at DESC`. For a carried item it is the
    latest re-extraction date, not the first date it was raised.
- **Record lines carry a date, but nothing tells the writer to use it.**
  - `_item_line` renders `date time` on topic and chunk lines, without a weekday.
  - The writer prompt shows `Today (New Zealand)` once, and has no rule that a relative
    expression ("by Friday", "tomorrow") resolves against the date it was said. So the writer
    anchors to today (U2 every run; R3; M1).
- **The planner cannot anchor spoken dates.** It never sees evidence (S5). It can only
  resolve the instruction's own relative dates, which are correctly relative to today.
- **The verifier cannot catch a wrong anchor.** `action_pack_verify.build_prompt` renders
  each ref as `ref | kind | text`, with no date.

### 6.2 The change

- **Items tool.**
  - `_scoped_items` adds `a.deadline_text` and `raised_on`.
  - `raised_on` is the earliest `report_date` across every `action_items` row sharing the
    `stable_id` and visible under the same scope. Builder check: the cost of that subquery
    inside the 20 s rag-search hop, against the alternative `action_task_numbers.created_at`.
    The alternative is unreliable for backfilled numbers (Q2).
  - `ask_items.ITEM_FIELDS` adds both keys.
  - `SNAPSHOT_FIELDS` does not change: neither key can be patched.
- **Render (Gather and Write).**
  - Item lines add `deadline_text "<text>"` (when set) and `raised_on YYYY-MM-DD (Wkd)`.
  - Topic and chunk lines show `said_on YYYY-MM-DD (Wkd)` in place of the bare date.
  - The prompt header says `Today (New Zealand): YYYY-MM-DD (Weekday)` and adds "today is not
    the date the records were said".
- **Write rules.**
  - A relative date in a record resolves against that record's `said_on`.
  - An item's `deadline_text` resolves against its `raised_on`. That is the one case where
    the item's own ref may source `deadline` (§3.2.4 rule 3).
  - The writer fills `field_sources.deadline.said_on`. Code checks it (§3.2.4 rule 4).
- **Verify.** `build_prompt` renders each ref's date and weekday. For a deadline it also
  renders the claim's `said_on`. A deadline that does not follow from its anchor is a
  contradiction (§7.2).
- **No regex date parsing.** Spoken dates are the model's job. The code only checks that the
  anchor the writer names is a real ref date.

### 6.3 Acceptance

- Unit tests:
  - `deadline_text` and `raised_on` reach the item line;
  - `raised_on` is the earliest row's date for a carried item.
- Targeted U1, U2 and R3, three runs each: each expectation is matched **with the right
  value** (value-aware) in at least 2 of 3 runs.

### 6.4 Risks

- **The `raised_on` subquery slows the items hop.** Mitigation: the builder measures it and
  may fall back to the ledger (Q2).
- **Weekday text in the render costs tokens.** About 6 characters per line, which is
  negligible against `WRITE_RENDER_MAX_CHARS`.

---

## 7. Change 5 — the verifier flags instead of hiding (defect #7)

### 7.1 Current behaviour

- **Labels** (`action_pack_verify.verify`): `supported`, `weak`, `unsupported`.
  - An `unsupported` action is stored but never applicable (`action_pack_view.applicable_candidate`),
    and the eval does not count it as shown.
  - An `unsupported` conclusion line is **dropped before storage**. The §4.4 promise that it
    is "kept in the trace for the eval" holds only as a count.
- **Hiding also happens without a judgement.** `_check` turns each of these into
  `unsupported`:
  - a call that fails;
  - an answer `parse_label` rejects on the last candidate;
  - a check not back by the deadline.
- **`parse_label` is strict.** It accepts exactly `{"label": X}`, optionally fenced. Prose
  around it, a leading verdict word, or an extra key such as `reason` all fail and move to
  the next candidate. Sonnet's answers fell to the fallback 18–30 times per run.
- **No per-check record.** The trace has a per-pack `fallback_answers` count, and
  `models.verifier` is the most-used model. Which model judged which element is not recorded.
- **Measured.**
  - Muse as verifier hid 30–50% of correct actions: the Opus → Muse raw recall was 19, shown
    13.
  - Sonnet blocked almost nothing.
  - Opus blocked about 36–60% of bad actions and hid about 3–15% of good ones.
  - For the chosen configuration, verifier hiding cost 0–2 expectations per run. This change
    buys robustness to the verifier model more than recall.
- **The verifier sees only the claim.** It does not see the instruction or the values the
  user stated, so a value the user gave reads as unsupported.
- **No cross-element check.** Nothing compares elements with each other. For example, a note
  saying no such meeting took place can sit beside updates that cite that meeting.

### 7.2 The change

**What the verifier is asked.** `VERIFY_PROMPT` answers:

```json
{"label": "supported" | "weak" | "unsupported" | "contradicted",
 "reason_code": "partly_stated" | "not_in_evidence" | "value_not_in_evidence" | "date_anchor" | "other",
 "reason": "one sentence, ≤ 200 chars"}
```

- **`contradicted`** means the evidence says something different, not merely nothing.
- **The prompt adds a user-values block**, fenced as data like everything else: the
  instruction plus Plan's `stated_values`. It carries the rule that a value the user stated
  for that item and field counts as evidence for that field.
- **Ref dates** come from §6.2.

**How a verdict maps onto the stored element.** There are no new values in the
`action_pack_actions.verifier` CHECK:

| Verifier answer | Stored label | Shown? | Caution |
|---|---|---|---|
| supported | `supported` | yes | (none) |
| weak | `weak` | yes, not bulk-applied | `reason_code` + reason |
| unsupported | `weak` | yes, not bulk-applied | `not_in_evidence` (or its reason_code) + reason |
| contradicted | `unsupported` | **no** (stored for the eval) | `contradicted` + reason |
| check failed / timed out / unparseable on every candidate | `weak` | yes, not bulk-applied | `not_checked` |

- **Conclusion lines** map the same way. `weak`/`unsupported` become
  `confidence: "uncertain"` with the caution. A `contradicted` line is **stored** with
  `"hidden": true` and omitted by GET, so the eval can count it.
- **Code-check drops** (`drop_*`) are unchanged: those are structural failures, not
  judgements.
- **Caps keep their labels and add their cautions:**
  - web-only becomes `web_only`;
  - no snapshot becomes `not_archived`;
  - a fuzzy near-duplicate becomes `near_duplicate`.
- Change 1's `date_assumed` arrives as a caution too.
- **"Every check failed"** still fails the pack with reason `verifier`. A pack nobody
  checked is not a pack with cautions.

**Cross-element consistency.**
- One extra verifier call per pack runs **in parallel** with the per-element checks. It reads
  a compact list of the drafted elements (key, type, target task number or text, proposed
  fields, the element's text, ref ids) and returns
  `[{"a": "conclusion:1", "b": "action:4", "reason_code": "conflict", "reason": "..."}]`.
- Both elements of each pair get the caution `conflicts_with` naming the other. Neither is
  hidden, because the check cannot tell which side is right.
- The call is skipped when fewer than 2 elements remain, or less than `MIN_CALL_S` +
  `ATTEMPT_CAP_S` is left (`consistency_skipped_time`).

**Relaxed `parse_label`.** These forms are accepted, in order:
1. A JSON object anywhere in the text: whole, fenced, or brace-sliced, through the same
   logic as `action_pack_stages.parse_json_object`. It must have a valid `label`. Extra keys
   are allowed, and `reason_code`/`reason` are read when valid.
2. A leading verdict word. Markdown emphasis, quotes and a `label:`/`verdict:` prefix are
   stripped, and the first word must be one of the labels.

Anything else is `None`: two different labels, a label buried mid-prose, or an invalid JSON
label. Counts: `parse_json`, `parse_relaxed_json`, `parse_leading_word`, `parse_fail`.

**Record the model per check.** Each stored action and conclusion line carries
`verifier_detail`:

```json
{"model": "<requested id>", "served": "<served id>", "attempt": 1, "reason_code": "...", "reason": "...", "caution": ["..."]}
```

- `attempt` is 1 for the primary, 2 or more for fallbacks.
- `models` gains `verifier_answers: {"<model id>": n}`.
- `run_trace.verify` keeps its counts and adds `answered_primary` and `answered_fallback`.

**Verifier fallbacks.** S6's family rule only excludes the writer's family (meta). Recommended
TEST setting: `ACTION_VERIFIER_FALLBACKS=claude-sonnet-5-5,google/gemini-3.8-flash`.
- Sonnet comes first because it uses the same API and parses the same way.
- A non-Anthropic entry gives cover when the Claude API is down.
- Sonnet was measured lenient. Under flagging, a lenient fallback adds cautions less often,
  but it can no longer hide correct work (Q3).

### 7.3 Acceptance

- Unit tests:
  - the mapping table above;
  - `parse_label` accepts a fenced object with a reason, a prose-wrapped object, and "Weak —
    the record says …"; it rejects "supported or weak";
  - a failed check becomes `weak` + `not_checked`, not hidden;
  - a `contradicted` conclusion is stored hidden;
  - `verifier_detail.model` is set on every checked element.
- **Full runs:**
  - verifier fallbacks per run ≤ 2 with Opus primary (today: Sonnet 18–30);
  - shown recall ≥ raw recall − 1 in both gate runs (the verifier hides at most one correct
    expectation);
  - fabrication ≤ 5% counting flagged elements (§1.4).
- **The consistency check** flags the conflicting pair in at least 2 of 3 runs of a
  hand-built unit fixture (a conclusion denying an event, plus an action citing it). This is
  a fixture, not a gold id.

### 7.4 Risks

- **Fabrication goes up when fewer elements are hidden.** This is the reason Change 5 lands
  after Change 1 (§12). If shown fabrication exceeds 5% on the first full run after PR-7, the
  rollback is a single env switch, `ACTION_VERIFIER_HIDE_UNSUPPORTED=on` (default off once
  PR-7 lands). It restores today's mapping (unsupported is hidden).
- **Caution fatigue.** If most elements carry a caution, the flag means nothing. The eval
  reports the share of shown elements flagged (§10.6). The open question is the target
  (Q1).
- **The `reason` text is model-written free text in the pack.** It is pack content, like a
  rationale: it is stored and shown, but never logged (logs stay codes and counts).

---

## 8. Change 6 — writer and verifier configuration (defects #10, #11)

### 8.1 Current behaviour

- **There is no writer parameter.** `template.yaml` sets
  `ACTION_WRITER_MODEL: !Ref AskJudgeModel` on `ActionAgentFunction`, so the writer can only
  be changed together with the Ask judge.
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
  `deploy-prod.yml`'s job runs under `environment: production`. Today the key is a single
  repo secret, so TEST evals spend from the same account balance as prod.

### 8.2 The change

- **New template parameters**, wired into `ActionAgentFunction` only:

  | Parameter | Env var | Default |
  |---|---|---|
  | `ActionWriterModel` | `ACTION_WRITER_MODEL` | `meta/muse-spark-1.3-contributor` |
  | `ActionWriterReasoning` | `ACTION_WRITER_REASONING` | `''` = the model's default |
  | `ActionWriterProvider` | `ACTION_WRITER_PROVIDER` | `''` |
  | `ActionWriterFallbacks` | `ACTION_WRITER_FALLBACKS` | `''` = none |

  - The `ActionVerifierModel` default becomes `claude-opus-5-5`.
  - The `ActionVerifierFallbacks` default becomes the §7.2 recommendation.
  - Each parameter has `TEST_`/`PROD_` repo-variable overrides in both workflows, with the
    existing bare-`Key` guard.
- **Explicit routing.**
  - `complete_json` gains explicit routing arguments: the chain, plus the extras for the
    primary entry.
  - `judge_call` passes the `ACTION_WRITER_*` settings when `ACTION_WRITER_MODEL` is set. It
    keeps today's judge routing exactly when it is empty, for backward compatibility.
  - Reasoning is parsed with `parse_reasoning(raw, model=<writer>)`, so `off` aimed at muse is
    dropped with a warning instead of failing every call.
  - Claude ids still go to the Claude API only (`is_claude`).
- **Writer fallbacks default to none. My call:**
  - A writer outage then fails the pack with reason `model`.
  - The alternative is a fallback writer measured at 13% fabrication shipping quietly.
  - S6 is unchanged: the verifier family check reads the writer model as actually used.
- **The planner and round model keep using the writer settings.** A separate planner model
  is not part of this change.

**Owner step: separate OpenRouter keys (secrets; numbered, owner-run).**

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

- Unit tests:
  - with `ACTION_WRITER_MODEL` set, the request carries `ACTION_WRITER_REASONING`, carries
    no `ASK_JUDGE_*` pin, reasoning or fallbacks, and reasoning `off` for muse sends no
    reasoning key;
  - with it empty, the payload is byte-identical to today's.
- An env-parity test covers the new variables.
- A TEST deploy shows the parameters on the function, and one pack's `models.writer` is the
  Muse id.

### 8.4 Risks

- **Template defaults now name models.** On prod, `ACTION_PACKS` gates them (off or
  internal), so a release carrying them is inert until the owner turns packs on.

---

## 9. Data model, store shape and UI contract

### 9.1 Migration (one, additive)

The number is the next free one at build time (0097 or later; 0096 is taken). The migration:

- `ALTER TABLE action_packs ADD COLUMN sub_tasks jsonb` (Change 2);
- `ALTER TABLE action_pack_actions ADD COLUMN verifier_detail jsonb` (Change 5).

No CHECK changes. It is idempotent. It goes to TEST through the repo's usual path; the prod
migration is an owner step. The code tolerates the columns' absence until the migration runs,
with the same savepoint/42P01 pattern as migration 0093.

### 9.2 Store and GET

Each new key needs these updated together, in the same PR as the key:

- `action_pack_view.payload_shape_error` / `_PACK_TYPES`;
- the repository's known-key checks (`repositories/action_packs.py`);
- the store task;
- GET's `shape_pack`.

The new keys are `sub_tasks` on the pack; `verifier_detail` on actions; and
`verifier_detail` and `hidden` on conclusion lines. GET omits hidden conclusion lines and
returns `verifier_detail` minus `served`. `run_trace` stays counts-shaped
(`fail_trace_error`).

### 9.3 UI contract (hand-off to the ui session; no UI work here)

- **Caution chip.** A caution chip on any action or conclusion line with
  `verifier_detail.caution`. It shows the code's fixed label plus `reason`. These elements
  are already excluded from Apply-all (`weak`).
- **Sub-task checklist** under the reading: each `ask`, its status, the linked actions, and
  for `not_done` the reason.
- **Ambiguity notes and `date_assumed`** need no new component: they are a note, and a
  caution on an update.

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
- A new gold sha256. v1 results are not comparable, and every configuration in the
  decision is rerun on v2.

### 10.4 Repeats and spread

- Every configuration runs **at least twice**.
- The report gives per-gate mean, min and max, and passes a configuration only when every
  run passes.
- `compare` already computes the floor from two baselines. The baseline for this work is the
  chosen configuration on v2 **before** any change here: two runs.
- Targeted acceptance runs (§3.3–§7.3) need the harness to submit a subset of gold ids with a
  repeat count, and to report per-id hit rates over the repeats.

### 10.5 Blind second annotator stays

- Draft/blind agreement was 39%, and the draft was optimistic.
- The two gate runs (§1.4) are labelled in full by a blind second annotator, with
  disagreements reconciled before scoring.
- Exploratory runs may use the sampled blind subset.
- The blind item continues to exclude verifier labels and confidence, and now also the
  caution.

### 10.6 New report-only metrics

- Sub-task coverage: planner sub-tasks accounted for, and `not_done` by reason.
- Share of shown elements carrying a caution, by code.
- Fabrication split into flagged and unflagged shown elements.
- `scope_gate` and `write_dropped` counts by code.
- Verifier answers by model and attempt, from `verifier_detail`.

---

## 11. Latency budget

The Muse writer's p90 is about 255 s against the 300 s gate, and the Muse arms use all 8
gather rounds. That leaves about 45 s of headroom. What this design adds:

| Addition | Cost | Guard |
|---|---|---|
| Round-1 web (Change 3) | ≤ 45 s, concurrent with round 1's other calls | `MIN_WEB_S`; the gather deadline |
| Larger write output (`field_sources`, `sub_tasks`) | Estimate +10–20 s on Muse | None; measured in PR-3/PR-4 runs |
| Sub-task repair call (Change 2) | ≤ 1 write call | Only ≤ 180 s from claim and ≥ 45 s write window |
| Consistency check (Change 5) | One call, in parallel with per-element checks | Skipped under `MIN_CALL_S` + `ATTEMPT_CAP_S` |
| Muse reasoning=high (being measured) | Unknown | `ACTION_WRITER_REASONING` |

If p90 goes above 280 s in any run after PR-3 or PR-4, the next PR adds `ACTION_MAX_ROUNDS`
(default 8, today's `MAX_ROUNDS`). The planned searches in round 1 replace most of what the
later rounds were for (Q8).

---

## 12. PRs

Each PR goes to `develop` with its tests and is deployed to TEST.

| PR | Content | Depends on | Changes |
|---|---|---|---|
| PR-1 | Items tool: `deadline_text`, `raised_on`; dated render with weekdays; today stated separately; prompt date rules; verifier ref dates | none | 4 |
| PR-2 | Plan schema v2: `change_scope`, `targets`, `stated_values`, `delegations`, `sub_tasks`; round-1 target resolution; counts only (no gate yet) | none | 1, 2 |
| PR-3 | Writer `target_ref` / `field_sources`; the scope gate; assumed-default deadline in code | PR-1, PR-2 | 1 |
| PR-4 | Writer `sub_tasks`; accounting; repair call; migration column `sub_tasks`; store/GET | PR-2 | 2 |
| PR-5 | Round-1 planned web search; standards-as-note rule; note schema; per-type `write_dropped` | none | 3 |
| PR-6 | Relaxed `parse_label`; per-check model record (`verifier_detail` model/attempt); migration column `verifier_detail` | none | 5 (part) |
| PR-7 | Flag-not-hide mapping; `contradicted`; user-values block; consistency check; `ACTION_VERIFIER_HIDE_UNSUPPORTED` rollback switch | PR-3, PR-6 (and PR-2 for stated values) | 5 |
| PR-8 | `ActionWriter*` parameters; explicit routing in `complete_json`; template defaults; workflow variables | none | 6 |

- **One migration.** If PR-4 and PR-6 are built in parallel, the migration ships in
  whichever lands first, with both columns.
- **Harness changes (§10)** are made on the local eval branch, before the v2 baseline runs.
- **Order of landing.** PR-8, PR-1, PR-2, PR-5 and PR-6 in parallel; then PR-3, then PR-4
  and PR-7. PR-7 lands last, so fabrication protection (PR-3) is in place before less is
  hidden.

---

## 13. Rollout

| Step | What | Where | Who |
|---|---|---|---|
| 1 | Harness §10 changes; gold v2; two baseline runs of the chosen configuration | Local eval branch, TEST | Main agent |
| 2 | Set TEST variables: writer Muse, verifier Opus, fallbacks per §7.2 | Repo variables | Main agent |
| 3 | PRs land on `develop`, deploy to TEST (`ACTION_PACKS=internal` there already); the migration on TEST | TEST | Main agent |
| 4 | Targeted acceptance runs after each PR (§3.3–§7.3) | TEST | Main agent |
| 5 | Two consecutive full gold-v2 runs with blind labels; success per §1.4 | TEST | Main agent and blind annotator |
| 6 | OpenRouter key split (§8.2 owner step) | GitHub secrets, OpenRouter | **Owner** |
| 7 | Release PR to `main` (prod stays `ACTION_PACKS` off or internal; template defaults inert) | prod | Main agent opens; prod gate per the usual 15-minute wait |
| 8 | Prod migration for the two columns | prod | **Owner** |
| 9 | Any prod `ACTION_PACKS` change, and the prod writer/verifier variables | prod | **Owner** |

Owner steps carry PR numbers and run links when they are handed over.

---

## 14. Risks (cross-cutting)

- **Noise hides real effects.** One run cannot tell a 1-expectation gain from noise.
  Targeted repeats (3 per id) and the two-run success rule are the mitigation. Acceptance
  never rests on a single full run.
- **The fixes overfit to 24 instructions.** Most gates here are code rules (scope,
  provenance, schema), not prompt tuning against gold wording. Prompt additions are general
  rules, and none names a gold case.
- **Changes 1 and 5 pull in opposite directions.** Change 1 removes elements; Change 5 shows
  more. The ordering in §12 and the rollback switch in §7.4 handle this.
- **Prompt-only rules stay unenforced.** The standards-as-note rule and the date-anchoring
  rule are partly prompt-only. Their measured effect is the acceptance, not their presence in
  the prompt.

---

## 15. Open questions

1. **Fabrication denominator.** Do flagged (caution) elements count toward the 5% gate?
   **My call:** yes, strict. Flagged-only fabrication is reported separately. A related
   question: is there a ceiling on the share of flagged elements?
2. **`raised_on`.** The earliest `report_date` over the `stable_id`'s rows, or
   `action_task_numbers.created_at`? The ledger is cheaper but wrong for backfilled numbers.
3. **Verifier fallbacks.** `claude-sonnet-5-5`, then `google/gemini-3.8-flash` (§7.2), or a
   different non-Anthropic second entry?
4. **Writer fallback.** None (**my call**: fail the pack), or a measured second writer?
5. **OpenRouter key split.** A separate account, or a credit-limited TEST key on the shared
   account? A limited key caps TEST spend, but both still draw on one balance.
6. **C2/e1, C3/e1 and U4/e1 recurring misses.** These are not in the defect list. Trace them
   (run_trace, `write_dropped`, `scope_gate`) on the v2 baseline before attributing a cause.
   - U4 is an update of `responsible`. Apply (S10) requires a `members_for_site` display
     name, while `_proposed` only checks length.
   - C2 and C3 may be the measured refuse-vs-act flips.
7. **Second accepted type.** Should a `note` expectation also accept a `draft_rfi` whose body
   carries the same answer (gold decision, §10.2)?
8. **Muse writer settings.** Reasoning level (high is being measured), and whether
   `ACTION_MAX_ROUNDS` should drop below 8 for it.
9. **Consistency conflicts.** Should one side of a conflict ever be hidden? **My call:** no,
   flag both.
10. **Gold coverage.** v1 warns that the tidy, in-library and not-in-library scenarios are
    below the §8.1 counts (1/3, 2/5, 2/5). Expanding the gold set is a follow-on (gold v3).
    It is not part of this success definition.

---

## 16. Recon notes: where the code differs from the defect list

Checked against origin/develop `d14c5698` on 2026-11-05.

- **#8.** `deadline_text` is missing from the items SQL (`_scoped_items`) and from
  `ask_items.ITEM_FIELDS`, not only from the writer's render. The raised date is fetched but
  not rendered, and it is the newest row's date.
- **#5.** On the Muse-writer arms the planned searches did run. The skip happened on the
  Opus-writer arm. Round 1 never searches the web; only a model round can. S2's miss is a
  type choice, not a gather miss: no web query was planned for it, correctly.
- **#7.**
  - Hiding also comes from failed, unparseable or timed-out checks, not only from verdicts.
  - Unsupported conclusion lines are dropped before storage, not kept.
  - The verifier prompt carries no ref dates.
  - For the chosen configuration, verifier hiding cost 0–2 expectations per run.
- **#9.** Drops are counted in `run_trace.write_dropped`, but in one untyped bucket. The
  Muse arms had 0 writer-schema drops. The likely causes are the exact-`{"text"}` note shape,
  the 2,000-character cap, and the whole-element drop on any extra key.
- **#10/#11.**
  - There is no writer template parameter: `ACTION_WRITER_MODEL` is `!Ref AskJudgeModel`.
  - The writer also inherits `ASK_JUDGE_FALLBACKS`.
  - `parse_reasoning` already handles muse's `off` when given the model.
- **#10 (keys).** An environment-scoped `production` secret overrides the repo secret with no
  workflow change. A second key on the same account still shares the balance.
- **#2.** The planner never sees evidence, so anchoring is the writer's and verifier's job.
- **Recall is not value-aware today.** U2 counted as matched in 3 of 4 Muse runs while
  labelled unsupported.
