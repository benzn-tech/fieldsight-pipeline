# Track B — Stable identity for extracted items, and a record of every decision

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An extracted item (finding, action item, decision, question) keeps one identifier for its whole life, across the live → final re-extraction and every later re-run, so a human's tick survives, a question can be answered, an external system can reference it, and every AI verdict about it can be recorded and later scored.

**Architecture:** Stop physically deleting a source key's topics on re-extraction. Mark them `superseded_at` (the `speaker_turn_names` supersede-then-insert precedent, migration 0040) and insert the new pass beside them. Children carry a `stable_id` that is **carried forward** from the superseded pass when the text matches (exact `content_hash`, else a high fuzzy floor), and a human's edits ride with it. An old row a human touched that finds no successor is **kept and surfaced**, never silently lost — which is what `_warn_if_discarding_checkoffs` said the only safe rule was. Every gated AI verdict — accepted *and* rejected — is written to one `decision_records` table, and the confirm/reject endpoints stamp the human outcome onto the same row.

**Tech Stack:** Postgres (Aurora), psycopg3, Python 3.11 Lambda, SAM. Migration numbering: `develop` is at `0070` as of 2026-09-29 (0063 is unused; there are two 0041s and two 0044s). Take the next free number at merge time and expect to renumber once.

**Spec:** `docs/superpowers/specs/2026-09-24-event-graph-and-jev-assessment.md` §2.1, §2.2, §6.

**Owner decisions already taken (2026-09-24):** the event is the item row (finding / action item / decision / question), the topic is its container; Track B runs in parallel with Track A and does not wait for it.
**Added 2026-09-29 (brainstorm round two):** reserve `audience` on every item table and `kind` + `payload` on findings now, so the subcontractor and developer segments are later migrations that add CHECK values, not re-keys. Field definitions for the new kinds are deliberately not in this plan.

## Global Constraints

- **Never delete a row a human has touched.** Physical delete of topics is reserved for the deletion paths that already exist (the `redactions` tombstone mirror; `lambda_nonwork_expiry` redacts rather than deletes, so it is not one). Re-extraction supersedes.
- **A wrong carry-forward is worse than a lost one** (`lambda_item_writer.py:984-993`). Exact hash first; fuzzy only above 0.90 on normalised text, one-to-one, within the same source key and site; a tie or a second candidate within 0.05 is a miss, not a guess.
- **Readers must exclude superseded rows through ONE predicate**, added to `deleted_predicates.py`, never inlined. That module exists because eleven inlined copies drifted once already.
- **Every child table that gets `stable_id` gets it `NOT NULL DEFAULT gen_random_uuid()`** so existing rows are valid immediately and the column can be added without a backfill step.
- **Payload shape is unchanged in this track.** `key_decisions` and `open_questions` stay plain strings (`lambda_org_api.py:6783-6793`, React child constraint). The new tables are written alongside the jsonb columns; readers switch in a later change.
- **`decision_records` rows never carry transcript text.** Subject text is referenced by `stable_id`; the input the model saw is an S3 key (the existing `match_requests/` artifact), not a copy.
- **Run the SQL against a real database before trusting it** (CLAUDE.md "Testing"). The doubles do not enforce CASCADE, partial indexes or `NULLS LAST`. Every migration in this track has an integration test under `tests/integration/`.
- **`MSYS_NO_PATHCONV=1`** on any AWS CLI call with a `/`-prefixed argument.

---

### Task 1: Migration — supersession columns, stable ids, decision records

**Files:**
- Create: `src/migrations/00NN_stable_identity.sql`
- Test: `tests/unit/test_migration_stable_identity_shape.py`, `tests/integration/test_stable_identity_schema.py`

- [x] **Step 1: Write the migration.** Filed as `src/migrations/0071_stable_identity.sql` (Ruling R1). Postgres rejects `ADD CONSTRAINT IF NOT EXISTS` (no such grammar) and inline `ADD COLUMN ... CHECK` cannot carry `IF NOT EXISTS` on the CHECK itself either, so `findings.kind`, `findings.audience`, `action_items.audience` are each split into a plain `ADD COLUMN IF NOT EXISTS ...` followed by an unconditional `ADD CONSTRAINT <name> CHECK (...)` — safe because `schema_migrations` guarantees the file runs at most once. Everything else is verbatim from this brief.

```sql
-- Supersession instead of deletion for extraction topics. Spec 2026-09-24 §2.1.
ALTER TABLE topics ADD COLUMN IF NOT EXISTS superseded_at timestamptz;
ALTER TABLE topics ADD COLUMN IF NOT EXISTS superseded_by_run text;   -- "{tier}:{extracted_at}" of the pass that replaced it
CREATE INDEX IF NOT EXISTS idx_topics_live_source
    ON topics (source_s3_key) WHERE superseded_at IS NULL;

-- Stable child identity. DEFAULT so every existing row is valid at once.
ALTER TABLE action_items ADD COLUMN IF NOT EXISTS stable_id uuid NOT NULL DEFAULT gen_random_uuid();
ALTER TABLE action_items ADD COLUMN IF NOT EXISTS carried_from uuid;   -- the superseded row this took its stable_id from
ALTER TABLE findings     ADD COLUMN IF NOT EXISTS stable_id uuid NOT NULL DEFAULT gen_random_uuid();
ALTER TABLE findings     ADD COLUMN IF NOT EXISTS carried_from uuid;
CREATE INDEX IF NOT EXISTS idx_action_items_stable ON action_items (stable_id);
CREATE INDEX IF NOT EXISTS idx_findings_stable     ON findings (stable_id);

-- Room reserved on 2026-09-29 for the two customer segments (brainstorm round two), so the
-- segment work is a later migration ADDING CHECK values and payload keys, not re-keying rows.
-- `audience`: what may leave the company. 'internal' is the default and the only value any
-- writer sets today; 'owner' is set by a person (site manager) before an owner-facing
-- publish, never by the extractor alone. On every item table, because publishing is per item.
-- `kind` on findings: an observation today; 'instruction_received' / 'daywork_record' /
-- 'delay_event' are the subcontractor segment's event types, whose fields are NOT fixed yet
-- (they wait on the owner's phone calls) and will live in `payload` when they are.
ALTER TABLE findings     ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'observation'
    CHECK (kind IN ('observation','instruction_received','daywork_record','delay_event'));
ALTER TABLE findings     ADD COLUMN IF NOT EXISTS payload jsonb;
ALTER TABLE findings     ADD COLUMN IF NOT EXISTS audience text NOT NULL DEFAULT 'internal'
    CHECK (audience IN ('internal','owner'));
ALTER TABLE action_items ADD COLUMN IF NOT EXISTS audience text NOT NULL DEFAULT 'internal'
    CHECK (audience IN ('internal','owner'));

-- Decisions and questions become rows. Mirrors 0010 (site_id denormalised, CASCADE on both FKs).
-- These tables were declined on 2026-09-07 because ids churned; supersession is what makes them viable.
CREATE TABLE IF NOT EXISTS topic_decisions (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  stable_id   uuid NOT NULL DEFAULT gen_random_uuid(),
  carried_from uuid,
  topic_id    uuid NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
  site_id     uuid NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  decision    text NOT NULL,
  rationale   text,
  decided_by  text,
  audience    text NOT NULL DEFAULT 'internal' CHECK (audience IN ('internal','owner')),
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_topic_decisions_topic ON topic_decisions (topic_id);
CREATE TABLE IF NOT EXISTS topic_questions (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  stable_id   uuid NOT NULL DEFAULT gen_random_uuid(),
  carried_from uuid,
  topic_id    uuid NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
  site_id     uuid NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  question    text NOT NULL,
  status      text NOT NULL DEFAULT 'open' CHECK (status IN ('open','answered','dropped')),
  answered_by uuid REFERENCES users(id),
  answered_at timestamptz,
  audience    text NOT NULL DEFAULT 'internal' CHECK (audience IN ('internal','owner')),
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_topic_questions_topic ON topic_questions (topic_id);

-- One row per gated AI verdict, accepted or not, and the human's answer to it. Spec §2.2.
CREATE TABLE IF NOT EXISTS decision_records (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id      uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  site_id         uuid REFERENCES sites(id) ON DELETE CASCADE,
  kind            text NOT NULL,          -- programme_match | programme_impact | thread | work_class | ...
  subject_type    text NOT NULL,          -- topic | finding | action_item | decision | question
  subject_stable_id uuid NOT NULL,        -- topics.id for a topic (topics are not re-keyed); stable_id for children
  object_ref      text,                   -- the other side: task_id, earlier topic id, ...
  provider        text NOT NULL,          -- qwen | anthropic | typesafe | lexical | rule
  model           text,
  model_version   text,
  question_set    text,                   -- name+hash of the prompt or question set
  input_key       text,                   -- S3 key of what the model saw (match_requests/... ), never the text
  input_hash      text,
  output          jsonb NOT NULL,         -- probabilities / verdict as returned
  score           real,                   -- the single number the gate compared
  threshold       real,
  auto_outcome    text NOT NULL,          -- accepted | rejected | abstained
  human_outcome   text,                   -- confirmed | rejected | edited | NULL
  human_actor     uuid REFERENCES users(id),
  human_at        timestamptz,
  created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_decision_records_subject ON decision_records (subject_type, subject_stable_id);
CREATE INDEX IF NOT EXISTS idx_decision_records_kind_time ON decision_records (company_id, kind, created_at DESC);
```

- [x] **Step 2: Shape test** (unit, SQL text): both new child tables carry `ON DELETE CASCADE` on `site_id` (the 2026-09-07 review found a draft that dropped it); `stable_id` columns are `NOT NULL DEFAULT gen_random_uuid()`; the live-source index is partial on `superseded_at IS NULL`; every item table has `audience` defaulting to `'internal'` with the two-value CHECK, and `findings.kind` defaults to `'observation'`. `tests/unit/test_migration_stable_identity_shape.py`, 9 tests, all green.
- [x] **Step 3: Integration test** (real Postgres via `migrated_db_url`): insert a topic + action item, `UPDATE topics SET superseded_at=now()`, assert the child row still exists and its `stable_id` is unchanged; delete the topic, assert CASCADE removed the child and the decision_records row survives (it is not FK-bound to the child, by design — the record outlives the row it judged). Also covers: partial index hides superseded rows, CHECK rejects a bad `audience`/`kind` on `findings`/`action_items`, and a row inserted without the new columns gets `audience='internal'`, `kind='observation'`, non-null `stable_id`. `tests/integration/test_stable_identity_schema.py`, 7 tests (Task 1's own note said 8; corrected here in Task 2's commit — the file has 7), all green against real local Postgres. `tests/integration/test_migrations_apply.py` (idempotent re-apply) reconfirmed green, 7 tests.

---

### Task 2: One live-rows predicate, and every reader uses it

**Files:**
- Modify: `src/deleted_predicates.py`
- Modify: `src/repositories/topics.py`, `src/repositories/chunks.py`, `src/repositories/search_sql.py`, `src/repositories/findings.py` (`count_by_domain`), `src/repositories/rollup.py`, `src/repositories/threads.py`, `src/lambda_org_api.py` (any inlined `DELETED_TOPIC_PREDICATE` use)
- Test: `tests/unit/test_live_topic_predicate_everywhere.py`, `tests/integration/test_superseded_topics_invisible.py`

- [x] **Step 1:** Added `LIVE_TOPIC_PREDICATE` (+ its complement `SUPERSEDED_TOPIC_PREDICATE`, for the one caller — `company_excluded_topic_ids` — that has to name superseded rows affirmatively) and folded it into `visible_topics_predicate()`. `visible_chunks_predicate` gained the `EXISTS (... t.superseded_at IS NULL) OR {alias}.topic_id IS NULL` arm. `CHILD_OF_VISIBLE_TOPIC` moved from `topics.py` into `deleted_predicates.py` and gained the same live arm (kept importable as `topics.CHILD_OF_VISIBLE_TOPIC`).
- [x] **Step 2:** 26 read sites route through the shared predicates (most inherited automatically once the helper functions changed; `list_site_topics`/`list_extraction_topics_for_day`/`list_report_dates` got the arm added inline; `has_topics_for_source[_prefix]`/`list_topics_for_source_prefix` gained an `include_superseded` keyword, default `False`, with the 3 delete/undelete enumeration callers in `lambda_org_api.py` passing `True`; `rollup.portfolio_counts` inherits via `redactions.company_excluded_topic_ids`, which now also excludes superseded ids). 19 sites left deliberately unfiltered (write/lock/idempotency/enumeration/sweep paths per Ruling R2/R3) — full site-by-site table in `.superpowers/sdd/2026-09-24-track-b-stable-identity-and-decision-records/task-2-report.md`. **Fix round 1 (review):** `get_topic_full`'s photos child had silently inherited the live arm through the shared `CHILD_OF_VISIBLE_TOPIC` constant, contradicting its own R3 exemption — split into a new deletion-only `CHILD_OF_UNDELETED_TOPIC` helper in `deleted_predicates.py`; `threads.get_suggestion` (missed the first pass) added to the unfiltered list.
- [x] **Step 3: Unit test**, `tests/unit/test_live_topic_predicate_everywhere.py`, 23 tests: building-block checks, a source-text scan over the 26 filtered sites, an existence check that both lists name real functions, FakeConn spot-checks proving the `include_superseded` toggle actually branches (not just textually present), and (fix round 1) two regression guards pinning `get_topic_full`'s photos read to the deletion-only predicate by name.
- [x] **Step 4: Integration test**, `tests/integration/test_superseded_topics_invisible.py`, 11 tests against real Postgres: two passes of one source key (first superseded, via direct SQL) — `list_topics_for_date` returns only the second pass and its own children; `search_chunks` never returns the superseded pass's chunk even when it is the nearest by cosine, and still returns an unassigned (`topic_id IS NULL`) chunk; `has_topics_for_source[_prefix]` true; `list_topics_for_source_prefix` live-only by default, both passes with `include_superseded=True`; `list_expired_non_work` still sees the superseded row (R3); `company_excluded_topic_ids` includes the superseded id, not the live one; (fix round 1) `get_topic_full` still returns a superseded topic's photos, and still excludes a deleted topic's photos.

---

### Task 3: Supersede instead of delete in the item writer (and the two other delete sites)

**Files:**
- Modify: `src/repositories/topics.py` (add `supersede_topics_for_source`, `supersede_topics_for_source_prefix`; keep the delete functions for the deletion/expiry paths)
- Modify: `src/lambda_item_writer.py:995-1009`, `:891`, `_delete_member_topics`
- Modify: `src/lambda_ingest.py:657`, `:670`
- Modify: `src/lambda_nonwork_expiry.py` (it redacts by topic id and re-indexes; it must select superseded rows of an expired day too, or they stay unredacted and reachable to a future reader that drops the live predicate)
- Test: `tests/unit/test_item_writer_supersedes_not_deletes.py`, `tests/integration/test_supersede_two_passes.py`

- [x] **Step 1:** `supersede_topics_for_source(conn, source_s3_key, run) -> list[dict]` executes `UPDATE topics SET superseded_at=now(), superseded_by_run=%s WHERE source_s3_key=%s AND superseded_at IS NULL RETURNING id, title, summary` and returns the rows it retired (Task 4 needs them). Same for the prefix form, with the existing `_escape_like`.
- [x] **Step 2:** Replaced the four delete call sites (writer :892 authority-flip, writer :997 idempotent clear, `_delete_member_topics` → renamed `_supersede_member_topics` (group tier), ingest :658/:671). `_warn_if_discarding_checkoffs` message now "%d closed action item(s) on rows being superseded for %s -- Task 4 must carry them forward". Writer `run` = `f"{extraction.get('tier')}:{extraction.get('extracted_at')}"`; ingest `run` = `"report:" + (report['_report_metadata']['generated_at'] or report_key)` (report generation timestamp, falls back to the key). `superseded_by_run` names the pass that DID the retiring (the new pass), confirmed by the integration test.
- [x] **Step 3:** `_source_is_deleted` ordering unchanged — verified unmodified (advisory lock → I-4 report-already-ingested gate → deleted-source gate → supersede → inserts).
- [x] **Step 4: Unit test:** `tests/unit/test_item_writer_supersedes_not_deletes.py`, 4 tests — a `_Conn`/`_Cur` double recording SQL from both calling conventions, with the real `supersede_topics_for_source[_prefix]` restored over the suite's default stub, proves the extraction path, the authority-flip branch, the group-merge path (`_supersede_member_topics`, incl. its RETURNING rows actually coming back through the function's return value), and both ingest call sites each issue `UPDATE topics SET superseded_at` and never `DELETE FROM topics`. Plus (fix round 1) `tests/unit/test_item_writer_group.py` gained 2 tests proving `_supersede_member_topics` flattens every member's retired rows into its return value, and that `write_extraction_items`'s `retired_topics` accumulator is actually extended with the group-tier supersede's result (it previously discarded it). **Integration test:** `tests/integration/test_supersede_two_passes.py`, 6 tests — 5 repository-level (`db` fixture) covering supersede/idempotency/two-pass-reads/prefix-form/nonwork-expiry-redacts-both, plus one true end-to-end test driving `lambda_item_writer.write_extraction_items` itself twice (live then final pass) against a real, separately-committed connection with a real seeded company/site/user/membership (no repository function stubbed except the S3 `match_request.emit`) — proves two physical rows, one superseded with `superseded_by_run` set, both present, and the day's live read shows only the final pass. (Fix round 1: this test leaked its rows into the shared local test DB, breaking three unrelated tests' "empty DB" skip guards — now cleans up everything it created in a `finally`, verified by an in-test count assertion and by a full `tests/integration` run against a fresh schema returning to the pre-Task-3 skip count.)
- [x] **Step 5: Row growth note** added to the module docstring (a session's topics rows now grow ~3x across its 2-4 passes; the partial index keeps live reads at pre-Task-3 cost).

---

### Task 4: Carry-forward of stable ids and human edits

**Files:**
- Create: `src/carry_forward.py` (pure; no psycopg, no boto3)
- Modify: `src/lambda_item_writer.py` (after the topic loop, before `collected_topics` is used)
- Modify: `src/repositories/action_items.py`, `src/repositories/findings.py` (add `list_for_superseded_source`, `carry_identity`)
- Test: `tests/unit/test_carry_forward.py`, `tests/integration/test_tick_survives_final_pass.py`

**Interfaces:**
- `carry_forward.match(old: list[Item], new: list[Item], *, fuzzy_floor=0.90, tie_margin=0.05) -> (pairs: list[(old_id, new_id, how)], orphans: list[old_id])` where `Item = {id, stable_id, text, human_touched: bool}`.

- [x] **Step 1: The matcher.** Per Ruling R4: key = `content_hash.normalize(text)` then `evidence_match.strip_cjk_spacing` (new public helper, extracted from `normalise`'s own inline loop rather than copied). Pass 1: exact key equality, one-to-one greedy. Pass 2: mutual-best-with-margin over the remaining pools (ratio ≥ fuzzy_floor, and the runner-up on EITHER side more than tie_margin below) — computed once over the full remaining candidate set so accepting one pair never perturbs another pair's tie check. `src/carry_forward.py` is pure and generic over `Item = {id, stable_id, text, human_touched}`.
- [x] **Step 2: Apply in the writer.** `_carry_forward_children` runs in `write_extraction_items` right after the topic-insert loop (new topic ids only known then), still inside the one `with get_connection()` transaction, gated on `retired_topics` being non-empty. Old/new pools loaded via new `action_items.list_for_carry_forward(conn, topic_ids, site_id)` / `findings.list_for_carry_forward(conn, topic_ids, site_id)` (named after what they take, per the brief's "your call" — `findings.py` already had an unrelated `list_for_topics`), both site-scoped per Ruling R9. `carry_identity(conn, new_id, old_row)` always moves `stable_id`/`carried_from`; when `human_touched`, action items also copy `status, priority, deadline, deadline_text, responsible, updated_by, updated_at, audience`, findings copy `status, audience` only (never `kind`/`payload`/impact_*).
- [x] **Step 3: Orphans.** `_report_orphaned_human_edits(extraction_key, count)` replaces `_warn_if_discarding_checkoffs` (removed, along with its call site before the idempotent clear). Per Ruling R5: prints one CloudWatch EMF JSON line to stdout (namespace `FieldSight/Pipeline`, dimension `Stage`, metric `OrphanedHumanEdits`, `key` as a property) — never `put_metric_data` (ItemWriterFunction is in-VPC, BUG-36) — emitted on every pass with a non-empty `retired_topics`, including value 0, and never raises. `logger.warning` with the brief's exact line only when count > 0. Test file renamed `tests/unit/test_orphaned_human_edits_reported.py`.
- [x] **Step 4: Unit tests** — `tests/unit/test_carry_forward.py`, 7 tests. Real TEST string used for the exact-pair case (`extractions/Ben_UCPK2/2026-08-13/sid9db9293e82b94a4d9611572b1233f82d.json`, no name to mask). No live/final PAIR was retrievable: `aws s3api get-bucket-versioning` on the TEST bucket returned no `Status` (versioning off) and `extract_session.out_key` is computed once, so live/final overwrite the same S3 key — confirmed empirically, not assumed. The brief's named case ("Check the scaffolding before Monday" vs "Scaffolding to be checked before Monday") measures **ratio 0.6757**, below the 0.90 floor — asserted as an orphan (floor not tuned); a genuine in-floor reword ("Order rebar delivery for Monday" → "Order the rebar delivery for Monday", ratio 0.9394) is the fuzzy-pair case instead. Also covers the tie-orphan, CJK-exact, untouched-vs-touched-orphan (both are just orphans to `match`; the split is the caller's job, covered in Step 3's test file), and one-to-one dedup cases.
- [x] **Step 5: Integration test** — `test_carry_forward_survives_a_live_then_final_pass` added to `tests/integration/test_supersede_two_passes.py` (Task 3's e2e harness, committed connection, `finally`-cleanup deleting only this test's own company/site/user by id, same pattern as the existing test in the file). Drives two real `write_extraction_items` passes; between them, `action_items.update_action_item_fields(..., {"status": "done"}, updated_by)` and a raw `UPDATE findings SET status, audience`. Asserts the new (post-final) action item row has the old `stable_id`, `carried_from = old id`, `status = 'done'`, `updated_by` preserved, and the new finding row carries `stable_id`/`carried_from`/`status='resolved'`/`audience='owner'`. Both reworded texts measured at ratio 0.9444.

---

### Task 5: Decisions and questions as rows, dual-written

**Files:**
- Create: `src/repositories/topic_decisions.py`, `src/repositories/topic_questions.py` (shape of `findings.insert_findings`: `_COLS`, per-row insert loop, batched `list_for_topics` with `= ANY(%s)`)
- Modify: `src/lambda_item_writer.py` (write the rows in the same transaction, right after `insert_findings`; keep passing the jsonb to `upsert_topic` unchanged)
- Modify: `src/carry_forward.py` call site — both new tables go through Task 4 (`decision` / `question` text), questions carry `status, answered_by, answered_at` forward.
- Test: `tests/unit/test_decisions_questions_rows.py`, `tests/integration/test_question_answered_survives.py`

- [ ] **Step 1:** Repos and writer, defensive `.get` throughout, blank entries dropped exactly as the jsonb path drops them (`lambda_item_writer.py:1085-1101`).
- [ ] **Step 2:** `PATCH /api/org/questions/{stable_id}` → `status` in `answered|dropped|open`, roles `_CORRECTION_ROLES`, writes `content_edits` with `table_name='topic_questions'`. Look up by `stable_id` on the **live** topic (join through `visible_topics_predicate`). This is the endpoint the 2026-09-07 spec could not have.
- [ ] **Step 3:** Integration test: answer a question, re-extract with reworded question text within the floor, assert `status='answered'` on the new row and the same `stable_id`.
- [ ] **Step 4:** Payload untouched. Add one comment at `lambda_org_api.py:6783` saying the jsonb is now a mirror of `topic_decisions` and the reader switch is a separate change.

---

### Task 6: Every gated verdict becomes a decision record

**Files:**
- Create: `src/repositories/decision_records.py`
- Modify: `src/lambda_programme_matcher.py` (emit *all* parsed verdicts, not only accepted ones, in a new `verdicts` list on the writer event; keep `suggestions`/`impacts` as they are)
- Modify: `src/lambda_suggestion_writer.py` (insert one record per verdict, same transaction)
- Modify: `src/lambda_item_writer.py` `_suggest_threads` (one record per scored candidate ≥ 0.10, `provider='lexical'`), and the topic insert (one `work_class` record per topic with `provider` = the extraction's LLM, `score=work_confidence`)
- Modify: `src/lambda_org_api.py` `confirm_suggestion` / `reject_suggestion` / thread confirm + reject / `classification-feedback` POST → `decision_records.set_human_outcome(...)`
- Test: `tests/unit/test_decision_records_written_for_rejected_verdicts.py`, `tests/unit/test_matcher_emits_all_verdicts.py`, `tests/integration/test_decision_records_roundtrip.py`

- [ ] **Step 1: Repo.** `insert(conn, **cols) -> dict`; `set_human_outcome(conn, kind, subject_type, subject_stable_id, object_ref, outcome, actor) -> int` updating the **latest** record for that subject/object (`ORDER BY created_at DESC LIMIT 1`, and note `NULLS LAST` is irrelevant here but say why in the docstring); `list_for_eval(conn, company_id, kind, since) -> list[dict]` for Track A's export to switch to once this lands.
- [ ] **Step 2: Matcher.** `parse_verdict` and `parse_impact_verdicts` currently return only accepted items. Add a sibling `parse_all_verdicts` that returns every parsed element with `auto_outcome` computed by the same double gate, and pass that list to the writer as `verdicts`. `input_key` = the `match_requests/` key the matcher was invoked with; `question_set` = sha256 of the prompt template text (so a prompt change is visible in the records).
- [ ] **Step 3: Human outcomes.** In `confirm_suggestion` (after `decide()` succeeds) and `reject_suggestion`, call `set_human_outcome(kind='programme_match', subject_type='topic', subject_stable_id=topic_id, object_ref=task_id, outcome=...)`. Threads: `kind='thread'`, `object_ref` = the earlier topic id. Classification: `kind='work_class'`, outcome from the verdict enum. `edited` when a reviewer overrode `applied_status/applied_progress`.
- [ ] **Step 4: Tests.** Unit: a below-threshold verdict still produces a record with `auto_outcome='rejected'` (this is the row Track A needs most and today it is thrown away); the writer inserts records in the same transaction as suggestions. Integration: write → confirm → `list_for_eval` shows `human_outcome='confirmed'` on the right row and NULL on a different task's row for the same topic.
- [ ] **Step 5: Deletion.** `redactions` for a deleted recording must also hide its decision records: add a `DELETE FROM decision_records WHERE subject_stable_id IN (…)` step to the deletion path (`docs/runbooks/user-deletion-prod.md` lists the tables — add this one), and a test that the deletion mirror covers it.

---

### Task 7: Backfill existing human verdicts into decision records (optional, one script)

**Files:**
- Create: `scripts/backfill_decision_records.py` (RDS Data API, one transaction, `--apply` flag; dry-run by default)

- [ ] **Step 1:** For each `programme_progress_suggestions` row with `state IN ('confirmed','rejected')`, insert a record with `provider='legacy'`, `score=confidence`, `threshold=0.70`, `auto_outcome='accepted'`, `human_outcome` from state. Same for `topic_thread_suggestions` and `classification_feedback`. Idempotent on `(kind, subject_stable_id, object_ref, created_at)`.
- [ ] **Step 2:** Print counts; commit only with `--apply`.

---

### Task 8: Deploy to TEST and prove it on a real session

- [ ] **Step 1:** `develop` → `deploy.yml`. `lambda_migrate` applies the migration; confirm with `SELECT column_name FROM information_schema.columns WHERE table_name='topics' AND column_name='superseded_at'` through the Data API.
- [ ] **Step 2:** Record a short session on TEST, tick one action item in the app while recording, let finalize run. Verify through the Data API: two topic rows for the key, one superseded; the ticked item's `stable_id` appears on the live row with `status <> 'open'`; `OrphanedHumanEdits` metric is 0 for that key.
- [ ] **Step 3:** Confirm one programme suggestion and one thread suggestion; verify `decision_records` shows `human_outcome` on the matching rows and at least one `auto_outcome='rejected'` row from the same matcher run.
- [ ] **Step 4:** Check Ask and search on that day return no duplicates (Task 2's chunk predicate).
- [ ] **Step 5:** Watch `ItemWriter` errors and duration for 24h; the supersede UPDATE plus carry-forward SELECTs add two round-trips per pass.

---

## Verification (owner reads)

1. A ticked action item survives its session's final pass on TEST, with the same `stable_id`.
2. A question can be marked answered and stays answered after re-extraction.
3. `decision_records` contains rejected verdicts, not only accepted ones, and confirm/reject stamps them.
4. No read path returns a superseded topic (integration test + a search on the test day).
5. Track A's export (its Task 1) can be re-pointed at `decision_records.list_for_eval` — that is the hand-off between the two tracks.
6. A finding whose `audience` a site manager set to `'owner'` keeps that value across its session's final pass (the owner-publish path in a later track depends on it).

## Out of scope

The fields inside `findings.payload` for the three subcontractor event types, the owner-publish endpoint that flips `audience`, and the contract clock (`contract_clock.py`) — all wait on the owner's phone calls (brainstorm round two §6). `event_links` (edges), `claim_type`, `location`/`tags` tables, the Procore push, switching the org-api payload to the new decision/question rows, and any change to matcher thresholds — all Track C, after Track A's findings are read.
