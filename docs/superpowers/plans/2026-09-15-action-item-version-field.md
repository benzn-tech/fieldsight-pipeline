# Action-item `version` on /timeline — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every action item in the `/timeline` rendered shape carries `version = 1 + (number of content_edits rows for that action item)`.

**Architecture:** The two day-read repository functions (`list_topics_for_date`, `list_topics_for_source_prefix`) stamp `edit_count` on the action items that survive `todo_collapse`, using ONE batched `GROUP BY` query per call. The fixed-allowlist serializer `render_report_shape` turns that into `version`. Rows that never went through those functions (reindex via `get_topic_full`) have no `edit_count` and serialize `version: 1`.

**Tech Stack:** Python 3.11 Lambda, psycopg 3, Aurora Postgres, pytest with FakeConn doubles, RDS Data API for real-SQL checks.

**Spec:** `C:/Users/camil/Dropbox/wt-ui-specs/docs/specs/2026-09-15-todo-card-history-and-save-feedback.md`: §8.1 (backend) and §3.4 (numbering rule). Only §8.1 is in scope. Everything else in that spec is frontend.

## Global Constraints

- Additive only. No other key in the `/timeline` payload changes.
- `version = 1 + count(content_edits WHERE table_name='action_items' AND row_id = <item id>)`. Every field counts (text, status, priority, deadline, responsible).
- Count only the **survivors'** ids. `collapsed_ids` are not counted. That matches `GET /content/action_items/{id}/history`, which is per `row_id`.
- **No company predicate** on the count. Every writer stamps the row's own company, and `get_content_history` filters by the row's company, so the unscoped count equals what history returns (spec §8.1).
- One count query per repository call, whatever the item count. **Zero** queries when there are no surviving items.
- Serializer default when `edit_count` is absent: `version: 1`.
- Dev artefacts (code comments, commit messages, PR body) are in English. Every commit message ends with:
  ```
  Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_012vJF6RLXaUEWuai8RrFknd
  ```
- Windows `autocrlf`: run `git diff --stat` before every commit. If a file shows whole-file churn, stop and use single-line anchored edits instead. Never use `git add -A`.

## Deviations from the spec, found by reading the code

1. **The session-report preview does NOT get `version: 1`.** Spec §8.1 says the preview (`lambda_org_api.py:1251`) carries no `edit_count`. But its rows come from `_day_report_rows` (`:1160`), which calls `topics.list_topics_for_source_prefix`, the same function `/timeline` uses. So the preview inherits the real count. This is harmless: additive, the same query, and the preview then agrees with Today's chip. The plan pins the actual behaviour. Only reindex (`get_topic_full`) serializes `version: 1` without a query.
2. **The query casts `%s::uuid[]`.** The spec text writes `row_id = ANY(%s)`. The ids are passed as `str`, and psycopg adapts `list[str]` as `text[]`. `uuid = text` has no operator in Postgres, so the uncast query errors at runtime. The FakeConn cannot see this. Task 3 proves the cast against a real database. The same `::uuid[]` idiom already exists in `topics.py` (`t.user_id = ANY(%s::uuid[])`).

## Workspace

`C:/Users/camil/Dropbox/wt-pipe-specs` is on another agent's branch (`docs/scoped-ask`). Do not implement there. Create a fresh worktree off `origin/develop`:

```bash
cd C:/Users/camil/Dropbox/wt-pipe-specs
git fetch origin
git worktree add ../wt-action-version -b feat/action-item-version origin/develop
```

All paths below are relative to `C:/Users/camil/Dropbox/wt-action-version`.

Unit test command (named `UNIT` below):

```bash
export UV_LINK_MODE=copy AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_DEFAULT_REGION=ap-southeast-2
uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit -q
```

To run a single file, replace `tests/unit` with the file path.

---

### Task 1: Repository stamps `edit_count` on surviving action items

**Files:**
- Modify: `src/repositories/topics.py`. Add a helper above `list_topics_for_date` (~359). Change the collapse loops at ~485 and ~732.
- Modify: `tests/unit/test_topics_repo.py`. Two existing tests whose FakeConn queues shift (~117 and ~381).
- Create: `tests/unit/test_action_item_edit_count.py`

**Interfaces:**
- Produces: every action-item dict returned by `topics.list_topics_for_date(...)` and `topics.list_topics_for_source_prefix(...)` carries `edit_count: int` (0 when unedited). `get_topic_full` is unchanged and carries no `edit_count`.
- Produces: `topics._stamp_edit_counts(conn, items: list[dict]) -> list[dict]`. It mutates and returns `items`. It issues no query when `items` is empty.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_action_item_edit_count.py`:

```python
"""Unit: a to-do's version is 1 + its content_edits rows (todo-card spec §3.4 / §8.1).

The repository counts; the serializer (render_report_shape) turns the count into
`version`. These tests pin the repository half: one batched query, over the
SURVIVORS of the collapse only, and no query at all when there is nothing to count.
FakeConn records SQL and never parses it -- the real-SQL half lives in
tests/integration/test_action_item_edit_counts.py.
"""
import pytest

from tests.unit.test_topics_repo import FakeConn

topics = pytest.importorskip("repositories.topics", reason="requires psycopg (installed in CI)")

PREFIX = "extractions/Ada_L/2026-09-01/"


def _topic(tid):
    return {"id": tid, "site_id": "s-1", "user_id": "u-1",
            "source_s3_key": PREFIX + "sidAAA.json", "report_date": "2026-09-01",
            "occurred_at": None, "category": "progress", "title": tid, "summary": "s",
            "time_range": None, "participants": [], "source": "ai", "created_at": "c0",
            "site_name": "Alpha", "user_name": "Ada L"}


def _item(aid, tid, text="Order timber", created_at=1):
    return {"id": aid, "topic_id": tid, "text": text, "responsible": None,
            "deadline": None, "deadline_text": None, "priority": None,
            "status": "open", "created_at": created_at}


def _edit_calls(conn):
    return [c for c in conn.calls if "content_edits" in c["sql"]]


def test_an_unedited_item_counts_zero(monkeypatch):
    monkeypatch.delenv("ENABLE_TODO_COLLAPSE", raising=False)
    conn = FakeConn(results=[[_topic("t-1")], [_item("a-1", "t-1")], [], [], []])
    rows = topics.list_topics_for_date(conn, ["s-1"], "2026-09-01")
    assert rows[0]["action_items"][0]["edit_count"] == 0


def test_three_edits_count_three_in_one_batched_query(monkeypatch):
    monkeypatch.delenv("ENABLE_TODO_COLLAPSE", raising=False)
    items = [_item("a-1", "t-1"), _item("a-2", "t-1", text="Book pump"),
             _item("a-3", "t-1", text="Call engineer")]
    conn = FakeConn(results=[
        [_topic("t-1")],
        items,
        [{"row_id": "a-2", "n": 3}],   # content_edits counts
        [], [], [],                    # safety, findings, photos
    ])
    rows = topics.list_topics_for_source_prefix(conn, PREFIX)
    by_id = {a["id"]: a for a in rows[0]["action_items"]}
    assert by_id["a-2"]["edit_count"] == 3
    assert by_id["a-1"]["edit_count"] == 0
    calls = _edit_calls(conn)
    assert len(calls) == 1, "the count must be ONE query for all items, never per item"
    sql = calls[0]["sql"]
    assert "table_name = 'action_items'" in sql
    assert "row_id = ANY(%s::uuid[])" in sql
    assert "GROUP BY row_id" in sql
    assert "company_id" not in sql     # deliberate: see spec §8.1
    assert calls[0]["params"] == (["a-1", "a-2", "a-3"],)


def test_a_collapsed_pair_counts_the_survivor_only(monkeypatch):
    """Two recordings, two topics, one commitment. History is per row_id, so the
    chip must show the survivor's count -- the collapsed row's edits are not
    reachable from the card that is shown."""
    monkeypatch.setenv("ENABLE_TODO_COLLAPSE", "true")
    conn = FakeConn(results=[
        [_topic("t-1"), _topic("t-2")],
        [_item("a-1", "t-1", created_at=1), _item("a-2", "t-2", created_at=2)],
        [{"row_id": "a-1", "n": 2}],
        [], [], [],
    ])
    rows = topics.list_topics_for_source_prefix(conn, PREFIX)
    survivor = rows[0]["action_items"][0]
    assert survivor["id"] == "a-1" and survivor["collapsed_ids"] == ["a-2"]
    assert survivor["edit_count"] == 2
    assert _edit_calls(conn)[0]["params"] == (["a-1"],), "a collapsed id was counted"


def test_no_surviving_items_issues_no_count_query(monkeypatch):
    monkeypatch.delenv("ENABLE_TODO_COLLAPSE", raising=False)
    conn = FakeConn(results=[[_topic("t-1")], [], [], []])
    topics.list_topics_for_date(conn, ["s-1"], "2026-09-01")
    assert _edit_calls(conn) == []
    assert len(conn.calls) == 4      # main + action_items + safety + findings


def test_get_topic_full_does_not_count(monkeypatch):
    """Reindex's read. It embeds text; a version has no meaning there."""
    monkeypatch.delenv("ENABLE_TODO_COLLAPSE", raising=False)
    conn = FakeConn(results=[[_topic("t-1")], [_item("a-1", "t-1")], [], [], []])
    full = topics.get_topic_full(conn, "t-1")
    assert _edit_calls(conn) == []
    assert "edit_count" not in full["action_items"][0]
```

- [ ] **Step 2: Run the tests and verify they fail**

Run `UNIT` on `tests/unit/test_action_item_edit_count.py`.
Expected: the first four tests FAIL with `KeyError: 'edit_count'` or on the call-count asserts. `test_get_topic_full_does_not_count` PASSES already (it pins that nothing changes there).

- [ ] **Step 3: Implement the helper and the two call sites**

In `src/repositories/topics.py`, directly above `def list_topics_for_date`:

```python
# One count per day read, over the action items that survived the collapse.
# `version = 1 + edit_count` is what Today's to-do chip shows (todo-card spec
# §3.4), and it has to agree with GET /content/action_items/{id}/history, which
# lists content_edits by row_id -- so collapsed_ids are NOT counted, and there is
# no company predicate: every writer stamps the row's own company and history
# filters on the row's company, so the unscoped count is the same number.
# `::uuid[]` is required: the ids are passed as str, and uuid = text has no
# operator. Served by idx_content_edits_row (migration 0019).
_EDIT_COUNT_SQL = (
    "SELECT row_id, count(*) AS n FROM content_edits "
    "WHERE table_name = 'action_items' AND row_id = ANY(%s::uuid[]) "
    "GROUP BY row_id"
)


def _stamp_edit_counts(conn, items):
    if not items:
        return items
    ids = [str(a["id"]) for a in items]
    counts = {str(r["row_id"]): int(r["n"]) for r in conn.cursor(row_factory=dict_row)
              .execute(_EDIT_COUNT_SQL, (ids,)).fetchall()}
    for a in items:
        a["edit_count"] = counts.get(str(a["id"]), 0)
    return items
```

In `list_topics_for_date` (~485), replace:

```python
    for a in todo_collapse.collapse_if_enabled(_all_items):
        action_items_by_topic.setdefault(a["topic_id"], []).append(a)
```

with:

```python
    for a in _stamp_edit_counts(conn, todo_collapse.collapse_if_enabled(_all_items)):
        action_items_by_topic.setdefault(a["topic_id"], []).append(a)
```

Make the identical replacement in `list_topics_for_source_prefix` (~732). There are exactly two occurrences with a `for a in` prefix. `get_topic_full` (~818) assigns the collapse result directly and must stay untouched.

- [ ] **Step 4: Run the new tests and verify they pass**

Run `UNIT` on `tests/unit/test_action_item_edit_count.py`. Expected: 5 passed.

- [ ] **Step 5: Run the full unit suite and fix the two shifted queues**

Run `UNIT`. Expected: exactly two failures in `tests/unit/test_topics_repo.py`, because both feed a non-empty action item, so a new execute() now sits between action_items and safety.

`test_list_topics_for_date_joins_and_children_and_is_live` (~117): change the queue, call count, indices and item assertion:

```python
    conn = FakeConn(results=[
        [topic_report, topic_live],   # main topics query
        [action_row],                 # action_items children
        [],                           # content_edits counts (survivors only)
        [safety_row],                 # safety_observations children
        [],                           # findings children
    ])

    rows = topics.list_topics_for_date(conn, ["site-1", "site-2"], "2026-07-06")

    assert len(conn.calls) == 5
```

Replace the children assertions block with:

```python
    assert conn.calls[1]["params"] == (["t-1", "t-2"],)
    assert conn.calls[2]["params"] == (["a-1"],)
    assert conn.calls[3]["params"] == (["t-1", "t-2"],)
    assert conn.calls[4]["params"] == (["t-1", "t-2"],)
    assert "action_items" in conn.calls[1]["sql"]
    assert "content_edits" in conn.calls[2]["sql"]
    assert "safety_observations" in conn.calls[3]["sql"]
    assert "findings" in conn.calls[4]["sql"]
```

Also change `assert by_id["t-2"]["action_items"] == [action_row]` to `assert by_id["t-2"]["action_items"] == [{**action_row, "edit_count": 0}]`.

`test_list_for_source_prefix_orders_by_time_range_and_batches_four_children` (~381): insert `[],  # content_edits counts` after `[action_row],`. Change `assert len(conn.calls) == 5` to `== 6`. Replace the loop and SQL asserts with:

```python
    for i in (1, 3, 4, 5):
        assert conn.calls[i]["params"] == (["t-1", "t-2"],)
    assert conn.calls[2]["params"] == (["a-1"],)
    assert "action_items" in conn.calls[1]["sql"]
    assert "deadline_text" in conn.calls[1]["sql"]
    assert "content_edits" in conn.calls[2]["sql"]
    assert "safety_observations" in conn.calls[3]["sql"]
    assert "findings" in conn.calls[4]["sql"]
    assert "topic_photos" in conn.calls[5]["sql"]
```

Also change `assert by_id["t-1"]["action_items"] == [action_row]` to `assert by_id["t-1"]["action_items"] == [{**action_row, "edit_count": 0}]`.

Run `UNIT` again. Expected: all pass. If any OTHER test fails, it is a FakeConn queue fed a non-empty action item through one of these two functions. Fix it the same way (insert one `[]` after the action_items entry) and do not change the implementation.

- [ ] **Step 6: Mutation check (the test must be able to go red)**

Temporarily change `ids = [str(a["id"]) for a in items]` to also append collapsed ids: `ids += [str(x) for a in items for x in (a.get("collapsed_ids") or [])]`. Run `UNIT` on the new file. Expected: `test_a_collapsed_pair_counts_the_survivor_only` FAILS. Revert the change with an Edit and re-run: PASS.

- [ ] **Step 7: Commit**

```bash
git diff --stat
git add src/repositories/topics.py tests/unit/test_topics_repo.py tests/unit/test_action_item_edit_count.py
git commit -m "feat(topics): stamp edit_count on surviving action items in day reads

One batched GROUP BY over content_edits per call, survivors of the todo
collapse only, no query when there are no items. Feeds the to-do version
chip (todo-card spec 3.4 / 8.1).

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_012vJF6RLXaUEWuai8RrFknd"
```

---

### Task 2: `render_report_shape` emits `version`, tested on the rendered /timeline body

**Files:**
- Modify: `src/lambda_org_api.py`, the `action_items` allowlist in `render_report_shape` (~6209-6214) plus the comment above it (~6195-6208).
- Modify: `tests/unit/test_lambda_org_api.py`. Add two tests after `test_an_uncollapsed_row_still_says_it_was_mentioned_once` (~7301).
- Create: `tests/unit/test_timeline_carries_action_item_version.py`

**Interfaces:**
- Consumes: `edit_count: int` on action-item dicts (Task 1). It may be absent.
- Produces: every `topics[].action_items[]` in the rendered shape has `version: int >= 1`.

- [ ] **Step 1: Write the failing serializer tests**

Append to `tests/unit/test_lambda_org_api.py`:

```python
def test_render_shape_carries_the_action_item_version():
    """version = 1 + edit_count (todo-card spec 3.4). This serializer is a fixed
    allowlist that has already dropped two repository fields on the way out
    (mention_count, collapsed_ids) with every repository test green, so the
    count is asserted HERE, on what the browser receives."""
    row = _topic_row(action_items=[
        {"id": "a-1", "text": "Order timber", "responsible": None,
         "deadline": None, "deadline_text": None, "priority": None,
         "status": "open", "edit_count": 3},
        {"id": "a-2", "text": "Book pump", "responsible": None,
         "deadline": None, "deadline_text": None, "priority": None,
         "status": "open", "edit_count": 0},
    ])
    items = org.render_report_shape([row], None, "2026-09-01", "Ada_L")["topics"][0]["action_items"]
    assert [i["version"] for i in items] == [4, 1]


def test_a_row_that_was_never_counted_is_version_one():
    """get_topic_full (reindex) does not count. Absent is v1, never missing."""
    row = _topic_row(action_items=[
        {"id": "a-1", "text": "Order timber", "responsible": None,
         "deadline": None, "deadline_text": None, "priority": None,
         "status": "open"},
    ])
    item = org.render_report_shape([row], None, "2026-09-01", "Ada_L")["topics"][0]["action_items"][0]
    assert item["version"] == 1
```

- [ ] **Step 2: Write the failing handler-path test**

Create `tests/unit/test_timeline_carries_action_item_version.py`:

```python
"""Unit: the serialized /timeline 200 carries each to-do's version.

Driven through `_render_timeline_for_user` with the REAL render_report_shape, so
the assertion is on the JSON body a browser receives rather than on repository
output -- the layer where mention_count was once silently dropped.
"""
import json

import pytest

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")

SITE_ID = "a1a1a1a1-a1a1-a1a1-a1a1-a1a1a1a1a1a1"
DATE, USER = "2026-09-01", "Ada_L"
CALLER = {
    "id": "u-1", "cognito_sub": "sub-1", "company_id": "c-1", "email": "a@x.nz",
    "first_name": "Ada", "last_name": "L", "folder_name": "Ada_L",
    "avatar_s3_key": None, "global_role": "admin", "created_at": "2026-07-25",
}
ROW = {
    "id": "t-1", "site_id": SITE_ID, "site_name": "Alpha", "user_name": "Ada L",
    "source_s3_key": f"extractions/{USER}/{DATE}/sid" + "a" * 32 + ".json",
    "category": "progress", "title": "Slab", "summary": "s", "time_range": None,
    "participants": [], "safety_observations": [], "findings": [], "photos": [],
    "action_items": [{"id": "a-1", "text": "Order timber", "responsible": None,
                      "deadline": None, "deadline_text": None, "priority": None,
                      "status": "open", "edit_count": 1}],
}


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_the_timeline_body_carries_version(monkeypatch):
    monkeypatch.setattr(org.topics, "has_topics_for_source_prefix",
                        lambda conn, prefix: prefix.startswith("extractions/"))
    monkeypatch.setattr(org.topics, "list_topics_for_source_prefix",
                        lambda conn, prefix, **kw: [dict(ROW)])
    monkeypatch.setattr(org, "_allowed_site_ids", lambda conn, caller: {SITE_ID})
    monkeypatch.setattr(org, "_get_lake_json", lambda key: None)
    monkeypatch.setattr(org.redactions, "list_active_for_topics", lambda conn, ids: {})
    monkeypatch.setattr(org.recordings, "day_stats",
                        lambda conn, company, folder, date: {"sessions": 0, "duration_s": 0})
    monkeypatch.setattr(org.recordings, "photo_list_for_day",
                        lambda conn, company, folder, date: [])
    monkeypatch.setattr(org.redactions, "deleted_photo_keys",
                        lambda conn, company, keys=None: set())

    res = org._render_timeline_for_user(FakeConn(), dict(CALLER), DATE, USER)

    assert res["statusCode"] == 200
    item = json.loads(res["body"])["topics"][0]["action_items"][0]
    assert item["version"] == 2
```

- [ ] **Step 3: Run the tests and verify they fail**

Run `UNIT` on `tests/unit/test_timeline_carries_action_item_version.py` and on `tests/unit/test_lambda_org_api.py -k "version"`.
Expected: all 3 FAIL with `KeyError: 'version'`. If the handler test fails earlier on an unrelated conn call (`AttributeError: 'FakeConn' object has no attribute 'cursor'`), stub that function with `monkeypatch.setattr` in the same style and re-run until the ONLY failure is `KeyError: 'version'`.

- [ ] **Step 4: Implement**

In `src/lambda_org_api.py` `render_report_shape`, add one line to the action-item dict, after `collapsed_ids`:

```python
                              "mention_count": a.get("mention_count", 1),
                              "collapsed_ids": [str(x) for x in (a.get("collapsed_ids") or [])],
                              # 1 + content_edits rows for this item (todo-card spec 3.4).
                              # edit_count is stamped by the two day reads in
                              # repositories/topics.py; get_topic_full (reindex) does not
                              # count, so its rows are v1. The session-report preview reads
                              # through list_topics_for_source_prefix and carries the count.
                              "version": 1 + int(a.get("edit_count") or 0),
                              } for a in t["action_items"]],
```

In the comment above (~6208), change `Any future field the collapse adds has to be added here too.` to `Any future field the collapse or the day reads add (edit_count -> version) has to be added here too.`

- [ ] **Step 5: Run the tests and verify they pass**

Run `UNIT` (the full suite). Expected: all pass. If a test asserts the exact key set of a rendered action item, add `"version": 1` to its expected dict. That is the additive field, not a regression.

- [ ] **Step 6: Mutation check**

Delete the `"version"` line and run `UNIT` on the new handler file. Expected: FAIL. Restore it and re-run: PASS.

- [ ] **Step 7: Commit**

```bash
git diff --stat
git add src/lambda_org_api.py tests/unit/test_lambda_org_api.py tests/unit/test_timeline_carries_action_item_version.py
git commit -m "feat(org-api): emit action item version on the rendered timeline shape

version = 1 + edit_count, added to render_report_shape's fixed allowlist
and asserted on the serialized /timeline body. Rows that were never
counted (reindex via get_topic_full) render as version 1.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_012vJF6RLXaUEWuai8RrFknd"
```

---

### Task 3: The count query runs on real Postgres

FakeConn never parses SQL. This task is where the `::uuid[]` cast, the `GROUP BY`, and the `table_name` filter get answered by a database.

**Files:**
- Create: `tests/integration/test_action_item_edit_counts.py`

**Interfaces:**
- Consumes: `topics.list_topics_for_date`, `topics.list_topics_for_source_prefix`, `topics.get_topic_full` (Task 1).

- [ ] **Step 1: Run the query against fieldsight_test via the Data API (read-only, rolled back)**

Use Git Bash:

```bash
export AWS_PROFILE=fieldsight-deployer MSYS_NO_PATHCONV=1 AWS_DEFAULT_REGION=ap-southeast-2
CL=arn:aws:rds:ap-southeast-2:509194952652:cluster:fieldsight-db-test-dbcluster-hywiixu8ihi9
SEC='arn:aws:secretsmanager:ap-southeast-2:509194952652:secret:rds!cluster-1757a281-ee31-460d-b56e-950817921010-Ansbey'
TX=$(aws rds-data begin-transaction --resource-arn "$CL" --secret-arn "$SEC" \
      --database fieldsight_test --query transactionId --output text)
# The Data API has no list binding: pass a Postgres array literal as a string and CAST it.
# (psycopg's `%s::uuid[]` in the code is the same cast, different placeholder syntax.)
aws rds-data execute-statement --resource-arn "$CL" --secret-arn "$SEC" \
  --database fieldsight_test --transaction-id "$TX" \
  --sql "SELECT row_id, count(*) AS n FROM content_edits WHERE table_name = 'action_items' AND row_id = ANY(CAST(:ids AS uuid[])) GROUP BY row_id" \
  --parameters '[{"name":"ids","value":{"stringValue":"{2e3b17e0-4317-41e0-96f4-19fde87f3002,00000000-0000-0000-0000-000000000000}"}}]'
aws rds-data execute-statement --resource-arn "$CL" --secret-arn "$SEC" \
  --database fieldsight_test --transaction-id "$TX" \
  --sql "SELECT field, company_id, created_at FROM content_edits WHERE table_name = 'action_items' AND row_id = '2e3b17e0-4317-41e0-96f4-19fde87f3002'::uuid"
aws rds-data rollback-transaction --resource-arn "$CL" --secret-arn "$SEC" --transaction-id "$TX"
```

Expected:
- The first statement returns exactly one record: `2e3b17e0-…` with `n = 1`. The all-zero uuid is absent, because no row means no group, which is what the code's `.get(id, 0)` relies on.
- The second statement returns one `status` row.

If `n` is not 1, **stop**. The Task 4 expected value (`version: 2`) is then wrong and must be recomputed from this output. Do not edit the data.

- [ ] **Step 2: Write the integration tests**

Create `tests/integration/test_action_item_edit_counts.py`:

```python
"""Integration: the action-item edit count is valid SQL and counts the right rows.

The unit doubles record the SQL string and never parse it, so three properties of
the count can only be answered here: the `::uuid[]` cast (ids are passed as str;
uuid = text has no operator), the table_name filter (content_edits is polymorphic
-- a findings edit that happens to share nothing but a row_id must not count), and
GROUP BY returning no row for an unedited item.

Skipped unless TEST_DATABASE_URL is set (tests/conftest.py).
"""
import uuid

import pytest

from repositories import companies, sites, topics, users

pytestmark = pytest.mark.integration

DATE = "2026-09-01"


def _seed(db):
    co = companies.create_company(db, f"Ver-Co-{uuid.uuid4().hex[:6]}")
    s = sites.create_site(db, co["id"], "Ver-Site")
    u = users.upsert_user(db, f"sub-{uuid.uuid4().hex[:8]}", "v@x.nz", company_id=co["id"])
    src = f"extractions/VerFolder/{DATE}/sid{uuid.uuid4().hex}.json"
    t = topics.upsert_topic(
        db, s["id"], DATE, "Slab", user_id=u["id"], source_s3_key=src,
        action_items=[{"text": "Order timber"}, {"text": "Book pump"}])
    return co, s, t


def _edit(db, company_id, table, row_id, field="status"):
    db.execute(
        "INSERT INTO content_edits (company_id, table_name, row_id, field, before_text, after_text) "
        "VALUES (%s, %s, %s, %s, 'open', 'done')",
        (company_id, table, row_id, field))


def _items(rows):
    return {a["text"]: a for r in rows for a in r["action_items"]}


def test_unedited_items_count_zero(db):
    _co, s, _t = _seed(db)
    items = _items(topics.list_topics_for_date(db, [s["id"]], DATE))
    assert items["Order timber"]["edit_count"] == 0
    assert items["Book pump"]["edit_count"] == 0


def test_three_edits_on_any_field_count_three_in_both_reads(db):
    co, s, _t = _seed(db)
    aid = _items(topics.list_topics_for_date(db, [s["id"]], DATE))["Order timber"]["id"]
    for field in ("text", "deadline", "status"):
        _edit(db, co["id"], "action_items", aid, field)

    by_date = _items(topics.list_topics_for_date(db, [s["id"]], DATE))
    by_prefix = _items(topics.list_topics_for_source_prefix(db, f"extractions/VerFolder/{DATE}/"))
    assert by_date["Order timber"]["edit_count"] == 3
    assert by_prefix["Order timber"]["edit_count"] == 3
    assert by_date["Book pump"]["edit_count"] == 0


def test_an_edit_on_another_table_with_the_same_row_id_does_not_count(db):
    co, s, _t = _seed(db)
    aid = _items(topics.list_topics_for_date(db, [s["id"]], DATE))["Order timber"]["id"]
    _edit(db, co["id"], "findings", aid)
    assert _items(topics.list_topics_for_date(db, [s["id"]], DATE))["Order timber"]["edit_count"] == 0


def test_get_topic_full_carries_no_count(db):
    _co, _s, t = _seed(db)
    full = topics.get_topic_full(db, t["id"])
    assert full["action_items"] and all("edit_count" not in a for a in full["action_items"])
```

- [ ] **Step 3: Run the integration tests where a database is reachable**

The test cluster is VPC-private, so `TEST_DATABASE_URL` is usually unavailable locally. First confirm the file collects and skips cleanly:

```bash
uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/integration/test_action_item_edit_counts.py -q
```

Expected: `4 skipped` (or `no tests ran`, since conftest ignores `integration/*` without the URL). No collection error. CI runs them where `TEST_DATABASE_URL` exists. Step 1 was the real-database proof for this session.

If `TEST_DATABASE_URL` IS set: expected `4 passed`. Mutation check: remove `::uuid[]` from `_EDIT_COUNT_SQL` and run again. Expected: FAIL with `operator does not exist: uuid = text`. Restore it.

- [ ] **Step 4: Commit**

```bash
git diff --stat
git add tests/integration/test_action_item_edit_counts.py
git commit -m "test(integration): action item edit counts against real Postgres

Pins the uuid[] cast, the table_name filter on the polymorphic
content_edits table, and zero for unedited items. Query also verified
against fieldsight_test via the Data API in a rolled-back transaction.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_012vJF6RLXaUEWuai8RrFknd"
```

---

### Task 4: PR to develop, then verify on TEST

The user decides the merge. Do not merge.

- [ ] **Step 1: Final suite and push**

Run `UNIT`. Expected: all pass. Then:

```bash
git push -u origin feat/action-item-version
```

- [ ] **Step 2: Open the PR**

```bash
gh pr create --base develop --head feat/action-item-version \
  --title "feat: action item version on /timeline (todo-card spec 8.1)" \
  --body "$(cat <<'EOF'
## What
Each `/timeline` action item carries `version = 1 + content_edits rows for that item` (fieldsight-ui spec `docs/specs/2026-09-15-todo-card-history-and-save-feedback.md` §3.4 / §8.1). Additive; the UI treats a missing `version` as 1.

## How
- `repositories/topics.py`: `list_topics_for_date` and `list_topics_for_source_prefix` stamp `edit_count` on the items that survive `todo_collapse`, using one batched `GROUP BY` (`row_id = ANY(%s::uuid[])`) per call and no query when there are no items. `collapsed_ids` are not counted (history is per row_id). There is no company predicate (writers stamp the row's company and history filters on it).
- `render_report_shape`: `"version": 1 + edit_count` in the fixed allowlist.

## Deviations from the spec
- The session-report preview reads through `list_topics_for_source_prefix`, so it carries the real version, not 1. Only reindex (`get_topic_full`) is uncounted.
- The spec's `ANY(%s)` needs `::uuid[]` (str ids, `uuid = text` has no operator).

## Verification
- Unit tests: 0 edits -> v1, 3 -> v4, collapsed pair -> survivor only, no items -> no query, rendered `/timeline` body carries `version`, `get_topic_full` uncounted.
- Count query run against `fieldsight_test` via the RDS Data API (rolled back): item `2e3b17e0-…` -> n=1.
- `tests/integration/test_action_item_edit_counts.py` (runs where `TEST_DATABASE_URL` exists).
- After merge: TEST `/api/org/timeline?date=2026-09-01&user=Ben_UCPK2` -> that item `version: 2`.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_012vJF6RLXaUEWuai8RrFknd
EOF
)"
```

Report the PR URL to the user and stop until they merge.

- [ ] **Step 3: After the user merges, confirm TEST runs the merge commit**

```bash
MERGE_SHA=$(gh pr view feat/action-item-version --json mergeCommit --jq .mergeCommit.oid)
gh run list --branch develop --limit 5 --json headSha,status,conclusion,name
```

Wait until the run whose `headSha == $MERGE_SHA` shows `conclusion: success`. A run for the previous commit proves nothing. Then confirm the deployed function changed after that run:

```bash
AWS_PROFILE=fieldsight-deployer aws lambda get-function-configuration \
  --function-name fieldsight-test-org-api --query LastModified --output text --region ap-southeast-2
```

- [ ] **Step 4: Invoke TEST /timeline as the real user**

```bash
export AWS_PROFILE=fieldsight-deployer MSYS_NO_PATHCONV=1 AWS_DEFAULT_REGION=ap-southeast-2
uv run python scripts/invoke_org_api.py d95e24e8-a001-709b-1a3f-9196c584b2a6 GET /api/org/timeline \
  '{"date":"2026-09-01","user":"Ben_UCPK2"}' > "$TEMP/timeline-0901.json"
node -e "let d='';process.stdin.on('data',c=>d+=c);process.stdin.on('end',()=>{const r=JSON.parse(d);const b=typeof r.body==='string'?JSON.parse(r.body):r;const items=(b.topics||[]).flatMap(t=>t.action_items||[]);console.log('status',r.statusCode,'items',items.length,'missing version',items.filter(i=>!('version' in i)).length);console.log(items.filter(i=>i.id==='2e3b17e0-4317-41e0-96f4-19fde87f3002'))})" < "$TEMP/timeline-0901.json"
```

Expected: `status 200`, `missing version 0`, and the listed item has `"version": 2`.

Otherwise:
- **Item absent:** the day may be collapsed onto another survivor, or site-clipped. Check `collapsed_ids` on the other items before concluding anything.
- **`version: 1`:** re-read the Task 3 Step 1 count. Then check the function's `PGDATABASE` is `fieldsight_test` (`get-function-configuration --query Environment.Variables.PGDATABASE`) before suspecting the code.
- **Any 404 on every route:** `MSYS_NO_PATHCONV` was not set.

Report the observed JSON line to the user. There is nothing to commit in this task.

---

## Self-review

- **Spec coverage (§8.1):** repository stamping in both functions (T1); a single batch query with no company predicate, survivors only (T1 tests); serializer allowlist (T2); rendered-shape test via the handler (T2); callers without `edit_count` get v1 (T2 plus T1 `get_topic_full`); no N+1 (T1 single-call assert); real Postgres before merge (T3); TEST rollout before the UI reads it (T4). The mock fixture in `daily-report.fixture.js` is frontend and out of scope.
- **§3.4:** every field counts, because no `field` predicate exists and T3 uses text, deadline and status edits.
- **Placeholders:** none. The only conditional instruction is T2 Step 3's "stub further conn calls if hit", which names the exact error and the stubbing style.
- **Names:** `edit_count`, `_stamp_edit_counts`, `_EDIT_COUNT_SQL` and `version` are used consistently across T1–T4.
