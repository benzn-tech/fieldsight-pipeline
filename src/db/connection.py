"""Imports psycopg/pgvector. Repositories receive a connection from here."""
import os
import psycopg
from pgvector.psycopg import register_vector


def get_connection(dsn: str | None = None, autocommit: bool = False):
    """Return a psycopg connection with pgvector registered.

    NOTE: repositories never commit. The caller owns the transaction:
    use `with get_connection() as conn:` (commits on clean exit) or pass
    autocommit=True. A bare get_connection() + close() ROLLS BACK writes.
    """
    dsn = dsn or os.environ["DATABASE_URL"]
    conn = psycopg.connect(dsn, autocommit=autocommit)
    register_vector(conn)
    return conn
