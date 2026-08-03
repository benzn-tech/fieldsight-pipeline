"""
Integration tests for list_for_site's author filter against a real PostgreSQL.

tests/unit/test_programme_suggestions_repo.py asserts the PARAMS the repo
binds, which by construction cannot fail the thing that is actually risky
here: `%s::uuid IS NULL OR topic_user_id = %s` with a NULL bound parameter.
Get that wrong in either direction and it is an access-control bug — everyone
sees everything, or nobody sees anything — with no exception raised either
way.

Skipped unless TEST_DATABASE_URL is set (tests/conftest.py).
"""
import pytest

from repositories import programme_suggestions as repo

pytestmark = pytest.mark.integration


def _seed(db):
    cid = db.execute(
        "INSERT INTO companies (name) VALUES ('C') RETURNING id").fetchone()[0]
    sid = db.execute(
        "INSERT INTO sites (company_id, name) VALUES (%s,'S') RETURNING id",
        (cid,)).fetchone()[0]

    def user(name):
        return db.execute(
            "INSERT INTO users (company_id, cognito_sub, email, first_name) "
            "VALUES (%s,%s,%s,%s) RETURNING id",
            (cid, f"sub-{name}", f"{name}@example.com", name)).fetchone()[0]

    ben, sam = user("ben"), user("sam")

    def sugg(task_id, topic_user_id):
        db.execute(
            "INSERT INTO programme_progress_suggestions "
            "(site_id, task_id, topic_title, topic_user_id, report_date, "
            " source_s3_key, task_name, suggested_progress, confidence, dedupe_key) "
            "VALUES (%s,%s,'t',%s,'2026-04-05',%s,'n',50,0.9,%s)",
            (sid, task_id, topic_user_id, f"k/{task_id}", f"d-{task_id}"))

    sugg("A1", ben)
    sugg("A2", ben)
    sugg("B1", sam)
    # The source topic was deleted; topic_id/topic_user_id go NULL.
    sugg("C1", None)
    return sid, ben, sam


def _ids(rows):
    return sorted(r["task_id"] for r in rows)


def test_no_author_restriction_returns_the_whole_site(db):
    """None means "do not filter" — the manager view."""
    sid, _, _ = _seed(db)
    rows = repo.list_for_site(db, sid, topic_user_id=None)
    assert _ids(rows) == ["A1", "A2", "B1", "C1"]


def test_an_author_restriction_returns_only_that_person(db):
    sid, ben, _ = _seed(db)
    assert _ids(repo.list_for_site(db, sid, topic_user_id=ben)) == ["A1", "A2"]


def test_two_people_do_not_see_each_other(db):
    """The assertion that makes this an ACL test rather than a filter test."""
    sid, ben, sam = _seed(db)
    assert _ids(repo.list_for_site(db, sid, topic_user_id=sam)) == ["B1"]
    assert "B1" not in _ids(repo.list_for_site(db, sid, topic_user_id=ben))


def test_a_suggestion_whose_topic_was_deleted_stays_out_of_personal_views(db):
    """topic_user_id goes NULL when the source topic is superseded
    (ON DELETE SET NULL). A NULL author must not match anybody — `= NULL` is
    never true, which is the behaviour wanted here, but it is worth pinning
    because the alternative reading (an unattributed row belongs to everyone)
    would leak it to every user on the site."""
    sid, ben, sam = _seed(db)
    assert "C1" not in _ids(repo.list_for_site(db, sid, topic_user_id=ben))
    assert "C1" not in _ids(repo.list_for_site(db, sid, topic_user_id=sam))
    # Still reachable by a manager, who is the one who can act on it.
    assert "C1" in _ids(repo.list_for_site(db, sid, topic_user_id=None))


def test_the_author_filter_composes_with_the_state_filter(db):
    sid, ben, _ = _seed(db)
    db.execute("UPDATE programme_progress_suggestions SET state='confirmed' "
               "WHERE task_id='A1'")
    assert _ids(repo.list_for_site(db, sid, topic_user_id=ben)) == ["A2"]
    assert _ids(repo.list_for_site(db, sid, state=None,
                                   topic_user_id=ben)) == ["A1", "A2"]
