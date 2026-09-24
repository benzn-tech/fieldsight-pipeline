# Track B — Stable identity for extracted items, and a record of every decision

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An extracted item (finding, action item, decision, question) keeps one identifier for its whole life, across the live → final re-extraction and every later re-run, so a human's tick survives, a question can be answered, an external system can reference it, and every AI verdict about it can be recorded and later scored.

**Architecture:** Stop physically deleting a source key's topics on re-extraction. Mark them `superseded_at` (the `speaker_turn_names` supersede-then-insert precedent, migration 0040) and insert the new pass beside them. Children carry a `stable_id` that is **carried forward** from the superseded pass when the text matches (exact `content_hash`, else a high fuzzy floor), and a human's edits ride with it. An old row a human touched that finds no successor is **kept and surfaced**, never silently lost — which is what `_warn_if_discarding_checkoffs` said the only safe rule was. Every gated AI verdict — accepted *and* rejected — is written to one `decision_records` table, and the confirm/reject endpoints stamp the human outcome onto the same row.

**Tech Stack:** Postgres (Aurora), psycopg3, Python 3.11 Lambda, SAM. Migration numbering continues from `0064_quarantine_incoherent_samples.sql` (0063 is unused; there are two 0041s and two 0044s, so take the next free number at merge time).

**Spec:** `docs/superpowers/specs/2026-09-24-event-graph-and-jev-assessment.md` §2.1, §2.2, §6.

**Owner decisions already taken (2026-09-24):** the event is the item row (finding / action item / decision / question), the topic is its container; Track B runs in parallel with Track A and does not wait for it.

## Global Constraints

- **Never delete a row a human has touched.** Physical delete of topics is reserved for the deletion feature (`redactions` tombstones) and `lambda_nonwork_expiry`. Re-extraction supersedes.
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

- [ ] **Step 1: Write the migration.**

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

- [ ] **Step 2: Shape test** (unit, SQL text): both new child tables carry `ON DELETE CASCADE` on `site_id` (the 2026-09-07 review found a draft that dropped it); `stable_id` columns are `NOT NULL DEFAULT gen_random_uuid()`; the live-source index is partial on `superseded_at IS NULL`.
- [ ] **Step 3: Integration test** (real Postgres via `migrated_db_url`): insert a topic + action item, `UPDATE topics SET superseded_at=now()`, assert the child row still exists and its `stable_id` is unchanged; delete the topic, assert CASCADE removed the child and the decision_records row survives (it is not FK-bound to the child, by design — the record outlives the row it judged).

---

### Task 2: One live-rows predicate, and every reader uses it

**Files:**
- Modify: `src/deleted_predicates.py`
- Modify: `src/repositories/topics.py`, `src/repositories/chunks.py`, `src/repositories/search_sql.py`, `src/repositories/findings.py` (`count_by_domain`), `src/repositories/rollup.py`, `src/repositories/threads.py`, `src/lambda_org_api.py` (any inlined `DELETED_TOPIC_PREDICATE` use)
- Test: `tests/unit/test_live_topic_predicate_everywhere.py`, `tests/integration/test_superseded_topics_invisible.py`

- [ ] **Step 1:** Add `LIVE_TOPIC_PREDICATE = "{alias}.superseded_at IS NULL"` and fold it into `visible_topics_predicate()`. Chunks: `visible_chunks_predicate` gains an `EXISTS (SELECT 1 FROM topics t WHERE t.id={alias}.topic_id AND t.superseded_at IS NULL) OR {alias}.topic_id IS NULL` arm — a chunk bound to a superseded topic must not be retrievable, and an unbound transcript-window chunk stays.
- [ ] **Step 2:** `grep -n "DELETED_TOPIC_PREDICATE\|DELETED_SOURCE_PREDICATE" src/` and route every site through the two helper functions. Where a query builds its own WHERE on `topics` without the helper (e.g. `has_topics_for_source`, `list_topics_for_source_prefix`, `list_extraction_topics_for_day`, `report_date_counts`), add the live arm explicitly and list each in the test.
- [ ] **Step 3: Unit test** that asserts, by SQL text, every function in the list above contains `superseded_at IS NULL` (the same style as the search_sql tests: assert on text where a double cannot type-check).
- [ ] **Step 4: Integration test:** two passes of the same source key, first superseded; `list_topics_for_date` returns only the second; `search_chunks` never returns a chunk of the first; `has_topics_for_source` is true.

---

### Task 3: Supersede instead of delete in the item writer (and the two other delete sites)

**Files:**
- Modify: `src/repositories/topics.py` (add `supersede_topics_for_source`, `supersede_topics_for_source_prefix`; keep the delete functions for the deletion/expiry paths)
- Modify: `src/lambda_item_writer.py:995-1009`, `:891`, `_delete_member_topics`
- Modify: `src/lambda_ingest.py:657`, `:670`
- Modify: `src/lambda_nonwork_expiry.py` (its delete must also remove superseded rows of the expired topics, or they linger forever)
- Test: `tests/unit/test_item_writer_supersedes_not_deletes.py`, `tests/integration/test_supersede_two_passes.py`

- [ ] **Step 1:** `supersede_topics_for_source(conn, source_s3_key, run) -> list[dict]` executes `UPDATE topics SET superseded_at=now(), superseded_by_run=%s WHERE source_s3_key=%s AND superseded_at IS NULL RETURNING id, title, summary` and returns the rows it retired (Task 4 needs them). Same for the prefix form, with the existing `_escape_like`.
- [ ] **Step 2:** Replace the three delete calls. The `_warn_if_discarding_checkoffs` call stays for one release but its message changes to "N closed action items on rows being superseded" (it is now a count of what Task 4 must carry, not a loss). `run` = `f"{extraction.get('tier')}:{extraction.get('extracted_at')}"`.
- [ ] **Step 3:** `_source_is_deleted` ordering is unchanged: the deleted-source gate still runs after the advisory lock and before the supersede.
- [ ] **Step 4: Unit test:** replay the actual defect — a `_Conn` double records SQL; assert the writer issues `UPDATE topics SET superseded_at` and never `DELETE FROM topics` on the extraction path; the group path supersedes member keys. **Integration test:** live pass then final pass on one key → two topic rows, one superseded, both present; `nonwork_expiry` on the day removes both.
- [ ] **Step 5: Row growth note** in the module docstring: a session produces 2–4 passes, so topics rows grow ~3×. At current volume (hundreds of topics per site-month) this is nothing; the partial index keeps live reads at today's cost.

---

### Task 4: Carry-forward of stable ids and human edits

**Files:**
- Create: `src/carry_forward.py` (pure; no psycopg, no boto3)
- Modify: `src/lambda_item_writer.py` (after the topic loop, before `collected_topics` is used)
- Modify: `src/repositories/action_items.py`, `src/repositories/findings.py` (add `list_for_superseded_source`, `carry_identity`)
- Test: `tests/unit/test_carry_forward.py`, `tests/integration/test_tick_survives_final_pass.py`

**Interfaces:**
- `carry_forward.match(old: list[Item], new: list[Item], *, fuzzy_floor=0.90, tie_margin=0.05) -> (pairs: list[(old_id, new_id, how)], orphans: list[old_id])` where `Item = {id, stable_id, text, human_touched: bool}`.

- [ ] **Step 1: The matcher.** Normalise with `content_hash.normalize` (same function `compliance_resolutions` keys on, so the two notions of "same text" cannot drift). Pass 1: exact `content_hash` equality, one-to-one. Pass 2: for the remaining, `difflib.SequenceMatcher(None, a, b).ratio()` on the normalised strings; accept the best pair only if `ratio ≥ 0.90` **and** the runner-up for either side is more than `tie_margin` lower. CJK: strip whitespace inside CJK runs before comparing, exactly as `evidence_match` does — a bilingual product biases the floor otherwise. Everything else is an orphan.
- [ ] **Step 2: Apply in the writer.** Before the supersede (Task 3 Step 1 returns the old topic rows), load their children with `human_touched = (updated_by IS NOT NULL OR status <> 'open')` for action items and `(status <> 'open')` for findings; after the inserts, run `match` per child table, then `UPDATE <table> SET stable_id = old.stable_id, carried_from = old.id` on each pair, and for action items where the old row was human-touched also copy `status, priority, deadline, deadline_text, responsible, updated_by, updated_at`. Findings: copy `status` only; impact columns are re-derived by the matcher and must not be copied.
- [ ] **Step 3: Orphans a human touched** are counted and logged as `carry_forward: %d human-touched rows had no successor (key=%s)`, and emitted as a CloudWatch metric `OrphanedHumanEdits` through the same `put_metric_data` path `lambda_extraction_backlog` uses. They remain readable on the superseded topic; a later UI change can list them ("this item you ticked did not appear in the final extraction"). This replaces `_warn_if_discarding_checkoffs` and its test — rewrite `tests/unit/test_supersession_reports_lost_checkoffs.py` to assert the new behaviour and rename it.
- [ ] **Step 4: Unit tests, replaying real shapes** (take the strings from a real TEST extraction pair, names masked): identical text → exact pair; "Check the scaffolding before Monday" vs "Scaffolding to be checked before Monday" → fuzzy pair (record the actual ratio in the test); two new items both ≥0.90 against one old → orphan, not a guess; an untouched orphan is silent, a touched orphan is counted; CJK pair with different spacing → exact after normalisation.
- [ ] **Step 5: Integration test** (the defect itself): insert live pass, PATCH the action item `status='done', updated_by=<user>`, run the writer with a final pass whose text is reworded within the floor, assert the new row has the old `stable_id` and `status='done'`.

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

## Out of scope

`event_links` (edges), `claim_type`, `location`/`tags` tables, the Procore push, switching the org-api payload to the new decision/question rows, and any change to matcher thresholds — all Track C, after Track A's findings are read.
