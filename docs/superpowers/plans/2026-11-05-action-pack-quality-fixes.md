# Action pack quality fixes: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the pipeline defects the TEST eval measured, so that the chosen configuration
passes all four action-pack gates in two consecutive gold-v2 runs. The chosen configuration
is a Muse writer with an Opus 5.5 verifier.

**Architecture:** The guards are code-first.
- The planner declares scope, targets, stated values and sub-tasks.
- Gather resolves each target in code.
- A scope gate after `validate_draft` enforces target scope and field provenance.
- The writer sees dated evidence.
- The verifier gets the user's values and the ref dates, parses answers tolerantly, and
  records per check which model answered. Its hiding policy is unchanged (switch default
  `on`).
- Writer routing becomes independent of the Ask judge.

**Tech Stack:** Python 3.11 Lambdas, Aurora Postgres (psycopg 3), SAM `template.yaml`,
GitHub Actions deploy workflows, OpenRouter, and the Claude API.

**Spec:** `docs/superpowers/specs/2026-11-05-action-pack-quality-fixes-design.md` (rev 2).
Section references such as "spec §3.2.4" point there. The base design is
`docs/superpowers/specs/2026-11-01-background-action-agent-design.md`.

## Global Constraints

**Branches and merging.**
- Each PR gets its own branch off `origin/develop`, and lands on `develop` in the order
  PR-1 → PR-7. PR-4 waits for the eval-branch gate E3.
- Stage files with `git add <path>` only. Never `git add -A`, never `git stash`.
- Commit trailers on every commit:
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` and
  `Claude-Session: https://claude.ai/code/session_014QUZA3uk6P8T7qYJoLRfQe`.

**Tests.**
- Python is `C:\Users\camil\fspy\Scripts\python.exe`.
- Unit tests: `C:\Users\camil\fspy\Scripts\python.exe -m pytest -q tests/unit/<file>`.
- Integration tests run against local Postgres through the `fswork/run-tests.sh` harness:
  one pytest at a time, `PGTZ=UTC`. Fake-connection unit tests never execute SQL, so any new
  SQL needs an integration test.
- CI is the source of truth. Local bench tests may fail on CRLF; check CI before treating a
  failure as real.

**Data and logs.**
- `run_trace` stays counts-shaped and must pass `action_pack_view.fail_trace_error`. Keys
  match `^[A-Za-z0-9_]{1,64}$`, and string values match `^[a-z0-9_:.-]{1,40}$`.
- Logs carry ids, counts and codes only. Never instruction text, an excerpt, a reason or a
  model answer.

**Models.**
- Claude ids go to the Claude API only (`llm_tools_client.is_claude`), never OpenRouter.

**Settings, values fixed by the spec.**

| Setting | Value |
|---|---|
| `TARGET_MATCH_MIN` | 0.8 |
| Ambiguity margin | 0.15 |
| Target lookups | ≤ 4 |
| `targets` / `stated_values` / `delegations` | each ≤ 10 |
| `sub_tasks` | ≤ 8 |
| Note text | ≤ 4,000 (`BODY_MAX_CHARS`) |
| `WRITE_MAX_TOKENS` | 12,000 |
| `ACTION_MAX_ROUNDS` | default 8, clamped 1–8 |
| `ACTION_VERIFIER_HIDE_UNSUPPORTED` | default `on` |

**Template defaults:**
- `ActionWriterModel` = `meta/muse-spark-1.3-contributor`;
- `ActionVerifierModel` = `claude-opus-5-5`;
- `ActionVerifierFallbacks` = `claude-sonnet-5-5,google/gemini-3.8-flash`;
- `ActionWriterReasoning`, `ActionWriterProvider` and `ActionWriterFallbacks` = `''`.

**Writer routing.** When `ACTION_WRITER_MODEL` is set, only the `ACTION_WRITER_*` settings
apply, even when it equals `ASK_JUDGE_MODEL`. When it is empty, today's judge routing applies,
byte for byte.

**Applicability does not change.** `unsupported` and `not_checked` stay non-applicable.

**Eval branch.** Tasks marked **[eval-branch, local only]** run on the local
`eval/action-pack-gold-v1` worktree. They ship no repo path, are never pushed, and never copy
names or quotes into this repo.

## Review Focus

These are failure modes the spec implies but no single task's happy-path test covers. Each
one has a pinned test in the task named.

1. **An instruction in Chinese naming an item by a CJK phrase.** Target containment must
   tokenise CJK per character and still resolve. Pinned in Task 3.2.
2. **A pack stored before the migration runs on that database** (a TEST deploy ahead of the
   migration). Store must retry without the new columns, and GET must still read the pack.
   Pinned in Task 5.1.
3. **The writer stops on the token limit with the new, larger output.** The answer must be
   counted in `write_finish_length` and take the repair path, not crash. Pinned in Task 4.5.
4. **A verifier outage on every candidate.** The pack must still fail `verifier`, and no
   element may become applicable. Pinned in Task 7.4.
5. **A named target the user gave by task number that is already closed.** It must become an
   "already done" note, never a redirect to an open look-alike. Pinned in Task 4.2.

---

## PR-1: writer and verifier configuration (spec §8)

Branch `feat/action-pack-writer-config`. Deploying it changes TEST's effective writer to Muse
(spec §8.2).

### Task 1.1: explicit routing in `complete_json`

**Files:**
- Modify: `src/llm_tools_client.py` (`complete_json`, `ChatTurn`)
- Test: `tests/unit/test_llm_tools_client_json.py`

**Interfaces:**
- Produces:
  `complete_json(prompt, *, model=None, max_tokens=2048, deadline=None, caller="ask_judge", attempt_timeout=None, judge_routing=True, chain: list[str] | None = None, primary_extras: dict | None = None) -> ChatTurn`.
  - When `chain` is given, it is the whole chain; `judge_routing` and the env are ignored.
    `primary_extras` goes on index 0 only, and never on a Claude entry.
- Produces: `ChatTurn.finish_reason: str = ""`. The OpenRouter answer's
  `choices[0].finish_reason`, or Anthropic's `stop_reason` mapped so that `max_tokens`
  becomes `length`.

- [ ] **Step 1: Write the failing tests.**
  - `test_explicit_chain_ignores_judge_env`: with `ASK_JUDGE_MODEL=x`,
    `ASK_JUDGE_PROVIDER=coreweave/fp8`, `ASK_JUDGE_REASONING=high` and
    `ASK_JUDGE_FALLBACKS=y`, call
    `complete_json(p, chain=["x"], primary_extras={})`. The payload has no `provider` or
    `reasoning` key, and only one attempt is made on a 5xx.
  - `test_explicit_chain_extras_on_primary_only`:
    `chain=["meta/muse-spark-1.3-contributor", "deepseek/x"]` with
    `primary_extras={"reasoning": {"effort": "high"}}`. The first payload has reasoning; the
    fallback payload does not.
  - `test_finish_reason_length_recorded`: an OpenRouter answer with
    `finish_reason: "length"` gives `turn.finish_reason == "length"`. An Anthropic answer with
    `stop_reason: "max_tokens"` gives `"length"`.
  - `test_no_chain_is_byte_identical`: the existing judge tests still pass. Run the whole
    file.
- [ ] **Step 2: Run** `C:\Users\camil\fspy\Scripts\python.exe -m pytest -q tests/unit/test_llm_tools_client_json.py`.
  Expect the three new tests to FAIL.
- [ ] **Step 3: Implement** the `chain` / `primary_extras` parameters and `finish_reason` in
  `src/llm_tools_client.py`.
- [ ] **Step 4: Run** the same file, plus `test_llm_tools_client.py` and
  `test_llm_tools_client_anthropic.py`. Expect PASS.
- [ ] **Step 5: Commit** `feat(llm): explicit chain and extras for complete_json; record finish_reason`.

### Task 1.2: writer routing from `ACTION_WRITER_*`

**Files:**
- Modify: `src/lambda_action_agent.py`
  - add `WriterRouting`, `writer_routing()`, `verifier_hide_unsupported()`;
  - keep `writer_model()`.
- Modify: `src/action_pack_stages.py` (`judge_call`, `Run.model`)
- Test: `tests/unit/test_action_agent_handler.py` (routing helpers),
  `tests/unit/test_action_pack_stages.py` (judge_call)

**Interfaces:**
- Produces:
  `@dataclass WriterRouting(model: str, chain: list[str] | None, extras: dict | None, explicit: bool)`
  and `writer_routing() -> WriterRouting`.
  - When `ACTION_WRITER_MODEL` is set, `explicit=True` and:
    - `chain` = `[model]` + `ACTION_WRITER_FALLBACKS` (deduplicated);
    - `extras` holds `provider` from `llm_utils.parse_provider(ACTION_WRITER_PROVIDER)` and
      `reasoning` from `llm_utils.parse_reasoning(ACTION_WRITER_REASONING, model=model, name="ACTION_WRITER_REASONING")`,
      each omitted when None.
  - Otherwise `explicit=False`, `chain=None`, `extras=None`.
- Produces: `verifier_hide_unsupported() -> bool`. It is `False` only for the literal `off`
  (case-insensitive, stripped); anything else is `True`.
- `judge_call` passes `chain` and `primary_extras` when `explicit`, and calls
  `complete_json` exactly as today otherwise.

- [ ] **Step 1: Write the failing tests.**
  - `test_writer_routing_explicit_ignores_judge` (set `ACTION_WRITER_MODEL` equal to
    `ASK_JUDGE_MODEL`, with judge pin, reasoning and fallbacks set):
    `chain == [model]` and `extras == {}`.
  - `test_writer_routing_muse_off_dropped`: `ACTION_WRITER_REASONING=off` with the Muse
    model gives `extras == {}`.
  - `test_writer_routing_unset_is_judge`: `explicit is False`.
  - `test_judge_call_passes_explicit_chain`: patch `complete_json` and assert its kwargs.
  - `test_verifier_hide_default_on`: unset or `ON` gives True; `off` gives False.
- [ ] **Step 2: Run** both files. Expect FAIL.
- [ ] **Step 3: Implement** the code.
- [ ] **Step 4: Run** both files. Expect PASS.
- [ ] **Step 5: Commit** `feat(action-agent): writer routing independent of the Ask judge`.

### Task 1.3: template parameters, workflows and env parity

**Files:**
- Modify: `src/template.yaml`:
  - new parameters `ActionWriterModel`, `ActionWriterReasoning`, `ActionWriterProvider`,
    `ActionWriterFallbacks`, and `ActionVerifierHideUnsupported` (`AllowedValues: ['on','off']`,
    `Default: 'on'`);
  - `ActionVerifierModel` default `claude-opus-5-5`;
  - `ActionVerifierFallbacks` default as in Global Constraints;
  - `ActionAgentFunction` env: `ACTION_WRITER_MODEL: !Ref ActionWriterModel` (replacing
    `!Ref AskJudgeModel`), plus the four new variables.
- Modify: `.github/workflows/deploy.yml` and `.github/workflows/deploy-prod.yml`: add
  `TEST_`/`PROD_` variables and `*_PARAM` lines for each new parameter, following the
  `ACTION_VERIFIER_MODEL_PARAM` block and its bare-`Key` guard, and append them to the
  `sam deploy` parameter list.
- Test: `tests/unit/test_action_agent_template.py`,
  `tests/unit/test_template_workflow_parameter_wiring.py` (the tuple list around line 1380)

- [ ] **Step 1: Write the failing tests.**
  - `test_action_agent_env_has_writer_settings`: the five env keys are present and
    `ACTION_WRITER_MODEL` refs `ActionWriterModel`.
  - `test_action_template_defaults`: the defaults are exactly the Global Constraints values.
  - Add five tuples to the wiring list, so its parametrised test covers them in both
    workflows.
- [ ] **Step 2: Run** both files. Expect FAIL.
- [ ] **Step 3: Edit** the template and both workflows. Keep the workflow YAML LF
  (`.gitattributes` enforces it).
- [ ] **Step 4: Run** both files, then cfn-lint at the CI pin and `sam validate --lint`.
  Expect PASS and no lint errors.
- [ ] **Step 5: Commit** `feat(template): action writer/verifier parameters and workflow wiring`.

### Task 1.4: PR-1 TEST verification

- [ ] Open the PR to `develop`. Check that the base is `develop`, then merge on green CI.
  Wait for the TEST `deploy` run.
- [ ] Use AWS profile `fieldsight-deployer`. Read `ActionAgentFunction`'s env and confirm
  `ACTION_WRITER_MODEL=meta/muse-spark-1.3-contributor`,
  `ACTION_VERIFIER_MODEL=claude-opus-5-5` and `ACTION_VERIFIER_HIDE_UNSUPPORTED=on`.
- [ ] Run gold C1 once through the harness. The stored pack's `models.writer` is the Muse id,
  `models.verifier` is `claude-opus-5-5`, and the pack is `ready`.

**Acceptance:**
- Tasks 1.1–1.3 are green in CI.
- On TEST, the writer is Muse and the verifier is Opus.
- With `ACTION_WRITER_MODEL` empty, the judge payload is unchanged (Task 1.1's last test).

**Revert:** set `TEST_ACTION_WRITER_MODEL` to an explicit judge id, or revert the PR. The
code with the variable empty restores judge routing exactly. No data changes.

---

## E: eval-branch work (part 1)

### Task E1: harness changes **[eval-branch, local only]**

Implement on the local eval branch, with tests in its own test files (spec §10). Behaviours:

- **Value-aware recall.**
  - Expectations carry scored fields. A match counts only when the values agree:
    - close: status in `{done, closed}`;
    - update: each listed field;
    - R3: the raised-on date;
    - U2: any listed alternative.
  - The report gives `matched_value_ok` and `matched_value_wrong`. The gate uses
    `matched_value_ok`.
- **Type-mismatch warning.** An annotator match to an expectation of another type is listed
  as a warning, with a `type_mismatch_dropped` count.
- **`allow_split: true`** lets several actions of the expectation's type fill one
  expectation, for that expectation only.
- **Repeats.**
  - `submit` takes `--only <ids>` and `--repeat N`.
  - The report gives per-id hit rates.
  - Multi-run reports give the mean, min and max per gate, and pass a configuration only when
    every run passes.
- **Resolution scoring.** Score `run_trace.target_resolution` against gold
  `target_stable_id` and `must_not_touch`: resolved-correct, resolved-wrong, ambiguous, none.
- **Report-only metrics:**
  - a per-pack `scope_gate` summary;
  - sub-task coverage;
  - `write_dropped` by type and reason;
  - verifier answers by model and attempt;
  - `not_checked`;
  - fabrication split by verifier label.
- **The blind item** also excludes `verifier_detail` and any caution text.

Verify that the eval branch's own tests pass. Commit locally only and never push.

### Task E2: gold v2 and the baseline **[eval-branch, local only]**

- [ ] Gold v2:
  - add C4's newer look-alike to its `must_not_touch`;
  - set `allow_split: true` on S2's note expectation;
  - mark scored fields on every expectation.
- [ ] Record the new gold sha256.
- [ ] Run the full v2 set twice on TEST with PR-1 deployed. Label it, with a blind second
  annotator on both runs, and score it. Record the floor with `compare` (two baselines, same
  config label).

---

## PR-2: dates and the item view (spec §6; verifier ref dates, spec §7.2)

Branch `feat/action-pack-dated-evidence`.

### Task 2.1: items SQL returns `deadline_text` and `raised_on`

**Files:**
- Modify: `src/repositories/topics.py` (`_scoped_items`, both ledger shapes)
- Test: `tests/integration/test_rag_search_items_sql.py`

**Interfaces:**
- Produces two new row keys, `deadline_text` (text or None) and `raised_on` (date).
- `raised_on` is the minimum `topics.report_date` over **all** `action_items` rows with the
  same `stable_id`, with no visibility or supersession filter, as a correlated subquery on
  `action_items`/`topics`.

- [ ] **Step 1: Write the failing tests.**
  - `test_items_rows_include_deadline_text`.
  - `test_raised_on_is_earliest_row_even_if_superseded`: three rows of one `stable_id` on
    topics dated D1 < D2 < D3. The D1 topic is superseded and hidden. The result has
    `raised_on == D1`.
- [ ] **Step 2: Run** with the integration harness (`fswork/run-tests.sh`, this file only).
  Expect FAIL.
- [ ] **Step 3: Implement.** Add an index migration only if an `EXPLAIN` on local Postgres
  shows a sequential scan on `action_items.stable_id`. Otherwise add nothing.
- [ ] **Step 4: Run.** Expect PASS.
- [ ] **Step 5: Commit** `feat(items): deadline_text and raised_on in the agent items read`.

### Task 2.2: items tool fields

**Files:**
- Modify: `src/ask_items.py` (`ITEM_FIELDS` gains `"deadline_text"` and `"raised_on"`).
- Modify: `src/lambda_rag_search.py` (`_items`): `raised_on` goes through `_iso` like the
  other dates.
- Test: `tests/unit/test_ask_items.py`

- [ ] **Step 1: Write the failing test.** `test_item_fields_always_present_new_keys`: an
  envelope item has both keys, None included.
- [ ] **Step 2: Run** and see it FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** and see it PASS.
- [ ] **Step 5: Commit** `feat(ask-items): carry deadline_text and raised_on`.

### Task 2.3: the agent's dated renderer and today header

**Files:**
- Modify: `src/action_pack_stages.py`:
  - new `_record_line(item) -> str`;
  - `_line` uses `_record_line` instead of `ask_orchestrator._item_line` for topic and chunk;
    the item branch adds `deadline_text` and `raised_on`;
  - the `today` header in `PLAN_PROMPT`, `ROUND_PROMPT` and `WRITE_PROMPT`;
  - new helper `_day(date_str) -> str`.
- Test: `tests/unit/test_action_pack_stages.py`

**Interfaces:**
- `_day("2026-11-06")` returns `"2026-11-06 (Fri)"`. An invalid or empty value returns
  `"-"`.
- `_record_line` returns `f"{ref} | said_on {_day(date)} {time} | {site} | {who} | {title} — {text}"`,
  with the same sanitising as `_item_line`.
- The item line adds ` | deadline_text "<sanitised>"` (when set) and
  ` | raised_on {_day(raised_on)}`.
- The headers read `Today (New Zealand): {_day(today)} — today is not the date the records were said.`
- `WRITE_PROMPT` gains two rules, worded generally:
  - resolve a relative date in a record against that line's `said_on`;
  - resolve an item's `deadline_text` against its `raised_on`.

- [ ] **Step 1: Write the failing tests.**
  - `test_record_line_has_said_on_weekday`.
  - `test_item_line_has_deadline_text_and_raised_on`.
  - `test_write_prompt_states_today_with_weekday`.
  - `test_ask_item_line_unchanged`: `ask_orchestrator._item_line` on a fixture equals the
    string pinned from today's output.
- [ ] **Step 2: Run** and see them FAIL (except the last).
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `test_action_pack_stages.py`, `test_action_pack_stages_fix1.py` and
  `test_action_pack_final_fix.py`. Expect PASS.
- [ ] **Step 5: Commit** `feat(action-agent): dated evidence lines and today header`.

### Task 2.4: verifier ref dates

**Files:**
- Modify: `src/action_pack_verify.py` (`build_prompt`): each evidence line becomes
  `ref | kind | said_on {_day(date)} | text`. The date comes from `registry[ref]["item"]`:
  `date` for topics and chunks, `raised_on` for items, `-` otherwise. A targeted action's
  target line adds `deadline_text` and `raised_on`.
- Test: `tests/unit/test_action_pack_verify.py`

- [ ] **Step 1: Write the failing test.** `test_verify_prompt_carries_ref_dates`.
- [ ] **Step 2: Run** and see it FAIL.
- [ ] **Step 3: Implement.** Import `_day` from `action_pack_stages`.
- [ ] **Step 4: Run** and see it PASS.
- [ ] **Step 5: Commit** `feat(action-verify): ref dates in the verifier prompt`.

### Task 2.5: PR-2 TEST verification

- [ ] Merge to `develop` and wait for the TEST deploy.
- [ ] **[eval-branch]** Run U1, U2 and R3 three times each with `--only U1,U2,R3 --repeat 3`.
  Measure value-aware hit rates and record them. The gate for these ids is after PR-4
  (R3 needs the assumed default). Here, record whether U1 and U2 improve against the E2
  baseline.

**Acceptance:**
- Tasks 2.1–2.4 are green.
- Ask's renderer is unchanged.
- On TEST, a pack's write render shows `said_on` and `raised_on`. Check this from a unit
  fixture, not from logs: logs never carry evidence.

**Revert:** revert the PR. No schema change, unless 2.1 added an index; that index is
harmless and stays.

---

## PR-3: plan schema v2 and target resolution, no enforcement (spec §3.2.1–3.2.2, §4.2 plan side, §11)

Branch `feat/action-pack-plan-v2`.

### Task 3.1: tolerant Plan v2 keys

**Files:**
- Modify: `src/action_pack_stages.py`:
  - the `Plan` dataclass gains `change_scope: str = "named"`, `targets: list`,
    `stated_values: list`, `delegations: list` and `sub_tasks: list`;
  - `validate_plan` and `PLAN_PROMPT`;
  - `_PLAN_KEYS` stays the required core;
  - new `_PLAN_V2_KEYS`.
- Test: `tests/unit/test_action_pack_stages.py`

**Interfaces:**
- `validate_plan` accepts `set(obj)` ⊇ `_PLAN_KEYS` and ⊆ `_PLAN_KEYS ∪ _PLAN_V2_KEYS`.
  Each v2 key is validated by its own `_v2_<key>(value) -> list | None`. On None, the field
  becomes empty and `defaulted` collects the key name.
- The return becomes `(Plan, None)` with `plan.defaulted: list[str]`.
- `plan_stage` writes these to the trace:
  - `run_trace["plan_scope"]`;
  - `run_trace["plan_key_defaulted"]`: a list of key names;
  - `run_trace["plan_targets"]`;
  - `run_trace["plan_sub_tasks"]`.
- Shapes are as in spec §3.2.1 and §4.2. Target ids, sub-task ids and value fields are
  checked with closed sets and length caps. `stated_values[].value` for `deadline` must be
  ISO.

- [ ] **Step 1: Write the failing tests.**
  - `test_plan_v1_answer_still_valid`: an old-shape answer is accepted with scope `named` and
    empty v2 lists.
  - `test_plan_v2_malformed_key_defaulted`: a bad `targets` is emptied and listed in
    `defaulted`.
  - `test_plan_invalid_scope_is_named`.
  - `test_plan_v2_caps`: 11 targets are defaulted.
  - `test_plan_unknown_extra_key_rejected`: the core strictness still holds.
- [ ] **Step 2: Run** and see them FAIL.
- [ ] **Step 3: Implement.** Extend `PLAN_PROMPT` with the five keys and their rules (spec
  §3.2.1, §4.2).
- [ ] **Step 4: Run** and see them PASS.
- [ ] **Step 5: Commit** `feat(action-agent): plan v2 keys, tolerant`.

### Task 3.2: phrase containment matcher

**Files:**
- Create: `src/action_pack_targets.py`
- Test: `tests/unit/test_action_pack_targets.py` (new)

**Interfaces:**
- `TARGET_MATCH_MIN = 0.8` and `AMBIGUITY_MARGIN = 0.15`.
- `tokens(text: str) -> list[str]`:
  - normalise with `carry_forward._key(todo_collapse.normalise(text))`;
  - Latin text splits into words, and every CJK character is its own token;
  - drop `STOP_WORDS` (a small fixed English set: the, a, an, of, to, for, on, in, at, and,
    or, item, task).
- `containment(phrase: str, text: str) -> float`: the share of the phrase's distinct tokens
  found in the text's tokens. Returns 0.0 for an empty phrase.
- `@dataclass Resolution(kind: str, stable_id: str | None, candidates: list[str])`, where
  `kind` is `resolved`, `ambiguous` or `none`.
- `resolve(target: dict, items: dict[str, dict]) -> Resolution`:
  - `task_no` match first; a numbered target never falls back to text;
  - then containment over `items.values()`, with the resolved/ambiguous/none rules from spec
    §3.2.2.

- [ ] **Step 1: Write the failing tests.**
  - `test_short_phrase_matches_long_item`.
  - `test_two_close_items_ambiguous`.
  - `test_cjk_phrase_resolves`: a Chinese phrase inside a longer Chinese item text.
  - `test_task_no_wins_and_never_falls_back`.
  - `test_below_threshold_none`.
  - `test_done_item_still_resolves`: the status is ignored here.
- [ ] **Step 2: Run** and see them FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** and see them PASS.
- [ ] **Step 5: Commit** `feat(action-agent): target phrase containment matcher`.

### Task 3.3: concurrent resolution hops in round 1

**Files:**
- Modify: `src/action_pack_stages.py`:
  - `round1` runs the day/docs `_execute` and the items calls in a `ThreadPoolExecutor`;
  - new `resolve_targets(run) -> dict`;
  - `Run` gains `resolution: dict`.
- Test: `tests/unit/test_action_pack_stages.py`

**Interfaces:**
- **Calls.** One `ask_items.run` per target, up to 4, with
  `{"status": ["open","in_progress","blocked","done","closed"], "task_no" or "query", "limit": 50}`
  and `call_id=f"target_{id}"`.
- **Budget per hop:** `min(ask_items.MAX_DEADLINE_S, ctx.left("gather") - ROUND_FLOOR_S)`.
  Under 5 s, the hop is skipped and the target is `none`
  (`run.trace["target_resolve_time"] += 1`).
- **Concurrency.** The items calls run concurrently with the round-1 batch, so the wall time
  is one hop.
- **Matching.** The returned items enter the registry as usual. `resolve()` runs over the
  union of each hop's items and `run.items`.
- **Trace:** `run.trace["target_resolution"] = {tid: stable_id | "ambiguous" | "none"}`.

- [ ] **Step 1: Write the failing tests.**
  - `test_round1_issues_target_calls_all_statuses`.
  - `test_target_calls_run_concurrently`: fake `items_run` sleeping 0.2 s; four targets
    finish in < 0.5 s.
  - `test_target_hop_skipped_without_time`.
  - `test_target_resolution_in_trace_is_counts_shaped`: run `fail_trace_error` on the trace
    and expect None.
- [ ] **Step 2: Run** and see them FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** and see them PASS.
- [ ] **Step 5: Commit** `feat(action-agent): resolve named targets concurrently in round 1`.

### Task 3.4: show the targets to the writer

**Files:**
- Modify: `src/action_pack_stages.py` (`write_stage`, `WRITE_PROMPT`): add a
  `Named targets:` block.

**Interfaces:**
- One line per target:
  - resolved: `t1: R7 (open)`;
  - ambiguous: `t1: AMBIGUOUS — R4, R9: write a note, change neither`;
  - done or closed: `t2: R7 (done) — already done`;
  - none: `t3: NOT FOUND — write a note`.
- Refs come from the registry entry of each `stable_id`.
- Test: `tests/unit/test_action_pack_stages.py`

- [ ] **Step 1: Write the failing test.** `test_write_prompt_lists_named_targets`, covering
  the four shapes.
- [ ] **Step 2: Run** and see it FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** and see it PASS.
- [ ] **Step 5: Commit** `feat(action-agent): named targets in the write render`.

### Task 3.5: the `ACTION_MAX_ROUNDS` knob

**Files:**
- Modify: `src/lambda_action_agent.py` (new `max_rounds() -> int`: default 8, clamped 1–8,
  non-int gives 8).
- Modify: `src/action_pack_stages.py`: `model_rounds` reads `agent.max_rounds()` instead of
  `MAX_ROUNDS`. `MAX_ROUNDS` stays as the ceiling.
- Modify: `src/template.yaml`: `ActionMaxRounds`, default `'8'`, and the env entry.
- Modify: both workflows, with the `TEST_`/`PROD_ACTION_MAX_ROUNDS` param.
- Test: `tests/unit/test_action_agent_handler.py`, `tests/unit/test_action_pack_stages.py`,
  `tests/unit/test_template_workflow_parameter_wiring.py`,
  `tests/unit/test_action_agent_template.py`

- [ ] **Step 1: Write the failing tests.**
  - `test_max_rounds_clamps`: 0 → 1, 99 → 8, `"x"` → 8.
  - `test_model_rounds_honours_knob`: with 2, at most 1 model round runs.
  - A wiring tuple and the env-parity key.
- [ ] **Step 2: Run** and see them FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** and see them PASS, then lint the template.
- [ ] **Step 5: Commit** `feat(action-agent): ACTION_MAX_ROUNDS knob`.

### Task 3.6: PR-3 TEST deploy

- [ ] Merge to `develop` and wait for the TEST deploy.
- [ ] Confirm one C1 pack's `run_trace` has `plan_scope` and `target_resolution`.

**Acceptance:**
- Tasks 3.1–3.5 are green.
- No behaviour change to actions: no gate yet.
- The E1 harness reads `target_resolution`.

**Revert:** revert the PR. Older plans remain valid. `ACTION_MAX_ROUNDS=8` equals today.

### Task E3: blocking target-resolution gate **[eval-branch, local only]**

- [ ] Run C1–C5, U1–U5, R3 and M2 with `--repeat 3`.
- [ ] Score the resolution (Task E1). Pass requires all of:
  - ≥ 90% of expectation targets resolved to the gold `stable_id`, pooled;
  - **0** resolved to a wrong `stable_id`;
  - no `must_not_touch` resolution on C4, C5 or U5.
- [ ] On a miss, tune `TARGET_MATCH_MIN`, `STOP_WORDS` or the plan's phrase rule in a
  follow-up PR on `develop`, redeploy, and re-measure. **PR-4 does not merge until this
  passes.** Record the numbers in the PR-4 description.

---

## PR-4: scope gate and field provenance (spec §3.2.3–3.2.4, §11 write size)

Branch `feat/action-pack-scope-gate`. Depends on PR-2, PR-3 and E3.

### Task 4.1: writer keys `target_ref` and `field_sources`

**Files:**
- Modify: `src/action_pack_stages.py`:
  - `validate_draft`: the action key allowlist gains `target_ref` and `field_sources`;
  - new `_field_sources(raw, proposed) -> dict | None`;
  - `WRITE_PROMPT`: the schema and the scope sentence from spec §3.2.3.
- Test: `tests/unit/test_action_pack_stages.py`

**Interfaces:**
- Validated actions carry `target_ref: str | None` and
  `field_sources: {field: {"source": str, "refs": [str], "said_on": str | None}}`.
- `source` is in `{user, evidence, assumed_default}`.
- Refs are filtered to citable refs.
- A missing or malformed entry leaves that field's source as None (the gate then removes the
  field).

- [ ] **Step 1: Write the failing tests.**
  - `test_validate_draft_keeps_field_sources`.
  - `test_field_sources_bad_source_is_none`.
- [ ] **Step 2: Run** and see them FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** and see them PASS.
- [ ] **Step 5: Commit** `feat(action-agent): writer declares targets and field sources`.

### Task 4.2: scope gate rules 1–2 (target scope and named mismatch)

**Files:**
- Create: `src/action_pack_scope.py`
- Test: `tests/unit/test_action_pack_scope.py` (new)

**Interfaces:**
- Produces:
  `gate(draft: dict, *, plan, resolution: dict, items: dict, registry: dict) -> tuple[dict, dict]`,
  returning the gated draft and counts.
  - `resolution` is `{tid: Resolution}` from Task 3.3. `Run` keeps the objects; the trace
    keeps the codes.
  - Demoted notes are `{"type": "note", "target_stable_id": None, "proposed": {"text": <rationale + suffix>}, "rationale": ..., "refs": ...}`.
    The text is capped at `BODY_MAX_CHARS`.
  - Counts use the spec codes: `drop_unasked_target`, `drop_named_mismatch`,
    `demoted_ambiguous`, `demoted_target_none`, `demoted_target_closed`.
  - Rule 1: the target must have a status in `action_pack_verify.OPEN_STATUSES`, applied
    before the scope check.
  - The note suffixes live in a new dict `NOTE_SUFFIX` with en and zh entries, picked through
    `_lang_key(plan.language)` as `SITE_NOT_FOUND` is. The English entries are:
    - `"matches more than one item: R4, R9"`;
    - `"named item not found"`;
    - `"already done"`.

- [ ] **Step 1: Write the failing tests.**
  - `test_m1_shape_unasked_updates_dropped` (scope `named`, three updates on unnamed items).
  - `test_c4_lookalike_dropped` (`target_ref` resolves to another `stable_id`).
  - `test_ambiguous_target_demoted_to_note`.
  - `test_target_none_demoted_to_note`.
  - `test_closed_named_target_by_task_no_becomes_already_done_note` (Review Focus 5).
  - `test_tidy_allows_unnamed_open_item`.
  - `test_tidy_rejects_closed_item`.
  - `test_demoted_note_suffix_zh` (pack language zh).
- [ ] **Step 2: Run** and see them FAIL.
- [ ] **Step 3: Implement** rules 1–2.
- [ ] **Step 4: Run** and see them PASS.
- [ ] **Step 5: Commit** `feat(action-agent): scope gate — target scope and named mismatch`.

### Task 4.3: scope gate rule 3 (field provenance, user override, assumed default)

**Files:**
- Modify: `src/action_pack_scope.py`
- Test: `tests/unit/test_action_pack_scope.py`

**Interfaces** (rule 3 inside `gate`):
- **`user`.** Look up `plan.stated_values` by (target id or None, field). Missing → remove the
  field (`field_unsourced__<field>`). Present → set the value to the stated value; on a
  normalised difference, count `user_value_overridden__<field>`.
  - Normalisation: ISO for dates, lower-case for status and priority, casefold for
    responsible.
- **`evidence`.**
  - At least one ref whose registry kind is in `{topic, chunk, document}`, not counting the
    target's own item ref.
  - Exception: `deadline` when the target item has `deadline_text` and the refs include its
    own item ref.
  - Web refs never count.
- **`assumed_default`.** Only for `deadline`; only with a matching `plan.delegations` entry;
  only when the item has neither `deadline` nor `deadline_text`. Set the value to
  `items[sid]["raised_on"]` and mark the action `assumed=["deadline"]` for Change 5
  (`date_assumed`). Same-day: no change.
- **An update left with no fields** → `drop_no_sourced_fields`.
- **`create_item` optional fields** follow the same rules. The text keeps its refs rule.

- [ ] **Step 1: Write the failing tests.**
  - `test_priority_without_source_removed`.
  - `test_item_own_ref_not_evidence`.
  - `test_deadline_text_own_ref_is_evidence`.
  - `test_user_value_overrides_writer` (counted, field kept).
  - `test_user_without_stated_value_removed`.
  - `test_r3_assumed_default_uses_raised_on`.
  - `test_assumed_default_same_day_is_today`.
  - `test_assumed_default_refused_when_deadline_text_present`.
  - `test_web_ref_cannot_source_field`.
  - `test_close_needs_source`.
- [ ] **Step 2: Run** and see them FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** and see them PASS.
- [ ] **Step 5: Commit** `feat(action-agent): scope gate — field provenance`.

### Task 4.4: scope gate rule 4 (deadline sanity)

**Files:**
- Modify: `src/action_pack_scope.py`
- Test: `tests/unit/test_action_pack_scope.py`

**Interfaces:**
- An `evidence` deadline needs `said_on` equal to the date of one of its refs: `date` for
  topics and chunks, `raised_on` for the own item ref. Otherwise
  `field_said_on_mismatch`.
- A deadline earlier than `said_on` → `field_deadline_before_source`.

- [ ] **Step 1: Write the failing tests.**
  - `test_said_on_mismatch_removes_deadline`.
  - `test_deadline_before_source_removed`.
- [ ] **Step 2: Run** and see them FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** and see them PASS.
- [ ] **Step 5: Commit** `feat(action-agent): scope gate — deadline sanity`.

### Task 4.5: wire the gate in; larger write budget

**Files:**
- Modify: `src/action_pack_stages.py`:
  - `write_stage` calls `action_pack_scope.gate` after `validate_draft`;
  - `run.trace["scope_gate"] = counts`;
  - `WRITE_MAX_TOKENS = 12000`;
  - `judge_call` puts `finish_reason` into `info`;
  - `Run.model` counts `write_finish_length` when `info["finish_reason"] == "length"` and the
    caller is `action_write`.
- Test: `tests/unit/test_action_pack_stages.py`

- [ ] **Step 1: Write the failing tests.**
  - `test_write_stage_applies_scope_gate`.
  - `test_write_length_stop_counted_and_repaired` (Review Focus 3): the fake model first
    returns truncated JSON with finish `length`, then valid JSON. The trace has
    `write_finish_length == 1` and `write_repairs == 1`, and the pack is built.
  - `test_scope_gate_trace_counts_shaped`.
- [ ] **Step 2: Run** and see them FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the whole `tests/unit -k action_pack` set. Expect PASS.
- [ ] **Step 5: Commit** `feat(action-agent): apply the scope gate; write budget 12000`.

### Task 4.6: PR-4 TEST verification

- [ ] Merge to `develop` and wait for the TEST deploy.
- [ ] **[eval-branch]** Run M1, S3, S4, C4, U2 and R3 with `--repeat 3`. Measure:
  - no shown `update_item` on an M1 `must_not_touch` item;
  - zero wrong targets on C4 and S4;
  - R3/e1 value-ok in ≥ 2 of 3 runs;
  - U2/e1 value-ok in ≥ 2 of 3 runs;
  - the per-pack `scope_gate` summary, to check for removed fields that were right.

**Acceptance:** all of the above. The p90 of these runs is ≤ 300 s.

**Revert:** revert the PR. Everything upstream of the gate (PR-3) is inert without it.

---

## PR-5: sub-task accounting, the migration, legacy-column tolerance (spec §4, §9)

Branch `feat/action-pack-subtasks`.

### Task 5.1: migration and legacy-column tolerance

**Files:**
- Create: `src/migrations/0097_action_pack_subtasks_verifier_detail.sql`. Use the next free
  number at build time; 0096 is taken.

  ```sql
  ALTER TABLE action_packs ADD COLUMN IF NOT EXISTS sub_tasks jsonb;
  ALTER TABLE action_pack_actions ADD COLUMN IF NOT EXISTS verifier_detail jsonb;
  ```
- Modify: `src/repositories/action_packs.py`:
  - `_validate_payload` allows `sub_tasks` (pack) and `verifier_detail` (action);
  - `store_result` writes them;
  - on `psycopg.errors.UndefinedColumn` / `UndefinedTable`, it rolls back to a savepoint
    taken at the start of the store and **retries the whole store** without the two keys,
    returning a flag the store task counts as `store_legacy_columns`;
  - `_PACK_COLS` / `_ACTION_COLS` reads go through `_has_new_columns(conn) -> bool`, cached
    per process and computed from `information_schema.columns`; missing means select
    without the columns and return None for them.
- Modify: `src/action_pack_view.py`: `_PACK_TYPES` gains `sub_tasks: list`, and
  `_ACTION_TYPES` gains `verifier_detail: dict`.
- Test: `tests/integration/test_action_packs_repo.py`,
  `tests/unit/test_action_pack_view.py`

- [ ] **Step 1: Write the failing tests.**
  - Integration `test_store_and_get_with_new_columns`.
  - Integration `test_store_without_migration_retries_whole_store` (Review Focus 2): apply
    the schema without the new migration, store a payload with `sub_tasks`; it succeeds, the
    pack is complete, and the legacy flag is returned.
  - Integration `test_get_old_pack_new_fields_null`.
  - Unit `test_payload_shape_accepts_sub_tasks_and_verifier_detail`.
- [ ] **Step 2: Run** each integration file alone with the harness, and the unit file.
  Expect FAIL.
- [ ] **Step 3: Implement.** Check the migration against the repo's migration parse test.
- [ ] **Step 4: Run.** Expect PASS.
- [ ] **Step 5: Commit** `feat(action-packs): sub_tasks and verifier_detail columns, legacy-tolerant store/GET`.

### Task 5.2: writer `sub_tasks` and accounting

**Files:**
- Modify: `src/action_pack_stages.py`:
  - `validate_draft` parses `sub_tasks` (spec §4.2 shape, closed `status` and `reason_code`
    sets, `reason` ≤ 300);
  - new `account_sub_tasks(plan, draft, index_map) -> list | None`, called after the scope
    gate (which returns the index map of surviving and demoted elements);
  - `build_payload` adds `sub_tasks` with action `seq`s;
  - `WRITE_PROMPT` gains the sub-task output rule.
- Modify: `src/action_pack_scope.py`: `gate` also returns `index_map: dict[int, int | None]`
  (old action index → new index, or None when dropped).
- Test: `tests/unit/test_action_pack_stages.py`, `tests/unit/test_action_pack_scope.py`

**Interfaces:**
- Returns None when the plan has no sub-tasks.
- Rules: a missing sub-task is `not_answered`; done/partly with every action dropped is
  `not_done` + `dropped_by_checks`; unknown ids are counted in
  `run.trace["subtask_unknown"]`.
- No repair call.

- [ ] **Step 1: Write the failing tests.**
  - `test_missing_subtask_not_answered`.
  - `test_done_subtask_all_dropped_becomes_not_done`.
  - `test_plan_without_subtasks_stores_null`.
  - `test_subtask_indices_follow_demotion`.
- [ ] **Step 2: Run** and see them FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** and see them PASS.
- [ ] **Step 5: Commit** `feat(action-agent): sub-task accounting`.

### Task 5.3: GET returns `sub_tasks`

**Files:**
- Modify: `src/action_pack_view.py` (`shape_pack`): it passes `sub_tasks` through (None for
  old packs).
- Test: `tests/unit/test_action_pack_view.py`, `tests/unit/test_org_api_action_packs.py`

- [ ] **Step 1: Write the failing test.** `test_shape_pack_returns_sub_tasks`, and None for an
  old pack.
- [ ] **Step 2: Run** and see it FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** and see it PASS.
- [ ] **Step 5: Commit** `feat(action-packs): GET returns sub_tasks`.

### Task 5.4: PR-5 TEST verification

- [ ] Merge to `develop`. The TEST deploy runs the migration through the repo's usual path;
  confirm the two columns exist on TEST.
- [ ] **[eval-branch]** Run M1 and M2 with `--repeat 3`. Measure:
  - every planner sub-task is stored;
  - M2 ≥ 4 of 5 and M1 ≥ 2 of 4, value-ok, in ≥ 2 of 3 runs.
- [ ] Send the UI contract line (spec §9.3, the sub-task checklist) to the ui session.

**Revert:** revert the PR's code. Keep the migration: the columns are nullable and unused
once reverted.

---

## PR-6: standards answers as notes; note schema (spec §5)

Branch `feat/action-pack-notes`. Independent of PR-3 to PR-5.

### Task 6.1: notes survive the schema

**Files:**
- Modify: `src/action_pack_stages.py`:
  - in `_proposed`, the note text cap becomes `BODY_MAX_CHARS`;
  - in `validate_draft` and `_proposed`, unknown keys on the action or inside `proposed` are
    removed and counted `extra_key__<type>` instead of dropping the element. Known keys are
    unaffected.
- Modify: `src/action_pack_view.py`: check that the store and apply paths accept 4,000
  characters on a note's `proposed.text`. Notes have no apply, so only the store shape is
  involved.
- Test: `tests/unit/test_action_pack_stages.py`

- [ ] **Step 1: Write the failing tests.**
  - `test_long_note_kept` (3,000 chars).
  - `test_stray_key_removed_and_counted`.
  - `test_note_over_4000_dropped`.
- [ ] **Step 2: Run** and see them FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** and see them PASS.
- [ ] **Step 5: Commit** `fix(action-agent): notes survive the writer schema`.

### Task 6.2: typed `write_dropped`

**Files:**
- Modify: `src/action_pack_stages.py` (`validate_draft`, `_clean_refs`, `_proposed`): count
  keys become `<type>__<reason>`, with reasons `refs`, `targets`, `transitions`, `priority`,
  `proposed`, `rationale` and `cap`. Conclusions use `conclusion__<reason>`. A type outside
  `ACTION_TYPES` uses `unknown`.
- Test: `tests/unit/test_action_pack_stages.py`, `tests/unit/test_action_pack_fail_trace.py`

- [ ] **Step 1: Write the failing tests.**
  - `test_write_dropped_keys_carry_type`.
  - `test_write_dropped_counts_shaped`.
- [ ] **Step 2: Run** and see them FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** and see them PASS.
- [ ] **Step 5: Commit** `feat(action-agent): write_dropped by type and reason`.

### Task 6.3: the standards-as-note rule

**Files:**
- Modify: `src/action_pack_stages.py` (`WRITE_PROMPT`): add the rule exactly as in spec
  §5.2. It is general wording and names no gold case.
- Test: `tests/unit/test_action_pack_stages.py`

- [ ] **Step 1: Write the failing test.** `test_write_prompt_has_standards_note_rule`: it
  asserts the presence of the three key phrases ("answer is a `note` action", the
  `create_item` condition, the `draft_rfi` condition).
- [ ] **Step 2: Run** and see it FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** and see it PASS.
- [ ] **Step 5: Commit** `feat(action-agent): standards questions are answered as notes`.

### Task 6.4: PR-6 TEST verification

- [ ] Merge to `develop` and wait for the TEST deploy.
- [ ] **[eval-branch]** Run S1–S4 with `--repeat 3`. Measure:
  - S2, S3 and S4 each have at least one shown note matching their expectation in ≥ 2 of 3
    runs;
  - S1 keeps its `create_item`;
  - record whether each planned web query was searched (`run_trace.web`). A skip means
    spec §12.2 is next.

**Revert:** revert the PR. Prompt and schema only.

---

## PR-7: verifier inputs and records (spec §7)

Branch `feat/action-pack-verifier-inputs`. Depends on PR-3 (`stated_values`) and on PR-5's
migration, or carries it if it lands first.

### Task 7.1: relaxed `parse_label`

**Files:**
- Modify: `src/action_pack_verify.py`: `parse_label(text) -> tuple[str | None, dict]`,
  returning the label and `{"reason_code", "reason", "form"}`. `_check` adapts.
- Test: `tests/unit/test_action_pack_verify.py`

**Interfaces:**
- Forms are tried in order:
  1. JSON through `action_pack_stages.parse_json_object`, with a valid `label` and extra
     keys allowed; `form` is `json` or `relaxed_json`;
  2. a leading verdict word after stripping `*`, `_`, quotes and a `label:`/`verdict:`
     prefix; `form` is `leading_word`.
- `reason_code` must be in the closed set `{partly_stated, not_in_evidence, value_not_in_evidence, date_anchor, contradicted, other}`,
  else None. `reason` is ≤ 200 chars, else truncated to 200.
- Counts: `parse_json`, `parse_relaxed_json`, `parse_leading_word`, `parse_fail`.

- [ ] **Step 1: Write the failing tests.**
  - `test_parse_fenced_with_reason`.
  - `test_parse_prose_wrapped_object`.
  - `test_parse_leading_word_weak`: "Weak — the record says …".
  - `test_parse_rejects_two_labels`: "supported or weak".
  - `test_parse_rejects_mid_prose_label`.
  - `test_parse_bad_reason_code_none`.
- [ ] **Step 2: Run** and see them FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** and see them PASS.
- [ ] **Step 5: Commit** `feat(action-verify): tolerant label parsing with reasons`.

### Task 7.2: the user-values block and the answer schema

**Files:**
- Modify: `src/action_pack_verify.py`:
  - `VERIFY_PROMPT` gets the answer schema from spec §7.2;
  - `build_prompt(kind, el, *, registry, items, user_values=None)` adds a fenced
    `USER INSTRUCTION` block holding the instruction and the `stated_values` lines, and the
    rule that a stated value counts as evidence for that field;
  - `verify(...)` gains `user_values: dict | None = None`
    (`{"instruction": str, "stated_values": list}`).
- Modify: `src/action_pack_stages.py` (`_run`): pass `user_values` from the context and
  `run.plan`.
- Test: `tests/unit/test_action_pack_verify.py`

- [ ] **Step 1: Write the failing tests.**
  - `test_prompt_has_user_values_block_fenced`.
  - `test_prompt_without_user_values_unchanged_shape`.
- [ ] **Step 2: Run** and see them FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** and see them PASS.
- [ ] **Step 5: Commit** `feat(action-verify): user values and reason codes in the verifier prompt`.

### Task 7.3: per-check `verifier_detail` and model counts

**Files:**
- Modify: `src/action_pack_verify.py`: `_check` returns `attempt` (the 1-based candidate
  index that answered), `served`, `reason_code` and `reason`. `verify` adds each element's
  detail to the answer, as `{"index", "verifier", "detail"}` for actions and
  `{"index", "confidence", "detail"}` for conclusions. It also returns
  `"verifier_answers": {model: n}`.
- Modify: `src/action_pack_stages.py`:
  - `apply_verdicts` accepts and copies `detail` (and allows the new answer keys);
  - `build_payload` puts `verifier_detail` on actions and conclusion lines;
  - `models["verifier_answers"]`;
  - `run_trace.verify` adds `answered_primary` and `answered_fallback`.
- Modify: `src/action_pack_view.py` (`shape_pack`): GET returns `verifier_detail` without
  `served`.
- Test: `tests/unit/test_action_pack_verify.py`, `tests/unit/test_action_pack_stages.py`,
  `tests/unit/test_action_pack_view.py`

- [ ] **Step 1: Write the failing tests.**
  - `test_every_checked_element_has_detail_model`.
  - `test_fallback_attempt_recorded`.
  - `test_get_omits_served`.
  - `test_code_caps_set_reason_code` (`web_only`, `not_archived`, `near_duplicate`,
    `date_assumed` from Task 4.3's `assumed`).
- [ ] **Step 2: Run** and see them FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** and see them PASS.
- [ ] **Step 5: Commit** `feat(action-verify): record the answering model per check`.

### Task 7.4: `not_checked`, hidden conclusions, the hide switch

**Files:**
- Modify: `src/action_pack_verify.py` (`verify`):
  - a failed, timed-out or unparseable check becomes `unsupported` + `reason_code: not_checked`
    (count `not_checked`);
  - an unsupported conclusion is returned as `{"index", "confidence": "uncertain", "hidden": true, "detail"}`
    instead of being left out;
  - with `agent.verifier_hide_unsupported()` False, an unsupported action whose
    `reason_code` is not in `{contradicted, not_checked}` becomes `weak`;
  - `counts["verify_hide_mode"]` is `"on"` or `"off"`.
- Modify: `src/action_pack_stages.py` (`apply_verdicts`, `build_payload`): carry `hidden` on
  conclusion lines.
- Modify: `src/action_pack_view.py`: `payload_shape_error` allows `hidden: bool` on a
  conclusion; `shape_pack` omits hidden lines.
- Test: `tests/unit/test_action_pack_verify.py`, `tests/unit/test_action_pack_stages.py`,
  `tests/unit/test_action_pack_view.py`

- [ ] **Step 1: Write the failing tests.**
  - `test_failed_check_is_not_checked_non_applicable_both_modes`.
  - `test_all_checks_fail_still_verifier_failure` (Review Focus 4): with the switch off, too.
    No element applicable.
  - `test_contradicted_stays_unsupported_both_modes`.
  - `test_hide_off_not_in_evidence_becomes_weak`.
  - `test_unsupported_conclusion_stored_hidden_and_omitted_by_get`.
  - `test_hide_mode_in_trace`.
- [ ] **Step 2: Run** and see them FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the `tests/unit -k "action_pack or action_agent"` set. Expect PASS.
- [ ] **Step 5: Commit** `feat(action-verify): not_checked, hidden conclusions, hide switch (default on)`.

### Task 7.5: PR-7 TEST verification

- [ ] Merge to `develop` and wait for the TEST deploy. Confirm
  `ACTION_VERIFIER_HIDE_UNSUPPORTED=on` on the function.
- [ ] **[eval-branch]** Do one full v2 run (this is iteration-loop step 1, Task E4). Measure:
  - verifier fallbacks ≤ 2 per run with Opus as primary;
  - `verifier_detail.model` present on every checked element;
  - no `not_checked` element applicable.
- [ ] Send the UI contract line (spec §9.3: the `weak` element shows its
  `verifier_detail.reason`) to the ui session.

**Revert:** revert the PR. The hide switch is already on (today's behaviour). The
`verifier_detail` column stays, nullable.

---

## E: eval-branch work (part 2)

### Task E4: iteration loop and success runs **[eval-branch, local only]**

- [ ] **Measure.** Take the Task 7.5 full run. Read the gates, the per-pack `scope_gate`,
  sub-task coverage and `write_finish_length`.
- [ ] **One tuning round.** Change only prompt wording, `TARGET_MATCH_MIN`,
  `ACTION_MAX_ROUNDS` or `ACTION_WRITER_REASONING`. Add no new mechanism. Make the change as
  a small PR on `develop` or as a TEST variable.
  - If p90 > 280 s in any run, lower `ACTION_MAX_ROUNDS` first.
- [ ] **Two consecutive full v2 runs.** Both are blind-labelled in full and reconciled. Both
  must pass all four gates (spec §1.4).
- [ ] **On failure:** stop. Report the gap with the per-pack traces, and the next lever in
  order: spec §12.2, then §12.4, then §12.1.
- [ ] **On success:** hand the owner the numbered prod steps (spec §14 steps 6–9):
  - the OpenRouter key split (spec §8.2, steps 1–5);
  - the release PR number;
  - the prod migration;
  - the prod variables.

---

## Self-review notes

**Spec coverage.**

| Spec section | Tasks |
|---|---|
| §3.2.1 | 3.1 |
| §3.2.2 | 3.2, 3.3, 3.4 |
| §3.2.3 | 4.1 |
| §3.2.4 rules 1–4 | 4.2–4.4 |
| §3.3 gate | E3 |
| §4 | 5.2, 5.3 |
| §5 | 6.1–6.3 |
| §6 | 2.1–2.4 |
| §7 | 2.4, 7.1–7.4 |
| §8 | 1.1–1.3, 3.5 |
| §9 | 5.1, 5.3, 7.3, 7.4 |
| §10 | E1, E2 |
| §11 | 3.3, 4.5, 3.5 |
| §1.4 | E4 |

§12 items are out of scope by design.

**Type consistency.**
- `Resolution` (3.2) is consumed by 3.3, 3.4 and 4.2.
- `gate(...) -> (draft, counts)` gains `index_map` in 5.2. Task 4.5's caller unpacks three
  values once 5.2 lands; 5.2 updates the call.
- `WriterRouting` (1.2) is used only by `judge_call`.
- `verifier_hide_unsupported()` (1.2) is read in 7.4.
- `_day` (2.3) is reused in 2.4.


