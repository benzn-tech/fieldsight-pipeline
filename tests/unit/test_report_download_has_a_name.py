"""The two report/status endpoints presign with a readable filename.

The browser names a download after the last segment of the presigned URL, and
these keys end in the requestId -- so every generated report anybody ever
downloaded landed as a uuid. `ResponseContentDisposition` fixes that at serve
time; report_download_name builds the value, and this file pins that the
parameter actually reaches boto, on BOTH endpoints.

Both, deliberately. Two gateways serving the same feature and only one repaired
is this repo's oldest trap, and the session and day paths presign at two
separate call sites.

THE test is `the signed request carries the name` -- it is the only one that
fails if the parameter is dropped on its way to boto, which is precisely how
this class of fix dies: the helper is correct, well tested, and never called.
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
DOC = "session_reports/James_Lamb/2026-09-10/%s.docx" % RID

GENERATED = {"status": "done", "docKey": DOC, "emailed": False,
             "generated": True, "templateId": "personal-meeting", "templateVersion": 3}
ASSEMBLED = {"status": "done", "docKey": DOC, "emailed": False}


def _wire(monkeypatch, stored, folder="James_Lamb"):
    """Returns the list the presign kwargs land in."""
    payload = json.dumps(stored).encode("utf-8")
    signed = []

    class _Body:
        def read(self):
            return payload

    class _S3:
        def get_object(self, **kw):
            return {"Body": _Body()}

        def generate_presigned_url(self, op, Params=None, ExpiresIn=None):
            signed.append(Params or {})
            return "https://signed.example/%s" % RID

    monkeypatch.setattr(oa, "s3", lambda: _S3())
    monkeypatch.setattr(oa, "_resolve_org_media_folder",
                        lambda conn, caller, user, what=None: (folder, None))
    monkeypatch.setattr(oa, "_session_was_removed", lambda *a, **kw: False)
    monkeypatch.setattr(deletion_mirror, "deleted_sessions_strict",
                        lambda s3, bucket, folder, date: set())
    return signed


def _session_event():
    return {"queryStringParameters": {"date": DATE, "user": "James_Lamb", "requestId": RID}}


def _day_event():
    return {"queryStringParameters": {"user": "James_Lamb", "requestId": RID}}


def _as_day(stored):
    return dict(stored, sessionIds=[SID, GRP], mirrorFolders=["James_Lamb"])


# ---- THE test ---------------------------------------------------------------

def test_the_signed_request_carries_the_name(monkeypatch):
    signed = _wire(monkeypatch, GENERATED)
    oa.session_report_status(None, CALLER, SID, _session_event())
    disp = signed[0]["ResponseContentDisposition"]
    assert "James_Lamb_personal-meeting-v3_2026-09-10.docx" in disp
    assert RID not in disp, "the uuid is the thing this replaces"


def test_the_day_endpoint_carries_it_too(monkeypatch):
    """The other half of the oldest trap in this repo: fixing one of two
    gateways and believing the feature is done."""
    signed = _wire(monkeypatch, _as_day(GENERATED))
    oa.day_report_status(None, CALLER, DATE, _day_event())
    disp = signed[0]["ResponseContentDisposition"]
    assert "James_Lamb_personal-meeting-v3_2026-09-10.docx" in disp


# ---- what the name says -----------------------------------------------------

def test_an_assembled_report_is_named_report_not_none(monkeypatch):
    signed = _wire(monkeypatch, ASSEMBLED)
    oa.session_report_status(None, CALLER, SID, _session_event())
    disp = signed[0]["ResponseContentDisposition"]
    assert "James_Lamb_report_2026-09-10.docx" in disp
    assert "None" not in disp


def test_a_chinese_folder_reaches_the_header_intact(monkeypatch):
    signed = _wire(monkeypatch, GENERATED, folder="林本_UCPK2")
    oa.session_report_status(None, CALLER, SID, _session_event())
    disp = signed[0]["ResponseContentDisposition"]
    # The real name rides on filename*, percent-encoded; the header stays ascii.
    from urllib.parse import unquote
    assert "林本" in unquote(disp)
    disp.encode("ascii")
    # ...and the fallback beside it is not an empty stem.
    import re
    fallback = re.search(r'filename="([^"]*)"', disp).group(1)
    assert fallback[: -len(".docx")].strip("_. ")


# ---- the key does not move --------------------------------------------------

def test_the_object_key_is_untouched(monkeypatch):
    """Only how it is SERVED changes. If the key moved, every object already
    written and every deletion mirror entry would be pointing at nothing."""
    signed = _wire(monkeypatch, GENERATED)
    oa.session_report_status(None, CALLER, SID, _session_event())
    assert signed[0]["Key"] == DOC
    assert signed[0]["Bucket"] == oa.LAKE_BUCKET


def test_the_response_shape_gains_nothing(monkeypatch):
    """The name is a property of the URL, not a new field to parse."""
    _wire(monkeypatch, ASSEMBLED)
    body = json.loads(oa.session_report_status(None, CALLER, SID, _session_event())["body"])
    assert set(body) == {"status", "docUrl", "emailed"}


# ---- never the reason a finished report cannot be handed over ---------------

def test_a_broken_name_still_serves_the_report(monkeypatch):
    """Degrades to the bare presign -- a uuid filename, which is what this
    endpoint did for its whole life -- rather than 500ing on a finished
    document."""
    signed = _wire(monkeypatch, GENERATED)
    monkeypatch.setattr(oa.report_download_name, "display_name",
                        lambda *a, **kw: (_ for _ in ()).throw(ValueError("boom")))
    resp = oa.session_report_status(None, CALLER, SID, _session_event())
    assert json.loads(resp["body"])["status"] == "done"
    assert "ResponseContentDisposition" not in signed[0]
    assert signed[0]["Key"] == DOC
