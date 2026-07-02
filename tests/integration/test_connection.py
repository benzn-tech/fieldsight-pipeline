import pytest
from db.connection import get_connection

pytestmark = pytest.mark.integration


def test_get_connection_runs_query_and_has_vector(migrated_db_url):
    conn = get_connection(migrated_db_url, autocommit=True)
    try:
        assert conn.execute("SELECT 1").fetchone()[0] == 1
        conn.execute("SELECT %s::vector", ([0.0, 1.0, 2.0],))  # vector type usable
    finally:
        conn.close()
