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


# ---- the group riding on the upload (offline joins) -----------------------
#
# /open is fire-and-forget and being offline is normal on a site. If the group
# only travelled on that call, joining in a shed with no signal would silently
# produce two unmerged recordings — the exact situation multi-device capture
# exists for. The upload is the one thing guaranteed to arrive eventually.

CHUNK_NAME = f"Ben_2026-08-06_09-00-00_sid{SID}_c0001.wav"


def _capture_ensure_open(monkeypatch, seen):
    def fake(conn, sid, company_id, user_id, site_id, kind, opened_at, group_id=None):
        seen.append({"session_id": sid, "group_id": group_id, "company_id": company_id,
                     "opened_at": opened_at})
        return {"session_id": sid, "status": "open", "version": 0, "group_id": group_id}
    monkeypatch.setattr(org.meeting_session, "ensure_open", fake)


def _upload(monkeypatch, body, seen, lead=None):
    _capture_ensure_open(monkeypatch, seen)
    monkeypatch.setattr(org.meeting_session, "get", lambda conn, sid: lead)
    monkeypatch.setattr(org.meeting_session, "group_ended_at", lambda conn, gid: None)
    org._adopt_group_from_upload(CONN, CALLER, body, body.get("fileName"), "audio", None)


def test_the_group_is_recorded_against_the_session_in_the_filename(monkeypatch):
    seen = []
    _upload(monkeypatch, {"groupId": LEAD, "fileName": CHUNK_NAME}, seen)
    assert seen[0]["session_id"] == SID and seen[0]["group_id"] == LEAD
    assert seen[0]["company_id"] == "c-1"


def test_a_solo_upload_costs_no_query(monkeypatch):
    """The overwhelming majority of uploads, on the synchronous no-retry route
    where added latency turns into lost data."""
    seen = []
    monkeypatch.setattr(org.meeting_session, "get",
                        lambda *a, **k: pytest.fail("must not touch the DB"))
    _capture_ensure_open(monkeypatch, seen)
    org._adopt_group_from_upload(CONN, CALLER, {"fileName": CHUNK_NAME}, CHUNK_NAME,
                                 "audio", None)
    assert seen == []


def test_a_photo_or_legacy_filename_is_skipped(monkeypatch):
    seen = []
    _upload(monkeypatch, {"groupId": LEAD, "fileName": "Ben_2026-08-06_09-00-00.jpg"}, seen)
    assert seen == []


def test_a_malformed_group_is_ignored_not_stored(monkeypatch):
    seen = []
    _upload(monkeypatch, {"groupId": "not-a-session", "fileName": CHUNK_NAME}, seen)
    assert seen == []


def test_a_cross_company_group_is_refused(monkeypatch):
    """Skipped rather than failing the upload: refusing would cost the audio,
    and /open already answers 403 for this."""
    seen = []
    _upload(monkeypatch, {"groupId": LEAD, "fileName": CHUNK_NAME}, seen,
            lead={"session_id": LEAD, "company_id": "other-co"})
    assert seen == []


def test_an_unknown_lead_is_still_accepted(monkeypatch):
    """The joiner can reach us before the lead does — that is the whole point of
    not depending on call ordering."""
    seen = []
    _upload(monkeypatch, {"groupId": LEAD, "fileName": CHUNK_NAME}, seen, lead=None)
    assert len(seen) == 1 and seen[0]["group_id"] == LEAD


def test_a_failure_never_breaks_the_upload(monkeypatch):
    """A lost group costs a merge; a lost upload costs the audio."""
    monkeypatch.setattr(org.meeting_session, "get", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db")))
    org._adopt_group_from_upload(CONN, CALLER, {"groupId": LEAD, "fileName": CHUNK_NAME},
                                 CHUNK_NAME, "audio", None)


def test_the_session_start_comes_from_the_filename_not_the_chunk(monkeypatch):
    """The body's startedAt is THIS CHUNK's start; the filename's timestamp is
    the session's, identical on every chunk.

    After an offline day the chunks arrive in whatever order the queue drains,
    so using the body's would let an arbitrary chunk define the session start —
    and ensure_open COALESCEs, so the wrong value would never be corrected.
    A late chunk of a two-hour meeting would move the session start by two
    hours, which is enough to change the day the report is filed under.
    """
    seen = []
    _capture_ensure_open(monkeypatch, seen)
    monkeypatch.setattr(org.meeting_session, "get", lambda conn, sid: None)
    monkeypatch.setattr(org.meeting_session, "group_ended_at", lambda conn, gid: None)
    late_chunk = f"Ben_2026-08-06_09-00-00_sid{SID}_c0050.wav"
    org._adopt_group_from_upload(
        CONN, CALLER,
        {"groupId": LEAD, "fileName": late_chunk, "startedAt": "2026-08-06T11:25:00"},
        late_chunk, "audio", None)
    opened = str(seen[0]["opened_at"])
    assert "09:00" in opened, f"expected the session start (09:00), got {opened!r}"
    assert "11:25" not in opened, "took the chunk's own start"

