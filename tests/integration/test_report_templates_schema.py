"""Integration: migration 0062's constraints actually bite, against real PostgreSQL.

The unit test reads the SQL as text and parses it. That proves the syntax and
nothing about behaviour: a CHECK with the comparison on the wrong side, a unique
index whose WHERE clause never matches, an ON DELETE that cascades where it
should restrict -- all of those parse perfectly and all of them are the bug.
This file is where the rules are exercised.

Skipped unless TEST_DATABASE_URL is set (tests/conftest.py). A SKIP IS NOT A
PASS: CI provides the database. As of this branch these have run nowhere but
CI -- there is no local PostgreSQL on the author's machine and no embedded one
available for Windows, so do not read a green local run as covering this file.
"""
import os
import uuid

import psycopg
import pytest

from db.migrate import apply_migrations

pytestmark = pytest.mark.integration

MIGRATIONS_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "src", "migrations")
)

BODY = '{"sections": [{"key": "what", "title": "What this was", "purpose": "x"}],' \
       ' "catch_all": {"key": "other", "title": "Anything else", "purpose": "y"},' \
       ' "excluded_subjects": [], "style": ["short"]}'


@pytest.fixture()
def db():
    conn = psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True)
    conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    apply_migrations(conn, MIGRATIONS_DIR)
    try:
        yield conn
    finally:
        conn.close()


def _company(db, name="C"):
    return db.execute("INSERT INTO companies (name) VALUES (%s) RETURNING id",
                      (name,)).fetchone()[0]


def _user(db, company_id, email=None):
    return db.execute(
        "INSERT INTO users (company_id, email, global_role) VALUES (%s, %s, 'gm') RETURNING id",
        (company_id, email or f"{uuid.uuid4().hex}@example.test")).fetchone()[0]


def _template(db, company_id, user_id, scope="org", slug="daily", owner=None):
    return db.execute(
        "INSERT INTO report_templates "
        "(company_id, scope, owner_user_id, slug, name, report_type, created_by) "
        "VALUES (%s, %s, %s, %s, 'N', 'daily', %s) RETURNING id",
        (company_id, scope, owner, slug, user_id)).fetchone()[0]


# ---- scope and owner may not disagree ---------------------------------------

def test_an_org_template_owned_by_a_person_is_refused(db):
    c = _company(db)
    u = _user(db, c)
    with pytest.raises(psycopg.errors.CheckViolation):
        _template(db, c, u, scope="org", owner=u)


def test_a_personal_template_owned_by_nobody_is_refused(db):
    c = _company(db)
    u = _user(db, c)
    with pytest.raises(psycopg.errors.CheckViolation):
        _template(db, c, u, scope="personal", owner=None)


def test_both_coherent_combinations_are_accepted(db):
    c = _company(db)
    u = _user(db, c)
    assert _template(db, c, u, scope="org", slug="a", owner=None)
    assert _template(db, c, u, scope="personal", slug="b", owner=u)


# ---- the slug rules ---------------------------------------------------------

def test_two_live_org_templates_cannot_share_a_slug(db):
    """NULL owner on both -- without the coalesce in the index, NULLs do not
    compare equal and this would be allowed."""
    c = _company(db)
    u = _user(db, c)
    _template(db, c, u, slug="daily")
    with pytest.raises(psycopg.errors.UniqueViolation):
        _template(db, c, u, slug="daily")


def test_archiving_frees_the_slug(db):
    c = _company(db)
    u = _user(db, c)
    first = _template(db, c, u, slug="daily")
    db.execute("UPDATE report_templates SET archived_at = now() WHERE id = %s", (first,))
    assert _template(db, c, u, slug="daily"), "the name must be reusable once archived"


def test_two_people_may_each_have_their_own_daily(db):
    c = _company(db)
    a, b = _user(db, c), _user(db, c)
    _template(db, c, a, scope="personal", slug="daily", owner=a)
    assert _template(db, c, b, scope="personal", slug="daily", owner=b)


def test_two_companies_may_each_have_their_own_daily(db):
    c1, c2 = _company(db, "One"), _company(db, "Two")
    u1, u2 = _user(db, c1), _user(db, c2)
    _template(db, c1, u1, slug="daily")
    assert _template(db, c2, u2, slug="daily")


# ---- versions are append-only ----------------------------------------------

def test_a_version_number_cannot_repeat(db):
    c = _company(db)
    u = _user(db, c)
    t = _template(db, c, u)
    db.execute("INSERT INTO report_template_versions (template_id, version, body, created_by) "
               "VALUES (%s, 1, %s, %s)", (t, BODY, u))
    with pytest.raises(psycopg.errors.UniqueViolation):
        db.execute("INSERT INTO report_template_versions (template_id, version, body, created_by) "
                   "VALUES (%s, 1, %s, %s)", (t, BODY, u))


def test_version_zero_is_refused(db):
    """0 is the 'no body yet' marker on report_templates.current_version; it is
    not a version anything may be written to."""
    c = _company(db)
    u = _user(db, c)
    t = _template(db, c, u)
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("INSERT INTO report_template_versions (template_id, version, body, created_by) "
                   "VALUES (%s, 0, %s, %s)", (t, BODY, u))


def test_the_body_is_queryable_json_not_an_opaque_blob(db):
    """jsonb, so the section keys a template names can be asked about in SQL."""
    c = _company(db)
    u = _user(db, c)
    t = _template(db, c, u)
    db.execute("INSERT INTO report_template_versions (template_id, version, body, created_by) "
               "VALUES (%s, 1, %s, %s)", (t, BODY, u))
    row = db.execute(
        "SELECT body->'sections'->0->>'key' FROM report_template_versions "
        "WHERE template_id = %s", (t,)).fetchone()
    assert row[0] == "what"


def test_archiving_a_template_keeps_its_versions(db):
    """A deleted template is still the template that wrote last month's reports."""
    c = _company(db)
    u = _user(db, c)
    t = _template(db, c, u)
    db.execute("INSERT INTO report_template_versions (template_id, version, body, created_by) "
               "VALUES (%s, 1, %s, %s)", (t, BODY, u))
    db.execute("UPDATE report_templates SET archived_at = now() WHERE id = %s", (t,))
    n = db.execute("SELECT count(*) FROM report_template_versions WHERE template_id = %s",
                   (t,)).fetchone()[0]
    assert n == 1


# ---- bindings ---------------------------------------------------------------

def test_one_binding_per_company_and_report_type(db):
    c = _company(db)
    u = _user(db, c)
    t1 = _template(db, c, u, slug="a")
    t2 = _template(db, c, u, slug="b")
    db.execute("INSERT INTO report_template_bindings "
               "(company_id, report_type, template_id, set_by) VALUES (%s,'daily',%s,%s)",
               (c, t1, u))
    with pytest.raises(psycopg.errors.UniqueViolation):
        db.execute("INSERT INTO report_template_bindings "
                   "(company_id, report_type, template_id, set_by) VALUES (%s,'daily',%s,%s)",
                   (c, t2, u))


def test_a_bound_template_cannot_be_deleted_out_from_under_the_schedule(db):
    """THE test for the binding table. If this cascaded, deleting a template
    would silently drop the company back to the default format for its nightly
    reports -- the exact silent fallback this design refuses."""
    c = _company(db)
    u = _user(db, c)
    t = _template(db, c, u)
    db.execute("INSERT INTO report_template_bindings "
               "(company_id, report_type, template_id, set_by) VALUES (%s,'daily',%s,%s)",
               (c, t, u))
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        db.execute("DELETE FROM report_templates WHERE id = %s", (t,))


def test_the_schedule_kinds_are_the_only_bindable_ones(db):
    """session/day reports are on-demand: nobody schedules them, so binding one
    would be state that never gets read."""
    c = _company(db)
    u = _user(db, c)
    t = _template(db, c, u)
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("INSERT INTO report_template_bindings "
                   "(company_id, report_type, template_id, set_by) VALUES (%s,'session',%s,%s)",
                   (c, t, u))


def test_applying_0062_twice_changes_nothing(db):
    """Every statement is IF NOT EXISTS; the runner records versions, but a
    hand-reapply during an incident must not be destructive either."""
    assert apply_migrations(db, MIGRATIONS_DIR) == []
