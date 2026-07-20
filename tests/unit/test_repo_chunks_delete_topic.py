import pytest

chunks = pytest.importorskip("repositories.chunks",
                             reason="requires psycopg (installed in CI)")


class FakeResult:
    def __init__(self, rowcount):
        self.rowcount = rowcount


class FakeConn:
    def __init__(self):
        self.sql = None
        self.params = None

    def execute(self, sql, params=None):
        self.sql = sql
        self.params = params
        return FakeResult(3)


def test_delete_chunks_for_topic_deletes_by_topic_id():
    conn = FakeConn()
    n = chunks.delete_chunks_for_topic(conn, "t-1")
    assert n == 3
    assert "delete from report_chunks where topic_id=%s" in conn.sql.lower()
    assert conn.params == ("t-1",)
