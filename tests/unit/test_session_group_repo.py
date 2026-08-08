"""Tests for src/repositories/session_group.py — Phase C group merge state.

FakeConn/FakeCursor record every execute() so SQL shape + params are asserted
without a real Postgres (same harness as test_meeting_session_repo.py). These
prove the shape of the statements and NOTHING about their semantics — the
exactly-once behaviour of the CAS is covered in tests/integration against a real
database, because that is the property a connection double cannot demonstrate.

Every test here guards a specific way the FIRST design failed: it hung the merge
check on a finalize event, and a group becomes mergeable at a moment when no
event is firing.
"""
import pytest

from tests.unit.test_meeting_session_repo import FakeConn

sg = pytest.importorskip("repositories.session_group",
                         reason="requires psycopg (installed in CI)")

GID = "61be49d5" + "0" * 24
CID = "3f7c1e2a-4b6d-47f0-a1b2-c3d4e5f60718"


def test_ensure_row_is_idempotent():
    # /open is best-effort and may arrive twice, and several joiners create the
    # same group. A second call must not raise or reset the state.
    conn = FakeConn(results=[[]])
    sg.ensure_row(conn, GID, CID)
    sql = conn.calls[0]["sql"]
    assert "INSERT INTO session_group" in sql
    assert "ON CONFLICT (group_id) DO NOTHING" in sql
    assert conn.calls[0]["params"] == (GID, CID)


def test_claim_is_a_conditional_update_not_a_blind_one():
    # Two sweep ticks can overlap. The CAS is what makes exactly one of them
    # win; a blind UPDATE would merge -- and email every member -- twice.
    conn = FakeConn(results=[[{"group_id": GID}]])
    assert sg.claim(conn, GID, "extractions/A/2026-08-07/grp%s.json" % GID) is True
    sql = conn.calls[0]["sql"]
    assert "UPDATE session_group" in sql
    assert "merged_at IS NULL" in sql, "no CAS condition — two ticks would both claim"
    assert "merge_count = merge_count + 1" in sql, \
        "the cap must count actual merges, not re-arms"
    assert "merged_key" in sql, "the merged key must be persisted, never re-derived"


def test_claim_returns_false_when_another_tick_won():
    conn = FakeConn(results=[[]])          # RETURNING matched no row
    assert sg.claim(conn, GID, "k") is False


def test_list_due_reads_only_unresolved_groups():
    # THE bounding predicate. It must be on session_group itself, or the scan
    # re-reads every group ever created, every minute, forever — and groups that
    # settle with no content accumulate in the candidate set permanently.
    conn = FakeConn(results=[[]])
    sg.list_due(conn, 900)
    sql = conn.calls[0]["sql"]
    assert "merge_result IS NULL" in sql
    assert "session_group" in sql
    assert "segment_count > 0" in sql, \
        "'some member produced content' must be a column test, not an S3 listing"
    assert conn.calls[0]["params"] == (900,)


def test_list_due_counts_the_lead_as_a_member():
    # The lead carries no group_id of its own — the group id IS its session id.
    # A membership test written as `group_id = %s` alone would exclude the lead,
    # so a group whose lead is still recording would look settled.
    conn = FakeConn(results=[[]])
    sg.list_due(conn, 900)
    sql = conn.calls[0]["sql"]
    assert "m.group_id = g.group_id OR m.session_id = g.group_id" in sql


def test_rearm_is_conditional_so_two_late_members_do_not_double_count():
    conn = FakeConn(results=[[{"group_id": GID}]])
    assert sg.rearm(conn, GID) is True
    sql = conn.calls[0]["sql"]
    assert "merged_at IS NOT NULL" in sql, "unconditional re-arm double-counts"
    assert "merge_count" not in sql, "the count increments at claim, not here"


def test_rearm_returns_false_when_already_clear():
    conn = FakeConn(results=[[]])
    assert sg.rearm(conn, GID) is False


def test_mark_result_terminates_the_group():
    conn = FakeConn(results=[[]])
    sg.mark_result(conn, GID, "empty")
    assert "merge_result = %s" in conn.calls[0]["sql"]
    assert conn.calls[0]["params"] == ("empty", GID)


def test_get_returns_the_row():
    conn = FakeConn(results=[[{"group_id": GID, "merged_key": "k"}]])
    assert sg.get(conn, GID)["merged_key"] == "k"
