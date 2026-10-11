"""The legacy gateway logs one LEGACY_CALL line per authenticated request so
we can see who still calls it before retiring it. The line carries no tenant
content: no query-param values, no body values."""
import json
import logging
import os

import pytest

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")

fapi = pytest.importorskip("lambda_fieldsight_api", reason="requires boto3 (installed in CI)")

KEYS = {"route", "method", "role", "has_sub", "ua"}


def _call(monkeypatch, caplog, *, sub="sub-1", headers=None, path="/api/timeline",
          params=None):
    monkeypatch.setattr(fapi, "get_caller_identity",
                        lambda event: {"sub": sub, "role": "gm", "email": "", "name": "",
                                       "display_name": ""})
    monkeypatch.setattr(fapi, "health_check", lambda p: fapi.ok({}))
    event = {"httpMethod": "GET", "path": path, "queryStringParameters": params,
             "headers": headers}
    with caplog.at_level(logging.INFO):
        fapi.lambda_handler(event, None)
    return [r.getMessage() for r in caplog.records if "LEGACY_CALL" in r.getMessage()]


def _parse(line):
    return json.loads(line.split("LEGACY_CALL ", 1)[1])


def test_one_line_with_exactly_five_keys(monkeypatch, caplog):
    lines = _call(monkeypatch, caplog)
    assert len(lines) == 1
    d = _parse(lines[0])
    assert set(d) == KEYS
    assert d["route"] == "/api/timeline" and d["method"] == "GET"
    assert d["role"] == "gm" and d["has_sub"] is True


def test_has_sub_false_without_sub(monkeypatch, caplog):
    assert _parse(_call(monkeypatch, caplog, sub="")[0])["has_sub"] is False


@pytest.mark.parametrize("hdrs,family", [
    ({"User-Agent": "Mozilla/5.0 (X11) Gecko"}, "browser"),
    ({"user-agent": "Mozilla/5.0 (X11) Gecko"}, "browser"),
    ({"User-Agent": "okhttp/4.12.0"}, "android"),
    ({"User-Agent": "Dalvik/2.1.0 (Linux)"}, "android"),
    ({"User-Agent": "CFNetwork/1494 Darwin/23"}, "ios"),
    ({"USER-AGENT": "Mozilla/5.0 (X11) Gecko"}, "browser"),
    ({"User-Agent": "MyApp/3 CFNetwork/1494 Darwin/23.1"}, "ios"),
    ({"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0) AppleWebKit"}, "ios"),
    ({"User-Agent": "curl/8.0"}, "other"),
    ({}, "none"),
    (None, "none"),
])
def test_ua_family(monkeypatch, caplog, hdrs, family):
    assert _parse(_call(monkeypatch, caplog, headers=hdrs)[0])["ua"] == family


def test_no_param_values_in_any_log_record(monkeypatch, caplog):
    _call(monkeypatch, caplog, params={"user": "Secret_Folder", "date": "2026-01-01"})
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "Secret_Folder" not in text and "2026-01-01" not in text


def test_a_closed_route_is_logged_by_name_and_answers_410(monkeypatch, caplog):
    # KNOWN_ROUTES keeps the closed routes so their 410s stay visible in the logs.
    lines = _call(monkeypatch, caplog, path="/api/users")
    assert len(lines) == 1 and _parse(lines[0])["route"] == "/api/users"


def test_role_comes_from_the_claim_or_is_empty():
    claim = {"requestContext": {"authorizer": {"claims": {"sub": "s", "custom:role": "gm"}}}}
    assert fapi.get_caller_identity(claim)["role"] == "gm"
    bare = {"requestContext": {"authorizer": {"claims": {"sub": "s"}}}}
    assert fapi.get_caller_identity(bare)["role"] == ""


def test_health_logs_nothing(monkeypatch, caplog):
    assert _call(monkeypatch, caplog, path="/api/health") == []


def test_unknown_path_logs_route_other_once(monkeypatch, caplog):
    lines = _call(monkeypatch, caplog, path="/api/secret-client-text-12345")
    assert len(lines) == 1 and _parse(lines[0])["route"] == "other"
    assert "secret-client-text" not in " ".join(r.getMessage() for r in caplog.records
                                               if "LEGACY_CALL" in r.getMessage())


def test_post_body_values_never_logged(monkeypatch, caplog):
    monkeypatch.setattr(fapi, "get_caller_identity", lambda e: {"sub": "s", "role": "gm"})
    monkeypatch.setattr(fapi, "ask_question", lambda b, c: fapi.ok({}))
    event = {"httpMethod": "POST", "path": "/api/ask", "headers": {},
             "body": json.dumps({"question": "TopSecretQuestion", "user": "Secret_Folder"})}
    with caplog.at_level(logging.INFO):
        fapi.lambda_handler(event, None)
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "TopSecretQuestion" not in text and "Secret_Folder" not in text


def test_raising_handler_still_logs_exactly_once(monkeypatch, caplog):
    monkeypatch.setattr(fapi, "get_caller_identity", lambda e: {"sub": "s", "role": "gm"})

    def boom(b, c):
        raise RuntimeError("boom")
    monkeypatch.setattr(fapi, "ask_question", boom)
    with caplog.at_level(logging.INFO):
        res = fapi.lambda_handler({"httpMethod": "POST", "path": "/api/ask"}, None)
    assert res["statusCode"] == 500
    assert len([r for r in caplog.records if "LEGACY_CALL" in r.getMessage()]) == 1
