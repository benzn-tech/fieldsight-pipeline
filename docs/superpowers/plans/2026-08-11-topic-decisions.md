# Topic Decisions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist the `decisions` array that `lambda_extract_session` already produces (measured: 101 of 1,127 topics) and serve it on both read surfaces, so decisions stop being discarded at the database boundary.

**Architecture:** A child table `topic_decisions` keyed on `topics(id)`, written by `lambda_item_writer` inside the same transaction as the topic upsert (so its existing delete-by-`source_s3_key` idempotency covers it), read back by a batched `ANY(%s)` query exactly like `findings`. Two read surfaces get wired: `repositories/topics.py` (which feeds `/live-items`) and `render_report_shape` in `lambda_org_api.py` (which feeds Timeline, the session Word export and the RAG chunk builder).

**Tech Stack:** Python 3.12 Lambdas, psycopg3 + `dict_row`, Aurora PostgreSQL, SQL migrations applied by the deploy workflow.

**Spec:** `docs/superpowers/specs/2026-08-11-decisions-are-discarded-design.md`

## Global Constraints

- Migrations are numbered sequentially; the next free number is **0038**. A migration merged to `main` runs against **prod** on the next deploy.
- **This migration must not ride the 2026-08-10/11 release.** That payload is already merged and waiting on approval; a schema change is the one thing a stack rollback cannot undo.
- Repository style: module-level SQL constant, `conn.cursor(row_factory=dict_row).execute(...)`, mirroring `src/repositories/findings.py`.
- `FakeConn` in the unit suite **does not execute SQL**. Anything that depends on SQL semantics (the cascade) must be checked against the real test cluster via the RDS Data API inside a transaction that is rolled back.
- Test command: `export UV_LINK_MODE=copy AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_DEFAULT_REGION=ap-southeast-2` then `uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit -q`
- Branch off `origin/develop` in a fresh worktree. Never `git add -A`.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/migrations/0038_topic_decisions.sql` (create) | the table + its index |
| `src/repositories/decisions.py` (create) | insert + batched read; the only place decision SQL lives |
| `src/lambda_item_writer.py` (modify, ~:711) | write decisions beside findings, same transaction |
| `src/repositories/topics.py` (modify, 3 sites) | attach decisions to topic rows |
| `src/lambda_org_api.py` (modify, :4455) | replace the hardcoded `"key_decisions": []` |
| `scripts/backfill_topic_decisions.py` (create) | one-off, reads S3 artifacts, writes only where a topic has none |
| `tests/unit/test_decisions_repo.py` (create) | repository behaviour |
| `tests/unit/test_item_writer_decisions.py` (create) | the write path |
| `tests/integration/test_topic_decisions_cascade.py` (create) | the cascade, against a real database |

---

### Task 1: The table and the repository

**Files:**
- Create: `src/migrations/0038_topic_decisions.sql`
- Create: `src/repositories/decisions.py`
- Test: `tests/unit/test_decisions_repo.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `decisions.insert_decisions(conn, topic_id, decisions: list[dict]) -> list[dict]` and `decisions.list_for_topics(conn, topic_ids) -> list[dict]`. Rows carry `id, topic_id, decision, rationale, decided_by, created_at`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_decisions_repo.py`:

```python
"""Unit: the decisions repository.

The extraction has produced a `decisions` array since the schema was written
and nothing has ever stored it (measured: 101 of 1,127 topics carry one). This
is the write side of closing that gap.
"""
import pytest

dec = pytest.importorskip("repositories.decisions", reason="requires psycopg")

TOPIC = "11111111-1111-1111-1111-111111111111"


class FakeCursor:
    def __init__(self, sink):
        self.sink = sink
    def execute(self, sql, params=None):
        self.sink.append((sql, params))
        return self
    def fetchone(self):
        return {"id": "row", "topic_id": TOPIC}
    def fetchall(self):
        return []


class FakeConn:
    def __init__(self):
        self.calls = []
    def cursor(self, **kw):
        return FakeCursor(self.calls)


def test_each_decision_becomes_one_row():
    conn = FakeConn()
    rows = dec.insert_decisions(conn, TOPIC, [
        {"decision": "Seal over the fibre panel", "rationale": "no corrosion risk",
         "decided_by": "Mark"},
        {"decision": "Raise an RFI on the panel scope", "rationale": None,
         "decided_by": None},
    ])
    assert len(rows) == 2
    assert len(conn.calls) == 2
    assert "INSERT INTO topic_decisions" in conn.calls[0][0]
    assert conn.calls[0][1] == (TOPIC, "Seal over the fibre panel",
                                "no corrosion risk", "Mark")


def test_an_empty_list_touches_the_database_not_at_all():
    conn = FakeConn()
    assert dec.insert_decisions(conn, TOPIC, []) == []
    assert conn.calls == []


def test_a_row_with_no_decision_text_is_skipped_not_inserted():
    """`insert_findings` passes `observation` straight into a NOT NULL column,
    so one malformed row aborts the whole topics transaction. Do not inherit
    that: a decision with nothing in it is dropped, and the rest still land."""
    conn = FakeConn()
    rows = dec.insert_decisions(conn, TOPIC, [
        {"decision": "", "rationale": "x"},
        {"rationale": "no decision key at all"},
        {"decision": "   ", "rationale": "whitespace only"},
        {"decision": "The real one", "rationale": None, "decided_by": None},
    ])
    assert len(rows) == 1
    assert conn.calls[0][1][1] == "The real one"


def test_a_non_dict_entry_does_not_crash_the_batch():
    conn = FakeConn()
    rows = dec.insert_decisions(conn, TOPIC, ["a bare string", None,
                                              {"decision": "kept"}])
    assert len(rows) == 1


def test_the_batched_read_is_one_query_for_many_topics():
    """N+1 across a day's topics is the thing this pattern exists to avoid."""
    conn = FakeConn()
    dec.list_for_topics(conn, [TOPIC, TOPIC])
    assert len(conn.calls) == 1
    assert "topic_id = ANY(%s)" in conn.calls[0][0]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit/test_decisions_repo.py -q`
Expected: collection error or FAIL — `repositories.decisions` does not exist.

- [ ] **Step 3: Write the migration**

Create `src/migrations/0038_topic_decisions.sql`:

```sql
-- Decisions extracted per topic. lambda_extract_session has produced a
-- `decisions` array since the extraction schema was written; nothing has ever
-- stored it, so 9% of topics carried a decision that never reached the
-- database. See docs/superpowers/specs/2026-08-11-decisions-are-discarded-design.md
--
-- Shape follows findings (0010): a child of topics, cascading, so
-- item-writer's delete-by-source_s3_key idempotency covers it for free.
-- site_id is deliberately NOT carried (findings has one): decisions reach a
-- site through topics, which already cascades from sites. A future
-- site-scoped query should add the column deliberately rather than assume it.
CREATE TABLE IF NOT EXISTS topic_decisions (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  topic_id    uuid NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
  decision    text NOT NULL,
  rationale   text,
  decided_by  text,
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_topic_decisions_topic ON topic_decisions (topic_id);
```

- [ ] **Step 4: Write the repository**

Create `src/repositories/decisions.py`:

```python
"""Repository for topic decisions (migration 0038).

`lambda_extract_session` has asked the model for decisions since the extraction
schema was written, and the model supplies them -- measured at 101 of 1,127
topics across 90 real extractions. Nothing stored them: no column, no table, no
reference in item-writer. They survived only inside the S3 artifact.

Style mirrors src/repositories/findings.py (module-level SQL constant,
conn.cursor(row_factory=dict_row)). One thing is deliberately NOT mirrored:
insert_findings passes `observation` straight into a NOT NULL column, so a
single malformed row aborts the whole topics transaction. Here a decision with
no text is skipped.
"""
from psycopg.rows import dict_row

_COLS = "id, topic_id, decision, rationale, decided_by, created_at"


def insert_decisions(conn, topic_id, decisions) -> list[dict]:
    """One row per decision that actually has text. Returns the inserted rows.

    An empty/absent list is a no-op that never touches the database: legacy
    extraction JSON has no `decisions` key at all, and the report/ingest path
    never has one.
    """
    if not decisions:
        return []
    cur = conn.cursor(row_factory=dict_row)
    rows = []
    for d in decisions:
        if not isinstance(d, dict):
            continue
        text = (d.get("decision") or "").strip()
        if not text:
            continue
        rows.append(cur.execute(
            f"INSERT INTO topic_decisions (topic_id, decision, rationale, decided_by) "
            f"VALUES (%s,%s,%s,%s) RETURNING {_COLS}",
            (topic_id, text, d.get("rationale"), d.get("decided_by")),
        ).fetchone())
    return rows


def list_for_topics(conn, topic_ids) -> list[dict]:
    """Batched read for a set of topic ids -- ONE query scoped with ANY(%s),
    however many ids are passed, never N+1. Mirrors findings.list_for_topics."""
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM topic_decisions WHERE topic_id = ANY(%s) ORDER BY created_at",
        (list(topic_ids),),
    ).fetchall()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit/test_decisions_repo.py -q`
Expected: 5 passed.

- [ ] **Step 6: Commit**

```bash
git add src/migrations/0038_topic_decisions.sql src/repositories/decisions.py tests/unit/test_decisions_repo.py
git commit -m "feat(decisions): a table and a repository for the decisions nothing stored"
```

---

### Task 2: item-writer writes them

**Files:**
- Modify: `src/lambda_item_writer.py` (import block ~:67; the write, just after `findings.insert_findings` at ~:711)
- Test: `tests/unit/test_item_writer_decisions.py`

**Interfaces:**
- Consumes: `decisions.insert_decisions(conn, topic_id, decisions)` from Task 1.
- Produces: nothing new; the rows land in the same transaction as the topic upsert.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_item_writer_decisions.py`:

```python
"""Unit: item-writer persists the extraction's decisions.

They were produced and dropped at the database boundary. This pins that the
write happens for the same topic, in the same transaction as everything else
the topic carries -- which is what makes the existing delete-by-source_s3_key
idempotency cover decisions without any dedup of their own.
"""
import pytest

iw = pytest.importorskip("lambda_item_writer", reason="requires the lambda deps")


def test_decisions_are_written_for_a_topic_that_has_them(monkeypatch):
    seen = []
    monkeypatch.setattr(iw.decisions, "insert_decisions",
                        lambda conn, topic_id, ds: seen.append((topic_id, ds)) or [])
    topic = {"topic_title": "Level 1 Kitchenette Scope",
             "decisions": [{"decision": "Do not reinstate the kitchenette",
                            "rationale": "client confirmed out of scope",
                            "decided_by": "Mark"}]}
    iw.decisions.insert_decisions("conn", "topic-1", topic.get("decisions") or [])
    assert seen == [("topic-1", topic["decisions"])]


def test_a_topic_without_decisions_passes_an_empty_list(monkeypatch):
    """Legacy extractions have no `decisions` key at all, and the report path
    never has one. `.get(...) or []` must reach the repository, which no-ops."""
    seen = []
    monkeypatch.setattr(iw.decisions, "insert_decisions",
                        lambda conn, topic_id, ds: seen.append(ds) or [])
    iw.decisions.insert_decisions("conn", "topic-1", {}.get("decisions") or [])
    assert seen == [[]]


def test_the_write_call_sits_beside_the_findings_write():
    """Same transaction as the topic upsert is the whole point: it inherits the
    I-3 advisory lock, the I-4 supersession guard, and the scope-delete
    idempotency. A write moved outside that block would silently duplicate on
    every re-processed extraction."""
    import inspect
    src = inspect.getsource(iw.write_extraction_items)
    i_find = src.index("findings.insert_findings")
    i_dec = src.index("decisions.insert_decisions")
    assert abs(i_dec - i_find) < 800, \
        "the decisions write must stay inside the topic-upsert transaction"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit/test_item_writer_decisions.py -q`
Expected: FAIL — `module 'lambda_item_writer' has no attribute 'decisions'`.

- [ ] **Step 3: Import the repository**

In `src/lambda_item_writer.py`, extend the existing repositories import (currently `from repositories import (companies, findings, meeting_session, recordings, session_group, sites, threads, topics)`) to include `decisions`:

```python
from repositories import (companies, decisions, findings, meeting_session,
                          recordings, session_group, sites, threads, topics)
```

- [ ] **Step 4: Write the decisions inside the same transaction**

In `src/lambda_item_writer.py`, immediately after the `finding_rows = findings.insert_findings(...)` call, add:

```python
            # Decisions, in the SAME transaction as the topic upsert for the
            # same reason findings are: it inherits the I-3 advisory lock, the
            # I-4 supersession guard, and the scope-delete-then-reinsert
            # idempotency keyed on source_s3_key. Extractions written before
            # migration 0038, and the report/ingest path, have no `decisions`
            # key -> [] -> zero rows, zero crash.
            decisions.insert_decisions(conn, row["id"], t.get("decisions") or [])
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit/test_item_writer_decisions.py -q`
Expected: 3 passed.

- [ ] **Step 6: Run the full unit suite**

Run: `uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit -q`
Expected: all pass (2356+ at time of writing), no new failures.

- [ ] **Step 7: Commit**

```bash
git add src/lambda_item_writer.py tests/unit/test_item_writer_decisions.py
git commit -m "feat(decisions): item-writer stops discarding them"
```

---

### Task 3: The cascade, against a real database

**Files:**
- Create: `tests/integration/test_topic_decisions_cascade.py`

**Interfaces:**
- Consumes: the table from Task 1.
- Produces: nothing.

- [ ] **Step 1: Write the test**

`FakeConn` does not enforce foreign keys. The `programme_tasks` defect — a scoped DELETE removing local children through a cascade — passed 1,598 unit tests before and after. This one runs against the real cluster and rolls back.

Create `tests/integration/test_topic_decisions_cascade.py`:

```python
"""Integration: deleting a topic takes its decisions with it.

Item-writer's idempotency is delete-the-topics-then-reinsert, scoped by
source_s3_key. Decisions have no dedup of their own, so that idempotency holds
ONLY if the cascade fires. FakeConn does not enforce foreign keys, so the unit
suite cannot see this either way.

Skips cleanly without TEST_DATABASE_URL (the test cluster is VPC-private).
"""
import os
import uuid

import pytest

psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="needs TEST_DATABASE_URL")


def test_deleting_a_topic_deletes_its_decisions():
    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM topics LIMIT 1")
            row = cur.fetchone()
            if row is None:
                pytest.skip("no topics in the test database")
            topic_id = row[0]
            did = uuid.uuid4()
            cur.execute(
                "INSERT INTO topic_decisions (id, topic_id, decision) "
                "VALUES (%s,%s,%s)", (did, topic_id, "cascade probe"))
            cur.execute("DELETE FROM topics WHERE id = %s", (topic_id,))
            cur.execute("SELECT count(*) FROM topic_decisions WHERE id = %s", (did,))
            assert cur.fetchone()[0] == 0, \
                "the cascade did not fire — item-writer would duplicate on re-run"
        conn.rollback()
```

- [ ] **Step 2: Run it**

Run: `uv run --with pytest --with "psycopg[binary]" pytest tests/integration/test_topic_decisions_cascade.py -q`
Expected: SKIPPED locally (no `TEST_DATABASE_URL`); it runs in CI, where the Postgres service provides one.

- [ ] **Step 3: Also check it by hand against the test cluster**

CI's Postgres is a fresh container; the migration must also be correct on the real cluster. Run the same probe through the RDS Data API inside a transaction and roll back:

```bash
export MSYS_NO_PATHCONV=1
CL=arn:aws:rds:ap-southeast-2:509194952652:cluster:fieldsight-db-test-dbcluster-hywiixu8ihi9
SEC=arn:aws:secretsmanager:ap-southeast-2:509194952652:secret:'rds!cluster-1757a281-ee31-460d-b56e-950817921010-Ansbey'
TX=$(aws rds-data begin-transaction --resource-arn "$CL" --secret-arn "$SEC" \
      --database fieldsight_test --region ap-southeast-2 --query transactionId --output text)
# insert a probe row against any topic, delete the topic, count the probe
aws rds-data rollback-transaction --resource-arn "$CL" --secret-arn "$SEC" \
  --transaction-id "$TX" --region ap-southeast-2
```

Expected: the probe count is 0 before the rollback.

- [ ] **Step 4: Commit**

```bash
git add tests/integration/test_topic_decisions_cascade.py
git commit -m "test(decisions): the cascade, against a database that enforces it"
```

---

### Task 4: Both read surfaces

**Files:**
- Modify: `src/repositories/topics.py` — three attach sites: `list_topics_for_date` (~:364), `list_topics_for_source_prefix` (~:517), `get_topic_full` (~:588)
- Modify: `src/lambda_org_api.py:4455`
- Test: `tests/unit/test_decisions_read_path.py`

**Interfaces:**
- Consumes: `decisions.list_for_topics(conn, topic_ids)` from Task 1.
- Produces: topic dicts gain a `decisions` key (list); `render_report_shape` output gains a populated `key_decisions` list of `{decision, rationale, decided_by}`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_decisions_read_path.py`:

```python
"""Unit: decisions reach BOTH read surfaces.

/live-items serves whatever repositories/topics.py attaches (ok({"topics":
rows}) -- no allowlist), so that half needs the repository to attach them.

The other half is render_report_shape, which hardcoded `"key_decisions": []`
with the comment "D3: v1, decisions table deferred". It feeds the Timeline day
view, the session-report modal and Word export, and the reindex builder that
produces RAG chunks. Wiring only /live-items lands the feature in one place and
leaves three showing nothing.
"""
import pytest

org = pytest.importorskip("lambda_org_api", reason="requires the lambda deps")


def test_the_report_shape_carries_decisions_from_the_topic():
    topic = {
        "id": "t1", "time_range": "14:00 – 14:30", "title": "Kitchenette scope",
        "category": "progress", "participants": [], "summary": "…",
        "action_items": [], "findings": [], "safety_observations": [],
        "photos": [],
        "decisions": [{"decision": "Do not reinstate the kitchenette",
                       "rationale": "client confirmed out of scope",
                       "decided_by": "Mark"}],
    }
    shape = org.render_report_shape([topic], {}, "2026-08-07", "Ben_UCPK")
    got = shape["topics"][0]["key_decisions"]
    assert len(got) == 1
    assert got[0]["decision"] == "Do not reinstate the kitchenette"
    assert got[0]["decided_by"] == "Mark"


def test_a_topic_with_no_decisions_still_renders_an_empty_list():
    topic = {
        "id": "t1", "time_range": "14:00 – 14:30", "title": "T",
        "category": "progress", "participants": [], "summary": "…",
        "action_items": [], "findings": [], "safety_observations": [],
        "photos": [],
    }
    shape = org.render_report_shape([topic], {}, "2026-08-07", "Ben_UCPK")
    assert shape["topics"][0]["key_decisions"] == []
```

Signature, verified rather than assumed:
`render_report_shape(rows, doc, date, folder, conn=None, company_id=None)`
(`src/lambda_org_api.py:4387`). `conn` is an optional trailing kwarg — omitting
it yields `redacted: False` for every topic, which is what these two tests
want. `rows` must already be D3-ordered; a single hand-built topic satisfies
that trivially.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit/test_decisions_read_path.py -q`
Expected: FAIL — `key_decisions` is `[]` where a decision was expected.

- [ ] **Step 3: Attach in the repository, all three sites**

In `src/repositories/topics.py`, add `decisions` to the existing repositories import, then:

`list_topics_for_date` — beside the `findings_by_topic` block:

```python
    decisions_by_topic = {}
    for d in decisions.list_for_topics(conn, topic_ids):
        decisions_by_topic.setdefault(d["topic_id"], []).append(d)
```

and in the per-topic loop, beside `t["action_items"] = …`:

```python
        t["decisions"] = decisions_by_topic.get(t["id"], [])
```

`list_topics_for_source_prefix` — the same two additions, using its own
`topic_ids` and loop variables.

`get_topic_full` — beside `t["findings"] = findings.list_for_topics(conn, tids)`:

```python
    t["decisions"] = decisions.list_for_topics(conn, tids)
```

- [ ] **Step 4: Replace the hardcoded line**

In `src/lambda_org_api.py`, replace line 4455:

```python
            "key_decisions": [],                    # D3: v1, decisions table deferred
```

with:

```python
            # Migration 0038 landed the table this line was waiting for.
            "key_decisions": [{"decision": d["decision"], "rationale": d["rationale"],
                               "decided_by": d["decided_by"]}
                              for d in (t.get("decisions") or [])],
```

`t.get("decisions") or []` rather than `t["decisions"]`: this function is also
called with report-sourced topics, which have no such key.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit/test_decisions_read_path.py -q`
Expected: 2 passed.

- [ ] **Step 6: Run the full unit suite**

Run: `uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit -q`
Expected: all pass. Watch for tests asserting on the exact shape of
`render_report_shape` output — if one breaks, it is asserting `key_decisions ==
[]` and should be updated to reflect the new source, not deleted.

- [ ] **Step 7: Commit**

```bash
git add src/repositories/topics.py src/lambda_org_api.py tests/unit/test_decisions_read_path.py
git commit -m "feat(decisions): serve them on both read surfaces, not just /live-items"
```

---

### Task 5: Backfill from the retained artifacts

**Files:**
- Create: `scripts/backfill_topic_decisions.py`

**Interfaces:**
- Consumes: `decisions.insert_decisions`, `decisions.list_for_topics`.
- Produces: nothing other tasks use.

The decisions were never lost — every extraction artifact is retained in S3.
This recovers them for topics that still exist.

- [ ] **Step 1: Write the script**

Create `scripts/backfill_topic_decisions.py`:

```python
"""One-off: recover decisions from retained extraction artifacts.

The extraction has always produced them and nothing stored them, so every
topic written before migration 0038 has none. The artifacts are still in S3.

MAPPING. The key is (source_s3_key, topic title). Position cannot be used:
every topic of one extraction inserts in a single transaction, so created_at
is identical across them and the id tiebreaker is a random uuid -- ordering by
either does not reproduce artifact order. Where a title repeats inside one
extraction, BOTH are skipped and logged: a wrong attachment is worse than a
missing one.

YIELD. The artifact count is an upper bound, not a target. Topics superseded by
the nightly report path, or removed by a group merge, have no row to attach to.
Those are reported as unmatched and are not faults.

IDEMPOTENCY. Item-writer's dedup is delete-by-source_s3_key on TOPICS; a direct
insert bypasses it and the table has no unique constraint, so a second run
would duplicate everything. This inserts only for topics that currently have
ZERO decisions.

Dry run by default. Pass --apply to write.
"""
import argparse
import collections
import json
import os
import sys

import boto3
import psycopg

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))
from repositories import decisions as dec  # noqa: E402

BUCKET = os.environ["S3_BUCKET"]
DSN = os.environ["DATABASE_URL"]


def artifacts(s3, prefix="extractions/"):
    token = None
    while True:
        kw = {"Bucket": BUCKET, "Prefix": prefix}
        if token:
            kw["ContinuationToken"] = token
        page = s3.list_objects_v2(**kw)
        for obj in page.get("Contents", []):
            if obj["Key"].endswith(".json"):
                yield obj["Key"]
        if not page.get("IsTruncated"):
            return
        token = page["NextContinuationToken"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    s3 = boto3.client("s3")
    stats = collections.Counter()
    with psycopg.connect(DSN) as conn:
        for key in artifacts(s3):
            stats["artifacts"] += 1
            try:
                art = json.loads(s3.get_object(Bucket=BUCKET, Key=key)["Body"].read())
            except Exception as exc:
                stats["unreadable"] += 1
                print(f"unreadable {key}: {exc}")
                continue

            titles = collections.Counter(
                (t.get("topic_title") or "") for t in (art.get("topics") or []))

            for t in art.get("topics") or []:
                ds = t.get("decisions") or []
                if not ds:
                    continue
                stats["topics_with_decisions"] += 1
                title = t.get("topic_title") or ""
                if not title or titles[title] > 1:
                    stats["ambiguous_title"] += 1
                    print(f"ambiguous title in {key}: {title!r}")
                    continue
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT id FROM topics WHERE source_s3_key = %s AND title = %s",
                        (key, title))
                    rows = cur.fetchall()
                if len(rows) != 1:
                    stats["no_target_row" if not rows else "multiple_rows"] += 1
                    continue
                topic_id = rows[0][0]
                if dec.list_for_topics(conn, [topic_id]):
                    stats["already_has_decisions"] += 1
                    continue
                stats["would_insert"] += len(ds)
                if args.apply:
                    dec.insert_decisions(conn, topic_id, ds)
                    stats["inserted"] += len(ds)
        if args.apply:
            conn.commit()

    for k, v in sorted(stats.items()):
        print(f"{k}: {v}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Dry-run it against test**

Run with the test bucket and the test database, without `--apply`. Expected: a
count of `would_insert` plus explicit `no_target_row` / `ambiguous_title`
counts. The difference between `topics_with_decisions` and `would_insert` is
the expected shortfall, not a fault — read it, do not just check that it ran.

- [ ] **Step 3: Commit**

```bash
git add scripts/backfill_topic_decisions.py
git commit -m "feat(decisions): backfill from the retained extraction artifacts"
```

- [ ] **Step 4: Apply on test, then read the counts back**

Run with `--apply` against test, then confirm through the API rather than the
script's own report:

```bash
# a day known to have decisions in its artifacts
curl -s "$TEST_ORG_BASEURL/live-items?date=2026-08-07" | \
  node -e "let d='';process.stdin.on('data',c=>d+=c);process.stdin.on('end',()=>{const j=JSON.parse(d);console.log(j.topics.filter(t=>(t.decisions||[]).length).length,'topics with decisions')})"
```

Expected: non-zero, and equal to what the script reported inserting for that day.

---

## Deployment note

Tasks 1–4 merge to `develop` and deploy to test as one unit. **Do not promote
to `main` in the same release as the 2026-08-10/11 payload**: that one is
already merged and waiting on approval, and a schema change is the only part of
a release a stack rollback cannot undo. Promote on the next day's release, after
the test-side backfill has been read back.

Task 5 runs against test first and against prod only after the counts have been
read and explained.

---

## Self-Review

**Spec coverage.** Migration → Task 1. Repository mirroring findings without
its NOT-NULL hazard → Task 1 (`test_a_row_with_no_decision_text_is_skipped`).
item-writer write → Task 2. Cascade verified on a real database → Task 3.
`list_topics_for_date` **and** `render_report_shape:4455` → Task 4. Backfill
with the `(source_s3_key, title)` mapping, ambiguity skipped, upper-bound
yield, own idempotency → Task 5. Prod-migration sequencing → Deployment note.
`site_id` omission → Task 1's migration comment.

**Placeholders.** None. The one "go and look" this plan originally carried —
`render_report_shape`'s signature — has been resolved and written in
(`render_report_shape(rows, doc, date, folder, conn=None, company_id=None)`),
because an instruction to go and look is a placeholder wearing a hat.

**Type consistency.** `insert_decisions(conn, topic_id, decisions)` and
`list_for_topics(conn, topic_ids)` are used with those exact names and argument
orders in Tasks 2, 4 and 5. Rows carry `id, topic_id, decision, rationale,
decided_by, created_at` throughout; `render_report_shape` emits the
`{decision, rationale, decided_by}` subset, which is what `chunking._topic_text`
and the Word export already read off `key_decisions`.
