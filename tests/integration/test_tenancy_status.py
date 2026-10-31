"""Integration: tenancy_status reads against real Postgres (migration 0092).

Covers company_status for each state, inactive_company_ids, the missing-column /
missing-table path (the window between `sam deploy` and the migrate step) and that a
failed read never poisons the caller's transaction, and caller_blocked end to end.

Skipped without TEST_DATABASE_URL -- a skip is NOT a pass.
"""
import uuid

import psycopg
import pytest

import tenancy_status
from repositories import companies, users

pytestmark = pytest.mark.integration


def _company(db, status="active"):
    co = companies.create_company(db, f"Co-ts-{uuid.uuid4().hex[:8]}")
    if status != "active":
        db.execute("UPDATE companies SET status=%s WHERE id=%s", (status, co["id"]))
    return co


def _user(db, co, role=None):
    tag = uuid.uuid4().hex[:8]
    return users.upsert_user(db, f"sub-ts-{tag}", f"ts-{tag}@example.com",
                             company_id=co["id"], global_role=role)


def _assert_usable(db):
    assert db.info.transaction_status == psycopg.pq.TransactionStatus.INTRANS
    assert db.execute("SELECT 1").fetchone()[0] == 1


@pytest.mark.parametrize("status", ["active", "suspended", "exited"])
def test_company_status_reads_each_state(db, status):
    co = _company(db, status)
    assert tenancy_status.company_status(db, co["id"]) == status
    _assert_usable(db)


def test_company_status_none_and_unknown_are_active(db):
    _company(db)  # open the request transaction, as get_user_by_sub does in a handler
    assert tenancy_status.company_status(db, None) == "active"
    assert tenancy_status.company_status(db, uuid.uuid4()) == "active"
    _assert_usable(db)


def test_inactive_company_ids(db):
    active = _company(db)
    suspended = _company(db, "suspended")
    exited = _company(db, "exited")
    ids = tenancy_status.inactive_company_ids(db)
    assert isinstance(ids, set)
    assert suspended["id"] in ids
    assert exited["id"] in ids
    assert active["id"] not in ids
    # Same type psycopg hands back for users.company_id, so membership tests work.
    u = _user(db, suspended)
    assert u["company_id"] in ids


def test_missing_column_reads_active_and_keeps_transaction(db):
    co = _company(db, "exited")
    db.execute("ALTER TABLE companies RENAME COLUMN status TO status_gone")
    assert tenancy_status.company_status(db, co["id"]) == "active"
    _assert_usable(db)
    assert tenancy_status.inactive_company_ids(db) == set()
    _assert_usable(db)
    # The request carries on: an ordinary read after both failed reads still works.
    assert db.execute("SELECT name FROM companies WHERE id=%s",
                      (co["id"],)).fetchone()[0] == co["name"]


def test_missing_table_reads_active_and_keeps_transaction(db):
    co = _company(db, "suspended")
    db.execute("ALTER TABLE companies RENAME TO companies_gone")
    assert tenancy_status.company_status(db, co["id"]) == "active"
    _assert_usable(db)
    assert tenancy_status.inactive_company_ids(db) == set()
    _assert_usable(db)


def test_other_errors_propagate_but_do_not_poison(db):
    # A non-schema error (bad uuid text) is not swallowed, yet the savepoint still
    # leaves the outer transaction usable.
    _company(db)  # open the request transaction first: the read must run in a savepoint
    with pytest.raises(psycopg.errors.InvalidTextRepresentation):
        tenancy_status.company_status(db, "not-a-uuid")
    _assert_usable(db)


@pytest.mark.parametrize("flag", ["on", "off"])
def test_caller_blocked_end_to_end(db, monkeypatch, flag):
    monkeypatch.setenv("COMPANY_STATUS_ENFORCE", flag)
    on = flag == "on"
    live = _user(db, _company(db))
    suspended_co = _company(db, "suspended")
    blocked = _user(db, suspended_co)
    exited_user = _user(db, _company(db, "exited"))
    admin = _user(db, suspended_co, role="platform_admin")
    archived = _user(db, _company(db))
    assert users.archive_user(db, archived["cognito_sub"], archived["company_id"])

    def resolve(u):
        return users.get_user_by_sub(db, u["cognito_sub"])

    assert tenancy_status.caller_blocked(db, resolve(live)) is None
    assert tenancy_status.caller_blocked(db, resolve(blocked)) == (
        "company_inactive" if on else None)
    assert tenancy_status.caller_blocked(db, resolve(exited_user)) == (
        "company_inactive" if on else None)
    assert tenancy_status.caller_blocked(db, resolve(admin)) is None
    assert tenancy_status.caller_blocked(db, resolve(archived)) == (
        "account_archived" if on else None)
    _assert_usable(db)
