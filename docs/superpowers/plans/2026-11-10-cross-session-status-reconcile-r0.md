# Cross-Session Status Reconcile (R0) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After a session's final extraction, propose a status for open action items from *other* sessions recorded by the same person on the same day (rung R0). Certain matches become suggestions. Verified but ambiguous ones become pick cards. Nothing is auto-applied.

**Architecture:**
1. `item_writer` (in-VPC) calls Track C's `find_items` inside its connection block and, after commit, writes a `reconcile_requests/` artifact.
2. A new branch in the non-VPC `MatcherFunction` rebuilds the session's turns, makes one LLM call, and runs the code guards in a pure module.
3. It invokes `SuggestionWriterFunction`, which re-reads each target by `stable_id` and writes to `action_status_suggestions`, or to a new `status_pick_cards` table.

**Tech Stack:** Python 3.11, psycopg 3, AWS SAM, S3 events (wired by script), pytest. The local Postgres harness is `bash /c/Users/camil/fswork/run-tests.sh` (one pytest at a time).

**Spec:** `docs/superpowers/specs/2026-11-09-cross-session-status-reconcile-design.md` (PR #1270, owner-approved 2026-11-10; §12 records the owner's decisions).

## Global Constraints

- Statuses: `done`, `closed`, `blocked`, `in_progress` (`status_suggestions.STATUSES`). Candidate statuses passed to `find_items`: `{"action_item": ["open", "in_progress", "blocked"]}`.
- R0 filters: `recorded_by=user_folder`, `since=until=date` (the session's NZ date), `exclude_source_keys=[extraction_key]`, `limit=RECONCILE_CANDIDATE_CAP` (default `40`), no `query`.
- `company_id` passed to `find_items` = `owner_company_id` (the site's company), never the recorder's home company.
- Artifact candidate fields are exactly: `kind, stable_id, text, status, responsible, deadline, first_seen_date, recorded_by, score, matched_terms, place`. No `origin`, no `company_id`, and no other session's text.
- The writer takes `company_id` only from the re-read live row.
- The system never chooses between plausible candidates (owner, §12.2). Ambiguous plus verified means a pick card holding at most 4 candidates.
- Never log quote text or item text. Log ids and counts only.
- LLM: `llm_utils.call_llm(prompt, force_json=True, enable_thinking=True, caller="status_reconcile")`. Claude models only through the Claude API.
- Flags: `RECONCILE_STATUS` (default `'false'`) on ItemWriter. TEST is turned on by repo var; there is no prod flag in this plan.
- No `git add -A`. Stage named files only.

## Review Focus

1. **A generic shared word anchors a quote to the wrong item**, e.g. "the pipe's all done" with candidates "fix west pipe leak" and "order pipe fittings". Expect a pick card, never a suggestion. Test in Task 1 (`test_generic_token_goes_to_pick_card`).
2. **The recorder works on another company's project** (project-owned tenancy). `find_items` must receive the site's company, or it silently returns `[]`. Test in Task 4 (`test_reconcile_uses_owner_company_id`).
3. **A status-only writer payload** (no programme suggestions, impacts or verdicts) must still open a connection and write. Test in Task 3 (`test_status_only_payload_opens_connection`).
4. **The target moved between request and write** (superseded, deleted recording, closed by hand, moved site). Expect it skipped and counted, never written. Tests in Task 3 (`test_reread_skips_*`).
5. **Number words vs digits, the pronoun "one", self-corrections and 中文 twins**: "unit four" vs "unit 4"; "that one's done" (must stay subject-less); "west… no wait, the east one, done"; "二楼防水" vs "三楼防水". The anchor and twin guards must pick correctly or send a pick card. Tests in Task 1: `test_numeral_normalisation`, `test_pronoun_one_is_not_a_number`, `test_quote_naming_both_twins_goes_to_pick_card`, `test_cjk_twins`.

---

### Task 1: Pure reconcile logic (`status_reconcile.py`)

**Files:**
- Create: `src/status_reconcile.py`
- Test: `tests/unit/test_status_reconcile.py`

**Interfaces:**
- Consumes:
  - `item_text_match.tokens(text) -> list`
  - `action_pack_targets.containment(phrase, text) -> float`
  - `action_pack_targets.AMBIGUITY_MARGIN`
  - `status_suggestions.STATUSES`
- Produces:
  - `normalise_numerals(text: str) -> str`
  - `build_prompt(candidates: list[dict], transcript_lines: str) -> str`: aliases `C1..Cn` in candidate order.
  - `parse_hits(raw: str, n_candidates: int) -> list[dict] | None`: each hit is `{"alias": "C3", "status", "at", "quote"}`; `None` on unparseable JSON.
  - `judge(hits, candidates, check) -> Outcome`. `check(quote, at) -> dict` has the shape of `_check_status_quote`: `status`, `segment_key`, `offset_sec`, `quote`.
  - `Outcome = NamedTuple(suggestions: list[dict], picks: list[dict], counts: dict)`:
    - suggestion: `{stable_id, suggested_status, quote, segment_key, offset_sec}`;
    - pick: `{suggested_status, quote, segment_key, offset_sec, candidates: [stable_id, ...] (≤4, best first)}`;
    - counts: `{"offered": n, "kept": n, "picks": n, "dropped_by": {reason: n}}`.

- [ ] **Step 1: Write the failing tests** in `tests/unit/test_status_reconcile.py`. Use a helper `cand(sid, text, status="open")` and a fake `check` that returns `{"status": "verified", "segment_key": "audio_segments/u/d/b.wav", "offset_sec": 12.0, "quote": q}`, with the anchor keyed off the quote.
  - `test_numeral_normalisation`: `normalise_numerals("unit four, level one, 三楼") == "unit 4, level 1, 3楼"`. Also: `normalise_numerals("一起去统一") == "一起去统一"` (unchanged).
  - `test_pronoun_one_is_not_a_number`: candidates "Pour level 1 slab" / "Order skip bin"; quote "Yeah that one's done now mate, all good". No suggestion and no pick; `dropped_by["no_anchor"] == 1`.
  - `test_quote_naming_both_twins_goes_to_pick_card`: west/east pipe candidates; quote "The west block copper pipe, no wait, the east one, that's done". Zero suggestions, one pick over both.
  - `test_one_candidate_card_is_dropped`: the model picks "Order skip bin for site", but the quote is about the west copper pipe (candidates: west copper pipe, skip bin). No pick card with fewer than 2 entries; drop as `anchor`.
  - `test_conflicting_statuses_same_anchor_dropped`: two hits on the same anchor with different statuses are both dropped as `conflict`.
  - `test_prompt_aliases_in_order`: `"C1"` and `"C2"` appear with each candidate's text. No stable_id appears in the prompt.
  - `test_parse_rejects_unknown_alias`: a hit with `"C9"` when n=2 is dropped as `shape`. Invalid JSON returns `None`.
  - `test_clear_match_becomes_suggestion`: candidates "Order copper pipe for the west block" and "Order PVC pipe for the east block"; quote "The copper pipe for the west block is ordered, that's done", status `done`. One suggestion on the west item; `counts["kept"] == 1`.
  - `test_twin_wrong_side_is_dropped`: the model says C2 (east), but the quote names "west". Not a suggestion on east; a pick card or `twin` drop, never east.
  - `test_generic_token_goes_to_pick_card`: "Fix west pipe leak under slab" / "Order pipe fittings for level 2"; quote "The pipe's all done mate, finished it". Zero suggestions, one pick with both stable_ids.
  - `test_no_item_word_is_silent_drop`: quote "Yeah that's all done now, good job" gives no suggestion, no pick, and `dropped_by["no_anchor"] == 1`.
  - `test_unverified_quote_dropped`: `check` returns `{"status": "weak"}`, so the hit is dropped as `weak`.
  - `test_one_anchor_two_candidates_becomes_pick`: two hits with the same status share `(segment_key, offset_sec)`. One pick, zero suggestions. For this test the fake `check` is keyed on `at`, so that different quotes can share an anchor.
  - `test_latest_status_wins_per_candidate`: two hits on the same alias, `at` 10:00:00 `in_progress` and 10:05:00 `done`. One suggestion, `done`.
  - `test_same_status_dropped`: the candidate's status is `blocked` and the hit says `blocked`, so it is dropped as `same_status`.
  - `test_cjk_twins`: "二楼防水膜订购" / "三楼防水膜订购"; quote "二楼的防水膜已经订好了". One suggestion on 二楼.
  - `test_pick_caps_at_four`: six plausible candidates give a pick with exactly 4 stable_ids.

- [ ] **Step 2: Run them and confirm they fail.**
  Run `C:\Users\camil\fspy\Scripts\python.exe -m pytest tests/unit/test_status_reconcile.py -q`.
  Expected: errors with `ModuleNotFoundError: status_reconcile`.

- [ ] **Step 3: Implement `src/status_reconcile.py`.**
  - Pure: no boto3, no psycopg, no env reads, no logging of text.
  - Guard order, as spec §6: `shape` → `evidence` (keep `verified` / `verified_fuzzy`; any other status drops under its own name) → `anchor` → `twin` → `one_to_one` → `same_status`.
  - Token sets: `set(item_text_match.tokens(normalise_numerals(x)))`.
  - The anchor algorithm, which the tests do not fully determine:

```python
Q = toks(quote)
overlaps = {c: len(Q & toks(c.text)) for c in candidates}
if max(overlaps.values()) == 0:                        -> drop "no_anchor"
top = [c for c in candidates if overlaps[c] == max]
unique = Q & toks(pick) - union(toks(c) for c in candidates if c is not pick)
if len(top) > 1 or pick not in top or not unique:      -> pick card over the candidates
                                                          with overlap > 0, best first, ≤ 4
```

  - Any card set with fewer than 2 entries is dropped as `anchor`. It is never a 1-candidate card.
  - Twin, applied after the anchor passes, against **every other candidate with overlap > 0**, not only containment-siblings. For each such `c`:
    - `d_pick = toks(pick) - toks(c)` and `d_c = toks(c) - toks(pick)`;
    - require `Q & d_pick` and `not (Q & d_c)`;
    - otherwise, a pick card over `[pick] + offending c's`, at most 4.
    - (`containment` / `AMBIGUITY_MARGIN` are only used to order the card, best first.) This is what catches "west… no wait, the east one".
  - `normalise_numerals` rewrites number words **only after a unit noun**: `unit`, `level`, `block`, `floor`, `stage`, `lot`, `house`, `room`, `apartment`. Chinese numerals are rewritten **only before a measure word**: `楼`, `层`, `号`, `栋`, `单元`, `期`. A bare "one" or "一起" is untouched.
  - `one_to_one` groups surviving hits by `(segment_key, offset_sec)`:
    - same status and more than one stable_id → one pick card with that status;
    - different statuses → drop all of them as `conflict`.

    Then, per stable_id, keep the hit with the latest `at`.

- [ ] **Step 4: Run the tests and confirm they pass.** Same command. Expected: all pass.

- [ ] **Step 5: Commit.**

```bash
git add src/status_reconcile.py tests/unit/test_status_reconcile.py
git commit -m "feat(reconcile): pure guards, prompt and parser for cross-session status"
```

---

### Task 2: Pick-card store (migration plus repository)

**Files:**
- Create: `src/migrations/0099_status_pick_cards.sql`. Use the next free number when executing; 0098 is the current max on develop.
- Create: `src/repositories/status_pick_cards.py`
- Test: `tests/integration/test_status_pick_cards_repo.py`

**Interfaces:**
- Produces: `insert_pick_card(conn, company_id, site_id, suggested_status, quote, segment_key, offset_sec, candidates: list, run) -> dict | None`. It is idempotent on `(segment_key, offset_sec, suggested_status)` and returns None on conflict.

- [ ] **Step 1: Write the failing integration tests.**
  - `test_insert_and_idempotent`: the second insert with the same anchor and status returns None.
  - `test_candidates_bounds`: 1 or 5 candidates raise a check violation, because the `CHECK (jsonb_array_length(candidates) BETWEEN 2 AND 4)` is violated.
  - `test_cascade_on_site_delete`.
- [ ] **Step 2: Run them and confirm they fail.**
  Run `bash /c/Users/camil/fswork/run-tests.sh tests/integration/test_status_pick_cards_repo.py -q`.
  Expected: FAIL, because the relation does not exist.
- [ ] **Step 3: Write the migration.**
  - The table `status_pick_cards` has these columns:
    - `id uuid PK default gen_random_uuid()`;
    - `company_id` and `site_id`, with the same FKs and ON DELETE CASCADE as 0090;
    - `suggested_status` with the 0090 CHECK;
    - `quote text`, `segment_key text NOT NULL`, `offset_sec double precision NOT NULL`;
    - `candidates jsonb NOT NULL` (an array of stable_id strings, 2–4);
    - `run text NOT NULL`;
    - `state text NOT NULL DEFAULT 'pending'`, CHECK in `pending`, `chosen`, `none`;
    - `chosen_stable_id uuid`, `resolved_by uuid REFERENCES users(id)`, `resolved_at timestamptz`, `created_at timestamptz NOT NULL DEFAULT now()`.
  - Constraint: `UNIQUE (segment_key, offset_sec, suggested_status)`.
  - Index on `(site_id) WHERE state='pending'`.
  - Write the repo with `ON CONFLICT DO NOTHING RETURNING *`, mirroring `repositories/status_suggestions.insert_suggestion`. Bind `candidates` with `psycopg.types.json.Jsonb(list_of_str)`, as `repositories/decision_records.insert` passes jsonb. A bare list does not bind.
- [ ] **Step 4: Run the tests and confirm they pass.** Also run `tests/integration/test_migrations_apply.py` on its own.
- [ ] **Step 5: Commit** the migration, the repo and the test, by name.

---

### Task 3: suggestion_writer accepts `status_suggestions` and `pick_cards`

**Files:**
- Modify: `src/lambda_suggestion_writer.py`: the docstring event shape, the empty-batch guard (lines 160–168), and a new loop inside the existing transaction.
- Test: `tests/unit/test_lambda_suggestion_writer.py` (extend) and `tests/integration/test_status_suggestions_writer.py` (extend).

**Interfaces:**
- Consumes:
  - `action_items.get_live_by_stable_id(conn, stable_id)`, which returns a dict with `site_id`, `company_id`, `status`, `stable_id`;
  - `status_suggestions_repo.insert_suggestion(...)`;
  - `status_pick_cards.insert_pick_card(...)` from Task 2.
- Produces: event keys
  - `status_suggestions: [{stable_id, site_id, suggested_status, quote, segment_key, offset_sec, run}]`
  - `pick_cards: [{site_id, suggested_status, quote, segment_key, offset_sec, candidates, run}]`

  The response adds `status_written`, `status_skipped: {gone, moved_site, terminal, same, duplicate}` and `picks_written`. These keys are present only when the corresponding input is non-empty, which keeps it backward compatible.

- [ ] **Step 1: Write the failing tests.**
  - Unit `test_status_only_payload_opens_connection`: with only `status_suggestions`, `get_connection` is entered.
  - Unit `test_old_payload_response_unchanged`: no new keys appear.
  - Integration `test_reread_skips_gone`, `test_reread_skips_moved_site`, `test_reread_skips_terminal` (`done`, `closed`), `test_reread_skips_same`.
  - Integration `test_company_from_row_not_event`: the event carries a bogus `company_id`, and the stored row has the site's company.
  - Integration `test_pick_card_requires_all_candidates_live_on_site`: when a candidate is gone or on another site, it is removed. If fewer than 2 remain, the card is skipped, counted under `gone`.
- [ ] **Step 2: Run them and confirm they fail.** The unit run uses fspy pytest; the integration run uses run-tests.sh, one file at a time.
- [ ] **Step 3: Implement.**
  - Add `status_suggestions` and `pick_cards` to the guard condition.
  - In the same `with get_connection() as conn:` transaction, loop over the entries:
    - re-read each by `stable_id`;
    - skip by the reasons above;
    - insert with `row["company_id"]`, `row["site_id"]`.
  - For a pick card, re-read every candidate, then insert with the company of the first surviving row (all of them must share the site).
  - **Wrap each status or pick insert in its own `with conn.transaction():` savepoint**, as `_record_verdict` does (Ruling R14, lines 118–150). One failed insert is then counted under `failed` and does not abort the batch's programme suggestions. Add the unit test `test_failed_status_insert_does_not_abort_batch`.
- [ ] **Step 4: Run the tests and confirm they pass**, plus the existing `tests/unit/test_lambda_suggestion_writer.py`.
- [ ] **Step 5: Commit.**

---

### Task 4: item_writer emits the reconcile request; template and wiring

**Files:**
- Create: `src/reconcile_request.py`. It mirrors `match_request.py`: `emit(s3, bucket, site_id, date, extraction_key, user_folder, session_base, candidates) -> str | None`, using `lake_put` with `owner_hint={"site_id": ...}`.
- Modify: `src/lambda_item_writer.py`:
  - read the env at top: `RECONCILE_STATUS`, `RECONCILE_CANDIDATE_CAP`;
  - compute candidates inside the connection block, after `owner_company_id` is known (around line 1406);
  - emit after the block, next to `match_request.emit` (around line 1914).
- Modify: `src/template.yaml`:
  - ItemWriter: env `RECONCILE_STATUS: !Ref ReconcileStatus` (a new parameter, default `'false'`), `RECONCILE_CANDIDATE_CAP: '40'`, and `s3:PutObject` on `${IngestBucketName}/reconcile_requests/*`, mirroring lines 5150–5154.
  - MatcherFunction, mirroring ExtractSessionFunction's grants (template around lines 4035, 4051 and 4090–4102):
    - `s3:GetObject` on `reconcile_requests/*`, `transcripts/*`, `voice_ask/*` (for `agent_turn_filter`) and `audio_segments/*` (for `_batch_map.json` via `_rebase_batch_turns`). A denied batch-map read is swallowed and silently shifts the anchors;
    - `s3:ListBucket` on the ingest bucket for prefixes `transcripts/*`, `voice_ask/*` and `reconcile_requests/*`;
    - env `EVIDENCE_WINDOW_SEC`, `STATUS_EVIDENCE_FLOOR_TOKENS`, `EVIDENCE_FUZZY_THRESHOLD`, with the same `!Ref`s or values as ExtractSessionFunction.
- Modify: `scripts/wire-s3-events.sh`: add `{"Id":"fs-status-reconcile", same matcher ARN, prefix "reconcile_requests/", suffix ".json"}` beside `fs-programme-match`.
- Modify: `.github/workflows/deploy.yml` (the TEST deploy; there is no deploy-test.yml): add `"ReconcileStatus=${{ vars.TEST_RECONCILE_STATUS || 'false' }}"` beside `StatusSuggestions` (around lines 440–442). The `|| 'false'` stops SAM from keeping a stale value. `deploy-prod.yml` needs nothing, because the new parameter's Default is `'false'`. Extend `tests/unit/test_template_workflow_parameter_wiring.py` if it enumerates the parameters.
- Test:
  - `tests/unit/test_reconcile_request.py`
  - `tests/unit/test_lambda_item_writer.py` (extend)
  - `tests/unit/test_template_reconcile_iam.py`, following `test_template_item_writer_group_iam.py`

**Interfaces:**
- Consumes: `item_history.find_items(conn, *, company_id, site_id, kinds, statuses, since, until, recorded_by, exclude_source_keys, limit)`.
- Produces: the artifact `reconcile_requests/{site_id}/{date}/{sha256(extraction_key)[:16]}.json` with body `{site_id, date, user_folder, session_base, extraction_key, extracted_at, rung: "R0", candidates: [...]}`.
  - `extracted_at` comes from the extraction item_writer already holds, so the worker never reads `extractions/`.
  - Candidates use the Global Constraints field list, with `first_seen_date = source["date"]`.
  - **Coerce to JSON types** in the candidate builder: `stable_id` → `str`, `deadline` / `first_seen_date` → `isoformat()` or None. `find_items` returns `uuid.UUID` and `date`.

- [ ] **Step 1: Write the failing tests.**
  - `test_emit_key_deterministic_and_fields_whitelisted`: there is no `origin` and no `company_id` in the body.
  - `test_candidates_serialise_uuid_and_date`: the fake `find_items` rows carry a real `uuid.UUID` and a real `datetime.date`, and the emitted body round-trips through `json.loads`.
  - `test_emit_none_on_empty`.
  - item_writer tests, with `find_items` monkeypatched and recording its kwargs:
    - `test_reconcile_uses_owner_company_id`: the site belongs to company B and the recorder's home is A, so it is called with B;
    - `test_reconcile_r0_filters`: `recorded_by`, `since == until == date`, `exclude_source_keys == [extraction_key]`, `statuses`;
    - `test_no_request_when_flag_off`, `test_no_request_on_live_tier`, `test_no_request_when_incomplete`, `test_no_request_for_group_member` (`_group_for_session` returns a row), `test_no_request_when_no_candidates`.
  - `test_template_reconcile_iam`: the ItemWriter Put grant, the matcher Get and List grants, the evidence env names present on the matcher, and the `ReconcileStatus` parameter default `'false'`.
- [ ] **Step 2: Run them and confirm they fail.**
- [ ] **Step 3: Implement.**
  - Log `reconcile site=<id> candidates=<n>` when the flag is on.
  - Run `find_items` **inside its own `with conn.transaction():` savepoint**. On any exception, log it and use `candidates = []`. A failed statement without a savepoint leaves the psycopg transaction aborted, and every later item write would fail ("current transaction is aborted"). Reconcile must never fail the item write. Add the test `test_find_items_error_does_not_abort_item_write`; use the integration harness, since a fake connection cannot show the abort.
- [ ] **Step 4: Run the tests and confirm they pass**, plus all of `tests/unit/test_lambda_item_writer.py` and `cfn-lint` at the pinned version (see the CI workflow).
- [ ] **Step 5: Commit.**

---

### Task 5: Matcher reconcile branch (the worker)

**Files:**
- Create: `src/reconcile_worker.py`, which holds `process_request(req: dict, input_key: str, call=llm_utils.call_llm) -> dict`.
- Modify: `src/lambda_programme_matcher.py`, `lambda_handler` (lines 925–967):
  - when the record key starts with `reconcile_requests/`, route to `reconcile_worker.process_request` and collect `status_suggestions` and `pick_cards`;
  - include those in the writer payload and in the non-empty condition;
  - leave the `match_requests/` path byte-identical.
- Test: `tests/unit/test_reconcile_worker.py` and `tests/unit/test_lambda_programme_matcher.py` (extend).

**Interfaces:**
- Consumes:
  - from `lambda_extract_session`, imported lazily inside `process_request`: `gather_session_segments(bucket, user_folder, date, session_base)`, `assemble_session_turns(bucket, keys) -> (turns, source_filenames, stats)`, `_check_status_quote(quote, at, turns, date, user_folder)`, and `session_brief.render_turns(turns)` for the `[HH:MM:SS] speaker: text` lines;
  - Task 1's `build_prompt`, `parse_hits` and `judge`.
- Produces: `{"status_suggestions": [...], "pick_cards": [...], "counts": {...}}`. Each entry carries the Task 3 event fields, with `site_id` taken from the request and `run = f"reconcile:R0:{req['extracted_at']}"`. `extracted_at` comes from the artifact envelope; there is no `extractions/` read.

**Deviation from spec §4.2, recorded:**
- R0 imports these helpers from `lambda_extract_session` instead of lifting them. That file is TrackB-adjacent and 2.4k lines long; lifting it is a separate refactor.
- The import graph has no psycopg. A test pins that.

- [ ] **Step 1: Write the failing tests.**
  - `test_import_graph_has_no_psycopg`: in a subprocess, `import reconcile_worker, lambda_extract_session`, then assert `"psycopg" not in sys.modules`.
  - `test_process_request_happy_path`: S3 and the turn functions are faked; `call` returns one valid hit, so there is one status suggestion with the request's `site_id` and the expected `run`.
  - `test_llm_error_raises`: `call` returns `(None, "boom")`, so it raises (fail closed).
  - `test_unparseable_raises`.
  - `test_zero_hits_returns_empty`.
  - Matcher: `test_reconcile_record_routes_and_invokes_writer`; `test_match_requests_path_unchanged`, where an existing fixture yields an identical payload; `test_writer_function_error_raises`.
- [ ] **Step 2: Run them and confirm they fail.**
- [ ] **Step 3: Implement.**
  - The LLM call: `call(prompt, max_tokens=4000, force_json=True, enable_thinking=True, caller="status_reconcile")`.
  - Log the counts only.
- [ ] **Step 4: Run the tests and confirm they pass**, plus all of `tests/unit/test_lambda_programme_matcher.py`.
- [ ] **Step 5: Commit.**

---

### Task 6: PR, TEST rollout, scripted acceptance

**Files:**
- Modify: this plan file. Tick the boxes and record the TEST results at the bottom.

- [x] **Step 1:** Open a PR to `develop` with Tasks 1–5. Wait for CI green, then merge (it is my own develop PR). Wait for the TEST deploy.
- [x] **Step 2:** Ask the owner to set the repo var `TEST_RECONCILE_STATUS=true` and re-run the TEST deploy (`deploy.yml`), or do it myself if the var is TEST-only and allowed. Run `scripts/wire-s3-events.sh` for TEST. Verify:
  - `aws s3api get-bucket-notification-configuration` lists `fs-status-reconcile`;
  - the ItemWriter env shows `RECONCILE_STATUS=true`.
- [x] **Step 3:** The owner records scripts A and B from `Dropbox/FieldSight-录音脚本/2026-11-10_跨录音对账脚本.md` on the same site and day. Verify in this order:
  - the item_writer log line `reconcile ... candidates=N` for B, where N covers A's 7 items;
  - the artifact under `reconcile_requests/`;
  - the matcher counts;
  - DB rows.

  Expected for B:
  - suggestions: west copper → done, unit 4 → closed, skip bin → blocked;
  - zero rows on east PVC and unit 3;
  - zero for the plan, hearsay, never-assigned and "all done" lines;
  - pick cards: zero expected. Record any that appear, with the reason.
- [ ] **Step 4:** Script C (day 2) at R0 gives zero suggestions and no artifact (no candidates, because of the date filter).
- [ ] **Step 5:** Record the results table here: per statement, expected vs actual, and every miss with its cause (ASR, a guard by name, the model, duplicate, ordering). Report to the owner. The R1 decision is the owner's.

### TEST results, run 1 (2026-11-11)

Recordings (TEST, `Ben_Lin_test2`, site `d104ebbe…`):
- A: `sid6fcef858…`, 13:36 NZ. GrandTime app.
- B: `sid3aad34b3…`, 17:54 NZ, the same device.
- An earlier attempt at A from the phone web app never uploaded. The device line has it; it is not a reconcile defect.

What ran:
- A's extraction wrote all 7 items. ASR heard "pre-line" as "purlin" for both units, so the twins stay symmetric.
- B's request carried candidates=7: A's 7 items, nothing else.
- The matcher made one model call: 46.4 s, 474 prompt tokens. That is well inside the 150 s per-attempt timeout.
- Counts: offered=1, kept=0, picks=1.
- DB:
  - one `status_pick_cards` row: status done, 4 candidates (west copper, unit 4, as-built, skip bin), quote = the whole 58 s paragraph;
  - zero `action_status_suggestions`.

| B statement | Expected | Actual | Cause |
|---|---|---|---|
| west copper ordered | copper/west → done | in the pick card | transcript granularity (below) |
| unit four cancelled | unit 4 → closed | in the pick card | the same |
| skip bin stuck | skip bin → blocked | in the pick card | the same. ASR also heard "Skip the MRR for now" |
| as-built tomorrow (plan) | none | listed in the pick card | the same |
| paint "I heard" (hearsay) | none | none | |
| gutter finished (never assigned) | none | none | |
| "that's all done" | none | none | |
| east PVC, unit 3 (twins) | none | none | |

Wrong-item: 0. False positives: 0. Recall: 0 of 3.

**Cause.**
- B has one speaker, so the whole 58 s chunk was ONE turn.
- The prompt asks for "the WHOLE transcript line", so the quote was the paragraph, and the anchor guard rightly found four items in it.
- one_to_one also keyed the anchor on (segment_key, offset_sec). That offset is turn-granular, so every sentence of a monologue would have been one "anchor" in conflict with itself.

**Fix (PR after #1275).**
- The model sees `status_reconcile.sentence_lines(turns)`: the same turns, split into sentences, each keeping its turn's clock. The quote check still runs on the original turns.
- The one_to_one anchor key adds the quote's token set.

**Offline check on the real B transcript.** Real `_check_status_quote`, real candidates, synthetic hits:
- copper → done, unit 4 → closed, skip bin → blocked: 3 kept, 0 cards;
- the twin traps (the same quotes aimed at east PVC and unit 3): both dropped by label_conflict.

**Still owed:**
- a replay of B's request on TEST after the deploy, with the real model call;
- Step 4 (C on 11-12).

**Follow-ups, not in this plan:**
- The UI pick card and its accept endpoint (UI line plus org-api). Until they exist, pick cards are visible only in the DB on TEST.
- Lifting the turn helpers out of `lambda_extract_session`.
- Verdict rows in `decision_records`, once the `kind` is agreed with TrackB.
- R1+.
