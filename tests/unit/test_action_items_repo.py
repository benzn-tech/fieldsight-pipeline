"""
Tests for src/repositories/action_items.py — editable-tasks-reassignment
spec Task 1 (TDD): the whitelisted partial-update write path behind
PATCH /api/org/action-items/{id}.

A FakeConn/FakeCursor double records every execute() call's SQL text +
params so behaviour can be asserted without a real Postgres — copied from
tests/unit/test_topics_repo.py's harness style.
"""
from repositories import action_items


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


class FakeConn:
    """`results` is consumed in call order: one entry per cursor().execute()
    call (a list of row dicts)."""

    def __init__(self, results=None):
        self.calls = []
        self._results = list(results or [])

    def _pop_result(self):
        return self._results.pop(0) if self._results else []

    def cursor(self, row_factory=None):
        return FakeCursor(self)


def test_update_action_item_fields_builds_whitelisted_set_and_audit():
    conn = FakeConn(results=[[{"id": "a-1", "status": "done"}]])
    out = action_items.update_action_item_fields(
        conn, "a-1", {"status": "done", "priority": "high"}, "sub-9")
    assert out == {"id": "a-1", "status": "done"}
    call = conn.calls[0]
    assert "UPDATE action_items SET" in call["sql"]
    assert "status=%s" in call["sql"] and "priority=%s" in call["sql"]
    assert "updated_at=now()" in call["sql"] and "updated_by=%s" in call["sql"]
    assert "WHERE id=%s" in call["sql"]
    # values in caller-supplied order, then updated_by, then the id
    assert call["params"] == ["done", "high", "sub-9", "a-1"]


def test_update_action_item_fields_ignores_non_whitelisted_keys():
    conn = FakeConn(results=[[{"id": "a-1"}]])
    action_items.update_action_item_fields(
        conn, "a-1", {"status": "open", "site_id": "hack", "text": "hax"}, "sub-9")
    sql = conn.calls[0]["sql"]
    assert "site_id=%s" not in sql and "text=%s" not in sql   # not editable


def test_update_action_item_fields_empty_short_circuits():
    conn = FakeConn(results=[])
    assert action_items.update_action_item_fields(conn, "a-1", {}, "sub-9") is None
    assert conn.calls == []                                    # no round-trip
