# Programme Item Feedback — Design (2026-07-12)

**Feature:** Daily items feed back into Programme progress, with human confirmation
(backlog: 跨周话题完成识别 + 人工确认).
**Status:** **APPROVED** by product owner 2026-07-12 ("按推荐"). Ready for `writing-plans`. No code yet. See §8 for resolved decisions.
**Scope of infra:** account 509194952652 / ap-southeast-2 / `fieldsight-test` stack ONLY.

---

## 1. Goal & v1 scope

Topics extracted from daily site recordings (Aurora item store, `topics` +
`action_items`, `src/migrations/0003_dashboard_readmodel.sql`) are matched to the
corresponding scheduled Programme task (the per-site S3 JSON blob,
`src/repositories/programme.py:19-51`) and produce a **suggested** progress/status
update for that task. An admin/gm/pm reviews each suggestion and confirms or
rejects it. Confirmation writes the change to `programme.json` through the existing
whole-document PUT path. Nothing is ever auto-applied.

In scope (v1):

- Match one topic → at most one programme task, fail-closed (below threshold → no
  suggestion; a missed match is acceptable, a wrong match is not).
- Suggest a status transition (`in_progress` / `completed` / `blocked` / `delayed`)
  and, when the recording is explicit about it, a `progress_pct`.
- Persist suggestions in a new Aurora table (migration **0008**), with full audit
  (who decided, when, what was applied) and idempotency.
- Review + confirm/reject API on `lambda_org_api` and a review-queue UI block on the
  Programme page.
- "Cross-week completion recognition" falls out of the daily mechanism: a task
  spanning weeks accumulates one suggestion per relevant day; the reviewer sees the
  series and confirms when real. No separate cross-week aggregator in v1.

Out of scope (v1): auto-apply of any kind; promoting Programme to a SQL table;
matching `safety_observations`; multi-task suggestions from one topic; editing
programme dates/dependencies from a suggestion.

Prerequisites already live: persistent item store (Phase 4a/4b) and the semantic
engine (DashScope `text-embedding-v4` @1024, `src/dashscope_utils.py:40-41`; two-hop
non-VPC↔in-VPC pattern, `src/template.yaml:592-630`).

---

## 2. Architecture overview + data flow

Ground truth (verified 2026-07-12):

| Fact | Where |
|---|---|
| Programme = per-site S3 JSON `programmes/{site_id}/programme.json`, site_id = org `sites.id` UUID; plain get/put | `src/repositories/programme.py:19-51` |
| Programme API = whole-doc GET/PUT `/api/org/programme?site=<id\|slug>`; PUT gated admin/gm/pm; ACL via `_allowed_site_ids` | `src/lambda_org_api.py:187-191, 671-703, 706-732` |
| Task shape: `task_id` string code ('T-001'), `wbs`, `parent_id`, `name`, `start`/`end` ISO date range, `progress_pct`, `status` (not_started/in_progress/completed/blocked/delayed/group), `depends_on`, `assignees` = user folder-name strings | `ui/scripts/mock/programme.fixture.js:55-112` |
| Per-task CRUD in the UI is mock-only ("NOT YET implemented server-side") | `ui/scripts/api/programme.js:4, 106-115` |
| Item store: `topics` (id uuid, site_id uuid FK, user_id uuid FK nullable, source_s3_key, report_date date, title, summary, category), children `action_items` / `safety_observations` | `src/migrations/0003_dashboard_readmodel.sql:1-35` |
| Live-item chain: `transcripts/*.json` → extract-session (non-VPC, Claude) → `extractions/*.json` → item-writer (in-VPC, Aurora) | `scripts/wire-s3-events.sh:100-121`, `src/lambda_item_writer.py` |
| Report chain: `reports/*daily_report.json` → embed-report (non-VPC, DashScope) → `embeddings/*vectors.json` → ingest (in-VPC) | `scripts/wire-s3-events.sh:69-99`, `src/lambda_embed_report.py:10-27` |
| Identity bridge: `users.get_by_folder_name(conn, company_id, folder_name)`; programme `assignees` are folder_name strings | `src/repositories/users.py:38-42` |
| Two-hop precedent: AskAgent (non-VPC: DashScope + Claude) invokes RagSearch (in-VPC: Aurora) via Lambda invoke | `src/template.yaml:592-630` |
| In-VPC lambdas have S3 gateway-endpoint access but **zero other egress** (no DashScope, no Claude, no Lambda API) | `src/repositories/programme.py:2-4`, CLAUDE.md BUG-36 |
| Fail-closed distance-threshold precedent: `_NO_LEX_MAX_DIST = 0.55` cosine distance | `src/lambda_ask_agent.py:532` |
| Topics are deleted + re-inserted on reprocess, and live topics are bulk-deleted when the nightly report supersedes them | `src/repositories/topics.py:60-101` |
| Migrations 0001–0007 applied; next is **0008**, run via `fieldsight-test-migrate` | `src/migrations/`, `src/template.yaml:654` |

**CRITICAL non-reuse:** the fixture's `linked_action_items[].topic_id`
(`ui/scripts/mock/programme.fixture.js:65-68`) is the LEGACY report-JSON sequential
integer served by the deprecated `src/lambda_fieldsight_api.py`/DynamoDB path. This
feature keys on Aurora `topics.id` (uuid) exclusively and never touches the legacy
path (dashboard-first: Aurora item store is source of truth).

### Data flow (new pieces in caps)

```
extractions/{user}/{date}/{sess}.json ──S3──▶ item-writer (in-VPC)
                                               │ writes topics to Aurora (existing)
                                               │ EMITS match_requests/{site_id}/{date}/{h}.json  (S3, gateway endpoint)
reports/…/daily_report.json ─▶ embed-report ─▶ embeddings/…vectors.json ──S3──▶ ingest (in-VPC)
                                               │ writes report topics (existing)
                                               │ EMITS the same match_requests artifact
                                               ▼
                    match_requests/*.json ──S3 event──▶ PROGRAMME-MATCHER (non-VPC, NEW)
                                               │ reads programmes/{site_id}/programme.json (S3)
                                               │ hard gates → DashScope embed rank → Claude classify → fail-closed accept
                                               │ Lambda-invokes ▼   (AskAgent→RagSearch precedent)
                                       SUGGESTION-WRITER (in-VPC, NEW)
                                               │ inserts pending rows into programme_progress_suggestions (0008)
                                               ▼
        lambda_org_api (existing): GET /programme/suggestions · POST …/{id}/confirm|reject
                                               │ confirm → read programme.json → apply → write_programme (whole-doc)
                                               ▼
                              Programme page UI: review queue + task badges
```

Why the `match_requests/` S3 hop: the matcher needs internet (DashScope + Claude) so
it must be non-VPC, but `extractions/` already triggers item-writer and S3 forbids a
second overlapping notification on the same prefix/suffix
(`scripts/wire-s3-events.sh:111-121` keeps all prefixes disjoint by design). An
in-VPC lambda's only egress is S3 (BUG-36), so item-writer/ingest hand off by
writing a small artifact — exactly the embed-report → sidecar → ingest pattern in
reverse direction.

---

## 3. Decisions

### D1 — Structural fork for Programme

**(a) Keep S3 blob; new Aurora table holds suggestions + a minimal task snapshot.**
Cost: one migration, one repo module. Programme stays whole-doc; the matcher reads
the doc straight from S3 (tens of tasks — trivially cheap; the matcher is non-VPC
and item-writer-side lambdas reach S3 via the gateway endpoint). No FK to tasks is
possible (task_id is a string in a JSON file), so integrity is enforced at read/
confirm time (task vanished from doc → suggestion marked `stale`).
Risk: task snapshot in the suggestion row can drift from the doc (rename/date edit)
— acceptable: snapshot is provenance ("what the matcher saw"), the confirm handler
always re-reads the live doc before writing.

**(b) Promote Programme tasks into an Aurora relational table.** Enables SQL joins,
real FKs, per-task site_id — but requires first building the real per-task CRUD
backend that today is mock-only (`ui/scripts/api/programme.js:4`), a data migration
for existing docs, dual-write or cut-over risk on the authoring path, and rework of
the whole-doc GET/PUT contract the UI already uses. Weeks of prerequisite work that
delivers zero user-visible feedback value.

**(c) Middle path = (a) stated precisely:** S3 stays the *authoring source of
truth*; Aurora holds the *link/suggestion* rows plus just enough denormalized task
metadata (`task_id`, `task_name`, window, pre-change status/progress) to query and
audit without opening the doc.

**Recommendation: (c).** v1 needs to *store links and suggestions*, not to *query
tasks relationally*. Promotion (b) remains the right long-term move if/when per-task
CRUD becomes a product requirement — this design does not block it (the suggestion
table keys on `(site_id, task_id)` which survives a future promotion unchanged).

### D2 — Matching strategy (topic → task)

Candidate space is tiny (tens of tasks per site), precision is the hard requirement
("never mis-attribute; worst case is unmatched"), and the output must include a
*suggested status/progress* — which is a language-understanding output, not a
similarity score.

**Pure-embedding cosine ranking.** Deterministic, cheap, reuses DashScope directly.
But (i) an absolute cosine threshold is a poor "no-match" detector — this repo
already learned that pure semantic ranking is unstable and had to add lexical
hybrid + a 0.55 distance cutoff (`src/lambda_ask_agent.py:532,602-607`); (ii) short
task names ("Cladding", "Steel frame") vs conversational topic titles give noisy
absolute similarities; (iii) cosine cannot produce `suggested_status`/`progress` at
all — you'd bolt on brittle keyword rules ("finished", "完成").

**LLM classifier ("which of these N tasks, or none?").** With N ≤ ~20 the whole
programme fits in one small prompt. An explicit **"none"** option is exactly the
fail-closed discriminator we need, and the same call yields status/progress + an
evidence quote. Precedent: extract-session already runs non-VPC Claude with strict
JSON output (`src/lambda_extract_session.py:295-299`). Cost ≈ one 1–2K-token call
per topic — negligible at current volumes. Weakness: nondeterminism, so it must not
be the *only* gate.

**Recommendation: layered funnel, double-gated (embedding floor AND LLM choice must
agree), fail-closed at every step.** Deterministic hard gates (site, date-window,
status) shrink candidates; embedding cosine (reused engine, no new provider) gives a
deterministic relevance floor and ordering; one Claude call picks the task **or
none** and drafts the suggestion; a suggestion is emitted only when both layers
agree. Assignee↔user overlap via folder_name is a confidence **booster**, never a
hard gate (`topics.user_id` is nullable, and cross-crew work is real). Full
algorithm in §5.

### D3 — Suggestion/link data model + confirmation flow

New table `programme_progress_suggestions` (DDL in §4). Design points:

- **Survival across topic churn.** Topics are deleted + re-inserted on reprocess and
  bulk-deleted on nightly supersession (`src/repositories/topics.py:60-101`), so a
  plain `ON DELETE CASCADE` FK would silently destroy confirmed/rejected decisions.
  Instead: `topic_id … ON DELETE SET NULL` (the `report_chunks.topic_id` precedent,
  `src/migrations/0004_report_chunks.sql:6`) + denormalized topic snapshot columns
  (`topic_title`, `topic_summary`, `report_date`, `source_s3_key`).
- **"Never re-suggest" = a stable dedupe key**, not the volatile topic uuid:
  `dedupe_key = sha256(site_id | task_id | report_date | normalize(topic_title))`
  where `normalize` = lowercase + collapse internal whitespace + strip, UNIQUE. Re-processing or supersession re-runs the matcher; `ON CONFLICT` keeps the
  existing row (re-pointing `topic_id` at the fresh uuid only while still
  `pending`). A `rejected` row therefore blocks that suggestion forever.
- **States:** `pending → confirmed | rejected | stale` (`stale` = target task no
  longer in the doc, or the source topic was superseded before review).
- **Confirm flow** (org-api, mirrors `put_programme` gates at
  `src/lambda_org_api.py:715-732`): role admin/gm/pm AND site ACL via
  `_resolve_site_param`. Handler re-reads `programme.json`, locates the task by
  `task_id` (missing → mark `stale`, 409), applies the reviewer-editable
  status/progress (guard: never lower `progress_pct` unless the reviewer explicitly
  typed the lower value), calls `write_programme` (the existing whole-doc write),
  then marks the row `confirmed` with `decided_by/decided_at/applied_*`. The
  **only** code path that writes programme.json remains the role-gated org-api —
  the matcher and writers have no write access to `programmes/*`.
- **Reject flow:** state → `rejected`, `decided_by/at` stamped; dedupe key
  guarantees no re-suggest.
- **Idempotent + audited:** insertion idempotent via dedupe key; every row carries
  `match_evidence` jsonb (cosine, llm rationale + model, assignee overlap) and the
  full decision trail. Never auto-apply, by construction.
- Known v1 concession: confirm's read-modify-write of the whole doc can race a
  concurrent UI whole-doc PUT (last-write-wins — the doc's existing semantics).
  Optimistic locking on `updated_at` is listed in §8.

### D4 — When matching runs

**On item-writer/ingest completion (event-driven), with a manual batch mode.**
After its transaction commits, item-writer (and ingest, for report-sourced topics)
writes `match_requests/{site_id}/{report_date}/{sha256(source_s3_key)[:16]}.json`
containing the inserted topic ids + snapshots. S3 event (new disjoint prefix in
`wire-s3-events.sh`) triggers the non-VPC matcher; the matcher Lambda-invokes the
in-VPC suggestion-writer (AskAgent→RagSearch precedent, `src/template.yaml:614,630`).

Why not batch/cron as primary: dashboard-first means same-day feedback; the S3-event
chain is the established pipeline idiom (four chains already live in
`wire-s3-events.sh`); S3 event retry gives free at-least-once delivery, and the
dedupe key makes retries harmless. Why not "on-demand from the UI": adds latency and
a synchronous LLM call to a page load, and loses the audit-friendly "suggestions
exist before anyone looks" property.
Batch mode retained: the matcher accepts a manual invoke payload
`{"backfill": {"site": …, "date_from": …, "date_to": …}}` (RagSearch
"manual/invoke only" precedent) for backfill and threshold recalibration.
Deterministic artifact key per source_s3_key = idempotent overwrite on reprocess.

---

## 4. New data model — migration 0008 (DDL sketch)

`src/migrations/0008_programme_suggestions.sql` (style per `0006_observations.sql`;
applied via `fieldsight-test-migrate`):

```sql
-- 0008: programme progress suggestions (daily items -> programme feedback,
-- human-confirmed; spec 2026-07-12)
CREATE TABLE programme_progress_suggestions (
  id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  site_id              uuid NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  task_id              text NOT NULL,   -- programme task code ('T-004'); lives in S3 doc, no FK possible
  -- topic linkage: SET NULL + snapshot (0004 report_chunks precedent) because
  -- topics are deleted/re-inserted on reprocess and superseded nightly
  topic_id             uuid REFERENCES topics(id) ON DELETE SET NULL,
  topic_title          text NOT NULL,
  topic_summary        text,
  topic_user_id        uuid REFERENCES users(id),
  report_date          date NOT NULL,
  source_s3_key        text NOT NULL,   -- extraction/report key that produced the topic
  -- task snapshot at match time (provenance; confirm always re-reads the live doc)
  task_name            text NOT NULL,
  task_status_before   text,
  task_progress_before smallint,
  -- the suggestion
  suggested_status     text CHECK (suggested_status IN
                         ('in_progress','completed','blocked','delayed')),
  suggested_progress   smallint CHECK (suggested_progress BETWEEN 0 AND 100),
  confidence           real NOT NULL,
  match_evidence       jsonb NOT NULL DEFAULT '{}'::jsonb,
  -- never re-suggest: stable across topic uuid churn
  dedupe_key           text NOT NULL,   -- sha256(site|task|report_date|norm(title))
  -- decision trail
  state                text NOT NULL DEFAULT 'pending'
                       CHECK (state IN ('pending','confirmed','rejected','stale')),
  decided_by           uuid REFERENCES users(id),
  decided_at           timestamptz,
  applied_status       text,
  applied_progress     smallint,
  created_at           timestamptz NOT NULL DEFAULT now(),
  updated_at           timestamptz NOT NULL DEFAULT now(),
  CHECK (suggested_status IS NOT NULL OR suggested_progress IS NOT NULL)
);
CREATE UNIQUE INDEX uq_pps_dedupe ON programme_progress_suggestions (dedupe_key);
CREATE INDEX idx_pps_site_state ON programme_progress_suggestions (site_id, state, report_date);
CREATE INDEX idx_pps_topic ON programme_progress_suggestions (topic_id);
```

Insert semantics (repository `src/repositories/programme_suggestions.py`, new):
`INSERT … ON CONFLICT (dedupe_key) DO UPDATE SET topic_id = EXCLUDED.topic_id,
updated_at = now() WHERE programme_progress_suggestions.state = 'pending'` — decided
rows are immutable to the pipeline; pending rows re-link to the freshest topic uuid.

No task-embedding table in v1: task names are re-embedded per matcher run (≤ tens of
short strings, one DashScope batch call) — caching is a later optimization, not a
schema commitment.

---

## 5. Matching algorithm (concrete, fail-closed)

Inputs per match request: site_id, report_date, topic snapshots (title, summary,
user_id, action_items), plus `programmes/{site_id}/programme.json` read from S3.
Tunables are env vars on the matcher with these defaults:

| Knob | Default | Rationale |
|---|---|---|
| `LEAD_DAYS` / `LAG_DAYS` | 7 / 14 | work runs early/late vs plan; delayed tasks keep receiving talk after `end` |
| `SIM_MAX_DIST` | 0.55 cosine distance | mirrors `_NO_LEX_MAX_DIST` (`src/lambda_ask_agent.py:532`) |
| `TOP_K` | 5 | candidates passed to the LLM |
| `CONF_MIN` | 0.70 | minimum LLM confidence |

Steps per topic:

1. **Site gate (structural).** Only that site's doc is loaded; topic.site_id and the
   doc share the same `sites.id` uuid by construction.
2. **Candidate gate (deterministic).** Keep tasks where `status NOT IN
   ('completed','group')` AND `start − LEAD_DAYS ≤ report_date ≤ end + LAG_DAYS`.
   Zero candidates → **no suggestion**, log, done.
3. **Embed + rank (reused engine).** One `dashscope_utils.embed()` batch: the topic
   text (`title + '\n' + summary`) and each candidate's text (`name` + parent WBS
   group name for context). Rank by cosine distance; drop candidates with distance
   > `SIM_MAX_DIST`; keep top `TOP_K`. Zero survivors → **no suggestion**.
4. **LLM discriminate (one Claude call, `claude_utils` pattern).** Prompt: topic
   title/summary/action_items/date/speaker + the ≤K candidates (task_id, name, wbs,
   window, status, progress_pct, assignees) → strict JSON
   `{task_id | null, confidence, suggested_status | null,
   suggested_progress | null, evidence}` with an explicit instruction that `null`
   is the correct answer when no task clearly matches, and `suggested_progress`
   only when a figure/completion is explicitly stated in the topic.
5. **Double-gate accept.** Emit a suggestion only if: LLM returned a `task_id` ∈
   step-3 survivors (an LLM pick that failed the embedding floor is discarded) AND
   `confidence ≥ CONF_MIN` AND the suggestion is a real change (differs from the
   task's current status/progress; never a progress decrease). Exactly **one
   suggestion per topic** (the LLM's single pick).
6. **Confidence boost (recorded, not gating).** If the topic's user_id resolves via
   folder_name to a name in `task.assignees`, note `assignee_overlap: true` in
   `match_evidence` and surface it in the UI.
7. **Fail-closed error handling.** Missing/empty programme, DashScope failure after
   retries (`dashscope_utils` raises rather than zero-vector,
   `src/dashscope_utils.py:14-18`), Claude parse failure, writer failure → zero
   suggestions written for that request; the error propagates so the S3 event
   retries. Dedupe key makes retries and replays idempotent.

Cost envelope: ~10 topics/site/day × (1 embed batch + 1 small Claude call) —
immaterial against the existing extract-session per-session Claude call.

---

## 6. API surface, confirmation flow, UI touch-points

New routes in `lambda_org_api.py` (dispatch style of
`src/lambda_org_api.py:168-193`; ACL via `_resolve_site_param`; all three gated to
admin/gm/pm — same gate as `put_programme:721-722`, since this is a manager review
queue):

| Route | Method | Behavior |
|---|---|---|
| `/api/org/programme/suggestions?site=<id\|slug>&state=pending` | GET | list suggestions for the site (default state=pending; `all` for history) |
| `/api/org/programme/suggestions/{id}/confirm` | POST | body `{status?, progress_pct?}` (reviewer overrides, defaults = suggested values). Re-read doc → task present? else mark `stale` + 409 → apply (no silent progress regression) → `write_programme` → row `confirmed` + `applied_*` + `decided_by/at` |
| `/api/org/programme/suggestions/{id}/reject` | POST | row `rejected` + `decided_by/at`; dedupe key blocks any future re-suggest |

Both POSTs verify the suggestion's `site_id` is in the caller's
`_allowed_site_ids` and its state is `pending` (else 409 — no double-decide).

UI touch-points (mock-first per `ui/CLAUDE.md` conventions; live wiring only after
the backend is deployed — Sprint 5 lesson "don't ship write actions ahead of their
backend"):

1. **Review queue** — Programme page right pane block "Suggested updates (n)": per
   card, topic title/summary + date + speaker, target task, the delta
   (`progress 12% → 60%`, `status → completed`), evidence quote, assignee-overlap
   badge, actions Confirm / Adjust… / Reject.
2. **Task-row badge** — pending-suggestion count on Gantt/board task rows.
3. **Provenance** — task editor drawer lists this task's confirmed suggestions
   (date, topic, applied delta, decided_by) from the Aurora table — replacing, and
   never reusing, the legacy `linked_action_items` fixture wiring.

---

## 7. Phased implementation plan

Each task is independently reviewable, TDD-first, and ends in a testable
deliverable. Merges to `develop` auto-deploy to the test stack
(`.github/workflows/deploy.yml`). Suitable for fresh subagent implementers + Fable
final review; the user merges PRs.

| # | Task | Deliverable / test gate |
|---|---|---|
| 1 | **Migration 0008 + repository.** `0008_programme_suggestions.sql` + `repositories/programme_suggestions.py` (insert-with-dedupe, list_for_site, get, decide, mark_stale) | unit tests (`tests/unit/test_programme_suggestions_repo.py`, style of `test_topics_repo.py`); migration applies cleanly via `fieldsight-test-migrate` |
| 2 | **Suggestion-writer lambda (in-VPC, new).** Payload → repo inserts; mirrors ItemWriterFunction's VpcConfig/Layers/PG-env verbatim (`src/template.yaml:963-1014`) | unit tests w/ mocked conn (style of `test_lambda_item_writer.py`); manual invoke on test stack inserts rows |
| 3 | **Matcher lambda (non-VPC, new).** Pure-function core (candidate gate, ranking, prompt build/parse, double-gate accept) + DashScope/Claude adapters + `template.yaml` + `wire-s3-events.sh` entry (`match_requests/` prefix) + LambdaInvokePolicy on the writer | unit tests with stubbed HTTP (style of `test_lambda_embed_report.py` / `test_lambda_extract_session.py`); `dry_run` invoke returns would-be suggestions without writing |
| 4 | **Emit match_requests.** item-writer + ingest write the artifact post-commit; stale-marking of pending suggestions whose live topics were superseded | unit tests; E2E on test stack: drop an extraction JSON → pending suggestion row appears |
| 5 | **Org-api endpoints.** GET/confirm/reject + programme write-back + guards | tests in `test_lambda_org_api.py`; curl-able confirm on test stack changes `programme.json` and stamps the row |
| 6 | **UI review queue.** Fixture + composites + live branch behind `useMocks` | preview HTML demo; `node --check` on all touched JS; live wiring verified against test stack |
| 7 | **Calibration + smoke + docs.** Backfill-mode replay over existing test-site data; tune `SIM_MAX_DIST`/`CONF_MIN` from observed precision; runbook update | recorded precision snapshot; E2E smoke checklist green |

Tasks 1→2→3 are sequential; 4 and 5 are parallel after 1–3; 6 after 5; 7 last.

---

## 8. Open questions — RESOLVED (product owner, 2026-07-12 "按推荐")

| # | Question | Resolution |
|---|---|---|
| 1 | One suggestion per topic? | **Yes — one best task per topic in v1** (topic-level matching; action-level later). |
| 2 | Progress-number policy | **Agreed** — status always suggestable; numeric `progress_pct` only when explicitly stated; reviewer can edit either before confirm. |
| 3 | Provenance inside programme.json? | **Aurora-only in v1** (recommended default applied — doc schema untouched). |
| 4 | Worker visibility of confirmed provenance? | **No in v1** (recommended default applied — review queue admin/gm/pm only). |
| 5 | Concurrency | **Optimistic locking in v1** — confirm re-reads the doc and rejects with 409 if `updated_at` changed since the suggestion was made (product owner chose optimistic over last-write-wins). |
| 6 | Initial thresholds | **Start conservative**, tune upward during the Task-7 backfill pilot. |

Refinement locked with #5's spirit: the candidate date window uses `LEAD_DAYS=7 / LAG_DAYS=14` (task active in `[start−7d, end+14d]`) rather than the simpler `[report_date−7d, report_date]` first floated — it additionally captures delayed tasks still discussed after their planned end. See §5.

Items 3, 4, 6 were resolved by applying the recommended default; flag if any should differ before `writing-plans` locks them into tasks.

---

### Original open questions (for reference)

1. **One suggestion per topic.** v1 emits only the single best task per topic. A
   day that touches two tasks in one topic yields at most one suggestion. OK for
   v1, or must a topic be able to suggest to N tasks (adds review noise)?
2. **Progress-number policy.** Recommended: always allowed to suggest a *status*;
   suggest a numeric `progress_pct` only when explicitly stated in the recording;
   the reviewer can edit either value before confirming. Agree?
3. **Provenance in the document.** Should a confirmed suggestion also stamp a
   provenance entry *inside* `programme.json` (visible in exports), or is
   Aurora-side provenance (queryable, shown in the task drawer) enough for v1?
   (Recommended: Aurora-only in v1 — keeps the doc schema untouched.)
4. **Visibility.** Review queue is admin/gm/pm with site ACL (pm sees only
   membership sites). Should workers see confirmed provenance on their tasks?
5. **Concurrency hardening.** Accept last-write-wins on the whole-doc write during
   confirm for v1 (existing PUT semantics), with optimistic locking on
   `updated_at` as a fast-follow?
6. **Initial thresholds.** Start conservative (fewer suggestions, near-zero wrong
   links) and tune upward during the pilot via Task 7's backfill replays?
