"""Integration: migration 0092 -- company status + offboarding audit tables.

Checks what 0092 adds against real Postgres: companies.status (default 'active',
CHECK), the status timestamp/reason columns, company_status_events (FK to
companies) and company_status_cognito (CHECKs, FK, unique per event/user/action),
and that the file is safe to run twice.

Skipped without TEST_DATABASE_URL -- a skip is NOT a pass.
"""
import os
import uuid

import psycopg
import pytest

from repositories import companies

pytestmark = pytest.mark.integration

MIGRATION_0092 = os.path.join(
    os.path.dirname(__file__), "..", "..", "src", "migrations",
    "0092_company_status.sql")


def _migration_sql():
    with open(MIGRATION_0092, encoding="utf-8") as fh:
        return fh.read()


def _company(db):
    return companies.create_company(db, f"Co-0092-{uuid.uuid4().hex[:8]}")


def _event(db, co, to_status="suspended"):
    return db.execute(
        "INSERT INTO company_status_events (company_id, from_status, to_status, reason, "
        "actor_sub, detail) VALUES (%s, 'active', %s, 'test', 'sub-x', '{}'::jsonb) "
        "RETURNING id",
        (co["id"], to_status),
    ).fetchone()[0]


def test_existing_company_reads_active_with_null_timestamps(db):
    co = _company(db)
    row = db.execute(
        "SELECT status, status_changed_at, status_reason, billing_stopped_at, exited_at "
        "FROM companies WHERE id=%s", (co["id"],)).fetchone()
    assert row == ("active", None, None, None, None)


def test_status_check_rejects_unknown_value(db):
    co = _company(db)
    with pytest.raises(psycopg.errors.CheckViolation):
        with db.transaction():
            db.execute("UPDATE companies SET status='deleted' WHERE id=%s", (co["id"],))
    for ok in ("suspended", "exited", "active"):
        db.execute("UPDATE companies SET status=%s WHERE id=%s", (ok, co["id"]))
    with pytest.raises(psycopg.errors.NotNullViolation):
        with db.transaction():
            db.execute("UPDATE companies SET status=NULL WHERE id=%s", (co["id"],))


def test_status_events_defaults_and_fk(db):
    co = _company(db)
    eid = _event(db, co)
    at, detail = db.execute(
        "SELECT at, detail FROM company_status_events WHERE id=%s", (eid,)).fetchone()
    assert at is not None
    assert detail == {}
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with db.transaction():
            db.execute(
                "INSERT INTO company_status_events (company_id, to_status) VALUES (%s, 'exited')",
                (uuid.uuid4(),))


def test_cognito_rows_checks_defaults_unique_fk(db):
    co = _company(db)
    eid = _event(db, co)
    insert = ("INSERT INTO company_status_cognito (event_id, user_sub, action) "
              "VALUES (%s, %s, %s)")
    db.execute(insert, (eid, "sub-a", "disable"))
    db.execute(insert, (eid, "sub-a", "signout"))
    state, attempts, last_error, updated_at = db.execute(
        "SELECT state, attempts, last_error, updated_at FROM company_status_cognito "
        "WHERE event_id=%s AND user_sub='sub-a' AND action='disable'", (eid,)).fetchone()
    assert (state, attempts, last_error) == ("pending", 0, None)
    assert updated_at is not None

    with pytest.raises(psycopg.errors.UniqueViolation):
        with db.transaction():
            db.execute(insert, (eid, "sub-a", "disable"))
    with pytest.raises(psycopg.errors.CheckViolation):
        with db.transaction():
            db.execute(insert, (eid, "sub-b", "delete"))
    with pytest.raises(psycopg.errors.CheckViolation):
        with db.transaction():
            db.execute(
                "INSERT INTO company_status_cognito (event_id, user_sub, action, state) "
                "VALUES (%s, 'sub-c', 'enable', 'running')", (eid,))
    for st in ("done", "failed"):
        db.execute(
            "UPDATE company_status_cognito SET state=%s WHERE event_id=%s AND user_sub='sub-a' "
            "AND action='signout'", (st, eid))
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with db.transaction():
            db.execute(insert, (uuid.uuid4(), "sub-d", "enable"))


def test_migration_is_idempotent(db):
    sql = _migration_sql()
    db.execute(sql)
    db.execute(sql)
    co = _company(db)
    assert db.execute("SELECT status FROM companies WHERE id=%s",
                      (co["id"],)).fetchone()[0] == "active"
    n = db.execute(
        "SELECT count(*) FROM pg_constraint WHERE conrelid='companies'::regclass "
        "AND conname='companies_status_check'").fetchone()[0]
    assert n == 1
