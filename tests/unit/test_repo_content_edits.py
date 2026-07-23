import pytest

ce = pytest.importorskip("repositories.content_edits",
                         reason="requires psycopg (installed in CI)")


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self.sql = None
        self.params = None

    def execute(self, sql, params=None):
        self.sql = sql
        self.params = params
        return self

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeConn:
    def __init__(self, rows):
        self.cur = FakeCursor(rows)

    def cursor(self, *a, **k):
        return self.cur


def test_append_binds_before_after_actor():
    conn = FakeConn([{"id": "e-1"}])
    ce.append_content_edit(conn, "co-1", "topics", "t-1", "title",
                           "Mackon", "McCahon", "u-1", "site_manager")
    assert conn.cur.params == ("co-1", "topics", "t-1", "title",
                               "Mackon", "McCahon", "u-1", "site_manager")


def test_list_is_company_guarded_newest_first():
    conn = FakeConn([{"id": "e-2"}, {"id": "e-1"}])
    rows = ce.list_content_edits(conn, "co-1", "topics", "t-1")
    assert len(rows) == 2
    assert "company_id=%s" in conn.cur.sql or "company_id = %s" in conn.cur.sql
    # PR #117 aliased content_edits as `ce` to JOIN users for actor_name, so the
    # ORDER BY became qualified. Assert on the qualified column, not the bare one.
    assert "order by ce.created_at desc" in conn.cur.sql.lower()
