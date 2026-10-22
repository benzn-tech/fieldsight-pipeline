"""Late-chunk reopen against a real PostgreSQL (M2 spec, owner decision).

A phone that was offline for more than the idle-close window delivers chunks
after the session was idle-closed and emailed. The sweep reopens such a session
so it is re-finalized with a complete ("Updated") email.

The test is `last_chunk_at > closed_chunk_at`: both are the DEVICE's file-name
time (closed_chunk_at is last_chunk_at snapshotted at the idle close), so a
device clock that is hours off (BUG-37) cancels out. Comparing with `closed_at`
(server time) would reopen every session from a fast device.

Skipped unless TEST_DATABASE_URL is set (tests/conftest.py).
"""
from datetime import datetime, timedelta, timezone

import pytest

import lambda_finalize_claim
from repositories import meeting_session

pytestmark = pytest.mark.integration

SID = "a" * 32
UTC = timezone.utc


def _t(hh, mm):
    return datetime(2026, 10, 22, hh, mm, tzinfo=UTC)


def _seed(db):
    cid = db.execute(
        "INSERT INTO companies (name) VALUES ('C') RETURNING id").fetchone()[0]
    uid = db.execute(
        "INSERT INTO users (company_id, email, global_role) "
        "VALUES (%s,'a@b.c','worker') RETURNING id", (cid,)).fetchone()[0]
    return cid, uid


def _session(db, *, status="sent", close_intent="idle", closed_at=_t(11, 20),
             closed_chunk_at=_t(10, 5), last_chunk_at=_t(10, 6), version=4,
             reopen_count=0):
    cid, uid = _seed(db)
    db.execute(
        "INSERT INTO meeting_session (session_id, company_id, user_id, kind, status, "
        "opened_at, last_segment_at, closed_at, close_intent, closed_chunk_at, "
        "last_chunk_at, version, reopen_count, finalizing_at) "
        "VALUES (%s,%s,%s,'audio',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (SID, cid, uid, status, _t(9, 0), _t(10, 20), closed_at, close_intent,
         closed_chunk_at, last_chunk_at, version, reopen_count,
         _t(11, 21) if status != "open" else None))


def _row(db):
    return meeting_session.get(db, SID)


def test_touch_advances_last_chunk_at(db):
    _session(db, status="open", close_intent=None, closed_at=None,
             closed_chunk_at=None, last_chunk_at=None)
    now = datetime.now(UTC)
    T = _t(10, 21)

    meeting_session.touch_segment(db, SID, now, chunk_at=T)
    assert _row(db)["last_chunk_at"] == T
    meeting_session.touch_segment(db, SID, now, chunk_at=T - timedelta(minutes=5))
    assert _row(db)["last_chunk_at"] == T, "an older chunk must not move it back"
    meeting_session.touch_segment(db, SID, now)
    assert _row(db)["last_chunk_at"] == T, "chunk_at=None leaves it"


def test_idle_close_snapshots_chunk_time(db):
    _session(db, status="open", close_intent=None, closed_at=None,
             closed_chunk_at=None, last_chunk_at=_t(10, 5))
    row = meeting_session.mark_pending_close(db, SID, datetime.now(UTC), "idle")
    assert row["closed_chunk_at"] == _t(10, 5)


def test_end_close_does_not_snapshot_chunk_time(db):
    _session(db, status="open", close_intent=None, closed_at=None,
             closed_chunk_at=None, last_chunk_at=_t(10, 5))
    row = meeting_session.mark_pending_close(db, SID, datetime.now(UTC), "end")
    assert row["closed_chunk_at"] is None


def test_idle_sent_with_late_chunk_reopens(db):
    _session(db)

    assert meeting_session.reopen_late(db, 3) == [
        {"session_id": SID, "reopen_count": 1}]
    row = _row(db)
    assert row["status"] == "open"
    assert row["version"] == 5
    assert row["closed_at"] is None
    assert row["close_intent"] is None
    assert row["closed_chunk_at"] is None
    assert row["finalizing_at"] is None
    assert row["reopen_count"] == 1


def test_failed_also_reopens(db):
    _session(db, status="failed")
    assert meeting_session.reopen_late(db, 3) == [
        {"session_id": SID, "reopen_count": 1}]
    assert _row(db)["status"] == "open"


def test_end_is_never_reopened(db):
    _session(db, close_intent="end")
    before = _row(db)
    assert meeting_session.reopen_late(db, 3) == []
    assert _row(db) == before


def test_old_chunk_does_not_reopen(db):
    # A re-driven old transcript touches with the same (or an older) time.
    _session(db, closed_chunk_at=_t(10, 5), last_chunk_at=_t(10, 5))
    assert meeting_session.reopen_late(db, 3) == []
    assert _row(db)["status"] == "sent"


def test_skewed_clock_does_not_reopen(db):
    # Device 12 h fast: its file-name times are far past the server's closed_at.
    _session(db, closed_at=_t(10, 20), closed_chunk_at=_t(22, 5),
             last_chunk_at=_t(22, 5))
    assert meeting_session.reopen_late(db, 3) == []
    assert _row(db)["status"] == "sent"

    db.execute("UPDATE meeting_session SET last_chunk_at = %s WHERE session_id = %s",
               (_t(22, 6), SID))
    assert meeting_session.reopen_late(db, 3) == [
        {"session_id": SID, "reopen_count": 1}]


def test_null_snapshot_never_reopens(db):
    # Closed before 0089: no snapshot to compare against.
    _session(db, closed_chunk_at=None)
    assert meeting_session.reopen_late(db, 3) == []
    assert meeting_session.list_reopen_capped(db, 0) == []


def test_finalizing_waits_until_settled(db):
    _session(db, status="finalizing")
    assert meeting_session.reopen_late(db, 3) == []
    assert _row(db)["status"] == "finalizing"

    meeting_session.mark_sent(db, SID)
    assert meeting_session.reopen_late(db, 3) == [
        {"session_id": SID, "reopen_count": 1}]


def test_reopen_stops_at_reopen_max(db):
    _session(db, reopen_count=3)
    assert meeting_session.reopen_late(db, 3) == []
    assert _row(db)["status"] == "sent"
    assert meeting_session.list_reopen_capped(db, 3) == [SID]


def test_capped_list_excludes_rows_under_the_cap(db):
    _session(db, reopen_count=2)
    assert meeting_session.list_reopen_capped(db, 3) == []


def test_list_finalizing_carries_type_and_reopen_count(db):
    _session(db, status="finalizing", reopen_count=2)
    db.execute("UPDATE meeting_session SET session_type = 'meeting' "
               "WHERE session_id = %s", (SID,))
    (row,) = meeting_session.list_finalizing(db)
    assert row["session_type"] == "meeting"
    assert row["reopen_count"] == 2


def test_sweep_flag_off_is_inert(db):
    _session(db)
    lambda_finalize_claim.sweep(db, reopen=False, infer_idle=False)
    assert _row(db)["status"] == "sent"


def test_sweep_flag_on_reopens(db):
    _session(db)
    lambda_finalize_claim.sweep(db, reopen=True, infer_idle=False)
    row = _row(db)
    assert row["status"] == "open"
    assert row["reopen_count"] == 1
