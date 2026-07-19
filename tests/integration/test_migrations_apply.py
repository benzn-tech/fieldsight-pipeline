import os
import pytest
import psycopg
from db.migrate import apply_migrations, applied_versions

pytestmark = pytest.mark.integration

MIGRATIONS_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "src", "migrations")
)


def _fresh_conn():
    """Autocommit connection on a wiped public schema. CI DB is ephemeral."""
    url = os.environ["TEST_DATABASE_URL"]
    conn = psycopg.connect(url, autocommit=True)
    conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    return conn


def test_apply_is_idempotent_and_records_versions():
    conn = _fresh_conn()
    try:
        first = apply_migrations(conn, MIGRATIONS_DIR)
        assert first, "expected at least one migration applied on empty DB"
        assert "0001_extensions.sql" in first
        second = apply_migrations(conn, MIGRATIONS_DIR)
        assert second == [], "re-running must apply nothing"
        assert "0001_extensions.sql" in applied_versions(conn)
    finally:
        conn.close()


def test_extensions_installed():
    conn = _fresh_conn()
    try:
        apply_migrations(conn, MIGRATIONS_DIR)
        names = {r[0] for r in conn.execute("SELECT extname FROM pg_extension").fetchall()}
        assert "vector" in names
        assert "pgcrypto" in names
    finally:
        conn.close()


def test_platform_company_seeded():
    # Phase 3 T3 / D6: 0015 seeds the dedicated FieldSight-platform company row
    # so a platform_admin's company_id stays NOT NULL.
    conn = _fresh_conn()
    try:
        apply_migrations(conn, MIGRATIONS_DIR)
        rows = conn.execute(
            "SELECT name, industry FROM companies WHERE name='FieldSight-platform'"
        ).fetchall()
        assert rows == [("FieldSight-platform", "platform")]
    finally:
        conn.close()


def test_sites_has_coordinate_columns():
    # 0018: nullable lat/lng for weather + map features.
    conn = _fresh_conn()
    try:
        apply_migrations(conn, MIGRATIONS_DIR)
        cols = {
            r[0]
            for r in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='sites'"
            ).fetchall()
        }
        assert {"latitude", "longitude"} <= cols
    finally:
        conn.close()


def test_platform_company_seed_sql_is_idempotent_on_direct_rerun():
    # Belt-and-suspenders on top of the schema_migrations gate (which already
    # prevents a file from re-running via apply_migrations): even a manual
    # re-run of 0015's raw SQL (e.g. a recovery script) must not double-insert
    # the platform company row -- the file's own WHERE NOT EXISTS guard.
    conn = _fresh_conn()
    try:
        apply_migrations(conn, MIGRATIONS_DIR)
        path = os.path.join(MIGRATIONS_DIR, "0015_platform_company.sql")
        with open(path, "r", encoding="utf-8") as fh:
            sql = fh.read()
        conn.execute(sql)
        conn.execute(sql)
        count = conn.execute(
            "SELECT count(*) FROM companies WHERE name='FieldSight-platform'"
        ).fetchone()[0]
        assert count == 1
    finally:
        conn.close()
