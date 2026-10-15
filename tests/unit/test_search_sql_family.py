"""The index-family filter: both SQL builders carry it, the repository binds it,
and rag-search passes the SEARCH_INDEX_READ value to BOTH reads and echoes it.

Whether the predicate selects the right rows is a database question and is
asked in tests/integration/test_search_family_pg.py.
"""
import pytest

from repositories import chunks
from repositories.search_sql import (build_latest_date_sql, build_search_sql,
                                     family_predicate)

rag = pytest.importorskip("lambda_rag_search", reason="requires psycopg (installed in CI)")


def test_family_predicate_text_is_the_contract():
    p = family_predicate("c")
    assert "%(source_family)s::text IS NULL" in p
    assert "c.source_s3_key LIKE 'extractions/%%'" in p
    assert "c.source_s3_key IS NULL" in p and "NOT LIKE 'extractions/%%'" in p


def test_session_arm_falls_back_to_uncovered_report_rows():
    p = family_predicate("c")
    assert "NOT EXISTS (SELECT 1 FROM report_chunks s" in p
    assert "s.site_id = c.site_id" in p and "s.report_date = c.report_date" in p
    # NULL user_id must match NULL user_id, or an unattributed day is never "covered".
    assert "s.user_id IS NOT DISTINCT FROM c.user_id" in p
    assert "s.source_s3_key LIKE 'extractions/%%'" in p
    # The report arm is untouched: no coverage subquery inside it.
    report_arm = p.split("= 'report'")[1]
    assert "NOT EXISTS" not in report_arm


def test_search_sql_carries_the_family_predicate_on_both_arms():
    # vec and lex arms share _scope_predicate, so the predicate appears twice.
    assert build_search_sql().count(family_predicate("c")) == 2


def test_latest_date_sql_carries_the_family_predicate():
    assert family_predicate("c") in build_latest_date_sql()


class _Cursor:
    def __init__(self, sink):
        self.sink = sink

    def execute(self, sql, params):
        self.sink["sql"], self.sink["params"] = sql, params
        return self

    def fetchall(self):
        return []

    def fetchone(self):
        return {"latest": None}


class FakeConn:
    def __init__(self):
        self.seen = {}

    def cursor(self, **kw):
        return _Cursor(self.seen)


def test_search_chunks_binds_source_family():
    conn = FakeConn()
    chunks.search_chunks(conn, [0.0], ["s"], source_family="session")
    assert conn.seen["params"]["source_family"] == "session"
    chunks.search_chunks(conn, [0.0], ["s"])
    assert conn.seen["params"]["source_family"] is None


def test_latest_visible_date_binds_source_family():
    conn = FakeConn()
    chunks.latest_visible_date(conn, ["s"], source_family="report")
    assert conn.seen["params"]["source_family"] == "report"
    chunks.latest_visible_date(conn, ["s"])
    assert conn.seen["params"]["source_family"] is None


class _Conn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


CALLER = {"id": "u-1", "cognito_sub": "sub-1", "company_id": "c-1", "email": "a@x.nz",
          "first_name": "Ada", "last_name": "L", "avatar_s3_key": None,
          "global_role": "admin", "created_at": "2026-07-04"}


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(rag, "get_cached_connection", lambda *a, **k: _Conn())
    monkeypatch.setattr(rag.users, "get_user_by_sub", lambda conn, sub: dict(CALLER))
    monkeypatch.setattr(rag.aliases, "list_active", lambda conn, cid, site_ids=None: [])
    monkeypatch.setattr(rag.scope, "visible_scope",
                        lambda conn, caller: {"site_ids": {"s-1"}, "author_ids": None})
    return monkeypatch


def test_rag_search_passes_the_family_to_both_reads_and_echoes_it(wired):
    wired.setattr(rag, "INDEX_FAMILY", "session")
    search_kw, latest_kw = [], []

    def fake_search(conn, qv, site_ids, **kw):
        search_kw.append(kw)
        return []

    def fake_latest(conn, site_ids, **kw):
        latest_kw.append(kw)
        return "2026-10-08"

    wired.setattr(rag.chunks, "search_chunks", fake_search)
    wired.setattr(rag.chunks, "latest_visible_date", fake_latest)

    out = rag.lambda_handler({"sub": "sub-1", "query_embedding": [0.1] * 1024,
                              "date_from": "2026-10-13", "date_to": "2026-10-13",
                              "widen_when_empty": True}, None)

    assert len(search_kw) == 2 and len(latest_kw) == 1   # first read + widened retry
    assert all(kw["source_family"] == "session" for kw in search_kw + latest_kw)
    assert out["index_family"] == "session"

