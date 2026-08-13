"""Unit: `complete` must not stamp uploaded_at for bytes that never arrived.

`POST /org/recordings/{id}/complete` took the client's word, and the client
deliberately does not check either — `UploadWorker` ignores the PUT result and
names this endpoint as the real verdict. That contract was never implemented,
so a stalled PUT followed by an accepted `complete` lost the recording
silently: 141 of 938 prod rows claim an object that has no versions and no
delete markers, i.e. one that never existed.

Spec: docs/superpowers/specs/2026-08-09-upload-completion-verified.md

Two things are easy to get wrong and are pinned here:

* **`list_objects_v2`, not `HeadObject`.** The org-api role's `s3:ListBucket`
  grant is conditioned on `s3:prefix`. A HeadObject request carries no prefix,
  so a missing object comes back 403 and is indistinguishable from a broken
  permission — the third recurrence of BUG-43 waiting to happen.
* **A guard that cannot read its input must not stop uploads.** Any unexpected
  S3 error accepts the completion in every mode.
"""
import json

import pytest

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")

KEY = "users/Ben/audio/2026-08-07/Ben_2026-08-07_09-00-00_c0001.wav"

CALLER = {"id": "u-1", "cognito_sub": "sub-1", "company_id": "c-1", "email": "a@x.nz",
          "first_name": "Ada", "last_name": "L", "folder_name": "Ada_L",
          "global_role": "pm", "created_at": "2026-07-04", "archived_at": None}


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
    """Holds a listing keyed by full object key. `head_object` is deliberately
    a landmine: reaching for it is the bug this design avoids."""

    def __init__(self, objects=None, raises=None):
        self.objects = dict(objects or {})
        self.raises = raises
        self.calls = []

    def list_objects_v2(self, Bucket=None, Prefix=None, MaxKeys=None):
        self.calls.append(("list_objects_v2", Bucket, Prefix, MaxKeys))
        if self.raises is not None:
            raise self.raises
        contents = [{"Key": k, "Size": v} for k, v in sorted(self.objects.items())
                    if k.startswith(Prefix or "")]
        return {"Contents": contents[:MaxKeys or 1000]}

    def head_object(self, **kw):
        raise AssertionError("HeadObject cannot tell 404 from 403 for this role")


def make_event(body=None, rec_id="rec-1"):
    return {"httpMethod": "POST", "path": f"/api/org/recordings/{rec_id}/complete",
            "queryStringParameters": None,
            "body": json.dumps(body if body is not None else {}),
            "requestContext": {"authorizer": {"claims": {"sub": "sub-1"}}}}


def body_of(res):
    return json.loads(res["body"])


@pytest.fixture
def wired(monkeypatch):
    """org-api with a fake connection, caller and S3, and a recording row that
    exists. Returns (monkeypatch, marked) where `marked` records whether
    mark_uploaded ran — the whole point being that it must not, when the object
    is absent and the mode is enforce."""
    monkeypatch.setattr(org, "get_connection", lambda *a, **k: FakeConn())
    monkeypatch.setattr(org.users, "get_user_by_sub",
                        lambda conn, sub: dict(CALLER) if sub == "sub-1" else None)
    monkeypatch.setattr(org, "S3_BUCKET", "fieldsight-data")
    monkeypatch.setattr(org.recordings, "get_by_id",
                        lambda conn, rid: {"id": rid, "company_id": "c-1", "s3_key": KEY,
                                           "size_bytes": None, "uploaded_at": None})
    marked = {}

    def fake_mark(conn, rid, cid, sz=None, gps_track=None):
        marked.update(rid=rid, cid=cid, sz=sz)
        return {"id": rid, "s3_key": KEY}

    monkeypatch.setattr(org.recordings, "mark_uploaded", fake_mark)
    monkeypatch.setattr(org.meeting_session, "get", lambda conn, sid: None)
    return monkeypatch, marked


def _run(mp, mode, s3_fake, body=None):
    mp.setattr(org, "UPLOAD_VERIFY_MODE", mode)
    mp.setattr(org, "_s3_client", s3_fake)
    return org.lambda_handler(make_event(body), None)


def test_off_is_the_old_behaviour_and_does_not_even_look(wired):
    """Rollback has to be a true rollback: no S3 call, no extra query, and the
    same 200 as before this change existed."""
    mp, marked = wired
    s3 = FakeS3(objects={})
    res = _run(mp, "off", s3)
    assert res["statusCode"] == 200 and body_of(res) == {"ok": True}
    assert marked["rid"] == "rec-1"
    assert s3.calls == []


def test_a_present_object_completes(wired):
    mp, marked = wired
    s3 = FakeS3(objects={KEY: 1024})
    res = _run(mp, "enforce", s3, body={"sizeBytes": 1024})
    assert res["statusCode"] == 200 and body_of(res) == {"ok": True}
    assert marked["rid"] == "rec-1"


def test_verification_lists_the_exact_key_under_its_own_prefix(wired):
    """The call shape is the fix: list_objects_v2 sends s3:prefix, which is the
    only way this role's ListBucket condition can be satisfied."""
    mp, _ = wired
    s3 = FakeS3(objects={KEY: 1024})
    _run(mp, "enforce", s3)
    assert s3.calls == [("list_objects_v2", "fieldsight-data", KEY, 1)]


def test_an_absent_object_is_rejected_under_enforce(wired):
    mp, marked = wired
    s3 = FakeS3(objects={})
    res = _run(mp, "enforce", s3)
    assert res["statusCode"] == 409
    assert marked == {}, "uploaded_at must stay NULL so the device re-sends"


def test_a_sibling_sharing_the_prefix_is_not_the_object(wired):
    """`Prefix=key` also matches `key + anything`. Only an exact match counts,
    or a half-written `.part` file would certify the real one as delivered."""
    mp, marked = wired
    s3 = FakeS3(objects={KEY + ".part": 12})
    res = _run(mp, "enforce", s3)
    assert res["statusCode"] == 409
    assert marked == {}


def test_observe_completes_anyway_and_says_what_enforce_would_have_done(wired, caplog):
    """Prod ships observe first on purpose: the cost of a wrong enforce is every
    upload rejected, against a measured 0.9% loss from waiting one more day."""
    mp, marked = wired
    s3 = FakeS3(objects={})
    with caplog.at_level("WARNING"):
        res = _run(mp, "observe", s3)
    assert res["statusCode"] == 200 and body_of(res) == {"ok": True}
    assert marked["rid"] == "rec-1"
    assert any("absent" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("mode", ["observe", "enforce"])
def test_an_unreadable_bucket_never_stops_an_upload(wired, mode, caplog):
    """A guard that cannot read its input is not evidence of absence. This is
    the exact shape that turned a permission slip into a total outage in
    BUG-43 / PR#288."""
    mp, marked = wired
    s3 = FakeS3(raises=RuntimeError("AccessDenied"))
    with caplog.at_level("WARNING"):
        res = _run(mp, mode, s3)
    assert res["statusCode"] == 200 and body_of(res) == {"ok": True}
    assert marked["rid"] == "rec-1"


def test_a_size_that_disagrees_is_logged_and_never_rejected(wired, caplog):
    """A truncated upload is real, silent quality loss — but a systematic
    off-by-something in how either side counts would reject every upload, and
    that is not a risk worth taking in the same release as the existence check."""
    mp, marked = wired
    s3 = FakeS3(objects={KEY: 999})
    with caplog.at_level("WARNING"):
        res = _run(mp, "enforce", s3, body={"sizeBytes": 1024})
    assert res["statusCode"] == 200
    assert marked["rid"] == "rec-1"
    assert any("size" in r.getMessage().lower() for r in caplog.records)


def test_an_unknown_recording_is_still_a_404(wired):
    mp, _ = wired
    mp.setattr(org.recordings, "get_by_id", lambda conn, rid: None)
    res = _run(mp, "enforce", FakeS3(objects={KEY: 1}))
    assert res["statusCode"] == 404


def test_another_company_cannot_complete_this_recording(wired):
    """The row lookup added for verification must not become a way around the
    company scoping mark_uploaded used to enforce on its own."""
    mp, marked = wired
    mp.setattr(org.recordings, "get_by_id",
               lambda conn, rid: {"id": rid, "company_id": "c-OTHER", "s3_key": KEY})
    res = _run(mp, "enforce", FakeS3(objects={KEY: 1}))
    assert res["statusCode"] == 404
    assert marked == {}


def test_a_verified_upload_says_so(wired, caplog):
    """The check must leave a mark when it PASSES, not only when it fails.

    Measured 2026-08-13: 1078 uploads reached the prod bucket in a day and the log carried
    zero `upload-verify` lines. That reads as "nothing was lost" and it reads exactly the
    same as "the check never ran" — and the second is what happened three times over in the
    batching feature, each time behind a missing IAM grant. `enforce` is gated on a day of
    observe logs being explainable, and a silent day explains nothing.

    So: one line per verified upload, carrying the mode. Then `ok` count against
    `object absent` count is the measurement, and a day with neither is a fault report.
    """
    import logging
    mp, marked = wired
    s3 = FakeS3(objects={KEY: 1024})
    with caplog.at_level(logging.INFO):
        res = _run(mp, "observe", s3, body={"sizeBytes": 1024})
    assert res["statusCode"] == 200
    lines = [r.getMessage() for r in caplog.records if "upload-verify" in r.getMessage()]
    assert any("ok" in ln for ln in lines), f"no success line: {lines}"
    assert any("observe" in ln for ln in lines), "the mode must be on the line"


def test_the_success_line_is_not_written_when_verification_is_off(wired, caplog):
    """`off` must stay a true rollback — no S3 call and no new log volume."""
    import logging
    mp, marked = wired
    with caplog.at_level(logging.INFO):
        _run(mp, "off", FakeS3(objects={}))
    assert [r for r in caplog.records if "upload-verify" in r.getMessage()] == []
