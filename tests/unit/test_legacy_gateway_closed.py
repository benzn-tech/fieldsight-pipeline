"""Retire-the-legacy-gateway, phase B: the data routes are closed.

Every closed route answers HTTP 410 {"error": "gone", "use": <replacement>} for
every role and method, BEFORE any S3 or DynamoDB access. Only /api/health and the
ask proxies (/api/ask, /api/ask/voice, /api/ask/corroborate, /api/search) remain.
The IAM that backed the removed handlers is gone from the template with them.
"""
import json
import logging
import os
import re

import pytest

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")

fapi = pytest.importorskip("lambda_fieldsight_api", reason="requires boto3 (installed in CI)")

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "src")

# route -> exact "use" text the 410 must carry
CLOSED = {
    "/api/timeline": "/api/org/timeline",
    "/api/dates": "/api/org/dates",
    "/api/media/presigned-url": "/api/org/media/presigned-url",
    "/api/reports/history": "/api/org/reports/history",
    "/api/reports/generate": "POST /api/org/reports/regenerate",
    "/api/users": "/api/org/members",
    "/api/sites": "/api/org/sites",
    "/api/site-users": "/api/org/sites/{id}/members",
    "/api/transcripts": "/api/org/transcripts",
    "/api/audio-segments": "/api/org/audio-segments",
    "/api/video-segments": "/api/org/video-segments",
    "/api/recording-stats": "none",
    "/api/actions": "none",
    "/api/actions/toggle": "PATCH /api/org/action-items/{id}",
}

ROLES = ["admin", "gm", "worker", None]  # None = the authorizer yields no sub


class _Boom:
    def __getattr__(self, name):
        raise AssertionError("storage touched on a closed route: " + name)

    def __call__(self, *a, **k):
        raise AssertionError("storage touched on a closed route")


@pytest.fixture
def no_storage(monkeypatch):
    """Any S3/DynamoDB client or resource the handler could reach raises."""
    monkeypatch.setattr(fapi, "s3_client", _Boom(), raising=False)
    monkeypatch.setattr(fapi, "dynamodb", _Boom(), raising=False)

    def guard(service, *a, **k):
        raise AssertionError("boto3 %s created on a closed route" % service)

    monkeypatch.setattr(fapi.boto3, "client", guard)
    monkeypatch.setattr(fapi.boto3, "resource", guard)


def _event(path, method, role):
    claims = {} if role is None else {"sub": "sub-" + role, "custom:role": role,
                                      "email": role + "@x.nz", "name": role}
    return {"httpMethod": method, "path": path,
            "queryStringParameters": {"user": "Someone", "date": "2026-10-01",
                                      "key": "reports/x/daily_report.json"},
            "body": json.dumps({"date": "2026-10-01", "checked": True}) if method != "GET" else None,
            "headers": {"User-Agent": "Mozilla/5.0"},
            "requestContext": {"authorizer": {"claims": claims}}}


@pytest.mark.parametrize("method", ["GET", "POST", "PATCH", "DELETE"])
@pytest.mark.parametrize("role", ROLES, ids=lambda r: r or "no-sub")
@pytest.mark.parametrize("path,use", sorted(CLOSED.items()))
def test_closed_route_is_410_naming_the_replacement(no_storage, path, use, role, method):
    res = fapi.lambda_handler(_event(path, method, role), None)
    assert res["statusCode"] == 410
    assert json.loads(res["body"]) == {"error": "gone", "use": use}


def test_the_table_here_is_the_whole_table():
    assert fapi.CLOSED_ROUTES == CLOSED


@pytest.mark.parametrize("path,use", sorted(CLOSED.items()))
def test_every_replacement_is_a_real_org_api_route(path, use):
    if use == "none":
        return
    # /api/org/sites/{id}/members -> /api/org/sites
    route = use.split(" ")[-1].split("{")[0].rstrip("/")
    org = open(os.path.join(SRC, "lambda_org_api.py"), encoding="utf-8").read()
    assert route in org, "%s names %s but org-api has no such route" % (path, use)


def test_closed_routes_stay_visible_in_the_caller_log(no_storage, caplog):
    with caplog.at_level(logging.INFO):
        fapi.lambda_handler(_event("/api/users", "GET", "worker"), None)
    lines = [r.getMessage() for r in caplog.records if "LEGACY_CALL" in r.getMessage()]
    assert len(lines) == 1
    d = json.loads(lines[0].split("LEGACY_CALL ", 1)[1])
    assert d["route"] == "/api/users" and d["role"] == "worker" and d["has_sub"] is True


def test_the_removed_handlers_and_directory_reads_are_gone():
    for name in ("get_timeline", "get_dates", "get_presigned_url", "get_report_history",
                 "get_users", "get_sites", "get_site_users", "get_transcripts",
                 "get_audio_segments", "get_video_segments", "get_recording_stats",
                 "get_actions", "toggle_action", "find_any_report", "can_access_user_data",
                 "load_user_mapping", "trigger_report_generation", "s3_client", "dynamodb"):
        assert not hasattr(fapi, name), name


# ---- what stays ---------------------------------------------------------

def test_health_still_answers_without_auth():
    res = fapi.lambda_handler({"httpMethod": "GET", "path": "/api/health"}, None)
    assert res["statusCode"] == 200 and json.loads(res["body"])["status"] == "ok"


def test_unknown_route_is_still_404():
    res = fapi.lambda_handler(_event("/api/nothing-here", "GET", "admin"), None)
    assert res["statusCode"] == 404


@pytest.mark.parametrize("path,handler", [
    ("/api/ask", "ask_question"), ("/api/ask/voice", "ask_voice"),
    ("/api/ask/corroborate", "corroborate_answer"), ("/api/search", "search_topics"),
])
def test_the_ask_proxies_still_dispatch(monkeypatch, path, handler):
    seen = {}

    def fake(body, caller):
        seen["caller"] = caller
        return fapi.ok({"ok": 1})

    monkeypatch.setattr(fapi, handler, fake)
    res = fapi.lambda_handler(_event(path, "POST", "worker"), None)
    assert res["statusCode"] == 200 and seen["caller"]["sub"] == "sub-worker"


@pytest.mark.parametrize("path,body", [
    ("/api/ask", {"question": "q"}),
    ("/api/ask/voice", {"audio": "AAAA"}),
    ("/api/ask/corroborate", {"question": "q", "answer": "a"}),
    ("/api/search", {"question": "concrete pour"}),
])
def test_the_ask_proxies_still_refuse_an_empty_sub(monkeypatch, path, body):
    def invoked(**k):
        raise AssertionError("the ask agent was invoked without a sub")

    monkeypatch.setattr(fapi.lambda_client, "invoke", invoked)
    monkeypatch.setattr(fapi.ask_lambda_client, "invoke", invoked)
    ev = _event(path, "POST", None)
    ev["body"] = json.dumps(body)
    assert fapi.lambda_handler(ev, None)["statusCode"] == 401


def test_caller_identity_reads_claims_only(no_storage):
    ident = fapi.get_caller_identity(_event("/api/ask", "POST", "gm"))
    assert ident["sub"] == "sub-gm" and ident["role"] == "gm"
    assert ident["email"] == "gm@x.nz" and ident["name"] == "gm"
    assert fapi.get_caller_identity({})["role"] == ""


# ---- the IAM went with the handlers --------------------------------------

def _block(tpl, name):
    i = tpl.index("\n  %s:\n" % name)
    m = re.search(r"\n  (?:[A-Za-z0-9]+:|# =+)\n", tpl[i + 5:])
    return tpl[i:i + 5 + (m.start() if m else len(tpl))]


def test_the_gateway_function_holds_only_the_ask_agent_invoke_grant():
    tpl = open(os.path.join(SRC, "template.yaml"), encoding="utf-8").read()
    blk = _block(tpl, "ApiFunction")
    policies = blk[blk.index("      Policies:"):blk.index("      Events:")]
    assert "LambdaInvokePolicy" in policies and "!Ref AskAgentFunction" in policies
    for banned in ("DynamoDB", "S3ReadPolicy", "S3WritePolicy", "ReportGeneratorFunction",
                   "UsersTableName", "AuditTableName"):
        assert banned not in policies, banned
    for banned in ("USERS_TABLE", "AUDIT_TABLE", "ITEMS_TABLE", "REPORTS_TABLE", "REPORT_FUNCTION"):
        assert banned not in blk, banned


def test_the_ask_agent_has_no_s3_grant():
    tpl = open(os.path.join(SRC, "template.yaml"), encoding="utf-8").read()
    blk = _block(tpl, "AskAgentFunction")
    policies = blk[blk.index("      Policies:"):]
    assert "S3ReadPolicy" not in policies and "S3WritePolicy" not in policies
