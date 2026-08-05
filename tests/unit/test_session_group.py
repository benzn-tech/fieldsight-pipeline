"""
Unit: multi-device session groups (spec 2026-08-04).

The group's identity is the LEAD device's session_id, so a group forms with no
network. These tests pin the parts whose failure is either irreversible (a
destructive migration running against prod) or silent (a group that loses a
member, or one that spans two companies).
"""
import os

import pytest

from repositories import meeting_session

MIG = os.path.join(os.path.dirname(__file__), "..", "..",
                   "src", "migrations", "0031_session_group.sql")


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, params=None):
        self.conn.calls.append({"sql": sql, "params": params})
        self._rows = self.conn._pop()
        return self

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class FakeConn:
    def __init__(self, results=None):
        self.calls = []
        self._results = list(results or [])

    def cursor(self, **kwargs):
        return FakeCursor(self)

    def _pop(self):
        return self._results.pop(0) if self._results else []


SID_A = "a" * 32
SID_B = "b" * 32


# ---- migration ------------------------------------------------------------

def test_migration_is_additive_and_idempotent():
    """A destructive statement here runs against the PROD database on the next
    merge to main. Additive + IF NOT EXISTS is the whole safety story."""
    sql = open(MIG, encoding="utf-8").read().lower()
    assert "add column if not exists group_id" in sql
    assert "create index if not exists" in sql
    for destructive in ("drop ", "truncate", "delete from", "alter column"):
        assert destructive not in sql, f"destructive statement present: {destructive}"


def test_group_id_is_nullable_and_self_referencing():
    sql = open(MIG, encoding="utf-8").read().lower()
    assert "references meeting_session(session_id)" in sql
    added = sql.split("add column if not exists")[1].split(";")[0]
    assert "not null" not in added, "every existing row is solo; the column must be nullable"


# ---- repository -----------------------------------------------------------

def test_ensure_open_stores_group_id():
    conn = FakeConn(results=[[{"session_id": SID_A, "group_id": SID_B}]])
    row = meeting_session.ensure_open(
        conn, SID_A, "co-1", "u-1", None, "audio", None, group_id=SID_B)
    assert row["group_id"] == SID_B
    assert SID_B in conn.calls[0]["params"]


def test_ensure_open_without_group_is_unchanged():
    """Solo recording: the column is simply NULL, no new behaviour."""
    conn = FakeConn(results=[[{"session_id": SID_A, "group_id": None}]])
    row = meeting_session.ensure_open(conn, SID_A, "co-1", "u-1", None, "audio", None)
    assert row["group_id"] is None


def test_ensure_open_never_clears_an_existing_group():
    """/open is best-effort and can arrive twice. A second call without the
    group id must not orphan a device that already joined — hence COALESCE on
    the existing value rather than EXCLUDED."""
    conn = FakeConn(results=[[{"session_id": SID_A, "group_id": SID_B}]])
    meeting_session.ensure_open(conn, SID_A, "co-1", "u-1", None, "audio", None)
    sql = conn.calls[0]["sql"].lower()
    assert "coalesce(meeting_session.group_id, excluded.group_id)" in sql


def test_list_group_members_scopes_to_the_group_and_orders_by_open():
    conn = FakeConn(results=[[{"session_id": SID_A}, {"session_id": "c" * 32}]])
    rows = meeting_session.list_group_members(conn, SID_B)
    assert len(rows) == 2
    sql = conn.calls[0]["sql"].lower()
    assert "group_id = %s" in sql
    assert "order by opened_at" in sql
    assert conn.calls[0]["params"] == (SID_B, SID_B)


def test_the_lead_is_a_member_of_its_own_group():
    """The bug this pins cost the lead its own audio.

    The lead never scans anything — it shows a code and keeps recording — so it
    carries NO group_id, and its /open fired before the group existed. Matching
    on group_id alone therefore returned only the joiners, and the merged report
    would have quietly excluded the person holding the meeting. Membership is
    derived from the group id BEING the lead's session id, so it needs nothing
    from the device."""
    conn = FakeConn(results=[[]])
    meeting_session.list_group_members(conn, SID_B)
    sql = conn.calls[0]["sql"].lower()
    assert "session_id = %s" in sql, "the lead has no group_id; match it by identity"


def test_settling_waits_for_the_lead_too():
    """Settling without the lead would finalize the meeting while the person
    holding the code is still recording it."""
    conn = FakeConn(results=[[{"unsettled": 0}]])
    meeting_session.group_is_settled(conn, SID_B, 900)
    assert "session_id = %s" in conn.calls[0]["sql"].lower()


# ---- ending a meeting for everyone ---------------------------------------


def test_ending_a_group_marks_every_member():
    conn = FakeConn(results=[
        [{"x": 1}],                                        # a joiner exists
        [{"session_id": SID_A}, {"session_id": SID_B}],    # rows marked
    ])
    assert meeting_session.end_group(conn, SID_B) == 2
    sql = conn.calls[1]["sql"].lower()
    assert "group_ended_at = now()" in sql
    assert "group_id = %s" in sql and "session_id = %s" in sql


def test_ending_a_solo_session_marks_nothing():
    """THE guard on this whole feature.

    A solo session's id is indistinguishable from a lead's at the close
    handler. Without this, every ordinary End would mark itself ended, and the
    upload path would then tell that device to stop recording — turning a
    multi-device feature into a bug in the only path most users ever take."""
    conn = FakeConn(results=[[]])          # no joiner
    assert meeting_session.end_group(conn, SID_A) == 0
    assert len(conn.calls) == 1, "must not issue the UPDATE at all"


def test_ending_twice_keeps_the_first_time():
    """The timestamp records when the meeting ended, not when the last device
    got round to asking."""
    conn = FakeConn(results=[[{"x": 1}], []])
    meeting_session.end_group(conn, SID_B)
    assert "group_ended_at is null" in conn.calls[1]["sql"].lower()


def test_group_is_ended_reads_the_whole_group():
    """A device that joined AFTER the end has group_ended_at NULL on its own
    row. Reading only that row would let it record into a finished meeting."""
    conn = FakeConn(results=[[{"x": 1}]])
    assert meeting_session.group_is_ended(conn, SID_B) is True
    sql = conn.calls[0]["sql"].lower()
    assert "group_id = %s" in sql and "session_id = %s" in sql


def test_group_is_ended_false_when_nobody_has_ended_it():
    assert meeting_session.group_is_ended(FakeConn(results=[[]]), SID_B) is False


def test_group_is_settled_false_while_a_member_could_still_be_recording():
    conn = FakeConn(results=[[{"unsettled": 1}]])
    assert meeting_session.group_is_settled(conn, SID_B, 900) is False


def test_group_is_settled_true_when_every_member_is_terminal_or_quiet():
    conn = FakeConn(results=[[{"unsettled": 0}]])
    assert meeting_session.group_is_settled(conn, SID_B, 900) is True


def test_group_is_settled_excludes_terminal_members_from_the_wait():
    """A device that already sent must never hold the group open."""
    conn = FakeConn(results=[[{"unsettled": 0}]])
    meeting_session.group_is_settled(conn, SID_B, 900)
    sql = conn.calls[0]["sql"].lower()
    assert "status not in ('sent','failed')" in sql
    assert "make_interval" in sql, "the idle window must be parameterised, not hardcoded"
