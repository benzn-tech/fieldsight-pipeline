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
