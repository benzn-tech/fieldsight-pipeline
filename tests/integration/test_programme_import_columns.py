"""Integration: the column names `apply_plan` interpolates must be real columns.

`programme_import.apply_plan` builds its UPDATE by interpolating dictionary KEYS as column
names:

    sets = [f"{c} = %s" for c in fields]

Those keys come from `programme_reconcile`, which takes them from `_IMPORT_OWNED` — a tuple
in one module naming columns declared in a migration in another. Nothing connects the two.
Rename a column, or add a name to that tuple that is not one, and a live import path raises
`psycopg.errors.UndefinedColumn` — on a real import, in front of a customer, and nowhere
else. `programme_import.py` has no integration coverage at all today.

Not a hypothetical class: two queries in `topics.py` referenced an alias that did not exist
in their own statement and raised on every call, unnoticed by a green unit suite, because
its connection doubles record SQL strings and never parse them.

The names are a fixed module constant rather than user input, so this is not injection —
it is the crash. The identifier check is here anyway, because the day someone routes a
field name in from a payload, the interpolation above stops being safe and nothing else
would say so.
"""
import pytest

import programme_reconcile
from repositories import programme_import  # noqa: F401  (the module under discussion)

pytestmark = pytest.mark.integration

# The two sources of keys `apply_plan` can be handed: the owned columns, plus the revival
# field reconcile sets by name when a task returns after being removed.
CANDIDATE_COLUMNS = set(programme_reconcile._IMPORT_OWNED) | {"removed_in_version"}


def _columns(db, table):
    return {r[0] for r in db.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name=%s",
        (table,)).fetchall()}


def test_every_importable_field_is_a_real_column(db):
    """The coupling this pins is invisible: a tuple in `programme_reconcile` and a CREATE
    TABLE in a migration, with nothing between them."""
    actual = _columns(db, "programme_tasks")
    missing = sorted(CANDIDATE_COLUMNS - actual)
    assert not missing, (
        f"programme_reconcile can emit these as UPDATE column names and "
        f"programme_tasks has no such column: {missing} — a real import would raise "
        f"UndefinedColumn")


def test_the_interpolated_names_are_plain_identifiers(db):
    """`apply_plan` interpolates these into SQL rather than binding them, which is only
    safe while they stay a fixed constant. If one ever arrives from a payload, this is the
    assertion that should have been failing already."""
    for c in CANDIDATE_COLUMNS:
        assert c.replace("_", "").isalnum(), f"not a plain identifier: {c!r}"


def test_the_generated_update_actually_runs(db):
    """Names being real is necessary and not sufficient — the statement `apply_plan`
    assembles has to parse and execute as a whole, with the three trailing SET clauses it
    appends after the dynamic ones."""
    cid = db.execute(
        "INSERT INTO companies (name) VALUES ('Imp-Co') RETURNING id").fetchone()[0]
    sid = db.execute(
        "INSERT INTO sites (company_id, name) VALUES (%s,'Imp-Site') RETURNING id",
        (cid,)).fetchone()[0]
    pid = db.execute(
        "INSERT INTO programmes (site_id, name) VALUES (%s,'Imp-P') RETURNING id",
        (sid,)).fetchone()[0]
    tid = db.execute(
        "INSERT INTO programme_tasks (programme_id, origin, source_task_id, name, "
        "first_seen_version) VALUES (%s,'imported','T-1','Old name',1) RETURNING id",
        (pid,)).fetchone()[0]

    plan = {"insert": [], "remove": [], "update": [{
        "id": tid,
        "fields": {"name": "New name", "wbs_code": "1.2", "start_date": "2026-08-17",
                   "end_date": "2026-08-18", "duration_days": 2,
                   "removed_in_version": None},
    }]}

    out = programme_import.apply_plan(db, pid, plan, version_no=2, updated_by="tester")

    assert out["updated"] == 1
    row = db.execute(
        "SELECT name, wbs_code, duration_days, locally_modified, row_version "
        "FROM programme_tasks WHERE id=%s", (tid,)).fetchone()
    assert row[0] == "New name" and row[1] == "1.2" and row[2] == 2
    assert row[3] is False, "the import is the authority for these columns"
    assert row[4] == 2, "row_version must advance so a concurrent save is detected"


def test_an_update_with_no_fields_writes_nothing(db):
    """`if not fields: continue` — without it the SET list is empty and the statement is a
    syntax error, and an import that changed nothing would take the whole request down."""
    cid = db.execute(
        "INSERT INTO companies (name) VALUES ('Imp-Co2') RETURNING id").fetchone()[0]
    sid = db.execute(
        "INSERT INTO sites (company_id, name) VALUES (%s,'S2') RETURNING id",
        (cid,)).fetchone()[0]
    pid = db.execute(
        "INSERT INTO programmes (site_id, name) VALUES (%s,'P2') RETURNING id",
        (sid,)).fetchone()[0]
    tid = db.execute(
        "INSERT INTO programme_tasks (programme_id, origin, name, first_seen_version) "
        "VALUES (%s,'imported','Untouched',1) RETURNING id", (pid,)).fetchone()[0]

    out = programme_import.apply_plan(
        db, pid, {"insert": [], "remove": [], "update": [{"id": tid, "fields": {}}]},
        version_no=2, updated_by="tester")

    assert out["updated"] == 0
    assert db.execute(
        "SELECT row_version FROM programme_tasks WHERE id=%s", (tid,)).fetchone()[0] == 1
