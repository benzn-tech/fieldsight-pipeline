"""
Integration tests for count_local_tasks against a real PostgreSQL.

The unit tests drive the handler through FakeProgrammeStore, which
reimplements this count in Python. That proves the guard's behaviour and
proves nothing about the SQL underneath it — and this query decides whether a
save is refused, so both of its failure directions are bad: counting too much
refuses saves that were safe, counting too little lets a replace discard
allocated work silently.

Skipped unless TEST_DATABASE_URL is set (tests/conftest.py).
"""
import pytest

from repositories import programme_tasks

pytestmark = pytest.mark.integration


def _seed(db):
    cid = db.execute(
        "INSERT INTO companies (name) VALUES ('C') RETURNING id").fetchone()[0]
    sid = db.execute(
        "INSERT INTO sites (company_id, name) VALUES (%s,'S') RETURNING id",
        (cid,)).fetchone()[0]
    pid = db.execute(
        "INSERT INTO programmes (site_id, name) VALUES (%s,'P') RETURNING id",
        (sid,)).fetchone()[0]
    return sid, pid


def _task(db, pid, origin, name, *, source=None, removed=None):
    db.execute(
        "INSERT INTO programme_tasks (programme_id, origin, source_task_id, "
        "name, first_seen_version, removed_in_version) "
        "VALUES (%s,%s,%s,%s,1,%s)",
        (pid, origin, source, name, removed))


def test_a_programme_with_no_tasks_counts_zero(db):
    _, pid = _seed(db)
    assert programme_tasks.count_local_tasks(db, pid) == 0


def test_imported_rows_are_not_local(db):
    """The common case. A guard that fired on an ordinary imported programme
    would make Save refuse for everyone."""
    _, pid = _seed(db)
    _task(db, pid, "imported", "Pour slab", source="A1")
    assert programme_tasks.count_local_tasks(db, pid) == 0


def test_zone_children_are_counted(db):
    _, pid = _seed(db)
    _task(db, pid, "imported", "Pour slab", source="A1")
    _task(db, pid, "local", "Zone L1")
    _task(db, pid, "local", "Zone L2")
    assert programme_tasks.count_local_tasks(db, pid) == 2


def test_a_soft_removed_local_row_is_not_counted(db):
    """A departed row is already gone from the user's point of view.
    Counting it would refuse a save over rows nobody can see."""
    _, pid = _seed(db)
    _task(db, pid, "local", "Zone L1")
    _task(db, pid, "local", "Deleted zone", removed=2)
    assert programme_tasks.count_local_tasks(db, pid) == 1


def test_another_programmes_local_rows_do_not_leak_in(db):
    """The guard must not refuse a save because a DIFFERENT programme has
    local rows — one site's zone split would block every other site."""
    sid, pid = _seed(db)
    other = db.execute(
        "INSERT INTO programmes (site_id, name, is_primary) "
        "VALUES (%s,'Other',false) RETURNING id", (sid,)).fetchone()[0]
    _task(db, other, "local", "Elsewhere")
    assert programme_tasks.count_local_tasks(db, pid) == 0
