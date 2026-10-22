"""A deliberate End is not undone by its own last chunk (TEST 2026-10-22, M2 Task 9).

The phone queues `/close {intent:'end'}` straight after the last chunk's
`/complete`. That chunk's VAD sidecar lands 2-11 s later and session_activity
touches the row. touch_segment treated every touch on a `pending_close` row as
a resume within grace -- `open` again, close cleared, version bumped -- so the
End was undone and the session waited out the idle hour. An End has grace 0:
there is no window to resume within. An idle/stop close keeps its resume.

Skipped unless TEST_DATABASE_URL is set (tests/conftest.py).
"""
from datetime import datetime, timedelta, timezone

import pytest

import lambda_finalize_claim
from repositories import meeting_session, recordings

pytestmark = pytest.mark.integration

SID = "b" * 32
UTC = timezone.utc
T_CHUNK = datetime(2026, 10, 22, 11, 27, 37, tzinfo=UTC)   # c0003's device start
T_END = datetime(2026, 10, 22, 11, 28, 7, tzinfo=UTC)      # the body's endedAt


def _seed(db):
    cid = db.execute(
        "INSERT INTO companies (name) VALUES ('C') RETURNING id").fetchone()[0]
    uid = db.execute(
        "INSERT INTO users (company_id, email, global_role) "
        "VALUES (%s,'a@b.c','worker') RETURNING id", (cid,)).fetchone()[0]
    return cid, uid


def _open(db):
    cid, uid = _seed(db)
    db.execute(
        "INSERT INTO meeting_session (session_id, company_id, user_id, kind, status, "
        "opened_at, last_segment_at, last_chunk_at) "
        "VALUES (%s,%s,%s,'audio','open', now() - interval '20 minutes', "
        "now() - interval '17 minutes', %s)",
        (SID, cid, uid, T_CHUNK - timedelta(seconds=90)))
    return cid, uid


def _row(db):
    return meeting_session.get(db, SID)


def test_end_close_survives_its_last_chunks_sidecar(db):
    _open(db)
    closed = meeting_session.mark_pending_close(db, SID, T_END, "end")
    before = _row(db)

    now = datetime.now(UTC)
    meeting_session.touch_segment(db, SID, now, chunk_at=T_CHUNK)

    row = _row(db)
    assert row["status"] == "pending_close"
    assert row["close_intent"] == "end"
    assert row["closed_at"] == T_END
    assert row["version"] == closed["version"], \
        "a version bump would CAS-fail the sweep's claim"
    # Activity still advances: idle tracking and the late-chunk test read these.
    assert row["last_segment_at"] > before["last_segment_at"]
    assert row["last_chunk_at"] == T_CHUNK
    assert row["segment_count"] == before["segment_count"] + 1

    # The sweep's claim, against the version the close returned, still wins.
    assert meeting_session.claim_finalize(db, SID, closed["version"])["status"] == "finalizing"


def test_end_close_is_due_after_the_touch(db):
    _open(db)
    meeting_session.mark_pending_close(db, SID, T_END, "end")
    meeting_session.touch_segment(db, SID, datetime.now(UTC), chunk_at=T_CHUNK)
    assert [r["session_id"] for r in meeting_session.list_due_finalize(db, 30)] == [SID]


def test_idle_close_still_resumes_on_a_touch(db):
    _open(db)
    closed = meeting_session.mark_pending_close(db, SID, datetime.now(UTC), "idle")

    meeting_session.touch_segment(db, SID, datetime.now(UTC), chunk_at=T_CHUNK)

    row = _row(db)
    assert row["status"] == "open"
    assert row["close_intent"] is None
    assert row["closed_at"] is None
    assert row["version"] == closed["version"] + 1
    assert meeting_session.claim_finalize(db, SID, closed["version"]) is None


def test_reopened_session_then_ended_holds(db):
    """REOPEN_LATE_CHUNKS: an idle-closed, emailed session reopens on a late
    chunk. If the phone then sends close(end), that End holds like any other."""
    _open(db)
    meeting_session.mark_pending_close(db, SID, datetime.now(UTC), "idle")
    db.execute("UPDATE meeting_session SET status = 'sent' WHERE session_id = %s", (SID,))
    meeting_session.touch_segment(db, SID, datetime.now(UTC), chunk_at=T_CHUNK)
    assert meeting_session.reopen_late(db, 3, 48) == [{"session_id": SID, "reopen_count": 1}]

    meeting_session.mark_pending_close(db, SID, T_END, "end")
    meeting_session.touch_segment(db, SID, datetime.now(UTC), chunk_at=T_CHUNK)
    row = _row(db)
    assert (row["status"], row["close_intent"]) == ("pending_close", "end")
    # And once sent it is not reopen-eligible (end is terminal).
    db.execute("UPDATE meeting_session SET status = 'sent', last_chunk_at = %s "
               "WHERE session_id = %s", (T_END, SID))
    assert meeting_session.reopen_late(db, 3, 48) == []


# --- the settle gate's read: which uploads of this session are recent ---------

def _rec(db, cid, uid, key, *, kind="audio", uploaded_ago=None, started_ago=0):
    db.execute(
        "INSERT INTO recordings (company_id, user_id, kind, s3_key, client_uuid, "
        "started_at, uploaded_at) VALUES (%s,%s,%s,%s,gen_random_uuid(),"
        "now() - make_interval(secs => %s),"
        "CASE WHEN %s::int IS NULL THEN NULL ELSE now() - make_interval(secs => %s) END)",
        (cid, uid, kind, key, started_ago, uploaded_ago, uploaded_ago))


def _key(i, sid=SID, ext="wav", kind="audio"):
    return (f"users/Ben/{kind}/2026-10-23/"
            f"ben_2026-10-23_00-26-{i:02d}_sid{sid}_c{i:04d}.{ext}")


def test_recent_session_chunks_returns_only_this_sessions_recent_uploads(db):
    cid, uid = _open(db)
    _rec(db, cid, uid, _key(1), uploaded_ago=5)                  # yes
    _rec(db, cid, uid, _key(2, ext="mp4", kind="video"), uploaded_ago=10)   # yes
    _rec(db, cid, uid, _key(3), uploaded_ago=4000)               # too old
    _rec(db, cid, uid, _key(4), uploaded_ago=None)               # never completed
    _rec(db, cid, uid, _key(5, sid="c" * 32), uploaded_ago=5)    # another session
    _rec(db, cid, uid, f"users/Ben/pictures/2026-10-23/ben_sid{SID}.jpg",
         kind="photo", uploaded_ago=5)                            # a photo
    _, other = _seed_other_user(db, cid)
    _rec(db, cid, other, _key(6), uploaded_ago=5)                 # another recorder
    # Started two days before the session opened: outside the index-bounded scan
    # (a sid is never reused across days, so this only bounds the cost).
    _rec(db, cid, uid, _key(7), uploaded_ago=5, started_ago=2 * 86400)

    rows = recordings.recent_session_chunks(db, SID, 300)
    assert sorted(r["s3_key"] for r in rows) == sorted(
        [_key(1), _key(2, ext="mp4", kind="video")])
    ages = {r["s3_key"]: r["age_s"] for r in rows}
    assert 4 <= ages[_key(1)] <= 60, "age_s is seconds since /complete"


def test_recent_session_chunks_scan_is_bounded_by_the_sessions_open(db):
    """M1: the read must use idx_recordings_user_started, not walk the
    recorder's whole history -- the plan names the index."""
    cid, uid = _open(db)
    # A recorder with a year of history (one chunk every ~15 min), so the
    # planner has a reason to choose between the two indexes leading on user_id.
    db.execute(
        "INSERT INTO recordings (company_id, user_id, kind, s3_key, client_uuid, "
        "started_at, uploaded_at) "
        "SELECT %s, %s, 'audio', 'users/Ben/audio/old/' || g || '.wav', gen_random_uuid(), "
        "now() - make_interval(mins => g * 15), now() - make_interval(mins => g * 15) "
        "FROM generate_series(1, 35000) g", (cid, uid))
    db.execute("ANALYZE recordings")
    # What is pinned is that the bound is an index condition (it CAN be used);
    # which plan wins on prod is the planner's call on prod's statistics.
    db.execute("SET LOCAL enable_seqscan = off")
    plan = db.execute("EXPLAIN " + recordings.RECENT_SESSION_CHUNKS_SQL,
                      recordings.recent_session_chunks_params(SID, 300)).fetchall()
    text = " ".join(r[0] for r in plan)
    assert "idx_recordings_user_started" in text, text
    bound = text.split("idx_recordings_user_started", 1)[1]
    assert "started_at" in bound, \
        "the index must be bounded on started_at, not just its user_id prefix"


def _seed_other_user(db, cid):
    uid = db.execute(
        "INSERT INTO users (company_id, email, global_role) "
        "VALUES (%s,'z@b.c','worker') RETURNING id", (cid,)).fetchone()[0]
    return cid, uid


def test_sweep_defers_an_end_whose_upload_has_not_been_through_vad(db, monkeypatch):
    """Step 4 of the TEST run, against real rows: the close lands, the chunk is
    uploaded, its sidecar is not there yet -- the claim waits for it."""
    cid, uid = _open(db)
    _rec(db, cid, uid, _key(3), uploaded_ago=3)
    meeting_session.mark_pending_close(db, SID, T_END, "end")
    monkeypatch.setattr(lambda_finalize_claim, "_read_sidecar", lambda s3, key: None)
    monkeypatch.setattr(lambda_finalize_claim, "BATCH_TRANSCRIPTION", False)
    asked = []
    monkeypatch.setattr(lambda_finalize_claim, "_request_extraction",
                        lambda *a, **kw: asked.append(a[0]))

    assert lambda_finalize_claim.sweep(db, infer_idle=False, reopen=False) == []
    assert _row(db)["status"] == "pending_close"

    monkeypatch.setattr(lambda_finalize_claim, "_read_sidecar",
                        lambda s3, key: {"segments": []})
    lambda_finalize_claim.sweep(db, infer_idle=False, reopen=False)
    assert _row(db)["status"] == "finalizing"


def test_a_sql_error_in_the_gate_fails_open_and_the_tick_goes_on(db, monkeypatch):
    """M4: the gate's read runs in a savepoint. A SQL error there must not abort
    the tick's transaction: the session is still claimed, the connection still
    usable, and reconcile after it still runs."""
    _open(db)
    meeting_session.mark_pending_close(db, SID, T_END, "end")
    monkeypatch.setattr(lambda_finalize_claim, "BATCH_TRANSCRIPTION", False)
    monkeypatch.setattr(lambda_finalize_claim, "_request_extraction", lambda *a, **kw: None)

    def bad_sql(conn, sid, within):
        conn.execute("SELECT no_such_column FROM recordings")

    monkeypatch.setattr(recordings, "recent_session_chunks", bad_sql)

    lambda_finalize_claim.sweep(db, infer_idle=False, reopen=False)
    assert _row(db)["status"] == "finalizing"
    assert lambda_finalize_claim.reconcile(db, lambda rid: {"status": "sent"}) == [(SID, "sent")]
    assert _row(db)["status"] == "sent"
