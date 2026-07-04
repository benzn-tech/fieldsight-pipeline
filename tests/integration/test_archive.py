import pytest
from repositories import companies, users, sites, memberships

pytestmark = pytest.mark.integration


def _columns(conn, table):
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name=%s",
        (table,),
    ).fetchall()
    return {r[0] for r in rows}


def test_archived_at_columns_exist(db):
    for t in ("sites", "users", "memberships"):
        assert "archived_at" in _columns(db, t)
