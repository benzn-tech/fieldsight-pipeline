"""rag-search: a pinned topic and a named author narrow retrieval, and say so.

Spec 2026-09-15 §4.3 / §5 tests 1-6. What is asserted is ROUTING: which site,
author and day reach search_chunks, whether widening may run, and what `applied`
reports. Whether get_topic_visible's SQL is right is answered by
tests/integration/test_get_topic_visible.py, not here.

§4.3.1 (controller ruling, commit 1e21006): a pinned topic overrides a
requested `site`/`author` rather than combining with them, and each override
is reported in `applied.dropped`.
"""
import pytest

rag = pytest.importorskip("lambda_rag_search", reason="requires psycopg (installed in CI)")


class FakeConn:
    pass


CALLER = {"id": "u-1", "cognito_sub": "sub-1", "company_id": "c-1",
          "email": "a@x.nz", "first_name": "A", "last_name": "B",
          "global_role": "site_manager"}

ROW = {"id": "c-1", "site_id": "s-2", "topic_id": None, "report_date": "2026-09-03",
       "chunk_text": "Scaffold tagged.", "chunk_type": "topic", "distance": 0.1,
       "site_name": "UC PK", "site_slug": "uc-pk", "source_s3_key": "reports/x.json",
       "metadata": {}, "topic_title": "Scaffold", "topic_summary": ""}

TOPIC_ID = "df023596-1111-4222-8333-444455556666"
PINNED = {"id": TOPIC_ID, "title": "Scaffold handover", "summary": "Signed off.",
          "report_date": "2026-09-03", "site_id": "s-2", "site_name": "UC PK",
          "user_id": "u-7", "time_range": "09:10–09:40",
          "action_items": [{"text": "Send tag photos", "responsible": "Ben",
                            "deadline": None, "status": "open"}]}


def boom(*a, **k):
    raise AssertionError("must not be called")


def set_scope(mp, sites, authors, cross_company=False):
    mp.setattr(rag.scope, "visible_scope", lambda conn, caller: {
        "site_ids": set(sites),
        "author_ids": set(authors) if authors is not None else None,
        "cross_company": cross_company})


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(rag, "get_cached_connection", lambda *a, **k: FakeConn())
    monkeypatch.setattr(rag, "close_cached_connection", lambda *a, **k: None)
    monkeypatch.setattr(rag.users, "get_user_by_sub", lambda conn, sub: dict(CALLER))
    monkeypatch.setattr(rag.aliases, "list_active", lambda conn, cid, site_ids=None: [])
    monkeypatch.setattr(rag.sites, "get_company_site_by_slug", lambda conn, cid, slug: None)
    set_scope(monkeypatch, sites={"s-1", "s-2"}, authors=None)
    return monkeypatch


def wire_search(mp, rows=()):
    calls = []

    def fake(conn, qv, site_ids, k=5, author_ids=None, date_from=None, date_to=None, query_text=None):
        calls.append({"site_ids": sorted(site_ids),
                      "author_ids": sorted(author_ids) if author_ids is not None else None,
                      "date_from": date_from, "date_to": date_to})
        return [dict(r) for r in rows]

    mp.setattr(rag.chunks, "search_chunks", fake)
    return calls


def wire_topic(mp, result):
    seen = {}

    def fake(conn, topic_id, site_ids, author_ids):
        seen.update(topic_id=topic_id, site_ids=sorted(site_ids),
                    author_ids=sorted(author_ids) if author_ids is not None else None)
        return dict(result) if result else None

    mp.setattr(rag.topics, "get_topic_visible", fake)
    return seen


def event(**kw):
    ev = {"sub": "sub-1", "query_embedding": [0.1] * 1024}
    ev.update(kw)
    return ev


# -- spec §5 test 1 ----------------------------------------------------------

def test_a_visible_topic_pins_site_day_and_author_and_never_widens(wired):
    calls = wire_search(wired, rows=[])
    wired.setattr(rag.chunks, "latest_visible_date", boom)      # widen forced off
    wired.setattr(rag.users, "get_by_folder_name", boom)        # requested author ignored
    wire_topic(wired, PINNED)

    out = rag.lambda_handler(event(topic_row_id=TOPIC_ID, date_from="2026-08-01",
                                   date_to="2026-08-31", widen_when_empty=True,
                                   site="s-1", author="Someone_Else"), None)

    assert calls == [{"site_ids": ["s-2"], "author_ids": ["u-7"],
                      "date_from": "2026-09-03", "date_to": "2026-09-03"}]
    assert out["pinned_topic"]["title"] == "Scaffold handover"
    assert out["basis"] == {"from": "2026-09-03", "to": "2026-09-03", "widened": False}
    # §4.3.1: a requested site/author is overridden by the pinned topic, not
    # combined with it, and each override is reported dropped.
    assert out["applied"] == {"topic_row_id": TOPIC_ID, "topic_title": "Scaffold handover",
                              "site_id": "s-2", "date": "2026-09-03",
                              "dropped": [{"field": "site_id", "reason": "overridden_by_topic"},
                                          {"field": "author_folder", "reason": "overridden_by_topic"}]}


def test_a_pinned_topic_overrides_requested_site_and_author(wired):
    """Controller ruling, spec §4.3.1 (commit 1e21006, postdates this task's
    plan text): a visible topic wins over a requested site/author rather than
    combining with them, and BOTH overrides are named in `applied.dropped`
    even though Task 4's author-narrowing code does not exist yet."""
    wire_search(wired, rows=[])
    wire_topic(wired, PINNED)

    out = rag.lambda_handler(event(topic_row_id=TOPIC_ID, site="s-1", author="Someone_Else"), None)

    assert {"field": "site_id", "reason": "overridden_by_topic"} in out["applied"]["dropped"]
    assert {"field": "author_folder", "reason": "overridden_by_topic"} in out["applied"]["dropped"]


def test_the_topic_lookup_receives_the_callers_acl(wired):
    set_scope(wired, sites={"s-1", "s-2"}, authors={"u-1", "u-7"})
    wire_search(wired, rows=[ROW])
    seen = wire_topic(wired, PINNED)

    rag.lambda_handler(event(topic_row_id=TOPIC_ID), None)

    assert seen == {"topic_id": TOPIC_ID, "site_ids": ["s-1", "s-2"], "author_ids": ["u-1", "u-7"]}


# -- spec §5 test 2 ----------------------------------------------------------

@pytest.mark.parametrize("authors,expected", [({"u-1", "u-2"}, ["u-1", "u-2"]), (None, None)])
def test_a_null_author_topic_leaves_author_ids_as_resolved(wired, authors, expected):
    set_scope(wired, sites={"s-2"}, authors=authors)
    calls = wire_search(wired, rows=[ROW])
    wire_topic(wired, dict(PINNED, user_id=None))

    out = rag.lambda_handler(event(topic_row_id=TOPIC_ID), None)

    assert calls[0]["author_ids"] == expected       # never [None]
    assert out["pinned_topic"]["user_id"] is None


# -- spec §5 test 3 ----------------------------------------------------------

def test_an_invisible_topic_is_dropped_and_search_keeps_the_other_narrowing(wired):
    """get_topic_visible is the single gate for out-of-reach site, author outside
    author_ids, non_work, redacted and deleted-recording (proved on a real DB in
    tests/integration/test_get_topic_visible.py). Whatever the reason, the
    response must not reveal which."""
    calls = wire_search(wired, rows=[ROW])
    wire_topic(wired, None)

    out = rag.lambda_handler(event(topic_row_id=TOPIC_ID, date_from="2026-09-01",
                                   date_to="2026-09-01"), None)

    assert "pinned_topic" not in out
    assert out["applied"]["dropped"] == [{"field": "topic_row_id", "reason": "not_visible"}]
    assert calls == [{"site_ids": ["s-1", "s-2"], "author_ids": None,
                      "date_from": "2026-09-01", "date_to": "2026-09-01"}]


def test_hidden_and_unknown_topics_produce_the_same_response(wired):
    """Proves the HANDLER does not branch on the id -- both cases are stubbed to
    the same None from get_topic_visible, so this can only show the two inputs
    are routed identically here, not that a hidden and an unknown topic are
    actually indistinguishable in the real query. That equality is proved on a
    real DB by tests/integration/test_get_topic_visible.py."""
    wire_search(wired, rows=[ROW])
    wire_topic(wired, None)
    hidden = rag.lambda_handler(event(topic_row_id=TOPIC_ID), None)
    unknown = rag.lambda_handler(event(topic_row_id="00000000-0000-4000-8000-000000000000"), None)
    assert hidden == unknown


# -- spec §5 tests 4-6: author narrowing --------------------------------------

def test_an_author_outside_the_callers_company_matches_nothing(wired):
    seen = {}

    def lookup(conn, company_id, folder):
        seen["company"], seen["folder"] = company_id, folder
        return None

    wired.setattr(rag.users, "get_by_folder_name", lookup)
    wired.setattr(rag.users, "get_by_folder_name_global", boom)
    wired.setattr(rag.chunks, "search_chunks", boom)            # never the full set

    out = rag.lambda_handler(event(author="Other_Co_Worker"), None)

    assert seen == {"company": "c-1", "folder": "Other_Co_Worker"}
    assert out["chunks"] == []
    assert out["applied"]["dropped"] == [{"field": "author_folder", "reason": "not_visible"}]
    assert "author_folder" not in out["applied"]


def test_an_unrestricted_caller_is_narrowed_to_the_named_author(wired):
    set_scope(wired, sites={"s-1"}, authors=None)
    wired.setattr(rag.users, "get_by_folder_name", lambda conn, cid, folder: {"id": "u-9"})
    calls = wire_search(wired, rows=[ROW])

    out = rag.lambda_handler(event(author="Ben_UCPK2"), None)

    assert calls[0]["author_ids"] == ["u-9"]
    assert out["applied"] == {"author_folder": "Ben_UCPK2", "dropped": []}


def test_an_author_outside_the_callers_author_set_matches_nothing(wired):
    """A site_manager (SELF+WORKERS) naming a pm must get nothing, not the pm."""
    set_scope(wired, sites={"s-1"}, authors={"u-1", "u-2"})
    wired.setattr(rag.users, "get_by_folder_name", lambda conn, cid, folder: {"id": "u-9"})
    wired.setattr(rag.chunks, "search_chunks", boom)

    out = rag.lambda_handler(event(author="The_PM"), None)

    assert out["chunks"] == []
    assert out["applied"]["dropped"] == [{"field": "author_folder", "reason": "not_visible"}]


def test_an_author_inside_the_author_set_is_narrowed_to(wired):
    set_scope(wired, sites={"s-1"}, authors={"u-1", "u-9"})
    wired.setattr(rag.users, "get_by_folder_name", lambda conn, cid, folder: {"id": "u-9"})
    calls = wire_search(wired, rows=[ROW])

    rag.lambda_handler(event(author="A_Worker"), None)

    assert calls[0]["author_ids"] == ["u-9"]


def test_a_cross_company_caller_resolves_the_folder_globally(wired):
    set_scope(wired, sites={"s-1"}, authors=None, cross_company=True)
    wired.setattr(rag.users, "get_by_folder_name", boom)
    wired.setattr(rag.users, "get_by_folder_name_global", lambda conn, folder: {"id": "u-9"})
    calls = wire_search(wired, rows=[ROW])

    rag.lambda_handler(event(author="Ben_UCPK2"), None)

    assert calls[0]["author_ids"] == ["u-9"]


def test_a_uuid_object_author_id_is_normalised_to_str_before_narrowing(wired):
    """get_by_folder_name can return {"id": uuid.UUID(...)} (a real row, not a
    string) -- str(target["id"]) must match the scope's already-stringified
    author_ids or a real author narrows to nothing."""
    import uuid as _uuid
    uid = _uuid.UUID("00000000-0000-4000-8000-0000000000a1")
    set_scope(wired, sites={"s-1"}, authors={str(uid)})
    wired.setattr(rag.users, "get_by_folder_name", lambda conn, cid, folder: {"id": uid})
    calls = wire_search(wired, rows=[ROW])

    out = rag.lambda_handler(event(author="Ben_UCPK2"), None)

    assert calls[0]["author_ids"] == [str(uid)]
    assert out["applied"] == {"author_folder": "Ben_UCPK2", "dropped": []}


def test_a_cross_company_author_excluded_by_scope_matches_nothing_and_never_searches(wired):
    """cross_company=True with a non-None author set that excludes the
    globally-resolved id: not_visible, no chunks, search_chunks never called."""
    set_scope(wired, sites={"s-1"}, authors={"u-1", "u-2"}, cross_company=True)
    wired.setattr(rag.users, "get_by_folder_name", boom)
    wired.setattr(rag.users, "get_by_folder_name_global", lambda conn, folder: {"id": "u-9"})
    wired.setattr(rag.chunks, "search_chunks", boom)

    out = rag.lambda_handler(event(author="Someone_Else"), None)

    assert out["chunks"] == []
    assert out["applied"]["dropped"] == [{"field": "author_folder", "reason": "not_visible"}]


# -- site ----------------------------------------------------------------------

def test_a_site_in_reach_is_reported_as_applied(wired):
    calls = wire_search(wired, rows=[ROW])
    out = rag.lambda_handler(event(site="s-1"), None)
    assert calls[0]["site_ids"] == ["s-1"]
    assert out["applied"] == {"site_id": "s-1", "dropped": []}


def test_a_site_out_of_reach_is_not_visible_and_matches_nothing(wired):
    wired.setattr(rag.chunks, "search_chunks", boom)
    out = rag.lambda_handler(event(site="5c0e8d7a-1111-4222-8333-444455556666"), None)
    assert out["chunks"] == []
    assert out["applied"]["dropped"] == [{"field": "site_id", "reason": "not_visible"}]


def test_every_return_carries_applied(wired):
    for ev, patch in (({"sub": "sub-1", "query_embedding": None}, None),
                      (event(), "not_provisioned"),
                      (event(), "no_sites")):
        if patch == "not_provisioned":
            wired.setattr(rag.users, "get_user_by_sub", lambda conn, sub: None)
        if patch == "no_sites":
            wired.setattr(rag.users, "get_user_by_sub", lambda conn, sub: dict(CALLER))
            set_scope(wired, sites=set(), authors=None)
        out = rag.lambda_handler(ev, None)
        assert out["applied"] == {"dropped": []}, patch
