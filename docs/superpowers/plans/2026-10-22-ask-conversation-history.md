# Ask Conversation History Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Screen Ask keeps each caller's last 5 conversations server-side. Reopening one shows the stored turns without a model call, and a turn whose record sources are gone or no longer visible is withheld.

**Architecture:** The AskVoiceWorker appends each successful palette turn to `ask_threads/{thread_prefix}/{thread_id}.json` in the data bucket before it writes `done`. org-api serves list/get/delete under `/api/org/ask/threads` and re-checks every stored turn's server-side source manifest against the caller's current scope and tombstones. The UI palette shows "Recent conversations" apart from keyword "Recent searches" and seeds AskChat from a stored thread.

**Tech Stack:**
- pipe: Python 3.11 Lambdas, boto3 S3, psycopg3 (org-api), SAM.
- ui: vanilla JS + React (no build), `node --test`.

**Spec:** `docs/superpowers/specs/2026-10-22-ask-conversation-history-design.md` (rev 3). Read it first; section refs below (§n, Dn) point there.

**Repos / branches:**
- pipe: clone `C:\Users\camil\fsw\pipe`, branch `feat/ask-threads` off `origin/develop`, worktree under `C:\Users\camil\wt`.
- ui: `C:\Users\camil\fsw\ui`, branch `feat/ask-threads` off `origin/dev`.

Python is `C:\Users\camil\fspy\Scripts\python.exe`. Unit tests: `python -m pytest tests/unit -q -p no:cacheprovider`. Integration tests: `fswork/run-tests.sh` (local pgserver; only one pytest at a time).

## Global Constraints

- Limits:
  - max **5** threads per caller;
  - **20** turns per thread;
  - **256 KB** per object (over it: store `result.answer` only, plus `result.truncated: true`);
  - expiry **30 days** after the last turn;
  - lifecycle rule `ask-threads-expiry` at **31 days**;
  - `title` = first question cut to **80** chars.
- `thread_id` matches `^[0-9a-f]{32}$`, minted with `secrets.token_hex(16)`.
- `thread_prefix(sub)` is 16 hex, mirroring `owner_prefix`. `sub_hash` (12 hex) is for logs only.
- Store a turn only when the request carried `thread_id` or `new_thread: true`, the flag is on, the turn is not cancelled, `result` has no `error`, and `result.answer` is non-empty.
- `_sources` never reaches any client: strip it before `done`, and strip it on the sync `/api/ask` path.
- No log line may contain question, answer, source key or document id text. Allowed fields: `sub_hash`, `thread_id`, counts, byte size, exception type name.
- Identity comes from the authorizer `sub` only. A client `thread_id` only addresses the caller's own prefix.
- Flags default off:
  - backend: env `ASK_THREADS` = `true`/other, from template param `AskThreads`;
  - deploy vars: `TEST_ASK_THREADS` / `PROD_ASK_THREADS`;
  - UI: Amplify `FS_ASK_THREADS` → `window.FS_ENV.askThreads` → `FS.api.askThreads`.
- Withheld copy, exact: `A record behind this answer is no longer available to you.`
- Rollover copy, exact: `Started a new conversation`.
- Never touch the search line's files beyond the reads named here: `ask_tools.py`, `rag_tools_dispatch.py`, `lexical_terms.py`, `query_slots`, `repositories/chunks.py`. Keep the `round1_calls` / `_scope_args` shape.

## Review Focus

1. **Two tabs on one thread.** Each tab appends from its own stale read; one turn can be lost (accepted), but the object must never be corrupted or deleted. Test: concurrent appends leave valid JSON with ≥ 1 new turn. Owner: Task 1.
2. **A tab still holding an evicted or expired `thread_id`.** The follow-up must land in a fresh thread whose id comes back in `result.thread`. It must not recreate the old id with one turn. Test in Task 4; UI switch in Task 8.
3. **A re-ingested recording.** The nightly job deletes and re-inserts its chunks, so chunk ids change, but the stored answer must still show. Integration test in Task 5.
4. **One session deleted inside a day report.** A `reports/…/daily_report.json` source must withhold the answer. Integration test in Task 5.
5. **An S3 AccessDenied on the org-api list.** It must surface as 5xx, never as "no conversations". Test in Task 6.

---

### Task 1: Thread store module

**Files:**
- Create: `src/ask_threads.py`
- Test: `tests/unit/test_ask_threads_store.py`

**Interfaces:**
- Consumes:
  - `ask_voice_turn._read_json(s3, bucket, key)`;
  - `ask_voice_turn._is_missing(exc)`;
  - the hashing `owner_prefix` uses (`ask_voice_turn.py:92-102`).
- Produces:
  - `PREFIX = "ask_threads/"`; `MAX_THREADS = 5`; `MAX_TURNS = 20`; `MAX_BYTES = 262144`; `EXPIRY_S = 30*86400`; `THREAD_RE`
  - `enabled() -> bool` (env `ASK_THREADS == "true"`)
  - `thread_prefix(sub) -> str` (`"ask_threads/<16hex>/"`); `thread_key(sub, thread_id) -> str`
  - `new_thread_id() -> str`
  - `append_turn(s3, bucket, sub, thread_id: str | None, turn: dict, scope: dict, now: float) -> dict`, returning `{"thread_id": str, "rolled_over": bool, "created": bool}`
    - `thread_id=None` → mint.
    - Mint a new id when the key is missing (when an id was supplied) or the thread already has 20 turns; `rolled_over` is then true.
    - Applies the 256 KB cap.
  - `evict(s3, bucket, sub) -> int`: deletes the oldest by `LastModified` beyond 5; returns the count deleted.
  - `list_threads(s3, bucket, sub, now) -> list[dict]`:
    - returns `{thread_id, title, updated_at, turns}`, newest first;
    - deletes objects older than 30 d (at most 50 per call);
    - raises on `ClientError` other than missing.
  - `get_thread(s3, bucket, sub, thread_id) -> dict | None`
  - `delete_thread(s3, bucket, sub, thread_id) -> None` (idempotent)

- [ ] **Step 1: Write failing tests**

  Use the fake S3 pattern from `tests/unit/test_ask_voice_turn.py` (dict-backed put/get/delete/list with `LastModified`).

  Tests:
  - `test_new_thread_minted_and_stored`: `append_turn(..., None, ...)` → `created=True`; the object has `v==1`, one turn, `scope`.
  - `test_append_to_existing`: a second append with the same id → 2 turns, `created=False`, `rolled_over=False`.
  - `test_missing_supplied_id_mints_new`: an unknown id → the returned id differs, `rolled_over=True`, and the supplied key is not created.
  - `test_twenty_turn_rollover`: a thread with 20 turns → a new id, `rolled_over=True`, and the old thread still has 20.
  - `test_size_cap_truncates`: a turn with a 300 KB answer → the stored `result` is `{"answer": <capped text>, "truncated": True}` and the object is ≤ 262144 bytes.
  - `test_evict_keeps_five_newest`: 7 objects → `evict` returns 2, and the 5 newest by `LastModified` remain.
  - `test_list_expires_old_and_sorts`: one object 31 d old → not listed and deleted; the rest are newest first; the title is cut to 80.
  - `test_list_raises_on_access_denied`: the fake raises `ClientError` `AccessDenied` → `pytest.raises(ClientError)`.
  - `test_prefix_is_16_hex_and_not_sub_hash`: `thread_prefix(sub)` == `"ask_threads/" + owner_prefix(sub) segment` (16 hex), and differs from `sub_hash(sub)`.
  - `test_concurrent_appends_stay_valid`: two appends from the same stale read → the object parses and has ≥ 2 turns.

- [ ] **Step 2: Run, expect FAIL** (`ModuleNotFoundError: ask_threads`)

  Run: `python -m pytest tests/unit/test_ask_threads_store.py -q -p no:cacheprovider`

- [ ] **Step 3: Implement `src/ask_threads.py`** with the signatures above.

  Reuse the `ask_voice_turn` helpers; do not copy them. Approach:
  - append = read → mutate → put;
  - no index object;
  - no logging of text.

- [ ] **Step 4: Run, expect PASS**

- [ ] **Step 5: Commit** `src/ask_threads.py tests/unit/test_ask_threads_store.py` — "Ask threads: per-caller thread store (spec 2026-10-22 §3)"

### Task 2: Source manifest from both answer paths

**Files:**
- Modify: `src/lambda_ask_agent.py`:
  - `_rag_answer` (from 1117): every success return that used `chunks`;
  - the sync handler return near 2409.
- Modify: `src/ask_orchestrator.py`: the client result built near 1441-1447, using `shown` (1361) and `out["documents"]`.
- Test: `tests/unit/test_ask_sources_manifest.py`

**Interfaces:**
- Produces:
  - `result["_sources"] = {"record_keys": [str], "documents": [{"document_id", "site_id"}], "report_days": [{"folder", "report_date"}]}`
    - `record_keys`: distinct, non-null `source_s3_key` of every chunk retrieved into the answer (not only the cited ones).
    - `report_days`: parsed from keys matching `reports/{date}/{folder}/daily_report.json`.
  - `ask_threads.pop_sources(result: dict) -> dict | None`: removes `_sources` and returns it. Add it to Task 1's module.
  - `ask_threads.sources_from_chunks(chunks: list[dict], documents: list[dict] | None) -> dict`: the one builder both paths call.

- [ ] **Step 1: Write failing tests**
  - `test_rag_answer_success_carries_sources`: patch the retrieval like the existing `_rag_answer` tests do. Two chunks, one cited (the uncited one included), one with `source_s3_key=None` (skipped), one `reports/2026-10-20/ben_lin/daily_report.json` → `report_days == [{"folder": "ben_lin", "report_date": "2026-10-20"}]`.
  - `test_orchestrator_result_carries_shown_and_documents`: `shown` items plus documents with `document_id`/`site_id` → both in `_sources`.
  - `test_web_turn_record_cards_in_sources`: a web turn with record chunks → those keys are present.
  - `test_sync_api_ask_strips_sources`: the sync handler's response body has no `_sources` key.
  - `test_pop_sources_removes_key`.

- [ ] **Step 2: Run, expect FAIL**

- [ ] **Step 3: Implement**

  Call `sources_from_chunks` at each success return. Strip it in the sync handler with `pop_sources` before serialising.

- [ ] **Step 4: Run the new tests, plus `tests/unit -k "ask_agent or orchestrator or ask_screen"`**

  Expect PASS with no regressions.

- [ ] **Step 5: Commit** — "Ask: both answer paths hand the worker a source manifest; never sent to clients"

### Task 3: Request plumbing for `thread_id` / `new_thread`

**Files:**
- Modify: `src/lambda_fieldsight_api.py`, `_build_ask_payload` (128-209) and the `/api/ask/start` handler.
- Modify: `src/ask_voice_turn.py:516`, `_SCREEN_DROP`.
- Test: extend `tests/unit/test_lambda_fieldsight_api_ask_screen_turn.py` and `tests/unit/test_ask_screen_turn.py`.

**Interfaces:**
- Produces: the screen turn's `state["request"]` carries `thread_id` (valid) and/or `new_thread: True`; `_SCREEN_DROP` includes both.

- [ ] **Step 1: Write failing tests**
  - `test_start_passes_valid_thread_id`
  - `test_start_rejects_bad_thread_id_400` (e.g. `"../x"`, uppercase hex, 31 chars)
  - `test_start_passes_new_thread_true_only_when_bool_true`
  - `test_screen_rag_body_excludes_thread_fields`: `_work_screen`'s `rag_body` has neither key.
  - `test_voice_start_ignores_thread_fields`

- [ ] **Step 2: Run, expect FAIL**

- [ ] **Step 3: Implement** the whitelist additions and the 400 on a bad id. Validate with `ask_threads.THREAD_RE`.

- [ ] **Step 4: Run, expect PASS**

- [ ] **Step 5: Commit** — "Ask start: carry thread_id/new_thread to the turn, never to the model"

### Task 4: Worker appends before `done`

**Files:**
- Modify: `src/ask_voice_turn.py`, `_work_screen` (591-645).
- Modify: `src/template.yaml`:
  - parameter `AskThreads` (pattern of `AskAgentic`, `template.yaml:342`), default `''`;
  - env `ASK_THREADS` on AskVoiceWorker;
  - worker S3 grant on `ask_threads/*` (Get/Put/Delete) plus `ListBucket` with `s3:prefix: ['ask_threads/*']`, next to the `ask_turns/*` grant (2647-2655).
- Modify: `.github/workflows/deploy.yml`: `ASK_THREADS_PARAM` guarded like `ASK_AGENTIC_PARAM` (178-184, 274) for TEST, plus the PROD equivalent.
- Test: `tests/unit/test_ask_screen_threads.py`

**Interfaces:**
- Consumes: Task 1 (`append_turn`, `evict`, `pop_sources`, `enabled`), Task 2 (`_sources`), Task 3 (request fields).
- Produces: `done.result.thread = {"thread_id", "rolled_over"}` when stored. Absent when not stored or when the append failed.

- [ ] **Step 1: Write failing tests**

  Use a fake S3, a stub `laa._rag_answer` and the orchestrator.
  - `test_success_with_new_thread_stores_and_returns_id`
    - stored turn = `{turn_id, question, at, request: {offer_web, web_intent, history_len}, result (no _sources), sources}`;
    - `done.result.thread.thread_id` matches.
  - `test_append_happens_before_done`: record the call order on the fake → the thread put precedes the `done` put.
  - `test_error_result_not_stored`: `{"answer": "", "error": "x"}` → no object; `done` is still written.
  - `test_empty_answer_not_stored`
  - `test_no_thread_fields_not_stored`
  - `test_flag_off_not_stored_and_sources_stripped`
  - `test_cancelled_before_append_not_stored`
  - `test_append_failure_logs_type_only_and_done_written`: caplog has no question/answer text, and `done` has no `thread`.
  - `test_evicted_id_returns_new_id`: the supplied id is missing → a different `thread_id`, `rolled_over=True`.
  - `test_eviction_runs_after_done`
  - `test_done_never_contains_sources`: flag on and off.

- [ ] **Step 2: Run, expect FAIL**

- [ ] **Step 3: Implement** in `_work_screen`, after the second `is_cancelled` check and before the `done` `put_state`:
  - `pop_sources` always;
  - if storing, call `append_turn` inside try/except and log `type(e).__name__`;
  - set `result["thread"]`;
  - after `done`, call `evict` when `created`.

- [ ] **Step 4: Run, expect PASS**

  Also run `tests/unit -k "ask or template or deploy"` and the cfn-lint step the CI uses.

- [ ] **Step 5: Commit** — "Ask worker: store successful palette turns in the caller's thread before done (D4/D8)"

### Task 5: Re-check rules (D5)

**Files:**
- Create: `src/ask_thread_check.py`
- Test: `tests/unit/test_ask_thread_check.py`, `tests/integration/test_ask_thread_check_sql.py`

**Interfaces:**
- Consumes:
  - `scope.visible_scope(conn, caller)` → `site_ids`, `author_ids`;
  - `redactions.is_source_deleted(conn, key)`;
  - `redactions.deleted_source_prefixes(conn, folder, date)` (check the exact signatures in `src/repositories/redactions.py:212-292`).
- Produces: `withheld_turns(conn, caller, turns: list[dict]) -> set[str]`, returning the `turn_id`s to withhold.
  - Rules 1–4 of §6.
  - Batched: one live-chunk query for all `record_keys`, one documents query.
  - Any exception → every turn with any record/document source is withheld (fail closed).

- [ ] **Step 1: Write failing tests**

  Unit, with fakes for the repository calls:
  - `test_tombstoned_key_withheld`
  - `test_key_without_visible_live_chunk_withheld`
  - `test_author_restricted_caller_needs_own_author`
  - `test_document_gone_or_out_of_scope_withheld`
  - `test_report_day_with_any_tombstone_withheld`
  - `test_null_key_skipped`
  - `test_web_only_turn_never_withheld`
  - `test_db_error_fails_closed`

  Integration (local Postgres, real `report_chunks` / redactions rows):
  - `test_reingested_key_still_visible`: delete and re-insert chunks for a key → not withheld.
  - `test_deleted_session_inside_day_report_withheld`
  - `test_site_removed_from_scope_withheld`

- [ ] **Step 2: Run, expect FAIL**

- [ ] **Step 3: Implement**

  The live-chunk predicate must be the same one search uses (`search_sql.py` live/archived filter, lines 59-60 for authors). Import it; do not inline a copy.

- [ ] **Step 4: Run unit, then `fswork/run-tests.sh tests/integration/test_ask_thread_check_sql.py`**

  Expect PASS.

- [ ] **Step 5: Commit** — "Ask threads: re-check stored answers against current access and tombstones (§6)"

### Task 6: org-api routes

**Files:**
- Modify: `src/lambda_org_api.py`: routes after the caller guard (≈573), beside the other `/me`-style routes; S3 client lazily, like the module's other S3 users.
- Modify: `src/template.yaml`, OrgApiFunction (2868-2900):
  - env `ASK_THREADS`, `ASK_TURN_BUCKET` (or reuse the data-bucket env it already has);
  - S3 Get/Delete on `ask_threads/*` plus `ListBucket` with `s3:prefix: ['ask_threads/*']`.
- Test: `tests/unit/test_org_api_ask_threads.py`

**Interfaces:**
- Consumes: Task 1 (`list_threads`, `get_thread`, `delete_thread`, `enabled`), Task 5 (`withheld_turns`).
- Produces (wire contract for the UI):
  - `GET /api/org/ask/threads` → `{"threads": [...]}`
  - `GET /api/org/ask/threads/{id}` → `{thread_id, scope, turns: [{turn_id, question, at, request, result}` or `{turn_id, question, at, request, withheld: true}]}`
  - `DELETE /api/org/ask/threads/{id}` → 204

- [ ] **Step 1: Write failing tests**
  - `test_flag_off_404_all_three`
  - `test_list_returns_callers_threads_only`: two subs → disjoint lists.
  - `test_get_strips_sources_and_marks_withheld`
  - `test_get_unknown_404`, `test_get_bad_id_404`
  - `test_delete_idempotent_204`
  - `test_archived_caller_403`: proves the inherited guard covers the new routes.
  - `test_access_denied_is_5xx_not_empty`

- [ ] **Step 2: Run, expect FAIL**

- [ ] **Step 3: Implement** the three routes.

- [ ] **Step 4: Run, expect PASS**; then the full `tests/unit` suite.

- [ ] **Step 5: Commit** — "org-api: list/get/delete Ask threads with the access re-check (§5)"

- [ ] **Step 6: Lifecycle rule**

  Add `ask-threads-expiry` (prefix `ask_threads/`, expire 31 d) to `scripts/wire-bucket-lifecycle.sh` and its rule-ID exclusion list. Its test, if the script has one, extends; otherwise a shell test asserting the JSON contains the rule.

  Commit with Task 6, or as its own commit.

- [ ] **Step 7: Open the pipe PR to `develop`**

  Check the base is `develop`. Merge when CI is green. Watch the TEST deploy run to success.

### Task 7: UI API layer

**Files:**
- Modify: `fieldsight-ui/scripts/api/ask.js`:
  - `ask()` body whitelist (198-257): add `thread_id` (string) and `new_thread` (true only);
  - `_askAsyncTurn` stays (it already returns `res.result`, which carries `thread`).
- Create: `fieldsight-ui/scripts/api/ask-threads.js`, exposing `FS.api.askThreads.list()`, `.get(id)`, `.remove(id)` through the org fetch (`_fetch.js:355-364`, gated on `orgBaseUrl`). Load it in the same HTML files that load `ask.js`.
- Modify: `amplify.yml` env printf (append `askThreads: %s` with `"${FS_ASK_THREADS:-false}"`, same position in format and args); `scripts/api/index.js`: `FS.api.askThreadsOn` from `FS_ENV.askThreads`.
- Test: `fieldsight-ui/tests/ask-threads-api.test.js`

- [ ] **Step 1: Write failing tests**
  - `the build emits askThreads from FS_ASK_THREADS, default false, same position in format and args` (mirror `tests/ask-web-offer.test.js:40`)
  - `ask() forwards thread_id and new_thread:true, drops new_thread:false and non-string ids`
  - `askThreads.list/get/remove hit /api/org/ask/threads[/id] with GET/GET/DELETE`
  - `askThreads.list resolves [] when the flag is off without a request`

- [ ] **Step 2: Run** `node --test tests/ask-threads-api.test.js`, expect FAIL

- [ ] **Step 3: Implement**

- [ ] **Step 4: Run, expect PASS**

- [ ] **Step 5: Commit** `scripts/api/ask.js scripts/api/ask-threads.js scripts/api/index.js amplify.yml <html files> tests/ask-threads-api.test.js`

  Do not stage `tests/fixtures/editor-to-prompt.body.json`.

### Task 8: AskChat can be seeded and keeps a thread

**Files:**
- Modify: `fieldsight-ui/scripts/composites/ask-chat.js`:
  - `send()` result handling (1341-1388);
  - mount (1033-1035);
  - props.
- Test: `fieldsight-ui/tests/ask-chat-threads.test.js`

**Interfaces:**
- Produces:
  - `messageFromResult(question, result, facts)` → the message object `send()` builds today. `facts` = `{offerWeb, sentHistory, scopeResponse}`; `zh` is derived from the question as today. Exported on `window.FieldSight.AskChat._messageFromResult` for tests.
  - Props:
    - `threads: bool` → send `new_thread`/`thread_id`; off = today's body exactly;
    - `initialThread: {thread_id, scope, turns}` → seed messages, no `/ask/start`.

- [ ] **Step 1: Write failing tests**
  - `live send and seeded turn render the same message for the same result`
  - `seeded thread makes no /ask/start call`
  - `first question with threads sends new_thread:true; follow-up sends thread_id and history from seeded turns`
  - `result.thread with a different id switches thread and shows "Started a new conversation"`
  - `withheld turn renders "A record behind this answer is no longer available to you." and an Ask again button`
  - `Ask again re-runs the question as a new turn with thread_id`
  - `stored answers show "Answered {date}"`
  - `without threads prop the request body is byte-identical to today` (snapshot against the current builder)
  - `stored offer turn's Check the web re-asks with the stored history`

- [ ] **Step 2: Run, expect FAIL**

- [ ] **Step 3: Implement**

  Refactor `send()` to use `messageFromResult`; add the props and the thread state.

- [ ] **Step 4: Run, expect PASS**; then the full `node --test` suite (baseline 2329 pass).

- [ ] **Step 5: Commit**

### Task 9: Palette shows Recent conversations

**Files:**
- Modify: `fieldsight-ui/scripts/composites/search-palette.js`:
  - empty state (595-605);
  - `doAsk` (387-389, stop `saveRecent` for Ask);
  - the inline AskChat (534-543: pass `threads: FS.api.askThreadsOn`, `initialThread`).
- Test: `fieldsight-ui/tests/search-palette-threads.test.js`

- [ ] **Step 1: Write failing tests**
  - `flag on: Recent conversations above Recent searches, up to 5, from askThreads.list`
  - `asking no longer adds to fs.search.recent; keyword search still does`
  - `clicking a conversation fetches it and opens AskChat with initialThread`
  - `Delete removes the row and calls askThreads.remove`
  - `flag off: no request, no section, today's behaviour`
  - `list failure hides the section and keeps Recent searches`
  - `docked Timeline AskChat sends no thread fields` (render it without `threads`)

- [ ] **Step 2: Run, expect FAIL**

- [ ] **Step 3: Implement**

- [ ] **Step 4: Run the full suite, expect PASS**

- [ ] **Step 5: Commit**, then open the ui PR to `dev`

  Check the base is `dev`. Merge when green. Confirm the Amplify `dev` build SUCCEED.

### Task 10: TEST rollout and live check

- [ ] **Step 1:** Set the flags:
  - `gh variable set TEST_ASK_THREADS -R benzn-tech/fieldsight-pipeline -b true`
  - Amplify `dev` env `FS_ASK_THREADS=true`
  - redeploy both.
- [ ] **Step 2:** Run `scripts/wire-bucket-lifecycle.sh` for the TEST bucket. Read the config back and confirm `ask-threads-expiry` plus the pre-existing rules.
- [ ] **Step 3:** IAM, with `aws iam simulate-principal-policy` for:
  - the worker role: `s3:PutObject` on `ask_threads/x/y.json`, `s3:ListBucket` with prefix;
  - the org-api role: `s3:GetObject`, `s3:DeleteObject`, `s3:ListBucket`.
  
  All `allowed`.
- [ ] **Step 4:** Live on dev with a test account. State the model calls first: about 3 turns ≈ 3 orchestrator runs.
  1. Ask, follow up.
  2. Open the palette in a second browser → the conversation is listed.
  3. Open it → no `/ask/start` in the gateway log.
  4. Confirm the worker log shows a thread write with no text.
- [ ] **Step 5:** Withheld path: point a TEST thread's manifest at a tombstoned key. Use a recording deleted on TEST through the app's delete, never a direct DB write. Reopen → withheld.
- [ ] **Step 6:** Update the memory `ask-orchestration-v2.md` with the shipped state.

  Owner steps for prod, numbered with PR numbers:
  1. Ask v2 on prod.
  2. `PROD_ASK_THREADS=true`.
  3. The lifecycle rule on the prod bucket.
  4. Amplify `main` `FS_ASK_THREADS=true`.
  5. ui `dev` → `main`.
