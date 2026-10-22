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


# --- A late /open (M2): the phone queues /open offline and sends it later -----
# session_open stores the body's startedAt as opened_at and no chunk has touched
# the row yet, so opened_at alone can be hours old the moment the row appears.
# Idle is measured on SERVER time, so until the first chunk lands the anchor is
# whichever is later of opened_at and created_at (server now() at insert).

def _user(db):
    import uuid
    from repositories import companies, users
    tag = uuid.uuid4().hex[:6]
    co = companies.create_company(db, f"IdleCo-{tag}")
    u = users.upsert_user(db, f"sub-idle-{tag}", f"idle-{tag}@a.nz", company_id=co["id"],
                          first_name="I", last_name="D", global_role="worker")
    return users.get_by_id(db, u["id"])


def test_late_open_is_not_idle_closed_before_its_first_chunk(db):
    from datetime import datetime, timedelta, timezone

    import lambda_org_api as api
    from repositories import meeting_session

    user = _user(db)
    started = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    res = api.session_open(db, user, SID, {"kind": "audio", "sessionType": "meeting",
                                           "startedAt": started})
    assert res["statusCode"] == 200, res
    row = meeting_session.get(db, SID)
    assert row["last_segment_at"] is None
    assert row["opened_at"] < datetime.now(timezone.utc) - timedelta(minutes=110)

    assert infer_idle_closes(db, lambda_finalize_claim.IDLE_CLOSE_SECONDS) == 0
    assert _state(db)[0] == "open"
    assert meeting_session.count_live(db) == 1

    # The same row once it has itself sat on the server past the window.
    db.execute("UPDATE meeting_session SET created_at = now() - interval '61 minutes' "
               "WHERE session_id = %s", (SID,))
    assert infer_idle_closes(db, lambda_finalize_claim.IDLE_CLOSE_SECONDS) == 1
    assert _state(db) == ("pending_close", "idle")


def test_row_with_neither_timestamp_is_still_excluded(db):
    # GREATEST ignores NULLs, so created_at must not become an anchor on its own:
    # a row with neither opened_at nor last_segment_at stays unclosable and
    # uncounted (count_live's scale-to-zero guard).
    from repositories import meeting_session

    cid, uid = _seed(db)
    db.execute(
        "INSERT INTO meeting_session (session_id, company_id, user_id, kind, status, "
        "created_at) VALUES (%s,%s,%s,'audio','open', now() - interval '3 hours')",
        (SID, cid, uid))
    assert infer_idle_closes(db, lambda_finalize_claim.IDLE_CLOSE_SECONDS) == 0
    assert meeting_session.count_live(db) == 0
