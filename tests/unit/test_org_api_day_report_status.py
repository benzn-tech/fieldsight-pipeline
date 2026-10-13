"""GET /days/{date}/report/status — never presign a day with a deleted session.

A presigned URL outlives the check that produced it, so every session named in
the result is re-checked against every folder's mirror BEFORE a URL is built. A
result that names no sessions cannot be checked and is not served.
Spec 2026-09-15 §9 step 3, finding F7.
"""
import json

import pytest
from botocore.exceptions import ClientError

oa = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")
import deletion_mirror  # noqa: E402

CALLER = {"id": "u-1", "company_id": "c-1", "global_role": "admin"}
DATE = "2026-09-10"
RID = "d" * 32
A = "sid" + "a" * 32
GRP = "grp" + "b" * 32


class _Presigned(Exception):
    pass


def _event(**extra):
    params = {"user": "James_Lamb", "requestId": RID}
    params.update(extra)
    return {"queryStringParameters": params}


def _wire(monkeypatch, stored=None, removed_by_folder=None, missing=False, presign_raises=False):
    got = []
    payload = json.dumps(stored or {}).encode("utf-8")

    class _Body:
        def read(self):
            return payload

    class _S3:
        def get_object(self, Bucket, Key):
            got.append(Key)
            if missing:
                raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
            return {"Body": _Body()}

        def generate_presigned_url(self, *a, **kw):
            if presign_raises:
                raise _Presigned("a day with a deleted session must never reach a presign")
            return "https://signed.example/day.docx"

    monkeypatch.setattr(oa, "s3", lambda: _S3())
    monkeypatch.setattr(oa, "_resolve_org_media_folder",
                        lambda conn, caller, user, what=None: ("James_Lamb", None))
    monkeypatch.setattr(deletion_mirror, "deleted_sessions_strict",
                        lambda s3, bucket, folder, date: set((removed_by_folder or {}).get(folder, set())))
    return got


DONE = {"status": "done", "docKey": "session_reports/James_Lamb/2026-09-10/day/x.docx",
        "sessionIds": [A, GRP], "mirrorFolders": ["James_Lamb", "Lead_F"], "emailed": False}


def test_pending_until_the_worker_writes_a_result(monkeypatch):
    got = _wire(monkeypatch, missing=True)
    res = oa.day_report_status(None, CALLER, DATE, _event())
    assert json.loads(res["body"]) == {"status": "pending"}
    assert got == [f"session_report_results/James_Lamb/{DATE}/day/{RID}.json"]


def test_done_presigns_when_nothing_was_deleted(monkeypatch):
    _wire(monkeypatch, stored=DONE)
    b = json.loads(oa.day_report_status(None, CALLER, DATE, _event())["body"])
    assert b["status"] == "done" and b["docUrl"].startswith("https://")


def test_a_deleted_session_is_removed_and_never_presigned(monkeypatch):
    _wire(monkeypatch, stored=DONE, removed_by_folder={"James_Lamb": {A}}, presign_raises=True)
    assert json.loads(oa.day_report_status(None, CALLER, DATE, _event())["body"]) == {"status": "removed"}


def test_a_merged_meeting_deleted_in_the_leads_mirror_is_removed(monkeypatch):
    _wire(monkeypatch, stored=DONE, removed_by_folder={"Lead_F": {GRP}}, presign_raises=True)
    assert json.loads(oa.day_report_status(None, CALLER, DATE, _event())["body"]) == {"status": "removed"}


def test_a_result_naming_no_sessions_is_not_served(monkeypatch):
    _wire(monkeypatch, stored=dict(DONE, sessionIds=[]), presign_raises=True)
    b = json.loads(oa.day_report_status(None, CALLER, DATE, _event())["body"])
    assert b["status"] == "error"


@pytest.mark.parametrize("bad", ["", "r-1", "../x", "D" * 32, "d" * 31])
def test_a_malformed_request_id_is_a_400(monkeypatch, bad):
    _wire(monkeypatch, stored=DONE)
    assert oa.day_report_status(None, CALLER, DATE, _event(requestId=bad))["statusCode"] == 400


def test_an_unreadable_mirror_raises(monkeypatch):
    _wire(monkeypatch, stored=DONE)

    def boom(s3, bucket, folder, date):
        raise RuntimeError("mirror unreadable")
    monkeypatch.setattr(deletion_mirror, "deleted_sessions_strict", boom)
    with pytest.raises(RuntimeError):
        oa.day_report_status(None, CALLER, DATE, _event())
