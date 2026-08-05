"""Unit: ending a multi-device meeting for everyone (spec 2026-08-04, Task 13).

Kept out of test_org_api_session_lifecycle.py deliberately. That file is being
appended to by a parallel change (the stale-lead guard), and two branches
appending at the same end-of-file is a conflict with no content behind it —
both sides always want to keep both. A separate file makes the two independent.
"""
import json

import pytest

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")

SID = "9f8c1e2a4b6d47f0a1b2c3d4e5f60718"
LEAD = "b" * 32
CALLER = {"id": "u-1", "company_id": "c-1"}
CONN = object()


def body_of(res):
    return json.loads(res["body"])


def _patch_get(monkeypatch, row):
    monkeypatch.setattr(org.meeting_session, "get", lambda conn, sid: row)


def _patch_close(monkeypatch, row, ended):
    _patch_get(monkeypatch, row)
    monkeypatch.setattr(org.meeting_session, "mark_pending_close",
                        lambda conn, sid, at, intent: {"status": "pending_close", "version": 1})
    monkeypatch.setattr(org.meeting_session, "end_group",
                        lambda conn, gid: ended.append(gid) or 1)


def test_a_deliberate_end_ends_the_whole_group(monkeypatch):
    ended = []
    _patch_close(monkeypatch, {"user_id": "u-1", "status": "open", "version": 0,
                               "group_id": LEAD}, ended)
    assert org.session_close(CONN, CALLER, SID, {"intent": "end"})["statusCode"] == 200
    assert ended == [LEAD]


def test_the_lead_ending_uses_its_own_id_as_the_group(monkeypatch):
    """The lead carries no group_id — the group id IS its session id."""
    ended = []
    _patch_close(monkeypatch, {"user_id": "u-1", "status": "open", "version": 0,
                               "group_id": None}, ended)
    org.session_close(CONN, CALLER, SID, {"intent": "end"})
    assert ended == [SID]


def test_an_idle_stop_ends_nothing(monkeypatch):
    """Putting the device down is not ending the meeting. Only a deliberate End
    speaks for everyone else."""
    ended = []
    _patch_close(monkeypatch, {"user_id": "u-1", "status": "open", "version": 0,
                               "group_id": LEAD}, ended)
    org.session_close(CONN, CALLER, SID, {"intent": "idle"})
    assert ended == []
