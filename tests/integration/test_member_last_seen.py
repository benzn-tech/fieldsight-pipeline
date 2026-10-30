"""Integration: migration 0091 users.last_seen_at, the /me stamp throttle, and the member
list carrying it. Real Postgres -- a fake connection never executes the SQL.

Skipped without TEST_DATABASE_URL -- a skip is NOT a pass.
"""
import os
import uuid

import pytest

from repositories import companies, users

pytestmark = pytest.mark.integration

MIGRATION_0091 = os.path.join(os.path.dirname(__file__), "..", "..", "src", "migrations",
                              "0091_users_last_seen_at.sql")


def _seed(db):
    tag = uuid.uuid4().hex[:8]
    co = companies.create_company(db, f"Co-0091-{tag}")
    u = users.upsert_user(db, f"sub-0091-{tag}", f"u-{tag}@example.com", company_id=co["id"])
    return co, u


def _last_seen(db, sub):
    return db.execute("SELECT last_seen_at FROM users WHERE cognito_sub=%s", (sub,)).fetchone()[0]


def test_migration_adds_a_nullable_timestamptz_and_is_idempotent(db):
    row = db.execute(
        "SELECT data_type, is_nullable FROM information_schema.columns "
        "WHERE table_name='users' AND column_name='last_seen_at'").fetchone()
    assert row == ("timestamp with time zone", "YES")
    with open(MIGRATION_0091, encoding="utf-8") as fh:
        db.execute(fh.read())          # second run: no error
    _, u = _seed(db)
    assert _last_seen(db, u["cognito_sub"]) is None   # no backfill, no default


def test_touch_is_throttled_to_once_per_five_minutes(db):
    _, u = _seed(db)
    sub = u["cognito_sub"]
    assert users.touch_last_seen(db, sub) is True
    first = _last_seen(db, sub)
    assert first is not None
    # Within the window: no write.
    assert users.touch_last_seen(db, sub) is False
    assert _last_seen(db, sub) == first
    # 4 minutes old: still inside the window.
    db.execute("UPDATE users SET last_seen_at = now() - interval '4 minutes' WHERE cognito_sub=%s",
               (sub,))
    assert users.touch_last_seen(db, sub) is False
    # 6 minutes old: stamped again, to now().
    db.execute("UPDATE users SET last_seen_at = now() - interval '6 minutes' WHERE cognito_sub=%s",
               (sub,))
    assert users.touch_last_seen(db, sub) is True
    assert _last_seen(db, sub) == db.execute("SELECT now()").fetchone()[0]


def test_touch_of_an_unknown_sub_writes_nothing(db):
    assert users.touch_last_seen(db, "sub-nobody-" + uuid.uuid4().hex) is False


def test_touch_only_stamps_the_caller(db):
    co, u = _seed(db)
    other = users.upsert_user(db, "sub-0091-other-" + uuid.uuid4().hex[:8], "o@example.com",
                              company_id=co["id"])
    users.touch_last_seen(db, u["cognito_sub"])
    assert _last_seen(db, other["cognito_sub"]) is None


def test_member_lists_carry_last_seen_at(db):
    co, u = _seed(db)
    users.touch_last_seen(db, u["cognito_sub"])
    rows = users.list_company_users(db, co["id"])
    assert rows[0]["last_seen_at"] is not None
    assert any(r["cognito_sub"] == u["cognito_sub"] and r["last_seen_at"] is not None
               for r in users.list_all_users(db))


def test_failed_stamp_does_not_abort_the_request_transaction(db, monkeypatch):
    """The stamp runs inside the request's single transaction. Without its savepoint a
    failing UPDATE would leave that transaction aborted and every later read in get_me
    would fail (and the commit would become a rollback)."""
    org = pytest.importorskip("lambda_org_api")
    _, u = _seed(db)

    def broken(conn, sub):
        conn.execute("UPDATE users SET no_such_column = 1 WHERE cognito_sub=%s", (sub,))
    monkeypatch.setattr(org.users, "touch_last_seen", broken)
    org._stamp_last_seen(db, u)
    # The transaction is still usable and the seeded rows are still there.
    assert db.execute("SELECT count(*) FROM users WHERE cognito_sub=%s",
                      (u["cognito_sub"],)).fetchone()[0] == 1


def test_successful_stamp_survives_the_request_commit(migrated_db_url):
    """End to end on the real request shape: `with conn:` + savepoint, then a fresh
    connection sees the stamp, so it was committed."""
    import psycopg
    org = pytest.importorskip("lambda_org_api")
    tag = uuid.uuid4().hex[:8]
    sub = f"sub-0091-commit-{tag}"
    with psycopg.connect(migrated_db_url) as conn:
        co = companies.create_company(conn, f"Co-0091-commit-{tag}")
        users.upsert_user(conn, sub, f"c-{tag}@example.com", company_id=co["id"])
    try:
        with psycopg.connect(migrated_db_url) as conn:
            org._stamp_last_seen(conn, users.get_user_by_sub(conn, sub))
        with psycopg.connect(migrated_db_url) as conn:
            assert _last_seen(conn, sub) is not None
    finally:
        with psycopg.connect(migrated_db_url) as conn:
            conn.execute("DELETE FROM users WHERE cognito_sub=%s", (sub,))
            conn.execute("DELETE FROM companies WHERE id=%s", (co["id"],))
