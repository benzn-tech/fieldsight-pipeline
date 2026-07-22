# Life-Conversation Separation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Separate personal/off-work conversation from site work at the topic level so it never reaches company-tier analytics or cross-project RAG, while the record is preserved and a privacy-preserving feedback loop improves the classifier.

**Architecture:** The extraction LLM tags each topic `work/non_work` (+confidence, +`is_mixed`) in its existing pass. A `non_work` topic is auto-held from the company tier; a human confirms → a `redactions` tombstone (soft, recoverable), or clears → the column flips to `work`. Every human verdict is logged to `classification_feedback` as **metadata only** (no personal text). A single `redactions.company_excluded_topic_ids` helper is the choke point wired into the rollup aggregator and the RAG embed/reindex paths.

**Tech Stack:** Python 3.11 (psycopg3, AWS Lambda), Postgres/Aurora (pgvector), pytest (unit = `FakeConn`+mocked repos; integration = `db` fixture, `pytest.mark.integration`, gated on `TEST_DATABASE_URL`), browser React (fieldsight-ui, no build step).

## Global Constraints

- **Never hard-delete.** A redaction is a tombstone; `reverted_at IS NULL` = active; reverting sets `reverted_at`, preserving audit. (spec §2, §4)
- **`classification_feedback` stores NO transcript / personal text** — only the two verdicts, the confidence, and a coarse `topic_category`. A test must assert the table has no free-text content column beyond category. (spec §2, §7)
- **Company-tier reads exclude redacted + `work_class='non_work'` topics; site/self tier is unchanged.** The one helper is `redactions.company_excluded_topic_ids(conn, site_ids)`. (spec §6)
- **Topic-level only.** `is_mixed` is stored but drives no logic yet (reserved for a future segment upgrade). No segment/turn table. (spec §2)
- **Classifier bias: when unsure → `work`** (a suspected-personal topic is only held, never dropped; a false `work` is recoverable via the manual button). (spec §3)
- **Reviewer authority = the existing `content:edit` / site-authority gate** (admin/gm, this site's pm/site_manager, or the topic's author) — reuse `patch_content`'s exact ACL; invent no new role. (spec §5)
- **Migration numbering continues after `0020`: `0021`, `0022`, `0023`.**
- **Windows/CRLF (repo memory):** use single-line anchors when editing; never `git add -A` — stage explicit paths.

---

## File Structure

**Create (fieldsight-pipeline):**
- `src/migrations/0021_topic_work_class.sql` — `topics.work_class/work_confidence/is_mixed`.
- `src/migrations/0022_redactions.sql` — tombstone table.
- `src/migrations/0023_classification_feedback.sql` — verdict-only feedback table.
- `src/repositories/redactions.py` — create/revert/get, `is_topic_redacted`, `company_excluded_topic_ids`, `list_active_for_topics`.
- `src/repositories/classification_feedback.py` — `append_feedback`, `summary`.
- `tests/integration/test_redactions_repo.py`, `tests/integration/test_classification_feedback_repo.py`.

**Modify (fieldsight-pipeline):**
- `src/repositories/topics.py` — `_TOPIC_COLS` / `_TOPIC_COLS_JOINED` / `upsert_topic` carry the 3 new columns.
- `src/repositories/rollup.py` — `portfolio_counts` excludes `company_excluded_topic_ids`.
- `src/reindex.py` — `enqueue_topic_reindex` delete-only for redacted/`non_work`.
- `src/lambda_embed_report.py` — honor a `delete_only` request (write empty vectors artifact so ingest deletes).
- `src/lambda_org_api.py` — `render_report_shape` threads `work_class`/`redacted` (Task 1b); 4 endpoints + routes.
- `src/chunking.py` — `chunk_report` skips `non_work` topics.
- `src/lambda_extract_session.py` — `EXTRACTION_SCHEMA` + prompt add the 3 fields.
- `src/lambda_item_writer.py` — pass the 3 fields into `upsert_topic`.
- `src/lambda_org_api.py` — 4 endpoints + routes + imports.
- `tests/unit/test_lambda_org_api.py`, `tests/unit/test_lambda_extract_session.py`, `tests/unit/test_lambda_item_writer.py`, `tests/unit/test_chunking.py`, `tests/unit/test_lambda_embed_report_reindex.py`, `tests/integration/test_core_repositories.py` (rollup) — new tests.

**Modify (fieldsight-ui):**
- `scripts/api/actions.js` — `createRedaction` / `revertRedaction` / `submitClassificationFeedback` + exports.
- `scripts/pages/timeline.js` — review buttons + removed-area.
- `app-shell-preview.html` — bump `actions.js?v=9→10`, `timeline.js?v=38→39`.

---

# SLICE 1 — Data + enforcement backbone

### Task 1: topics work_class columns + repo pass-through

**Files:**
- Create: `src/migrations/0021_topic_work_class.sql`
- Modify: `src/repositories/topics.py` (`_TOPIC_COLS` ~line 6, `_TOPIC_COLS_JOINED` ~line 195, `upsert_topic` ~line 46)
- Test: `tests/integration/test_core_repositories.py` (add one test)

**Interfaces:**
- Produces: `topics.upsert_topic(..., work_class=None, work_confidence=None, is_mixed=False)`; every topic-read (`list_topics_for_date`, `list_site_topics`, `get_topic_full`) now returns `work_class`, `work_confidence`, `is_mixed`.

- [ ] **Step 1: Write the migration**

`src/migrations/0021_topic_work_class.sql`:
```sql
-- src/migrations/0021_topic_work_class.sql
-- Life-conversation separation (2026-07-21 spec §4): per-topic work/non_work
-- classification produced at extraction time. work_class NULL = legacy /
-- unclassified (enforcement treats NULL and 'work' alike). is_mixed marks a
-- topic holding both work and personal talk -- the quantitative trigger to
-- build segment-level separation later (reserved; no segment table now).
ALTER TABLE topics ADD COLUMN work_class text
  CHECK (work_class IN ('work', 'non_work'));
ALTER TABLE topics ADD COLUMN work_confidence real;
ALTER TABLE topics ADD COLUMN is_mixed boolean NOT NULL DEFAULT false;
```

- [ ] **Step 2: Write the failing test**

In `tests/integration/test_core_repositories.py`, add (reuse whatever `_seed`/company+site helper the file already has; if it seeds a `site`, mirror it):
```python
def test_topic_work_class_roundtrip(db):
    import repositories.topics as topics
    from repositories import companies, sites
    co = companies.create_company(db, "WC-Co")
    s = sites.create_site(db, co["id"], "WC-Site")
    row = topics.upsert_topic(
        db, s["id"], "2026-07-21", "Lunch chat",
        work_class="non_work", work_confidence=0.91, is_mixed=True)
    assert row["work_class"] == "non_work"
    assert abs(row["work_confidence"] - 0.91) < 1e-6
    assert row["is_mixed"] is True
    got = topics.list_site_topics(db, s["id"], "2026-07-21")[0]
    assert got["work_class"] == "non_work" and got["is_mixed"] is True
```

- [ ] **Step 3: Run it — expect FAIL**

Run: `TEST_DATABASE_URL=$TEST_DATABASE_URL uv run pytest tests/integration/test_core_repositories.py::test_topic_work_class_roundtrip -v`
Expected: FAIL — `work_class` is not a column / not returned / not an `upsert_topic` kwarg. (If `TEST_DATABASE_URL` is unset the test is skipped, not failed — set it in CI; locally rely on Step 6's suite.)

- [ ] **Step 4: Extend the column constants**

`src/repositories/topics.py` — `_TOPIC_COLS` (append the 3 columns before the closing quote):
```python
_TOPIC_COLS = ("id, site_id, user_id, source_s3_key, report_date, occurred_at, "
               "category, title, summary, time_range, participants, source, created_at, "
               "work_class, work_confidence, is_mixed")
```
`_TOPIC_COLS_JOINED` (append `t.`-prefixed):
```python
_TOPIC_COLS_JOINED = (
    "t.id, t.site_id, t.user_id, t.source_s3_key, t.report_date, t.occurred_at, "
    "t.category, t.title, t.summary, t.time_range, t.participants, t.source, t.created_at, "
    "t.work_class, t.work_confidence, t.is_mixed"
)
```

- [ ] **Step 5: Add the `upsert_topic` kwargs + INSERT columns**

`src/repositories/topics.py` `upsert_topic` — add kwargs to the signature (after `participants=None`):
```python
                 time_range=None, participants=None,
                 work_class=None, work_confidence=None, is_mixed=False) -> dict:
```
and extend the INSERT column list + VALUES + params (the topics INSERT only — leave the children loops unchanged):
```python
    topic = cur.execute(
        f"INSERT INTO topics (site_id, user_id, source_s3_key, report_date, occurred_at, "
        f"category, title, summary, time_range, participants, "
        f"work_class, work_confidence, is_mixed) "
        f"VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING {_TOPIC_COLS}",
        (site_id, user_id, source_s3_key, report_date, occurred_at, category, title, summary,
         time_range, Jsonb(participants) if participants is not None else None,
         work_class, work_confidence, is_mixed),
    ).fetchone()
```

- [ ] **Step 6: Run tests — expect PASS**

Run: `uv run pytest tests/unit/test_lambda_item_writer.py tests/unit/test_lambda_org_api.py -q` (unit: existing topic-read/write consumers still pass with the widened SELECT) and, where a DB is configured, `TEST_DATABASE_URL=… uv run pytest tests/integration/test_core_repositories.py -q`.
Expected: PASS.

- [ ] **Step 7: Commit**
```bash
git add src/migrations/0021_topic_work_class.sql src/repositories/topics.py tests/integration/test_core_repositories.py
git commit -m "feat(topics): work_class/work_confidence/is_mixed columns + pass-through"
```

---

### Task 1b: thread work_class + redaction status through the timeline read

**Why (Fable review C1):** `render_report_shape` builds each topic from a FIXED
field whitelist, so the columns Task 1 added are dropped before the frontend
sees them — the review UI would never see `work_class` and the "removed area"
would have no redaction flag. This task threads them through the one read the
timeline page uses.

**Files:**
- Modify: `src/lambda_org_api.py` (`render_report_shape` `topics_out.append({...})`, ~line 1699-1714)
- Modify: `src/repositories/topics.py` (add `set_work_class`)
- Test: `tests/unit/test_lambda_org_api.py`

**Interfaces:**
- Consumes: `redactions.list_active_for_topics(conn, topic_ids)` (Task 2), topic rows carrying `work_class/work_confidence/is_mixed` (Task 1).
- Produces: each timeline topic carries `work_class`, `work_confidence`, `is_mixed`, `redacted` (bool); `topics.set_work_class(conn, topic_id, work_class) -> row|None`.

- [ ] **Step 1: Write the failing test** (in `tests/unit/test_lambda_org_api.py`, drive `render_report_shape` directly with two topic rows + a stubbed `redactions.list_active_for_topics`):
```python
def test_render_report_shape_carries_work_class_and_redacted(wired):
    wired.setattr(org.redactions, "list_active_for_topics",
                  lambda conn, ids: {"t-red": {"id": "r-1"}})
    rows = [
        {"id": "t-red", "site_id": "s", "user_id": None, "source_s3_key": "extractions/U/2026-07-21/x.json",
         "report_date": "2026-07-21", "occurred_at": None, "category": "progress", "title": "Call",
         "summary": "", "time_range": None, "participants": None, "source": "ai", "created_at": "t",
         "work_class": "work", "work_confidence": 0.2, "is_mixed": False,
         "action_items": [], "safety_observations": [], "findings": []},
        {"id": "t-lunch", "site_id": "s", "user_id": None, "source_s3_key": "extractions/U/2026-07-21/x.json",
         "report_date": "2026-07-21", "occurred_at": None, "category": "progress", "title": "Lunch",
         "summary": "", "time_range": None, "participants": None, "source": "ai", "created_at": "t",
         "work_class": "non_work", "work_confidence": 0.9, "is_mixed": False,
         "action_items": [], "safety_observations": [], "findings": []},
    ]
    shape = org.render_report_shape(rows, None, "2026-07-21", "U")
    by = {t["topic_row_id"]: t for t in shape["topics"]}
    assert by["t-red"]["redacted"] is True and by["t-lunch"]["redacted"] is False
    assert by["t-lunch"]["work_class"] == "non_work" and by["t-lunch"]["work_confidence"] == 0.9
```

- [ ] **Step 2: Run — expect FAIL** (`KeyError`/absent `redacted`/`work_class`).
Run: `uv run pytest tests/unit/test_lambda_org_api.py::test_render_report_shape_carries_work_class_and_redacted -v`

- [ ] **Step 3: Thread the fields through `render_report_shape`.** Read `src/lambda_org_api.py` around `render_report_shape`. Before the per-topic loop that builds `topics_out`, fetch redaction state once:
```python
    _redacted = redactions.list_active_for_topics(conn, [r["id"] for r in rows])
```
> `render_report_shape` must have a `conn` in scope — it is called from `_render_timeline_for_user`/`reindex` which both hold `conn`. If the current signature lacks `conn`, add it as a parameter and pass it at both call sites (`_aurora_shape` and `reindex.enqueue_topic_reindex`). Verify by reading the two call sites first.

Then add these keys to the `topics_out.append({...})` dict (additive — verbatim-S3 topics simply won't have them):
```python
        "work_class": t.get("work_class"),
        "work_confidence": t.get("work_confidence"),
        "is_mixed": t.get("is_mixed"),
        "redacted": t["id"] in _redacted,
        "redaction_id": (_redacted.get(t["id"]) or {}).get("id"),   # for the removed-area revert
```

- [ ] **Step 4: Add `topics.set_work_class`** (`src/repositories/topics.py`), used by the Task 9 flip:
```python
def set_work_class(conn, topic_id, work_class):
    """Human override of the machine work/non_work call (spec §5: '其实是工作'
    flips it to 'work', releasing the topic to the company tier). Returns the
    updated row, or None if the id is missing."""
    return conn.cursor(row_factory=dict_row).execute(
        f"UPDATE topics SET work_class=%s WHERE id=%s RETURNING {_TOPIC_COLS}",
        (work_class, topic_id)).fetchone()
```

- [ ] **Step 5: Run — expect PASS**, then commit:
```bash
git add src/lambda_org_api.py src/repositories/topics.py tests/unit/test_lambda_org_api.py
git commit -m "feat(timeline): thread work_class + redacted through render_report_shape; topics.set_work_class"
```

> **Ordering:** Task 1b consumes `redactions.list_active_for_topics` (Task 2) — execute Task 2 first, then Task 1b. (The task numbers are labels, not a strict order; the executor follows the Interfaces `Consumes` lines.)

---

### Task 2: redactions table + repository

**Files:**
- Create: `src/migrations/0022_redactions.sql`, `src/repositories/redactions.py`, `tests/integration/test_redactions_repo.py`

**Interfaces:**
- Produces: `redactions.create_redaction(conn, company_id, target_id, reason, actor_user_id, actor_role, *, target_type="topic", scope="analysis") -> row`; `revert_redaction(conn, redaction_id, company_id) -> row|None`; `get_redaction(conn, id) -> row|None`; `is_topic_redacted(conn, topic_id) -> bool`; `company_excluded_topic_ids(conn, site_ids) -> set`; `list_active_for_topics(conn, topic_ids) -> {topic_id: row}`.

- [ ] **Step 1: Write the migration**

`src/migrations/0022_redactions.sql`:
```sql
-- src/migrations/0022_redactions.sql
-- Life-conversation separation (2026-07-21 spec §4, from 2026-07-17 §3.4): a
-- redaction is a TOMBSTONE, never a hard delete. Original content is retained;
-- reverted_at IS NULL = active, reverting sets reverted_at (audit survives).
-- Company-tier reads exclude topics with an active redaction; the site/self
-- tier still reaches them (relocated to the "removed / personal" area).
-- target_type is 'topic' now; 'segment'/'finding' reserved for the future
-- segment-level upgrade. NO FK on target_id: the topic can be superseded by
-- nightly re-extraction and the tombstone must outlive that (mirrors 0019).
CREATE TABLE redactions (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id    uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  target_type   text NOT NULL DEFAULT 'topic'
                  CHECK (target_type IN ('topic', 'segment', 'finding')),
  target_id     uuid NOT NULL,
  reason        text NOT NULL,
  actor_user_id uuid REFERENCES users(id),
  actor_role    text,
  scope         text NOT NULL DEFAULT 'analysis'
                  CHECK (scope IN ('analysis', 'all')),
  created_at    timestamptz NOT NULL DEFAULT now(),
  reverted_at   timestamptz
);

CREATE INDEX idx_redactions_target ON redactions (target_type, target_id, reverted_at);
CREATE INDEX idx_redactions_company ON redactions (company_id, created_at);
```

- [ ] **Step 2: Write the failing test**

`tests/integration/test_redactions_repo.py`:
```python
import pytest
from repositories import companies, sites, topics, redactions

pytestmark = pytest.mark.integration


def _seed(db):
    co = companies.create_company(db, "Red-Co")
    s = sites.create_site(db, co["id"], "Red-Site")
    return co, s


def test_create_excludes_and_revert_restores(db):
    co, s = _seed(db)
    personal = topics.upsert_topic(db, s["id"], "2026-07-21", "Lunch", work_class="non_work")
    work = topics.upsert_topic(db, s["id"], "2026-07-21", "Pour", work_class="work")
    manual = topics.upsert_topic(db, s["id"], "2026-07-21", "Family call", work_class="work")

    # non_work is auto-excluded even with no redaction
    excl = redactions.company_excluded_topic_ids(db, [s["id"]])
    assert personal["id"] in excl and work["id"] not in excl

    red = redactions.create_redaction(db, co["id"], manual["id"], "non_work", None, "site_manager")
    assert red["reverted_at"] is None and red["scope"] == "analysis"
    assert redactions.is_topic_redacted(db, manual["id"]) is True
    assert manual["id"] in redactions.company_excluded_topic_ids(db, [s["id"]])

    reverted = redactions.revert_redaction(db, red["id"], co["id"])
    assert reverted is not None and reverted["reverted_at"] is not None
    assert redactions.is_topic_redacted(db, manual["id"]) is False
    assert manual["id"] not in redactions.company_excluded_topic_ids(db, [s["id"]])
    # wrong company can neither revert nor re-revert
    other = companies.create_company(db, "Other-Co")
    red2 = redactions.create_redaction(db, co["id"], work["id"], "privacy", None, "admin")
    assert redactions.revert_redaction(db, red2["id"], other["id"]) is None
```

- [ ] **Step 3: Run it — expect FAIL**

Run: `TEST_DATABASE_URL=… uv run pytest tests/integration/test_redactions_repo.py -v`
Expected: FAIL — `No module named 'repositories.redactions'`.

- [ ] **Step 4: Implement the repository**

`src/repositories/redactions.py`:
```python
"""Redaction tombstones for life-conversation separation (2026-07-21 spec §4).
A redaction soft-excludes a topic from company-tier reads without deleting it;
reverted_at IS NULL = active. company_excluded_topic_ids is the single choke
point every company-tier read routes through (rollup, RAG embed/reindex)."""
from psycopg.rows import dict_row

_COLS = ("id, company_id, target_type, target_id, reason, actor_user_id, "
         "actor_role, scope, created_at, reverted_at")


def create_redaction(conn, company_id, target_id, reason, actor_user_id, actor_role,
                     *, target_type="topic", scope="analysis"):
    return conn.cursor(row_factory=dict_row).execute(
        f"INSERT INTO redactions (company_id, target_type, target_id, reason, "
        f"actor_user_id, actor_role, scope) VALUES (%s,%s,%s,%s,%s,%s,%s) "
        f"RETURNING {_COLS}",
        (company_id, target_type, target_id, reason, actor_user_id, actor_role, scope),
    ).fetchone()


def get_redaction(conn, redaction_id):
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM redactions WHERE id=%s", (redaction_id,)).fetchone()


def revert_redaction(conn, redaction_id, company_id):
    """Un-tombstone (spec §4). Company-guarded; sets reverted_at so audit
    survives. None if missing / wrong company / already reverted."""
    return conn.cursor(row_factory=dict_row).execute(
        f"UPDATE redactions SET reverted_at=now() "
        f"WHERE id=%s AND company_id=%s AND reverted_at IS NULL RETURNING {_COLS}",
        (redaction_id, company_id)).fetchone()


def is_topic_redacted(conn, topic_id) -> bool:
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT 1 FROM redactions WHERE target_type='topic' AND target_id=%s "
        "AND reverted_at IS NULL LIMIT 1", (topic_id,)).fetchone() is not None


def company_excluded_topic_ids(conn, site_ids) -> set:
    """Topic ids a COMPANY-tier read excludes across site_ids: any topic
    classified non_work (auto-held) OR carrying an active redaction. The
    site/self tier does NOT use this. Empty site_ids -> empty set (no query)."""
    if not site_ids:
        return set()
    rows = conn.cursor(row_factory=dict_row).execute(
        "SELECT id FROM topics WHERE site_id = ANY(%s) AND ("
        "  work_class='non_work' "
        "  OR id IN (SELECT target_id FROM redactions "
        "            WHERE target_type='topic' AND reverted_at IS NULL))",
        (list(site_ids),)).fetchall()
    return {r["id"] for r in rows}


def list_active_for_topics(conn, topic_ids) -> dict:
    """Active redaction row keyed by target topic id (UI 'removed' area). {}."""
    if not topic_ids:
        return {}
    return {r["target_id"]: r for r in conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM redactions WHERE target_type='topic' "
        f"AND target_id = ANY(%s) AND reverted_at IS NULL", (list(topic_ids),)).fetchall()}
```

- [ ] **Step 5: Run — expect PASS**

Run: `TEST_DATABASE_URL=… uv run pytest tests/integration/test_redactions_repo.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**
```bash
git add src/migrations/0022_redactions.sql src/repositories/redactions.py tests/integration/test_redactions_repo.py
git commit -m "feat(redactions): tombstone table + repo + company_excluded_topic_ids"
```

---

### Task 3: classification_feedback table + repository

**Files:**
- Create: `src/migrations/0023_classification_feedback.sql`, `src/repositories/classification_feedback.py`, `tests/integration/test_classification_feedback_repo.py`

**Interfaces:**
- Produces: `classification_feedback.append_feedback(conn, company_id, topic_id, human_verdict, *, classifier_verdict=None, classifier_confidence=None, topic_category=None, actor_user_id=None) -> row`; `summary(conn, company_id) -> {confirm_non_work, reject_is_work, missed_personal, precision}`.

- [ ] **Step 1: Write the migration**

`src/migrations/0023_classification_feedback.sql`:
```sql
-- src/migrations/0023_classification_feedback.sql
-- Life-conversation separation (2026-07-21 spec §4/§7): the privacy-preserving
-- feedback loop. Stores ONLY the human's verdict on the machine's work/non_work
-- call, the classifier confidence, and a COARSE topic category -- NEVER the
-- transcript or any personal text. This is the entire signal used to measure/
-- tune the classifier; personal content is never a training input, never
-- embedded. human_verdict: classifier flagged non_work & human agrees
-- (confirm_non_work=TP) / disagrees, it is work (reject_is_work=FP) / human
-- removed a NOT-flagged topic as personal (missed_personal=FN).
CREATE TABLE classification_feedback (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id            uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  topic_id              uuid NOT NULL,
  classifier_verdict    text CHECK (classifier_verdict IN ('work', 'non_work')),
  classifier_confidence real,
  human_verdict         text NOT NULL
                          CHECK (human_verdict IN ('confirm_non_work', 'reject_is_work', 'missed_personal')),
  topic_category        text,
  actor_user_id         uuid REFERENCES users(id),
  created_at            timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_classification_feedback_company ON classification_feedback (company_id, created_at);
```

- [ ] **Step 2: Write the failing test**

`tests/integration/test_classification_feedback_repo.py`:
```python
import pytest
from repositories import companies, classification_feedback as cf

pytestmark = pytest.mark.integration


def test_append_and_summary_metadata_only(db):
    co = companies.create_company(db, "CF-Co")
    tid = "11111111-1111-1111-1111-111111111111"
    r = cf.append_feedback(db, co["id"], tid, "confirm_non_work",
                           classifier_verdict="non_work", classifier_confidence=0.8,
                           topic_category="progress")
    assert r["human_verdict"] == "confirm_non_work" and r["topic_category"] == "progress"
    cf.append_feedback(db, co["id"], tid, "reject_is_work", classifier_verdict="non_work")
    cf.append_feedback(db, co["id"], tid, "missed_personal")
    s = cf.summary(db, co["id"])
    assert s == {"confirm_non_work": 1, "reject_is_work": 1, "missed_personal": 1, "precision": 0.5}
    # privacy invariant: the table exposes no verbatim-content column
    cols = {c[0] for c in db.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name='classification_feedback'").fetchall()}
    assert "observation" not in cols and "text" not in cols and "transcript" not in cols
```

- [ ] **Step 3: Run — expect FAIL**

Run: `TEST_DATABASE_URL=… uv run pytest tests/integration/test_classification_feedback_repo.py -v`
Expected: FAIL — `No module named 'repositories.classification_feedback'`.

- [ ] **Step 4: Implement the repository**

`src/repositories/classification_feedback.py`:
```python
"""Privacy-preserving classifier feedback (2026-07-21 spec §7). Stores only the
human verdict + classifier confidence + coarse category -- never any transcript
or personal text. summary() is the metadata-only accuracy roll-up."""
from psycopg.rows import dict_row


def append_feedback(conn, company_id, topic_id, human_verdict, *,
                    classifier_verdict=None, classifier_confidence=None,
                    topic_category=None, actor_user_id=None):
    return conn.cursor(row_factory=dict_row).execute(
        "INSERT INTO classification_feedback (company_id, topic_id, "
        "classifier_verdict, classifier_confidence, human_verdict, topic_category, "
        "actor_user_id) VALUES (%s,%s,%s,%s,%s,%s,%s) "
        "RETURNING id, company_id, topic_id, classifier_verdict, classifier_confidence, "
        "human_verdict, topic_category, actor_user_id, created_at",
        (company_id, topic_id, classifier_verdict, classifier_confidence,
         human_verdict, topic_category, actor_user_id)).fetchone()


def summary(conn, company_id) -> dict:
    rows = conn.cursor(row_factory=dict_row).execute(
        "SELECT human_verdict, count(*) AS n FROM classification_feedback "
        "WHERE company_id=%s GROUP BY human_verdict", (company_id,)).fetchall()
    by = {r["human_verdict"]: r["n"] for r in rows}
    tp, fp, fn = by.get("confirm_non_work", 0), by.get("reject_is_work", 0), by.get("missed_personal", 0)
    return {"confirm_non_work": tp, "reject_is_work": fp, "missed_personal": fn,
            "precision": (tp / (tp + fp)) if (tp + fp) else None}
```

- [ ] **Step 5: Run — expect PASS**, then **commit**
```bash
git add src/migrations/0023_classification_feedback.sql src/repositories/classification_feedback.py tests/integration/test_classification_feedback_repo.py
git commit -m "feat(classification-feedback): verdict-only feedback table + repo"
```

---

### Task 4: rollup enforcement (company-tier aggregation excludes personal)

**Files:**
- Modify: `src/repositories/rollup.py` (`portfolio_counts`, ~lines 79-149)
- Test: `tests/integration/test_core_repositories.py`

**Interfaces:**
- Consumes: `redactions.company_excluded_topic_ids(conn, site_ids)` (Task 2).

- [ ] **Step 1: Write the failing test**

Add to `tests/integration/test_core_repositories.py`:
```python
def test_portfolio_counts_excludes_non_work_and_redacted(db):
    import datetime as _dt
    import repositories.topics as topics
    from repositories import companies, sites, rollup, redactions
    today = _dt.date.today().isoformat()   # topics_count is report_date >= CURRENT_DATE-30
    co = companies.create_company(db, "RU-Co")
    s = sites.create_site(db, co["id"], "RU-Site")
    work = topics.upsert_topic(db, s["id"], today, "Pour", work_class="work",
                               action_items=[{"text": "fix rebar"}])
    topics.upsert_topic(db, s["id"], today, "Lunch", work_class="non_work",
                        action_items=[{"text": "personal errand"}])
    redacted = topics.upsert_topic(db, s["id"], today, "Call", work_class="work",
                                   action_items=[{"text": "call spouse"}])
    redactions.create_redaction(db, co["id"], redacted["id"], "privacy", None, "admin")

    counts = rollup.portfolio_counts(db, [s["id"]])[str(s["id"])]
    # only the one 'work', non-redacted topic + its action item are counted
    assert counts["topics_count"] == 1
    assert counts["open_actions"] == 1 and counts["total_actions"] == 1
```

- [ ] **Step 2: Run — expect FAIL** (counts include non_work + redacted)

Run: `TEST_DATABASE_URL=… uv run pytest tests/integration/test_core_repositories.py::test_portfolio_counts_excludes_non_work_and_redacted -v`

- [ ] **Step 3: Wire the exclusion into `portfolio_counts`**

`src/repositories/rollup.py` — add the import at the top of the module (next to `from psycopg.rows import dict_row`):
```python
from repositories import redactions
```
Inside `portfolio_counts`, right after `ids = list(site_ids)` and `merged = {...}`, compute the exclusion set once:
```python
    excluded = list(redactions.company_excluded_topic_ids(conn, ids))
```
Then add `AND <topic-id column> != ALL(%s::uuid[])` to each aggregate query and append `excluded` to its params:
- **safety_rows** — add to BOTH UNION arms and pass `excluded` twice more:
  ```python
        "  SELECT site_id, status, (severity='major') AS is_high "
        "  FROM findings WHERE site_id = ANY(%s) AND domain='safety' "
        "    AND topic_id != ALL(%s::uuid[]) "
        "  UNION ALL "
        "  SELECT so.site_id, so.status, (lower(so.risk_level)='high') AS is_high "
        "  FROM safety_observations so "
        "  WHERE so.site_id = ANY(%s) AND so.topic_id != ALL(%s::uuid[]) "
        "    AND NOT EXISTS (SELECT 1 FROM findings f "
        "                    WHERE f.topic_id = so.topic_id AND f.domain='safety')"
        ") u GROUP BY site_id",
        (ids, excluded, ids, excluded),
  ```
- **action_rows** — `... FROM action_items WHERE site_id = ANY(%s) AND topic_id != ALL(%s::uuid[]) GROUP BY site_id`, params `(ids, excluded)`.
- **topic_rows** — add `AND id != ALL(%s::uuid[])` before `GROUP BY`, params `(ids, excluded)`.
- **activity_rows** — add `AND id != ALL(%s::uuid[])`, params `(ids, excluded)`.

(`x != ALL(%s::uuid[])` with an empty list is TRUE for every row, so a site with nothing excluded is unchanged.)

- [ ] **Step 4: Run — expect PASS**, plus the existing rollup tests:

Run: `TEST_DATABASE_URL=… uv run pytest tests/integration/test_core_repositories.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**
```bash
git add src/repositories/rollup.py tests/integration/test_core_repositories.py
git commit -m "feat(rollup): exclude non_work + redacted topics from company-tier counts"
```

---

### Task 5: RAG reindex — delete-only for redacted / non_work

**Files:**
- Modify: `src/reindex.py` (`enqueue_topic_reindex`, ~lines 47-87)
- Test: `tests/unit/test_lambda_embed_report_reindex.py` (add one test)

**Interfaces:**
- Consumes: `redactions.is_topic_redacted(conn, topic_id)` (Task 2); `topics.get_topic_full` now returns `work_class` (Task 1).
- Produces: an `enqueue_topic_reindex` that writes a request with `topic_chunks: []` when the topic is redacted or `non_work`, so `apply_vectors` deletes its vectors and inserts none.

- [ ] **Step 1: Write the failing test**

In `tests/unit/test_lambda_embed_report_reindex.py` (mirror its existing fake-s3 / monkeypatch style):
```python
def test_enqueue_delete_only_for_non_work(monkeypatch):
    import reindex
    puts = {}
    class S3:
        def put_object(self, Bucket, Key, Body, ContentType):
            import json as _j
            puts["key"] = Key; puts["body"] = _j.loads(Body)
    monkeypatch.setattr(reindex.topics, "get_topic_full",
                        lambda conn, tid: {"id": tid, "site_id": "s1", "user_id": None,
                                           "report_date": "2026-07-21",
                                           "source_s3_key": "extractions/U/2026-07-21/x.json",
                                           "work_class": "non_work"})
    monkeypatch.setattr(reindex, "_company_id_for_site", lambda conn, sid: "c1")
    monkeypatch.setattr(reindex.aliases, "list_active", lambda *a, **k: [])
    monkeypatch.setattr(reindex.redactions, "is_topic_redacted", lambda conn, tid: False)
    key = reindex.enqueue_topic_reindex(S3(), "bucket", object(), "t-1", "U", "2026-07-21")
    assert key is not None
    assert puts["body"]["topic_chunks"] == []          # delete-only: no vectors re-inserted
```

- [ ] **Step 2: Run — expect FAIL** (`work_class` ignored; chunks built)

Run: `uv run pytest tests/unit/test_lambda_embed_report_reindex.py::test_enqueue_delete_only_for_non_work -v`

- [ ] **Step 3: Add the guard in `enqueue_topic_reindex`**

`src/reindex.py` — add `redactions` to the top import: `from repositories import aliases, chunks, redactions, topics`. Then, inside `enqueue_topic_reindex`, right after `t = topics.get_topic_full(conn, topic_id)` / the `if t is None: return None` guard and before building `shaped`, insert:
```python
    # Life-conversation separation (spec §6): a redacted or non_work topic is
    # removed from RAG. Write a DELETE-ONLY request (no topic_chunks) so
    # apply_vectors deletes its existing vectors and inserts nothing.
    if t.get("work_class") == "non_work" or redactions.is_topic_redacted(conn, topic_id):
        key = request_key(date, folder, topic_id)
        s3_client.put_object(Bucket=bucket, Key=key, ContentType="application/json",
            Body=json.dumps({
                "topic_id": str(topic_id), "site_id": str(t["site_id"]),
                "user_id": str(t["user_id"]) if t.get("user_id") is not None else None,
                "report_date": str(t["report_date"]),
                "source_s3_key": t["source_s3_key"], "report_key": None, "topic_seq": None,
                "folder": folder, "date": date, "aliases": [], "topic_chunks": [],
                "delete_only": True,
            }))
        return key
```

- [ ] **Step 4: (Fable review C2) Make the embed worker HONOR a delete-only request.**
`src/lambda_embed_report.py` skips empty-chunk requests (`if not chunks_out: return {skip}`, ~line 162-164), so a delete-only request never writes a `reindex_vectors/` artifact and `apply_vectors` (which does `delete_chunks_for_topic`) never runs — the vectors are never deleted. Read `embed_reindex_request` in `src/lambda_embed_report.py`, and change the empty-chunks early return so that a request flagged `delete_only` STILL writes a vectors artifact with `chunks: []` (same key/shape it writes on the happy path, just empty):
```python
    if not chunks_out:
        if request.get("delete_only"):
            # Redacted/non_work topic (spec §6): write an empty vectors result so
            # ingest's apply_vectors runs delete_chunks_for_topic and removes it.
            result = {"topic_id": request["topic_id"], "site_id": request["site_id"],
                      "report_date": request["report_date"],
                      "user_id": request.get("user_id"),
                      "source_s3_key": request.get("source_s3_key"), "chunks": []}
            vkey = reindex.vectors_key(request["date"], request["folder"], request["topic_id"])
            s3_client.put_object(Bucket=<vectors_bucket>, Key=vkey,
                                 Body=json.dumps(result), ContentType="application/json")
            return {"reindex": vkey, "chunks": 0, "deleted": True}
        logger.info("reindex %s: no chunks -- skipping", key)
        return {"reindex": key, "chunks": 0}
```
> Read the surrounding function first to bind the real variable names (`s3_client`, the vectors bucket, whether it already imports `reindex` / builds `result` a particular way) — mirror the happy-path artifact write exactly, only with `chunks: []`.

- [ ] **Step 5: Add the embed-worker test** in `tests/unit/test_lambda_embed_report_reindex.py`:
```python
def test_embed_writes_empty_vectors_artifact_for_delete_only(monkeypatch):
    # a delete_only request with no chunks must still emit a vectors artifact
    # (chunks: []) so ingest deletes the topic's vectors — NOT be skipped.
    # Arrange by copying the file's existing embed-reindex happy-path test and
    # setting the request's topic_chunks=[] + delete_only=True; assert put_object
    # was called with a reindex_vectors/ key whose body has chunks == [].
    ...
```
> Copy the nearest existing embed-reindex test's arrange/act, flip the request to delete-only, and assert the vectors artifact is written (not skipped).

- [ ] **Step 6: Run — expect PASS**, plus the existing reindex + ingest tests:

Run: `uv run pytest tests/unit/test_lambda_embed_report_reindex.py tests/unit/test_lambda_ingest_reindex.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**
```bash
git add src/reindex.py src/lambda_embed_report.py tests/unit/test_lambda_embed_report_reindex.py
git commit -m "feat(reindex): delete-only re-index actually removes redacted/non_work vectors"
```

---

### Task 6: RAG initial-embed — chunk_report skips non_work

**Files:**
- Modify: `src/chunking.py` (`chunk_report`, line 99-124)
- Test: `tests/unit/test_chunking.py` (add one test)

- [ ] **Step 1: Write the failing test**

In `tests/unit/test_chunking.py`:
```python
def test_chunk_report_skips_non_work_topics():
    import chunking
    report = {"user_name": "U", "site": "S", "report_date": "2026-07-21", "topics": [
        {"topic_id": 0, "topic_title": "Pour", "summary": "work", "work_class": "work"},
        {"topic_id": 1, "topic_title": "Lunch", "summary": "personal", "work_class": "non_work"},
        {"topic_id": 2, "topic_title": "Legacy", "summary": "no class"},  # work_class absent -> kept
    ]}
    chunks = chunking.chunk_report(report)
    seqs = {c["topic_seq"] for c in chunks}
    assert 0 in seqs and 2 in seqs and 1 not in seqs
```

- [ ] **Step 2: Run — expect FAIL** (all 3 chunked)

Run: `uv run pytest tests/unit/test_chunking.py::test_chunk_report_skips_non_work_topics -v`

- [ ] **Step 3: Add the guard**

`src/chunking.py` — in `chunk_report`, make the loop skip non_work topics (first line inside the `for`):
```python
    for t in report.get("topics", []):
        if t.get("work_class") == "non_work":
            continue                       # spec §6: personal talk never embedded
        text = _topic_text(t)
```

- [ ] **Step 4: Run — expect PASS**, plus existing chunking tests:

Run: `uv run pytest tests/unit/test_chunking.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**
```bash
git add src/chunking.py tests/unit/test_chunking.py
git commit -m "feat(chunking): exclude non_work topics from RAG chunks"
```

---

# SLICE 2 — Classifier

### Task 7: extraction prompt + schema emit work_class

**Files:**
- Modify: `src/lambda_extract_session.py` (`EXTRACTION_SCHEMA` lines 140-179; `build_extraction_prompt` lines 182-237)
- Test: `tests/unit/test_lambda_extract_session.py` (add one test)

**Interfaces:**
- Produces: each extracted topic dict carries `work_class` (`"work"|"non_work"`), `work_confidence` (0-1), `is_mixed` (bool).

- [ ] **Step 1: Write the failing test**

In `tests/unit/test_lambda_extract_session.py`:
```python
def test_prompt_and_schema_request_work_class():
    import lambda_extract_session as les
    assert '"work_class"' in les.EXTRACTION_SCHEMA
    assert '"work_confidence"' in les.EXTRACTION_SCHEMA
    assert '"is_mixed"' in les.EXTRACTION_SCHEMA
    prompt = les.build_extraction_prompt("U", "2026-07-21", "sess", [], 0)
    assert "work_class" in prompt and "non_work" in prompt
```

- [ ] **Step 2: Run — expect FAIL**

Run: `uv run pytest tests/unit/test_lambda_extract_session.py::test_prompt_and_schema_request_work_class -v`

- [ ] **Step 3: Extend the schema constant**

`src/lambda_extract_session.py` `EXTRACTION_SCHEMA` — add three fields to the topic object, right after the `"category": ...` line:
```python
      "category": "safety | progress | quality",
      "work_class": "work | non_work",
      "work_confidence": 0.0,
      "is_mixed": false,
```

- [ ] **Step 4: Add the classification instruction + rules**

`src/lambda_extract_session.py` `build_extraction_prompt` — add an instruction (renumber is unnecessary; append after instruction 2) inside `## Instructions`:
```
2b. work_class: classify each topic as "work" (site operations: inspections,
    progress, safety, coordination) or "non_work" (personal/off-work talk:
    meals, family, weekend, banter). When UNSURE, choose "work" -- a
    non_work topic is only held for human review, never dropped, so bias
    toward not over-flagging. work_confidence is YOUR confidence (0.0-1.0).
    is_mixed = true only if the topic genuinely contains BOTH work and
    personal conversation.
```
and add to the `Rules:` block:
```
- work_class MUST be one of: work, non_work
- work_confidence is a number 0.0-1.0; is_mixed is a boolean
```

- [ ] **Step 5: Run — expect PASS**, then commit
```bash
git add src/lambda_extract_session.py tests/unit/test_lambda_extract_session.py
git commit -m "feat(extract): classify topics work/non_work (+confidence, +is_mixed)"
```

---

### Task 8: item-writer persists work_class

**Files:**
- Modify: `src/lambda_item_writer.py` (the `upsert_topic(...)` call, ~lines 269-289)
- Test: `tests/unit/test_lambda_item_writer.py` (add one test)

**Interfaces:**
- Consumes: `topics.upsert_topic(..., work_class=, work_confidence=, is_mixed=)` (Task 1); topic dict keys `work_class/work_confidence/is_mixed` (Task 7).

- [ ] **Step 1: Write the failing test**

The topic→`upsert_topic` mapping loop is inline in `write_extraction_items` (there is NO per-topic seam to call directly — do not invent one). Find the nearest existing test in `tests/unit/test_lambda_item_writer.py` that drives `write_extraction_items(...)` end-to-end (it already builds an extraction payload with a `topics: [...]` list and monkeypatches the S3/DB boundary). **Copy that test's entire arrange block verbatim**, then: (a) add `work_class`/`work_confidence`/`is_mixed` to one topic in its payload, and (b) replace/augment its `topics.upsert_topic` monkeypatch with a capturing fake and assert on the kwargs:
```python
    # add this capture to the copied arrange block, replacing its upsert_topic stub:
    captured = {}
    def fake_upsert(conn, site_id, date, title, **kw):
        captured.update(kw); return {"id": "t-1"}
    monkeypatch.setattr(<module-alias>.topics, "upsert_topic", fake_upsert)
    # ... one topic in the payload gains: "work_class": "non_work",
    #     "work_confidence": 0.9, "is_mixed": True ...
    # ... after calling write_extraction_items(...) as the copied test does:
    assert captured["work_class"] == "non_work"
    assert captured["work_confidence"] == 0.9 and captured["is_mixed"] is True
```
(`<module-alias>` = whatever the copied test imports `lambda_item_writer` as.) The production change lives ONLY in Step 3.

- [ ] **Step 2: Run — expect FAIL** (kwargs absent)

Run: `uv run pytest tests/unit/test_lambda_item_writer.py -k work_class -v`

- [ ] **Step 3: Pass the fields through — SANITIZED (Fable review #7)**

The columns carry CHECK constraints (`work_class IN ('work','non_work')`; `work_confidence` is `real`). A raw LLM value like `"personal"` or a non-numeric confidence would raise inside the `write_extraction_items` transaction and **abort the whole session's topics/findings write**. Sanitize before the call — invalid → `NULL` (= legacy/unclassified, which enforcement treats as `work`). In `src/lambda_item_writer.py`, just above the `topics.upsert_topic(...)` call, add:
```python
            _wc = t.get("work_class")
            _wc = _wc if _wc in ("work", "non_work") else None
            try:
                _wconf = float(t["work_confidence"]) if t.get("work_confidence") is not None else None
            except (TypeError, ValueError):
                _wconf = None
```
then add the kwargs to the call next to `time_range=`/`participants=`:
```python
                time_range=t.get("time_range"), participants=t.get("participants"),
                work_class=_wc, work_confidence=_wconf, is_mixed=bool(t.get("is_mixed")),
                photos=[{"s3_key": p["key"], "caption_text": None} for p in matched_photos],
```
Add a second unit test asserting a garbage `work_class="personal"` + `work_confidence="high"` sanitize to `work_class=None`, `work_confidence=None` (not raised).

- [ ] **Step 4: Run — expect PASS**, then commit
```bash
git add src/lambda_item_writer.py tests/unit/test_lambda_item_writer.py
git commit -m "feat(item-writer): persist topic work_class/work_confidence/is_mixed"
```

---

# SLICE 3 — Endpoints + UI

### Task 9: org-api endpoints (redactions + feedback)

**Files:**
- Modify: `src/lambda_org_api.py` (imports ~line 69; new handlers near `patch_content`; routes in `dispatch` ~line 251)
- Test: `tests/unit/test_lambda_org_api.py`

**Interfaces:**
- Consumes: `content.get_content_row(conn, "topics", id) -> {company_id, site_id, author_user_id}` (already used by `patch_content`); `redactions.*` (Task 2); `classification_feedback.append_feedback` (Task 3); `_allowed_site_ids`, `is_cross_company`, `resolve_scope`, `_enqueue_content_reindex` (existing).
- Produces routes: `POST /api/org/redactions`, `POST /api/org/redactions/{id}/revert`, `POST /api/org/classification-feedback`.

- [ ] **Step 1: Write failing tests**

Add to `tests/unit/test_lambda_org_api.py` (mirror `test_create_observation_ok` + the ACL-403 pattern):
```python
def test_create_redaction_ok_and_enqueues_reindex(wired):
    wired.setattr(org, "_allowed_site_ids", lambda conn, caller: {SITE_ID})
    wired.setattr(org.content, "get_content_row",
                  lambda conn, table, rid: {"company_id": "c-uuid-1", "site_id": SITE_ID,
                                            "author_user_id": "u-uuid-1"})
    wired.setattr(org.memberships, "caller_site_roles", lambda conn, uid: {SITE_ID: "site_manager"})
    seen = {}
    wired.setattr(org.redactions, "create_redaction",
                  lambda conn, cid, tid, reason, auid, arole, **k: seen.update(
                      cid=cid, tid=tid, reason=reason, scope=k.get("scope")) or {"id": "r-1"})
    enq = []
    wired.setattr(org, "_enqueue_content_reindex", lambda conn, table, rid: enq.append((table, rid)))
    res = org.lambda_handler(make_event("POST", "/api/org/redactions",
                                        body={"target_id": "t-9", "reason": "non_work"}), None)
    assert res["statusCode"] == 201
    assert seen == {"cid": "c-uuid-1", "tid": "t-9", "reason": "non_work", "scope": "analysis"}
    assert enq == [("topics", "t-9")]                       # RAG removal enqueued


def test_create_redaction_denies_site_outside_reach_403(wired):
    wired.setattr(org, "_allowed_site_ids", lambda conn, caller: {SITE_ID})
    wired.setattr(org.content, "get_content_row",
                  lambda conn, table, rid: {"company_id": "c-uuid-1", "site_id": OTHER_SITE_ID,
                                            "author_user_id": None})
    called = []
    wired.setattr(org.redactions, "create_redaction", lambda *a, **k: called.append(1))
    res = org.lambda_handler(make_event("POST", "/api/org/redactions",
                                        body={"target_id": "t-9", "reason": "non_work"}), None)
    assert res["statusCode"] == 403 and called == []


def test_classification_feedback_ok(wired):
    wired.setattr(org, "_allowed_site_ids", lambda conn, caller: {SITE_ID})
    wired.setattr(org.content, "get_content_row",
                  lambda conn, table, rid: {"company_id": "c-uuid-1", "site_id": SITE_ID,
                                            "author_user_id": "u-uuid-1"})
    wired.setattr(org.memberships, "caller_site_roles", lambda conn, uid: {SITE_ID: "site_manager"})
    seen = {}
    wired.setattr(org.classification_feedback, "append_feedback",
                  lambda conn, cid, tid, verdict, **k: seen.update(
                      cid=cid, tid=tid, verdict=verdict) or {"id": "f-1"})
    res = org.lambda_handler(make_event("POST", "/api/org/classification-feedback",
                                        body={"topic_id": "t-9", "human_verdict": "confirm_non_work"}), None)
    assert res["statusCode"] == 201 and seen["verdict"] == "confirm_non_work"
```

- [ ] **Step 2: Run — expect FAIL** (no such routes / imports)

Run: `uv run pytest tests/unit/test_lambda_org_api.py -k "redaction or classification_feedback" -v`

- [ ] **Step 3: Add imports**

`src/lambda_org_api.py` — add `classification_feedback` and `redactions` to the `from repositories import (...)` tuple:
```python
from repositories import (action_items, aliases, classification_feedback, companies, content,
                          content_edits, memberships, observations, programme,
                          programme_suggestions, recordings, redactions, rollup,
                          scope, sites, topics, users, voice_messages)
```

- [ ] **Step 4: Add the three handlers** (place after `patch_content`)

```python
def _topic_authority(conn, caller, topic_id):
    """Shared ACL for topic-scoped redaction/feedback writes -- IDENTICAL to
    patch_content's per-item tier. Returns (row, None) if allowed, else
    (None, error_response)."""
    row = content.get_content_row(conn, "topics", topic_id)
    cross = is_cross_company(caller["global_role"])
    if row is None or (not cross and str(row["company_id"]) != str(caller["company_id"])):
        return None, error("topic not found", 404)
    site_id = str(row["site_id"])
    if site_id not in _allowed_site_ids(conn, caller):
        return None, error("access denied to this topic's site", 403)
    site_role = memberships.caller_site_roles(conn, caller["id"]).get(site_id)
    is_admin = resolve_scope(caller["global_role"]) == "ALL" or cross
    is_site_authority = site_role in ("pm", "site_manager")
    is_author = row.get("author_user_id") is not None and \
        str(row["author_user_id"]) == str(caller["id"])
    if not (is_admin or is_site_authority or is_author):
        return None, error("admin/gm, this site's pm/site_manager, or the author only", 403)
    return row, None


def create_redaction_endpoint(conn, caller, body):
    """Soft-remove one topic (spec §4/§5): write a tombstone + enqueue a
    delete-only re-index so it leaves RAG. Never hard-deletes."""
    if body is None:
        return error("malformed JSON body", 400)
    target_id = body.get("target_id")
    if not target_id:
        return error("target_id required", 400)
    reason = body.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return error("reason required", 400)
    scope_val = body.get("scope", "analysis")
    if scope_val not in ("analysis", "all"):
        return error("scope must be one of ['all', 'analysis']", 400)
    row, err = _topic_authority(conn, caller, target_id)
    if err is not None:
        return err
    red = redactions.create_redaction(conn, row["company_id"], target_id, reason.strip(),
                                      caller["id"], caller["global_role"], scope=scope_val)
    try:
        _enqueue_content_reindex(conn, "topics", target_id)
    except Exception:
        logger.exception("redaction %s: reindex enqueue failed (redaction kept)", target_id)
    return ok({"redaction": red}, 201)


def revert_redaction_endpoint(conn, caller, redaction_id):
    """Un-tombstone (spec §4) + re-index so the topic returns to RAG."""
    red = redactions.get_redaction(conn, redaction_id)
    cross = is_cross_company(caller["global_role"])
    if red is None or (not cross and str(red["company_id"]) != str(caller["company_id"])):
        return error("redaction not found", 404)
    _, err = _topic_authority(conn, caller, red["target_id"])
    if err is not None:
        return err
    reverted = redactions.revert_redaction(conn, redaction_id, red["company_id"])
    if reverted is None:
        return error("redaction already reverted", 409)
    try:
        _enqueue_content_reindex(conn, "topics", red["target_id"])
    except Exception:
        logger.exception("revert %s: reindex enqueue failed (revert kept)", redaction_id)
    return ok({"redaction": reverted})


def create_classification_feedback_endpoint(conn, caller, body):
    """Record a human verdict on the classifier (spec §7, metadata only). On
    'reject_is_work' (false positive) ALSO flip topics.work_class -> 'work' and
    re-index, releasing the topic to the company tier (spec §5/§6) -- otherwise a
    mis-flagged topic would stay excluded forever (Fable review C3)."""
    if body is None:
        return error("malformed JSON body", 400)
    topic_id = body.get("topic_id")
    if not topic_id:
        return error("topic_id required", 400)
    verdict = body.get("human_verdict")
    if verdict not in ("confirm_non_work", "reject_is_work", "missed_personal"):
        return error("human_verdict must be one of "
                     "['confirm_non_work', 'missed_personal', 'reject_is_work']", 400)
    cv = body.get("classifier_verdict")                       # Fable review #10: validate
    if cv is not None and cv not in ("work", "non_work"):
        return error("classifier_verdict must be one of ['non_work', 'work']", 400)
    conf = body.get("classifier_confidence")
    if conf is not None and not isinstance(conf, (int, float)):
        return error("classifier_confidence must be a number", 400)
    row, err = _topic_authority(conn, caller, topic_id)
    if err is not None:
        return err
    fb = classification_feedback.append_feedback(
        conn, row["company_id"], topic_id, verdict,
        classifier_verdict=cv, classifier_confidence=conf,
        topic_category=body.get("topic_category"), actor_user_id=caller["id"])
    if verdict == "reject_is_work":                           # Fable review C3
        topics.set_work_class(conn, topic_id, "work")
        try:
            _enqueue_content_reindex(conn, "topics", topic_id)   # re-embed into RAG
        except Exception:
            logger.exception("feedback %s: reindex enqueue failed (verdict kept)", topic_id)
    return ok({"feedback": fb}, 201)
```
Add a test asserting `human_verdict='reject_is_work'` calls `topics.set_work_class(topic_id, 'work')` and enqueues a reindex (stub both, assert called); and a `400` test for a bad `classifier_verdict`.

- [ ] **Step 5: Add the routes** in `dispatch` (after the `/aliases` POST route, before the fall-through):
```python
    if route == "/redactions" and method == "POST":
        return create_redaction_endpoint(conn, caller, parse_body(event))
    m_rr = re.match(r"^/redactions/([^/]+)/revert$", route)
    if m_rr and method == "POST":
        return revert_redaction_endpoint(conn, caller, m_rr.group(1))
    if route == "/classification-feedback" and method == "POST":
        return create_classification_feedback_endpoint(conn, caller, parse_body(event))
```

- [ ] **Step 6: Run — expect PASS**, plus the full org-api suite:

Run: `uv run pytest tests/unit/test_lambda_org_api.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**
```bash
git add src/lambda_org_api.py tests/unit/test_lambda_org_api.py
git commit -m "feat(org-api): redaction + classification-feedback endpoints"
```

---

### Task 10: UI data layer (actions.js)

**Files:**
- Modify: `scripts/api/actions.js` (add helpers near `confirmAlias` ~line 262; export block ~line 329)
- Test: `node --check`

**Interfaces:**
- Produces: `FS.api.actions.createRedaction(targetId, reason)`, `revertRedaction(redactionId)`, `submitClassificationFeedback(payload)` — all `orgRequest` POSTs, gated on `!useMocks && !writeMocks` exactly like `updateContent`.

- [ ] **Step 1: Add the helpers** (mirror `updateContent`'s write-gate + `confirmAlias`'s POST shape)

`scripts/api/actions.js`, after `confirmAlias`:
```javascript
  async function createRedaction(targetId, reason) {
    if (!window.FS.api.useMocks && !window.FS.api.writeMocks) {
      return window.FS.api.orgRequest('/redactions',
        { method: 'POST', body: { target_id: targetId, reason: reason || 'non_work' } });
    }
    await window.FS.api.delay(60);
    return { redaction: { id: 'mock-red', target_id: targetId, reason: reason || 'non_work' } };
  }

  async function revertRedaction(redactionId) {
    if (!window.FS.api.useMocks && !window.FS.api.writeMocks) {
      return window.FS.api.orgRequest('/redactions/' + encodeURIComponent(redactionId) + '/revert',
        { method: 'POST', body: {} });
    }
    await window.FS.api.delay(60);
    return { redaction: { id: redactionId, reverted_at: 'mock' } };
  }

  async function submitClassificationFeedback(payload) {
    if (!window.FS.api.useMocks && !window.FS.api.writeMocks) {
      return window.FS.api.orgRequest('/classification-feedback',
        { method: 'POST', body: payload || {} });
    }
    await window.FS.api.delay(60);
    return { feedback: Object.assign({ id: 'mock-fb' }, payload || {}) };
  }
```
and add them to the export block:
```javascript
    updateContent:   updateContent,
    getContentHistory: getContentHistory,
    confirmAlias:    confirmAlias,
    createRedaction: createRedaction,
    revertRedaction: revertRedaction,
    submitClassificationFeedback: submitClassificationFeedback,
    actionKey:       actionKey,
```

- [ ] **Step 2: Syntax-check + commit**

Run: `node --check scripts/api/actions.js` (expect no output)
```bash
git add scripts/api/actions.js
git commit -m "feat(ui-api): createRedaction/revertRedaction/submitClassificationFeedback"
```

---

### Task 11: UI review buttons + removed-area (timeline.js)

**Files:**
- Modify: `scripts/pages/timeline.js` (topic-detail render, near `editToggle` ~line 1516 and the action-item row ~line 1591); `app-shell-preview.html` (cache busters lines 203, 238)
- Test: `node --check`

**Interfaces:**
- Consumes: `FS.api.actions.createRedaction / submitClassificationFeedback` (Task 10); `topic.work_class`, `topic.work_confidence`, `topic.topic_row_id` (durable id, already threaded); the existing `canEditContent` gate + `IconButton` + `window.FS.toast`.

- [ ] **Step 1: Add a `TopicReviewButtons` component** (place beside `GlossaryConfirm`, ~line 1328). One confirm+remove action writes BOTH the redaction and the feedback verdict:
```javascript
  function TopicReviewButtons(props) {
    // props: { topicRowId, workClass, workConfidence, category, onRemoved }
    var IconBtn = window.FieldSight.IconButton;
    var busyRef = React.useState(false);
    var busy = busyRef[0], setBusy = busyRef[1];
    if (!IconBtn || !props.topicRowId) return null;
    var isFlagged = props.workClass === 'non_work';

    function toast(msg, tone) {
      var t = window.FS && window.FS.toast;
      if (t) t.show({ message: msg, tone: tone || 'success', duration: tone === 'error' ? 5000 : 3000 });
    }
    function feedback(verdict) {
      return window.FS.api.actions.submitClassificationFeedback({
        topic_id: props.topicRowId, human_verdict: verdict,
        classifier_verdict: props.workClass || null,
        classifier_confidence: props.workConfidence != null ? props.workConfidence : null,
        topic_category: props.category || null,
      });
    }
    function remove(verdict) {
      if (busy) return;
      setBusy(true);
      Promise.all([
        window.FS.api.actions.createRedaction(props.topicRowId, 'non_work'),
        feedback(verdict),
      ]).then(function (r) {
        setBusy(false);
        if (!r[0] || r[0]._accessDenied || r[0]._notFound) { toast((r[0] && r[0].error) || 'Could not remove', 'error'); return; }
        toast('Removed from reports');
        if (props.onRemoved) props.onRemoved();
      }).catch(function () { setBusy(false); toast('Could not remove', 'error'); });
    }
    function keepAsWork() {
      if (busy) return;
      setBusy(true);
      feedback('reject_is_work').then(function () { setBusy(false); toast('Kept as work'); })
        .catch(function () { setBusy(false); toast('Could not save', 'error'); });
    }

    return React.createElement('div', { className: 'fs-topic-detail__review' },
      isFlagged
        ? React.createElement(React.Fragment, null,
            React.createElement('span', { className: 'fs-topic-detail__review-flag' }, '疑似个人 · 待确认 '),
            React.createElement(IconBtn, { icon: 'check', size: 'sm', variant: 'ghost', disabled: busy,
              ariaLabel: '确认个人并移除', onClick: function () { remove('confirm_non_work'); } }),
            React.createElement(IconBtn, { icon: 'x', size: 'sm', variant: 'ghost', disabled: busy,
              ariaLabel: '其实是工作', onClick: keepAsWork }))
        : React.createElement(IconBtn, { icon: 'user-x', size: 'sm', variant: 'ghost', disabled: busy,
            ariaLabel: '标为个人并移除', onClick: function () { remove('missed_personal'); } }),
    );
  }
```

- [ ] **Step 2: Mount it** inside `OverviewTab` (where `canEditContent`, `topicRowId`, and `topic` are already in scope — the gate is computed at ~line 1489-1491; NOT the title editor at ~1996, per Fable review #11). Render only for authorized reviewers, near the topic title:
```javascript
      canEditContent && topicRowId ? React.createElement(TopicReviewButtons, {
        topicRowId:     topicRowId,
        workClass:      topic.work_class,
        workConfidence: topic.work_confidence,
        category:       topic.category,
        onRemoved:      null,   // v1: toast + next data refresh; no onContentChanged prop exists here
      }) : null,
```

- [ ] **Step 3: Removed-area + revert (Fable review #6, spec §5 "hidden + recoverable").** The timeline topic list must (a) NOT show `redacted` topics in the default flow, and (b) list them in a collapsible "已移除 / 个人" section with a revert control. Where the middle column maps `report.topics` to `TopicCard`s, partition on the new `redacted` flag:
```javascript
      var visibleTopics = (report.topics || []).filter(function (t) { return !t.redacted; });
      var removedTopics = (report.topics || []).filter(function (t) { return t.redacted; });
```
Render `visibleTopics` as today. Below them, when `removedTopics.length && canEditContent`, render a collapsed section; each row shows the title + a revert button:
```javascript
      function RemovedTopic(props) {
        var IconBtn = window.FieldSight.IconButton;
        var busyRef = React.useState(false); var busy = busyRef[0], setBusy = busyRef[1];
        return React.createElement('div', { className: 'fs-timeline-page__removed-row' },
          React.createElement('span', null, unfolder(props.topic.topic_title || props.topic.title || 'Removed')),
          IconBtn ? React.createElement(IconBtn, {
            icon: 'rotate-ccw', size: 'sm', variant: 'ghost', disabled: busy || !props.topic.redaction_id,
            ariaLabel: '恢复',
            onClick: function () {
              if (busy || !props.topic.redaction_id) return;
              setBusy(true);
              window.FS.api.actions.revertRedaction(props.topic.redaction_id).then(function (r) {
                setBusy(false);
                var toast = window.FS && window.FS.toast;
                if (!r || r._accessDenied) { if (toast) toast.show({ message: (r && r.error) || 'Could not restore', tone: 'error', duration: 5000 }); return; }
                if (toast) toast.show({ message: 'Restored', tone: 'success', duration: 3000 });
              }).catch(function () { setBusy(false); });
            },
          }) : null);
      }
```
> This makes `revertRedaction` (Task 10) a live call. `unfolder` already exists in `timeline.js`. Keep the section behind `canEditContent` — site/self reviewers only.

- [ ] **Step 4: Bump cache busters**

`app-shell-preview.html`:
- line 203: `scripts/api/actions.js?v=9` → `?v=10`
- line 238: `scripts/pages/timeline.js?v=38` → `?v=39`

- [ ] **Step 5: Syntax-check + commit**

Run: `node --check scripts/pages/timeline.js`
```bash
git add scripts/pages/timeline.js app-shell-preview.html
git commit -m "feat(ui): topic review buttons + removed-area with revert"
```

---

# SLICE 4 — Feedback report

### Task 12: classification-feedback summary endpoint

**Files:**
- Modify: `src/lambda_org_api.py` (handler + route)
- Test: `tests/unit/test_lambda_org_api.py`

**Interfaces:**
- Consumes: `classification_feedback.summary(conn, company_id)` (Task 3).
- Produces route: `GET /api/org/classification-feedback/summary` (admin/gm/platform_admin only).

- [ ] **Step 1: Write the failing test**
```python
def test_feedback_summary_admin_only(wired):
    wired.setattr(org.classification_feedback, "summary",
                  lambda conn, cid: {"confirm_non_work": 3, "reject_is_work": 1,
                                     "missed_personal": 0, "precision": 0.75})
    res = org.lambda_handler(make_event("GET", "/api/org/classification-feedback/summary"), None)
    assert res["statusCode"] == 200 and body_of(res)["precision"] == 0.75
    # a worker is denied
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "worker"})
    res2 = org.lambda_handler(make_event("GET", "/api/org/classification-feedback/summary"), None)
    assert res2["statusCode"] == 403
```

- [ ] **Step 2: Run — expect FAIL**

Run: `uv run pytest tests/unit/test_lambda_org_api.py -k feedback_summary -v`

- [ ] **Step 3: Add the handler + route**

Handler (near the feedback endpoint):
```python
def classification_feedback_summary_endpoint(conn, caller):
    """Classifier accuracy roll-up (spec §7). Admin/gm/platform_admin only —
    metadata, no PII, but company-wide so gate to ALL-scope roles. platform_admin
    is cross-company (resolve_scope != ALL for it) so include it explicitly
    (Fable review #5 — the recurring 'teach each endpoint about platform_admin')."""
    if resolve_scope(caller["global_role"]) != "ALL" and not is_cross_company(caller["global_role"]):
        return error("admin or gm role required", 403)
    return ok(classification_feedback.summary(conn, caller["company_id"]))
```
> The `test_feedback_summary_admin_only` test's worker-403 assertion still holds; add a `platform_admin` 200 case.
Route (put the specific `/summary` route BEFORE the `POST /classification-feedback` route so it can't be shadowed — both are distinct here since methods differ, but keep them adjacent):
```python
    if route == "/classification-feedback/summary" and method == "GET":
        return classification_feedback_summary_endpoint(conn, caller)
```

- [ ] **Step 4: Run — expect PASS**, then commit
```bash
git add src/lambda_org_api.py tests/unit/test_lambda_org_api.py
git commit -m "feat(org-api): classification-feedback summary (accuracy roll-up)"
```

---

## Notes for the executor

- **Migrations apply** via `MigrateFunction` on deploy (idempotent); locally the `db` fixture applies them. No manual apply step.
- **No template.yaml / IAM change**: `redactions` and `classification_feedback` are Aurora tables reached by the already-VPC'd `OrgApiFunction`; the reindex reuse writes to the same `reindex_requests/` prefix the org-api already has PutObject on.
- **Enforcement coverage** shipped here: rollup aggregation (Task 4) — the PRIMARY "personal content not in later analysis" guarantee, **fully effective everywhere**; plus RAG delete-only reindex (Task 5) + `chunk_report` guard (Task 6, made work_class-aware on the reindex path by Task 1b), which are the correct RAG mechanism but bounded by the gap below.
- **⚠️ KNOWN RESIDUAL RAG GAP (verified in `lambda_ingest.ingest_report`; scoped OUT of v1 — decision A, 2026-07-21). The company-tier rollup exclusion is unaffected; this is RAG-only:**
  1. **RAG embedding is nightly-report-doc-driven, not extraction-driven.** `ingest_report` chunks the `daily_report.json` — whose topics carry no `work_class` (report_generator unmodified) — so the extraction topics that DO carry `work_class` are never the embed input; `chunk_report`'s guard fires only on the reindex path, not the nightly embed. An investigated-and-**REJECTED** "lightweight" idea was to have `lambda_item_writer` enqueue a delete-only reindex per non_work topic — it does NOT work: item_writer's extraction topics are never embedded, so there is nothing to delete.
  2. **On authority-flip days (the prod default), chunks are inserted `topic_id=None`** (`lambda_ingest.py:330`), so `delete_chunks_for_topic(topic_id)` targets nothing. Net: the delete-only reindex (Task 5) removes a redacted/non_work topic's vectors **only on non-flip days**; confirmed-personal is removed from company-tier ANALYTICS everywhere, and from RAG only on non-flip days. `chunk_transcripts`' raw per-turn text is also unguarded.
  3. **Proper fix = a separate "embedding re-architecture" plan:** build RAG chunks on the defer path from the extraction topics (via the now-work_class-aware `render_report_shape`) instead of the report doc — making chunks topic-keyed AND work_class-aware, so the guard and the delete-only reindex both work on flip days. Tasks 1b/5/6 are the correct mechanism and become fully effective once that lands.
  4. **Tombstone vs re-extraction:** `redactions.target_id` pins a topic uuid; nightly re-ingest is delete-and-reinsert (new uuid), so a manual redaction (especially `missed_personal` on a topic the classifier calls `work`) is silently lost on re-extraction. Mirrors the 0019 no-FK posture; acceptable for v1 — the durable fix is to re-apply active redactions after re-extraction by (site, date, title) match.
- **Out of scope (spec §9):** F1 masking, `review_state` publish-gate, segment-level, hard-purge.
