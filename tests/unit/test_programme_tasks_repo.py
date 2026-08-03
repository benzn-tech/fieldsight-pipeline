"""
Tests for src/repositories/programme_tasks.py — Task 2 of the programme
storage foundation plan:

  fieldsight-ui/docs/superpowers/specs/2026-08-02-programme-foundation-design.md
  fieldsight-ui/docs/superpowers/plans/2026-08-02-programme-storage-foundation.md

The FakeConn/FakeCursor doubles record every execute()'s SQL text and params,
so behaviour is asserted without a live Postgres — same style as
tests/unit/test_programme_suggestions_repo.py.

The properties that matter here and are easy to regress:
  - update_task's WHERE carries row_version, so a lost optimistic-lock race
    updates nothing and returns None rather than silently overwriting
  - update_task only ever writes columns from an allow-list, so a client
    cannot PATCH its way to a different programme_id or origin
  - delete_local_task refuses imported rows in SQL, not just in the handler
  - list_tasks excludes soft-deleted rows unless asked
"""
import pytest

from repositories import programme_tasks as repo


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._rows = []

    def execute(self, sql, params=None):
        self.conn.calls.append({"sql": sql, "params": params})
        self._rows = self.conn._pop_result()
        return self

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    @property
    def rowcount(self):
        return len(self._rows)


class FakeConn:
    """`results` is consumed in call order: one entry per execute()."""

    def __init__(self, results=None):
        self.calls = []
        self._results = list(results or [])

    def cursor(self, row_factory=None):
        return FakeCursor(self)

    def _pop_result(self):
        if not self._results:
            return []
        nxt = self._results.pop(0)
        if nxt is None:
            return []
        return nxt if isinstance(nxt, list) else [nxt]


TASK_ID = "11111111-1111-1111-1111-111111111111"
PROG_ID = "22222222-2222-2222-2222-222222222222"
USER_ID = "33333333-3333-3333-3333-333333333333"


def test_update_task_guards_on_row_version():
    conn = FakeConn([{"id": TASK_ID, "row_version": 3}])
    repo.update_task(conn, TASK_ID, fields={"progress_pct": 50},
                     row_version=2, updated_by=USER_ID)
    sql = conn.calls[0]["sql"]
    assert "row_version = %s" in sql, "the optimistic lock must be in the WHERE clause"
    assert "row_version = row_version + 1" in sql, \
        "a successful update must bump the version"
    assert 2 in conn.calls[0]["params"], "the caller's expected version must be bound"


def test_update_task_returns_none_when_the_lock_lost():
    conn = FakeConn([[]])          # UPDATE ... RETURNING matched no row
    assert repo.update_task(conn, TASK_ID, fields={"progress_pct": 50},
                            row_version=2, updated_by=USER_ID) is None


def test_update_task_rejects_columns_outside_the_allow_list():
    conn = FakeConn([{"id": TASK_ID}])
    with pytest.raises(ValueError):
        repo.update_task(conn, TASK_ID, fields={"programme_id": "somewhere-else"},
                         row_version=1, updated_by=USER_ID)
    with pytest.raises(ValueError):
        repo.update_task(conn, TASK_ID, fields={"origin": "imported"},
                         row_version=1, updated_by=USER_ID)
    assert conn.calls == [], "nothing may be executed for a rejected field"


def test_update_task_rejects_an_empty_field_set():
    conn = FakeConn([])
    with pytest.raises(ValueError):
        repo.update_task(conn, TASK_ID, fields={}, row_version=1, updated_by=USER_ID)
    assert conn.calls == []


def test_update_task_accepts_the_allowed_columns():
    for field, value in [("name", "Pour slab"), ("start_date", "2026-04-01"),
                         ("end_date", "2026-04-10"), ("progress_pct", 40),
                         ("status", "in_progress"), ("zone", "Level 3"),
                         ("duration_days", 10), ("sort_order", 3)]:
        conn = FakeConn([{"id": TASK_ID}])
        repo.update_task(conn, TASK_ID, fields={field: value},
                         row_version=1, updated_by=USER_ID)
        assert field in conn.calls[0]["sql"], f"{field} should reach the SET clause"


def test_update_task_marks_imported_rows_locally_modified():
    """An edit to an imported row must be visible in the next import's diff,
    so the PM sees what the file is about to overwrite rather than losing it
    silently."""
    conn = FakeConn([{"id": TASK_ID}])
    repo.update_task(conn, TASK_ID, fields={"name": "Renamed here"},
                     row_version=1, updated_by=USER_ID)
    assert "locally_modified" in conn.calls[0]["sql"]


def test_delete_local_task_refuses_imported_rows_in_sql():
    conn = FakeConn([[]])
    repo.delete_local_task(conn, TASK_ID)
    sql = conn.calls[0]["sql"]
    assert "origin = 'local'" in sql, \
        "the guard must be in the DELETE's WHERE, not only in the handler"


def test_list_tasks_excludes_soft_deleted_by_default():
    conn = FakeConn([[]])
    repo.list_tasks(conn, PROG_ID)
    assert "removed_in_version IS NULL" in conn.calls[0]["sql"]


def test_list_tasks_can_include_soft_deleted():
    conn = FakeConn([[]])
    repo.list_tasks(conn, PROG_ID, include_removed=True)
    assert "removed_in_version IS NULL" not in conn.calls[0]["sql"]


def test_create_task_always_writes_origin_local():
    """Only an import may mint an imported row. The create endpoint is for
    breakdown subtasks and manual work."""
    conn = FakeConn([{"id": TASK_ID}])
    repo.create_task(conn, programme_id=PROG_ID, parent_id=None, name="Formwork",
                     wbs_code=None, start_date="2026-04-01", end_date="2026-04-05",
                     duration_days=5, status="not_started", zone=None,
                     sort_order=0, updated_by=USER_ID)
    assert "'local'" in conn.calls[0]["sql"]


def test_get_primary_programme_filters_active_and_primary():
    conn = FakeConn([{"id": PROG_ID}])
    repo.get_primary_programme(conn, "site-uuid")
    sql = conn.calls[0]["sql"]
    assert "is_primary" in sql and "active" in sql


def test_list_assignees_returns_empty_mapping_for_no_ids():
    """Guard against building `IN ()`, which is a syntax error, and against
    an unfiltered query that would return every assignee in the database."""
    conn = FakeConn([])
    assert repo.list_assignees(conn, []) == {}
    assert conn.calls == []


def test_list_assignees_groups_by_task():
    conn = FakeConn([[{"task_id": TASK_ID, "assignee": "Sam_SM"},
                      {"task_id": TASK_ID, "assignee": "Pat_PM"}]])
    assert repo.list_assignees(conn, [TASK_ID]) == {TASK_ID: ["Sam_SM", "Pat_PM"]}


def test_replace_all_tasks_reads_local_rows_before_touching_anything():
    """Step 1 of the rewrite. The parent's source_task_id has to be captured
    BEFORE the delete, because the parent's uuid does not survive it and the
    file's id is the only durable link across the rebuild."""
    conn = FakeConn([[], [], {"id": "g-uuid"}, [], []])
    repo.replace_all_tasks(
        conn, PROG_ID,
        parents=[{"task_id": "G1", "name": "Foundations", "wbs": "1"}],
        leaves=[{"task_id": "A1", "parent_id": "G1", "name": "Pour slab",
                 "start": "2026-04-01", "end": "2026-04-10"}],
        version_no=1, updated_by=USER_ID)
    first = conn.calls[0]["sql"]
    assert first.startswith("SELECT")
    assert "parent_source" in first
    assert "origin = 'local'" in first


def test_replace_all_tasks_deletes_imported_rows_only():
    """The contract this rewrite changed. Deleting everything is what let a
    plain Save destroy zone splits and AI breakdowns (fieldsight-ui#186)."""
    conn = FakeConn([[], [], {"id": "g-uuid"}, [], []])
    repo.replace_all_tasks(
        conn, PROG_ID,
        parents=[{"task_id": "G1", "name": "Foundations", "wbs": "1"}],
        leaves=[{"task_id": "A1", "parent_id": "G1", "name": "Pour slab",
                 "start": "2026-04-01", "end": "2026-04-10"}],
        version_no=1, updated_by=USER_ID)
    delete = [c for c in conn.calls if c["sql"].startswith("DELETE FROM programme_tasks")][0]
    assert "origin = 'imported'" in delete["sql"], (
        "an unscoped DELETE takes the local rows with it")


def test_replace_all_tasks_parents_the_leaves_by_source_id():
    """The file expresses parentage with its own ids; the rows have to be
    linked by our uuids, so groups must be inserted before their leaves."""
    conn = FakeConn([[], [], {"id": "g-uuid"}, [], []])
    repo.replace_all_tasks(
        conn, PROG_ID,
        parents=[{"task_id": "G1", "name": "Foundations", "wbs": "1"}],
        leaves=[{"task_id": "A1", "parent_id": "G1", "name": "Pour slab",
                 "start": "2026-04-01", "end": "2026-04-10"}],
        version_no=1, updated_by=USER_ID)
    leaf_call = [c for c in conn.calls if "A1" in (c["params"] or ())][0]
    assert "g-uuid" in leaf_call["params"], \
        "the leaf must be linked to the group's uuid, not its source id"
