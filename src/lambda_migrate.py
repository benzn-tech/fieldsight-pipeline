"""In-VPC Lambda: apply pending SQL migrations to Aurora. Wired by Phase 2B."""
import os
from db.connection import get_connection
from db.migrate import apply_migrations

_DEFAULT_DIR = os.path.join(os.path.dirname(__file__), "migrations")


def lambda_handler(event, context):
    migrations_dir = os.environ.get("MIGRATIONS_DIR", _DEFAULT_DIR)
    conn = get_connection(autocommit=True)
    try:
        applied = apply_migrations(conn, migrations_dir)
    finally:
        conn.close()
    return {"applied": applied}
