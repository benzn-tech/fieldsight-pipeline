"""
Integration: voice_ask_log insert roundtrip — SP-Ask Task 6. Uses the shared
`db` fixture (migrations applied session-scoped via conftest; per-test rollback
isolation), the same wiring every other integration test uses. Reads back one
row. Mirrors test_core_repositories.py.
"""
import pytest

from repositories import voice_ask_log

pytestmark = pytest.mark.integration


def test_insert_and_read_back(db):
    row_id = voice_ask_log.insert_voice_ask(
        db, "sub-int-1", "what happened today", "The pour finished.",
        company_id=None)
    row = db.execute(
        "SELECT caller_sub, transcript, answer FROM voice_ask_log WHERE id = %s",
        (row_id,)).fetchone()
    assert row == ("sub-int-1", "what happened today", "The pour finished.")
