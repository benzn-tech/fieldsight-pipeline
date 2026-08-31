"""rag-search must not leave an Aurora connection open across invokes.

The module-level connection cache was a deliberate latency fix: reconnecting
costs ~1-2s and dominated search. But Aurora counts any open user-initiated
connection as activity, and a frozen Lambda container's socket stays
established, so one search would keep the cluster from ever starting its
auto-pause countdown — for however long that container survives, which nothing
observes or controls.

The handler has many return paths and can also raise. This pins that every one
of them releases the connection, because the failure is invisible: search still
works perfectly, the bill just never goes down.

It said "four" until the metric route added more, which is the reason this file
enumerates rather than trusting the `finally`: the enumeration is what goes stale
when someone adds a return, and a stale enumeration is what makes the next
addition look covered.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import lambda_rag_search as rs  # noqa: E402


@pytest.fixture
def released(monkeypatch):
    calls = []
    monkeypatch.setattr(rs, "close_cached_connection", lambda: calls.append(1))
    return calls


def test_released_on_the_early_missing_args_return(released, monkeypatch):
    monkeypatch.setattr(rs, "get_cached_connection",
                        lambda: pytest.fail("must not connect without args"))
    rs.lambda_handler({}, None)
    assert released == [1]


def test_released_when_caller_is_not_provisioned(released, monkeypatch):
    monkeypatch.setattr(rs, "get_cached_connection", lambda: object())
    monkeypatch.setattr(rs.users, "get_user_by_sub", lambda conn, sub: None)
    out = rs.lambda_handler({"sub": "s", "query_embedding": [0.1]}, None)
    assert out["error"] == "caller not provisioned"
    assert released == [1]


def test_released_even_when_the_handler_raises(released, monkeypatch):
    monkeypatch.setattr(rs, "get_cached_connection", lambda: object())

    def _boom(conn, sub):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(rs.users, "get_user_by_sub", _boom)
    with pytest.raises(RuntimeError):
        rs.lambda_handler({"sub": "s", "query_embedding": [0.1]}, None)
    assert released == [1], "a raising handler must still give the connection back"


def test_close_is_idempotent_and_never_raises():
    """It runs in a `finally`: an exception there would replace the handler's
    real result — or its real error — with this one."""
    from db import connection

    connection._cached_conn = None
    connection.close_cached_connection()          # no connection at all

    class _Bad:
        def close(self):
            raise RuntimeError("refused")

    connection._cached_conn = _Bad()
    connection.close_cached_connection()          # must swallow
    assert connection._cached_conn is None, "a failed close must still clear the cache"


# ---- the metric route's return paths --------------------------------------

def test_released_when_the_metric_is_refused_before_connecting(released, monkeypatch):
    """`unknown metric` returns BEFORE `get_cached_connection`, so this asserts
    the release happens even on a path that never took a connection -- close is
    idempotent and the `finally` runs regardless."""
    monkeypatch.setattr(rs, "get_cached_connection",
                        lambda: pytest.fail("must not connect for an unknown metric"))
    out = rs.lambda_handler({"mode": "metric", "metric": "astrology", "sub": "s",
                             "date_from": "2026-08-30"}, None)
    assert out["error"] == "unknown metric"
    assert released == [1]


def test_released_on_a_successful_metric_answer(released, monkeypatch):
    monkeypatch.setattr(rs, "get_cached_connection", lambda: object())
    monkeypatch.setattr(rs.users, "get_user_by_sub",
                        lambda conn, sub: {"id": "u", "company_id": "c",
                                           "global_role": "pm"})
    monkeypatch.setattr(rs.scope, "visible_scope",
                        lambda conn, caller: {"site_ids": {"s-1"}, "author_ids": None})
    monkeypatch.setattr(rs.redactions, "deleted_session_bases", lambda *a, **k: set())
    monkeypatch.setattr(rs.recordings, "range_stats",
                        lambda *a, **k: {"sessions": 2, "duration_s": 60,
                                         "unmeasured": 0, "unattributed": 0, "photos": 0})
    out = rs.lambda_handler({"mode": "metric", "metric": "count_sessions", "sub": "s",
                             "date_from": "2026-08-30", "date_to": "2026-08-30"}, None)
    assert out["value"] == 2
    assert released == [1]


def test_released_when_a_metric_query_raises(released, monkeypatch):
    monkeypatch.setattr(rs, "get_cached_connection", lambda: object())
    monkeypatch.setattr(rs.users, "get_user_by_sub",
                        lambda conn, sub: {"id": "u", "company_id": "c",
                                           "global_role": "pm"})
    monkeypatch.setattr(rs.scope, "visible_scope",
                        lambda conn, caller: {"site_ids": {"s-1"}, "author_ids": None})
    monkeypatch.setattr(rs.redactions, "deleted_session_bases", lambda *a, **k: set())

    def _boom(*a, **k):
        raise RuntimeError("query exploded")

    monkeypatch.setattr(rs.recordings, "range_stats", _boom)
    with pytest.raises(RuntimeError):
        rs.lambda_handler({"mode": "metric", "metric": "count_sessions", "sub": "s",
                           "date_from": "2026-08-30"}, None)
    assert released == [1]


def test_released_when_the_caller_can_see_no_sites(released, monkeypatch):
    monkeypatch.setattr(rs, "get_cached_connection", lambda: object())
    monkeypatch.setattr(rs.users, "get_user_by_sub",
                        lambda conn, sub: {"id": "u", "company_id": "c",
                                           "global_role": "worker"})
    monkeypatch.setattr(rs.scope, "visible_scope",
                        lambda conn, caller: {"site_ids": set(), "author_ids": {"u"}})
    out = rs.lambda_handler({"mode": "metric", "metric": "count_sessions", "sub": "s",
                             "date_from": "2026-08-30"}, None)
    assert out["notes"]["zero_kind"] == "nothing_visible"
    assert released == [1]
