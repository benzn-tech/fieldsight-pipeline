"""rag-search `mode: "tools"` end to end through the handler, against a real PostgreSQL
(spec 2026-10-15-ask-retrieval-tools §3.1/§3.3/§3.4).

Real caller resolution, real visible_scope, real repository SQL, real alias rewriting.
Skipped without TEST_DATABASE_URL. A SKIP IS NOT A PASS — read the CI run.
"""
import pytest

import lambda_rag_search
from repositories import aliases, chunks, companies, sites, topics, users

pytestmark = pytest.mark.integration

D = "2026-10-14"
EMB = [0.1] * 1024


def _user(db, co, folder, role):
    u = users.upsert_user(db, f"sub-{folder}", f"{folder}@x.nz", company_id=co["id"],
                          first_name=folder)
    db.execute("UPDATE users SET folder_name=%s, global_role=%s WHERE id=%s",
               (folder, role, u["id"]))
    return u


@pytest.fixture
def world(db, monkeypatch):
    monkeypatch.setattr(lambda_rag_search, "get_cached_connection", lambda: db)
    a = companies.create_company(db, "A")
    b = companies.create_company(db, "B")
    sa = sites.create_site(db, a["id"], "Alpha")
    sb = sites.create_site(db, b["id"], "Bravo")
    # A gm reaches every site of its own company with no membership row
    # (scope.visible_scope, the ALL branch).
    ga = _user(db, a, "GmA", "gm")
    wa = _user(db, a, "WorkerA", "worker")
    bw = _user(db, b, "WorkerB", "worker")
    topics.upsert_topic(db, sb["id"], D, "B secret", user_id=bw["id"],
                        source_s3_key=f"extractions/WorkerB/{D}/a.json")
    return dict(a=a, b=b, sa=sa, sb=sb, ga=ga, wa=wa, bw=bw)


def _tools(calls, sub="sub-GmA"):
    return lambda_rag_search._search({"mode": "tools", "sub": sub, "deadline_s": 20,
                                      "calls": calls}, None)["results"]


def test_cross_company_person_ref_and_site_get_the_no_match_shape(world):
    w = world
    res = _tools([
        {"id": "1", "name": "day_overview",
         "args": {"date_from": D, "date_to": D, "person": str(w["bw"]["id"])}},
        {"id": "2", "name": "day_overview",
         "args": {"date_from": D, "date_to": D, "site": str(w["sb"]["id"])}},
        {"id": "3", "name": "day_overview",
         "args": {"date_from": D, "date_to": D, "site": "Bravo"}},
        {"id": "4", "name": "find_people", "args": {"name": "WorkerB"}}])
    assert all(r["rows"] == [] for r in res)
    assert res[0]["meta"].get("person_unresolved") is True
    assert res[1]["meta"]["empty_reason"] == "no_match" == res[2]["meta"]["empty_reason"]
    assert res[3]["meta"]["empty_reason"] == "no_match"
    assert all("error" not in r["meta"] for r in res)


def test_happy_path_day_overview_by_site_name_and_person_name_with_aliases(db, world):
    w = world
    topics.upsert_topic(db, w["sa"]["id"], D,
                        "Pour with Jon", user_id=w["wa"]["id"], summary="Jon checked",
                        source_s3_key=f"extractions/WorkerA/{D}/a.json",
                        time_range="09:00 - 09:40")
    aliases.create_alias(db, w["a"]["id"], None, "Jon", "John", "person", w["ga"]["id"])
    res = _tools([
        {"id": "1", "name": "day_overview",
         "args": {"date_from": D, "date_to": D, "site": "alpha", "person": "workera"}},
        {"id": "2", "name": "day_overview", "args": {"date_from": "2026-10-15",
                                                     "date_to": "2026-10-15"}}])
    rows, meta = res[0]["rows"], res[0]["meta"]
    assert [r["title"] for r in rows] == ["Pour with John"]
    assert rows[0]["summary"] == "John checked" and rows[0]["time_range"] == "09:00 - 09:40"
    assert meta["truncated"] is False and "empty_reason" not in meta and "error" not in meta
    # The next day is empty and points back at the 14th, from the topics table.
    assert res[1]["meta"]["empty_reason"] == "nothing_in_range"
    assert res[1]["meta"]["latest_before"] == {"date": D, "from": "topics"}


def test_search_records_and_latest_activity_through_the_real_search(db, world):
    w = world
    # find_people's candidate set is a topic/recording/membership footprint; a chunk
    # alone is not one.
    topics.upsert_topic(db, w["sa"]["id"], "2026-10-12", "Slab", user_id=w["wa"]["id"],
                        source_s3_key="reports/2026-10-12/WorkerA/daily_report.json")
    chunks.insert_chunk(db, w["sa"]["id"], "2026-10-12", "topic", "slab pour on level two",
                        EMB, user_id=w["wa"]["id"],
                        source_s3_key="reports/2026-10-12/WorkerA/daily_report.json")
    chunks.insert_chunk(db, w["sb"]["id"], "2026-10-13", "topic", "B slab pour", EMB,
                        user_id=w["bw"]["id"],
                        source_s3_key="reports/2026-10-13/WorkerB/daily_report.json")
    res = _tools([
        {"id": "1", "name": "search_records", "query_embedding": EMB,
         "args": {"query": "slab pour", "person": "WorkerA"}},
        {"id": "2", "name": "search_records", "query_embedding": EMB,
         "args": {"query": "slab pour", "date_from": D, "date_to": D}},
        {"id": "3", "name": "latest_activity", "query_embedding": EMB,
         "args": {"query": "slab pour", "on_or_before": D}},
        {"id": "4", "name": "count_metric",
         "args": {"metric": "duration", "date_from": D, "date_to": D}}])
    assert [r["chunk_text"] for r in res[0]["rows"]] == ["slab pour on level two"]
    # Empty dated search: latest visible chunk day before the range -- never B's 13th.
    assert res[1]["rows"] == []
    assert res[1]["meta"]["latest_before"] == {"date": "2026-10-12", "from": "chunks"}
    assert [r["chunk_text"] for r in res[2]["rows"]] == ["slab pour on level two"]
    assert res[3]["rows"][0]["value"] == 0 and res[3]["meta"]["empty_reason"]
    assert all("error" not in r["meta"] for r in res)


def test_unprovisioned_caller(world):
    res = _tools([{"id": "1", "name": "find_people", "args": {"name": "x"}}], sub="nobody")
    assert res[0]["meta"] == {"error": "not_provisioned"}
