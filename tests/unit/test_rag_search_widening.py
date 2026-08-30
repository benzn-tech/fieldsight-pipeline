"""rag-search: when a dated search finds nothing, widen to the nearest day.

Ask now sends the range the question named. On a corpus with gaps -- which is
every corpus, and today's more than most -- that turns "what happened
yesterday" from a summary of the wrong week into an empty answer. Empty is more
honest and no more useful.

So the search widens: nearest day the caller may actually see, at or before the
range they asked about, then the same query again on that day. It happens HERE
rather than in ask-agent because this is the side holding the connection and
the ACL; ask-agent would have to be handed a way to ask "what dates exist",
which is a way to ask about days it cannot read.

What is asserted here is the ROUTING -- when widening runs, when it must not,
and that the scope reported back is the range really used. Whether the SQL is
right is a question only a database can answer, and
tests/integration/test_latest_visible_date.py asks it there.
"""
import pytest

rag = pytest.importorskip("lambda_rag_search", reason="requires psycopg (installed in CI)")


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


CALLER = {
    "id": "u-1", "cognito_sub": "sub-1", "company_id": "c-1",
    "email": "a@x.nz", "first_name": "Ada", "last_name": "L",
    "avatar_s3_key": None, "global_role": "admin", "created_at": "2026-07-04",
}

ROW = {"id": "c-1", "site_id": "s-1", "topic_id": None, "report_date": "2026-08-27",
       "chunk_text": "Concrete pour moved to Thursday.", "chunk_type": "topic",
       "distance": 0.1, "site_name": "Ellesmere", "site_slug": "ellesmere",
       "source_s3_key": "reports/2026-08-27/Ben/r.json", "metadata": {},
       "topic_title": "Programme", "topic_summary": ""}


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(rag, "get_cached_connection", lambda *a, **k: FakeConn())
    monkeypatch.setattr(rag.users, "get_user_by_sub", lambda conn, sub: dict(CALLER))
    monkeypatch.setattr(rag.aliases, "list_active", lambda conn, cid, site_ids=None: [])
    monkeypatch.setattr(rag.scope, "visible_scope",
                        lambda conn, caller: {"site_ids": {"s-1"}, "author_ids": None})
    return monkeypatch


def wire_search(monkeypatch, results):
    """`results` is a list, one entry per expected call. Records the kwargs."""
    calls = []

    def fake_search(conn, qv, site_ids, k=5, author_ids=None, date_from=None, date_to=None):
        calls.append({"date_from": date_from, "date_to": date_to})
        return results[len(calls) - 1] if len(calls) <= len(results) else []

    monkeypatch.setattr(rag.chunks, "search_chunks", fake_search)
    return calls


def wire_latest(monkeypatch, value):
    seen = {}

    def fake_latest(conn, site_ids, *, author_ids=None, on_or_before=None):
        seen["on_or_before"] = on_or_before
        return value

    monkeypatch.setattr(rag.chunks, "latest_visible_date", fake_latest)
    return seen


def event(**kw):
    ev = {"sub": "sub-1", "query_embedding": [0.1] * 1024}
    ev.update(kw)
    return ev


# --------------------------------------------------------------------------

def test_a_dated_search_that_hits_does_not_widen(wired):
    calls = wire_search(wired, [[ROW]])
    wire_latest(wired, "2026-08-01")   # would be wrong to use

    out = rag.lambda_handler(event(date_from="2026-08-29", date_to="2026-08-29",
                                   widen_when_empty=True), None)

    assert len(calls) == 1
    assert out["scope"] == {"from": "2026-08-29", "to": "2026-08-29", "widened": False}


def test_an_empty_dated_search_retries_on_the_nearest_earlier_day(wired):
    calls = wire_search(wired, [[], [ROW]])
    seen = wire_latest(wired, "2026-08-27")

    out = rag.lambda_handler(event(date_from="2026-08-29", date_to="2026-08-29",
                                   widen_when_empty=True), None)

    assert seen["on_or_before"] == "2026-08-29"          # never looks forward
    assert calls[1] == {"date_from": "2026-08-27", "date_to": "2026-08-27"}
    assert out["chunks"] and out["scope"] == {"from": "2026-08-27", "to": "2026-08-27",
                                              "widened": True}


def test_widening_is_opt_in(wired):
    """Search (mode=search) sends dates and wants exactly those dates. Widening
    a filtered LIST would show rows outside the filter the user set."""
    calls = wire_search(wired, [[]])
    wire_latest(wired, "2026-08-27")

    out = rag.lambda_handler(event(date_from="2026-08-29", date_to="2026-08-29"), None)

    assert len(calls) == 1
    assert out["scope"]["widened"] is False


def test_an_undated_search_never_widens(wired):
    """With no range there is nothing to widen FROM; the search already spans
    everything the caller can see."""
    calls = wire_search(wired, [[]])

    def boom(*a, **k):
        raise AssertionError("latest_visible_date must not be consulted without a range")

    wired.setattr(rag.chunks, "latest_visible_date", boom)

    out = rag.lambda_handler(event(widen_when_empty=True), None)

    assert len(calls) == 1
    assert out["scope"] == {"from": None, "to": None, "widened": False}


def test_nothing_to_widen_onto_stays_empty_and_says_so(wired):
    """A caller whose whole corpus is empty gets an empty answer with a scope
    that shows what was tried -- not a scope claiming a day it never read."""
    calls = wire_search(wired, [[]])
    wire_latest(wired, None)

    out = rag.lambda_handler(event(date_from="2026-08-29", date_to="2026-08-29",
                                   widen_when_empty=True), None)

    assert len(calls) == 1
    assert out["chunks"] == []
    assert out["scope"] == {"from": "2026-08-29", "to": "2026-08-29", "widened": False}


def test_every_early_return_carries_a_scope(wired):
    """`result.get("scope")` coming back None must never be how a caller learns
    the search failed -- it reads as 'this deploy predates scope' instead, and
    ask-agent would silently report the range it asked for as the range used.
    """
    for ev, patch in (
        ({"sub": "sub-1", "query_embedding": None}, None),
        (event(), "not_provisioned"),
        (event(), "no_sites"),
    ):
        if patch == "not_provisioned":
            wired.setattr(rag.users, "get_user_by_sub", lambda conn, sub: None)
        if patch == "no_sites":
            wired.setattr(rag.users, "get_user_by_sub", lambda conn, sub: dict(CALLER))
            wired.setattr(rag.scope, "visible_scope",
                          lambda conn, caller: {"site_ids": set(), "author_ids": None})
        out = rag.lambda_handler(ev, None)
        assert "scope" in out, f"missing scope on {patch or 'guard'} return"
