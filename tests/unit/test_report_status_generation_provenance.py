"""GET .../report/status must echo generation provenance when the worker's result
carries it, and must not change the response shape when it doesn't.

Found on TEST: a genuinely generated day report came back as
`{"status": "done", "docUrl": ..., "emailed": false}` -- `generated`, `templateId`,
`templateVersion` and `model` were all absent, so a caller could not tell a
generated report from an assembled one, and the template/model that wrote a
document was lost at the read side. `promptChars` is internal noise, not
provenance, and must stay out of the response.
"""
import json

import pytest

oa = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")
import deletion_mirror  # noqa: E402

CALLER = {"id": "u-1", "company_id": "c-1", "global_role": "admin"}
DATE = "2026-09-10"
RID = "d" * 32
SID = "sid" + "a" * 32
GRP = "grp" + "b" * 32


def _wire(monkeypatch, stored, folder="James_Lamb"):
    payload = json.dumps(stored).encode("utf-8")

    class _Body:
        def read(self):
            return payload

    class _S3:
        def get_object(self, **kw):
            return {"Body": _Body()}

        def generate_presigned_url(self, *a, **kw):
            return "https://signed.example/doc.docx"

    monkeypatch.setattr(oa, "s3", lambda: _S3())
    monkeypatch.setattr(oa, "_resolve_org_media_folder",
                        lambda conn, caller, user, what=None: (folder, None))
    monkeypatch.setattr(oa, "_session_was_removed", lambda *a, **kw: False)
    monkeypatch.setattr(deletion_mirror, "deleted_sessions_strict",
                        lambda s3, bucket, folder, date: set())


GENERATED = {
    "status": "done", "docKey": "session_reports/James_Lamb/2026-09-10/x.docx",
    "emailed": True, "generated": True, "templateId": "tmpl-1", "templateVersion": 3,
    "model": "claude-sonnet-5", "promptChars": 4821,
}

ASSEMBLED = {
    "status": "done", "docKey": "session_reports/James_Lamb/2026-09-10/x.docx",
    "emailed": False,
}


def _session_event():
    return {"queryStringParameters": {"date": DATE, "user": "James_Lamb", "requestId": RID}}


def _day_event():
    return {"queryStringParameters": {"user": "James_Lamb", "requestId": RID}}


def test_session_report_status_echoes_provenance_when_present(monkeypatch):
    _wire(monkeypatch, GENERATED)
    body = json.loads(oa.session_report_status(None, CALLER, SID, _session_event())["body"])
    assert body == {
        "status": "done", "docUrl": "https://signed.example/doc.docx", "emailed": True,
        "generated": True, "templateId": "tmpl-1", "templateVersion": 3,
        "model": "claude-sonnet-5",
    }


def test_session_report_status_is_unchanged_when_provenance_absent(monkeypatch):
    _wire(monkeypatch, ASSEMBLED)
    body = json.loads(oa.session_report_status(None, CALLER, SID, _session_event())["body"])
    assert body == {
        "status": "done", "docUrl": "https://signed.example/doc.docx", "emailed": False,
    }


def test_day_report_status_echoes_provenance_when_present(monkeypatch):
    stored = dict(GENERATED, sessionIds=[SID, GRP], mirrorFolders=["James_Lamb"])
    _wire(monkeypatch, stored)
    body = json.loads(oa.day_report_status(None, CALLER, DATE, _day_event())["body"])
    assert body == {
        "status": "done", "docUrl": "https://signed.example/doc.docx", "emailed": True,
        "generated": True, "templateId": "tmpl-1", "templateVersion": 3,
        "model": "claude-sonnet-5",
    }


def test_day_report_status_is_unchanged_when_provenance_absent(monkeypatch):
    stored = dict(ASSEMBLED, sessionIds=[SID, GRP], mirrorFolders=["James_Lamb"])
    _wire(monkeypatch, stored)
    body = json.loads(oa.day_report_status(None, CALLER, DATE, _day_event())["body"])
    assert body == {
        "status": "done", "docUrl": "https://signed.example/doc.docx", "emailed": False,
    }
