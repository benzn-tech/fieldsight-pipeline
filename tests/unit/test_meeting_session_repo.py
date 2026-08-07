"""Tests for src/repositories/meeting_session.py — the session lifecycle state
machine + optimistic-lock idempotency (voice-timeliness spec §3.2, §8.4).

FakeConn/FakeCursor record every execute() so SQL shape + params are asserted
without a real Postgres (same harness as test_action_items_repo.py)."""
from repositories import meeting_session as ms


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._rows = []

    def execute(self, sql, params=None):
        self.conn.calls.append({"sql": sql, "params": params})
        self._rows = self.conn._pop_result()
        return self

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakeConn:
    def __init__(self, results=None):
        self.calls = []
        self._results = list(results or [])

    def _pop_result(self):
        return self._results.pop(0) if self._results else []

    def cursor(self, row_factory=None):
        return FakeCursor(self)


SID = "9f8c1e2a4b6d47f0a1b2c3d4e5f60718"


def test_ensure_open_upserts_without_regressing():
    conn = FakeConn(results=[[{"session_id": SID, "status": "open"}]])
    out = ms.ensure_open(conn, SID, "co-1", "u-1", "site-1", "audio", "2026-07-28T14:03:00")
    assert out == {"session_id": SID, "status": "open"}
    sql = conn.calls[0]["sql"]
    assert "INSERT INTO meeting_session" in sql
    assert "ON CONFLICT (session_id) DO UPDATE" in sql
    # never overwrites an existing opened_at / status
    assert "COALESCE(meeting_session.opened_at, EXCLUDED.opened_at)" in sql
    # `kind` fills in like the rest. The VAD sidecar opens the session before any
    # transcript exists and its key carries no _src{ext} token, so the first
    # insert legitimately has kind=None; without this the column stays NULL for
    # the life of the session even though the transcript arrives knowing it.
    assert "COALESCE(meeting_session.kind, EXCLUDED.kind)" in sql
    assert conn.calls[0]["params"][0] == SID


def test_touch_segment_resumes_from_pending_close_and_bumps_version():
    conn = FakeConn(results=[[{"session_id": SID, "status": "open", "version": 2}]])
    ms.touch_segment(conn, SID, "2026-07-28T14:07:20")
    sql = conn.calls[0]["sql"]
    # resume: pending_close -> open, clear close, bump version — all keyed off OLD status
    assert "status = CASE WHEN status = 'pending_close' THEN 'open' ELSE status END" in sql
    assert "version = CASE WHEN status = 'pending_close' THEN version + 1 ELSE version END" in sql
    assert "closed_at = CASE WHEN status = 'pending_close' THEN NULL" in sql
    assert "segment_count = segment_count + 1" in sql


def test_mark_pending_close_bumps_version_and_guards_status():
    conn = FakeConn(results=[[{"session_id": SID, "status": "pending_close", "version": 3}]])
    out = ms.mark_pending_close(conn, SID, "2026-07-28T14:19:41", "end")
    assert out["version"] == 3
    sql = conn.calls[0]["sql"]
    assert "status = 'pending_close'" in sql
    assert "version = version + 1" in sql
    assert "AND status IN ('open', 'pending_close')" in sql
    assert conn.calls[0]["params"] == ("2026-07-28T14:19:41", "end", SID)


def test_claim_finalize_is_a_version_cas():
    conn = FakeConn(results=[[{"session_id": SID, "status": "finalizing"}]])
    out = ms.claim_finalize(conn, SID, 3)
    assert out == {"session_id": SID, "status": "finalizing"}
    sql = conn.calls[0]["sql"]
    assert "status = 'finalizing'" in sql
    assert "WHERE session_id = %s AND version = %s AND status = 'pending_close'" in sql
    assert conn.calls[0]["params"] == (SID, 3)


def test_claim_finalize_noops_when_version_bumped_by_resume():
    # empty result set -> the resume already bumped version; finalize claims nothing
    conn = FakeConn(results=[[]])
    assert ms.claim_finalize(conn, SID, 3) is None


def test_update_rolling_summary_is_version_cas():
    conn = FakeConn(results=[[{"session_id": SID, "rolling_summary_version": 5}]])
    ms.update_rolling_summary(conn, SID, "running summary…", 4)
    sql = conn.calls[0]["sql"]
    assert "rolling_summary_version = rolling_summary_version + 1" in sql
    assert "AND rolling_summary_version = %s" in sql
    assert conn.calls[0]["params"] == ("running summary…", SID, 4)


def test_update_rolling_summary_noops_on_version_race():
    conn = FakeConn(results=[[]])
    assert ms.update_rolling_summary(conn, SID, "x", 4) is None


def test_mark_sent_and_get():
    conn = FakeConn(results=[[{"session_id": SID, "status": "sent"}], [{"session_id": SID}]])
    assert ms.mark_sent(conn, SID)["status"] == "sent"
    assert "status = 'sent'" in conn.calls[0]["sql"]
    ms.get(conn, SID)
    assert conn.calls[1]["sql"].startswith("SELECT")


def test_list_idle_open_selects_open_sessions_past_the_idle_window():
    conn = FakeConn(results=[[
        {"session_id": SID, "version": 2, "last_activity": "2026-07-28T14:05:00"},
    ]])
    out = ms.list_idle_open(conn, 900)
    assert out == [{"session_id": SID, "version": 2, "last_activity": "2026-07-28T14:05:00"}]
    sql = conn.calls[0]["sql"]
    assert "status = 'open'" in sql
    # keys on last activity (last_segment_at, else opened_at) -> a still-active
    # long meeting (chunks arriving) is never mistaken for idle
    assert "COALESCE(last_segment_at, opened_at)" in sql
    assert "make_interval(secs => %s)" in sql
    assert conn.calls[0]["params"] == (900,)


def test_list_idle_open_empty_when_none_idle():
    conn = FakeConn(results=[[]])
    assert ms.list_idle_open(conn, 900) == []
