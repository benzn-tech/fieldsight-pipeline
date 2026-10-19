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


# ---- §4.3 ACL red tests for the search tools and graded callers (review I1 a/b) ----

UNIT = [1.0] + [0.0] * 1023          # a 1024-dim unit vector; seeded chunks use it too
NOWHERE = "00000000-0000-4000-8000-000000000000"


def _shape(results):
    """Everything but timing: what a caller could compare between two answers."""
    return [(r["rows"], {k: v for k, v in r["meta"].items() if k != "ms"}) for r in results]


def _seed_chunks(db, w):
    for site, user, text, day in ((w["sa"], w["wa"], "slab pour alpha", D),
                                  (w["sb"], w["bw"], "slab pour bravo", "2026-10-13")):
        chunks.insert_chunk(db, site["id"], day, "topic", text, UNIT, user_id=user["id"],
                            source_s3_key=f"reports/{day}/x/daily_report.json")


@pytest.mark.parametrize("tool", ["search_records", "latest_activity"])
def test_search_tools_answer_another_companys_site_and_person_like_nothing(db, world, tool):
    w = world
    _seed_chunks(db, w)

    def call(arg, value):
        args = {"query": "slab pour", arg: value}
        args.update({"date_from": "2026-10-13", "date_to": D} if tool == "search_records"
                    else {"on_or_before": D})
        return {"id": arg, "name": tool, "query_embedding": UNIT, "args": args}

    foreign = _tools([call("site", str(w["sb"]["id"])), call("site", "Bravo"),
                      call("person", str(w["bw"]["id"]))])
    unknown = _tools([call("site", NOWHERE), call("site", "Nowhere"),
                      call("person", NOWHERE)])
    assert _shape(foreign) == _shape(unknown)
    assert all(r["rows"] == [] and "error" not in r["meta"]
               and "latest_before" not in r["meta"] for r in foreign)
    assert [r["meta"]["empty_reason"] for r in foreign] == [
        "no_match", "no_match", "person_unresolved"]
    # Not vacuous: the same caller, unnarrowed, does find A's chunk and never B's.
    own = _tools([call("site", "Alpha")])
    assert [r["chunk_text"] for r in own[0]["rows"]] == ["slab pour alpha"]


def test_a_graded_worker_naming_a_colleague_gets_what_a_stranger_gets(db, world):
    from repositories import memberships
    w = world
    wc = _user(db, w["a"], "ColleagueC", "worker")
    for u in (w["wa"], wc):
        memberships.add_membership(db, u["id"], w["sa"]["id"], "worker")
    for u, folder, title in ((w["wa"], "WorkerA", "My pour"), (wc, "ColleagueC", "C pour")):
        topics.upsert_topic(db, w["sa"]["id"], D, title, user_id=u["id"],
                            source_s3_key=f"extractions/{folder}/{D}/a.json")
        chunks.insert_chunk(db, w["sa"]["id"], D, "topic", title.lower(), UNIT,
                            user_id=u["id"],
                            source_s3_key=f"reports/{D}/{folder}/daily_report.json")

    def batch(person):
        return [{"id": "d", "name": "day_overview",
                 "args": {"date_from": D, "date_to": D, "person": person}},
                {"id": "s", "name": "search_records", "query_embedding": UNIT,
                 "args": {"query": "pour", "date_from": D, "date_to": D, "person": person}}]

    by_ref = _tools(batch(str(wc["id"])), sub="sub-WorkerA")
    by_name = _tools(batch("ColleagueC"), sub="sub-WorkerA")
    assert _shape(by_ref) == _shape(_tools(batch(NOWHERE), sub="sub-WorkerA"))
    assert _shape(by_name) == _shape(_tools(batch("Nobody"), sub="sub-WorkerA"))
    assert all(r["rows"] == [] and r["meta"]["person_unresolved"] is True
               and "error" not in r["meta"] for r in by_ref + by_name)
    # Not vacuous: the worker sees their own day, and only their own.
    mine = _tools([{"id": "d", "name": "day_overview",
                    "args": {"date_from": D, "date_to": D}}], sub="sub-WorkerA")
    assert [r["title"] for r in mine[0]["rows"]] == ["My pour"]


# ---- per-call isolation on a NON-autocommit connection (review M6) ----

def test_a_sql_error_in_one_call_does_not_abort_the_next(db, world, monkeypatch):
    import rag_tools_dispatch
    w = world
    topics.upsert_topic(db, w["sa"]["id"], D, "Pour", user_id=w["wa"]["id"],
                        source_s3_key=f"extractions/WorkerA/{D}/a.json")

    def div_zero(conn, *a, **k):
        conn.execute("SELECT 1/0")
    monkeypatch.setattr(rag_tools_dispatch.users, "find_visible_people", div_zero)
    day = {"date_from": D, "date_to": D}
    res = _tools([{"id": "1", "name": "day_overview", "args": day},
                  {"id": "2", "name": "find_people", "args": {"name": "x"}},
                  {"id": "3", "name": "day_overview", "args": day}])
    assert res[1]["meta"]["error"] == "upstream"
    assert [r["title"] for r in res[0]["rows"]] == ["Pour"]
    assert [r["title"] for r in res[2]["rows"]] == ["Pour"]
    assert "error" not in res[2]["meta"]


# ---- latest_activity(query): the chosen day names the subject (spec 2026-10-20) ----

ORTHO = [0.0, 1.0] + [0.0] * 1022     # cosine distance 1.0 from UNIT: as far as it gets


def test_latest_activity_keeps_the_rows_that_name_the_subject(db, world):
    """On a small day every chunk is in the vector top-k, so the chunk naming the
    subject arrives from _search as the vector copy (lexical_hit=False) at
    distance 1.0. The filter must keep it because its TEXT names the subject, and
    drop the same day's unrelated chunk."""
    w = world
    key = "reports/2026-10-12/WorkerA/daily_report.json"
    topics.upsert_topic(db, w["sa"]["id"], "2026-10-12", "Day", user_id=w["wa"]["id"],
                        source_s3_key=key)
    for text in ("scaffolding inspection on level two", "concrete pour on level three"):
        chunks.insert_chunk(db, w["sa"]["id"], "2026-10-12", "topic", text, UNIT,
                            user_id=w["wa"]["id"], source_s3_key=key)
    res = _tools([{"id": "1", "name": "latest_activity", "query_embedding": ORTHO,
                   "args": {"query": "scaffolding", "on_or_before": D}}])
    rows, meta = res[0]["rows"], res[0]["meta"]
    assert [r["chunk_text"] for r in rows] == ["scaffolding inspection on level two"]
    assert rows[0]["lexical_hit"] is False and rows[0]["distance"] > 0.55
    assert meta["matched_date"] == "2026-10-12"
    assert "empty_reason" not in meta and "error" not in meta


def test_latest_activity_does_not_choose_a_near_day_without_the_words(db, world):
    """The distance gate is gone from day choice: a LATER day whose chunk is as
    near the query embedding as it gets, but does not name the subject, is not
    chosen; the earlier day that names it is, and only its naming row is kept."""
    w = world
    for day, text, emb in (("2026-10-13", "formwork stripped on level one", ORTHO),
                           ("2026-10-12", "scaffolding inspection on level two", UNIT),
                           ("2026-10-12", "concrete pour on level three", ORTHO)):
        key = f"reports/{day}/WorkerA/daily_report.json"
        topics.upsert_topic(db, w["sa"]["id"], day, "Day", user_id=w["wa"]["id"],
                            source_s3_key=key)
        chunks.insert_chunk(db, w["sa"]["id"], day, "topic", text, emb,
                            user_id=w["wa"]["id"], source_s3_key=key)
    res = _tools([{"id": "1", "name": "latest_activity", "query_embedding": ORTHO,
                   "args": {"query": "scaffolding", "on_or_before": D}}])
    assert [r["chunk_text"] for r in res[0]["rows"]] == ["scaffolding inspection on level two"]
    assert res[0]["meta"]["matched_date"] == "2026-10-12"


def test_naming_chunk_survives_a_crowded_day(db, world):
    """Spec 2026-10-20 §3.3: the day's search gets the subject words only. Fed the
    whole question, its keyword arm ORs "last or discuss or precast or panels" and
    takes the top 64 by ts_rank, so 70 chunks of chatter that say "last" and
    "discuss" over and over -- also the nearest by embedding -- outrank the one
    chunk that names the subject in both arms and push it out."""
    w = world
    day = "2026-10-12"
    key = f"reports/{day}/WorkerA/daily_report.json"
    topics.upsert_topic(db, w["sa"]["id"], day, "Day", user_id=w["wa"]["id"],
                        source_s3_key=key)
    for _ in range(70):
        chunks.insert_chunk(db, w["sa"]["id"], day, "topic",
                            "last time we said we would discuss it, discuss it at the last meeting", UNIT,
                            user_id=w["wa"]["id"], source_s3_key=key)
    chunks.insert_chunk(db, w["sa"]["id"], day, "topic", "precast panels arrive Friday",
                        ORTHO, user_id=w["wa"]["id"], source_s3_key=key)
    res = _tools([{"id": "1", "name": "latest_activity", "query_embedding": UNIT,
                   "args": {"query": "when did we last discuss the precast panels",
                            "on_or_before": D}}])
    assert [r["chunk_text"] for r in res[0]["rows"]] == ["precast panels arrive Friday"]
    assert res[0]["meta"]["matched_date"] == day
