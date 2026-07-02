"""Imports psycopg/pgvector. Repositories receive a connection from here."""
import os
import psycopg
from pgvector.psycopg import register_vector


def get_connection(dsn: str | None = None, autocommit: bool = False):
    dsn = dsn or os.environ["DATABASE_URL"]
    conn = psycopg.connect(dsn, autocommit=autocommit)
    register_vector(conn)
    return conn
