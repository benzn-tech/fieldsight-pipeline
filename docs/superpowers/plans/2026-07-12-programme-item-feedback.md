# Programme ↔ Daily-Item Progress Feedback — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:subagent-driven-development. Each task below is one PR: a fresh implementer writes the TDD code from this task + the design spec, a task review gates it, the user merges. Steps use `- [ ]`.
>
> **Companion design spec (read first, it is the source of truth):**
> `docs/superpowers/specs/2026-07-12-programme-item-feedback-design.md`.
> This plan is task-level: each task names exact files, real call signatures, test assertions, and commands. Implementers write the full function bodies from the spec's §4 (DDL), §5 (algorithm), §6 (API) — cited per task.

**Goal:** Daily-recording topics suggest a progress/status update to the matching scheduled Programme task; a manager confirms before the change is written back to `programme.json`.

**Architecture:** A non-VPC matcher (internet: DashScope + Claude) is triggered by a `match_requests/` S3 artifact emitted by the in-VPC item-writer/ingest; it hard-gates candidates (site + date-window + not-completed), embed-ranks, LLM-classifies to one task-or-none (fail-closed), then Lambda-invokes an in-VPC suggestion-writer that inserts `pending` rows into a new Aurora table `programme_progress_suggestions` (migration 0008). Managers list/confirm/reject via org-api; confirm re-reads and whole-doc-PUTs `programme.json` with optimistic `updated_at` locking.

**Tech Stack:** Python 3.12 Lambdas (psycopg3, boto3), Aurora PostgreSQL (pgvector already present), SAM (`template.yaml`), S3 event wiring (`scripts/wire-s3-events.sh`), DashScope v4 embeddings + Claude (`dashscope_utils`/`claude_utils`), browser-React UI prototype (no build step).

## Global Constraints (every task inherits these — copied from the spec)

- **Account 509194952652 / ap-southeast-2 / `fieldsight-test` stack ONLY.** Never touch another account.
- **Migration number is 0008** (0007 latest applied); apply via `invoke fieldsight-test-migrate`; verify with Aurora **Data API** (`aws rds-data execute-statement`, HttpEndpoint enabled — no query lambda needed).
- **Merge to `develop` auto-triggers a SAM test deploy** (`.github/workflows/deploy.yml`). Before merging ANY pipeline PR that a deploy will touch, confirm **`DbSecret.password == fieldsight-test-rag-search PGPASSWORD` (MATCH)** so the deploy's PGPASSWORD re-injection can't break in-VPC lambdas (rotation trap). The new suggestion-writer is in-VPC → subject to this.
- **Matcher MUST be non-VPC.** In-VPC lambdas have only an S3 gateway endpoint, zero other egress (BUG-36) → cannot reach DashScope/Claude/Lambda-API. Hand-off is the `match_requests/` S3 artifact (mirror of embed-report→sidecar→ingest).
- **Never reuse the legacy report-JSON sequential `topic_id` / DynamoDB path** (`lambda_fieldsight_api.py`). Key strictly on Aurora `topics.id` (uuid).
- **Human-confirm write-back, no auto-close. Fail-closed matching** (below threshold or "none" → no suggestion, never force a link).
- **Reuse the existing embedding engine** (`dashscope_utils.embed`); do not add a provider.
- **Windows/AWS quirks:** `MSYS_NO_PATHCONV=1` for `/aws/...` and Data-API ARNs; `cygpath -w` for `fileb://`; use `node` (not python3) for JSON on the shell; single-line Edit anchors (CRLF); never `git add -A` on develop.
- **User merges PRs.** Each task = one branch off `develop` → PR.

## File Structure

| File | Create/Modify | Responsibility |
|---|---|---|
| `src/migrations/0008_programme_suggestions.sql` | Create | `programme_progress_suggestions` table (spec §4 DDL) |
| `src/repositories/programme_suggestions.py` | Create | CRUD: dedupe-upsert, list_for_site, get, decide, mark_stale |
| `src/lambda_suggestion_writer.py` | Create | in-VPC: payload → repo insert (idempotent by dedupe_key) |
| `src/lambda_programme_matcher.py` | Create | non-VPC: candidate gate + embed rank + Claude classify + invoke writer |
| `src/lambda_item_writer.py` | Modify | after commit, emit `match_requests/…` artifact |
| `src/lambda_ingest.py` | Modify | same emit for report-sourced topics; expose helper shared with item-writer |
| `src/lambda_org_api.py` | Modify | 3 routes: list / confirm / reject suggestions + write-back |
| `src/template.yaml` | Modify | MatcherFunction (non-VPC + LambdaInvokePolicy) + SuggestionWriterFunction (in-VPC) + IAM |
| `scripts/wire-s3-events.sh` | Modify | add `fs-programme-match` on prefix `match_requests/` suffix `.json` |
| `tests/unit/test_programme_suggestions_repo.py` … (per task) | Create | unit tests |
| `fieldsight-ui/scripts/api/programme.js`, `scripts/composites/suggestion-review.js`, `scripts/pages/programme.js`, `scripts/mock/programme-suggestions.fixture.js` | Create/Modify | review queue UI |

---

### Task 1 — Migration 0008 + suggestions repository

**Files:** Create `src/migrations/0008_programme_suggestions.sql`, `src/repositories/programme_suggestions.py`; Test `tests/unit/test_programme_suggestions_repo.py`.

**Interfaces — Produces (later tasks depend on these EXACT signatures):**
- `upsert_suggestion(conn, *, site_id, task_id, topic_id, topic_title, topic_summary, topic_user_id, report_date, source_s3_key, task_name, task_status_before, task_progress_before, suggested_status, suggested_progress, confidence, match_evidence: dict) -> dict` — INSERT … ON CONFLICT (dedupe_key) DO UPDATE SET topic_id=EXCLUDED.topic_id, updated_at=now() WHERE state='pending'. `dedupe_key` computed inside as `sha256(f"{site_id}|{task_id}|{report_date}|{norm(topic_title)}")`, `norm` = `" ".join(s.lower().split())`.
- `list_for_site(conn, site_id, state='pending') -> list[dict]`
- `get(conn, suggestion_id) -> dict | None`
- `decide(conn, suggestion_id, state, decided_by, applied_status=None, applied_progress=None) -> dict | None` (state ∈ confirmed/rejected; guards `WHERE state='pending'`)
- `mark_stale(conn, suggestion_id) -> dict | None`

**Consumes:** DDL per spec §4 verbatim. Repo style mirrors `src/repositories/observations.py` (module-level SQL, `conn.execute(...).fetchone()`, dict_row).

- [ ] **Step 1 — failing test.** `test_dedupe_key_stable_across_title_whitespace` + `test_upsert_conflict_keeps_decided_row` + `test_decide_only_pending`. Use the `FakeConn`/monkeypatch pattern from an existing repo test; assert `upsert_suggestion` builds the expected dedupe_key and that a second upsert of a `rejected` row does NOT flip it to pending. `pytest tests/unit/test_programme_suggestions_repo.py -v` → FAIL (module missing).
- [ ] **Step 2 — DDL.** Write `0008_programme_suggestions.sql` exactly per spec §4 (table + `uq_pps_dedupe`, `idx_pps_site_state`, `idx_pps_topic`). Header comment in `0006_observations.sql` style.
- [ ] **Step 3 — repo.** Implement the 5 functions. `dedupe_key` via `hashlib.sha256`. `match_evidence` via `json.dumps` into the jsonb column.
- [ ] **Step 4 — pass.** `pytest tests/unit/test_programme_suggestions_repo.py -v` → PASS. Full suite green: `pytest tests/unit -q`.
- [ ] **Step 5 — apply on TEST + verify.** Branch, PR, (after DbSecret==PGPASSWORD MATCH check) merge → deploy → `invoke fieldsight-test-migrate` → Data-API `SELECT to_regclass('programme_progress_suggestions')` returns the table.
- [ ] **Step 6 — commit.** `feat(db): 0008 programme_progress_suggestions table + repo`.

**Done:** table live on TEST, repo unit-tested.

---

### Task 2 — Suggestion-writer lambda (in-VPC)

**Files:** Create `src/lambda_suggestion_writer.py`, `tests/unit/test_lambda_suggestion_writer.py`; Modify `src/template.yaml` (SuggestionWriterFunction).

**Interfaces — Consumes:** Task 1 repo. **Produces:** invoke contract `{"suggestions": [ {site_id, task_id, topic_id, topic_title, topic_summary, topic_user_id, report_date, source_s3_key, task_name, task_status_before, task_progress_before, suggested_status, suggested_progress, confidence, match_evidence} … ] } -> {"written": N}`.

- [ ] **Step 1 — failing test.** `test_writes_each_suggestion` + `test_empty_list_no_db` (mirror `test_lambda_item_writer.py`: monkeypatch `get_connection` + repo). FAIL.
- [ ] **Step 2 — handler.** `lambda_handler(event, _)`: `with get_connection() as conn:` loop `upsert_suggestion(conn, **s)`; return count. Reuse `db.connection.get_connection` (autocommit per item-writer). Guard: empty list returns `{"written":0}` without opening a connection.
- [ ] **Step 3 — pass.** `pytest tests/unit/test_lambda_suggestion_writer.py -v`.
- [ ] **Step 4 — SAM.** Add `SuggestionWriterFunction` mirroring `ItemWriterFunction` (template.yaml ~:967) **verbatim** for `VpcConfig`, `Layers`, `Environment` PG vars, `Condition: HasDb`; `Handler: lambda_suggestion_writer.lambda_handler`.
- [ ] **Step 5 — deploy + smoke.** Merge (MATCH check first) → deploy → manual `invoke fieldsight-test-suggestion-writer` with one fake suggestion → Data-API confirms a `pending` row.
- [ ] **Step 6 — commit.** `feat(lambda): suggestion-writer (in-VPC) inserts pending suggestions`.

**Done:** in-VPC writer live; a payload produces a row.

---

### Task 3 — Matcher lambda (non-VPC): pure core + adapters

**Files:** Create `src/lambda_programme_matcher.py`, `tests/unit/test_lambda_programme_matcher.py`; Modify `src/template.yaml` (MatcherFunction, non-VPC + LambdaInvokePolicy on the writer).

**Interfaces — Consumes:** `programme.read_programme(s3, bucket, site_id)`, `dashscope_utils.embed(texts)->vectors`, `claude_utils.call_claude(prompt, max_tokens)` + `claude_utils.extract_json(raw)`. **Produces:** nothing downstream depends on internal shapes except the writer payload (Task 2 contract).

**Pure functions (test WITHOUT I/O):**
- `candidate_tasks(programme: dict, report_date: date, lead=7, lag=14) -> list[task]` — keep `status not in ('completed','group')` and `start-lead ≤ report_date ≤ end+lag`; open-ended `end` = ongoing.
- `rank_by_embedding(topic_vec, task_vecs, tasks, max_dist=0.55, top_k=5) -> list[task]` — cosine distance filter + top-K.
- `build_prompt(topic, candidates) -> str` and `parse_verdict(raw, survivors) -> dict|None` — accept only if `task_id ∈ survivors`, `confidence ≥ CONF_MIN(0.70)`, and it's a real change; else `None` (fail-closed).

- [ ] **Step 1 — failing tests (pure).** `test_candidate_window_includes_slipped_task`, `test_candidate_excludes_completed`, `test_rank_drops_beyond_max_dist`, `test_parse_verdict_none_when_llm_picks_nonsurvivor`, `test_parse_verdict_none_below_confidence`. FAIL.
- [ ] **Step 2 — implement pure core.** No boto3/HTTP in these functions.
- [ ] **Step 3 — pass pure.** `pytest tests/unit/test_lambda_programme_matcher.py -v`.
- [ ] **Step 4 — handler + adapters (stubbed in tests).** `lambda_handler` reads `match_requests/…` S3 object → per topic: read programme (S3) → `candidate_tasks` → `embed` topic+tasks → `rank_by_embedding` → `call_claude(build_prompt)` → `parse_verdict` → collect writer payload → `boto3.client('lambda').invoke(FunctionName=SUGGESTION_WRITER_FUNCTION, Payload=…)`. Env: `S3_BUCKET`, `SUGGESTION_WRITER_FUNCTION`, `SIM_MAX_DIST`, `CONF_MIN`, `TOP_K`, `LEAD_DAYS`, `LAG_DAYS`. Fail-closed: any embed/claude/parse failure → write nothing for that topic, re-raise so S3 retries. Test with stubbed `embed`/`call_claude`/lambda-invoke (style of `test_lambda_extract_session.py`).
- [ ] **Step 5 — SAM.** `MatcherFunction` non-VPC (mirror `ExtractSessionFunction`/`EmbedReportFunction` ~:815/:923 — Layers + `Condition: HasDb`, NO VpcConfig) + `LambdaInvokePolicy: {FunctionName: !Ref SuggestionWriterFunction}` (mirror AskAgent→RagSearch at :629) + env `SUGGESTION_WRITER_FUNCTION: !Ref SuggestionWriterFunction`; IAM read on `programmes/*` and `match_requests/*`; DashScope/Claude keys as the extract-session function has them.
- [ ] **Step 6 — deploy + dry smoke.** Merge → deploy → drop a hand-written `match_requests/…json` in TEST bucket referencing a real topic+site with a real programme → CloudWatch shows candidate→verdict; a `pending` row lands (via the writer).
- [ ] **Step 7 — commit.** `feat(lambda): programme-matcher (non-VPC) — gate/embed/classify, fail-closed`.

**Done:** end-to-end matcher path works from a hand-dropped artifact.

---

### Task 4 — Emit `match_requests/` from item-writer + ingest; stale-marking

**Files:** Modify `src/lambda_item_writer.py`, `src/lambda_ingest.py`; Create `src/match_request.py` (shared emit helper); Tests alongside.

**Interfaces — Produces:** artifact `match_requests/{site_id}/{report_date}/{sha256(source_s3_key)[:16]}.json` = `{site_id, report_date, source_s3_key, topics:[{topic_id, title, summary, user_id, action_items}]}`.

- [ ] **Step 1 — failing test.** `test_emit_writes_artifact_key_shape` (shared helper) + item-writer test asserting the helper is called with the freshly-written topic ids after commit. FAIL.
- [ ] **Step 2 — helper.** `match_request.emit(s3, bucket, site_id, report_date, source_s3_key, topics)`; deterministic key (idempotent overwrite on reprocess).
- [ ] **Step 3 — wire.** item-writer (after its `with get_connection()` commit) and ingest (report-topic path) call `emit(...)` with the topics they wrote. Skip when zero topics.
- [ ] **Step 4 — stale-marking.** When a topic is superseded/deleted (topics.py bulk-delete path), pending suggestions for the vanished `topic_id` are set `stale` (via the writer or a small repo call the deleters invoke). Test the transition.
- [ ] **Step 5 — pass + E2E.** Merge → deploy → wire `scripts/wire-s3-events.sh` adding `{"Id":"fs-programme-match", prefix "match_requests/", suffix ".json"}` → run the wire script → drop a real `extractions/…json` → a `pending` suggestion appears end-to-end.
- [ ] **Step 6 — commit.** `feat(pipeline): emit match_requests on topic write + stale-mark superseded suggestions`.

**Done:** a new daily extraction produces a suggestion with no manual step.

---

### Task 5 — org-api endpoints + confirmed write-back

**Files:** Modify `src/lambda_org_api.py`, `tests/unit/test_lambda_org_api.py` (extend).

**Interfaces — Consumes:** Task 1 repo, `programme.read_programme/write_programme`, `_allowed_site_ids(conn, caller)`, `_resolve_site_param(conn, caller, site_param)`, `resolve_scope`, existing `ok/error` responders. Routes gated admin/gm/pm (mirror `put_programme` role gate).

- [ ] **Step 1 — failing tests.** `test_list_suggestions_acl`, `test_confirm_applies_and_stamps`, `test_confirm_409_when_task_missing`, `test_confirm_optimistic_conflict`, `test_reject_blocks_resuggest`, `test_non_manager_403`. FAIL.
- [ ] **Step 2 — routes.** Add to the path dispatch: `GET /programme/suggestions`, `POST /programme/suggestions/{id}/confirm`, `POST /programme/suggestions/{id}/reject`. List → `programme_suggestions.list_for_site` scoped by `_allowed_site_ids`. Confirm → re-read programme.json; task missing → `mark_stale` + 409; **optimistic check**: suggestion carries the `updated_at` seen at match time in `match_evidence`; if the live doc's `updated_at` differs → 409 (client re-reviews); else apply reviewer-editable `{status, progress_pct}` (never lower progress unless explicitly typed), `write_programme(..., updated_at=now-iso)`, `decide(state='confirmed', decided_by=caller.id, applied_*)`. Reject → `decide(state='rejected')`.
- [ ] **Step 3 — pass.** `pytest tests/unit/test_lambda_org_api.py -v`.
- [ ] **Step 4 — deploy + curl smoke.** Merge → deploy → confirm a real pending suggestion → Data-API shows `confirmed` + the site's `programme.json` task status changed.
- [ ] **Step 5 — commit.** `feat(org-api): list/confirm/reject programme suggestions + optimistic write-back`.

**Done:** a manager can list, confirm (writes back), reject via the API.

---

### Task 6 — UI review queue

**Files:** Create `fieldsight-ui/scripts/composites/suggestion-review.js`, `scripts/mock/programme-suggestions.fixture.js`; Modify `scripts/api/programme.js` (add `getSuggestions/confirmSuggestion/rejectSuggestion`, org-live gated), `scripts/pages/programme.js` (mount the queue + task-row badge), and the preview HTML cache-busters.

**Interfaces — Consumes:** Task 5 endpoints via `orgRequest` (org-live), mocks otherwise. Follow `ui/CLAUDE.md`: mock-first, `useMocks`/`orgLive()` gating, cache-buster bumps, tokens-only styling, reduced-motion.

- [ ] **Step 1 — api layer.** `getSuggestions({site, state})`, `confirmSuggestion(id, {status, progress_pct})`, `rejectSuggestion(id)` — real via `orgRequest` when `orgLive()`, else fixture. `node --check`.
- [ ] **Step 2 — composite.** `SuggestionReview` card: topic title/summary/date/speaker, target task + delta (`progress 12%→60%`, `status→completed`), rationale, assignee-overlap badge, Confirm / Adjust… / Reject; evidence deep-link reuses the timeline spotlight `?topicTitle=&site=`.
- [ ] **Step 3 — mount.** Programme page right pane "Suggested updates (n)" + task-row pending badge; live-gated (don't ship the write ahead of backend — it is now live from Task 5).
- [ ] **Step 4 — verify.** `node --check` all touched JS; register in `components-preview.html`; bump cache-busters; browser E2E on Amplify dev (Ben Lin admin): a pending suggestion renders, Confirm updates the task, Reject dismisses.
- [ ] **Step 5 — commit.** `feat(ui): programme suggestion review queue + confirm/reject`.

**Done:** managers review and act on suggestions in the live UI.

---

### Task 7 — Calibration, smoke, docs

**Files:** Modify runbook/docs; no schema change.

- [ ] **Step 1 — backfill replay.** `invoke fieldsight-test-programme-matcher {"backfill":{"site":…,"date_from":…,"date_to":…}}` over an existing test site that has both topics and a programme; record precision (how many suggestions were correct).
- [ ] **Step 2 — tune.** Adjust `SIM_MAX_DIST`/`CONF_MIN`/`LEAD_DAYS`/`LAG_DAYS` env from observed precision (start conservative → widen). Re-run.
- [ ] **Step 3 — docs.** Update CLAUDE.md/runbook with the new chain (`match_requests/` prefix, matcher non-VPC, writer in-VPC, thresholds) + a note that the matcher is manual-backfillable when a programme is uploaded after items exist.
- [ ] **Step 4 — Fable final review** (once quota resets) + full browser E2E smoke checklist.
- [ ] **Step 5 — commit.** `docs: programme-feedback runbook + tuned thresholds`.

**Done:** thresholds tuned, chain documented, reviewed.

---

## Self-review (author, 2026-07-12)

- **Spec coverage:** §2 flow → Tasks 3/4; §3 D1 (S3 blob + Aurora links) → Task 1/5; D2 (gate→embed→LLM, fail-closed) → Task 3; D3 (table, dedupe, SET NULL, confirm) → Tasks 1/4/5; D4 (event-driven + backfill) → Tasks 3/4/7. §4 DDL → Task 1. §5 algorithm → Task 3. §6 API+UI → Tasks 5/6. §7 phases → Tasks 1-7. §8 resolutions honored (topic-level; status+editable progress; Aurora-only provenance; admin/gm/pm; optimistic locking; conservative thresholds). No gap found.
- **Signatures cross-checked** against code this session: `dashscope_utils.embed(texts,dim=None)`, `claude_utils.call_claude/extract_json`, `programme.read_programme/write_programme(...,updated_at)`, `_allowed_site_ids/_resolve_site_param`, observations.py repo shape, item-writer `EXTRACTION_KEY_RE`/commit-per-extraction, template.yaml AskAgent LambdaInvokePolicy + ItemWriter/ExtractSession blocks, wire-s3-events `fs-*` prefix entries. Consistent across tasks (`upsert_suggestion`/`decide`/`emit` names used identically downstream).
- **Optimistic-lock note:** relies on `programme.json` carrying `updated_at` (write_programme sets it). Confirm stashes the match-time `updated_at` in `match_evidence` — Task 3 must record it; Task 5 reads it. Flagged as the one cross-task coupling to watch in review.
- **Granularity:** task-level (not micro-step-with-full-code) — calibrated for capable subagent implementers who also read the design spec; each task is still one reviewable PR ending in a testable deliverable.
