"""
Integration tests for get_task_by_doc_id against a real PostgreSQL.

The unit-level fake in tests/unit/test_lambda_org_api.py mirrors the same rule
in Python, and by construction cannot fail the two things that are actually
risky in the SQL:

  - `id::text = %s` — comparing a uuid column to arbitrary text
  - `DESC NULLS LAST` — a local row's source_task_id is NULL, so the ordering
    expression is NULL for it, and Postgres sorts NULLs FIRST under a plain
    DESC. Without the clause the local row beats an exact imported match.

The tie-break case is contrived (an Activity ID that happens to be another
row's UUID) but it is the only way to exercise the ordering, and nothing stops
a planner from using any string as an Activity ID.

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

    local_id = db.execute(
        "INSERT INTO programme_tasks (programme_id, origin, name, "
        "first_seen_version) VALUES (%s,'local','Rebar fixing',1) RETURNING id",
        (pid,)).fetchone()[0]
    db.execute(
        "INSERT INTO programme_tasks (programme_id, origin, source_task_id, "
        "name, first_seen_version) VALUES (%s,'imported','A1020','Pour slab',1)",
        (pid,))
    db.execute(
        "INSERT INTO programme_tasks (programme_id, origin, source_task_id, "
        "name, first_seen_version, removed_in_version) "
        "VALUES (%s,'imported','A9999','Departed',1,2)",
        (pid,))
    return pid, str(local_id)


def test_an_imported_row_resolves_by_its_activity_id(db):
    pid, _ = _seed(db)
    got = programme_tasks.get_task_by_doc_id(db, pid, "A1020")
    assert got["name"] == "Pour slab"


def test_a_local_row_resolves_by_its_uuid_text(db):
    """Local rows have no source_task_id, so the snapshot gives them their
    UUID as a document id and a suggestion carries that string back. A
    resolver that only checked source_task_id would fail silently on exactly
    the AI-generated breakdown subtasks Project 3 creates."""
    pid, local_id = _seed(db)
    got = programme_tasks.get_task_by_doc_id(db, pid, local_id)
    assert got["name"] == "Rebar fixing"


def test_a_soft_removed_row_never_resolves(db):
    """Departed rows are kept (allocations and progress hang off them) but a
    suggestion must not be applied to one."""
    pid, _ = _seed(db)
    assert programme_tasks.get_task_by_doc_id(db, pid, "A9999") is None


def test_an_unknown_document_id_resolves_to_nothing(db):
    pid, _ = _seed(db)
    assert programme_tasks.get_task_by_doc_id(db, pid, "no-such-task") is None


def test_the_lookup_is_scoped_to_one_programme(db):
    """Two sites can both have a task called A1020."""
    pid, _ = _seed(db)
    other_pid, _ = _seed(db)
    got = programme_tasks.get_task_by_doc_id(db, other_pid, "A1020")
    assert got is not None
    assert str(got["programme_id"]) == str(other_pid)


def test_an_exact_activity_id_match_beats_a_uuid_text_match(db):
    """The NULLS LAST case. Both rows match the WHERE clause; the imported one
    must win, because a suggestion is far more likely to carry the file's
    identifier than a UUID that collides with it."""
    pid, local_id = _seed(db)
    db.execute(
        "INSERT INTO programme_tasks (programme_id, origin, source_task_id, "
        "name, first_seen_version) VALUES (%s,'imported',%s,'Decoy',1)",
        (pid, local_id))

    got = programme_tasks.get_task_by_doc_id(db, pid, local_id)
    assert got["name"] == "Decoy", (
        "the local row won the tie-break — check that the ORDER BY still says "
        "NULLS LAST; under a plain DESC, Postgres sorts the local row's NULL "
        "comparison ahead of the imported row's exact match")
