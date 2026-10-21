"""Renderer parity on the local Postgres (review S9; replaces the spec's every-10th
live parity). The real rag-search tools dispatcher runs against a migrated, seeded
local database; every (template, params) it issues is captured, then executed twice on
the same connection -- psycopg-bound, and as `render()`'s literal with no params --
and the rows must be identical. This is the only offline proof that untyped string
arrays into ANY(%s) / ::uuid[] and the '[...]' vector literal bind as psycopg does.

Needs TEST_DATABASE_URL (run via /c/Users/camil/fswork/run-tests.sh, one at a time).
A SKIP IS NOT A PASS.
"""
import os

import pytest

from askv2.render import check_template, render
from conftest import SRC

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"),
                                reason="TEST_DATABASE_URL not set (local pgserver)")

D = "2026-10-14"
# Short decimals: %.7f is lossless for them, so parity is exact equality.
EMB = [0.25] + [0.1] * 1023
OTHER = [1.0] + [0.0] * 1023


@pytest.fixture
def db():
    import psycopg
    from pgvector.psycopg import register_vector
    from db.migrate import apply_migrations
    url = os.environ["TEST_DATABASE_URL"]
    with psycopg.connect(url, autocommit=True) as c:
        apply_migrations(c, str(SRC / "migrations"))
    conn = psycopg.connect(url)
    register_vector(conn)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


def _user(db, co, folder, role):
    from repositories import users
    u = users.upsert_user(db, f"sub-{folder}", f"{folder}@x.nz", company_id=co["id"],
                          first_name=folder)
    db.execute("UPDATE users SET folder_name=%s, global_role=%s WHERE id=%s",
               (folder, role, u["id"]))
    return u


def _seed(db):
    from repositories import aliases, chunks, companies, sites, topics
    a = companies.create_company(db, "A")
    sa = sites.create_site(db, a["id"], "Alpha")
    sb = sites.create_site(db, a["id"], "Beta")
    gm = _user(db, a, "GmA", "gm")
    wa = _user(db, a, "WorkerA", "worker")
    topics.upsert_topic(db, sa["id"], D, "Pour with Jon", user_id=wa["id"],
                        summary="Jon checked the slab it's 50%",
                        source_s3_key=f"extractions/WorkerA/{D}/a.json",
                        time_range="09:00 - 09:40")
    topics.upsert_topic(db, sb["id"], "2026-10-12", "Slab", user_id=wa["id"],
                        source_s3_key="reports/2026-10-12/WorkerA/daily_report.json")
    for day, text, emb in (("2026-10-12", "slab pour on level two", EMB),
                           (D, "drainage pipe laid near the slab pour", OTHER),
                           (D, "concrete truck late, slab pour delayed", EMB)):
        chunks.insert_chunk(db, sa["id"], day, "topic", text, emb, user_id=wa["id"],
                            source_s3_key=f"reports/{day}/WorkerA/daily_report.json")
    aliases.create_alias(db, a["id"], None, "Jon", "John", "person", gm["id"])
    return dict(a=a, sa=sa, sb=sb, gm=gm, wa=wa)


def test_every_dispatcher_statement_renders_to_the_same_rows(db, monkeypatch):
    import psycopg
    import lambda_rag_search

    w = _seed(db)
    captured = []

    class Recording(psycopg.Cursor):
        def execute(self, query, params=None, **kw):
            captured.append((query, params))
            return super().execute(query, params, **kw)

    db.cursor_factory = Recording
    monkeypatch.setenv("PROJECT_DOCUMENTS", "true")
    monkeypatch.setattr(lambda_rag_search, "get_cached_connection", lambda: db)
    monkeypatch.setattr(lambda_rag_search, "close_cached_connection", lambda: None)
    res = lambda_rag_search.lambda_handler({"mode": "tools", "sub": "sub-GmA", "deadline_s": 20,
                                            "calls": [
        {"id": "1", "name": "day_overview",
         "args": {"date_from": D, "date_to": D, "site": "alpha", "person": "workera"}},
        {"id": "2", "name": "search_records", "query_embedding": EMB,
         "args": {"query": "slab pour", "person": "WorkerA"}},
        {"id": "3", "name": "latest_activity", "query_embedding": EMB,
         "args": {"query": "slab pour", "on_or_before": D}},
        {"id": "4", "name": "find_people", "args": {"name": "WorkerA"}}]}, None)
    res2 = lambda_rag_search.lambda_handler({"mode": "tools", "sub": "sub-GmA", "deadline_s": 20,
                                             "calls": [
        {"id": "5", "name": "search_records", "query_embedding": EMB,
         "args": {"query": "slab", "date_from": "2026-10-13", "date_to": "2026-10-13"}},
        {"id": "6", "name": "day_overview", "args": {"date_from": "2026-10-13"}},
        {"id": "7", "name": "count_metric",
         "args": {"metric": "duration", "date_from": D, "date_to": D}},
        {"id": "8", "name": "search_project_documents", "query_embedding": EMB,
         "args": {"query": "slab spec"}}]}, None)
    db.cursor_factory = psycopg.Cursor
    for r in res["results"] + res2["results"]:
        assert "error" not in r["meta"], r
    assert res["results"][0]["rows"][0]["title"] == "Pour with John"
    assert res["results"][1]["rows"] and res["results"][2]["rows"]

    compared = []
    for sql, params in captured:
        if params is None:
            continue
        check_template(sql)                    # every reached read template is allowed
        bound = db.execute(sql, params).fetchall()
        literal = db.execute(render(sql, params)).fetchall()
        assert literal == bound, sql[:120]
        compared.append(" ".join(sql.split()))
    joined = "\n".join(compared)
    from repositories import search_sql
    assert " ".join(search_sql.build_search_sql().split()) in compared
    for needle in ("t.summary, t.source_s3_key",          # topics.day_overview_rows
                   "FROM sites WHERE id = ANY(%s)",       # sites.list_sites_by_ids
                   "FROM name_aliases",                   # aliases.list_active
                   "WHERE users.cognito_sub=%s",          # users.get_user_by_sub
                   "max(t.report_date)"):                 # topics.latest_visible_topic_date
        assert needle in joined, needle
    assert len(set(compared)) >= 12, len(set(compared))
