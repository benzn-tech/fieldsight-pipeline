"""Unit: a meeting that has ended takes no new members.

Observed in production on 2026-08-07. A device left a meeting, rejoined, and
was stopped on its own a few minutes later — twice:

    03:19:36  meeting ended
    03:21:09  rejoin  -> killed 37s later
    03:22:14  rejoin  -> killed 2m17s later

The loop: `group_is_ended` answered "has this group ever been ended", which is
not the question the upload path was asking — "should THIS recording stop". A
rejoin got `groupEnded: true` on its first chunk, the auto-stop sent
`/close intent=end`, that ran `end_group` again, and the next rejoin repeated
it. The group id stayed poisoned forever.

Two doors and one guard, all three tested here: joining an ended meeting is
refused at `/open` and at upload, and a session that STARTED after the end is
never told to stop — it is a new recording carrying a stale id.
"""
import datetime
import json

import pytest

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")

SID = "9f8c1e2a4b6d47f0a1b2c3d4e5f60718"
LEAD = "b" * 32
CALLER = {"id": "u-1", "company_id": "c-1"}
CONN = object()

ENDED_AT = datetime.datetime(2026, 8, 7, 3, 19, 36, tzinfo=datetime.timezone.utc)
BEFORE = ENDED_AT - datetime.timedelta(minutes=5)
AFTER = ENDED_AT + datetime.timedelta(minutes=2)


def body_of(res):
    return json.loads(res["body"])


# ---- door 1: /open --------------------------------------------------------

def test_open_refuses_to_join_a_meeting_that_has_ended(monkeypatch):
    monkeypatch.setattr(org.meeting_session, "get",
                        lambda conn, sid: {"session_id": sid, "company_id": "c-1"})
    monkeypatch.setattr(org.meeting_session, "lead_is_joinable", lambda *a, **k: True)
    monkeypatch.setattr(org.meeting_session, "group_ended_at", lambda conn, gid: ENDED_AT)
    monkeypatch.setattr(org.meeting_session, "ensure_open",
                        lambda *a, **k: pytest.fail("must not join a finished meeting"))

    res = org.session_open(CONN, CALLER, SID, {"groupId": LEAD})
    assert res["statusCode"] == 409


def test_open_refuses_even_when_the_lead_is_not_visible(monkeypatch):
    """The end is recorded on the group, not on the lead specifically. An
    unknown lead is normally fine (the joiner may arrive first) — but if the
    group is known to have ended, that overrides."""
    monkeypatch.setattr(org.meeting_session, "get", lambda conn, sid: None)
    monkeypatch.setattr(org.meeting_session, "group_ended_at", lambda conn, gid: ENDED_AT)
    monkeypatch.setattr(org.meeting_session, "ensure_open",
                        lambda *a, **k: pytest.fail("must not join a finished meeting"))

    assert org.session_open(CONN, CALLER, SID, {"groupId": LEAD})["statusCode"] == 409


def test_open_still_joins_a_live_meeting(monkeypatch):
    seen = {}
    monkeypatch.setattr(org.meeting_session, "get",
                        lambda conn, sid: {"session_id": sid, "company_id": "c-1"})
    monkeypatch.setattr(org.meeting_session, "lead_is_joinable", lambda *a, **k: True)
    monkeypatch.setattr(org.meeting_session, "group_ended_at", lambda conn, gid: None)

    def fake(conn, sid, cid, uid, site, kind, at, group_id=None):
        seen["group_id"] = group_id
        return {"session_id": sid, "status": "open", "version": 0, "group_id": group_id}
    monkeypatch.setattr(org.meeting_session, "ensure_open", fake)

    assert org.session_open(CONN, CALLER, SID, {"groupId": LEAD})["statusCode"] == 200
    assert seen["group_id"] == LEAD


def test_a_solo_open_never_asks_about_a_group(monkeypatch):
    monkeypatch.setattr(org.meeting_session, "group_ended_at",
                        lambda *a, **k: pytest.fail("no group to ask about"))
    monkeypatch.setattr(org.meeting_session, "ensure_open",
                        lambda *a, **k: {"session_id": SID, "status": "open",
                                         "version": 0, "group_id": None})
    assert org.session_open(CONN, CALLER, SID, {"kind": "audio"})["statusCode"] == 200


# ---- door 2: the upload path ---------------------------------------------

CHUNK = f"Ben_2026-08-07_03-21-09_sid{SID}_c0001.wav"


def test_an_upload_does_not_attach_a_finished_meeting(monkeypatch):
    monkeypatch.setattr(org.meeting_session, "get", lambda conn, sid: None)
    monkeypatch.setattr(org.meeting_session, "group_ended_at", lambda conn, gid: ENDED_AT)
    monkeypatch.setattr(org.meeting_session, "ensure_open",
                        lambda *a, **k: pytest.fail("must not attach a finished meeting"))

    org._adopt_group_from_upload(
        CONN, CALLER, {"groupId": LEAD, "fileName": CHUNK}, CHUNK, "audio", None)


# ---- the guard: who the stop signal applies to ----------------------------

def _ended_for(monkeypatch, *, opened_at, own_mark=None, group_ended=ENDED_AT):
    monkeypatch.setattr(org.meeting_session, "get", lambda conn, sid: {
        "session_id": sid, "group_id": LEAD,
        "group_ended_at": own_mark, "opened_at": opened_at})
    monkeypatch.setattr(org.meeting_session, "group_ended_at", lambda conn, gid: group_ended)
    return org._group_ended_for(CONN, CHUNK)


def test_a_session_that_was_in_the_meeting_is_told_to_stop(monkeypatch):
    assert _ended_for(monkeypatch, opened_at=BEFORE) is True


def test_a_session_that_started_after_the_end_is_left_alone(monkeypatch):
    """THE loop. This recording began after the meeting was over; it is carrying
    a stale group id, not participating in a finished meeting. Stopping it made
    its own stop write another end, which poisoned the next rejoin."""
    assert _ended_for(monkeypatch, opened_at=AFTER) is False


def test_a_session_marked_ended_on_its_own_row_still_stops(monkeypatch):
    """Already marked means it WAS in the meeting — no need to compare times."""
    assert _ended_for(monkeypatch, opened_at=AFTER, own_mark=ENDED_AT) is True


def test_a_live_meeting_stops_nobody(monkeypatch):
    assert _ended_for(monkeypatch, opened_at=BEFORE, group_ended=None) is False


def test_an_unplaceable_session_errs_towards_stopping(monkeypatch):
    """No opened_at means we cannot say which side of the end it falls on.
    Stopping costs one interrupted recording; not stopping leaves a device
    recording into a meeting everyone else has left."""
    assert _ended_for(monkeypatch, opened_at=None) is True
