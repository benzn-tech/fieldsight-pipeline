# Nightly re-match agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A site-local nightly hub handler that (J2) links unlinked final items to the transcript turn they came from by a model turn pick checked in code, (J3) recomputes `claim_type`, (J1) re-emits stale per-session requests after late uploads, and (J4) emits `item.recurring_open` facts to the hub.

**Architecture:** An hourly tick emits `nightly.due` site events into the P0 decision hub. The hub runs handler `nightly_rematch` in-VPC; it builds J2 request artifacts on S3. A non-VPC worker calls the model and invokes a small in-VPC writer, which writes `decision_records` and `event_links`. J1 and J4 reuse other lines' builders and edges and add no matcher of their own.

**Tech Stack:** Python 3.11 Lambdas, psycopg3, Aurora Postgres (local PG16 harness for tests), SAM, GitHub Actions, the `anthropic` SDK for the measurement and the worker.

**Spec:** `docs/superpowers/specs/2026-11-10-nightly-rematch-agent-design.md` (rev 2, pipeline#1272). Read it first; § numbers below refer to it. Parent: `docs/superpowers/specs/2026-11-05-site-decision-loop-design.md`.

## Phases and blockers

| Tasks | Phase | Can start when |
|---|---|---|
| 1-2 | N0 (offline measurement) | now. **Task 2 Step 4 (the paid run) waits for the owner to approve the call count** |
| 3-7 | N1/N2 | P0 plan `2026-11-08-site-decision-loop-p0.md` Tasks 1-4 are merged (hub tables, `decision_hub`, `decision_rules`, `lambda_decision_scan`). Task 5 J2 path ships `on` only if Task 2's bars pass |
| 8 | N3, J1 | pipeline#1273's `build_xday_request`, the status line's `reconcile_request.build`, and Track C's `find_items.first_seen_recorded_at` are on develop |
| 9 | N3, J4 | `CROSS_DAY_CONTINUITY=on` somewhere (real `continues` edges exist) |
| 10 | TEST verification | after 3-7 (8-9 when they land) |

A blocked task is skipped, not stubbed. Record the skip in the ledger.

## Global Constraints

- Branch per phase from `origin/develop`; PR to `develop`; check that the base is not `main`/`release` before merging; merge on green CI.
- Migration: the next free number at merge time (0099 is free on 2026-11-10, but P0 takes the first free number, so expect 0100). Recheck before committing.
- Flag `NIGHTLY_REMATCH` ∈ `off` | `shadow` | `on`, default `off` in the template parameter (AllowedValues), `TEST_NIGHTLY_REMATCH` and `PROD_NIGHTLY_REMATCH`. Prod flags are the owner's.
- Silent writes are only `event_links` rows with `link_type='supported_by'`, `method='nightly_turn_pick_v1'`, plus `claim_type` / `claim_basis` through `claim_type_store.recompute_session`. No status, `continues`, `resolves`, `supersedes` or item-text writes, ever.
- Nightly never updates or deletes a row it did not write.
- `score` on nightly links is `NULL`.
- Item text may leave the VPC; `origin` and other sessions' transcript text never do (Track C §5.2).
- Prompts get item text and turn text **fenced as quoted data**; free text never acts as an instruction (parent §4.1).
- Claude models only through the Claude API (`ANTHROPIC_API_KEY`), never OpenRouter. Invoke `Skill(claude-api)` before writing the model call.
- Logs and `decision_records.output` carry ids, counts and enums only, never quote or item text.
- In-VPC code makes no internet calls (BUG-36). S3 triggers are wired in `scripts/wire-s3-events.sh` (BUG-33). New CFN resources need IAM on `github-actions-fieldsight-deploy` (check with `simulate-principal-policy`).
- Every read of items or links goes through `deleted_predicates`, never an inlined copy.
- Site-local time goes through `nz_time.py`; v1 is NZ-only behind `site_local_now(site)`.
- Brakes (§6): J1 10 re-emits per site per night; J2 20 sessions per site per night and 60 items per call; 1 model call per session; worker `MaximumRetryAttempts: 1`; circuit breaker after 5 same-type errors per job per site per night.
- Tests: `bash /c/Users/camil/fswork/run-tests.sh <args>` from the worktree root, one pytest at a time; every SQL path gets a test under `tests/integration/` (a fake conn executes no SQL).
- No customer text in committed fixtures or results. Measurement data stays in the gitignored eval dirs.
- No model identifier in commit messages, PR titles or code comments (the registry holds it).
- Never `git stash`. Never `git add -A`. Add files by name.

## Review Focus

1. **An item whose topic `time_range` is missing or unparsable.** The item gets no turns and no prompt slot, and is counted as `no_window`; it is never matched against the whole session → Task 1 `test_item_without_time_range_is_skipped_not_whole_session`.
2. **The model answers a turn number from another topic** (`T4` exists in topic A, but the item is in topic B). The `shape` guard drops it because numbering is per topic → Task 1 `test_turn_from_other_topic_is_shape_drop`.
3. **A re-extraction between request and write.** The row is superseded and the writer skips it as `gone`; no link lands on a dead row → Task 5 `test_writer_skips_superseded_row`.
4. **Two ticks in one hour, or an EventBridge retry.** Exactly one `nightly.due` per site per night (dedupe key) → Task 6 `test_second_tick_same_night_is_duplicate`.
5. **A site whose company is suspended or exited.** No `nightly.due` dispatch (hub auth `blocked`), so no request and no model cost → Task 6 `test_inactive_company_gets_no_requests`.

---

### Task 1: Pure turn-pick module (prompt, parse, guards)

**Files:**
- Create: `src/nightly_turn_pick.py`
- Test: `tests/unit/test_nightly_turn_pick.py`

**Interfaces:**
- Produces:
  - `RULE_VERSION = "nightly_turn_pick_v1"`;
  - `METHOD = "nightly_turn_pick_v1"`;
  - `MAX_ITEMS = 60`.
  - `build_request(items: list[dict], turns: list[dict]) -> dict`.
    - `items` entries: `{stable_id, row_id, kind, text, topic_index, time_range}`, where `time_range` is the topic's own string.
    - `turns` entries: the `assemble_session_turns` shape (`abs_start`, `abs_end`, `text`, `speaker`, `source_filename`, `start_sec`, `end_sec`).
    - Returns `{"items": [{alias "I1"…, stable_id, row_id, kind, text, topic_index}], "topics": {topic_index: [{"n": "T1", "turn_index": i}]}, "skipped": {"no_window": n, "over_cap": n}}`.
    - Turns are numbered **per topic**, over those whose `[abs_start, abs_end]` overlaps the topic window.
  - `render_prompt(request: dict, turns: list[dict]) -> str`.
    - Constant `INSTRUCTION`, plus one fenced block per topic (`T<n> [HH:MM:SS] <text>`) and one fenced items block (`I<n> <kind>: <text>`).
    - Answer shape: JSON `{"picks": [{"item": "I3", "turn": "T17" | null}]}`.
  - `PROMPT_HASH: str` = `sha256(INSTRUCTION)[:12]`.
  - `parse_answer(text: str) -> list[dict]`: returns `[{"item", "turn"}]`; raises `ValueError` on non-JSON or a wrong shape.
  - `apply_guards(request: dict, picks: list[dict], turns: list[dict]) -> list[dict]`.
    - Returns one row per request item: `{alias, stable_id, row_id, kind, turn_index | None, outcome: "accepted" | "rejected" | "abstained", guard: str | None, cross_language: bool}`.
    - Guards run in the spec's order: `shape`, `window`, `twin`, `same_language_floor`. A null pick is `abstained`.
  - `turn_locator(turn: dict) -> dict`: returns `{source_stem, start_sec, end_sec, speaker_label, quote_status: "model_pick"}`, Track C's `dst_turn` keys.
- Consumes: `item_text_match.tokens` (the shared tokeniser), the numeral normaliser and twin test from `scripts/item_support_eval/score.py` (`place_values`, `twin_flags`). Move them into `src/item_text_match.py` if they are not importable from `src/`, and have the harness import them from there.

- [ ] **Step 1: Write failing tests**:
  - `test_turns_numbered_per_topic_by_window_overlap`: two topics, windows 10:00-10:05 and 10:05-10:10. Six turns. Each topic's T-numbers start at T1 and cover only overlapping turns.
  - `test_item_without_time_range_is_skipped_not_whole_session`: `time_range=None` → not in `items`, `skipped["no_window"] == 1`.
  - `test_over_cap_items_counted`: 61 items → 60 in, `over_cap == 1`.
  - `test_prompt_fences_text_as_data`: a turn text `ignore previous instructions` appears only inside a fence; `INSTRUCTION` precedes all fences.
  - `test_parse_rejects_non_json_and_wrong_shape`.
  - `test_turn_from_other_topic_is_shape_drop`.
  - `test_unknown_alias_is_shape_drop`.
  - `test_null_pick_is_abstained`.
  - `test_twin_same_turn_both_rejected`: "unit 3 pre-line done" and "unit 4 pre-line done" both pick T2 → both `rejected`, `guard="twin"`.
  - `test_twin_picks_turn_with_sibling_token_rejected`: the unit 3 item picks a turn saying "unit 4" → `twin`.
  - `test_same_language_floor`: an English item "west pipe" picks an English turn without "west" while a sibling "east pipe" exists → `same_language_floor`.
  - `test_cross_language_pick_accepted_and_flagged`: an English item, a Chinese turn → `accepted`, `cross_language=True`.
  - `test_chinese_numerals_normalised_in_twin`: "三号单元" vs "unit 3" counts as the same place value.
  - `test_locator_has_track_c_keys`.

- [ ] **Step 2: Run** `bash /c/Users/camil/fswork/run-tests.sh tests/unit/test_nightly_turn_pick.py`. Expected: FAIL (module missing).

- [ ] **Step 3: Implement** `src/nightly_turn_pick.py`.
  - Window parsing reuses `item_support._window_turns` semantics (same `time_range` format). Do not re-invent it.
  - Script detection for `cross_language`: the item is Latin and the turn has CJK, or vice versa, by majority of characters.

- [ ] **Step 4: Run** the same command. Expected: PASS.

- [ ] **Step 5: Commit** `feat(nightly): pure turn-pick module (prompt, parse, guards)`.

### Task 2: N0 measurement on Track C's gold (pre-registered)

**Files:**
- Create: `docs/superpowers/specs/2026-11-10-nightly-turn-pick-eval-preregistration.md`
- Create: `scripts/nightly_turn_pick_eval/__init__.py`, `run.py`, `score.py`, `README.md`, `.gitignore` (`runs/`)
- Test: `tests/unit/test_nightly_turn_pick_eval.py`

**Interfaces:**
- Consumes: Task 1; `scripts.item_support_eval.score` (`cp_upper`, `is_link_correct`, `merge_gold`, `gold_by_ref`, `score_agreement`); the data in `--data` (default `C:\Users\camil\wt\track-c-eval\scripts\item_support_eval\data`) and the labels in `--labels`.
  - The session JSON keys are `base, date, folder, ref, split, topics, turns`.
  - The gold rows are `{"item_ref": "<ref>:<topic_idx>:<kind_list>:<i>", "label": "turn" | "several" | "none", "turns": [session-wide turn indexes]}`.
- Produces:
  - `run.py --split fit|heldout --reps 3 --out runs/<name> [--dry-run]`.
    - `--dry-run` prints `sessions=N calls=N*reps` and exits without a call.
    - A real run writes `runs/<name>/rep<k>.jsonl` (per item: item_ref, turn_index | null, outcome, guard, cross_language) and `runs/<name>/meta.json` (model, PROMPT_HASH, started_at, calls, tokens).
  - `score.py --run runs/<name> → runs/<name>/summary.json` with:
    - bar1 (precision CP upper);
    - bar2 (cross-language stratum, or "insufficient" below 30 accepted);
    - bar3 (twins);
    - bar4 (rep agreement ≥ 0.90);
    - bar5 (agent–owner agreement ≥ 0.85, or "pending" without owner labels);
    - coverage per kind and per language;
    - the lexical-unlinked split.

- [ ] **Step 1: Write the pre-registration** before any result exists.
  - Content: the set, split and gold from Track C; the model id `claude-sonnet-5-5` via the Claude API, temperature 0; 3 reps; the five bars and the "reported" list exactly as spec §7; correctness by `is_link_correct` (Track C's adjacency rule); the fit half used only for prompt wording, at most 2 wording revisions, each logged; the held-out half scored once.
  - Frozen at the first held-out result row; later changes go in dated addenda.
  - Commit it alone: `docs(nightly): N0 pre-registration`.

- [ ] **Step 2: Write failing tests** (fixtures are synthetic, no customer text):
  - `test_dry_run_counts_calls_without_calling` (the client is patched to raise if called);
  - `test_gold_ref_maps_to_task1_alias`;
  - `test_bar1_uses_cp_upper`;
  - `test_bar2_insufficient_below_30`;
  - `test_bar4_rep_agreement_counts_null_as_answer`;
  - `test_bar5_pending_without_owner_labels`;
  - `test_heldout_refuses_second_scoring` (a `runs/<name>/summary.json` already present for heldout → exit non-zero unless `--addendum <date>`).

- [ ] **Step 3: Implement** `run.py` and `score.py`.
  - Writes are refused outside `scripts/nightly_turn_pick_eval/runs/`.
  - The model call uses the `anthropic` SDK with `ANTHROPIC_API_KEY`, and records `usage` into `meta.json`.
  - Run the Step 2 tests. Expected: PASS. Commit `feat(nightly): N0 measurement harness`.

- [ ] **Step 4: STOP.** Run `run.py --split fit --reps 3 --dry-run` and `--split heldout --reps 3 --dry-run`, then report both call counts to the owner (about 30×3 + 29×3 = 177, plus up to 2 fit rewording passes of 30 each). **Do not make a paid call until the owner approves.**

- [ ] **Step 5 (after approval):**
  - Fit runs → fix the wording.
  - Held-out run → score it.
  - Export about 40 held-out items for owner adjudication with `label`-style blind JSONL in the gitignored dir.
  - Commit only `scripts/nightly_turn_pick_eval/results/summary.json` (counts, rates, verdicts) with `docs(nightly): N0 result`.
  - Bar 5 stays "pending" until the owner labels.

### Task 3: Migration and repository functions

**Files:**
- Create: `src/migrations/NNNN_nightly_rematch.sql` (NNNN = next free)
- Create: `src/repositories/nightly.py`
- Modify: `src/repositories/event_links.py` (`insert_supported_by` gains `decision_record_id=None`)
- Modify: `src/lambda_voiceprint_writer.py` (write a recompute-failure marker)
- Test: `tests/integration/test_nightly_schema.py`, `tests/integration/test_nightly_repo.py`

**Interfaces:**
- Migration:
  - `nightly_reemits(id uuid pk default gen_random_uuid(), site_id uuid not null → sites on delete cascade, line text not null check in ('xday','reconcile'), n_extraction_key text not null, l_extraction_key text, artifact_key text not null, night date not null, created_at timestamptz default now())`, with `UNIQUE (line, n_extraction_key, l_extraction_key)` (`NULLS NOT DISTINCT` is not used; the dropped-request rows with `l_extraction_key` NULL get their own partial unique index `(line, n_extraction_key, night) WHERE l_extraction_key IS NULL`).
  - `claim_recompute_failures(company_id uuid not null → companies on delete cascade, session_base text not null, reason text not null check in ('timed_out','failed'), failed_at timestamptz default now(), resolved_at timestamptz, PRIMARY KEY (company_id, session_base))`.
  - Four partial unique indexes on `event_links`, one per `src_<kind>_id` column, `WHERE method LIKE 'nightly_%' AND <col> IS NOT NULL`.
  - All `IF NOT EXISTS`; idempotent.
- `insert_supported_by(..., decision_record_id=None)` writes the column. Existing callers are unchanged.
- `repositories/nightly.py`:
  - `sessions_needing_support(conn, *, company_id, site_id, since_utc, limit=20) -> list[dict]`: finals written since `since_utc` on the site, newest first, each `{extraction_key, session_base, user_folder, date, items: [{stable_id, row_id, kind, text, topic_index, time_range}]}`. Only live items with no live `supported_by` link of any method. Sessions with zero such items are omitted.
  - `nightly_support_count_today(conn, company_id, today_nz) -> int`: `decision_records` kind `nightly_support` rows created today. Used by the budget check.
  - `record_reemit(conn, *, site_id, line, n_key, l_key, artifact_key, night) -> bool`: returns False on conflict.
  - `failed_recomputes(conn, *, company_id, limit=50) -> list[dict]`: unresolved rows.
  - `resolve_recompute(conn, company_id, session_base)`.
- Voiceprint writer: when `recompute_session` returns `timed_out` or `failed > 0`, upsert `claim_recompute_failures`. Logging is unchanged.

- [ ] **Step 1: Write failing tests**:
  - `test_migration_idempotent`;
  - `test_one_nightly_link_per_row` (a second nightly insert on the same row conflicts);
  - `test_nightly_index_ignores_lexical_links` (a lexical plus a nightly link on one row are both allowed);
  - `test_insert_supported_by_writes_decision_record_id`;
  - `test_sessions_needing_support_excludes_linked_and_superseded`;
  - `test_sessions_needing_support_scoped_by_current_company` (a moved site → old company sees nothing);
  - `test_redacted_topic_items_excluded`;
  - `test_record_reemit_unique_per_pair`;
  - `test_voiceprint_writer_marks_timed_out_recompute`.

- [ ] **Step 2: Run** each file. Expected: FAIL.

- [ ] **Step 3: Implement.**

- [ ] **Step 4: Run** each file. Expected: PASS.

- [ ] **Step 5: Commit** `feat(nightly): migration NNNN and repository functions`.

### Task 4: Hub rules can run a job, not only build a card

**Files:**
- Modify: `src/decision_rules.py`, `src/decision_hub.py`, `src/lambda_decision_scan.py` (`process`)
- Test: `tests/unit/test_decision_hub.py`, `tests/integration/test_decision_scan.py`

**Interfaces:**
- `Rule` gains `run: Callable[[Connection, Event, RunContext], str] | None = None`, which returns an outcome in `{"done", "nothing"}` or raises. `build_card` becomes optional; a rule sets exactly one of `run` / `build_card` (a `__post_init__` check).
- `RunContext` (frozen dataclass in `decision_hub`): `run_id, company_id, site_id, now_utc, flag ("shadow" | "on"), s3_client, bucket`.
- `process`: for a rule with `run`, it calls it inside the run (`event_runs.start` → `run` → `finish(outcome)`). A raise follows the existing failure path (back-off, fault at 3, stop at 5). No card is written.
- `event_runs.add_usage(conn, run_id, *, tokens_in, tokens_out, model, cost_usd)`: increments the counts, `model_calls += 1`, and appends to `models`.

- [ ] **Step 1: Write failing tests**:
  - `test_rule_requires_exactly_one_of_run_or_build_card`;
  - `test_run_rule_outcome_nothing_closes_without_card`;
  - `test_run_rule_raise_uses_backoff_and_fault_at_3` (integration);
  - `test_add_usage_accumulates`.

- [ ] **Step 2: Run.** Expected: FAIL. **Step 3: Implement.** **Step 4: Run.** Expected: PASS, and all existing P0 hub tests still pass.

- [ ] **Step 5: Commit** `feat(decision-loop): rules may run a job`.

### Task 5: J2 worker and writer Lambdas

**Files:**
- Create: `src/lambda_nightly_turn_pick.py` (non-VPC worker)
- Create: `src/lambda_nightly_support_writer.py` (in-VPC writer)
- Create: `src/session_turns.py` (`assemble_session_turns` lifted out of `lambda_extract_session.py` with no psycopg import; the extractor imports it from there). If the status line has already lifted it, import theirs and skip this file.
- Create: `src/model_registry.py` with `ENTRIES = {"nightly_turn_pick": {"model": "claude-sonnet-5-5", "max_tokens": 4096, "temperature": 0, "prompt_hash": nightly_turn_pick.PROMPT_HASH}}` and `get(task) -> dict`. Use the model Task 2 measured.
- Test: `tests/unit/test_lambda_nightly_turn_pick.py`, `tests/integration/test_nightly_support_writer.py`

**Interfaces:**
- Request artifact `nightly_requests/{site_id}/{night}/{sha256(extraction_key)[:16]}.json`: `{site_id, night, run_id, extraction_key, session_base, user_folder, date, transcript_keys: [...], items: [...]}`. Items use Task 3's `sessions_needing_support` shape.
- Worker `lambda_handler(s3_event)`:
  1. Read the artifact and rebuild turns from `transcript_keys`.
  2. Run Task 1 `build_request` → `render_prompt` → one model call via the `anthropic` SDK with `model_registry.get("nightly_turn_pick")` → `parse_answer` → `apply_guards`.
  3. Invoke the writer RequestResponse with `{site_id, run_id, extraction_key, question_set: PROMPT_HASH, input_key, rows, usage}`.
  4. Raise on `FunctionError` or a parse error (fail closed).
- Writer `lambda_handler(event) -> {"written": n, "records": n, "skipped": {"gone": n, "linked": n, "moved_site": n}}`. Per row, in one transaction:
  1. Re-read the row through `deleted_predicates`.
  2. `decision_records.insert(kind="nightly_support", subject_type=kind, subject_stable_id=stable_id, object_ref=locator string or None, provider="anthropic", model=…, question_set=…, input_key=…, output={"turn": "T<n>" | None, "guard": guard}, auto_outcome=outcome)`.
  3. When `accepted` and the row is still live and unlinked: `insert_supported_by(..., method=METHOD, score=None, rule_version=RULE_VERSION, decision_record_id=…)` with `ON CONFLICT DO NOTHING` semantics. A conflict is counted as `linked`.
  4. `event_runs.add_usage(run_id, …)`.
  5. Finally `claim_type_store.recompute_session(conn, company_id=row company, site_id=site_id, session_base=…, statement_timeout="15s")` when `written > 0`. That is J3 for tonight's links.

- [ ] **Step 1: Write failing tests**:
  - `test_worker_fails_closed_on_parse_error`;
  - `test_worker_raises_on_writer_function_error`;
  - `test_worker_sends_no_origin_or_other_session_text`;
  - `test_writer_writes_record_per_row_and_link_per_accept`;
  - `test_writer_skips_superseded_row`;
  - `test_writer_never_touches_lexical_link` (a row already holding a `lexical_v1` link → skipped `linked`, lexical row unchanged);
  - `test_writer_site_mismatch_skipped`;
  - `test_writer_recomputes_claim_type_when_written`;
  - `test_extractor_still_imports_assemble_session_turns` (an import check on `lambda_extract_session`).

- [ ] **Step 2: Run.** Expected: FAIL. **Step 3: Implement.** **Step 4: Run.** Expected: PASS.

- [ ] **Step 5: Commit** `feat(nightly): J2 worker and writer`.

### Task 6: Tick, `nightly.due` rule, handler (J2 request build + J3 catch-up), infra and flag

**Files:**
- Create: `src/nightly_rematch.py` (the handler; in-VPC)
- Modify: `src/decision_rules.py` (rule `nightly.due`), `src/lambda_decision_scan.py` (scan `nightly_tick`), `src/template.yaml`, `.github/workflows/deploy.yml`, `.github/workflows/deploy-prod.yml`, `scripts/wire-s3-events.sh`
- Test: `tests/unit/test_nightly_rematch.py`, `tests/integration/test_nightly_tick.py`

**Interfaces:**
- `nightly_rematch.site_local_now(site: dict, now_utc: datetime) -> datetime`: NZ via `nz_time` in v1.
- `nightly_rematch.due_sites(conn, now_utc, local_hour=1) -> list[dict]`: active sites whose local hour equals `local_hour`.
- `emit_nightly_due(conn, now_utc) -> dict`: one event per due site.
  - Fields: `source="nightly_scan"`, `type="nightly.due"`, `subject_ref=site_id`, `occurred_at=now`, payload `{night}`.
  - The rule's bucket is `payload.night` (the previous local date).
- Rule `nightly.due`: `run=nightly_rematch.run`, `late_after_days=1`.
- `nightly_rematch.run(conn, event, ctx) -> str`:
  1. **J3 catch-up:** for `failed_recomputes(company)` on this site, call `recompute_session`; on success, `resolve_recompute`.
  2. **J2:**
     - Get `sessions_needing_support(since_utc = now − 7 days, limit=20)`.
     - Budget: when `nightly_support_count_today + items > NIGHTLY_MAX_ITEMS_PER_COMPANY_DAY` (env, default 2000), stop and count `budget_paused`.
     - Write one request artifact per session. In `shadow`, write the artifacts but the worker trigger is not wired.
  - **Circuit breaker (§6):** before J2, count last night's request artifacts for this site that have no `nightly_support` record (`decision_records.input_key` = the artifact key). If 5 or more, the worker is failing: write no J2 requests tonight, count `j2_breaker_open`, and log one alert line `nightly breaker site=<id> missing=<n>`. Test `test_breaker_opens_after_5_unanswered_requests`.
  3. Return `"nothing"` when there is no work, else `"done"`.
  4. Log one counts line: `nightly site=<id> j2_sessions=n j2_items=n j3_recomputed=n budget_paused=n skipped_no_window=n`.
- Template:
  - `NightlyRematch` parameter (AllowedValues `off,shadow,on`, default `off`) → env `NIGHTLY_REMATCH` on DecisionScanFunction.
  - An hourly Schedule `{"scan": "nightly_tick"}`, `State` enabled only when `NIGHTLY_REMATCH != off` and `ShouldEnableSchedules`.
  - `NightlyTurnPickFunction` (non-VPC, timeout 300, `MaximumRetryAttempts: 1`, GetObject/ListBucket on `transcripts/` and `nightly_requests/*`, invoke on the writer, the `ANTHROPIC_API_KEY` secret as the existing Claude callers get it).
  - `NightlySupportWriterFunction` (VPC, DB secret).
  - DecisionScanFunction PutObject on `nightly_requests/*`.
- Workflows: `TEST_NIGHTLY_REMATCH` / `PROD_NIGHTLY_REMATCH` → the `NightlyRematch` parameter override (memory: SAM keeps previous parameter values, so pass it every deploy).
- `wire-s3-events.sh`: `nightly_requests/` → NightlyTurnPickFunction. The script line is added but only run by the operator when the flag is `on`.

- [ ] **Step 1: Write failing tests**:
  - `test_due_sites_at_local_one_am_only` (13:00 UTC in NZDT = 02:00 → not due; 12:00 UTC = 01:00 → due);
  - `test_due_sites_nzst_offset` (winter: 13:00 UTC = 01:00);
  - `test_second_tick_same_night_is_duplicate`;
  - `test_inactive_company_gets_no_requests`;
  - `test_run_returns_nothing_without_new_finals` (no artifact, no model);
  - `test_run_respects_20_sessions_and_budget`;
  - `test_shadow_writes_artifacts_and_no_db_rows_besides_hub`;
  - `test_failed_recompute_resolved`;
  - a template check `test_nightly_flag_wired_three_places` (template param, both workflows, env).

- [ ] **Step 2: Run.** Expected: FAIL. **Step 3: Implement.** **Step 4: Run.** Expected: PASS.

- [ ] **Step 5: Commit** `feat(nightly): tick, nightly.due rule, handler, infra (flag off)`.

### Task 7: Revert script, invariants, mutation tests

**Files:**
- Create: `scripts/nightly_revert.py`, `scripts/nightly_invariants.sql`
- Test: `tests/integration/test_nightly_revert.py`, `tests/integration/test_nightly_invariants.py`

**Interfaces:**
- `nightly_revert.py --site <uuid> --since <date> [--rule-version V] [--apply]`.
  - Without `--apply` it prints the counts it would delete. With it, it deletes nightly links in range (only `method LIKE 'nightly_%'`) and recomputes `claim_type` for affected sessions.
  - It refuses without `--site`.
- Invariants, each a query that must return zero rows:
  - a nightly link without a `nightly_support` record;
  - two nightly links for one source row;
  - a nightly link whose `link_type` is not `supported_by`;
  - two `nightly_reemits` rows for one (line, N, L);
  - a nightly link whose source row was not live at its `created_at` (best effort: superseded before creation).

- [ ] **Step 1: Write failing tests**:
  - `test_revert_dry_run_deletes_nothing`;
  - `test_revert_deletes_only_nightly`;
  - `test_revert_recomputes_claim_type`;
  - `test_invariants_clean_on_fixture`;
  - mutation tests, each planting a fault in a copy of the writer or guards via monkeypatch and asserting that a test or invariant catches it:
    - drop the site filter;
    - skip the `window` guard;
    - skip the twin guard;
    - let the writer overwrite a non-nightly link;
    - re-emit one (N, L) twice.

- [ ] **Step 2: Run.** Expected: FAIL. **Step 3: Implement.** **Step 4: Run.** Expected: PASS.

- [ ] **Step 5: Commit** `feat(nightly): revert script, invariants, mutation tests`.

### Task 8: J1 catch-up for late arrivals (blocked; see Phases)

**Files:**
- Modify: `src/nightly_rematch.py`
- Test: `tests/integration/test_nightly_catchup.py`

**Interfaces:**
- Consumes:
  - `build_xday_request(conn, *, site, company_id, extraction, extraction_key, date, nonce=None, trigger="final")` (#1273);
  - `reconcile_request.build(conn, extraction, extraction_key, site, owner_company_id)` and `reconcile_request.emit(s3, bucket, body)` (status line);
  - `find_items(...)[i]["first_seen_recorded_at"]` (Track C).
- `nightly_rematch.stale_pairs(conn, *, company_id, site_id, now_utc) -> list[dict]`, each `{line, n_extraction_key, l_extraction_key}`. The spec §3 J1 rules apply:
  - L final written in the last 24 h;
  - L recorded before N by `first_seen_recorded_at`, with null meaning unordered and skipped;
  - L's final written after N's;
  - L within N's window (reconcile at N's rung; cross-day 28 days).
  - Newest N first, at most 10.
- In `run`: for each pair, `record_reemit` first. If it returns False, skip. Otherwise re-read N's final from S3 and call the line's builder with `nonce=night` (cross-day) and `trigger="nightly"`, then put the artifact. In `shadow`, only count.

- [ ] **Step 1: Write failing tests**:
  - `test_late_monday_upload_makes_tuesday_stale`;
  - `test_same_day_ordering_uses_recorded_at` (R0);
  - `test_null_recorded_at_is_unordered_and_skipped`;
  - `test_pair_reemitted_once_only`;
  - `test_cap_10_newest_first`;
  - `test_shadow_counts_without_put`.

- [ ] **Step 2-4:** run → FAIL, implement, run → PASS.

- [ ] **Step 5: Commit** `feat(nightly): J1 catch-up for late arrivals`.

### Task 9: J4 `item.recurring_open` (blocked; see Phases)

**Files:**
- Modify: `src/nightly_rematch.py`, `src/decision_rules.py`
- Test: `tests/integration/test_nightly_recurring_open.py`

**Interfaces:**
- `nightly_rematch.recurring_open(conn, *, site_id, today_nz) -> list[dict]`: action-item chains via `event_links` `continues`, raised on ≥3 distinct NZ recording dates within 14 days, live status `open`, and no `action_status_suggestions` row in state pending for any chain member. Each entry is `{subject_ref: earliest stable_id, days: [dates], text}`.
- Emitted as `source="nightly_scan"`, `type="item.recurring_open"`, bucket = ISO week.
- Rule `item.recurring_open`: `build_card` → a `fact` card, with the question "Raised on <days>; still open — still needed?", options keep / close / not sure, and recipients as `todo.overdue`.

- [ ] **Step 1: Write failing tests**:
  - `test_three_days_in_14_emits_once_per_week`;
  - `test_two_days_no_emit`;
  - `test_pending_suggestion_suppresses`;
  - `test_no_continues_edges_no_emit`.

- [ ] **Step 2-4:** run → FAIL, implement, run → PASS.

- [ ] **Step 5: Commit** `feat(nightly): J4 recurring-open fact`.

### Task 10: TEST verification and handover

- [ ] **Step 1:** Merge Tasks 3-7 to develop (green CI, base checked). Set `TEST_NIGHTLY_REMATCH=shadow`. Confirm the deploy run applied the migration and created both functions. Note the run number.
- [ ] **Step 2:** The next 01:00 NZ. Check that:
  - one `nightly.due` per active TEST site;
  - request artifacts under `nightly_requests/` match the counts log;
  - no `event_links` rows with `method LIKE 'nightly_%'`;
  - zero invariant rows.
- [ ] **Step 3:** Only if Task 2's bars passed:
  - Set `TEST_NIGHTLY_REMATCH=on` and run `wire-s3-events.sh` for the prefix.
  - The next night, check the writer counts, invariants, cost on `event_runs`, and `claim_type` distribution change on TEST.
- [ ] **Step 4:** After two weeks, write the counts (stale pairs per night, J2 coverage, orphaned nightly links) into the spec as a dated note. Open a release PR for the owner with the PR number and run number. Prod flags stay the owner's.
- [ ] **Step 5:** Run the full suite once at the end and quote CI's count on the same sha beside the local one.
