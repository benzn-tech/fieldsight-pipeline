# Programme-Impact Link (Unified Extraction, Task 3) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:subagent-driven-development. Each task below is one PR: a fresh implementer writes the TDD code from this task + the design spec, a task review gates it, the user merges. Steps use `- [ ]`.
>
> **Companion design spec (read first, it is the north star):**
> `docs/superpowers/specs/2026-07-13-unified-extraction-labeling-design.md` — §4 (enriched schema incl. `programme_impact`), §5 (data model), §8 (phases — Task 1 rich extractor SHIPPED as PR #46; this plan is §8 Task 3), §9 (⚠️ one link table, not two).
> Sibling mechanism being reused: `docs/superpowers/specs/2026-07-12-programme-item-feedback-design.md` + `docs/superpowers/plans/2026-07-12-programme-item-feedback.md` (matcher/writer/artifact, PRs #38–#44, all live on TEST).

**Goal:** For each extracted finding, determine which programme task it impacts and how badly, by REUSING the existing programme-matcher (candidate gate → DashScope embed rank → Claude verdict, fail-closed), and store that link in ONE place — so a manager can see "entity X / finding F is hurting task Y, major impact". 100% backend/data; UI is a later phase.

**Architecture (one sentence):** item-writer persists the rich `findings` to a NEW Aurora child table (migration 0010) and includes them (with their durable uuids) in the SAME `match_requests/` artifact it already emits; the SAME non-VPC matcher gains an impact phase (one extra Claude call per topic, reusing the topic's candidate gate + candidate embeddings); the SAME in-VPC suggestion-writer gains an `impacts` payload key and applies each verdict as an UPDATE on the finding row (`programme_task_id`, `impact_severity`, `impact_note`, …) — no new lambda, no new S3 prefix, no new link table.

---

## §0 Investigation record (file:line evidence — verified 2026-07-13)

1. **Extractor output (`src/lambda_extract_session.py`)** — `EXTRACTION_SCHEMA` (:140-179) emits per-topic `findings` (`observation`/`domain`/`severity`/`entity{name,trade}`/`recommended_action`), `decisions`, `questions`, `origin`, plus legacy-derived `safety_flags` (`_derive_safety_flags` :256-273, severity→risk_level bridge so item-writer/ingest needed no change in PR #46). Output: `extractions/{user}/{date}/{session_base}.json` (:393-398). **It carries NO programme fields** — confirmed, as expected. Non-VPC (docstring :5-7).
2. **The matcher (`src/lambda_programme_matcher.py`)** — non-VPC (BUG-36, :3-9), S3-triggered on `match_requests/{site_id}/{report_date}/{sha256(source_key)[:16]}.json` (artifact contract :48-52, emitter `src/match_request.py:23-45`, trigger `scripts/wire-s3-events.sh:129-137` Id `fs-programme-match`). Input: topics `{topic_id, title, summary, user_id, action_items}`. Per topic (:320-417): `programme.read_programme` (S3, PROGRAMME_BUCKET=DataBucketName — template.yaml:1117-1121, a DIFFERENT bucket than the lake) → `candidate_tasks` date-window gate (:140-169) → `dashscope_utils.embed` batch (:343) → `rank_by_embedding` ≤0.55 top-5 (:188-198) → one Claude call → `parse_verdict` double-gate (survivor set + `CONF_MIN` 0.70, :266-292) → real-change check → collect. Writes NOTHING itself: single `lambda.invoke` of the in-VPC `SuggestionWriterFunction` after ALL topics succeed (:437-449, fail-closed; `FunctionError` → raise → S3 retry). Supports `{"dry_run": true}` (:434-435). It matches **topic→task**, not finding→task.
3. **The link table (`src/migrations/0008_programme_suggestions.sql` + `src/repositories/programme_suggestions.py`)** — a row is a human-confirmable progress-update PROPOSAL: `suggested_status/suggested_progress` with `CHECK (suggested_status IS NOT NULL OR suggested_progress IS NOT NULL)` (0008:29), state machine `pending→confirmed/rejected/stale` (0008:21-22), `dedupe_key = sha256(site|task|date|norm(title))` (repo :26-31), `topic_id ... ON DELETE SET NULL` (0008:7) so rows SURVIVE nightly topic supersession. Consumed by org-api review queue (`lambda_org_api.py:199-206`, :828-965) which write-backs `programme.json` on confirm.
4. **The item store** — `topics` + children `action_items`/`safety_observations`/`topic_photos` (0003), `observations` (0006, manual entries — different lineage, not extraction-fed). **NO findings/decisions/questions tables exist.** `lambda_item_writer.write_extraction_items` (:93-179) writes topics with action_items + `safety=_map_safety(t["safety_flags"])` ONLY — the rich findings are dropped at the Aurora boundary today. Nightly supersession: `lambda_ingest.py:297` `delete_topics_for_source_prefix("extractions/{user}/{date}/")` cascades children (topics.py:76-101); item-writer's I-4 guard (:110-119) skips late extractions once the report is ingested.
5. **Wiring already in place** — item-writer AND ingest both emit `match_requests/` after commit (`lambda_item_writer.py:176-177`, `lambda_ingest.py:353-354`); `match_request.emit` passes the caller-built topic dicts through verbatim (match_request.py:23-45) → **extending the artifact needs zero change to match_request.py or wire-s3-events.sh**. IAM for `match_requests/*` Put (ingest template.yaml:906-912, item-writer :1024-1030) and matcher Get (:1142-1148) already granted.
6. **Severity fields** — the finding's `severity` is ALREADY defined as schedule-impact severity, task-agnostic (extract prompt :208-209: "the finding's impact on the SCHEDULE/programme"). Spec §4 gives `programme_impact` its OWN severity. Resolution in D3 below.

## §1 The CRUX (spec §9) — resolved

**A `programme_impact` is a NEW KIND of link, distinct from a suggestion — but it does NOT get a new link table. It lives as columns on the finding row.**

- **Distinct semantics:** a suggestion is a PROPOSAL (mutate programme.json, human-gated, decision state machine); an impact is a read-only ANALYTICAL signal (this finding is about / is hurting task T). Cramming impacts into `programme_progress_suggestions` fails structurally: its CHECK requires a suggested_status/progress an impact doesn't have (0008:29); its `pending` rows feed the manager review queue (`list_for_site` defaults `state='pending'`) so impact rows would pollute the confirm/reject workflow every existing consumer relies on; its dedupe/decide semantics are meaningless for a signal nobody "confirms".
- **No second link table either:** each finding impacts AT MOST ONE task (spec §4: singular `programme_impact` object; matcher is pick-one-or-none). A 1:1 optional link is a column set, not a junction table. Spec §5 says exactly this: "add `findings` with … `programme_task_id`/`impact_severity`". So the system keeps exactly ONE separate link-ish table (`programme_progress_suggestions`, the proposal queue) and the impact link is a property of the finding — §9 satisfied by construction, and there is nothing to drop/migrate later when Task 4 flips authority.
- **What IS shared (reuse at the mechanism level):** the `match_requests/` artifact + S3 trigger, the matcher lambda (candidate gate, embeddings, double-gate, fail-closed error model, dry_run), the in-VPC writer lambda (extended payload — no sibling lambda), and the non-VPC→in-VPC split (BUG-36).

## §2 Design decisions

- **D1 — storage:** new migration `0010_findings.sql` creates `findings` (child of topics, 0003 style: `topic_id ... ON DELETE CASCADE`, denormalized `site_id`, `status DEFAULT 'open'`) with the extraction fields (`observation`, `domain`, `severity`, `entity_name`, `entity_trade`, `recommended_action`) AND the impact columns (`programme_task_id text`, `impact_severity`, `impact_note`, `impact_task_name` — display snapshot mirroring 0008's `task_name` precedent, since resolving id→name needs a per-site S3 read — `impact_evidence jsonb`, `impact_matched_at timestamptz`). Deliberately NOT created here: `decisions`/`questions` tables (spec §5 lumps them, but they have no programme-impact role — defer to Task 4/5 planning; note for those planners).
- **D2 — wiring:** extract-session is UNTOUCHED. item-writer inserts findings rows and adds `findings: [{finding_id, observation, domain, severity, entity_name, entity_trade}]` to each topic entry in the artifact it already emits. Matcher gains an impact phase per topic: reuse the topic's `candidate_tasks` result, extend the ONE existing embed batch with the finding observation texts, per-finding `rank_by_embedding` against the already-computed candidate vectors, then ONE additional Claude call per topic returning a per-finding verdict array; per-finding double-gate (own survivor set + CONF_MIN). Writer gains `"impacts": [...]` and applies each as `UPDATE findings SET programme_task_id=…, impact_severity=…, … WHERE id=%s` — rowcount 0 (row vanished under supersession/re-extraction) is a normal skip, not an error.
- **D3 — severity semantics (spec §4 vs finding.severity):** they are related but NOT the same field. `findings.severity` = extraction-time schedule-impact severity with NO task context (extract prompt :208-209). `impact_severity` = the matcher's task-specific rating, judged WITH the matched task in view; the impact prompt passes the finding's severity as the prior/default and instructs Claude to deviate only when the task context clearly warrants it; a missing/invalid verdict value falls back to the finding's severity. Both are stored (extraction-time vs match-time provenance). `impact_severity='none'` with a matched task is VALID and stored — attribution ("this finding is about task T") is useful even without schedule pain; dashboards filter.
- **D4 — idempotency, no dedupe table:** impacts are UPDATEs keyed on `finding_id`, inherently idempotent. Re-extraction deletes/re-inserts findings (new uuids) → item-writer re-emits the deterministic artifact key → matcher re-runs → updates the NEW rows. An in-flight matcher holding stale finding_ids hits rowcount 0 and the newer artifact's run heals it. Suggestions keep their existing dedupe_key path, untouched.
- **D5 — provisional-data interaction (authority flip is Task 4 — FLAG):** findings + their impact links attach to the provisional extraction topics and ARE deleted by the nightly supersession (`lambda_ingest.py:297` cascade) and NOT re-created by the report path (daily_report.json has no findings). Until Task 4 lands, impact links are **same-day-only signals**. This is accepted deliberately: (a) spec §8 orders 3 before 4 knowing §2's overwrite; (b) Task 4 makes them persistent automatically with zero rework; (c) the alternative — an independent snapshot/link table that survives the wipe — is precisely the second overlapping link table §9 forbids, and would be obsolete the day Task 4 lands. Do NOT "fix" this here.
- **D6 — the S3 extraction JSON never carries `programme_impact`.** Spec §4 draws it inside the extraction schema, but the impact is computed DOWNSTREAM of item-writer; writing it back to `extractions/…` would re-trigger `fs-write-extractions` → infinite loop (BUG-13 family). The authoritative impact lives in Aurora only. (Spec inaccuracy noted — see §5.)
- **D7 — read path:** extend `topics.list_topics_for_date` with a third batched child query (same pattern as action_items/safety at topics.py:142-156) attaching `findings` (all columns incl. impact) per topic → `/live-items` (org-api :706-716) exposes them under the existing ACL with zero route changes. The eventual dashboard reads "entity_name → impact_task_name, impact_severity" straight off this. A dedicated aggregation endpoint (`/programme/impacts?site=`, "which findings hit task Y") is deferred to the dashboard phase — the partial index in 0010 pre-provisions it.
- **D8 — transitional double-write, FLAG:** safety-domain findings now land twice (legacy `safety_observations` via the PR #46 bridge + the new `findings` table). Accepted until Task 4 retires the bridge; rollup counts (`repositories/rollup.py`) keep reading `safety_observations` unchanged. Document, don't dedupe.

## Global Constraints (every task inherits these)

- **Account 509194952652 / ap-southeast-2 / `fieldsight-test` stack ONLY.**
- **Migration number is 0010** (0009_recordings latest). Apply via `invoke fieldsight-test-migrate`; verify with Aurora Data API (`aws rds-data execute-statement`).
- **Merge to `develop` auto-triggers the SAM test deploy.** Before merging ANY PR, confirm **`DbSecret.password == a live in-VPC lambda's PGPASSWORD` (MATCH)** (e.g. `fieldsight-test-rag-search`) — the rotation trap: deploy re-snapshots `{{resolve:secretsmanager:…}}`; a mid-rotation mismatch breaks every in-VPC lambda as silent "no records found".
- **Matcher stays non-VPC; all Aurora writes go through the in-VPC writer invoke** (BUG-36). Never add Aurora/psycopg to the matcher.
- **Fail-closed matching everywhere:** no survivor / null verdict / low confidence → no impact written, never force a link. Exceptions propagate → S3 retry → idempotent re-run.
- **Postgres traps (recurring "mock passes, live catches"):** any `WHERE (%s IS NULL OR col=%s)` needs an explicit cast (`%s::text IS NULL` — the PR #43 AmbiguousParameter lesson, see programme_suggestions.py:65-66); jsonb params via `psycopg.types.json.Jsonb` (chunks.py convention); ALWAYS follow a mock-green deploy with a Data-API live verification.
- **Windows/AWS quirks:** `MSYS_NO_PATHCONV=1` for `/aws/...` ARNs; `cygpath -w` for `fileb://`; `node` (not python3) for shell JSON; single-line Edit anchors (CRLF repo); never `git add -A` on develop.
- **User merges PRs.** Each task = one branch off `develop` → PR. Do not touch `programme_progress_suggestions` schema or the suggestion flow's behavior.

## File Structure

| File | Create/Modify | Responsibility |
|---|---|---|
| `src/migrations/0010_findings.sql` | Create | `findings` child table + impact columns + indexes |
| `src/repositories/findings.py` | Create | `insert_findings`, `apply_impact`, `list_for_topics` |
| `src/lambda_item_writer.py` | Modify | persist findings; add them (with uuids) to the artifact topics |
| `src/lambda_programme_matcher.py` | Modify | impact phase: shared candidates/embeddings + 1 Claude call/topic + per-finding double-gate |
| `src/lambda_suggestion_writer.py` | Modify | accept `impacts` key → `findings.apply_impact` per row |
| `src/repositories/topics.py` | Modify | `list_topics_for_date` attaches `findings` (batched) |
| `src/lambda_org_api.py` | none (verify only) | `/live-items` picks the new child up automatically |
| `src/template.yaml` | Modify (tiny) | writer IAM unchanged; matcher unchanged; nothing new — only bump comments if touched |
| `tests/unit/test_findings_repo.py`, extend `test_lambda_item_writer.py`, `test_lambda_programme_matcher.py`, `test_lambda_suggestion_writer.py`, `test_topics_repo.py` | Create/Modify | TDD per task |

---

### Task 1 — Migration 0010 + findings repository

**Files:** Create `src/migrations/0010_findings.sql`, `src/repositories/findings.py`, `tests/unit/test_findings_repo.py`.

**DDL (0006/0008 header-comment style; 0003 child-table lineage):**
```sql
-- 0010: findings (unified-extraction Task 3 — rich per-topic findings +
-- programme-impact link; spec docs/superpowers/specs/2026-07-13-unified-extraction-labeling-design.md §4/§5.
-- The impact link lives HERE as columns (1 finding -> at most 1 task), NOT in a
-- second link table — programme_progress_suggestions (0008) stays the only
-- proposal/link table (spec §9).)
CREATE TABLE findings (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  topic_id           uuid NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
  site_id            uuid NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  observation        text NOT NULL,
  domain             text CHECK (domain IN ('safety','quality','progress')),
  severity           text CHECK (severity IN ('none','minor','major')),
  entity_name        text,
  entity_trade       text,
  recommended_action text,
  programme_task_id  text,
  impact_severity    text CHECK (impact_severity IN ('none','minor','major')),
  impact_note        text,
  impact_task_name   text,
  impact_evidence    jsonb,
  impact_matched_at  timestamptz,
  status             text NOT NULL DEFAULT 'open',
  created_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_findings_topic ON findings (topic_id);
CREATE INDEX idx_findings_site_domain ON findings (site_id, domain);
CREATE INDEX idx_findings_task ON findings (programme_task_id) WHERE programme_task_id IS NOT NULL;
```

**Repo signatures (later tasks depend on these EXACTLY; style mirrors observations.py — module-level SQL, dict_row, Jsonb):**
- `insert_findings(conn, topic_id, site_id, findings: list[dict]) -> list[dict]` — batch INSERT … RETURNING all cols; input dicts use the EXTRACTOR's field names (`observation`, `domain`, `severity`, `entity` nested dict → flattened to `entity_name`/`entity_trade` HERE, `recommended_action`); defensive `.get` everywhere (Claude output); enum-invalid `domain`/`severity` values are passed as NULL, never raised (fail-open like the extractor).
- `apply_impact(conn, finding_id, *, task_id, impact_severity, impact_note, impact_task_name, impact_evidence: dict) -> dict | None` — `UPDATE findings SET programme_task_id=%s, impact_severity=%s, impact_note=%s, impact_task_name=%s, impact_evidence=%s, impact_matched_at=now() WHERE id=%s RETURNING …`; None = row gone (normal skip). `impact_evidence` through `Jsonb(...)`.
- `list_for_topics(conn, topic_ids) -> list[dict]` — `WHERE topic_id = ANY(%s) ORDER BY created_at` (batched, mirrors topics.py:143-147).

- [ ] **Step 1 — failing tests.** `test_insert_flattens_entity_and_nulls_bad_enum`, `test_apply_impact_returns_none_when_row_gone`, `test_apply_impact_uses_jsonb_wrapper`, `test_list_for_topics_batches` (FakeConn/monkeypatch pattern from `test_programme_suggestions_repo.py`). FAIL.
- [ ] **Step 2 — DDL + repo.** As specified. `pytest tests/unit/test_findings_repo.py -v` → PASS; full `pytest tests/unit -q` green.
- [ ] **Step 3 — apply on TEST.** PR → (PGPASSWORD MATCH check) merge → deploy → `aws lambda invoke --function-name fieldsight-test-migrate …` → Data-API `SELECT to_regclass('findings')` non-null; `\d`-style check of the three indexes via `pg_indexes`.
- [ ] **Step 4 — commit.** `feat(db): 0010 findings table (rich extraction findings + programme-impact columns) + repo`.

**Done:** table live on TEST, repo unit-tested.

---

### Task 2 — item-writer persists findings + artifact v2

**Files:** Modify `src/lambda_item_writer.py`; extend `tests/unit/test_lambda_item_writer.py`.

**Changes (inside `write_extraction_items`, after `topics.upsert_topic` :149-155):**
1. `finding_rows = findings.insert_findings(conn, row["id"], site["id"], t.get("findings") or [])` (same txn — cascade + advisory-lock semantics inherited).
2. Extend the `collected_topics.append({...})` snapshot (:160-166) with `"findings": [{"finding_id": str(f["id"]), "observation": f["observation"], "domain": f["domain"], "severity": f["severity"], "entity_name": f["entity_name"], "entity_trade": f["entity_trade"]} for f in finding_rows]`.
3. NOTHING else moves: `match_request.emit` passes topic dicts through verbatim (match_request.py:23-45) — no emitter/wire/IAM change. `lambda_ingest`'s report path stays findings-free (reports have none) — the matcher must treat a missing `findings` key as empty (Task 3).

- [ ] **Step 1 — failing tests.** `test_writes_findings_rows_per_topic`, `test_artifact_topics_carry_finding_ids`, `test_no_findings_key_still_works` (legacy extraction JSON without `findings` — backward compat with pre-#46 extractions still in S3). FAIL.
- [ ] **Step 2 — implement.** Keep the existing safety_flags bridge write UNTOUCHED (D8 double-write is deliberate).
- [ ] **Step 3 — pass + live smoke.** PR → MATCH check → merge → deploy → re-trigger one real extraction (`aws s3 cp s3://…/extractions/<key> s3://…/extractions/<key> --metadata-directive REPLACE --region ap-southeast-2`) → Data-API: `SELECT count(*) FROM findings WHERE topic_id IN (SELECT id FROM topics WHERE source_s3_key='<key>')` > 0; fetch the emitted `match_requests/` artifact and eyeball `findings[].finding_id`.
- [ ] **Step 4 — commit.** `feat(item-writer): persist rich findings to Aurora + carry finding ids in match_requests artifact`.

**Done:** findings land in Aurora; the artifact carries them with durable uuids.

---

### Task 3 — suggestion-writer applies impacts (in-VPC hop first, so Task 4 has a live target)

**Files:** Modify `src/lambda_suggestion_writer.py`; extend `tests/unit/test_lambda_suggestion_writer.py`.

**Contract (extends the existing payload, backward compatible):**
`{"suggestions": [...], "impacts": [{finding_id, task_id, impact_severity, impact_note, impact_task_name, impact_evidence}]}` → `{"written": N, "impacts_applied": M}`. Missing/empty `impacts` behaves exactly as today; the both-empty guard (:56-58) now checks both lists before opening a connection.

- [ ] **Step 1 — failing tests.** `test_impacts_applied_via_repo`, `test_vanished_finding_counts_zero_not_error` (apply_impact→None), `test_suggestions_only_payload_unchanged`, `test_empty_both_never_opens_db`. FAIL.
- [ ] **Step 2 — implement.** Same single `with get_connection()` txn loops both lists. No template change (function already in-VPC with PG env).
- [ ] **Step 3 — pass + live smoke.** PR → MATCH check → merge → deploy → manual `aws lambda invoke --function-name fieldsight-test-suggestion-writer` with one real `finding_id` from Task 2's smoke → Data-API shows `programme_task_id`/`impact_matched_at` set.
- [ ] **Step 4 — commit.** `feat(suggestion-writer): apply programme-impact verdicts as finding-row updates`.

**Done:** in-VPC write hop accepts impacts.

---

### Task 4 — matcher impact phase (the reuse core)

**Files:** Modify `src/lambda_programme_matcher.py`; extend `tests/unit/test_lambda_programme_matcher.py`.

**Pure functions (no I/O — mirror the existing build_prompt/parse_verdict style):**
- `build_impact_prompt(topic, findings, candidates) -> str` — ONE call per topic covering ALL its embedding-surviving findings: numbered candidate list (reuse the existing candidate-line format :212-229, incl. the `.get`-with-fallback rule for bare leaves), numbered findings each showing `observation`/`domain`/`severity` (labelled "extraction-stage schedule severity — your prior")/`entity_name`/`entity_trade`. Instructions: per finding pick ONE task_id or null (null is the expected common answer — same fail-closed language as :247-254); `impact_severity` ∈ none/minor/major, default to the finding's own severity unless the task context clearly warrants deviating; one-line `note`; `confidence` 0-1. Output: strict JSON `{"impacts": [{"finding_id", "task_id", "impact_severity", "note", "confidence"}]}`.
- `parse_impact_verdicts(raw, survivor_ids_by_finding: dict[str, set], finding_severity_by_id: dict, conf_min) -> list[dict]` — per element double-gate: `task_id` non-null AND in THAT finding's own survivor set; confidence a genuine float in `[conf_min, 1.0]` (copy the NaN/upper-bound guard :283-290); invalid `impact_severity` → fall back to the finding's severity (D3); unknown `finding_id` / unparseable JSON → drop that element (fail-closed), never the batch.

**Adapter changes (`_process_topic` :320-417 → returns `(suggestion|None, impacts: list)`):**
- After the existing suggestion flow, gather `topic.get("findings") or []` (report-path artifacts have none → impact phase no-ops).
- Extend the ONE existing embed batch (:341-344): `texts = [topic_text] + candidate_names + finding_observations` — same `dashscope_utils.embed` call (it self-batches ≤10 per request, dashscope_utils.py:109-123; no new provider, no extra round-trip logic).
- Per finding: `rank_by_embedding(finding_vec, cands, task_vecs, SIM_MAX_DIST, TOP_K)` reusing the candidate vectors already computed. Findings with zero survivors are excluded from the Claude call (fail-closed skip).
- If ≥1 finding survives: ONE `claude_utils.call_claude(build_impact_prompt(...), max_tokens=min(512 + 256*n_findings, 2000))` → `parse_impact_verdicts` → for each accepted verdict build the writer impact dict: `impact_task_name` = matched candidate's `name`; `impact_evidence` = `{"cosine_survivor_ids": …, "llm_confidence": …, "finding_severity": …, "programme_updated_at": programme_doc.get("updated_at")}`.
- `lambda_handler`: collect impacts alongside suggestions; ONE writer invoke with both keys, only after every record processed (preserve :437-449 fail-closed + FunctionError guard). `dry_run` returns `{"suggestions": …, "impacts": …, "dry_run": true}`.
- Reuse `CONF_MIN` for impacts (one knob; Task 5 calibration may split it into `IMPACT_CONF_MIN` if live precision diverges). No template.yaml change needed (env/IAM/timeout already cover this — note: +1 Claude call/topic within the 300s budget; if a many-topic artifact times out in the smoke, raise Timeout to 600 in this PR).

- [ ] **Step 1 — failing tests (pure).** `test_impact_prompt_lists_findings_with_severity_prior`, `test_impact_verdict_rejects_nonsurvivor_for_that_finding` (finding A's pick allowed only from A's survivors, not B's), `test_impact_verdict_bad_severity_falls_back_to_finding_severity`, `test_impact_verdict_confidence_gate_nan_and_range`, `test_impact_verdict_unknown_finding_id_dropped`. FAIL.
- [ ] **Step 2 — pure core pass.**
- [ ] **Step 3 — adapter tests.** Extend `_clean_match_setup` (:76-117) with findings in the artifact: `test_handler_collects_impacts_and_invokes_writer_once`, `test_report_artifact_without_findings_skips_impact_phase`, `test_zero_survivor_finding_excluded_from_claude_call`, `test_dry_run_returns_impacts_without_invoke`. PASS.
- [ ] **Step 4 — live smoke (dry_run first).** PR → MATCH check → merge → deploy → re-trigger Task 2's extraction → matcher fires on the artifact; check CloudWatch (`aws logs tail /aws/lambda/fieldsight-test-programme-matcher --since 10m --region ap-southeast-2`) for the impact phase; then Data-API: finding rows show `programme_task_id`/`impact_severity`/`impact_task_name`. (Site must have a `programmes/{site_id}/programme.json` — same precondition as suggestions.)
- [ ] **Step 5 — commit.** `feat(matcher): programme-impact phase — finding→task link via shared candidate gate + per-finding double-gate (fail-closed)`.

**Done:** end-to-end extraction → findings → impact links, with no manual step.

---

### Task 5 — read path (`/live-items` carries findings) + pilot/calibration + docs

**Files:** Modify `src/repositories/topics.py` (`list_topics_for_date`), extend `tests/unit/test_topics_repo.py` + `tests/unit/test_lambda_org_api.py`; docs.

- [ ] **Step 1 — failing tests.** `test_list_topics_attaches_findings_batched` (third child query, `ANY(%s)` batch — NO N+1), `test_live_items_payload_includes_findings_with_impact` (org-api passthrough). FAIL.
- [ ] **Step 2 — implement.** Third batched query via `findings.list_for_topics`; attach raw rows as `t["findings"]` (raw columns, matching how action_items/safety are exposed; the spec §4 nested `programme_impact: {task_id, severity, note}` shape is a UI-phase mapping concern — note it in the docstring). `/live-items` needs zero route changes (:706-716). PASS.
- [ ] **Step 3 — live curl smoke.** Merge (MATCH check) → deploy → `GET /api/org/live-items?date=<today>` as Ben Lin admin → topics carry `findings[]` with `entity_name`/`programme_task_id`/`impact_severity`/`impact_task_name`. Verify it BEFORE the nightly cron wipes the day's extraction topics (D5).
- [ ] **Step 4 — calibration pass.** Re-trigger 3-5 real extractions across a site with a real programme; `dry_run` the matcher over their artifacts; record per-finding precision (correct task? sensible severity?). Tune `SIM_MAX_DIST`/`CONF_MIN` only if impact precision clearly diverges from suggestion precision (then introduce `IMPACT_CONF_MIN` env in template.yaml rather than moving the shared knob).
- [ ] **Step 5 — docs.** Update `docs/superpowers/specs/2026-07-13-unified-extraction-labeling-design.md` §4 with a correction note (programme_impact is Aurora-side enrichment, NEVER written into the S3 extraction JSON — retrigger loop, D6; finding field is `observation` not `text`); CLAUDE.md/runbook chain note (findings table, artifact v2, same-day-only until Task 4, D8 double-write); breadcrumb in the file for the Task 4 (authority flip) planner: stop the supersession → findings/impacts become persistent, retire the safety_flags bridge, revisit report-path artifacts.
- [ ] **Step 6 — commit.** `feat(org-api): /live-items exposes findings + programme-impact; calibration + docs`.

**Done:** the consumer contract for the eventual dashboard exists and is verified live.

---

## Risks

1. **§9 double-table trap (the one this plan is shaped around):** held off by construction — impact = columns on `findings`, zero new link tables. Reviewers must reject any drift toward a `programme_impact_links` table or impact rows in `programme_progress_suggestions`.
2. **Provisional data / authority-flip interaction (D5):** impact links are wiped by the 05:00 nightly supersession until Task 4. Same-day-only value is ACCEPTED; anyone tempted to "persist" them here re-opens risk 1. Smoke tests must run same-day.
3. **Severity-mapping ambiguity (D3):** two severities (extraction-time vs match-time) could confuse consumers — both stored, prompt seeds the prior, fallback on invalid; calibration (Task 5 Step 4) validates the LLM actually uses the prior sensibly.
4. **Matcher internet dependency:** non-VPC matcher needs DashScope + Claude; an outage fails closed (S3 retry, nothing half-written) — same posture as suggestions, no new exposure. +1 Claude call/topic squeezes the 300s timeout on many-topic artifacts — watch the smoke, bump to 600 if needed.
5. **Rotation trap:** three of five tasks deploy in-VPC lambdas — the DbSecret==PGPASSWORD MATCH check is non-negotiable before every merge.
6. **Backward-compat artifacts:** pre-existing `match_requests/` objects and report-path artifacts have no `findings` key; the matcher must `.get(..., [])` (tested, Task 4 Step 3).

## Self-review (author, 2026-07-13)

- **Spec coverage:** §4 `programme_impact {task_id, severity, note}` → 0010 columns + D3/D6 (with two documented spec corrections); §5 findings columns → 0010 verbatim plus display/evidence extras justified by the 0008 precedent; §8 Task 3 scope only (no authority change, no correction loop, no UI); §9 → §1 crux + risk 1.
- **Signatures cross-checked against code this session:** extractor findings shape (`observation`, `entity{name,trade}` — lambda_extract_session.py:157-166), item-writer collect/emit points (:147-177), matcher `_process_topic`/`parse_verdict`/embed batch (:320-417, :266-292, dashscope_utils.py:109-123), writer payload guard (:53-68), topics batched-children pattern (topics.py:110-163), org-api `/live-items` (:706-716), template IAM already sufficient (:906-912, :1024-1030, :1142-1148), wire-s3-events already wired (:129-137).
- **One cross-task coupling to watch:** the artifact `findings[].finding_id` field name (Task 2 produces, Task 4 consumes) and the writer `impacts` contract (Task 3 produces the interface, Task 4 invokes) — both spelled identically in all three tasks.
