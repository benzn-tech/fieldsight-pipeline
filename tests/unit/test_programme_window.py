"""
Tests for src/repositories/programme_window.py — Task 2 of the programme
time-window plan. Spec §7.

The window is what gets LOADED, not a filter over what was loaded: that is
the design's whole answer to a 30,000-task programme. So the properties that
matter are what the SQL does and does not ask for.

Ancestor expansion is the subtle part. A task inside the window whose parent
sits outside it still needs that parent row, or the tree renders as orphans —
but the parent is context, not content, and must be marked so the client does
not present it as work happening in the window.
"""
from repositories import programme_window as repo

from tests.unit.test_programme_tasks_repo import FakeConn

PROG = "22222222-2222-2222-2222-222222222222"


def test_the_query_filters_on_overlap_not_containment():
    """A task running from before the window to after it is very much in the
    window. Containment would hide exactly the long tasks a PM cares about."""
    conn = FakeConn([[]])
    repo.tasks_in_window(conn, PROG, date_from="2026-04-01", date_to="2026-05-31")
    sql = conn.calls[0]["sql"]
    assert "start_date <= %s" in sql and "end_date   >= %s" in sql, \
        "overlap is start <= window_end AND end >= window_start"


def test_the_query_expands_ancestors_recursively():
    conn = FakeConn([[]])
    repo.tasks_in_window(conn, PROG, date_from="2026-04-01", date_to="2026-05-31")
    assert "RECURSIVE" in conn.calls[0]["sql"].upper(), \
        "a parent outside the window is still needed to render the tree"


def test_ancestors_are_marked_as_context_not_content():
    conn = FakeConn([[{"id": "g", "in_window": False},
                      {"id": "t", "in_window": True}]])
    rows = repo.tasks_in_window(conn, PROG, date_from="2026-04-01",
                                date_to="2026-05-31")
    assert [r["in_window"] for r in rows] == [False, True]


def test_soft_deleted_tasks_are_excluded():
    conn = FakeConn([[]])
    repo.tasks_in_window(conn, PROG, date_from="2026-04-01", date_to="2026-05-31")
    assert "removed_in_version IS NULL" in conn.calls[0]["sql"]


def test_an_assignee_filter_joins_the_assignee_table():
    conn = FakeConn([[]])
    repo.tasks_in_window(conn, PROG, date_from="2026-04-01", date_to="2026-05-31",
                         assignee="Sam_SM")
    sql = conn.calls[0]["sql"]
    assert "programme_task_assignees" in sql
    assert "Sam_SM" in conn.calls[0]["params"]


def test_no_assignee_filter_means_everyone_not_nobody():
    """An absent filter is 'no restriction'. The inverse reading — treating a
    missing value as an empty allow-list — is the over-permission bug this
    codebase has shipped before, and here it would silently render an empty
    programme instead."""
    conn = FakeConn([[]])
    repo.tasks_in_window(conn, PROG, date_from="2026-04-01", date_to="2026-05-31")
    assert "programme_task_assignees" not in conn.calls[0]["sql"]


def test_an_empty_string_assignee_is_not_treated_as_no_filter():
    """`assignee=""` reaching the query unfiltered would hand a caller with
    no folder identity the entire programme."""
    conn = FakeConn([[]])
    repo.tasks_in_window(conn, PROG, date_from="2026-04-01", date_to="2026-05-31",
                         assignee="")
    assert "programme_task_assignees" in conn.calls[0]["sql"]
    assert "" in conn.calls[0]["params"]


def test_the_window_bounds_are_bound_as_parameters():
    conn = FakeConn([[]])
    repo.tasks_in_window(conn, PROG, date_from="2026-04-01", date_to="2026-05-31")
    params = conn.calls[0]["params"]
    assert "2026-04-01" in params and "2026-05-31" in params


def test_the_programme_id_scopes_the_query():
    conn = FakeConn([[]])
    repo.tasks_in_window(conn, PROG, date_from="2026-04-01", date_to="2026-05-31")
    assert PROG in conn.calls[0]["params"]
    assert "programme_id = %s" in conn.calls[0]["sql"]


def test_results_are_ordered_for_stable_rendering():
    conn = FakeConn([[]])
    repo.tasks_in_window(conn, PROG, date_from="2026-04-01", date_to="2026-05-31")
    assert "ORDER BY" in conn.calls[0]["sql"]


def test_a_row_reached_both_ways_counts_as_in_window():
    """A task can be both a match and someone else's ancestor. Being a match
    has to win, or a real task in the window would render greyed as context."""
    conn = FakeConn([[]])
    repo.tasks_in_window(conn, PROG, date_from="2026-04-01", date_to="2026-05-31")
    assert "bool_or" in conn.calls[0]["sql"], \
        "the aggregate must OR the two ways a row can be reached"
