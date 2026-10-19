# Check Done Gaps (step A) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When the inspector says "<check> done" on the device, an Ask worker hook can invoke one Lambda and get back, in under ~20 s, the checklist items still open on that check plus a `say` line to speak.

**Architecture:** Two new Lambdas mirroring AskAgent -> RagSearch. `CheckGapsFunction` (no VPC: S3, ElevenLabs STT on the Ask key, the LLM) invokes `CheckGapsDbFunction` (in VPC, Aurora only) for the caller, his live session, the candidate templates and the session's live check windows. The logic lives in `src/check_done.py`, pure where possible, with every outside call injected so unit tests never touch AWS, the network or `lambda_extract_session`.

**Tech Stack:** Python 3.11 (Lambda), pytest, SAM template, psycopg 3.

**Spec:** `docs/superpowers/specs/2026-10-20-check-done-gaps-design.md`

## Global Constraints

- Do not edit `ask_voice_turn.py`, `lambda_org_api.py`, `lambda_transcribe.py`, `lambda_extract_session.py`, `batch_ledger.py`, `batch_seal.py`, `batch_stitch.py`, `lambda_ask_agent.py`.
- Read-only: neither function writes to S3 or Aurora (log line only).
- Every S3 key is built from the DB hop's `folder_name` and session id; nothing in the request names a folder, a date or a session.
- The CheckGaps function never raises to its caller: every path returns `{"status", "say", ...}`.
- Never log transcript text.
- Branch `feat/check-done-gaps`; never `git add -A`, never `git stash`; one pytest process at a time.
- Python: `C:\Users\camil\fspy\Scripts\python.exe`.

## Interface to the Ask line (they own the hook)

- Worker env var: `CHECK_GAPS_FUNCTION` -- the CheckGaps function name when the flag is on, `''` when it is off. Empty => treat as `error` (spec).
- Invoke `RequestResponse` with `{"caller_sub", "check_name", "spoken", "pressed_at"}`; boto `read_timeout` must exceed 35 s (CheckGaps' Timeout) and stay inside the worker's 60 s.
- Response: `{"status": "ok|no_recording|no_template|error", "say": str, "check": {"name", "template_id"} | None, "not_covered": [{"n","text"}], "failed": [{"n","text","comment"}], "items": int, "covered_until": int|None, "partial": bool}`.

## Interface between the two functions

CheckGapsDb input `{"caller_sub", "pressed_at"}` -> output (JSON-safe: uuids/datetimes as strings):

```json
{"user": {"id": "...", "company_id": "...", "folder_name": "..."} | null,
 "owner_company_id": "...",
 "session": {"session_id": "32hex", "opened_at": "iso", "last_segment_at": "iso",
             "kind": "audio|video|null", "site_id": "..."} | null,
 "templates": [{"id", "name", "scope", "body"}],
 "live_windows": [{"name", "kind", "start_at", "end_at", "end_source", "template_id", "report_date"}]}
```
or `{"error": "..."}`.

## Tasks

### Task 1: pure helpers in `check_done.py`

**Files:** Create `src/check_done.py`; Test `tests/unit/test_check_done_helpers.py`.

Functions:
- `strip_done_words(name) -> str` -- drops "done/finished/complete(d)/check(s)/checklist" and "what's left on/what is left on".
- `choose_template(check_name, templates, live_windows) -> (template|None, window|None, closest_names)` -- spoken match via `inspection_match.match({"kind": n, "name": n}, templates)`; else the latest live window with a template; a template without checklist items -> None; closest = two best names.
- `checklist_items(template) -> list[str]` -- every checklist section's items, template order (flattened).
- `last_covered_index(stored_keys, batch_maps) -> int|None` -- max over `_c{NNNN}` of plain keys and `members[].chunk_index` of batch keys (`batch_stitch.is_batch_key`); batch without a readable map -> first `_c` + count - 1 (lower bound; may re-transcribe, never skips).
- `select_new_chunks(audio_keys, session_id, covered) -> (chosen, skipped_older)` -- raw `.wav` of this session with index > covered, newest 8.
- `wav_seconds(data) -> float`, `chunk_end_epoch(key, seconds) -> int` (filename time is NZ wall clock -> `nz_time.from_nz` -> epoch).
- `say_ok(label, items_total, not_covered, failed, partial)`, `say_no_template(name, closest)`, `say_no_recording()`, `say_error()`; `first_clause(text)` cuts to first clause, max 12 words.

- [ ] Write failing tests: done-word stripping; template choice (spoken, live-window fallback, none, sectionless); last-covered (plain, batch map, batch with no map, silent gap = by index); chunk selection (index > covered, cap 8 + skipped, other session's chunks ignored, non-wav ignored); WAV duration from header; `say` wording (none / some / >5 / failed / partial / no_template / error).
- [ ] Run `C:\Users\camil\fspy\Scripts\python.exe -m pytest tests/unit/test_check_done_helpers.py -q` -> fails (no module).
- [ ] Implement; rerun -> pass. Commit.

### Task 2: transcript-so-far and the gap pass in `check_done.run`

**Files:** Modify `src/check_done.py`; Test `tests/unit/test_check_done_run.py`.

`run(event, *, db_hop, s3, bucket, gather, assemble, stt, call_llm, now=time.time, log=logger) -> dict`.
- `gather(bucket, folder, date, session_base)` / `assemble(bucket, keys)` default (in the handler) to `lambda_extract_session.gather_session_segments` / `assemble_deduped_turns`, imported lazily.
- `session_base = "sid" + session_id` (what item-writer stores in `inspection_windows.session` and what `gather_session_segments` matches).
- Dates: NZ date of `opened_at`, plus NZ date of `pressed_at` if different.
- STT: up to 8 chunks in a thread pool, each GET + `stt` (single attempt, 12 s), futures read with `result(timeout=)` against one shared 14 s budget; failure -> `partial`.
- Window: live window start (that day's `HH:MM:SS`) else the last 45 min before the newest evidence; the prompt says which.
- LLM: one `call_llm(prompt, enable_thinking=False, deadline=12, caller="check_gaps")`; lines -> `checklist.rebuild({"paragraphs", "line_refs"}, items, transcript_text, report_date=nz_today())`, the same `transcript_text` the prompt carried.
- Log: one JSON line: status, template id, counts, partial, timings. No text.

- [ ] Failing tests: no session -> `no_recording`; db error -> `error`; LLM error -> `error` with the spec's sentence; `rebuild` gets the model rows and the same transcript text (fake LLM), gaps returned unchanged; every S3 key read/listed is under `transcripts/{folder}/`, `users/{folder}/audio/` (a request carrying `folder`/`session` keys cannot steer it); `sid` prefix passed to `gather` and live windows; STT failure -> partial; no Ask key -> stored only, partial; video session -> partial, no audio listed; covered_until from WAV header; log line has no transcript text.
- [ ] Run -> fail; implement; run -> pass. Commit.

### Task 3: DB hop

**Files:** Modify `src/repositories/meeting_session.py` (add `latest_for_user_near`), `src/repositories/inspection_windows.py` (add `for_session`); Create `src/lambda_check_gaps_db.py`; Tests `tests/unit/test_check_gaps_db.py`, `tests/integration/test_check_gaps_db_sql.py`.

- `latest_for_user_near(conn, user_id, at_epoch, window_s=300)`: `WHERE user_id = %s AND COALESCE(last_segment_at, opened_at)` within +-300 s of `to_timestamp(%s)`, `ORDER BY COALESCE(last_segment_at, opened_at) DESC LIMIT 1`.
- `for_session(conn, company_ids, user_folder, session)`: `company_id = ANY(%s::uuid[]) AND user_folder = %s AND session = %s`.
- Handler: user by sub -> session -> owner company (session site's company, else the user's) -> `checklist_templates(conn, owner, user_id)` -> live windows for {owner, home} company. Never raises; `{"error"}` on failure.

- [ ] Failing unit tests (FakeConn): SQL filters by user and the activity window; another user's session never returned (param check); live-window SQL includes `user_folder`; handler shapes JSON (str uuids, iso times); unknown sub -> `user: null`.
- [ ] Failing integration tests: `latest_for_user_near` against real rows (other user's newer session ignored; outside 5 min ignored; NULL last_segment_at uses opened_at); `for_session` filters folder and company.
- [ ] Implement; run unit; run integration with `bash /c/Users/camil/fswork/run-tests.sh tests/integration/test_check_gaps_db_sql.py -q`. Commit.

### Task 4: CheckGaps handler

**Files:** Create `src/lambda_check_gaps.py`; Test `tests/unit/test_lambda_check_gaps.py`.

- DB hop via `boto3.client("lambda", config=Config(connect_timeout=2, read_timeout=11, retries={"max_attempts": 1}))`; `FunctionError` or bad payload -> `{"error"}`.
- `stt_chunk(audio, filename)`: one POST to `elevenlabs_utils.ELEVENLABS_STT_URL` with `ELEVENLABS_ASK_API_KEY`, timeout 12 s, no diarisation, raises on failure; no key -> handler passes `stt=None`.
- Import-light: `lambda_extract_session` imported inside the default `gather`/`assemble` only.

- [ ] Failing tests: handler with fake invoke returning FunctionError -> status error, `say` present; missing Ask key -> `stt is None` path; importing `lambda_check_gaps` does not import `lambda_extract_session`.
- [ ] Implement; run -> pass. Commit.

### Task 5: template, workflows, invariants

**Files:** Modify `src/template.yaml`, `.github/workflows/deploy.yml`, `.github/workflows/deploy-prod.yml`, `tests/unit/test_template_timeout_invariants.py`; Create `tests/unit/test_check_gaps_template.py`.

- Parameter `CheckDoneGaps` (default `'false'`); condition `ShouldCheckDoneGaps: !And [!Condition HasDb, !Equals [!Ref CheckDoneGaps, 'true']]`.
- `CheckGapsFunction` (no VpcConfig, Timeout 35, MemorySize 512, LLM env copied from the worker with `LLM_HTTP_TIMEOUT '12'`, `LLM_CHAIN_BUDGET_SECONDS '14'`, Ask STT env, `S3_BUCKET: IngestBucketName`, `CHECK_GAPS_DB_FUNCTION`), S3 Get on `transcripts/*`, `voice_ask/*`, `audio_segments/*`, `users/*/audio/*`; ListBucket with prefix condition on `transcripts/*`, `voice_ask/*`, `users/*/audio/*`; invoke CheckGapsDb.
- `CheckGapsDbFunction` (VpcConfig, PsycopgLayer, PG env copied from RagSearch, Timeout 10).
- Worker: `CHECK_GAPS_FUNCTION: !If [ShouldCheckDoneGaps, !Ref CheckGapsFunction, '']`; invoke grant as `!If [ShouldCheckDoneGaps, LambdaInvokePolicy, !Ref AWS::NoValue]`.
- Workflows: `"CheckDoneGaps=${{ vars.TEST_CHECK_DONE_GAPS || 'false' }}"` / `PROD_CHECK_DONE_GAPS`.
- `SYNC_CALLEES` += `("AskVoiceWorkerFunction", "CheckGapsFunction")`, `("CheckGapsFunction", "CheckGapsDbFunction")`.

- [ ] Failing template tests: CheckGaps has no `VpcConfig`, CheckGapsDb has; both under `ShouldCheckDoneGaps`; worker's ref and grant are `!If`-guarded (never an unconditional ref to a conditional function); grants listed above exist and nothing writes; workflows pass the param.
- [ ] Implement; run the template test files and the workflow-wiring tests. Commit.

### Task 6: verification

- [ ] Full unit suite: `C:\Users\camil\fspy\Scripts\python.exe -m pytest tests/unit -q`.
- [ ] cfn-lint 1.53.3 (CI pin) in a scratch venv: `cfn-lint src/template.yaml infra/db-template.yaml`.

## Deviations from the spec

1. **Condition name and composition:** `ShouldCheckDoneGaps = HasDb AND CheckDoneGaps='true'` (the HasSiteVoice shape), because CheckGapsDb needs the DB stack and PsycopgLayer (both HasDb). The parameter keeps the spec's name `CheckDoneGaps`.
2. **Worker grant is conditional, not the W1001 pattern.** RagSearch's unconditional Ref survives only because HasDb is always true on real stacks; this flag defaults to false, so an unconditional `!Ref CheckGapsFunction` would fail the stack. Env and grant are `!If`-guarded instead; no new W1001 match.
3. **Owner company.** item-writer writes `inspection_windows.company_id` and matches templates with the SITE's company (project-owned tenancy), not the recorder's home company. The DB hop does the same: templates from the owner company (session site's company, else the user's), live windows filtered by `company_id IN {owner, home} AND user_folder AND session`.
4. **Session activity anchor:** `COALESCE(last_segment_at, opened_at)` -- a session a few seconds old (opened by `/upload-url`) has no `last_segment_at` yet; the idle sweep uses the same anchor.
5. **`inspection_windows.session` is `sid{32hex}`** (item-writer's session base), not the bare id.
6. **Several checklist sections** are flattened into one numbered list for the one gap pass.
7. **`lambda_extract_session` is imported lazily** (as `lambda_rolling_summary` does) rather than copied; `assemble_deduped_turns` is used (the two-value view of `assemble_session_turns`).
8. **IAM beyond the spec's list:** `voice_ask/*` (Get + List) because `assemble_session_turns` runs `agent_turn_filter`, which raises on an unreadable listing; `audio_segments/*` Get for the batch-map sidecar.
9. **Batch map for the covered index** is read from the embedded `fieldsight_batch_map` in the batch transcript JSON (one extra GET per batch key); a batch with none falls back to first index + count - 1, which can only re-transcribe, never skip.
10. **Say line names the customer's template** ("3 items open on the Pre-pour Inspection
    Checklist: ...") rather than echoing the spoken name: the template name is the
    customer's wording and is always present (the live-window fallback has no usable
    spoken name). Over five items: "...; and N more." then "See the app for the rest."
11. **Video is detected from `meeting_session.kind`** ('video' -> no audio listed,
    `partial`); `users/*/video/*` is never listed or read.
12. **Shared test pins moved** (each pin exists to make a new function look here):
    `test_template_pgdatabase` 20 -> 21 guarded PGDATABASE; `test_qwen_model_follows_the_thinking_mode`
    QwenModelFast carriers 2 -> 3; `test_lake_ownership_template` lists CheckGapsFunction under
    IMPORTS_BUT_NEVER_WRITES (it imports lake_put modules via extract_session, never writes).
13. **No-template line** with no candidates: "I couldn't find a checklist set up for this check."
14. **Bounded stored read (not in the spec's budget).** Only stored keys from the window
    (live-window start, else 45 min before the newest stored key, minus a 10-minute margin)
    are assembled, and only the newest batch's embedded map is read. Unbounded, a
    three-hour session is ~100+ sequential GETs before the gap pass.
