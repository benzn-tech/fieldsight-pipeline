# Decisions and Questions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Store the `decisions` and `questions` the extractor already produces, and send them to the two surfaces that already have somewhere to put them.

**Architecture:** Two child tables mirroring `findings` (migration 0010), inserted by `lambda_item_writer` in the same transaction as findings, attached to topics by the same batched `list_for_topics` read, and serialised into the payload **as plain strings** because that is what both existing renderers and the other existing producer use.

**Tech Stack:** Postgres (Aurora), psycopg3, Python 3.11 Lambda, SAM.

**Spec:** `docs/superpowers/specs/2026-09-07-decisions-and-questions-are-discarded-design.md`

## Global Constraints

- **Payload values are STRINGS, never objects.** `topic-card.js:283-287` and `meeting-topic-card.js:244-247` each pass the entry straight to React as a child. An object raises "Objects are not valid as a React child" and the card stops rendering. `lambda_report_generator.py:182` already emits `"key_decisions": ["Decision attributed to person"]`.
- **No `status` column on either table, and no row `id` in the payload.** `lambda_item_writer.py:756` deletes and reinserts every child on each extraction pass, so both would churn or silently revert. See spec §5.
- **Insert is a per-row loop returning `RETURNING {_COLS}`,** copying `insert_findings` exactly. Only the read side is batched.
- **Never raise on malformed model output.** `insert_findings` uses defensive `.get` throughout and passes out-of-enum values as NULL rather than aborting the topic; both new repositories do the same.
- **`MSYS_NO_PATHCONV=1`** on any AWS CLI call carrying a `/`-prefixed argument (BUG-42).
- Migration numbering continues from `0053_photo_tombstones.sql`.

---

### Task 1: Migration

**Files:**
- Create: `src/migrations/0054_decisions_and_questions.sql`
- Test: `tests/unit/test_migration_0054_shape.py`

**Interfaces:**
- Produces: tables `decisions` and `questions`, both with `id`, `topic_id`, `site_id`, `created_at`; `decisions` additionally `decision`/`rationale`/`decided_by`; `questions` additionally `question`.

- [ ] **Step 1: Write the migration**

```sql
-- 0054: decisions + questions (spec docs/superpowers/specs/
-- 2026-09-07-decisions-and-questions-are-discarded-design.md).
-- The extractor has always produced both; nothing stored them, so
-- lambda_org_api sent a hardcoded "key_decisions": [].
--
-- NO status column on `questions`, deliberately. lambda_item_writer deletes
-- and reinserts every child on each extraction pass (including the routine
-- live->final pass over one session), so a human's "answered" would be
-- reverted by that session's own final extraction. The line above that
-- delete is _warn_if_discarding_checkoffs, which exists because the same
-- thing already happens to check-offs. Closing a question waits for a write
-- path that survives re-extraction.
--
-- Mirrors 0010_findings.sql, including ON DELETE CASCADE on site_id: a child
-- that FK-errors on site deletion while every sibling cascades is a deletion
-- path that fails halfway.
CREATE TABLE decisions (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  topic_id     uuid NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
  site_id      uuid NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  decision     text NOT NULL,
  rationale    text,
  decided_by   text,
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX decisions_topic_id_idx ON decisions (topic_id);

CREATE TABLE questions (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  topic_id     uuid NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
  site_id      uuid NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  question     text NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX questions_topic_id_idx ON questions (topic_id);
```

- [ ] **Step 2: Write the test**

```python
"""0054 must mirror 0010 on the two things that bite.

Not a schema-diff test for its own sake: both assertions are defects that
have already happened once in this repo. A missing CASCADE leaves a deletion
path that fails halfway, and a status column on a delete-and-reinsert row
silently reverts human edits (spec §5)."""
import pathlib

SQL = (pathlib.Path(__file__).parents[2] / "src" / "migrations"
       / "0054_decisions_and_questions.sql").read_text()


def test_both_foreign_keys_cascade_like_findings():
    for table in ("decisions", "questions"):
        block = SQL.split(f"CREATE TABLE {table}")[1].split(");")[0]
        assert "topic_id     uuid NOT NULL REFERENCES topics(id) ON DELETE CASCADE" in block
        assert "site_id      uuid NOT NULL REFERENCES sites(id) ON DELETE CASCADE" in block


def test_questions_has_no_status_column():
    # The review's finding, pinned: item_writer deletes and reinserts these
    # rows on every extraction pass, so a mutable status is reverted by the
    # session's own final pass.
    block = SQL.split("CREATE TABLE questions")[1].split(");")[0]
    assert "status" not in block
```

- [ ] **Step 3: Run it**

Run: `python -m pytest tests/unit/test_migration_0054_shape.py -v`
Expected: PASS (the migration is written before the test here because the test asserts on file text, not behaviour).

- [ ] **Step 4: Commit**

```bash
git add src/migrations/0054_decisions_and_questions.sql tests/unit/test_migration_0054_shape.py
git commit -m "Two tables for the two things the extractor already produces"
```

---

### Task 2: Repositories

**Files:**
- Create: `src/repositories/decisions.py`, `src/repositories/questions.py`
- Test: `tests/unit/test_decisions_questions_repo.py`

**Interfaces:**
- Consumes: Task 1's tables.
- Produces: `insert_decisions(conn, topic_id, site_id, decisions) -> list[dict]`, `insert_questions(conn, topic_id, site_id, questions) -> list[dict]`, and `list_for_topics(conn, topic_ids) -> list[dict]` on each module.

- [ ] **Step 1: Write the failing tests**

```python
"""The two repositories, against the same fake-conn harness findings uses.

The shapes under test are the ones the EXTRACTOR emits
(lambda_extract_session.py EXTRACTION_SCHEMA): decisions[] carry
{decision, rationale, decided_by} and questions[] carry {question}. Claude
output is never trusted to have the shape it was asked for, so every case
here feeds something malformed and asserts the insert still happens."""
import pytest
from repositories import decisions, questions


class FakeCur:
    def __init__(self): self.sql = []; self.params = []
    def execute(self, sql, params=None):
        self.sql.append(sql); self.params.append(params); return self
    def fetchone(self): return {"id": "row-1"}
    def fetchall(self): return []


class FakeConn:
    def __init__(self): self.cur = FakeCur()
    def cursor(self, **kw): return self.cur


def test_empty_input_executes_no_query():
    conn = FakeConn()
    assert decisions.insert_decisions(conn, "t1", "s1", []) == []
    assert questions.insert_questions(conn, "t1", "s1", []) == []
    assert conn.cur.sql == []


def test_one_row_per_decision_not_one_batched_statement():
    # Copies insert_findings, which loops. Asserted so a later "improvement"
    # to a single VALUES batch is a deliberate change, not a drift.
    conn = FakeConn()
    decisions.insert_decisions(conn, "t1", "s1", [
        {"decision": "a", "rationale": "r", "decided_by": "Ben"},
        {"decision": "b"},
    ])
    assert len(conn.cur.sql) == 2


def test_a_missing_field_is_null_not_a_crash():
    conn = FakeConn()
    decisions.insert_decisions(conn, "t1", "s1", [{"decision": "only"}])
    assert conn.cur.params[0][3] is None   # rationale
    assert conn.cur.params[0][4] is None   # decided_by


def test_a_non_dict_entry_does_not_abort_the_topic():
    # One malformed item must never take the whole topic's insert with it.
    conn = FakeConn()
    decisions.insert_decisions(conn, "t1", "s1", ["not a dict", {"decision": "ok"}])
    assert len(conn.cur.sql) == 1
    questions.insert_questions(conn, "t1", "s1", [None, {"question": "ok"}])
    assert len(conn.cur.sql) == 2


def test_a_decision_with_no_text_is_skipped_rather_than_inserted_null():
    # `decision` is NOT NULL in the schema, so inserting one would raise and
    # abort the transaction the whole topic is in.
    conn = FakeConn()
    decisions.insert_decisions(conn, "t1", "s1", [{"rationale": "why"}])
    assert conn.cur.sql == []


def test_list_for_topics_is_one_query_for_many_topics():
    conn = FakeConn()
    decisions.list_for_topics(conn, ["t1", "t2", "t3"])
    assert len(conn.cur.sql) == 1
    assert "ANY(%s)" in conn.cur.sql[0]


def test_list_for_topics_short_circuits_on_empty():
    conn = FakeConn()
    assert decisions.list_for_topics(conn, []) == []
    assert conn.cur.sql == []
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/unit/test_decisions_questions_repo.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'repositories.decisions'`

- [ ] **Step 3: Write `src/repositories/decisions.py`**

```python
"""Per-topic decisions (migration 0054).

Mirrors repositories/findings.py deliberately: same _COLS constant, same
per-row insert loop with RETURNING, same batched read. The extractor emits
{decision, rationale, decided_by} per lambda_extract_session.py's
EXTRACTION_SCHEMA, and none of those can be trusted to be present."""
from psycopg.rows import dict_row

_COLS = "id, topic_id, site_id, decision, rationale, decided_by, created_at"


def insert_decisions(conn, topic_id, site_id, decisions: list[dict]) -> list[dict]:
    """Insert one topic's decisions and return the new rows.

    Defensive throughout, for the same reason insert_findings is: this is
    Claude output. A non-dict entry is skipped rather than raising, and an
    entry with no `decision` text is skipped rather than inserted as NULL --
    the column is NOT NULL, so inserting one would abort the transaction the
    entire topic write lives in."""
    if not decisions:
        return []
    cur = conn.cursor(row_factory=dict_row)
    rows = []
    for d in decisions:
        if not isinstance(d, dict):
            continue
        text = d.get("decision")
        if not text:
            continue
        rows.append(cur.execute(
            f"INSERT INTO decisions (topic_id, site_id, decision, rationale, "
            f"decided_by) VALUES (%s,%s,%s,%s,%s) RETURNING {_COLS}",
            (topic_id, site_id, text, d.get("rationale"), d.get("decided_by")),
        ).fetchone())
    return rows


def list_for_topics(conn, topic_ids) -> list[dict]:
    """Batched read -- ONE query with ANY(%s) regardless of how many topics.

    The caller is the timeline render, which runs over every topic on a day;
    a per-topic lookup is the N+1 that list_topics_for_date already avoids
    for action_items and findings."""
    if not topic_ids:
        return []
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM decisions WHERE topic_id = ANY(%s) ORDER BY created_at",
        (list(topic_ids),)).fetchall()
```

- [ ] **Step 4: Write `src/repositories/questions.py`**

```python
"""Per-topic open questions (migration 0054).

No status column and no close endpoint -- see the spec's §5. The extractor
emits {question} per EXTRACTION_SCHEMA."""
from psycopg.rows import dict_row

_COLS = "id, topic_id, site_id, question, created_at"


def insert_questions(conn, topic_id, site_id, questions: list[dict]) -> list[dict]:
    """Insert one topic's questions and return the new rows. Same defensive
    posture as decisions.insert_decisions: a non-dict entry or one with no
    question text is skipped, never raised on."""
    if not questions:
        return []
    cur = conn.cursor(row_factory=dict_row)
    rows = []
    for q in questions:
        if not isinstance(q, dict):
            continue
        text = q.get("question")
        if not text:
            continue
        rows.append(cur.execute(
            f"INSERT INTO questions (topic_id, site_id, question) "
            f"VALUES (%s,%s,%s) RETURNING {_COLS}",
            (topic_id, site_id, text),
        ).fetchone())
    return rows


def list_for_topics(conn, topic_ids) -> list[dict]:
    """Batched read -- ONE query with ANY(%s). See decisions.list_for_topics."""
    if not topic_ids:
        return []
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM questions WHERE topic_id = ANY(%s) ORDER BY created_at",
        (list(topic_ids),)).fetchall()
```

- [ ] **Step 5: Run to verify they pass**

Run: `python -m pytest tests/unit/test_decisions_questions_repo.py -v`
Expected: PASS (7 tests)

- [ ] **Step 6: Revert-check**

Delete the `if not isinstance(d, dict): continue` line and re-run.
Expected: `test_a_non_dict_entry_does_not_abort_the_topic` FAILS. Restore it.

- [ ] **Step 7: Commit**

```bash
git add src/repositories/decisions.py src/repositories/questions.py tests/unit/test_decisions_questions_repo.py
git commit -m "Repositories for decisions and questions, shaped after findings"
```

---

### Task 3: Write path

**Files:**
- Modify: `src/lambda_item_writer.py` (immediately after the `insert_findings` call at :832)
- Test: `tests/unit/test_item_writer_decisions_questions.py`

**Interfaces:**
- Consumes: Task 2's `insert_decisions` / `insert_questions`.
- Produces: rows in both tables, in the same transaction as the topic upsert and the findings insert.

- [ ] **Step 1: Write the failing test**

```python
"""The write happens in the SAME transaction as findings, and legacy
extractions without the keys do not crash.

The second case is not hypothetical: the report/ingest path never has these
keys, and pre-#46 extraction JSON is still in S3. insert_findings handles the
same case with `t.get(...) or []`, and this must match."""


def test_decisions_and_questions_are_written_for_a_topic(writer_harness):
    writer_harness.run_extraction({"topics": [{
        "topic_title": "t", "decisions": [{"decision": "phase the doors"}],
        "questions": [{"question": "who is Alex"}]}]})
    assert writer_harness.inserted("decisions") == ["phase the doors"]
    assert writer_harness.inserted("questions") == ["who is Alex"]


def test_a_topic_with_neither_key_writes_nothing_and_does_not_raise(writer_harness):
    writer_harness.run_extraction({"topics": [{"topic_title": "t"}]})
    assert writer_harness.inserted("decisions") == []
    assert writer_harness.inserted("questions") == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/unit/test_item_writer_decisions_questions.py -v`
Expected: FAIL — nothing writes to either table yet.

Note: `writer_harness` does not exist. Build it in `tests/unit/conftest.py` by copying the fixture the existing findings write test uses; find it with `grep -rln "insert_findings" tests/`. **Do not name the new test file something that sorts before an existing one without checking** — test file alphabetical order is a real, load-bearing constraint in this repo (lambdas read env at import time).

- [ ] **Step 3: Wire the writer**

In `src/lambda_item_writer.py`, directly below the `finding_rows = findings.insert_findings(...)` call:

```python
            # Same transaction, same posture, same reason as findings above:
            # legacy extraction JSON and the report/ingest path have neither
            # key, so `or []` returns [] and the repositories no-op.
            decisions.insert_decisions(
                conn, row["id"], site["id"], t.get("decisions") or [])
            questions.insert_questions(
                conn, row["id"], site["id"], t.get("questions") or [])
```

and add `decisions, questions` to the `from repositories import ...` line that already imports `findings`.

Note: the return values are deliberately not captured. `finding_rows` is captured because the match_requests artifact needs the durable uuids; nothing downstream needs these.

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/unit/test_item_writer_decisions_questions.py -v`
Expected: PASS

- [ ] **Step 5: Run the whole suite**

Run: `python -m pytest tests/unit -q`
Expected: no new failures. If an unrelated test breaks, check whether the new file's name changed import order before assuming your change caused it.

- [ ] **Step 6: Commit**

```bash
git add src/lambda_item_writer.py tests/unit/test_item_writer_decisions_questions.py tests/unit/conftest.py
git commit -m "Store the decisions and questions the extraction already carries"
```

---

### Task 4: Read path — attach to topics

**Files:**
- Modify: `src/repositories/topics.py` at **all three** attach sites (near lines 489, 711, 794)
- Test: `tests/unit/test_topics_attaches_decisions_questions.py`

**Interfaces:**
- Consumes: Task 2's `list_for_topics`.
- Produces: `t["decisions"]` and `t["questions"]` on every topic dict every read path returns.

- [ ] **Step 1: Write the failing test**

```python
"""All THREE attach sites, not one.

topics.py attaches findings in three places (list_topics_for_date, the
second aggregate read, and the single-topic read). Wiring one leaves the
payload's shape dependent on which read path served it -- a topic that has
decisions on the timeline and none on the topic page, with nothing in the
logs. Grep is the assertion here because the three call sites are the
defect, not the mapping inside any one of them."""
import pathlib

SRC = (pathlib.Path(__file__).parents[2] / "src" / "repositories"
       / "topics.py").read_text()


def test_every_findings_attach_site_has_a_decisions_and_questions_sibling():
    assert SRC.count("findings.list_for_topics") \
        == SRC.count("decisions.list_for_topics") \
        == SRC.count("questions.list_for_topics")


def test_every_topic_dict_that_gets_findings_also_gets_both():
    assert SRC.count('t["findings"]') \
        == SRC.count('t["decisions"]') == SRC.count('t["questions"]')
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/unit/test_topics_attaches_decisions_questions.py -v`
Expected: FAIL — counts are 3, 0, 0.

- [ ] **Step 3: Wire all three sites**

At each place `findings.list_for_topics` is called and grouped by topic id, add the same grouping for both new repositories, and set `t["decisions"]` / `t["questions"]` beside the existing `t["findings"]`. Add `decisions, questions` to the `from repositories import findings` line at the top.

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/unit/test_topics_attaches_decisions_questions.py tests/unit -q`
Expected: PASS, no new failures.

- [ ] **Step 5: Commit**

```bash
git add src/repositories/topics.py tests/unit/test_topics_attaches_decisions_questions.py
git commit -m "Attach decisions and questions wherever findings attach"
```

---

### Task 5: Payload — strings

**Files:**
- Modify: `src/lambda_org_api.py` (the topic dict at ~:5850, replacing the hardcoded `"key_decisions": []`)
- Test: `tests/unit/test_payload_decisions_are_strings.py`

**Interfaces:**
- Consumes: Task 4's `t["decisions"]` / `t["questions"]`.
- Produces: `key_decisions: list[str]`, `open_questions: list[str]` on every topic in the payload.

- [ ] **Step 1: Write the failing test**

```python
"""Strings, because two renderers and one other producer already say so.

topic-card.js:283-287 and meeting-topic-card.js:244-247 each pass the entry
straight to React as a child; an object raises "Objects are not valid as a
React child" and the card stops rendering. lambda_report_generator.py:182
emits `"key_decisions": ["Decision attributed to person"]`. The first draft
of this spec proposed objects, which is why this test exists."""
from lambda_org_api import render_report_shape   # adjust to the real entry point


def test_key_decisions_are_plain_strings(one_topic_with_children):
    out = render_report_shape(one_topic_with_children)
    values = out["topics"][0]["key_decisions"]
    assert values == ["phase the doors"]
    assert all(isinstance(v, str) for v in values)


def test_open_questions_are_plain_strings(one_topic_with_children):
    values = render_report_shape(one_topic_with_children)["topics"][0]["open_questions"]
    assert values == ["who is Alex"]
    assert all(isinstance(v, str) for v in values)


def test_a_topic_with_none_gets_empty_lists_not_missing_keys(topic_with_no_children):
    out = render_report_shape(topic_with_no_children)["topics"][0]
    assert out["key_decisions"] == []
    assert out["open_questions"] == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/unit/test_payload_decisions_are_strings.py -v`
Expected: FAIL — `key_decisions` is `[]` and `open_questions` is absent.

- [ ] **Step 3: Replace the hardcoded line**

```python
            # STRINGS, not objects. topic-card.js:283-287 passes each entry
            # straight to React as a child, and lambda_report_generator.py:182
            # -- the other producer of this key -- already emits strings.
            # `rationale` and `decided_by` are stored but not sent: no renderer
            # reads them, and widening the contract to carry fields nobody
            # consumes is inventing a consumer.
            "key_decisions": [d["decision"] for d in t["decisions"]],
            # No row id here on purpose: item-writer deletes and reinserts
            # these on every extraction pass, so an id would change under any
            # caller that stored it (spec §5).
            "open_questions": [q["question"] for q in t["questions"]],
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/unit/test_payload_decisions_are_strings.py tests/unit -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/lambda_org_api.py tests/unit/test_payload_decisions_are_strings.py
git commit -m "Send the decisions and questions as the strings both renderers read"
```

---

### Task 6: The customer-facing minutes stop claiming there were none

**Files:**
- Modify: `src/lambda_session_report.py:121`
- Test: `tests/unit/test_session_report_open_questions.py`

**Interfaces:**
- Consumes: Task 5's `open_questions` on each topic of `content`.

- [ ] **Step 1: Write the failing test**

```python
"""Every minutes document has been stating that the session raised no open
questions, because the field was hardcoded [] with no comment -- three lines
above a `photo_streams` line that carries an explicit one about
absent-versus-empty."""
from lambda_session_report import _content_to_minutes


def test_open_questions_come_from_the_content_not_from_a_literal():
    artifact = {"content": {"topics": [
        {"topic_title": "t", "open_questions": ["who is Alex"]}]}}
    assert _content_to_minutes(artifact)["topics"][0]["open_questions"] == ["who is Alex"]


def test_a_topic_without_the_key_still_yields_an_empty_list():
    artifact = {"content": {"topics": [{"topic_title": "t"}]}}
    assert _content_to_minutes(artifact)["topics"][0]["open_questions"] == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/unit/test_session_report_open_questions.py -v`
Expected: FAIL on the first test — the literal `[]` wins.

- [ ] **Step 3: Read it from the content**

```python
            "open_questions": t.get("open_questions") or [],
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/unit/test_session_report_open_questions.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/lambda_session_report.py tests/unit/test_session_report_open_questions.py
git commit -m "The minutes stop claiming a session raised no questions"
```

---

### Task 7 (other repo — `fieldsight-ui`): render `open_questions` on the daily path

**Files:**
- Modify: `scripts/pages/timeline.js` (topic detail, beside the existing Findings section)
- Modify: `scripts/mock/daily-report.fixture.js`
- Test: browser

`key_decisions` needs **no** frontend change — `topic-card.js` already renders it and will simply start having content. `open_questions` has a renderer only on the meeting-minutes path (`meeting-topic-card.js`), so the daily/timeline topic detail needs one.

- [ ] **Step 1: Add `open_questions` to a fixture topic**

No fixture carries one, so the section would never render locally — the same absent-mock trap that hid the Findings section and the "raised N×" badge. Add two plain strings to the 04-28 topic.

- [ ] **Step 2: Render the section**

Mirror the existing Findings section in `timeline.js`: a `fs-topic-detail__section` with a `section-label` reading `Open questions` and one `<li>` per string. Strings, so no `EditableText` — these are not correctable content in v1.

- [ ] **Step 3: Bump the cache buster**

`?v=` for `scripts/pages/timeline.js` in `app-shell-preview.html`. Without it a local reload serves the cached file and the change looks like it did not work — this cost a debugging round on 2026-09-07.

- [ ] **Step 4: Verify in the browser**

Serve on an unused port (check with `curl` first — a port held by another session serves a *different directory* and reads as a stale file), open `?dev=1#/timeline?date=2026-04-28&user=Jarley_Trainor`, click the topic title, and read the DOM for the new section.

- [ ] **Step 5: Commit and PR to `dev`**

---

## Deployment order

Tasks 1–6 ship together on `develop` → TEST. **The migration must reach TEST before item-writer does**, which the SAM deploy handles in one run, but a partial merge would not — do not split Task 1 into its own PR.

Task 7 is independent and safe to ship first or last: an absent `open_questions` key renders nothing.

**No prod deploy without checking the parallel sessions' state** — `main`'s relationship to `develop` shifts overnight, and migrations merged to `main` run against prod on the nightly deploy.
