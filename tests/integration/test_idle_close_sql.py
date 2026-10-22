"""Idle-close inference against a real PostgreSQL, at the deployed threshold.

The unit tests prove IDLE_CLOSE_SECONDS is 3600 by default; this proves what
that means for a row: a session quiet for 20 minutes — closed under the old
15-minute rule — stays `open`, and one quiet for 61 minutes is moved to
`pending_close` with intent 'idle'.

Skipped unless TEST_DATABASE_URL is set (tests/conftest.py).
"""
import pytest

import lambda_finalize_claim
from lambda_finalize_claim import infer_idle_closes

pytestmark = pytest.mark.integration

SID = "f" * 32


def _seed(db):
    cid = db.execute(
        "INSERT INTO companies (name) VALUES ('C') RETURNING id").fetchone()[0]
    uid = db.execute(
        "INSERT INTO users (company_id, email, global_role) "
        "VALUES (%s,'a@b.c','worker') RETURNING id", (cid,)).fetchone()[0]
    return cid, uid


def _open_session(db, cid, uid, quiet_minutes):
    db.execute(
        "INSERT INTO meeting_session (session_id, company_id, user_id, kind, status, "
        "opened_at, last_segment_at) "
        "VALUES (%s,%s,%s,'audio','open', now() - interval '3 hours', "
        "now() - make_interval(mins => %s))",
        (SID, cid, uid, quiet_minutes))


def _state(db):
    return db.execute(
        "SELECT status, close_intent FROM meeting_session WHERE session_id = %s",
        (SID,)).fetchone()


def test_open_session_idle_20_min_is_not_closed(db):
    cid, uid = _seed(db)
    _open_session(db, cid, uid, 20)

    assert infer_idle_closes(db, lambda_finalize_claim.IDLE_CLOSE_SECONDS) == 0
    assert _state(db)[0] == "open"


def test_open_session_idle_61_min_is_closed_as_idle(db):
    cid, uid = _seed(db)
    _open_session(db, cid, uid, 61)

    assert infer_idle_closes(db, lambda_finalize_claim.IDLE_CLOSE_SECONDS) == 1
    assert _state(db) == ("pending_close", "idle")
