import os
import pytest

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")
_needs_db = pytest.mark.skipif(
    not TEST_DB_URL, reason="TEST_DATABASE_URL not set; skipping DB integration test"
)


def pytest_collection_modifyitems(config, items):
    # Auto-skip anything marked 'integration' when no test DB is configured.
    for item in items:
        if "integration" in item.keywords and not TEST_DB_URL:
            item.add_marker(_needs_db)


@pytest.fixture(scope="session")
def migrated_db_url():
    """Apply all migrations once against the test DB; return its URL."""
    if not TEST_DB_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    import psycopg
    from db.migrate import apply_migrations

    migrations_dir = os.path.join(os.path.dirname(__file__), "..", "src", "migrations")
    with psycopg.connect(TEST_DB_URL, autocommit=True) as conn:
        apply_migrations(conn, os.path.abspath(migrations_dir))
    return TEST_DB_URL


@pytest.fixture
def db(migrated_db_url):
    """A connection whose work is rolled back after each test (isolation)."""
    import psycopg
    from pgvector.psycopg import register_vector

    conn = psycopg.connect(migrated_db_url)
    register_vector(conn)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()
