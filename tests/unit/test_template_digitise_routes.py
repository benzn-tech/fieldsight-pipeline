"""The digitise routes: who may upload, what may be read, and whose draft is served.

Through dispatch (not the handlers) so the route order is under test too: "digitise"
must never be read as a template id by "/templates/{id}".
"""
import json
from datetime import datetime, timedelta, timezone
from io import BytesIO

import pytest

oa = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")
from botocore.exceptions import ClientError  # noqa: E402

COMPANY = "c0000000-0000-0000-0000-000000000001"
OTHER = "c0000000-0000-0000-0000-000000000002"
GM = {"id": "u-gm", "company_id": COMPANY, "global_role": "gm", "cognito_sub": "s-gm"}
WORKER = {"id": "u-wk", "company_id": COMPANY, "global_role": "worker", "cognito_sub": "s-wk"}
PLATFORM = {"id": "u-pa", "company_id": OTHER, "global_role": "platform_admin",
            "cognito_sub": "s-pa"}
RID = "a" * 32


def _missing(op):
    return ClientError({"Error": {"Code": "404" if op == "HeadObject" else "NoSuchKey",
                                  "Message": "missing"}}, op)


class _S3:
    def __init__(self):
        self.objects = {}      # key -> (bytes, LastModified)
        self.puts = {}
        self.presigned = []

    def generate_presigned_url(self, op, Params, ExpiresIn):
        self.presigned.append((op, Params, ExpiresIn))
        return "https://s3.example/%s/%s" % (op, Params["Key"])

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise _missing("HeadObject")
        return {"ContentLength": len(self.objects[Key][0])}

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise _missing("GetObject")
        body, modified = self.objects[Key]
        return {"Body": BytesIO(body), "LastModified": modified}

    def put_object(self, Bucket, Key, Body, **kw):
        self.puts[Key] = Body
        return {}


@pytest.fixture
def s3(monkeypatch):
    fake = _S3()
    monkeypatch.setattr(oa, "s3", lambda: fake)
    monkeypatch.setattr(oa, "LAKE_BUCKET", "lake")
    monkeypatch.setenv("TEMPLATE_DIGITISE", "true")
    monkeypatch.setattr(oa, "companies", type("C", (), {"get_company_by_id": staticmethod(
        lambda conn, cid: {"id": cid})})())
    recorded = []
    monkeypatch.setattr(oa.lake_objects, "record_best_effort",
                        lambda conn, key, **kw: recorded.append((key, kw)) or True)
    fake.recorded = recorded
    return fake


def _routed(monkeypatch, caller, method, path, body=None, qs=None):
    monkeypatch.setattr(oa.users, "get_user_by_sub", lambda conn, sub: dict(caller))
    monkeypatch.setattr(oa.device_heartbeat, "parse_headers", lambda h: None)
    monkeypatch.setattr(oa.device_heartbeat, "record", lambda *a, **kw: None)
    ev = {"httpMethod": method, "path": "/api/org" + path, "queryStringParameters": qs,
          "body": json.dumps(body) if body is not None else None,
          "requestContext": {"authorizer": {"claims": {"sub": caller["cognito_sub"]}}}}
    resp = oa.dispatch(None, ev, method, path)
    return resp["statusCode"], json.loads(resp["body"])


def _src(ext="jpg", company=COMPANY, n="b"):
    return f"template_sources/{company}/{n * 32}.{ext}"


# ---- the switch -------------------------------------------------------------------

@pytest.mark.parametrize("method,path,body", [
    ("POST", "/templates/digitise/upload-url", {"filename": "f.pdf", "contentType": "application/pdf", "size": 1}),
    ("POST", "/templates/digitise", {"sourceKeys": [_src()], "name": "x"}),
    ("GET", "/templates/digitise/" + RID, None),
])
def test_THE_off_means_404_on_every_route_and_no_s3_call(s3, monkeypatch, method, path, body):
    monkeypatch.setenv("TEMPLATE_DIGITISE", "false")
    status, _ = _routed(monkeypatch, WORKER, method, path, body)
    assert status == 404
    assert s3.presigned == [] and s3.puts == {}


# ---- upload-url ---------------------------------------------------------------------

def test_upload_url_signs_a_put_under_the_callers_company(s3, monkeypatch):
    status, body = _routed(monkeypatch, WORKER, "POST", "/templates/digitise/upload-url",
                           {"filename": "Site Form.PDF", "contentType": "application/pdf",
                            "size": 1234})
    assert status == 200
    assert body["sourceKey"].startswith(f"template_sources/{COMPANY}/")
    assert body["sourceKey"].endswith(".pdf")
    op, params, expires = s3.presigned[0]
    assert op == "put_object"
    assert params == {"Bucket": "lake", "Key": body["sourceKey"], "ContentType": "application/pdf"}


@pytest.mark.parametrize("payload,why", [
    ({"filename": "notes.md", "contentType": "text/markdown", "size": 10}, "type"),
    ({"filename": "scan.tiff", "contentType": "image/tiff", "size": 10}, "type"),
    ({"filename": "f.pdf", "contentType": "image/png", "size": 10}, "match"),
    ({"filename": "f.pdf", "contentType": "application/pdf", "size": 20 * 1024 * 1024 + 1}, "20 MB"),
    ({"filename": "f.pdf", "contentType": "application/pdf"}, "size"),
])
def test_upload_url_refuses_before_any_signature(s3, monkeypatch, payload, why):
    status, body = _routed(monkeypatch, WORKER, "POST", "/templates/digitise/upload-url", payload)
    assert status == 400 and why in body["error"]
    assert s3.presigned == []


def test_a_platform_admin_uploads_into_the_company_it_names(s3, monkeypatch):
    status, body = _routed(monkeypatch, PLATFORM, "POST", "/templates/digitise/upload-url",
                           {"filename": "f.png", "contentType": "image/png", "size": 5},
                           qs={"company_id": COMPANY})
    assert status == 200 and body["sourceKey"].startswith(f"template_sources/{COMPANY}/")


def test_a_worker_cannot_name_another_company(s3, monkeypatch):
    status, _ = _routed(monkeypatch, WORKER, "POST", "/templates/digitise/upload-url",
                        {"filename": "f.png", "contentType": "image/png", "size": 5},
                        qs={"company_id": OTHER})
    assert status == 403


# ---- request ------------------------------------------------------------------------

def test_a_request_writes_the_artifact_and_records_ownership(s3, monkeypatch):
    keys = [_src("jpg", n="b"), _src("png", n="c")]
    for k in keys:
        s3.objects[k] = (b"img", datetime.now(timezone.utc))
    status, body = _routed(monkeypatch, WORKER, "POST", "/templates/digitise",
                           {"sourceKeys": keys, "name": "Site inspection"})
    assert status == 202 and body["status"] == "pending"
    rid = body["id"]
    req = json.loads(s3.puts[f"template_digitise_requests/{COMPANY}/{rid}.json"])
    assert (req["id"], req["companyId"], req["userId"], req["sourceKeys"], req["name"]) == (
        rid, COMPANY, "u-wk", keys, "Site inspection")
    kinds = {k: kw["kind"] for k, kw in s3.recorded}
    assert kinds[keys[0]] == "template_source" and kinds[keys[1]] == "template_source"
    assert f"template_digitise_requests/{COMPANY}/{rid}.json" in kinds
    assert f"template_digitise_results/{COMPANY}/{rid}.json" in kinds
    assert all(kw["company_id"] == COMPANY for _, kw in s3.recorded)


def test_a_key_from_another_company_is_never_accepted(s3, monkeypatch):
    k = _src(company=OTHER)
    s3.objects[k] = (b"img", datetime.now(timezone.utc))
    status, _ = _routed(monkeypatch, WORKER, "POST", "/templates/digitise",
                        {"sourceKeys": [k], "name": "x"})
    assert status == 400 and s3.puts == {}


@pytest.mark.parametrize("keys", [
    ["template_sources/../x.jpg"],
    [_src("docx", n="b"), _src("jpg", n="c")],
    [_src("jpg", n=c) for c in "bcdefghijkl"],   # 11 images
    [_src("jpg", n="b"), _src("jpg", n="b")],    # the same page twice
    [],
])
def test_a_request_of_the_wrong_shape_is_refused(s3, monkeypatch, keys):
    for k in keys:
        s3.objects[k] = (b"img", datetime.now(timezone.utc))
    status, _ = _routed(monkeypatch, WORKER, "POST", "/templates/digitise",
                        {"sourceKeys": keys, "name": "x"})
    assert status == 400 and s3.puts == {}


def test_a_request_for_a_file_that_was_never_uploaded_is_404(s3, monkeypatch):
    status, _ = _routed(monkeypatch, WORKER, "POST", "/templates/digitise",
                        {"sourceKeys": [_src()], "name": "x"})
    assert status == 404 and s3.puts == {}


def test_an_upload_larger_than_signed_for_is_refused_on_the_head(s3, monkeypatch):
    k = _src("pdf")
    s3.objects[k] = (b"x" * (20 * 1024 * 1024 + 1), datetime.now(timezone.utc))
    status, body = _routed(monkeypatch, WORKER, "POST", "/templates/digitise",
                           {"sourceKeys": [k], "name": "x"})
    assert status == 400 and "20 MB" in body["error"] and s3.puts == {}


def test_a_request_needs_a_name(s3, monkeypatch):
    k = _src()
    s3.objects[k] = (b"img", datetime.now(timezone.utc))
    status, _ = _routed(monkeypatch, WORKER, "POST", "/templates/digitise", {"sourceKeys": [k]})
    assert status == 400


# ---- poll ---------------------------------------------------------------------------

def _req(s3, user="u-wk"):
    s3.objects[f"template_digitise_requests/{COMPANY}/{RID}.json"] = (
        json.dumps({"id": RID, "companyId": COMPANY, "userId": user}).encode(),
        datetime.now(timezone.utc))


def _res(s3, user="u-wk", age=timedelta(0), status="done"):
    result = {"id": RID, "companyId": COMPANY, "userId": user, "status": status,
              "name": "Site inspection", "sourceKeys": [_src("jpg"), _src("pdf", n="c")],
              "sections": [{"title": "General", "items": [{"text": "Gate", "flags": []}],
                            "flags": []}],
              "counts": {"sections": 1, "items": 1}, "flags": [], "route": "vision",
              "model": "google/gemini-3.8-flash", "pages": 2}
    s3.objects[f"template_digitise_results/{COMPANY}/{RID}.json"] = (
        json.dumps(result).encode(), datetime.now(timezone.utc) - age)


def test_the_digitise_id_is_not_read_as_a_template_id(s3, monkeypatch):
    monkeypatch.setattr(oa, "get_report_template",
                        lambda *a, **k: pytest.fail("routed to /templates/{id}"))
    _req(s3)
    status, body = _routed(monkeypatch, WORKER, "GET", "/templates/digitise/" + RID)
    assert status == 200 and body == {"id": RID, "status": "pending"}


def test_poll_pending_until_the_worker_writes(s3, monkeypatch):
    _req(s3)
    assert _routed(monkeypatch, WORKER, "GET", "/templates/digitise/" + RID)[1]["status"] == "pending"


def test_poll_done_serves_the_draft_and_presigns_each_source_for_an_hour(s3, monkeypatch):
    _req(s3)
    _res(s3)
    status, body = _routed(monkeypatch, WORKER, "GET", "/templates/digitise/" + RID)
    assert status == 200 and body["status"] == "done"
    assert body["sections"][0]["items"][0]["text"] == "Gate"
    assert [s["contentType"] for s in body["sources"]] == ["image/jpeg", "application/pdf"]
    gets = [(p["Key"], e) for op, p, e in s3.presigned if op == "get_object"]
    assert gets == [(_src("jpg"), 3600), (_src("pdf", n="c"), 3600)]
    assert "userId" not in body and "companyId" not in body


def test_poll_an_id_that_never_existed_is_404(s3, monkeypatch):
    assert _routed(monkeypatch, WORKER, "GET", "/templates/digitise/" + RID)[0] == 404


@pytest.mark.parametrize("bad", ["digitise", "upload-url", "A" * 32, "a" * 31, "../x"])
def test_poll_a_malformed_id_is_404(s3, monkeypatch, bad):
    assert _routed(monkeypatch, WORKER, "GET", "/templates/digitise/" + bad)[0] == 404


def test_poll_someone_elses_draft_is_404(s3, monkeypatch):
    _req(s3, user="u-other")
    _res(s3, user="u-other")
    assert _routed(monkeypatch, WORKER, "GET", "/templates/digitise/" + RID)[0] == 404
    s3.objects.pop(f"template_digitise_results/{COMPANY}/{RID}.json")
    assert _routed(monkeypatch, WORKER, "GET", "/templates/digitise/" + RID)[0] == 404


def test_poll_a_platform_admin_may_read_it_in_the_company_it_names(s3, monkeypatch):
    _req(s3)
    _res(s3)
    status, body = _routed(monkeypatch, PLATFORM, "GET", "/templates/digitise/" + RID,
                           qs={"company_id": COMPANY})
    assert status == 200 and body["status"] == "done"


def test_poll_a_draft_older_than_seven_days_is_not_served(s3, monkeypatch):
    _req(s3)
    _res(s3, age=timedelta(days=7, minutes=1))
    status, body = _routed(monkeypatch, WORKER, "GET", "/templates/digitise/" + RID)
    assert status == 404 and "expired" in body["error"]


def test_poll_an_error_result_is_served_with_its_message(s3, monkeypatch):
    _req(s3)
    _res(s3, status="error")
    status, body = _routed(monkeypatch, WORKER, "GET", "/templates/digitise/" + RID)
    assert status == 200 and body["status"] == "error"


def test_poll_someone_elses_expired_draft_does_not_say_expired(s3, monkeypatch):
    # Ownership is checked before age: "expired" would confirm the draft exists.
    _req(s3, user="u-other")
    _res(s3, user="u-other", age=timedelta(days=8))
    status, body = _routed(monkeypatch, WORKER, "GET", "/templates/digitise/" + RID)
    assert status == 404 and "expired" not in body["error"]
