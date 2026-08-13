# Implementation plan — user deletes recordings; everything derived disappears

**Date:** 2026-08-14 · **Spec:** `docs/superpowers/specs/2026-08-14-user-deletes-a-recording.md`
**Verdict of the adversarial review: GO WITH CHANGES.** The core design decision — reuse
`redactions` as a soft, reversible, audited tombstone with one choke point — survives. The
spec as literally written does NOT: per-topic redaction alone is resurrected by the nightly
pipeline, the read side today *flags* rather than *filters*, search relies on an async
de-index whose failure is swallowed, four org-api endpoints serve the deleted recording's
raw media/transcripts untouched, and the flag-off rollback semantic is a second incident.
This plan IS the spec plus the required changes; where they disagree, this plan wins.
The changes are recorded in §0 so nobody re-derives the spec's original wording.
**Branch:** `feat/user-deletes-recording` off `origin/develop`.
**Prod impact:** phases marked below. The endpoint is gated by `ENABLE_USER_DELETION`
(off by default → endpoint refuses); the read filters are **always on** and are a no-op
until the first `scope='deleted'` row exists, so merging read-filter phases changes prod
behaviour only in the presence of data this feature itself writes.
**TEST impact:** same shape as prod; the flag will be turned on in TEST first.

Run tests exactly as the repo does:

```
export UV_LINK_MODE=copy
export AWS_ACCESS_KEY_ID=x AWS_SECRET_ACCESS_KEY=x AWS_DEFAULT_REGION=ap-southeast-2
uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit -q
```

Strict TDD: every phase writes its RED test first and watches it fail before
implementation. After any fix that pins a writer↔reader agreement, **revert the writer's
half and confirm the test goes red** (this repo has already lost a fix and its tests to a
whole-file rewrite with CI green throughout — use anchored Edits, never whole-file writes).

---

## 0. Decision record — what the review changed and why (do not re-litigate without new data)

### 0.1 The tombstone must be keyed on the SOURCE, not only on today's topic ids

The pipeline deletes and re-creates topic rows as a matter of routine:
`lambda_ingest.py:552` (`delete_topics_for_source_prefix` on nightly authority-flip),
`lambda_item_writer.py` re-runs, extraction retries, and redrives. A redaction whose
`target_id` is a topic uuid dies the moment that uuid is superseded — the new rows carry
new ids and are unredacted, and the nightly `lambda_report_generator` will happily
regenerate `daily_report.json` from the transcripts that (by explicit requirement) are
still on S3. **Per-topic redaction alone means the deleted content reappears within 24
hours.** Migration 0022 even documents this ("the topic can be superseded by nightly
re-extraction and the tombstone must outlive that") — the existing mechanism outlives the
topic but has nothing to re-attach to.

Rule as implemented: one delete action writes TWO layers.

- **Source-level tombstone** (the durable one): `target_type='recording'`,
  `target_id = recordings.id`, and a new `target_key` text column holding the extraction
  source prefix `extractions/{folder}/{date}/{session_base}` (derived via
  `session_scope`'s single key parse — one recording == one session_base == one distinct
  `topics.source_s3_key`, see `src/session_scope.py` module docstring). This is what the
  write-side guard (§0.5) and the orphan-chunk filter (§0.4) consult.
- **Per-topic redactions** (`scope='deleted'`, one per currently-derived topic): these
  feed the existing per-topic read machinery (`list_active_for_topics`,
  `company_excluded_topic_ids`) unchanged, and are **re-stamped by the write-side guard**
  whenever the pipeline re-creates topics under a tombstoned source.

`redactions.target_id` is `uuid NOT NULL` (0022_redactions.sql:16) — it cannot hold an S3
key, hence the new column. `recordings` rows can be absent (RealPTT-era captures,
pre-migration-0009 days): for those the endpoint accepts a session reference
(`{folder, date, session_base}`) and writes the tombstone with a fresh uuid target_id and
`target_type='recording'`, `target_key` set — target_key is the load-bearing field.

### 0.2 The feature flag is NOT the rollback; the batch revert is

Spec §7.1 said flag-off makes "the read filters no-op, which un-hides everything in one
deploy". That is not a rollback, it is a second trust incident: content four customers
were told is gone reappears for all of them because an operator toggled a flag — or
because one segment of the flag's three-segment wiring (repo-var → workflow → template →
env) silently went missing, which in this repo produces defaults with zero errors
(fieldsight-unwired-toggle-trap). Rule as implemented:

- `ENABLE_USER_DELETION` gates **only the write endpoint** (POST refuses when off).
- The read filters are **unconditional**. They no-op when no `scope='deleted'` rows exist.
- Rollback of a specific delete = `POST /api/org/recordings/undelete {batch_id}` (reverts
  exactly that batch's redactions + tombstones and re-indexes). Rollback of the feature =
  flag off (no new deletes) + reverting whatever batches should be reverted, deliberately
  and per-batch. The marker stays in the data, as the spec wanted:
  `SELECT * FROM redactions WHERE scope='deleted' AND reverted_at IS NULL`.

### 0.3 One delete action = one `batch_id`, or §9's arithmetic is impossible

Spec §9 requires "one revert restores exactly what one delete hid, counted the same way".
`redactions` has no grouping column, and `create_redaction` has no uniqueness — a retried
batch would stack duplicate tombstones and the revert loop would not know where one delete
ends. Migration adds `batch_id uuid` and a partial unique index
`(target_type, target_id) WHERE reverted_at IS NULL AND scope='deleted'` so a retry of the
same batch is idempotent (skip-on-conflict), and revert is `WHERE batch_id=%s`.

### 0.4 Search is filtered in SQL, not only by async de-index

`build_search_sql` (src/repositories/search_sql.py:10-25) has **no redaction predicate**.
Today "redacted leaves RAG" is achieved only by the reindex round-trip
(org-api → S3 request → embed lambda → apply_vectors), and the enqueue failure is caught
and logged with the redaction kept (lambda_org_api.py:2813-2816) — a guard whose success
is never positively observed, plus a window (minutes) where the content is searchable
after the user was told it is gone. Worse: `report_chunks.topic_id` is
`ON DELETE SET NULL` (0004_report_chunks.sql:6), so orphaned chunks are unreachable both
by `delete_chunks_for_topic` and by any topic-join filter. Rule as implemented:

- `build_search_sql` gains two predicates: (a) `NOT EXISTS` an active `scope='deleted'`
  redaction on `c.topic_id`; (b) `NOT EXISTS` an active recording tombstone whose
  `target_key` prefixes `c.source_s3_key` — (b) is what catches orphans and
  not-yet-re-stamped chunks. This is the guarantee.
- The endpoint ALSO deletes chunks synchronously in the same transaction
  (`delete_chunks_for_topic` per topic + a new `delete_chunks_for_source_prefix`) — this
  is hygiene (keeps the index small, kills the race window), not the guarantee.
- The async reindex enqueue stays for the S3-side vector artifacts, still best-effort.

### 0.5 The VPC split forces an S3 tombstone mirror for the non-VPC readers/writers

`lambda_ask_agent` (non-VPC, no DB), `lambda_report_generator` (non-VPC),
`lambda_meeting_minutes` (non-VPC) all read `reports/*`, `transcripts/*` directly from S3
and cannot consult Aurora (BUG-36 split: in-VPC no egress, non-VPC no DB). A DB-only
tombstone is invisible to exactly the components that materialise and serve S3 artifacts.
Rule as implemented: the delete endpoint, after DB commit, writes/updates
`redactions/{folder}/{date}/deleted_sessions.json` in the lake bucket — a small JSON list
of tombstoned `session_base` values (+ batch_id, created_at). Consulted by:

- `lambda_report_generator`: skips those sessions' transcripts when (re)generating
  `daily_report.json` / `summary_report.json` / `meeting_minutes.json`;
- `lambda_ask_agent`'s legacy S3 path (`load_report`/`load_transcripts`,
  lambda_ask_agent.py:159-259): filters matching transcript files and, if the mirror
  exists for (folder,date), refuses the verbatim report in favour of a filtered render;
- `lambda_item_writer` / `lambda_ingest` (in-VPC, have DB): consult the DB tombstone
  directly instead (write-side guard, phase 6).

The endpoint then enqueues report regeneration for each affected (folder, date) so the
stale pre-redaction `daily_report.json` is replaced by a filtered one. Until that lands,
the org-api read side never serves the stale doc anyway (phase 4's verbatim-fallback
guard). Revert deletes the session from the mirror and re-enqueues regeneration.

### 0.6 "The endpoint refuses if a read path cannot be filtered" → a CI enumeration test

Nothing at runtime can enumerate read paths, so the spec's §8 sentence would never fail.
Replaced by phase 5's enumeration test: an AST/grep-driven unit test that finds every
`FROM topics` / `FROM report_chunks` in `src/` and asserts each is either (a) routed
through the choke predicates or (b) on an explicit allowlist with a written justification
(write paths, existence checks that leak nothing, the redactions table itself). A new
unfiltered read path turns CI red. This is the testable version of the spec's intent.

### 0.7 The DB will reject `scope='deleted'` today

0022_redactions.sql:19-20 has `CHECK (scope IN ('analysis','all'))`, and the existing
endpoint whitelists the same two values (lambda_org_api.py:2806). The spec's "this adds
'deleted'" is a migration + endpoint change, not a parameter default. Phase 1.

### 0.8 Deleted ≠ hidden topics only: raw media and transcripts are read paths too

`/api/org/transcripts`, `/audio-segments`, `/video-segments`, `/media/presigned-url`
(lambda_org_api.py:5168, 5597, 5658, 5772) list and presign the recording's own S3
objects with no topic involvement. Spec §6's "the only readers that still see it are the
offline analysis paths" is false as written — these are online, customer-facing paths
that go to S3 directly. A recording the user deleted must not remain playable in the UI.
Phase 8 filters these by the (folder, date) tombstoned session set. The S3 objects
themselves are untouched (the requirement), so genuinely offline analysis keeps working.

### 0.9 Authorization for the delete itself

The spec never says who may delete. Rule: the recording's own `user_id`, or an
`admin`/`gm` of the same company, or `platform_admin`. `_topic_authority` covers the
per-topic half; the endpoint checks recording ownership/company before anything else.
Deny-by-default; a batch containing one unauthorized recording fails **that recording
only** (per-item result), never silently widens.

---

## Phase 1 — Migration 0041: the schema this feature stands on

**RED test first:** `tests/unit/test_redactions_repo.py::test_create_deleted_scope_redaction_sql_carries_batch_and_target_key`
— asserts `create_redaction(..., scope='deleted', batch_id=..., target_key=...)` binds all
three into the INSERT and the returned row echoes them. Fails now: `create_redaction` has
no such kwargs (src/repositories/redactions.py:11).
Also `tests/unit/test_migrations.py::test_0041_widens_scope_check_and_adds_columns` —
reads `src/migrations/0041_user_deletion.sql` as text and asserts it contains the
`DROP CONSTRAINT`/`ADD CONSTRAINT` pair including `'deleted'`, `ADD COLUMN batch_id`,
`ADD COLUMN target_key`, the partial unique index, and — because 0022's CHECK also pins
`target_type` — the widened `target_type` CHECK including `'recording'`. Fails now: file
absent.

**Change:** `src/migrations/0041_user_deletion.sql`:

```sql
ALTER TABLE redactions DROP CONSTRAINT redactions_scope_check;
ALTER TABLE redactions ADD CONSTRAINT redactions_scope_check
  CHECK (scope IN ('analysis','all','deleted'));
ALTER TABLE redactions DROP CONSTRAINT redactions_target_type_check;
ALTER TABLE redactions ADD CONSTRAINT redactions_target_type_check
  CHECK (target_type IN ('topic','segment','finding','recording'));
ALTER TABLE redactions ADD COLUMN batch_id uuid;
ALTER TABLE redactions ADD COLUMN target_key text;
CREATE UNIQUE INDEX uq_redactions_active_deleted
  ON redactions (target_type, target_id)
  WHERE reverted_at IS NULL AND scope = 'deleted';
CREATE INDEX idx_redactions_deleted_key ON redactions (target_key)
  WHERE reverted_at IS NULL AND scope = 'deleted';
```

(Constraint names: verify live with `\d redactions` on TEST before merging — 0022 used
inline CHECKs, Postgres auto-names them; the migration must use the real names.)
Extend `create_redaction` with `batch_id=None, target_key=None` kwargs (both appended to
`_COLS`).

**Prod behaviour on merge:** none until `lambda_migrate` runs; the migration itself is
additive + constraint-widening (no existing row violates either CHECK — verify with
`SELECT DISTINCT scope, target_type FROM redactions` on prod BEFORE deploy).
**Needs:** migration deploy via `lambda_migrate`. No IAM, no template change.
**Can merge alone:** yes.
**Verification against live state:** after deploy, on TEST:
`INSERT ... scope='deleted' ... RETURNING id` succeeds and a second identical active
insert fails with unique violation; then delete the probe rows. Do not trust the deploy
record — run the probe.
**Blast radius:** near-zero (DDL on a small table). **Rollback:** reverse migration
(narrow CHECKs back only after `DELETE`-ing probe rows; columns can stay, they are inert).

## Phase 2 — Repository layer: the two choke predicates and their helpers

**RED tests first**, all in `tests/unit/test_redactions_repo.py`:
- `test_deleted_topic_predicate_sql` — a new module-level constant
  `DELETED_TOPIC_PREDICATE` (a SQL fragment parameterised on the topics alias) contains
  `scope = 'deleted'`, `reverted_at IS NULL`, `target_type = 'topic'`. Fails: absent.
- `test_deleted_source_prefixes_for_day_queries_target_key` — new
  `deleted_source_prefixes(conn, folder, date)` runs a SELECT on `target_key` with the
  `extractions/{folder}/{date}/` prefix pattern and active-deleted filters; returns a
  list of prefixes. Fails: absent.
- `test_create_recording_tombstone_and_revert_batch` — `create_recording_tombstone(...)`
  writes `target_type='recording'` with `target_key`; `revert_batch(conn, batch_id,
  company_id)` UPDATEs `reverted_at` on all active rows of the batch and returns them.
  Fails: absent.

**Change:** add to `src/repositories/redactions.py`: `DELETED_TOPIC_PREDICATE`,
`create_recording_tombstone`, `deleted_source_prefixes`, `list_batch`, `revert_batch`,
and `is_source_deleted(conn, source_s3_key)` (prefix match on `target_key`). Add
`delete_chunks_for_source_prefix(conn, prefix)` to `src/repositories/chunks.py` (LIKE
with the `_escape_like` posture topics.py uses — S3 folders contain literal underscores).

**Prod behaviour on merge:** none (dead code until called). **Can merge alone:** yes.
**Verification:** unit only at this phase. **Rollback:** revert the commit.

## Phase 3 — Read-path filtering: every `FROM topics` read drops deleted rows

**RED tests first:**
- `tests/unit/test_topics_repo.py::test_list_topics_for_date_excludes_deleted_scope` —
  fake-conn assertion that the SQL emitted by `list_topics_for_date` contains the deleted
  predicate (and, with the existing harness style, that a row set marked deleted is
  filtered). Fails now: topics.py:331 has no redaction reference.
- Same-shape tests for `list_topics_for_source_prefix`, `list_site_topics`,
  `list_report_dates`, `report_date_counts`, `list_extraction_topics_for_day`,
  `list_contributor_folders_for_site_date`, `get_topic_full`, `get_topic`,
  `threads.facts_for_threads` (times_raised must not count deleted topics — the thread
  fact block leaks "raised 3 times" derived from deleted content, lambda_org_api.py:4505).
- `test_thread_suggestions_exclude_deleted_topics` — `threads.list_pending` backs
  GET `/threads/suggestions` (lambda_org_api.py:4215-4243), which returns the pending
  suggestion's `topicTitle`/`parentTitle` verbatim: a deleted topic's TITLE keeps
  surfacing in the suggestions inbox with no filter today. Predicate on both the topic
  and the parent-topic sides.
- `test_programme_suggestions_exclude_deleted_topics` — `list_suggestions`
  (lambda_org_api.py:4023) serves `programme_progress_suggestions`, whose `topic_id` is
  `ON DELETE SET NULL` (0008_programme_suggestions.sql:7): filter on the topic redaction
  AND on the row's own source key for orphans, same two-arm shape as search (§0.4).
- `tests/unit/test_lambda_org_api.py::test_render_report_shape_drops_deleted_topics` —
  a topic whose active redaction has `scope='deleted'` is ABSENT from `topics_out`, not
  flagged. Today render_report_shape includes full content with `redacted: True`
  (lambda_org_api.py:4519) — correct for the personal-conversation feature (site tier
  still sees it in the "removed" area), wrong for deleted. `scope != 'deleted'` keeps the
  existing flag behaviour byte-identical.

**Change:** append `AND NOT EXISTS (SELECT 1 FROM redactions r WHERE r.target_type='topic'
AND r.target_id = t.id AND r.scope='deleted' AND r.reverted_at IS NULL)` (via
`DELETED_TOPIC_PREDICATE`) to each read listed above; in `render_report_shape`, partition
`list_active_for_topics` results by scope — `deleted` rows drop the topic, others keep
today's flag. `build_day_sessions` / `_assemble_session_report` already exclude ANY
active redaction (lambda_org_api.py:4921-4931, 1052-1059) — add a test pinning that
`scope='deleted'` rows keep being excluded there (no code change expected).
`rollup.portfolio_counts` already excludes via `company_excluded_topic_ids`
(rollup.py:71), which matches any active redaction regardless of scope — pin with a test,
no change.

**Prod behaviour on merge:** none observable until a `scope='deleted'` row exists (the
NOT EXISTS is a no-op on an empty set; the predicate uses idx_redactions_target). This is
the phase to watch for query-plan cost: verify on TEST with EXPLAIN on
`list_topics_for_date` before and after.
**Can merge alone:** yes (filters with nothing to filter).
**Verification against live state:** on TEST, insert one probe deleted-scope redaction on
a real topic, hit `/live-items`, `/timeline`, `/dates`, `/sessions`, `/rollup/portfolio`
as an **admin and as the topic's own author and as platform_admin** — the topic must be
absent from all three tiers (this is the §6 requirement: exclusion applied at the row
read, so platform_admin's span-all inherits it automatically). Revert the probe, confirm
it returns.
**Blast radius:** every dashboard read (added subquery). **Rollback:** revert commit —
content un-hides, which at this phase is acceptable because the endpoint (phase 7) hasn't
shipped and no customer has been told anything.

## Phase 4 — The S3-materialised leaks inside org-api

**RED tests first:**
- `tests/unit/test_lambda_org_api.py::test_timeline_verbatim_fallback_refused_when_day_has_deleted_sources`
  — when `redactions.deleted_source_prefixes(conn, folder, date)` is non-empty and no
  Aurora topics survive, `_render_timeline_for_user` returns the 404 body, NOT the
  verbatim `daily_report.json`. Fails now: lambda_org_api.py:4621-4623 serves the doc
  unconditionally. (Without this, deleting ALL of a day's topics makes `_aurora_shape`
  return None and the pre-redaction S3 doc — containing everything — is served verbatim.
  This is the exact hole the review predicted.)
- `test_admin_summary_report_refused_when_date_has_deleted_sources` — same guard on
  `admin_disambiguation`'s `summary_report.json` verbatim serve (lambda_org_api.py:4642).
- `test_session_report_status_never_presigns_docs_for_deleted_sessions` — the status
  endpoint (lambda_org_api.py:1198-1211) checks the session against the tombstone set
  before presigning an already-generated report doc.
- `test_session_rolling_refuses_deleted_session` — `session_rolling`
  (lambda_org_api.py:1217-1240) serves
  `session_rolling/{folder}/{date}/{session}/latest.json` straight off S3 with no topic
  involvement; when the session is tombstoned it must return the same body as
  "no rolling summary yet" (a plain 404-shaped `pending`/absent answer — do not advertise
  that a deletion happened).

**Change:** guard those three serves with `deleted_source_prefixes` /
`is_source_deleted`. The prose-merge path (`render_report_shape`'s `doc` argument) is
already safe once phase 3 lands for topics, but the doc's own
`executive_summary`/`safety_observations` prose was generated FROM the deleted sessions —
so when the day has any tombstoned source, pass `doc=None` (same posture as
`cross_user_clip`) until the regenerated report (phase 6) replaces it.

**Prod behaviour on merge:** none until deleted rows exist. **Can merge alone:** yes,
after phase 2. **Verification live:** on TEST, tombstone a whole day's only session, hit
`/timeline` — expect a 404-shaped body, not the old report; check the rolling/status
endpoints for that session. **Rollback:** revert commit.

## Phase 5 — The enumerated read-path test (its own phase, by design)

**RED test first:** `tests/unit/test_deleted_read_path_enumeration.py::test_every_topics_and_chunks_read_is_filtered_or_allowlisted`
— walks `src/**/*.py`, regex-finds every `FROM topics` / `FROM report_chunks` occurrence
(string-literal SQL is the repo's universal style, so text-level enumeration is sound
here), and asserts each (file, function) is either (a) in the same statement/function as
the deleted-predicate marker, or (b) in `ALLOWLIST`, a dict in the test mapping
`file::function -> one-line justification` (e.g. `topics.py::delete_topics_for_source` —
write path; `topics.py::has_topics_for_source_prefix` — existence probe that gates which
RENDER path runs, rendering is filtered; `redactions.py::*` — the mechanism itself;
`lambda_item_writer.py` — write path, guarded in phase 6). Write it BEFORE wiring, watch
it fail with the real list of unfiltered reads, and drive phases 3-4 to completion
against it. This test is the durable replacement for the spec's untestable "endpoint
refuses if a read path cannot be filtered": a future `FROM topics` read added without the
predicate or an allowlist entry turns CI red with a message explaining the contract.

**Prod behaviour on merge:** none (test only). **Can merge alone:** only together with or
after phases 3-4 (it is red until they land — that is the point).
**Rollback:** n/a.

## Phase 6 — Write-side guard: the pipeline cannot resurrect deleted content

**RED tests first:**
- `tests/unit/test_lambda_item_writer.py::test_item_writer_skips_tombstoned_source` —
  item_writer, handed an extraction whose `source_s3_key` falls under an active deleted
  `target_key`, writes NO topics and logs a counted skip (positive evidence, not
  silence). Fails now: no such check.
- `tests/unit/test_lambda_ingest.py::test_ingest_restamps_deleted_topics_under_tombstoned_source`
  — after the nightly re-ingest inserts report topics for a (folder, date) with an active
  tombstone, every new topic whose source falls under the tombstone gets a fresh
  `scope='deleted'` redaction (same batch_id as the tombstone) IN THE SAME TRANSACTION,
  and its chunks are not inserted (or are deleted before commit). Re-stamp rather than
  skip in ingest: the Aurora row must EXIST so `_aurora_shape` doesn't fall through to
  the verbatim S3 doc (see phase 4) — hidden row beats absent row.
- `tests/unit/test_report_generator_tombstones.py::test_generator_excludes_deleted_sessions_transcripts`
  — `lambda_report_generator`, with `redactions/{folder}/{date}/deleted_sessions.json`
  present in the (mocked) bucket, excludes those sessions' transcript files from the
  report build, and the generated doc records `"excluded_deleted_sessions": [...]` so the
  exclusion is positively observable (the count is the evidence the path executed).
- `tests/unit/test_lambda_ask_agent.py::test_legacy_s3_path_filters_deleted_sessions` —
  `load_transcripts` drops files under a deleted session and `load_report` refuses the
  unfiltered verbatim doc when the mirror lists sessions for that (folder, date).

**Change:** as the tests state. `lambda_item_writer`/`lambda_ingest` consult the DB
(`redactions.is_source_deleted` / prefix set); the non-VPC generator and ask-agent
consult the S3 mirror (§0.5). Sweep note: the existing per-minute sweeps must not wake
anything new — the DB check rides on connections these lambdas already hold.

**Prod behaviour on merge:** none until tombstones exist; then the nightly run for an
affected day produces a filtered report. **Needs IAM:** read of `redactions/*` prefix for
the report generator + ask agent roles (template change; after deploy verify with
`aws iam simulate-principal-policy` per this repo's standing rule — a missing grant here
is an `except ClientError: pass`-shaped silent hole).
**Can merge alone:** yes (inert without data). **Rollback:** revert commit; tombstones in
data remain, read filters still hide.

## Phase 7 — The endpoint: batch delete, batch undelete, flag-gated — changes PROD on merge+flag

**RED tests first**, in `tests/unit/test_recording_delete_endpoint.py`:
- `test_refuses_when_flag_off` — `ENABLE_USER_DELETION` unset → 403 with a body naming
  the flag; nothing written.
- `test_delete_batch_writes_tombstone_topic_redactions_chunks_and_mirror` — for two
  recordings: one shared `batch_id`; per-recording tombstone rows with correct
  `target_key`; a `scope='deleted'` redaction per derived topic (found via
  `topics.source_s3_key` prefix match, both `extractions/...` and the report-sourced
  `reports/{date}/{folder}/...` rows); synchronous `delete_chunks_for_topic` +
  `delete_chunks_for_source_prefix`; ONE S3 mirror write per (folder, date); response
  reports per-recording `{recording_id, topics_hidden, chunks_deleted}` — **a run that
  redacts nothing must say `topics_hidden: 0`, not succeed silently** (spec §9's last
  line).
- `test_delete_is_idempotent` — repeating the same request writes no second active
  tombstone (unique index) and returns the existing batch's counts.
- `test_partial_authorization_failure_fails_that_item_only` — a batch with one foreign
  recording returns 207-style per-item results; authorized items commit, the foreign item
  reports 403; nothing about the foreign item is written.
- `test_s3_objects_never_deleted` — the endpoint's S3 surface is `put_object` on
  `redactions/*` and reindex enqueues ONLY; assert no `delete_object` call is reachable
  (mock records every S3 call). Never `topics.delete_by_source_key`, never
  `delete_topics_for_source*` (spec §8).
- `test_undelete_batch_round_trip` — undelete reverts exactly the batch's rows, rewrites
  the mirror without those sessions, enqueues re-index; counts match the delete's counts.

**Change:** `POST /api/org/recordings/delete` `{recordings: [{recordingId} |
{folder, date, sessionBase}], reason?}` and `POST /api/org/recordings/undelete`
`{batchId}` in `dispatch`; authorization per §0.9. Order inside: one DB transaction
(tombstones + per-topic redactions + chunk deletes) → commit → S3 mirror writes → report
regeneration enqueue → response. A mirror-write failure after commit returns 500 **with
the batch_id**; the retry is idempotent and completes the mirror (the DB read filters
already hide everything meanwhile, so the failure mode is "S3 artifacts lag", never
"customer sees content"). Also widen `create_redaction_endpoint`'s whitelist
(lambda_org_api.py:2806) — or deliberately do NOT, keeping 'deleted' writable only
through the recording endpoint; **decision: do not widen** — a per-topic 'deleted'
without a source tombstone is exactly the resurrection bug this plan removes.
Flag wiring: repo variable → both workflows → template `Parameters`/`Environment` → the
org-api function env, all three segments in ONE commit, with
`tests/unit/test_deploy_workflow_params.py`-style assertions pinning each segment
(the unwired-toggle trap is a known repo failure mode — the test must read the actual
workflow YAML and template, not a fixture).

**Prod behaviour on merge:** endpoint exists but refuses (flag off on prod). Turning the
repo variable on is the deliberate PROD activation step, separate from merge.
**Needs:** template change (env var + `redactions/*` put IAM for org-api — check the
deploy role too; new-prefix IAM is a known trap in this repo). **Can merge alone:** only
after phases 1-6. **Blast radius when flag on:** writes are scoped to the selected
recordings' derivations; the read filters were already live. **Rollback:** flag off
(stops new deletes); per-batch undelete for anything that must be restored.

## Phase 8 — Raw media/transcript endpoints stop serving deleted sessions

**RED tests first**, in `tests/unit/test_org_media_deleted.py`:
- `test_transcripts_excludes_deleted_sessions` — `_read_org_transcripts` output contains
  no file whose session_base is tombstoned for (folder, date).
- Same for `_read_org_audio_segments`, `_read_org_video_segments`.
- `test_presign_refuses_deleted_session_media` — `get_org_media_presigned_url` returns
  404 for a key under a deleted session's `users/{folder}/{video|audio}/{date}/...`
  media (map via the same base-time/session parse `session_scope` provides; the
  recordings row's own `s3_key` is the exact key for app-registered uploads).

**Change:** each reader resolves `deleted_source_prefixes(conn, folder, date)` once
(org-api is in-VPC, has the DB) and filters/refuses. The S3 objects stay.

**Prod behaviour on merge:** none until tombstones exist. **Can merge alone:** after
phase 2. **Rollback:** revert commit (media reappears; acceptable only pre-activation —
after activation this phase is part of the contract, roll back via undelete instead).

## Phase 9 — PROD verification (spec §9), against live state, not the deploy record

Not merged code — an operator runbook executed after the first real use, recorded in the
PR that flips the prod flag:

1. **Before:** `aws s3api list-objects-v2` counts under the affected
   `users/{folder}/*/{date}/`, `transcripts/{folder}/{date}/`,
   `extractions/{folder}/{date}/` prefixes; `SELECT count(*) FROM redactions WHERE
   scope='deleted'` (expect 0 or the known baseline); a search and an Ask query that DO
   return the target content (positive control — without it, "absent after" proves
   nothing).
2. Execute the delete as the customer user (batch of ≥2 recordings).
3. **After:** the same S3 counts are UNCHANGED (nothing deleted); the redactions count
   equals topics-derived-from-selection (the endpoint's response says the expected
   number — compare, don't assume); the positive-control search returns nothing, Ask
   answers without the content, `/timeline`, `/live-items`, `/dates`, `/sessions`,
   `/rollup/portfolio` show none of it **as admin, as the recorder, and as
   platform_admin**; `/transcripts` and `/media/presigned-url` refuse the session.
4. Wait for (or trigger) the nightly report for that (folder, date): the regenerated
   `daily_report.json` lacks the sessions and Aurora holds NO unredacted topic under the
   tombstoned prefixes: `SELECT count(*) FROM topics t WHERE t.source_s3_key LIKE
   '<prefix>%' AND NOT EXISTS (<deleted predicate>)` — **this query is the resurrection
   canary; it must be 0 the morning after, and it is the single most important line in
   this section.**
5. Undelete the batch; re-run the positive-control search (content returns) and the
   counts (restored == hidden). Then re-delete if the customer's request stands.
6. IAM: `simulate-principal-policy` for org-api put on `redactions/*` and the
   generator/ask-agent read — before trusting any of the above.

**Blast radius:** none beyond the batch operated on. **Rollback:** step 5 is the
rehearsed rollback.

---

## Explicitly out of scope, recorded so it is a decision and not an oversight

- **The legacy hand-deployed gateway** (`fieldsight-api` — idle — and any legacy path in
  `fieldsight-prod-api` not routed through org-api/ask-agent): `lambda_fieldsight_api.py`'s
  own S3 readers (get_timeline:281, get_transcripts:566, get_actions:807) are
  deployed outside this repo's SAM pipeline. The ask/search routes forward to the SAM
  ask-agent (covered). The S3-direct routes on the legacy lambda serve `daily_report.json`
  verbatim; phase 6's regeneration closes them for regenerated days, but until
  regeneration runs they can serve stale content. Mitigation is operational: the phase 9
  runbook includes hitting the legacy timeline for the affected date and confirming the
  regenerated (filtered) doc is what it serves. If the legacy lambdas are truly
  customer-load-bearing for the target customer, closing them is a prerequisite to
  flipping the flag — decide against live traffic, not assumptions
  (fieldsight-two-legacy-api-lambdas: investigating the wrong one yields the opposite
  conclusion).
- **Emails already sent** (finalize confirmations, report mails) cannot be recalled;
  future sends are covered by phase 6's generator/mirror.
- **Evidence-level (sub-recording) selection** — spec §5 already defers it.
- **`recordings.day_stats` KPIs** still count deleted recordings (Recordings/Words
  header numbers). Cosmetic; fix as a follow-up if the customer notices.
