"""Integration: repositories.report_templates against a real PostgreSQL.

The unit tests hand every statement to PostgreSQL's own parser. That proves the
grammar and nothing about behaviour -- a WHERE clause that selects the wrong
rows parses perfectly. The rules that actually matter are here:

  * a personal template is invisible to everyone else, gm/admin included
  * a version is appended, never rewritten, and a restore is an append
  * a concurrent save is rejected, not silently merged

Skipped unless TEST_DATABASE_URL is set (tests/conftest.py). A SKIP IS NOT A
PASS. These have run nowhere but CI on this branch: there is no local
PostgreSQL on the author's machine and no embedded build available for Windows.
"""
import os
import uuid

import psycopg
import pytest

from db.migrate import apply_migrations
from repositories import report_templates as rt

pytestmark = pytest.mark.integration

MIGRATIONS_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "src", "migrations")
)

BODY = {"sections": [{"key": "what", "title": "What this was", "purpose": "x"}],
        "catch_all": {"key": "other", "title": "Anything else", "purpose": "y"},
        "excluded_subjects": [], "style": ["Keep it short."]}
BODY2 = dict(BODY, style=["Keep it shorter."])


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


def _user(db, company_id, role="gm"):
    return db.execute(
        "INSERT INTO users (company_id, email, global_role) VALUES (%s, %s, %s) RETURNING id",
        (company_id, f"{uuid.uuid4().hex}@example.test", role)).fetchone()[0]


# ---- visibility -------------------------------------------------------------

def test_THE_test_a_personal_template_is_invisible_to_the_gm(db):
    """The promise personal scope makes. If this ever passes by accident --
    because a filter moved out of SQL and into Python, say -- the Library shows
    one person's private drafts to their manager."""
    c = _company(db)
    worker, gm = _user(db, c, "worker"), _user(db, c, "gm")
    mine = rt.create(db, c, "personal", worker, "mine", "Mine", "", "session", worker)

    assert rt.get_visible(db, c, worker, mine["id"]) is not None
    assert rt.get_visible(db, c, gm, mine["id"]) is None
    assert mine["id"] not in [t["id"] for t in rt.list_visible(db, c, gm)]


def test_an_org_template_is_visible_to_everyone_in_the_company(db):
    c = _company(db)
    gm, worker = _user(db, c, "gm"), _user(db, c, "worker")
    org = rt.create(db, c, "org", None, "daily", "Daily", "", "daily", gm)
    assert rt.get_visible(db, c, worker, org["id"]) is not None


def test_nothing_leaks_across_companies(db):
    c1, c2 = _company(db, "One"), _company(db, "Two")
    u1, u2 = _user(db, c1), _user(db, c2)
    org = rt.create(db, c1, "org", None, "daily", "Daily", "", "daily", u1)
    assert rt.get_visible(db, c2, u2, org["id"]) is None
    assert rt.list_visible(db, c2, u2) == []


def test_an_archived_template_falls_out_of_the_list(db):
    c = _company(db)
    gm = _user(db, c)
    org = rt.create(db, c, "org", None, "daily", "Daily", "", "daily", gm)
    rt.archive(db, org["id"])
    assert rt.list_visible(db, c, gm) == []
    assert rt.get_visible(db, c, gm, org["id"]) is None
    assert rt.get_any(db, org["id"]) is not None, "still readable for provenance"


def test_the_all_tab_is_the_union(db):
    c = _company(db)
    gm = _user(db, c)
    rt.create(db, c, "org", None, "daily", "Daily", "", "daily", gm)
    rt.create(db, c, "personal", gm, "mine", "Mine", "", "session", gm)
    assert len(rt.list_visible(db, c, gm)) == 2
    assert len(rt.list_visible(db, c, gm, "org")) == 1
    assert len(rt.list_visible(db, c, gm, "personal")) == 1


# ---- versions ---------------------------------------------------------------

def test_the_first_version_is_one_and_a_bodyless_template_is_zero(db):
    c = _company(db)
    gm = _user(db, c)
    t = rt.create(db, c, "org", None, "daily", "Daily", "", "daily", gm)
    assert t["current_version"] == 0
    assert rt.get_version(db, t["id"], None) is None, "'no body yet' is not an empty body"
    v = rt.add_version(db, t["id"], BODY, "first", gm)
    assert v["version"] == 1
    assert rt.get_any(db, t["id"])["current_version"] == 1


def test_a_version_is_never_rewritten(db):
    c = _company(db)
    gm = _user(db, c)
    t = rt.create(db, c, "org", None, "daily", "Daily", "", "daily", gm)
    rt.add_version(db, t["id"], BODY, "first", gm)
    rt.add_version(db, t["id"], BODY2, "second", gm)
    versions = rt.list_versions(db, t["id"])
    assert [v["version"] for v in versions] == [2, 1]
    assert versions[1]["body"] == BODY, "v1 must still say what it said"


def test_a_restore_moves_forward_not_backward(db):
    """Restoring v1 writes v3. A report recording (template, v2) must keep
    naming a body that still exists exactly as it was."""
    c = _company(db)
    gm = _user(db, c)
    t = rt.create(db, c, "org", None, "daily", "Daily", "", "daily", gm)
    rt.add_version(db, t["id"], BODY, "first", gm)
    rt.add_version(db, t["id"], BODY2, "second", gm)
    old = rt.get_version(db, t["id"], 1)
    restored = rt.add_version(db, t["id"], old["body"], "Restored from v1", gm)
    assert restored["version"] == 3
    assert rt.get_version(db, t["id"], 2)["body"] == BODY2, "v2 is untouched"
    assert rt.get_version(db, t["id"], None)["version"] == 3


def test_a_concurrent_save_is_rejected_not_merged(db):
    """Both writers compute the same next number; the index rejects one. That
    is the intended outcome -- the alternative is a silent lost update -- and
    the route turns it into a 409."""
    c = _company(db)
    gm = _user(db, c)
    t = rt.create(db, c, "org", None, "daily", "Daily", "", "daily", gm)
    rt.add_version(db, t["id"], BODY, "first", gm)
    db.execute("INSERT INTO report_template_versions "
               "(template_id, version, body, change_note, created_by) "
               "VALUES (%s, 2, %s, 'theirs', %s)", (t["id"], psycopg.types.json.Json(BODY2), gm))
    with pytest.raises(psycopg.errors.UniqueViolation):
        db.execute("INSERT INTO report_template_versions "
                   "(template_id, version, body, change_note, created_by) "
                   "VALUES (%s, 2, %s, 'mine', %s)",
                   (t["id"], psycopg.types.json.Json(BODY), gm))


def test_update_meta_never_touches_the_body(db):
    c = _company(db)
    gm = _user(db, c)
    t = rt.create(db, c, "org", None, "daily", "Daily", "", "daily", gm)
    rt.add_version(db, t["id"], BODY, "first", gm)
    rt.update_meta(db, t["id"], name="Renamed", description="New words")
    assert rt.get_any(db, t["id"])["name"] == "Renamed"
    assert rt.get_version(db, t["id"], None)["body"] == BODY
    assert rt.get_any(db, t["id"])["current_version"] == 1, "renaming is not an edit"


# ---- copying ----------------------------------------------------------------

def test_a_copy_is_a_copy_not_a_reference(db):
    c = _company(db)
    gm, worker = _user(db, c, "gm"), _user(db, c, "worker")
    org = rt.create(db, c, "org", None, "daily", "Daily", "", "daily", gm)
    rt.add_version(db, org["id"], BODY, "first", gm)

    mine = rt.copy_to_personal(db, org["id"], worker, "my-daily", "My Daily", worker)
    assert mine["scope"] == "personal" and mine["owner_user_id"] == worker
    assert rt.get_version(db, mine["id"], None)["body"] == BODY

    rt.add_version(db, org["id"], BODY2, "company changed its mind", gm)
    assert rt.get_version(db, mine["id"], None)["body"] == BODY, "the copy must not follow"


def test_copying_a_template_with_no_body_yet_returns_nothing(db):
    c = _company(db)
    gm = _user(db, c)
    org = rt.create(db, c, "org", None, "daily", "Daily", "", "daily", gm)
    assert rt.copy_to_personal(db, org["id"], gm, "mine", "Mine", gm) is None
    assert len(rt.list_visible(db, c, gm, "personal")) == 0, "no empty stub left behind"


# ---- bindings ---------------------------------------------------------------

def test_a_binding_is_replaced_not_duplicated(db):
    c = _company(db)
    gm = _user(db, c)
    a = rt.create(db, c, "org", None, "a", "A", "", "daily", gm)
    b = rt.create(db, c, "org", None, "b", "B", "", "daily", gm)
    rt.set_binding(db, c, "daily", a["id"], None, gm)
    rt.set_binding(db, c, "daily", b["id"], 2, gm)
    rows = rt.list_bindings(db, c)
    assert len(rows) == 1
    assert rows[0]["template_id"] == b["id"] and rows[0]["pinned_version"] == 2


def test_is_bound_sees_the_binding_before_an_archive_can_hide_it(db):
    """The FK is ON DELETE RESTRICT, but archiving is an UPDATE and no
    constraint can catch it. This is the check that has to."""
    c = _company(db)
    gm = _user(db, c)
    t = rt.create(db, c, "org", None, "daily", "Daily", "", "daily", gm)
    assert rt.is_bound(db, t["id"]) is False
    rt.set_binding(db, c, "daily", t["id"], None, gm)
    assert rt.is_bound(db, t["id"]) is True


def test_everyone_may_read_which_template_the_schedule_uses(db):
    """Only gm/admin may set it -- but a worker is entitled to know what format
    their nightly report is written to."""
    c = _company(db)
    gm, worker = _user(db, c, "gm"), _user(db, c, "worker")
    t = rt.create(db, c, "org", None, "daily", "Daily", "", "daily", gm)
    rt.set_binding(db, c, "daily", t["id"], None, gm)
    assert len(rt.list_bindings(db, c)) == 1
    del worker  # reading is not scoped to the caller; the company is the scope
