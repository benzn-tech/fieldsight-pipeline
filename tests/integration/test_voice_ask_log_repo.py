"""
Integration: voice_ask_log insert roundtrip — SP-Ask Task 6. Applies migrations
(idempotent) then inserts + reads back one row. Mirrors test_core_repositories.py.
"""
import os
import pytest

psycopg = pytest.importorskip("psycopg")
from db.connection import get_connection  # noqa: E402
from db.migrate import apply_migrations  # noqa: E402
from repositories import voice_ask_log  # noqa: E402

MIGRATIONS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "src", "migrations")


@pytest.mark.integration
def test_insert_and_read_back():
    with get_connection() as conn:
        apply_migrations(conn, MIGRATIONS_DIR)
    with get_connection() as conn:
        row_id = voice_ask_log.insert_voice_ask(
            conn, "sub-int-1", "what happened today", "The pour finished.",
            company_id=None)
    with get_connection() as conn:
        row = conn.execute(
            "SELECT caller_sub, transcript, answer FROM voice_ask_log WHERE id = %s",
            (row_id,)).fetchone()
    assert row == ("sub-int-1", "what happened today", "The pour finished.")
