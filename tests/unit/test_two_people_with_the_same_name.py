"""Two logins with the same name must not share one recording folder.

WHAT HAPPENED, in prod, 2026-09-11..15. `folder_name` is globally unique (0012).
A second login named "Ben Lin" was created in the same company; create_member saw
`Ben_Lin` was taken and left folder_name NULL. Nothing else treated that as a
problem:

  * the upload route derived `{first}_{last}` when folder_name was absent, so 18
    sessions of recordings were written into the FIRST Ben Lin's folder;
  * that person's own timeline and reports stayed empty (both are keyed on
    folder_name), while their clips showed as somebody else's;
  * the finalize sweep only asks for the session's final extraction when it has a
    folder, and skipped silently -- so those sessions never got their final pass.

Each of those was invisible: no error, no warning, nothing to count. The fixes
here are therefore as much about SAYING SO as about the folder itself.

Separately, the same investigation found the confirmation-email worker recording
a send failure to S3 and logging nothing at all -- 36 rejections (SES sandbox,
unverified recipient) left no trace in the logs -- and a `skipped` result (a
recording deleted before its email) leaving the session in `finalizing` forever,
because reconcile mapped only sent/error.
"""
import json
import logging

import pytest

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")
fc = pytest.importorskip("lambda_finalize_claim", reason="requires psycopg (installed in CI)")
fin = pytest.importorskip("lambda_session_finalize", reason="requires boto3 (installed in CI)")


class _NoopCtx:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def transaction(self):
        return _NoopCtx()


class FakeS3:
    def generate_presigned_url(self, op, Params=None, ExpiresIn=0):
        self.last = {"op": op, "params": Params}
        return "https://s3.example/" + Params["Key"]


# The second Ben Lin: a real login, no folder of their own.
NAMESAKE = {"id": "42feacd1-ee2a-47e6", "cognito_sub": "sub-2", "company_id": "c-1",
            "email": "ben+test2@x.nz", "first_name": "Ben", "last_name": "Lin",
            "folder_name": None, "global_role": "pm", "archived_at": None}


def make_event(method, path, sub="sub-2", body=None):
    return {"httpMethod": method, "path": path, "queryStringParameters": None,
            "body": json.dumps(body) if body is not None else None,
            "requestContext": {"authorizer": {"claims": {"sub": sub}}}}


@pytest.fixture
def upload(monkeypatch):
    """The upload route, called by the namesake, with the folder writers recorded."""
    taken = {"Ben_Lin"}                     # held by the FIRST Ben Lin
    state = {"set": []}
    fake = FakeS3()
    monkeypatch.setattr(org, "get_connection", lambda *a, **k: FakeConn())
    monkeypatch.setattr(org, "_s3_client", fake)
    monkeypatch.setattr(org.users, "get_user_by_sub",
                        lambda conn, sub: dict(NAMESAKE) if sub == "sub-2" else None)
    monkeypatch.setattr(org.users, "get_by_folder_name_global",
                        lambda conn, folder: ({"cognito_sub": "sub-1", "folder_name": folder}
                                              if folder in taken else None))
    monkeypatch.setattr(org.users, "set_folder_name",
                        lambda conn, sub, folder: state["set"].append((sub, folder)))
    monkeypatch.setattr(org.recordings, "get_by_client_uuid", lambda c, u, cu: None)
    monkeypatch.setattr(org.recordings, "insert_pending",
                        lambda conn, **kw: {"id": "rec-1", **kw})
    return monkeypatch, state, taken


def _upload_key(body_override=None):
    res = org.lambda_handler(make_event("POST", "/api/org/recordings/upload-url", body={
        "kind": "audio", "clientUuid": "cap-1", "siteId": None,
        "fileName": "Ben_Lin_20260915_100000.wav", "contentType": "audio/wav",
        "startedAt": "2026-09-15T10:00:00Z", **(body_override or {})}), None)
    assert res["statusCode"] == 200, res
    return json.loads(res["body"])["s3Key"]


def test_THE_test_an_upload_never_lands_in_the_namesakes_folder(upload):
    """The whole defect in one line: this key used to start users/Ben_Lin/."""
    assert not _upload_key().startswith("users/Ben_Lin/")


def test_the_upload_enrols_a_folder_of_their_own(upload):
    _, state, _ = upload
    assert _upload_key().startswith("users/Ben_Lin_2/")
    assert state["set"] == [("sub-2", "Ben_Lin_2")]


def test_a_login_that_already_has_a_folder_is_left_alone(upload):
    mp, state, _ = upload
    mp.setattr(org.users, "get_user_by_sub",
               lambda conn, sub: {**NAMESAKE, "folder_name": "Ben_Lin_test2"})
    assert _upload_key().startswith("users/Ben_Lin_test2/")
    assert state["set"] == []                       # no re-enrolment on every upload


def test_an_enrolment_that_fails_still_keeps_the_two_apart(upload, caplog):
    """The upload is synchronous and never retried, so a failed enrolment must
    not cost the recording -- but it must not fall back to the shared name."""
    mp, _, _ = upload

    def boom(conn, sub, folder):
        raise RuntimeError("unique violation")

    mp.setattr(org.users, "set_folder_name", boom)
    with caplog.at_level(logging.WARNING):
        key = _upload_key()
    assert key.startswith("users/Ben_Lin_42feacd1/")
    assert not key.startswith("users/Ben_Lin/")


def test_creating_the_second_namesake_gives_them_a_folder(monkeypatch):
    """create_member used to leave folder_name unset here, which is where the
    whole thing started."""
    monkeypatch.setattr(org.users, "first_officer_or_member",
                        lambda conn, cid: {"id": "u-officer"})
    monkeypatch.setattr(org.report_templates, "seed_starters",
                        lambda conn, cid, author: 0)
    admin = {**NAMESAKE, "cognito_sub": "sub-1", "global_role": "admin",
             "folder_name": "Admin"}
    state = {"set": []}
    monkeypatch.setattr(org, "get_connection", lambda *a, **k: FakeConn())
    monkeypatch.setattr(org.users, "get_user_by_sub",
                        lambda conn, sub: dict(admin) if sub == "sub-1" else None)
    monkeypatch.setattr(org.users, "upsert_user",
                        lambda conn, sub, email, **kw: {"id": "u-9", "cognito_sub": sub,
                                                        "folder_name": None})
    monkeypatch.setattr(org.users, "get_by_folder_name_global",
                        lambda conn, folder: ({"cognito_sub": "sub-other"}
                                              if folder == "Ben_Lin" else None))
    monkeypatch.setattr(org.users, "set_folder_name",
                        lambda conn, sub, folder: state["set"].append((sub, folder)))
    monkeypatch.setattr(org, "cognito", lambda: _FakeCognito())
    res = org.lambda_handler(make_event("POST", "/api/org/members", sub="sub-1", body={
        "email": "ben+test2@x.nz", "first_name": "Ben", "last_name": "Lin"}), None)
    assert res["statusCode"] == 201, res
    assert state["set"] == [("sub-new", "Ben_Lin_2")]
    assert json.loads(res["body"])["user"]["folder_name"] == "Ben_Lin_2"


class _FakeCognito:
    class exceptions:
        class UsernameExistsException(Exception):
            pass

    def admin_create_user(self, **kw):
        return {"User": {"Attributes": [{"Name": "sub", "Value": "sub-new"}]}}


# ---- saying so ------------------------------------------------------------

def test_finalize_says_when_a_session_has_no_recording_folder(monkeypatch, caplog):
    """Silent before: the session simply never got its final extraction."""
    monkeypatch.setattr(fc.meeting_session, "claim_finalize",
                        lambda conn, sid, v: {"session_id": sid, "user_id": "u-9", "version": v})
    monkeypatch.setattr(fc.meeting_session, "mark_failed", lambda conn, sid: None)
    asked = []
    with caplog.at_level(logging.WARNING):
        fc.finalize_claim("CONN", "s1", 1,
                          resolve_context=lambda conn, row: {"recipient": "", "folder": None,
                                                             "date": "2026-09-15"},
                          read_rolling=lambda *a: {},
                          request_extraction=lambda *a: asked.append(a))
    assert asked == []                                   # unchanged: nothing to ask for
    assert any("no recording folder" in r.getMessage() for r in caplog.records)


def test_a_rejected_email_is_logged_not_only_filed(monkeypatch, caplog):
    """36 prod rejections left no log line at all — only a file in S3."""
    written = []

    def boom(*a, **k):
        raise RuntimeError("MessageRejected: Email address is not verified")

    monkeypatch.setattr(fin, "_complete_summary", lambda artifact: None)
    monkeypatch.setattr(fin, "_session_was_deleted", lambda artifact: False)
    with caplog.at_level(logging.ERROR):
        out = fin.process_finalize_request(
            {"recipient": "someone@example.nz", "sessionId": "s1", "date": "2026-09-15"},
            send=boom, write_result=lambda sid, res: written.append(res))
    assert out["status"] == "error" and written[0]["status"] == "error"
    assert any("send failed" in r.getMessage() for r in caplog.records)


def test_a_session_nobody_will_email_is_settled_not_left_finalizing(monkeypatch):
    """A recording deleted before its email writes {"status": "skipped"}. Only
    sent/error were mapped, so the session stayed `finalizing` for good."""
    monkeypatch.setattr(fc.meeting_session, "list_finalizing",
                        lambda conn: [{"session_id": "s1"}])
    sent, failed = [], []
    monkeypatch.setattr(fc.meeting_session, "mark_sent", lambda conn, sid: sent.append(sid))
    monkeypatch.setattr(fc.meeting_session, "mark_failed", lambda conn, sid: failed.append(sid))
    out = fc.reconcile("CONN", read_result=lambda sid: {"status": "skipped",
                                                        "reason": "recording deleted"})
    assert out == [("s1", "skipped")]
    assert sent == ["s1"] and failed == []
