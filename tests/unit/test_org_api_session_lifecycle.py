"""POST /api/org/sessions/{id}/open|close — voice-timeliness session lifecycle
endpoints (spec §3.5). Handlers are called directly with repo functions
monkeypatched on the org module (same style as test_org_api_sessions.py)."""
import json

import pytest

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")

SID = "9f8c1e2a4b6d47f0a1b2c3d4e5f60718"
CALLER = {"id": "u-1", "company_id": "c-1"}
CONN = object()  # handlers only pass it through to the (patched) repos


def body_of(res):
    return json.loads(res["body"])


# ---- open -----------------------------------------------------------------

def test_open_creates_and_returns_status(monkeypatch):
    seen = {}
    def fake_ensure_open(conn, sid, company_id, user_id, site_id, kind, started_at):
        seen.update(sid=sid, company_id=company_id, user_id=user_id, kind=kind)
        return {"session_id": sid, "status": "open", "version": 0}
    monkeypatch.setattr(org.meeting_session, "ensure_open", fake_ensure_open)

    res = org.session_open(CONN, CALLER, SID, {"kind": "audio", "startedAt": "2026-07-28T14:03:00"})
    assert res["statusCode"] == 200
    assert body_of(res) == {"sessionId": SID, "status": "open", "version": 0}
    assert seen == {"sid": SID, "company_id": "c-1", "user_id": "u-1", "kind": "audio"}


def test_open_rejects_bad_session_id(monkeypatch):
    res = org.session_open(CONN, CALLER, "NOThex", {})
    assert res["statusCode"] == 400


def test_open_rejects_bad_kind(monkeypatch):
    res = org.session_open(CONN, CALLER, SID, {"kind": "hologram"})
    assert res["statusCode"] == 400


def test_open_rejects_inaccessible_site(monkeypatch):
    monkeypatch.setattr(org.sites, "get_site", lambda conn, sid: {"company_id": "other-co"})
    res = org.session_open(CONN, CALLER, SID, {"siteId": "s-1"})
    assert res["statusCode"] == 403


def test_open_malformed_body_is_400(monkeypatch):
    assert org.session_open(CONN, CALLER, SID, None)["statusCode"] == 400


# ---- close ----------------------------------------------------------------

def _patch_get(monkeypatch, row):
    monkeypatch.setattr(org.meeting_session, "get", lambda conn, sid: row)


def test_close_idle_records_pending_with_full_grace(monkeypatch):
    _patch_get(monkeypatch, {"user_id": "u-1", "status": "open", "version": 2})
    monkeypatch.setattr(org.meeting_session, "mark_pending_close",
                        lambda conn, sid, ended_at, intent: {"status": "pending_close", "version": 3})
    res = org.session_close(CONN, CALLER, SID, {"intent": "idle", "endedAt": "2026-07-28T14:19:00"})
    assert res["statusCode"] == 200
    b = body_of(res)
    assert b["status"] == "pending_close" and b["version"] == 3
    assert b["graceSeconds"] == org.STOP_GRACE_SECONDS == 30


def test_close_end_finalizes_immediately_grace_zero(monkeypatch):
    _patch_get(monkeypatch, {"user_id": "u-1", "status": "open", "version": 1})
    monkeypatch.setattr(org.meeting_session, "mark_pending_close",
                        lambda conn, sid, ended_at, intent: {"status": "pending_close", "version": 2})
    res = org.session_close(CONN, CALLER, SID, {"intent": "end"})
    assert body_of(res)["graceSeconds"] == 0


def test_close_defaults_intent_to_idle(monkeypatch):
    _patch_get(monkeypatch, {"user_id": "u-1", "status": "open", "version": 0})
    monkeypatch.setattr(org.meeting_session, "mark_pending_close",
                        lambda conn, sid, ended_at, intent: {"status": "pending_close", "version": 1})
    res = org.session_close(CONN, CALLER, SID, {})
    assert body_of(res)["graceSeconds"] == 30


def test_close_rejects_bad_intent(monkeypatch):
    _patch_get(monkeypatch, {"user_id": "u-1", "status": "open", "version": 0})
    assert org.session_close(CONN, CALLER, SID, {"intent": "abort"})["statusCode"] == 400


def test_close_not_found_is_404(monkeypatch):
    _patch_get(monkeypatch, None)
    assert org.session_close(CONN, CALLER, SID, {})["statusCode"] == 404


def test_close_other_users_session_is_403(monkeypatch):
    _patch_get(monkeypatch, {"user_id": "someone-else", "status": "open", "version": 0})
    assert org.session_close(CONN, CALLER, SID, {})["statusCode"] == 403


def test_close_already_sent_is_idempotent_noop(monkeypatch):
    _patch_get(monkeypatch, {"user_id": "u-1", "status": "sent", "version": 5})
    res = org.session_close(CONN, CALLER, SID, {"intent": "end"})
    assert res["statusCode"] == 200
    assert body_of(res)["noop"] is True
