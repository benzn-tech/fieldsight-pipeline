"""rag-search `mode: "tools"` dispatcher (spec 2026-10-15-ask-retrieval-tools §3.3/§3.4).

Fakes for the repositories, the dispatcher's real control flow. The real SQL is in
tests/integration/test_rag_tools_dispatch_sql.py.
"""
import pytest

d = pytest.importorskip("rag_tools_dispatch", reason="requires psycopg (installed in CI)")
rag = pytest.importorskip("lambda_rag_search", reason="requires psycopg (installed in CI)")


class Clock:
    def __init__(self, ticks): self.ticks = list(ticks)
    def __call__(self): return self.ticks.pop(0) if len(self.ticks) > 1 else self.ticks[0]


SC = {"site_ids": {"s1"}, "author_ids": None, "company_id": "co", "cross_company": False}
CALLER = {"id": "u1", "company_id": "co", "sub": "sub-1"}
DAY = {"date_from": "2026-10-14", "date_to": "2026-10-14"}
REF = "7d3c1a52-0000-0000-0000-000000000000"


class FakeConn:
    """Records transaction() entries; the dispatcher wraps every call in one."""
    def __init__(self):
        self.transactions = 0

    def transaction(self):
        conn = self

        class _Tx:
            def __enter__(self):
                conn.transactions += 1
                return self

            def __exit__(self, *exc):
                return False
        return _Tx()


def _run(calls, monkeypatch, *, clock=None, search=None, metric=None, sc=SC,
         source_family="report", normalize=False, deadline=20, conn=None):
    if not normalize:
        monkeypatch.setattr(d, "_normalize_rows", lambda conn, caller, sc, rows, fields: rows)
    event = {"calls": calls}
    if deadline is not ...:
        event["deadline_s"] = deadline
    return d.run(conn or FakeConn(), CALLER, sc, event,
                 search_fn=search or (lambda e, c: {"chunks": []}),
                 metric_fn=metric or (lambda e: {"value": 0}),
                 clock=clock or Clock([0.0]), source_family=source_family)["results"]


# ---- batch-level rules -------------------------------------------------------------

def test_one_failing_call_does_not_lose_the_others(monkeypatch):
    monkeypatch.setattr(d.topics, "day_overview_rows",
                        lambda *a, **k: [{"id": "t1", "title": "Pour"}])
    def boom(*a, **k): raise RuntimeError("sql")
    monkeypatch.setattr(d.users, "find_visible_people", boom)
    res = _run([{"id": "a", "name": "day_overview", "args": dict(DAY)},
                {"id": "b", "name": "find_people", "args": {"name": "Mike"}},
                {"id": "c", "name": "day_overview",
                 "args": {"date_from": "2026-10-13", "date_to": "2026-10-13"}}],
               monkeypatch)
    assert [r["meta"].get("error") for r in res] == [None, "upstream", None]
    assert res[0]["rows"][0]["title"] == "Pour"
    assert [r["id"] for r in res] == ["a", "b", "c"]


def test_failure_log_carries_the_exception_type_not_its_text(monkeypatch, caplog):
    def boom(*a, **k): raise RuntimeError("SELECT secret Mike Tane")
    monkeypatch.setattr(d.users, "find_visible_people", boom)
    with caplog.at_level("INFO"):
        _run([{"id": "b", "name": "find_people", "args": {"name": "Mike Tane"}}], monkeypatch)
    text = caplog.text
    assert "RuntimeError" in text
    assert "secret" not in text and "Mike" not in text


def test_past_deadline_calls_do_not_run(monkeypatch):
    ran = []
    monkeypatch.setattr(d.topics, "day_overview_rows", lambda *a, **k: ran.append(1) or [])
    monkeypatch.setattr(d.topics, "latest_visible_topic_date", lambda *a, **k: None)
    res = _run([{"id": "a", "name": "day_overview", "args": dict(DAY)},
                {"id": "b", "name": "day_overview",
                 "args": {"date_from": "2026-10-13", "date_to": "2026-10-13"}}],
               monkeypatch, clock=Clock([0.0, 0.0, 25.0]))
    assert len(ran) == 1 and res[1]["meta"]["error"] == "timeout"
    assert res[1]["rows"] == []


@pytest.mark.parametrize("deadline", [0, 0.0, -1, float("nan"), float("inf") * -1])
def test_no_time_left_means_every_call_times_out_without_running(monkeypatch, deadline):
    ran = []
    monkeypatch.setattr(d.users, "find_visible_people", lambda *a, **k: ran.append(1) or [])
    res = _run([{"id": "a", "name": "find_people", "args": {"name": "x"}},
                {"id": "b", "name": "find_people", "args": {"name": "y"}}],
               monkeypatch, deadline=deadline)
    assert not ran
    assert [r["meta"]["error"] for r in res] == ["timeout", "timeout"]


def test_missing_deadline_means_the_twenty_second_default(monkeypatch):
    ran = []
    monkeypatch.setattr(d.users, "find_visible_people", lambda *a, **k: ran.append(1) or [])
    res = _run([{"id": "a", "name": "find_people", "args": {"name": "x"}}],
               monkeypatch, deadline=..., clock=Clock([0.0, 19.9]))
    assert ran and "error" not in res[0]["meta"]
    assert d._deadline({}) == 20.0 and d._deadline({"deadline_s": 99}) == 20.0


def test_every_call_runs_in_its_own_transaction(monkeypatch):
    conn = FakeConn()
    monkeypatch.setattr(d.users, "find_visible_people", lambda *a, **k: [])
    _run([{"id": "a", "name": "find_people", "args": {"name": "x"}},
          {"id": "b", "name": "find_people", "args": {"name": "y"}}], monkeypatch, conn=conn)
    assert conn.transactions == 2


def test_log_carries_arg_shape_and_caller_hash_never_values(monkeypatch, caplog):
    import ask_voice_turn
    monkeypatch.setattr(d.users, "find_visible_people", lambda *a, **k: [
        {"person_ref": "m1", "name": "Mike Tane", "folder_name": "Mike_T"}])
    monkeypatch.setattr(d.topics, "day_overview_rows", lambda *a, **k: [{"id": "t", "title": "Pour"}])
    monkeypatch.setattr(d.sites, "list_sites_by_ids",
                        lambda conn, ids: [{"id": "s1", "name": "Riverside"}])
    with caplog.at_level("INFO"):
        _run([{"id": "a", "name": "day_overview",
               "args": {"date_from": "2026-10-12", "date_to": "2026-10-14",
                        "person": "Mike Tane", "site": "Riverside"}}], monkeypatch)
    line = next(r.getMessage() for r in caplog.records if "ask tool day_overview" in r.getMessage())
    assert "fields=date_from,date_to,person,site" in line
    assert "days=3" in line
    assert f"caller={ask_voice_turn.sub_hash('sub-1')}" in line
    assert "rows=1" in line and "err=None" in line
    for value in ("Mike", "Riverside", "2026-10-1", "Pour", "sub-1 "):
        assert value not in caplog.text


def test_more_than_four_calls_rejected(monkeypatch):
    res = _run([{"id": str(i), "name": "find_people", "args": {"name": "x"}} for i in range(5)],
               monkeypatch)
    assert all(r["meta"] == {"error": "bad_args", "field": "calls"} for r in res)
    assert all(r["rows"] == [] for r in res)


def test_unknown_tool_is_bad_args_name(monkeypatch):
    res = _run([{"id": "a", "name": "drop_tables", "args": {}}], monkeypatch)
    assert res[0]["meta"]["error"] == "bad_args" and res[0]["meta"]["field"] == "name"


def test_rows_are_json_safe(monkeypatch):
    import datetime
    import uuid
    monkeypatch.setattr(d.topics, "day_overview_rows", lambda *a, **k: [
        {"id": uuid.UUID(REF), "report_date": datetime.date(2026, 10, 14), "title": "Pour"}])
    res = _run([{"id": "a", "name": "day_overview", "args": dict(DAY)}], monkeypatch)
    assert res[0]["rows"][0] == {"id": REF, "report_date": "2026-10-14", "title": "Pour"}


# ---- day_overview ------------------------------------------------------------------

def test_empty_day_carries_latest_before_from_topics(monkeypatch):
    seen = {}
    monkeypatch.setattr(d.topics, "day_overview_rows", lambda *a, **k: [])
    def latest(conn, site_ids, before, **kw):
        seen.update(before=before, **kw)
        return "2026-10-12"
    monkeypatch.setattr(d.topics, "latest_visible_topic_date", latest)
    res = _run([{"id": "a", "name": "day_overview", "args": dict(DAY)}], monkeypatch)
    assert res[0]["meta"]["empty_reason"] == "nothing_in_range"
    assert res[0]["meta"]["latest_before"] == {"date": "2026-10-12", "from": "topics"}
    assert seen["before"] == "2026-10-14"


def test_no_sites_is_nothing_visible(monkeypatch):
    monkeypatch.setattr(d.topics, "day_overview_rows", lambda *a, **k: [])
    monkeypatch.setattr(d.topics, "latest_visible_topic_date", lambda *a, **k: None)
    res = _run([{"id": "a", "name": "day_overview", "args": dict(DAY)}],
               monkeypatch, sc={"site_ids": set(), "author_ids": None, "company_id": "co"})
    assert res[0]["meta"]["empty_reason"] == "nothing_visible"


def test_day_overview_truncation_trims_to_limit_and_flags_it(monkeypatch):
    def rows(conn, site_ids, df, dt, *, limit=60, **kw):
        return [{"id": f"t{i}", "title": str(i)} for i in range(limit + 1)]
    monkeypatch.setattr(d.topics, "day_overview_rows", rows)
    res = _run([{"id": "a", "name": "day_overview", "args": dict(DAY)}], monkeypatch)
    assert len(res[0]["rows"]) == d.DAY_LIMIT
    assert res[0]["meta"]["truncated"] is True
    assert res[0]["rows"][-1]["id"] == f"t{d.DAY_LIMIT - 1}"


def test_day_overview_not_truncated_when_no_extra_row(monkeypatch):
    monkeypatch.setattr(d.topics, "day_overview_rows",
                        lambda *a, **k: [{"id": "t1", "title": "Pour"}])
    res = _run([{"id": "a", "name": "day_overview", "args": dict(DAY)}], monkeypatch)
    assert res[0]["meta"]["truncated"] is False and len(res[0]["rows"]) == 1


def test_day_overview_titles_and_summaries_get_aliases(monkeypatch):
    monkeypatch.setattr(d.topics, "day_overview_rows", lambda *a, **k: [
        {"id": "t1", "title": "Pour with Jon", "summary": "Jon checked", "site_name": "Jon"}])
    monkeypatch.setattr(d.aliases, "list_active", lambda conn, cid, site_ids=None: [
        {"wrong_term": "Jon", "right_term": "John"}])
    res = _run([{"id": "a", "name": "day_overview", "args": dict(DAY)}],
               monkeypatch, normalize=True)
    row = res[0]["rows"][0]
    assert row["title"] == "Pour with John" and row["summary"] == "John checked"


# ---- site / person resolution ------------------------------------------------------

def test_site_by_name_case_insensitive_within_reach(monkeypatch):
    seen = {}
    monkeypatch.setattr(d.sites, "list_sites_by_ids",
                        lambda conn, ids: [{"id": "s1", "name": "Riverside Stage 2"}])
    def rows(conn, site_ids, df, dt, **kw):
        seen.update(kw)
        return [{"id": "t1"}]
    monkeypatch.setattr(d.topics, "day_overview_rows", rows)
    _run([{"id": "a", "name": "day_overview",
           "args": dict(DAY, site="  riverside stage 2 ")}], monkeypatch)
    assert seen["site_id"] == "s1"


def test_unresolved_site_is_no_match_and_runs_nothing(monkeypatch):
    called = []
    monkeypatch.setattr(d.sites, "list_sites_by_ids",
                        lambda conn, ids: [{"id": "s1", "name": "Alpha"}])
    monkeypatch.setattr(d.topics, "day_overview_rows", lambda *a, **k: called.append(1) or [])
    res = _run([{"id": "a", "name": "day_overview", "args": dict(DAY, site="Bravo")},
                {"id": "b", "name": "day_overview", "args": dict(DAY, site=REF)}], monkeypatch)
    assert not called
    assert all(r["rows"] == [] and r["meta"]["empty_reason"] == "no_match" for r in res)
    assert all("error" not in r["meta"] for r in res)


def test_ambiguous_person_returns_no_rows(monkeypatch):
    monkeypatch.setattr(d.users, "find_visible_people", lambda *a, **k: [
        {"person_ref": "m1", "name": "Mike Smith", "folder_name": "Mike_S"},
        {"person_ref": "m2", "name": "Mike Tane", "folder_name": "Mike_T"}])
    called = []
    monkeypatch.setattr(d.topics, "day_overview_rows", lambda *a, **k: called.append(1) or [{"id": "x"}])
    res = _run([{"id": "a", "name": "day_overview", "args": dict(DAY, person="Mike")}],
               monkeypatch)
    assert res[0]["rows"] == [] and not called
    assert [p["name"] for p in res[0]["meta"]["person_ambiguous"]] == ["Mike Smith", "Mike Tane"]
    assert res[0]["meta"]["empty_reason"] == "person_ambiguous"


def test_person_ref_out_of_reach_is_unresolved_not_error(monkeypatch):
    monkeypatch.setattr(d.users, "person_in_reach", lambda *a, **k: False)
    monkeypatch.setattr(d.users, "find_visible_people", lambda *a, **k: [])
    res = _run([{"id": "a", "name": "day_overview", "args": dict(DAY, person=REF)}], monkeypatch)
    assert res[0]["rows"] == [] and res[0]["meta"].get("person_unresolved") is True
    assert res[0]["meta"]["empty_reason"] == "person_unresolved"
    assert "error" not in res[0]["meta"]


def test_person_name_resolving_to_one_narrows_by_user_id(monkeypatch):
    seen = {}
    monkeypatch.setattr(d.users, "find_visible_people", lambda *a, **k: [
        {"person_ref": "m1", "name": "Mike Smith", "folder_name": "Mike_S"}])
    def rows(conn, site_ids, df, dt, **kw):
        seen.update(kw)
        return [{"id": "t1"}]
    monkeypatch.setattr(d.topics, "day_overview_rows", rows)
    _run([{"id": "a", "name": "day_overview", "args": dict(DAY, person="Mike")}], monkeypatch)
    assert seen["user_id"] == "m1"


# ---- search_records ----------------------------------------------------------------

def test_search_records_builds_a_plain_search_event(monkeypatch):
    seen = []
    def search(ev, ctx):
        seen.append(ev)
        return {"chunks": [{"id": "c1", "chunk_text": "pour"}]}
    res = _run([{"id": "a", "name": "search_records", "query_embedding": [0.1],
                 "args": dict(DAY, query="pour", k=5)}], monkeypatch, search=search)
    ev = seen[0]
    assert ev == {"sub": "sub-1", "query_embedding": [0.1], "question": "pour", "k": 10,
                  "date_from": "2026-10-14", "date_to": "2026-10-14"}
    assert res[0]["rows"] == [{"id": "c1", "chunk_text": "pour"}]


def test_search_records_default_k_is_sixteen(monkeypatch):
    seen = []
    _run([{"id": "a", "name": "search_records", "query_embedding": [0.1],
           "args": {"query": "pour"}}], monkeypatch,
         search=lambda ev, c: seen.append(ev) or {"chunks": []})
    assert seen[0]["k"] == 16 and "date_from" not in seen[0]


def test_empty_dated_search_carries_latest_before_from_chunks(monkeypatch):
    seen = {}
    def latest(conn, site_ids, **kw):
        seen.update(site_ids=site_ids, **kw)
        return "2026-10-10"
    monkeypatch.setattr(d.chunks, "latest_visible_date", latest)
    res = _run([{"id": "a", "name": "search_records", "query_embedding": [0.1],
                 "args": dict(DAY, query="pour")}], monkeypatch)
    assert res[0]["meta"]["latest_before"] == {"date": "2026-10-10", "from": "chunks"}
    assert res[0]["meta"]["empty_reason"] == "nothing_in_range"
    assert seen["on_or_before"] == "2026-10-13"
    assert seen["source_family"] == "report"


def test_empty_undated_search_is_no_match(monkeypatch):
    res = _run([{"id": "a", "name": "search_records", "query_embedding": [0.1],
                 "args": {"query": "pour"}}], monkeypatch)
    assert res[0]["meta"]["empty_reason"] == "no_match"
    assert "latest_before" not in res[0]["meta"]


def test_search_records_person_passes_that_persons_folder(monkeypatch):
    seen = []
    monkeypatch.setattr(d.users, "find_visible_people", lambda *a, **k: [
        {"person_ref": "m1", "name": "Mike Smith", "folder_name": "Mike_S"}])
    monkeypatch.setattr(d.users, "get_by_id",
                        lambda conn, uid: {"id": "m1", "folder_name": "Mike_S"})
    monkeypatch.setattr(d.users, "get_by_folder_name",
                        lambda conn, co, f: {"id": "m1", "folder_name": "Mike_S"})
    _run([{"id": "a", "name": "search_records", "query_embedding": [0.1],
           "args": {"query": "pour", "person": "Mike"}}], monkeypatch,
         search=lambda ev, c: seen.append(ev) or {"chunks": [{"id": "c"}]})
    assert seen[0]["author"] == "Mike_S"


@pytest.mark.parametrize("target", [None, {"id": "other", "folder_name": "Mike_S"}])
def test_search_person_without_a_safe_folder_never_searches_everyone(monkeypatch, target):
    """A NULL folder would make _search drop the author filter; a folder that _search
    resolves to a DIFFERENT user would search the wrong person. Both: no rows."""
    calls = []
    monkeypatch.setattr(d.users, "person_in_reach", lambda *a, **k: True)
    monkeypatch.setattr(d.users, "get_by_id",
                        lambda conn, uid: {"id": REF, "folder_name": target and "Mike_S"})
    monkeypatch.setattr(d.users, "get_by_folder_name", lambda conn, co, f: target)
    res = _run([{"id": "a", "name": "search_records", "query_embedding": [0.1],
                 "args": {"query": "pour", "person": REF}}], monkeypatch,
               search=lambda ev, c: calls.append(ev) or {"chunks": [{"id": "c"}]})
    assert not calls and res[0]["rows"] == []
    assert res[0]["meta"]["person_unresolved"] is True


def test_search_not_provisioned_maps_to_error(monkeypatch):
    res = _run([{"id": "a", "name": "search_records", "query_embedding": [0.1],
                 "args": {"query": "pour"}}], monkeypatch,
               search=lambda ev, c: {"chunks": [], "error": "caller not provisioned"})
    assert res[0]["meta"]["error"] == "not_provisioned"


@pytest.mark.parametrize("name", ["search_records", "latest_activity"])
def test_search_call_without_embedding_is_bad_args_not_upstream(monkeypatch, name):
    ran = []
    monkeypatch.setattr(d.chunks, "latest_matching_date", lambda *a, **k: ran.append(1))
    res = _run([{"id": "a", "name": name, "args": {"query": "pour"}}], monkeypatch,
               search=lambda ev, c: ran.append(1) or {"chunks": []})
    assert not ran and res[0]["rows"] == []
    assert (res[0]["meta"]["error"], res[0]["meta"]["field"]) == ("bad_args", "query_embedding")


# ---- latest_activity ---------------------------------------------------------------

def test_latest_activity_without_query_reads_that_days_overview(monkeypatch):
    seen = {}
    def latest(conn, site_ids, before, **kw):
        seen["before"] = before
        return "2026-10-12"
    def rows(conn, site_ids, df, dt, **kw):
        seen["day"] = (df, dt)
        return [{"id": "t1", "title": "Pour"}]
    monkeypatch.setattr(d.topics, "latest_visible_topic_date", latest)
    monkeypatch.setattr(d.topics, "day_overview_rows", rows)
    res = _run([{"id": "a", "name": "latest_activity", "args": {"on_or_before": "2026-10-14"}}],
               monkeypatch)
    assert seen == {"before": "2026-10-15", "day": ("2026-10-12", "2026-10-12")}
    assert res[0]["rows"] == [{"id": "t1", "title": "Pour"}]
    assert res[0]["meta"]["truncated"] is False


def test_latest_activity_default_bound_never_excludes_a_local_today_ahead_of_utc(monkeypatch):
    import datetime
    seen = {}
    monkeypatch.setattr(d.topics, "latest_visible_topic_date",
                        lambda conn, s, before, **kw: seen.setdefault("before", before) and None)
    _run([{"id": "a", "name": "latest_activity", "args": {}}], monkeypatch)
    utc_today = datetime.datetime.now(datetime.timezone.utc).date()
    # NZ is up to UTC+13/+14: the strictly-before bound must be after NZ's today.
    assert datetime.date.fromisoformat(seen["before"]) >= utc_today + datetime.timedelta(days=2)


def test_latest_activity_with_query_uses_the_subject_terms(monkeypatch):
    seen = {}
    def match(conn, site_ids, **kw):
        seen.update(kw)
        return "2026-10-11"
    monkeypatch.setattr(d.chunks, "latest_matching_date", match)
    searches = []
    def search(ev, c):
        searches.append(ev)
        # "named" arrives as the vector copy (lexical_hit False) and is far; "near"
        # is close but does not name the subject; "flag" claims lexical_hit but its
        # text does not name it. Only the SQL re-check decides.
        return {"chunks": [{"id": "near", "distance": 0.1, "lexical_hit": False},
                           {"id": "named", "distance": 0.9, "lexical_hit": False},
                           {"id": "flag", "distance": 0.9, "lexical_hit": True}]}
    def recheck(conn, ids, terms):
        seen["recheck"] = (list(ids), list(terms))
        return {"named"}
    monkeypatch.setattr(d.chunks, "subject_matched_ids", recheck)
    res = _run([{"id": "a", "name": "latest_activity", "query_embedding": [0.1],
                 "args": {"query": "when did we pour the slab", "on_or_before": "2026-10-14"}}],
               monkeypatch, search=search)
    assert seen["subject_terms"] == ["pour", "slab"]
    assert "gate" not in seen and "query_text" not in seen
    assert seen["on_or_before"] == "2026-10-14" and seen["source_family"] == "report"
    assert searches[0]["date_from"] == searches[0]["date_to"] == "2026-10-11"
    # The re-check binds the SAME subject words that chose the day.
    assert seen["recheck"] == (["near", "named", "flag"], ["pour", "slab"])
    assert [r["id"] for r in res[0]["rows"]] == ["named"]
    assert res[0]["meta"]["matched_date"] == "2026-10-11"


def test_latest_activity_with_query_searches_wide_then_rechecks_then_trims(monkeypatch):
    monkeypatch.setattr(d.chunks, "latest_matching_date", lambda *a, **k: "2026-10-11")
    far = [{"id": f"far{i}", "distance": 0.9, "lexical_hit": False} for i in range(30)]
    named = [{"id": f"named{i}", "distance": 0.1 + i / 100, "lexical_hit": False}
             for i in range(20)]
    monkeypatch.setattr(d.chunks, "subject_matched_ids",
                        lambda conn, ids, terms: {r["id"] for r in named})
    seen = []
    def search(ev, c):
        seen.append(ev)
        return {"chunks": far + named}   # the matches rank BELOW the default k*2
    res = _run([{"id": "a", "name": "latest_activity", "query_embedding": [0.1],
                 "args": {"query": "pour"}}], monkeypatch, search=search)
    assert seen[0]["k"] == 64
    # Trimmed to what an ordinary search returns (k*2): ask_tools' post-search
    # (lexical admit + rerank) reduces that to k, as for search_records.
    assert [r["id"] for r in res[0]["rows"]] == [f"named{i}" for i in range(d.DEFAULT_K * 2)]


def test_missing_embedding_is_bad_args_before_subject_check(monkeypatch):
    """Only asking words (no subject) AND no embedding: the orchestrator's
    contract break wins, so the model is not told "no match"."""
    res = _run([{"id": "a", "name": "latest_activity", "args": {"query": "when did we last talk"}}],
               monkeypatch)
    assert res[0]["rows"] == []
    assert (res[0]["meta"]["error"], res[0]["meta"]["field"]) == ("bad_args", "query_embedding")
    assert "empty_reason" not in res[0]["meta"]


def test_cjk_only_query_is_no_match_without_sql(monkeypatch):
    ran = []
    monkeypatch.setattr(d.chunks, "latest_matching_date", lambda *a, **k: ran.append(1))
    res = _run([{"id": "a", "name": "latest_activity", "query_embedding": [0.1],
                 "args": {"query": "上次浇混凝土是什么时候"}}],
               monkeypatch, search=lambda ev, c: ran.append(1) or {"chunks": []})
    assert not ran
    assert res[0]["rows"] == [] and res[0]["meta"]["empty_reason"] == "no_match"


def test_day_search_gets_only_subject_words(monkeypatch):
    """The day's search gets the subject words, not the whole question: fed
    "last"/"time"/"talk", its keyword arm's top 64 can be crowded out (spec
    2026-10-20 §3.3)."""
    monkeypatch.setattr(d.chunks, "latest_matching_date", lambda *a, **k: "2026-10-11")
    monkeypatch.setattr(d.chunks, "subject_matched_ids", lambda *a, **k: set())
    seen = []
    res = _run([{"id": "a", "name": "latest_activity", "query_embedding": [0.1],
                 "args": {"query": "when did we last pour concrete"}}], monkeypatch,
               search=lambda ev, c: seen.append(ev) or {"chunks": []})
    assert seen[0]["question"] == "pour concrete"
    assert seen[0]["date_from"] == seen[0]["date_to"] == "2026-10-11"
    assert seen[0]["k"] == 64
    assert res[0]["meta"]["matched_date"] == "2026-10-11"


def test_only_rechecked_rows_are_kept(monkeypatch):
    monkeypatch.setattr(d.chunks, "latest_matching_date", lambda *a, **k: "2026-10-11")
    monkeypatch.setattr(d.chunks, "subject_matched_ids", lambda *a, **k: {"b"})
    res = _run([{"id": "a", "name": "latest_activity", "query_embedding": [0.1],
                 "args": {"query": "pour concrete"}}], monkeypatch,
               search=lambda ev, c: {"chunks": [{"id": "a", "distance": 0.2},
                                                {"id": "b", "distance": 0.6}]})
    assert [r["id"] for r in res[0]["rows"]] == ["b"]


def test_day_without_rechecked_rows_is_no_match_with_date(monkeypatch):
    monkeypatch.setattr(d.chunks, "latest_matching_date", lambda *a, **k: "2026-10-11")
    monkeypatch.setattr(d.chunks, "subject_matched_ids", lambda *a, **k: set())
    res = _run([{"id": "a", "name": "latest_activity", "query_embedding": [0.1],
                 "args": {"query": "pour concrete"}}], monkeypatch,
               search=lambda ev, c: {"chunks": [{"id": "a", "distance": 0.2}]})
    assert res[0]["rows"] == [] and res[0]["meta"]["empty_reason"] == "no_match"
    assert res[0]["meta"]["matched_date"] == "2026-10-11"


def test_latest_activity_with_query_no_day_is_no_match(monkeypatch):
    monkeypatch.setattr(d.chunks, "latest_matching_date", lambda *a, **k: None)
    res = _run([{"id": "a", "name": "latest_activity", "query_embedding": [0.1],
                 "args": {"query": "pour"}}], monkeypatch)
    assert res[0]["rows"] == [] and res[0]["meta"]["empty_reason"] == "no_match"


# ---- find_people / count_metric ----------------------------------------------------

def test_find_people_empty_is_no_match(monkeypatch):
    monkeypatch.setattr(d.users, "find_visible_people", lambda *a, **k: [])
    res = _run([{"id": "a", "name": "find_people", "args": {"name": "Zed"}}], monkeypatch)
    assert res[0]["meta"]["empty_reason"] == "no_match"


def test_count_metric_passes_zero_kind_through(monkeypatch):
    seen = []
    res = _run([{"id": "a", "name": "count_metric",
                 "args": {"metric": "duration", "date_from": "2026-10-14", "date_to": "2026-10-14"}}],
               monkeypatch,
               metric=lambda e: seen.append(e) or {
                   "value": 0, "n": 0, "notes": {"zero_kind": "no_rows_for_that_day"}})
    assert res[0]["meta"]["empty_reason"] == "no_rows_for_that_day"
    assert seen[0] == {"mode": "metric", "sub": "sub-1", "metric": "duration",
                       "date_from": "2026-10-14", "date_to": "2026-10-14"}
    assert res[0]["rows"][0]["value"] == 0


def test_count_metric_nonzero_has_no_empty_reason(monkeypatch):
    res = _run([{"id": "a", "name": "count_metric",
                 "args": {"metric": "count_photos", "date_from": "2026-10-14",
                          "date_to": "2026-10-14"}}],
               monkeypatch, metric=lambda e: {"value": 3, "n": 3, "notes": {}})
    assert "empty_reason" not in res[0]["meta"] and res[0]["rows"][0]["value"] == 3


def test_count_metric_not_provisioned(monkeypatch):
    res = _run([{"id": "a", "name": "count_metric",
                 "args": {"metric": "duration", "date_from": "2026-10-14",
                          "date_to": "2026-10-14"}}],
               monkeypatch, metric=lambda e: {"error": "caller not provisioned"})
    assert res[0]["meta"]["error"] == "not_provisioned" and res[0]["rows"] == []


# ---- the handler route -------------------------------------------------------------

def test_handler_unprovisioned_caller_fails_every_call(monkeypatch):
    monkeypatch.setattr(rag, "get_cached_connection", lambda *a, **k: object())
    monkeypatch.setattr(rag.users, "get_user_by_sub", lambda conn, sub: None)
    res = rag._search({"mode": "tools", "sub": "nobody", "deadline_s": 20, "calls": [
        {"id": "1", "name": "find_people", "args": {"name": "x"}},
        {"id": "2", "name": "day_overview", "args": dict(DAY)}]}, None)["results"]
    assert [r["meta"] for r in res] == [{"error": "not_provisioned"}] * 2
    assert [r["id"] for r in res] == ["1", "2"]


def test_handler_resolves_scope_once_and_passes_sub_and_family(monkeypatch):
    monkeypatch.setattr(rag, "get_cached_connection", lambda *a, **k: object())
    monkeypatch.setattr(rag.users, "get_user_by_sub",
                        lambda conn, sub: {"id": "u1", "company_id": "co", "cognito_sub": sub,
                                           "global_role": "gm"})
    monkeypatch.setattr(rag.scope, "visible_scope", lambda conn, caller: {
        "site_ids": {"s1"}, "author_ids": None, "cross_company": False})
    monkeypatch.setattr(rag, "INDEX_FAMILY", "extraction")
    seen = {}
    def fake_run(conn, caller, sc, event, **kw):
        seen.update(caller=caller, sc=sc, **kw)
        return {"results": []}
    monkeypatch.setattr(d, "run", fake_run)
    rag._search({"mode": "tools", "sub": "sub-9", "calls": []}, None)
    assert seen["caller"]["sub"] == "sub-9"
    assert seen["sc"] == {"site_ids": {"s1"}, "author_ids": None, "company_id": "co",
                          "cross_company": False}
    assert seen["source_family"] == "extraction"
    assert seen["search_fn"] is rag._search and seen["metric_fn"] is rag._metric


# ---- rerank candidate parity (candidate_k) -----------------------------------------

@pytest.mark.parametrize("candidate_k,k,want", [
    (None, None, 16), (32, None, 32), (32, 5, 32), (32, 12, 32), (10, 12, 24),
    (200, None, 64), (0, None, 16), ("32", None, 16), (True, None, 16), (-5, None, 16)])
def test_search_records_fetch_is_max_of_2k_and_candidate_k_capped_64(monkeypatch, candidate_k,
                                                                      k, want):
    seen = []
    call = {"id": "a", "name": "search_records", "query_embedding": [0.1],
            "args": {"query": "pour", **({"k": k} if k else {})}}
    if candidate_k is not None:
        call["candidate_k"] = candidate_k
    _run([call], monkeypatch, search=lambda ev, c: seen.append(ev) or {"chunks": []})
    assert seen[0]["k"] == want


def test_latest_activity_trims_to_the_candidate_pool(monkeypatch):
    monkeypatch.setattr(d.chunks, "latest_matching_date", lambda *a, **k: "2026-10-11")
    near = [{"id": f"n{i}", "distance": 0.1, "lexical_hit": False} for i in range(50)]
    monkeypatch.setattr(d.chunks, "subject_matched_ids",
                        lambda conn, ids, terms: {r["id"] for r in near})
    seen = []
    res = _run([{"id": "a", "name": "latest_activity", "query_embedding": [0.1],
                 "candidate_k": 32, "args": {"query": "pour"}}], monkeypatch,
               search=lambda ev, c: seen.append(ev) or {"chunks": near})
    assert seen[0]["k"] == d.LATEST_DAY_SEARCH_K
    assert len(res[0]["rows"]) == 32


# ---- final review fixes (M3, M4, M8) -----------------------------------------------

NO_SITES = {"site_ids": set(), "author_ids": None, "company_id": "co"}


@pytest.mark.parametrize("site", [REF, "Alpha"])
def test_a_site_asked_by_a_caller_with_no_sites_is_nothing_visible(monkeypatch, site):
    monkeypatch.setattr(d.sites, "list_sites_by_ids", lambda conn, ids: [])
    res = _run([{"id": "a", "name": "day_overview", "args": dict(DAY, site=site)}],
               monkeypatch, sc=NO_SITES)
    assert res[0]["rows"] == [] and res[0]["meta"]["empty_reason"] == "nothing_visible"


def test_latest_activity_with_query_and_person_resolves_the_folder_once(monkeypatch):
    gets, seen = [], []
    monkeypatch.setattr(d.users, "person_in_reach", lambda *a, **k: True)
    monkeypatch.setattr(d.users, "get_by_id",
                        lambda conn, uid: gets.append(uid) or {"id": REF, "folder_name": "Mike_S"})
    monkeypatch.setattr(d.users, "get_by_folder_name",
                        lambda conn, co, f: {"id": REF, "folder_name": "Mike_S"})
    monkeypatch.setattr(d.chunks, "latest_matching_date", lambda *a, **k: "2026-10-11")
    monkeypatch.setattr(d.chunks, "subject_matched_ids", lambda *a, **k: {"c"})
    res = _run([{"id": "a", "name": "latest_activity", "query_embedding": [0.1],
                 "args": {"query": "pour", "person": REF}}], monkeypatch,
               search=lambda ev, c: seen.append(ev) or {
                   "chunks": [{"id": "c", "distance": 0.1}]})
    assert gets == [REF]
    assert seen[0]["author"] == "Mike_S" and [r["id"] for r in res[0]["rows"]] == ["c"]


def _latest_query_call(query="pour"):
    args = {"on_or_before": "2026-10-14"}
    if query:
        args["query"] = query
    return {"id": "a", "name": "latest_activity", "query_embedding": [0.1], "args": args}


@pytest.mark.parametrize("left,runs", [(4.9, False), (5.0, True), (20.0, True)])
def test_latest_activity_with_query_needs_five_seconds_to_start(monkeypatch, left, runs):
    """Its day query plus the full search of that day can outrun the 23 s Lambda
    timeout if it starts late, and a Lambda timeout loses the whole batch: refuse
    to START it with < 5 s left."""
    scans = []
    monkeypatch.setattr(d.chunks, "latest_matching_date",
                        lambda *a, **k: scans.append(1) or None)
    res = _run([_latest_query_call()], monkeypatch, clock=Clock([0.0, 20.0 - left]))
    assert bool(scans) is runs
    assert (res[0]["meta"].get("error") == "timeout") is (not runs)


def test_the_five_second_guard_is_only_for_latest_activity_with_a_query(monkeypatch):
    ran = []
    monkeypatch.setattr(d.topics, "latest_visible_topic_date",
                        lambda *a, **k: ran.append("latest") or None)
    monkeypatch.setattr(d.users, "find_visible_people", lambda *a, **k: ran.append("find") or [])
    res = _run([_latest_query_call(query=None),
                {"id": "b", "name": "find_people", "args": {"name": "x"}},
                {"id": "c", "name": "search_records", "query_embedding": [0.1],
                 "args": {"query": "pour"}}],
               monkeypatch, clock=Clock([0.0, 16.0]),
               search=lambda ev, c: ran.append("search") or {"chunks": []})
    assert ran == ["latest", "find", "search"]
    assert all("error" not in r["meta"] for r in res)
