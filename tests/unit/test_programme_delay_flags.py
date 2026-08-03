"""
Tests for src/repositories/programme_delay_flags.py — Task 3 of the programme
time-window plan. Spec §10, scenario D.

A site manager knows a date has slipped before the plan does. They cannot
change a contract date — the next import would overwrite it — so they raise a
flag carrying the reason and the expected new date, and it surfaces to the PM
who reschedules in P6/MSP and re-imports.

That makes the flag the only carrier of the signal. A flag that is malformed,
invisible, or closable by the person who raised it is worse than none at all,
because the site manager believes they have reported the problem.
"""
import pytest

from repositories import programme_delay_flags as repo

from tests.unit.test_programme_tasks_repo import FakeConn

TASK = "11111111-1111-1111-1111-111111111111"
USER = "33333333-3333-3333-3333-333333333333"
SITE = "44444444-4444-4444-4444-444444444444"


def test_raise_flag_records_reason_and_expected_end():
    conn = FakeConn([{"id": "f1"}])
    repo.raise_flag(conn, task_id=TASK, raised_by=USER,
                    reason="concrete pump unavailable", expected_end="2026-05-08")
    params = conn.calls[0]["params"]
    assert "concrete pump unavailable" in params
    assert "2026-05-08" in params
    assert TASK in params and USER in params


def test_raise_flag_requires_a_reason():
    """A flag with no reason is noise the PM cannot act on."""
    conn = FakeConn([])
    with pytest.raises(ValueError):
        repo.raise_flag(conn, task_id=TASK, raised_by=USER, reason="  ",
                        expected_end=None)
    assert conn.calls == []


def test_raise_flag_rejects_a_missing_reason():
    conn = FakeConn([])
    with pytest.raises(ValueError):
        repo.raise_flag(conn, task_id=TASK, raised_by=USER, reason=None,
                        expected_end=None)
    assert conn.calls == []


def test_raise_flag_trims_the_reason():
    conn = FakeConn([{"id": "f1"}])
    repo.raise_flag(conn, task_id=TASK, raised_by=USER,
                    reason="  pump unavailable  ", expected_end=None)
    assert "pump unavailable" in conn.calls[0]["params"]


def test_expected_end_is_optional():
    """A site manager often knows a task will slip before knowing by how
    much. Requiring a date would push them to invent one."""
    conn = FakeConn([{"id": "f1"}])
    repo.raise_flag(conn, task_id=TASK, raised_by=USER, reason="rain",
                    expected_end=None)
    assert conn.calls, "a flag with no expected date is still a flag"


def test_list_for_site_defaults_to_open_flags():
    conn = FakeConn([[]])
    repo.list_for_site(conn, SITE)
    assert "open" in conn.calls[0]["params"]


def test_list_for_site_joins_through_to_the_site():
    """Flags hang off tasks, tasks off programmes, programmes off sites —
    the ACL is applied on site_id, so the query has to reach it."""
    conn = FakeConn([[]])
    repo.list_for_site(conn, SITE)
    sql = conn.calls[0]["sql"]
    assert "programme_tasks" in sql and "programmes" in sql
    assert SITE in conn.calls[0]["params"]


def test_list_for_site_can_return_every_state():
    conn = FakeConn([[]])
    repo.list_for_site(conn, SITE, state=None)
    assert "open" not in (conn.calls[0]["params"] or ())


def test_set_state_rejects_an_unknown_state():
    conn = FakeConn([])
    with pytest.raises(ValueError):
        repo.set_state(conn, "f1", "banana")
    assert conn.calls == []


def test_set_state_accepts_the_three_real_states():
    for state in ("open", "acknowledged", "resolved"):
        conn = FakeConn([{"id": "f1", "state": state}])
        repo.set_state(conn, "f1", state)
        assert state in conn.calls[0]["params"]


def test_resolving_stamps_resolved_at():
    conn = FakeConn([{"id": "f1"}])
    repo.set_state(conn, "f1", "resolved")
    assert "resolved_at" in conn.calls[0]["sql"]


def test_acknowledging_does_not_stamp_resolved_at():
    """Acknowledged means the PM has seen it, not that the slip is fixed."""
    conn = FakeConn([{"id": "f1"}])
    repo.set_state(conn, "f1", "acknowledged")
    sql = conn.calls[0]["sql"]
    assert "resolved_at = now()" not in sql


def test_get_returns_none_when_missing():
    conn = FakeConn([[]])
    assert repo.get(conn, "no-such-flag") is None
