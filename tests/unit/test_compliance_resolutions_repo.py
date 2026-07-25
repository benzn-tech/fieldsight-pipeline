"""Tests for src/repositories/compliance_resolutions.py (spec 2026-07-26).

FakeConn/FakeCursor double records every execute()'s SQL + params so the key
shape, the empty-reach short-circuit, the null-safe resolver join, and the
re-key move can be asserted without a real Postgres -- same harness style as
test_action_items_repo.py, plus a transaction() double for rekey's SAVEPOINT.
"""
import pytest

from psycopg.errors import UniqueViolation
from repositories import compliance_resolutions as cr


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, params=None):
        self.conn.calls.append({"sql": sql, "params": params})
        self._rows = self.conn._pop_result()
        return self

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _Txn:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        self.conn.txn_log.append("enter")
        return self

    def __exit__(self, exc_type, exc, tb):
        self.conn.txn_log.append("rollback" if exc_type else "commit")
        return False                      # never suppress -- rekey catches its own


class FakeConn:
    def __init__(self, results=None, raise_on_execute=None):
        self.calls = []
        self.txn_log = []
        self._results = list(results or [])
        self._raise = raise_on_execute

    def _pop_result(self):
        return self._results.pop(0) if self._results else []

    def cursor(self, row_factory=None):
        if self._raise is not None:
            raise self._raise
        return FakeCursor(self)

    def transaction(self):
        return _Txn(self)


KEY = {"company_id": "c-1", "site_id": "s-1", "report_date": "2026-07-20",
       "domain": "safety", "user_folder": "Ada_L", "content_hash": "OLDHASH"}


# ---- upsert_resolution: 6-col key + null-safe resolver name ----
def test_upsert_targets_the_six_col_natural_key():
    conn = FakeConn(results=[[{"id": "r-1", "resolved": True, "resolved_by": "u-1",
                               "resolved_at": "t", "content_hash": "H",
                               "content_sample": "s", "resolved_by_name": "Ada L"}]])
    out = cr.upsert_resolution(conn, "c-1", "s-1", "2026-07-20", "safety",
                               "Ada_L", "H", "loose rail", True, "u-1")
    sql = conn.calls[0]["sql"]
    assert ("ON CONFLICT (company_id, site_id, report_date, domain, user_folder, "
            "content_hash)") in sql
    # user_folder is IN the key -- the 1a fix (not just site_id).
    assert "user_folder" in sql
    assert "DO UPDATE SET resolved=EXCLUDED.resolved" in sql
    assert conn.calls[0]["params"] == ("c-1", "s-1", "2026-07-20", "safety",
                                       "Ada_L", "H", "loose rail", True, "u-1")
    assert out["resolved_by_name"] == "Ada L"


def test_upsert_resolver_name_is_null_safe_concat_ws_form():
    conn = FakeConn(results=[[{"id": "r-1"}]])
    cr.upsert_resolution(conn, "c-1", "s-1", "2026-07-20", "safety",
                         "Ada_L", "H", "s", True, "u-1")
    sql = conn.calls[0]["sql"]
    assert "NULLIF(TRIM(CONCAT_WS(' ', u.first_name, u.last_name)), '')" in sql
    assert "LEFT JOIN users u ON u.id = up.resolved_by" in sql   # resolved_by is a users.id
    assert "||" not in sql                                       # no hand-rolled concat


# ---- list_resolutions: reach scoping + the []-means-no-filter trap ----
def test_list_empty_reach_returns_empty_without_a_query():
    conn = FakeConn()
    assert cr.list_resolutions(conn, "c-1", set(), "2026-07-01", "2026-07-31") == []
    assert conn.calls == []                       # NEVER a round-trip -> never "unscoped"


def test_list_scopes_by_sites_dates_company_and_optional_domain():
    conn = FakeConn(results=[[{"site_id": "s-1", "resolved": True}]])
    rows = cr.list_resolutions(conn, "c-1", ["s-1", "s-2"],
                               "2026-07-01", "2026-07-31", domain="quality")
    assert rows == [{"site_id": "s-1", "resolved": True}]
    call = conn.calls[0]
    assert "cr.site_id = ANY(%s::uuid[])" in call["sql"]
    assert "cr.report_date >= %s" in call["sql"] and "cr.report_date <= %s" in call["sql"]
    assert "cr.company_id = %s" in call["sql"] and "cr.domain = %s" in call["sql"]
    # resolved_by is the resolved display name (null-safe) on the read path too.
    assert "NULLIF(TRIM(CONCAT_WS(' ', u.first_name, u.last_name)), '') AS resolved_by" in call["sql"]
    assert call["params"] == (["s-1", "s-2"], "2026-07-01", "2026-07-31", "c-1", "quality")


def test_list_cross_company_drops_the_tenant_pin():
    conn = FakeConn(results=[[]])
    cr.list_resolutions(conn, None, ["s-1"], "2026-07-01", "2026-07-31")
    call = conn.calls[0]
    assert "cr.company_id = %s" not in call["sql"]       # company None -> no tenant filter
    assert "cr.domain = %s" not in call["sql"]           # domain None -> both domains
    assert call["params"] == (["s-1"], "2026-07-01", "2026-07-31")


# ---- rekey_resolution: move / no-op / merge-collision ----
def test_rekey_moves_the_mark_to_the_new_hash():
    conn = FakeConn(results=[[{"id": "r-1", "content_hash": "NEWHASH"}]])
    out = cr.rekey_resolution(conn, KEY, "NEWHASH", "new sample")
    assert out == {"id": "r-1", "content_hash": "NEWHASH"}
    call = conn.calls[0]
    assert "UPDATE compliance_resolutions" in call["sql"]
    assert "SET content_hash=%s, content_sample=%s" in call["sql"]
    # matched on the FULL old 6-col key, old content_hash last.
    assert call["params"] == ("NEWHASH", "new sample", "c-1", "s-1",
                              "2026-07-20", "safety", "Ada_L", "OLDHASH")
    assert conn.txn_log == ["enter", "commit"]           # ran inside a SAVEPOINT


def test_rekey_noop_when_hash_unchanged():
    conn = FakeConn()
    assert cr.rekey_resolution(conn, KEY, "OLDHASH", "s") is None
    assert conn.calls == [] and conn.txn_log == []       # no text change -> no work


def test_rekey_noop_when_no_mark_exists():
    conn = FakeConn(results=[[]])                         # UPDATE matched 0 rows
    assert cr.rekey_resolution(conn, KEY, "NEWHASH", "s") is None
    assert len(conn.calls) == 1                           # attempted, harmlessly


def test_rekey_swallows_unique_violation_as_benign_merge():
    # The edit made the text collide with an already-resolved sibling: the
    # SAVEPOINT rolls back only this move and rekey returns None -- it must NOT
    # propagate (that would poison the caller's edit transaction).
    conn = FakeConn(raise_on_execute=UniqueViolation("dup key"))
    assert cr.rekey_resolution(conn, KEY, "NEWHASH", "s") is None
    assert conn.txn_log == ["enter", "rollback"]
