"""Company offboarding, Task 9: the stop / summary email of an inactive company's
session is not sent (spec 2026-11-01 §5.5); the finalize work itself still runs.

The decision is made once, where the recipient and the site are resolved:
lambda_finalize_claim._resolve_context. Every enqueuer of a stop email goes through
it -- the claim, the backstop, and item-writer's final / group-merge ("updated")
paths -- and the send worker itself (lambda_session_finalize) has no database.
Suppressed: the recipient is blanked and `emailSuppressed` says why; the claim then
settles the session (mark_sent, like a voice note) so neither item-writer's final
email (it requires status 'finalizing') nor the backstop ever picks it up."""
import logging
import os

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")

import pytest

import lambda_finalize_claim as fc
import tenancy_status

ROW = {"session_id": "sid-1", "user_id": "u1", "site_id": "site-B", "version": 3}


def _stub_resolution(monkeypatch, status_by_site=None, status_by_company=None):
    """suppress_reason with the real flag/log decision and a fake status lookup."""
    seen = []

    def fake(conn, *, site_id=None, company_id=None, where=""):
        seen.append({"site_id": site_id, "company_id": company_id, "where": where})
        if site_id is not None and site_id in (status_by_site or {}):
            company, status = (status_by_site or {})[site_id]
        else:
            company, status = company_id, (status_by_company or {}).get(company_id, "active")
        return tenancy_status.suppress_decision(company, status, where=where, site_id=site_id)

    monkeypatch.setattr(fc.tenancy_status, "suppress_reason", fake)
    monkeypatch.setattr(fc.users, "get_by_id", lambda conn, uid: {
        "email": "rec@home.nz", "folder_name": "Rec_F", "company_id": "co-home"})
    monkeypatch.setattr(fc.sites, "get_site", lambda conn, sid: {"name": "Tower B"})
    return seen


def _shadow(caplog):
    return [r for r in caplog.records if "COMPANY_GATE_WOULD_SUPPRESS" in r.getMessage()]


# ---- _resolve_context: the decision ----------------------------------------------

def test_inactive_site_company_blanks_the_recipient(monkeypatch, caplog):
    monkeypatch.setenv("COMPANY_STATUS_ENFORCE", "on")
    seen = _stub_resolution(monkeypatch, status_by_site={"site-B": ("co-B", "suspended")})
    ctx = fc._resolve_context("CONN", ROW)
    assert ctx["recipient"] is None and ctx["emailSuppressed"] == "company_inactive"
    # Finalize work still has everything it needs.
    assert ctx["folder"] == "Rec_F" and ctx["siteName"] == "Tower B"
    # Site first, recipient's home company as the fallback.
    assert seen == [{"site_id": "site-B", "company_id": "co-home", "where": "stop-email"}]
    assert not _shadow(caplog)


def test_active_site_company_sends_as_before(monkeypatch):
    monkeypatch.setenv("COMPANY_STATUS_ENFORCE", "on")
    # The recipient's HOME company is inactive, but the site's company is active: the
    # site decides, so the email goes.
    _stub_resolution(monkeypatch, status_by_site={"site-B": ("co-B", "active")},
                     status_by_company={"co-home": "suspended"})
    ctx = fc._resolve_context("CONN", ROW)
    assert ctx["recipient"] == "rec@home.nz" and "emailSuppressed" not in ctx


def test_flag_off_sends_and_logs(monkeypatch, caplog):
    monkeypatch.setenv("COMPANY_STATUS_ENFORCE", "off")
    caplog.set_level(logging.WARNING)
    _stub_resolution(monkeypatch, status_by_site={"site-B": ("co-B", "exited")})
    ctx = fc._resolve_context("CONN", ROW)
    assert ctx["recipient"] == "rec@home.nz" and "emailSuppressed" not in ctx
    lines = _shadow(caplog)
    assert len(lines) == 1
    assert "where=stop-email" in lines[0].getMessage()
    assert "company=co-B" in lines[0].getMessage() and "site=site-B" in lines[0].getMessage()


def test_site_less_session_uses_the_recipients_home_company(monkeypatch):
    monkeypatch.setenv("COMPANY_STATUS_ENFORCE", "on")
    seen = _stub_resolution(monkeypatch, status_by_company={"co-home": "suspended"})
    ctx = fc._resolve_context("CONN", {**ROW, "site_id": None})
    assert ctx["recipient"] is None and ctx["emailSuppressed"] == "company_inactive"
    assert seen[0]["site_id"] is None and seen[0]["company_id"] == "co-home"


def test_a_status_read_failure_sends_rather_than_kill_the_tick(monkeypatch, caplog):
    monkeypatch.setenv("COMPANY_STATUS_ENFORCE", "on")
    _stub_resolution(monkeypatch)

    def boom(conn, **kw):
        raise RuntimeError("db hiccup")
    monkeypatch.setattr(fc.tenancy_status, "suppress_reason", boom)
    ctx = fc._resolve_context("CONN", ROW)
    assert ctx["recipient"] == "rec@home.nz"
    assert "could not read the company status" in caplog.text


# ---- finalize_claim: the email path is skipped, the finalize work is not ------------

def test_claim_of_a_suppressed_session_settles_it_without_email(monkeypatch, caplog):
    monkeypatch.setattr(fc.meeting_session, "claim_finalize", lambda c, s, v: dict(ROW))
    sent, failed, requested = [], [], []
    monkeypatch.setattr(fc.meeting_session, "mark_sent", lambda c, s: sent.append(s))
    monkeypatch.setattr(fc.meeting_session, "mark_failed", lambda c, s: failed.append(s))
    caplog.set_level(logging.WARNING)
    out = fc.finalize_claim(
        "CONN", "sid-1", 3,
        resolve_context=lambda c, r: {"recipient": None, "emailSuppressed": "company_inactive",
                                      "folder": "Rec_F", "date": "2026-11-01"},
        read_rolling=lambda *a: {},
        request_extraction=lambda *a, **k: requested.append(a))
    assert out == {"status": "suppressed", "sessionId": "sid-1", "reason": "company_inactive"}
    # The final extraction (what puts the session on the website) is still requested.
    assert requested == [("sid-1", "Rec_F", "2026-11-01")]
    # Settled, not failed: nothing waits for an email any more.
    assert sent == ["sid-1"] and failed == []
    assert "COMPANY_EMAIL_SUPPRESSED" in caplog.text


def test_claim_is_unchanged_when_not_suppressed(monkeypatch):
    monkeypatch.setattr(fc.meeting_session, "claim_finalize", lambda c, s, v: dict(ROW))
    monkeypatch.setattr(fc.meeting_session, "mark_sent",
                        lambda c, s: pytest.fail("must not settle a session that is mailed"))
    out = fc.finalize_claim("CONN", "sid-1", 3,
                            resolve_context=lambda c, r: {"recipient": "rec@home.nz",
                                                          "folder": "Rec_F", "date": "2026-11-01"},
                            read_rolling=lambda *a: {}, request_extraction=lambda *a, **k: None)
    assert out["status"] == "waiting"


def test_the_settled_session_gets_no_final_email_from_item_writer(monkeypatch):
    """item-writer is not edited (R6): its final email requires status 'finalizing',
    which the suppressed claim has already left."""
    iw = pytest.importorskip("lambda_item_writer")
    monkeypatch.setattr(iw.meeting_session, "get",
                        lambda conn, sid: {**ROW, "status": "sent"})
    assert iw._final_email_context("CONN", "sid1-xyz", {}, "2026-11-01") is None


def test_group_merge_updated_email_is_not_enqueued_for_a_suppressed_member(monkeypatch):
    """item-writer's merged-meeting email resolves each member through _resolve_context;
    a suppressed member comes back with no recipient and is not enqueued."""
    monkeypatch.setenv("COMPANY_STATUS_ENFORCE", "on")
    iw = pytest.importorskip("lambda_item_writer")
    _stub_resolution(monkeypatch, status_by_site={"site-B": ("co-B", "suspended"),
                                                  "site-A": ("co-A", "active")})
    rows = {"m1": {**ROW, "session_id": "m1"},
            "m2": {**ROW, "session_id": "m2", "site_id": "site-A"}}
    monkeypatch.setattr(iw.meeting_session, "get", lambda conn, sid: rows[sid])
    artifact = {"memberSessions": ["m1", "m2"], "groupId": "g1", "topics": []}
    contexts = iw._resolve_member_contexts("CONN", artifact)
    put = []
    iw._enqueue_updated_emails(artifact, contexts, put=lambda key, body, **k: put.append(key))
    assert put == ["session_finalize_requests/m2-updated.json"]
