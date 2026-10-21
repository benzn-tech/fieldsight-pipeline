# Retire the legacy gateway — Phase A Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Everything in sub-project 1 that does not wait on a week of observation: instrument the gateway, make its proxies fail closed, build the actions backfill, and move the web off the gateway's data routes. Phase B (410s, removing code, retiring the forgotten gateway) is a separate plan after the observation week.

**Architecture:** Backend changes in `fieldsight-pipeline` (gateway `src/lambda_fieldsight_api.py`, `src/lambda_ask_agent.py`, an org-api operator task). Frontend changes in `fieldsight-ui` (`scripts/api/sites.js`, `scripts/api/actions.js` and the overlay's readers). No new IAM: the backfill's input rows travel inline in the operator invoke.

**Tech Stack:** Python 3.11 Lambdas, psycopg 3, Aurora PostgreSQL; vanilla JS, `node --test`.

**Spec:** `docs/superpowers/specs/2026-10-05-retire-the-legacy-gateway-design.md` (rev 2) and the master `docs/superpowers/specs/2026-10-05-tenancy-roadmap-design.md`.

## Global Constraints

- Code comments, commits, docs in English. Never `git add -A`.
- Pipeline unit: `python -m pytest tests/unit -q` (report passed/skipped/failed). Integration: `bash /c/Users/camil/fswork/run-tests.sh tests/integration -q`, one pytest at a time.
- UI: `node --test tests/*.test.js`; `git checkout -- tests/fixtures/` before committing.
- Every new test red-proofed (revert, see it fail, restore); a test green against old code is labelled a regression pin.
- The instrumentation log line carries **no tenant content**: route, method, role, a `caller_sub` presence bool, User-Agent family. Never a folder, a date, a name or a body.
- Backfill writes go through `action_items.update_action_item_fields` + `content_edits.append_content_edit` (the `patch_action_item` path), with `updated_by` **null**.
- Backfill is a dry run unless `apply` is exactly `True` (the `COLLAPSE_PHOTOS_TASK` convention).
- UI live mode never substitutes fixture data for a failed directory call.
- org `/members` is admin/gm/platform_admin only; other roles use `/sites/{id}/members`.

## Review Focus

1. **A request whose authorizer yields an empty `sub`** reaching `/api/ask`, `/api/search` or the ask-agent's S3 path must be refused, never answered from S3. → Tasks 2.
2. **A legacy tick whose text appears twice in the same (date, folder)** must be listed, never applied to either. → Task 3.
3. **A legacy tick on a finding (`flag_N`, `obs_N`, `quality`)** must be recorded, never matched to an action item. → Task 3.
4. **A pm/site_manager/worker loading a page that needs users** must get site members via `/sites/{id}/members`, and a failure must render empty/error — never fixture people. → Task 4.
5. **A tick the user had set only in the legacy overlay** must still read as done after the UI drops the overlay — via the backfill having run before the UI release reaches prod. → Task 5 + rollout order.

---

## Part A — fieldsight-pipeline

Worktree: `git worktree add "C:\Users\camil\fswork\legacy-a" -b feat/legacy-gateway-phase-a origin/develop`.

### Task 1: Instrument the gateway — one line per request, after auth

**Files:** Modify `src/lambda_fieldsight_api.py` (`lambda_handler`); Test `tests/unit/test_legacy_gateway_logs_its_callers.py` (new).

**Interfaces:** Produces log line `LEGACY_CALL {json}` with keys exactly `route, method, role, has_sub, ua` where `ua ∈ {"browser","android","ios","other","none"}`.

- [ ] Step 1 — failing tests: (a) a request to `/api/timeline` with an authorizer `sub` and role logs one `LEGACY_CALL` line whose JSON has exactly those five keys; (b) `has_sub` is `false` when claims lack `sub`; (c) User-Agent `Mozilla/5.0 …` → `browser`, `okhttp/4…` or `Dalvik/…` → `android`, missing → `none`; (d) the line contains no query param value and no body value (send `params={"user":"Secret_Folder","date":"2026-01-01"}` and assert neither string appears in any log record); (e) `/api/health` logs nothing new. Use `caplog`. Stub `get_caller_identity` to return `{"sub": "...", "role": "gm", ...}`.
- [ ] Step 2 — run, see them fail.
- [ ] Step 3 — implement: a small `_ua_family(headers)` helper and, directly after `caller = get_caller_identity(event)`, `logger.info("LEGACY_CALL %s", json.dumps({...}, sort_keys=True))`. Headers are case-insensitive: read `User-Agent` and `user-agent`. Keep the existing `Request:` line unchanged.
- [ ] Step 4 — green; full unit suite; red-proof by reverting the source.
- [ ] Step 5 — commit `feat(legacy-api): log who still calls the gateway (no tenant content)`.

### Task 2: Proxies fail closed

**Files:** Modify `src/lambda_fieldsight_api.py` (the `/api/search`, `/api/ask`, `/api/ask/corroborate`, `/api/ask/voice` branches) and `src/lambda_ask_agent.py` (`lambda_handler`, the `caller_sub` / `RAG_SEARCH_FUNCTION` gate at ~2315); Test `tests/unit/test_ask_paths_fail_closed.py` (new).

- [ ] Step 1 — failing tests: gateway — each of the four routes returns **401** with `{"error": "sign-in required"}` when `caller["sub"]` is empty, and does not invoke the ask lambda (stub `lambda_client.invoke` to record calls); with a sub it still invokes, forwarding `caller_sub`. ask-agent — an event body with no `caller_sub` returns **401** and never calls `load_report`, `load_transcripts` or `download_json_from_s3` (stub each to record); with `caller_sub` and `RAG_SEARCH_FUNCTION` set it takes the RAG branch as before (stub `_rag_answer`).
- [ ] Step 2 — run, see them fail.
- [ ] Step 3 — implement: gateway — at the top of each of the four branches, `if not caller.get('sub'): return error('sign-in required', 401)`. ask-agent — before the RAG gate: `if not body.get('caller_sub'): return error('sign-in required', 401)` (keep the existing `error` helper's shape). Comment why: without a sub the next lines fall to a company-blind S3 read; the hand-built legacy `fieldsight-*` lambdas (spec step 6) are the only deploy target without `RAG_SEARCH_FUNCTION`, and they are being retired.
- [ ] Step 4 — green; check every existing ask-agent test that invokes the S3 path without `caller_sub` — each is now asserting the leak: rewrite it to pass a `caller_sub` where it tests the RAG path, or to assert 401 where it tested the no-sub path. List each in the commit message. Full unit suite; red-proof.
- [ ] Step 5 — commit `fix(ask): no sign-in, no answer -- the S3 path is never reached without a caller`.

### Task 3: The actions backfill — an org-api operator task

**Files:** Create `src/legacy_ticks_backfill.py`; Modify `src/lambda_org_api.py` (`lambda_handler`: a new `BACKFILL_LEGACY_TICKS_TASK = "backfill_legacy_ticks"` branch beside `COLLAPSE_PHOTOS_TASK`); Create `scripts/export_legacy_ticks.py` (operator, local: scans DynamoDB `fieldsight-audit` for `ACTIONS#` rows and prints the invoke payload); Tests `tests/unit/test_legacy_ticks_classify.py`, `tests/integration/test_legacy_ticks_backfill.py`.

**Interfaces:**
- `legacy_ticks_backfill.classify(row: dict) -> dict` → `{"date", "folder" | None, "topic": int|None, "action": str, "kind": "action"|"finding"|"no_folder", "text", "checked": bool, "checked_at", "checked_by"}`. `kind="finding"` when the action part is not a plain integer (`flag_N`, `obs_N`, `quality`, or a negative topic). `kind="no_folder"` when the SK has no `USER#{folder}#` prefix and the row has no `user_folder`.
- `legacy_ticks_backfill.run(conn, rows: list[dict], apply: bool) -> dict` → `{"applied": [...], "already_done": [...], "unmatched": [...], "ambiguous": [...], "findings": [...], "no_folder": [...], "unchecked": n}`; each list entry carries `date, folder, text` and, for applied/already_done, the `action_item_id`.
- Matching: only `checked` rows of `kind="action"`. Candidates: `SELECT ai.id, ai.text, ai.status FROM action_items ai JOIN topics t ON t.id = ai.topic_id JOIN users u ON u.id = t.user_id WHERE t.report_date = %(date)s AND u.folder_name = %(folder)s` (read the exact column names from the repositories before writing — `topics.report_date`, `topics.user_id`, `users.folder_name` per the spec's fact-check). Normalise text with `" ".join(s.lower().split())`. Exactly one candidate with equal normalised text → match; zero → `unmatched`; more than one → `ambiguous`.
- Write for a match whose status is not `done`: `action_items.update_action_item_fields(conn, id, {"status": "done"}, updated_by=None)` then `content_edits.append_content_edit(...)` with a note `legacy tick by {checked_by} at {checked_at}` — read both repo functions' exact signatures first and follow `patch_action_item`.
- Operator task: event `{"task": "backfill_legacy_ticks", "rows": [...], "apply": true|false}` → returns the `run` report. Dry run unless `apply is True`.

- [ ] Step 1 — unit tests for `classify`: the three real SK shapes from the spec (`USER#Jarley_Trainor#TOPIC#0#ACTION#0`, `TOPIC#0#ACTION#3`, `TOPIC#0#ACTION#flag_0`), a negative-topic `obs_1`, `quality`, and a row with `user_folder` but a folder-less SK.
- [ ] Step 2 — integration tests on real Postgres: build a company, user (folder `TickF-<uuid>`), topic on a date, three action_items (`"Fix the gate"`, `"Order scaffolding"`, and a duplicate `"Order scaffolding"`); rows: a tick on "Fix the gate" → applied (status `done`, one content_edits row, `updated_by` null); a tick on "order  SCAFFOLDING" → ambiguous, nothing written; a tick on "Nonexistent" → unmatched; a finding row → findings; a folder-less row → no_folder; a second `apply` → the first tick now `already_done`, nothing new written; `apply=False` writes nothing at all.
- [ ] Step 3 — implement `legacy_ticks_backfill.py`, the operator branch, and `scripts/export_legacy_ticks.py` (boto3 scan with pagination, `begins_with(PK, "ACTIONS#")`, prints `{"task": ..., "rows": [...], "apply": false}` as JSON; AWS profile from env; never writes).
- [ ] Step 4 — green; full unit + integration; red-proof the matcher (make it match on position instead of text → the reorder/duplicate tests fail).
- [ ] Step 5 — commit `feat(org-api): backfill legacy action ticks into action_items, by text, dry run first`.

### Part A wrap-up
- [ ] Rebase, full suites, push, PR to `develop`, merge on green CI.
- [ ] TEST: deploy lands; invoke the TEST org-api backfill **dry run** with rows exported from **`fieldsight-test-audit`** — expect a report and zero writes.
- [ ] PROD (mine, read-only): export from prod `fieldsight-audit`, invoke the **prod dry run** — reads only. Save the report for the owner. The `apply=true` run is the owner's.

---

## Part B — fieldsight-ui

Worktree: `git worktree add "C:\Users\camil\fswork\ui-legacy-a" -b feat/off-the-legacy-gateway origin/dev`. Read before editing: `scripts/api/sites.js`, `scripts/api/actions.js`, `scripts/api/org.js` (`getOrgSites`, `getMembers`, `orgRequest`), and every consumer named below.

### Task 4: Sites and users from the directory, never fixtures

**Files:** Modify `scripts/api/sites.js`; the consumers that substitute fixtures on failure (`scripts/**/tasks-aggregator.js`, `scripts/pages/evidence.js`, `scripts/pages/today.js` — find exact paths with `git grep -n "getUsers\|getSites"`); Test `tests/sites-and-users-come-from-the-directory.test.js` (new).

**Interfaces:**
- `getSites()` → org `GET /sites`, returning rows with **both** `site_id` (the UUID) and `slug`, plus the legacy fields consumers read (`name`, `users`/`user_count` as applicable — read each consumer to see what it uses, and map exactly those).
- `getUsers()` → admin/gm/platform_admin: org `GET /members`; other roles: union of `GET /sites/{id}/members` over `getSites()`; each row mapped to the legacy shape consumers read (`name`, `folder_name`, `role`, `sites`, `device_id` if read anywhere — read the consumers).
- `getSiteUsers(site)` → org `GET /sites/{id}/members` (accepts a UUID; if passed a legacy slug, resolve it through `getSites()`).
- Live mode: every one of the three rejects on failure; no consumer catches that and returns fixtures. Mock mode unchanged.

- [ ] Step 1 — failing tests (stub `orgRequest`, assert on calls and results): admin → `/members`; site_manager → `/sites` then `/sites/{id}/members` per site, union de-duplicated; a slug passed to `getSiteUsers` is resolved to the UUID; a failed `/members` in live mode makes the consumer produce **no fixture rows** (exercise one consumer's data function directly if it is exported; otherwise a wiring test that the live branch has no fixture fallback, labelled wiring-only); no request goes to `request('/sites'|'/users'|'/site-users')`.
- [ ] Steps 2–5 — red, implement, green + full suite, red-proof (restore the legacy call → the "no legacy request" test fails), commit `fix(sites): sites and users come from the directory -- and never from fixtures in live mode`.

### Task 5: Actions without the overlay

**Files:** Modify `scripts/api/actions.js` and the overlay's readers (`compliance-aggregator.js`, `tasks-aggregator.js`, `user-activity-aggregator.js`, `action-item-row.js`, `today-adapter.js` — exact paths via `git grep -n "getActions\|/actions\|checked"`); remove the dead writers `createAction` (and its caller wiring in `create-task-modal.js`) and the legacy `createSite`/`createUser`/`updateUserRole` in `sites.js` if no live path uses them (repoint to org if one does); Test `tests/done-ness-comes-from-the-task.test.js` (new).

**Interfaces:** done-ness of a task = `action_items.status === 'done'`, read from the org payload the page already has. No call to `request('/actions')` or `request('/actions/toggle')` remains. Toggling a task calls the existing `resolveActionItem` → `PATCH /action-items/{id}`. A report-sourced item with no `actionItemId` is not toggleable (rendered read-only) — the legacy toggle was its only writer.

- [ ] Step 1 — failing tests: each former overlay reader computes done-ness from `status` alone (feed an item with `status:'done'` and no overlay → done; an item with `status:'open'` and an overlay saying checked → **not** done); no module issues `/actions` or `/actions/toggle`; an id-less item renders without a toggle (wiring test if the renderer is not exported).
- [ ] Steps 2–5 — red, implement, green + full suite, red-proof, commit `fix(tasks): done means the task is done -- the legacy tick overlay is gone`.

### Part B wrap-up
- [ ] Rebase on `origin/dev`, full suite, push, PR to `dev`, merge.
- [ ] **Do not release Part B to `main` before the owner has run the prod backfill `apply`** (Review Focus 5): otherwise ticks that exist only in the overlay disappear from the UI until the backfill lands. Hand the owner: the prod dry-run report, the exact apply command, then the ui release PR and the Amplify `FS_LEGACY_READ_FALLBACK=false` step.

## Rollout summary

1. Part A → develop → TEST verify → release branch to `main` → owner approves deploy.
2. Prod backfill dry run (mine) → owner applies.
3. Part B → dev → ui release to `main` (owner merges or authorises) + Amplify `FS_LEGACY_READ_FALLBACK=false`.
4. One working week of `LEGACY_CALL` lines → Phase B plan (410s, code removal, forgotten gateway).
