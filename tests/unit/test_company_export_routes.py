"""POST/GET /companies/{id}/export: platform_admin only, async via LakeOps, presigned downloads
confined to exports/{company_id}/{export_id}/ with a 15-minute expiry."""
import io
import json
import uuid

import pytest
from botocore.exceptions import ClientError

import lambda_org_api as org

CO = str(uuid.uuid4())
EID = str(uuid.uuid4())
OTHER_CO = str(uuid.uuid4())
PREFIX = f"exports/{CO}/{EID}/"


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        return self

    def fetchone(self):
        return None


class Lam:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def invoke(self, **kw):
        if self.fail:
            raise RuntimeError("lambda down")
        self.calls.append(kw)
        return {"StatusCode": 202}


class S3:
    def __init__(self, docs=None):
        self.docs = docs or {}
        self.presigned = []

    def get_object(self, Bucket, Key):
        if Key not in self.docs:
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "x"}}, "GetObject")
        return {"Body": io.BytesIO(json.dumps(self.docs[Key]).encode())}

    def generate_presigned_url(self, op, Params, ExpiresIn):
        self.presigned.append((op, Params["Key"], ExpiresIn))
        return "https://signed/" + Params["Key"]


def caller(role):
    return {"id": "u1", "company_id": str(uuid.uuid4()), "global_role": role, "archived_at": None}


@pytest.fixture
def wired(monkeypatch):
    state = {"role": "platform_admin", "company": {"id": CO, "name": "Co"}, "lam": Lam(),
             "s3": S3(), "lookups": []}
    monkeypatch.setenv("LAKE_OPS_FUNCTION", "fs-lakeops")
    monkeypatch.setattr(org, "get_connection", lambda: FakeConn())
    monkeypatch.setattr(org.device_heartbeat, "record", lambda *a, **k: None)
    monkeypatch.setattr(org.users, "get_user_by_sub", lambda conn, sub: caller(state["role"]))

    def get_company(conn, cid):
        state["lookups"].append(cid)
        return state["company"]
    monkeypatch.setattr(org.companies, "get_company_by_id", get_company)
    monkeypatch.setattr(org, "lambda_client", lambda: state["lam"])
    monkeypatch.setattr(org, "s3", lambda: state["s3"])
    return state


def call(method, path):
    return org.lambda_handler(
        {"httpMethod": method, "path": "/api/org" + path, "body": "{}",
         "requestContext": {"authorizer": {"claims": {"sub": "sub-1"}}}}, None)


def body(resp):
    return json.loads(resp["body"])


@pytest.mark.parametrize("role", ["admin", "gm", "pm", "regional_manager", "site_manager", "worker"])
def test_only_platform_admin_may_export(wired, role):
    wired["role"] = role
    r1 = call("POST", f"/companies/{CO}/export")
    r2 = call("GET", f"/companies/{CO}/export/{EID}")
    assert r1["statusCode"] == 403 and r2["statusCode"] == 403
    assert wired["lam"].calls == [] and wired["s3"].presigned == []
    assert wired["lookups"] == []          # refused before the company is even looked up


def test_unknown_company_is_404_and_a_non_uuid_company_is_404(wired):
    wired["company"] = None
    assert call("POST", f"/companies/{CO}/export")["statusCode"] == 404
    assert call("GET", f"/companies/{CO}/export/{EID}")["statusCode"] == 404
    wired["company"] = {"id": CO}
    assert call("POST", "/companies/not-a-uuid/export")["statusCode"] == 404
    assert wired["lam"].calls == []


def test_post_invokes_lake_ops_once_asynchronously(wired):
    resp = call("POST", f"/companies/{CO}/export")
    assert resp["statusCode"] == 202
    (c,) = wired["lam"].calls
    assert c["FunctionName"] == "fs-lakeops" and c["InvocationType"] == "Event"
    payload = json.loads(c["Payload"])
    assert payload["task"] == "export_company" and payload["company_id"] == CO
    assert str(uuid.UUID(payload["export_id"])) == payload["export_id"]
    assert body(resp)["exportId"] == payload["export_id"]


def test_unconfigured_is_503_and_nothing_runs_inline(wired, monkeypatch):
    monkeypatch.delenv("LAKE_OPS_FUNCTION")
    ran = []
    monkeypatch.setattr(org.company_export, "run", lambda *a, **k: ran.append(1))
    resp = call("POST", f"/companies/{CO}/export")
    assert resp["statusCode"] == 503 and "export not configured" in body(resp)["error"]
    assert wired["lam"].calls == [] and ran == []


def test_an_invoke_failure_is_a_502_not_a_crash(wired):
    wired["lam"] = Lam(fail=True)
    assert call("POST", f"/companies/{CO}/export")["statusCode"] == 502


@pytest.mark.parametrize("crafted", ["..", "not-a-uuid", "..%2Fother-company%2Fx", "%2e%2e",
                                     f"{EID}%2f..", "x" * 300])
def test_a_crafted_export_id_is_400_and_never_reaches_s3(wired, crafted):
    resp = call("GET", f"/companies/{CO}/export/{crafted}")
    assert resp["statusCode"] == 400
    assert wired["s3"].presigned == []


def test_function_level_traversal_is_400(wired):
    r = org.company_export_status(FakeConn(), caller("platform_admin"), CO, "../other-company/x")
    assert r["statusCode"] == 400


def test_done_export_presigns_only_keys_under_its_prefix_for_15_minutes(wired):
    wired["s3"].docs[PREFIX + "manifest.json"] = {
        "company_id": CO, "parts": [{"key": PREFIX + "part-0001.zip", "objects": 3, "bytes": 30},
                  {"key": PREFIX + "part-0002.zip", "objects": 1, "bytes": 10},
                  # a manifest naming anything outside the prefix must not be presigned
                  {"key": f"exports/{OTHER_CO}/{EID}/part-0001.zip", "objects": 1, "bytes": 1},
                  {"key": PREFIX + "../../secret.zip", "objects": 1, "bytes": 1},
                  {"key": "transcripts/Ada/2026-10-01/a.json", "objects": 1, "bytes": 1}],
        "objects": [{}, {}, {}, {}], "excluded": {"recording_sid": 1}}
    resp = call("GET", f"/companies/{CO}/export/{EID}")
    assert resp["statusCode"] == 200
    out = body(resp)
    assert out["status"] == "done" and out["expiresIn"] == 900
    assert [p["key"] for p in out["parts"]] == [PREFIX + "part-0001.zip", PREFIX + "part-0002.zip"]
    signed = wired["s3"].presigned
    assert signed and all(k.startswith(PREFIX) and ".." not in k and exp == 900
                          for _, k, exp in signed)
    assert {k for _, k, _ in signed} == {PREFIX + "part-0001.zip", PREFIX + "part-0002.zip",
                                         PREFIX + "manifest.json"}


def test_running_and_unknown_exports(wired):
    wired["s3"].docs[PREFIX + "state.json"] = {"company_id": CO,
                                               "parts": [{"key": PREFIX + "part-0001.zip"}],
                                               "entries": [{}, {}]}
    out = body(call("GET", f"/companies/{CO}/export/{EID}"))
    assert out["status"] == "running" and out["done"] is False and out["partsWritten"] == 1
    assert wired["s3"].presigned == []                       # nothing downloadable until done
    other = str(uuid.uuid4())
    assert call("GET", f"/companies/{CO}/export/{other}")["statusCode"] == 404


def test_a_doc_for_another_company_is_404(wired):
    """M3: the stored company_id must match the path company, for manifest and state alike."""
    wired["s3"].docs[PREFIX + "manifest.json"] = {"company_id": OTHER_CO, "parts": [
        {"key": PREFIX + "part-0001.zip"}]}
    assert call("GET", f"/companies/{CO}/export/{EID}")["statusCode"] == 404
    del wired["s3"].docs[PREFIX + "manifest.json"]
    wired["s3"].docs[PREFIX + "state.json"] = {"company_id": OTHER_CO, "parts": [], "entries": []}
    assert call("GET", f"/companies/{CO}/export/{EID}")["statusCode"] == 404
    assert wired["s3"].presigned == []
