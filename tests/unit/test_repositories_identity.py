"""
Tests for identity Phase 1 (migration 0007) repo additions — Task 1 (TDD):

  - sites.get_company_site_by_slug / create_site(..., slug=...)
  - users.get_by_folder_name / upsert_field_only_user / set_folder_name

FakeConn/FakeCursor double records every execute() call's SQL text + params
so behaviour can be asserted without a real Postgres (mirrors the FakeConn
style of tests/unit/test_topics_repo.py / test_lambda_ingest.py).
"""
from repositories import sites, users


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
    """`results` is consumed in call order: one entry per execute() call.
    Each entry is a list of row dicts (fetchall) or a single row dict
    (fetchone via first element)."""

    def __init__(self, results=None):
        self.calls = []
        self._results = list(results or [])

    def _pop_result(self):
        return self._results.pop(0) if self._results else []

    def cursor(self, row_factory=None):
        return FakeCursor(self)


# ---------------------------------------------------------------------------
# sites.get_company_site_by_slug
# ---------------------------------------------------------------------------

def test_get_company_site_by_slug_queries_company_and_slug():
    site_row = {"id": "site-1", "company_id": "c-1", "name": "North Site",
                "location": None, "client": None, "industry": None,
                "icon_s3_key": None, "slug": "north-site",
                "created_at": "2026-07-06", "archived_at": None}
    conn = FakeConn(results=[[site_row]])

    row = sites.get_company_site_by_slug(conn, "c-1", "north-site")

    assert row == site_row
    assert len(conn.calls) == 1
    sql, params = conn.calls[0]["sql"], conn.calls[0]["params"]
    assert "WHERE company_id=%s AND slug=%s" in sql
    assert "slug" in sql.split("FROM")[0]  # slug is selected (_COLS includes it)
    assert params == ("c-1", "north-site")


def test_get_company_site_by_slug_miss_returns_none():
    conn = FakeConn(results=[[]])

    assert sites.get_company_site_by_slug(conn, "c-1", "ghost-slug") is None


# ---------------------------------------------------------------------------
# sites.set_slug — Task 2 (seed) backfill helper
# ---------------------------------------------------------------------------

def test_set_slug_updates_by_id():
    site_row = {"id": "site-1", "company_id": "c-1", "name": "North Site",
                "location": None, "client": None, "industry": None,
                "icon_s3_key": None, "slug": "north-site",
                "created_at": "2026-07-06", "archived_at": None}
    conn = FakeConn(results=[[site_row]])

    row = sites.set_slug(conn, "site-1", "north-site")

    assert row == site_row
    sql, params = conn.calls[0]["sql"], conn.calls[0]["params"]
    assert "UPDATE sites SET slug=%s WHERE id=%s" in sql
    assert params == ("north-site", "site-1")


# ---------------------------------------------------------------------------
# sites.create_site — slug param
# ---------------------------------------------------------------------------

def test_create_site_includes_slug_in_insert():
    site_row = {"id": "site-2", "company_id": "c-1", "name": "South Site",
                "location": None, "client": None, "industry": None,
                "icon_s3_key": None, "slug": "south-site", "address": None,
                "created_at": "2026-07-06", "archived_at": None}
    conn = FakeConn(results=[[site_row]])

    row = sites.create_site(conn, "c-1", "South Site", slug="south-site")

    assert row == site_row
    sql, params = conn.calls[0]["sql"], conn.calls[0]["params"]
    assert "slug" in sql.split("VALUES")[0]  # column list includes slug
    assert params == ("c-1", "South Site", None, None, None, None, "south-site", None, None, None)


def test_create_site_slug_defaults_to_none():
    conn = FakeConn(results=[[{"id": "site-3"}]])

    sites.create_site(conn, "c-1", "No Slug Site")

    params = conn.calls[0]["params"]
    assert params[6] is None  # slug defaults to None, still passed positionally


# ---------------------------------------------------------------------------
# users.get_by_folder_name
# ---------------------------------------------------------------------------

def test_get_by_folder_name_queries_company_and_folder():
    user_row = {"id": "u-1", "cognito_sub": None, "company_id": "c-1",
                "email": "", "first_name": "James", "last_name": "Lamb",
                "avatar_s3_key": None, "global_role": "worker",
                "folder_name": "James_Lamb", "kind": "field_only",
                "created_at": "2026-07-06", "archived_at": None}
    conn = FakeConn(results=[[user_row]])

    row = users.get_by_folder_name(conn, "c-1", "James_Lamb")

    assert row == user_row
    sql, params = conn.calls[0]["sql"], conn.calls[0]["params"]
    assert "WHERE company_id=%s AND folder_name=%s" in sql
    assert params == ("c-1", "James_Lamb")


def test_get_by_folder_name_miss_returns_none():
    conn = FakeConn(results=[[]])

    assert users.get_by_folder_name(conn, "c-1", "Ghost_Folder") is None


# ---------------------------------------------------------------------------
# users.upsert_field_only_user
# ---------------------------------------------------------------------------

def test_upsert_field_only_user_conflict_target_and_kind_and_null_sub():
    user_row = {"id": "u-2", "cognito_sub": None, "company_id": "c-1",
                "email": "", "first_name": "Jack", "last_name": "Gibson",
                "avatar_s3_key": None, "global_role": "worker",
                "folder_name": "Jack_Gibson", "kind": "field_only",
                "created_at": "2026-07-06", "archived_at": None}
    conn = FakeConn(results=[[user_row]])

    row = users.upsert_field_only_user(
        conn, "c-1", "Jack_Gibson", "Jack", "Gibson", "worker")

    assert row == user_row
    sql, params = conn.calls[0]["sql"], conn.calls[0]["params"]
    assert "ON CONFLICT (company_id, folder_name) DO UPDATE" in sql
    cols = sql.split("VALUES")[0]
    assert "kind" in cols
    assert "cognito_sub" in cols
    assert "'field_only'" in sql  # kind literal, not a param
    assert "NULL" in sql.split("VALUES")[1]  # cognito_sub literal NULL, not a param
    assert params == {
        "company_id": "c-1", "folder_name": "Jack_Gibson",
        "first": "Jack", "last": "Gibson", "role": "worker",
    }


def test_upsert_field_only_user_do_update_sets_only_name_and_role():
    conn = FakeConn(results=[[{"id": "u-2"}]])

    users.upsert_field_only_user(conn, "c-1", "Jack_Gibson", "Jack", "Gibson", "worker")

    sql = conn.calls[0]["sql"]
    do_update_clause = sql.split("DO UPDATE SET")[1]
    assert "first_name=EXCLUDED.first_name" in do_update_clause
    assert "last_name=EXCLUDED.last_name" in do_update_clause
    assert "global_role=EXCLUDED.global_role" in do_update_clause


# ---------------------------------------------------------------------------
# users.set_folder_name
# ---------------------------------------------------------------------------

def test_set_folder_name_updates_by_sub():
    conn = FakeConn(results=[[]])

    users.set_folder_name(conn, "sub-1", "Jarley_Trainor")

    sql, params = conn.calls[0]["sql"], conn.calls[0]["params"]
    assert "UPDATE users SET folder_name=%s WHERE cognito_sub=%s" in sql
    assert params == ("Jarley_Trainor", "sub-1")
