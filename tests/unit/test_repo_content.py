import pytest

content = pytest.importorskip("repositories.content",
                              reason="requires psycopg (installed in CI)")


class FakeCursor:
    def __init__(self, row):
        self._row = row
        self.sql = None
        self.params = None

    def execute(self, sql, params=None):
        self.sql = sql
        self.params = params
        return self

    def fetchone(self):
        return self._row


class FakeConn:
    def __init__(self, row):
        self.cur = FakeCursor(row)

    def cursor(self, *a, **k):
        return self.cur


def test_editable_allow_list_matches_spec():
    assert content.EDITABLE["topics"] == {"title", "summary"}
    assert content.EDITABLE["action_items"] == {"text", "responsible"}
    assert content.EDITABLE["findings"] == {
        "observation", "recommended_action", "entity_name", "entity_trade"}
    assert content.EDITABLE["safety_observations"] == {"observation"}


def test_is_editable_rejects_enum_and_unknown_tables():
    assert content.is_editable("topics", "title")
    assert not content.is_editable("topics", "category")     # enum, excluded (§3)
    assert not content.is_editable("action_items", "status")  # task metadata
    assert not content.is_editable("recordings", "title")     # not an item-store table


def test_get_content_row_joins_company_and_author():
    conn = FakeConn({"id": "t-1", "site_id": "s-1", "company_id": "co-1",
                     "author_user_id": "u-9", "title": "Slab pour", "summary": "x"})
    row = content.get_content_row(conn, "topics", "t-1")
    assert row["company_id"] == "co-1"
    assert row["author_user_id"] == "u-9"
    assert "join sites" in conn.cur.sql.lower()


def test_update_content_field_only_writes_whitelisted_column():
    conn = FakeConn({"id": "t-1", "title": "Corrected"})
    row = content.update_content_field(conn, "topics", "t-1", "title", "Corrected")
    assert row["title"] == "Corrected"
    assert "update topics set title" in conn.cur.sql.lower()
    assert conn.cur.params == ("Corrected", "t-1")


def test_update_content_field_rejects_non_whitelisted_field():
    conn = FakeConn(None)
    assert content.update_content_field(conn, "topics", "t-1", "category", "x") is None
