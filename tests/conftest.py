import os
import pytest

# The lambda modules read these at IMPORT time, so they have to be set before any test
# module is collected — conftest is the only place early enough.
#
# Five test modules currently set them at their own module scope, which works only while
# each of them happens to be the FIRST module to import the lambda that reads them. Adding
# an unrelated test file whose name sorts earlier and which imports any lambda flips that,
# and six tests in test_final_pass_coverage_recheck.py go red for a reason that has nothing
# to do with the change under test. That happened; this is the root fix rather than
# renaming the new file out of the way.
#
# `setdefault`, so a real value in the environment still wins.
for _k, _v in (("AWS_ACCESS_KEY_ID", "testing"),
               ("AWS_SECRET_ACCESS_KEY", "testing"),
               ("AWS_DEFAULT_REGION", "ap-southeast-2"),
               ("S3_BUCKET", "test-bucket"),
               ("ANTHROPIC_API_KEY", "sk-ant-test-dummy-key")):
    os.environ.setdefault(_k, _v)

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")
collect_ignore_glob = [] if TEST_DB_URL else ["integration/*"]
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
