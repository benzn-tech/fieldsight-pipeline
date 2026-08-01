"""
Integration tests for the programme time-window query against a real
PostgreSQL — Task 2 of the programme time-window plan. Spec §7.

tests/unit/test_programme_window.py asserts what the SQL SAYS (it mentions
RECURSIVE, it filters on overlap, it binds its parameters). Only a real
database can prove the recursion terminates, that the ancestor walk reaches
the root, and that bool_or resolves a row reached both as a match and as
someone else's ancestor.

Skipped unless TEST_DATABASE_URL is set (tests/conftest.py).
"""
import pytest

from repositories import programme_window

pytestmark = pytest.mark.integration


def _seed(db):
    """A three-level tree spanning the window boundary:

        G1  (no dates)                     <- root header
          M1  (Jan)                        <- ancestor OUTSIDE the window
            L1  (May)                      <- inside the window
            L2  (Jan)                      <- outside the window
          M2  (Apr-Jun, spans the window)  <- inside, by overlap not containment
    """
    cid = db.execute(
        "INSERT INTO companies (name) VALUES ('C') RETURNING id").fetchone()[0]
    sid = db.execute(
        "INSERT INTO sites (company_id, name) VALUES (%s,'S') RETURNING id",
        (cid,)).fetchone()[0]
    pid = db.execute(
        "INSERT INTO programmes (site_id, name) VALUES (%s,'P') RETURNING id",
        (sid,)).fetchone()[0]

    def task(src, parent, start, end):
        return db.execute(
            "INSERT INTO programme_tasks "
            "(programme_id, source_task_id, parent_id, origin, name, "
            " start_date, end_date, first_seen_version) "
            "VALUES (%s,%s,%s,'imported',%s,%s,%s,1) RETURNING id",
            (pid, src, parent, src, start, end)).fetchone()[0]

    g1 = task("G1", None, None, None)
    m1 = task("M1", g1, "2026-01-01", "2026-01-31")
    l1 = task("L1", m1, "2026-05-01", "2026-05-10")
    l2 = task("L2", m1, "2026-01-05", "2026-01-10")
    m2 = task("M2", g1, "2026-04-01", "2026-06-30")
    return {"programme_id": pid, "G1": g1, "M1": m1, "L1": l1, "L2": l2, "M2": m2}


WINDOW = {"date_from": "2026-05-01", "date_to": "2026-05-31"}


def test_the_recursive_ancestor_walk_reaches_the_root(db):
    ids = _seed(db)
    rows = programme_window.tasks_in_window(db, ids["programme_id"], **WINDOW)
    got = {str(r["id"]) for r in rows}
    assert str(ids["L1"]) in got, "the matching task"
    assert str(ids["M1"]) in got, "its parent, outside the window"
    assert str(ids["G1"]) in got, "and the root above that"


def test_ancestors_come_back_as_context_and_matches_as_content(db):
    ids = _seed(db)
    rows = programme_window.tasks_in_window(db, ids["programme_id"], **WINDOW)
    by_id = {str(r["id"]): r for r in rows}
    assert by_id[str(ids["L1"])]["in_window"] is True
    assert by_id[str(ids["M1"])]["in_window"] is False
    assert by_id[str(ids["G1"])]["in_window"] is False


def test_a_task_spanning_the_whole_window_is_in_it(db):
    """Overlap, not containment — this is the long task a PM most wants."""
    ids = _seed(db)
    rows = programme_window.tasks_in_window(db, ids["programme_id"], **WINDOW)
    by_id = {str(r["id"]): r for r in rows}
    assert str(ids["M2"]) in by_id
    assert by_id[str(ids["M2"])]["in_window"] is True


def test_a_task_wholly_outside_the_window_is_not_returned_as_content(db):
    ids = _seed(db)
    rows = programme_window.tasks_in_window(db, ids["programme_id"], **WINDOW)
    by_id = {str(r["id"]): r for r in rows}
    assert str(ids["L2"]) not in by_id, \
        "a January task is neither a match nor an ancestor of one"


def test_a_row_reached_both_ways_counts_as_in_window(db):
    """M2 overlaps the window AND is an ancestor once it has a child in it.
    bool_or must resolve that to true, or a real task would render greyed."""
    ids = _seed(db)
    db.execute(
        "INSERT INTO programme_tasks "
        "(programme_id, source_task_id, parent_id, origin, name, "
        " start_date, end_date, first_seen_version) "
        "VALUES (%s,'M2C',%s,'imported','child','2026-05-02','2026-05-03',1)",
        (ids["programme_id"], ids["M2"]))
    rows = programme_window.tasks_in_window(db, ids["programme_id"], **WINDOW)
    by_id = {str(r["id"]): r for r in rows}
    assert by_id[str(ids["M2"])]["in_window"] is True


def test_soft_removed_tasks_are_excluded_even_as_ancestors(db):
    ids = _seed(db)
    db.execute("UPDATE programme_tasks SET removed_in_version = 2 WHERE id = %s",
               (ids["M1"],))
    rows = programme_window.tasks_in_window(db, ids["programme_id"], **WINDOW)
    got = {str(r["id"]) for r in rows}
    assert str(ids["M1"]) not in got
    assert str(ids["L1"]) in got, \
        "the child still matches; only its removed ancestor drops out"


def test_the_assignee_filter_narrows_to_that_person(db):
    ids = _seed(db)
    db.execute(
        "INSERT INTO programme_task_assignees (task_id, assignee) VALUES (%s,'Sam_SM')",
        (ids["L1"],))
    mine = programme_window.tasks_in_window(
        db, ids["programme_id"], assignee="Sam_SM", **WINDOW)
    assert str(ids["L1"]) in {str(r["id"]) for r in mine}
    assert str(ids["M2"]) not in {str(r["id"]) for r in mine}, \
        "M2 is in the window but assigned to nobody"


def test_an_assignee_with_nothing_assigned_gets_an_empty_result(db):
    """Not the whole programme. The empty-list inversion would show every
    task to someone who owns none of them."""
    ids = _seed(db)
    rows = programme_window.tasks_in_window(
        db, ids["programme_id"], assignee="Nobody_Home", **WINDOW)
    assert rows == []


def test_no_assignee_filter_returns_everyone(db):
    ids = _seed(db)
    db.execute(
        "INSERT INTO programme_task_assignees (task_id, assignee) VALUES (%s,'Sam_SM')",
        (ids["L1"],))
    rows = programme_window.tasks_in_window(db, ids["programme_id"], **WINDOW)
    assert str(ids["M2"]) in {str(r["id"]) for r in rows}


def test_a_cycle_in_parent_id_does_not_hang_the_query(db):
    """parent_id has no cycle constraint, and UNION (not UNION ALL) is what
    stops a cycle from looping forever. A hung query here would be a Lambda
    timeout on every programme read for that site."""
    ids = _seed(db)
    db.execute("UPDATE programme_tasks SET parent_id = %s WHERE id = %s",
               (ids["L1"], ids["G1"]))
    rows = programme_window.tasks_in_window(db, ids["programme_id"], **WINDOW)
    assert rows, "the query must terminate and return something"
