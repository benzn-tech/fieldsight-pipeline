import pytest

aliases = pytest.importorskip("repositories.aliases",
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


def test_list_active_orders_site_scoped_first():
    conn = FakeConn([{"wrong_term": "Mackon", "right_term": "McCahon",
                      "site_id": "s-1", "kind": "person"}])
    rows = aliases.list_active(conn, "co-1", site_ids=["s-1"])
    assert rows[0]["right_term"] == "McCahon"
    assert "status = 'active'" in conn.cur.sql.lower() or "status='active'" in conn.cur.sql.lower()
    assert "nulls last" in conn.cur.sql.lower()


def test_create_alias_binds_all_columns():
    conn = FakeConn([{"id": "a-1", "wrong_term": "Fyfe", "right_term": "Fife"}])
    row = aliases.create_alias(conn, "co-1", "s-1", "Fyfe", "Fife", "person",
                               "u-1", source="correction")
    assert row["right_term"] == "Fife"
    assert conn.cur.params == ("co-1", "s-1", "Fyfe", "Fife", "person",
                               "correction", "u-1")
