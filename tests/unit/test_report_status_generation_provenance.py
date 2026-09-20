"""GET .../report/status must echo generation provenance when the worker's result
carries it, and must not change the response shape when it doesn't.

Found on TEST: a genuinely generated day report came back as
`{"status": "done", "docUrl": ..., "emailed": false}` -- `generated`, `templateId`
and `templateVersion` were all absent, so a caller could not tell a generated
report from an assembled one, and the template that wrote a document was lost
at the read side. `promptChars` is internal noise, not provenance, and must
stay out of the response.

2026-09-20: `model` joined `promptChars` as a field that must stay out of the
response, for a different reason -- it is not noise, it is exactly the
provenance this endpoint exists to echo, but this endpoint is a customer-facing
API response and a customer-facing surface must not name which LLM vendor or
model wrote the document. The worker's own result JSON (an internal record,
read by `s3().get_object` in `_wire` below) still carries `model` -- only the
copy this handler returns to the HTTP caller drops it.
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
    }
    assert "model" not in body, "a customer-facing status response must not name the model"


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
    }
    assert "model" not in body, "a customer-facing status response must not name the model"


def test_day_report_status_is_unchanged_when_provenance_absent(monkeypatch):
    stored = dict(ASSEMBLED, sessionIds=[SID, GRP], mirrorFolders=["James_Lamb"])
    _wire(monkeypatch, stored)
    body = json.loads(oa.day_report_status(None, CALLER, DATE, _day_event())["body"])
    assert body == {
        "status": "done", "docUrl": "https://signed.example/doc.docx", "emailed": False,
    }


def test_generation_provenance_drops_model_without_touching_the_input():
    """The internal record (the worker's own result dict, read straight off S3)
    must not be mutated on its way through this filter -- only the copy handed
    back to the HTTP caller loses `model`. If this ever mutated `result` in
    place, a caller holding the same dict (or a second read of the same
    object) would lose the provenance too."""
    result = dict(GENERATED)
    provenance = oa._generation_provenance(result)
    assert provenance == {"generated": True, "templateId": "tmpl-1", "templateVersion": 3}
    assert result["model"] == "claude-sonnet-5", "the internal record must not be scrubbed"
