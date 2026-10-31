"""The company gate at every caller resolution (spec 2026-11-01-company-offboarding
section 5.1, Task 3).

Each caller site is driven blocked/unblocked x flag on/off:
  * unblocked (company active)            -> today's response
  * blocked, flag OFF (shadow, R1)        -> byte-identical to today's response, plus one
                                             COMPANY_GATE_WOULD_BLOCK log line
  * blocked, flag ON                      -> the site's refusal shape
  * platform_admin in a suspended company -> today's response (R5)

The company's status is the one thing a connection double cannot answer, so tests set
it by patching tenancy_status.company_status (tests/unit/conftest.py defaults it to
'active'). The real SQL is proven in tests/integration/test_tenancy_status.py and
tests/integration/test_company_gate_org_api.py.
"""
import json
import logging

import pytest

import tenancy_status

FLAG = "COMPANY_STATUS_ENFORCE"
TOKEN = "COMPANY_GATE_WOULD_BLOCK"


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _status(monkeypatch, value):
    monkeypatch.setattr(tenancy_status, "company_status", lambda conn, company_id: value)


def _flag(monkeypatch, on):
    if on:
        monkeypatch.setenv(FLAG, "on")
    else:
        monkeypatch.delenv(FLAG, raising=False)


def _would_block(caplog):
    return [r for r in caplog.records if TOKEN in r.getMessage()]


def _run_matrix(monkeypatch, caplog, call):
    """(baseline, shadow, enforced) responses for one caller site, asserting the
    shadow log line along the way."""
    _flag(monkeypatch, False)
    _status(monkeypatch, "active")
    caplog.clear()
    baseline = call()
    assert _would_block(caplog) == []

    _status(monkeypatch, "suspended")
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        shadow = call()
    assert len(_would_block(caplog)) == 1, "flag off logs exactly one WOULD_BLOCK"
    assert "company_inactive" in _would_block(caplog)[0].getMessage()

    _flag(monkeypatch, True)
    caplog.clear()
    enforced = call()
    assert _would_block(caplog) == []

    # Flag on, company active: today's response.
    _status(monkeypatch, "active")
    assert call() == baseline
    return baseline, shadow, enforced


# --------------------------------------------------------------------------- org-api

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")

ORG_CALLER = {
    "id": "u-uuid-1", "cognito_sub": "sub-1", "company_id": "c-uuid-1",
    "email": "a@x.nz", "first_name": "Ada", "last_name": "L", "folder_name": "Ada_L",
    "avatar_s3_key": None, "global_role": "admin", "created_at": "2026-07-25",
    "archived_at": None,
}


def _org_event(method, route, sub="sub-1"):
    return {"httpMethod": method, "path": "/api/org" + route, "queryStringParameters": None,
            "body": None, "requestContext": {"authorizer": {"claims": {"sub": sub}}},
            "headers": {}}


@pytest.fixture
def org_wired(monkeypatch):
    caller = dict(ORG_CALLER)
    monkeypatch.setattr(org, "get_connection", lambda *a, **k: FakeConn())
    monkeypatch.setattr(org.users, "get_user_by_sub",
                        lambda conn, sub: dict(caller) if sub == "sub-1" else None)
    monkeypatch.setattr(org, "list_org_sites",
                        lambda conn, c, event: org.ok({"sites": ["s-1"], "by": c["id"]}))
    # get_me's reads, so /me can be compared byte for byte.
    monkeypatch.setattr(org, "GRADED_ROLES", False)
    monkeypatch.setattr(org, "_stamp_last_seen", lambda conn, c: None)
    monkeypatch.setattr(org.memberships, "accessible_site_ids", lambda conn, uid, role: ["s-1"])
    monkeypatch.setattr(org.memberships, "caller_site_roles", lambda conn, uid: {})
    monkeypatch.setattr(org.companies, "get_company_by_id",
                        lambda conn, cid: {"id": cid, "name": "Acme"})
    return caller


def _org(method, route):
    return org.lambda_handler(_org_event(method, route), None)


def test_org_api_normal_route_matrix(org_wired, monkeypatch, caplog):
    base, shadow, enforced = _run_matrix(monkeypatch, caplog, lambda: _org("GET", "/sites"))
    assert base["statusCode"] == 200
    assert shadow == base
    assert enforced["statusCode"] == 403
    assert json.loads(enforced["body"]) == {"error": "company_inactive"}


def test_org_api_get_me_matrix(org_wired, monkeypatch, caplog):
    base, shadow, enforced = _run_matrix(monkeypatch, caplog, lambda: _org("GET", "/me"))
    assert base["statusCode"] == 200 and "blocked" not in json.loads(base["body"])
    assert shadow == base                     # flag off: /me byte-identical
    assert enforced["statusCode"] == 200
    body = json.loads(enforced["body"])
    assert body["blocked"] == {"reason": "company_inactive"}
    assert {k: v for k, v in body.items() if k != "blocked"} == json.loads(base["body"])


def test_org_api_patch_me_is_blocked(org_wired, monkeypatch):
    _flag(monkeypatch, True)
    _status(monkeypatch, "suspended")
    res = _org("PATCH", "/me")
    assert res["statusCode"] == 403 and json.loads(res["body"]) == {"error": "company_inactive"}


def test_org_api_platform_admin_is_exempt(org_wired, monkeypatch, caplog):
    org_wired["global_role"] = "platform_admin"
    _flag(monkeypatch, False)
    base_sites, base_me = _org("GET", "/sites"), _org("GET", "/me")
    _flag(monkeypatch, True)
    _status(monkeypatch, "exited")
    assert _org("GET", "/sites") == base_sites
    assert _org("GET", "/me") == base_me
    assert _would_block(caplog) == []


def test_org_api_archived_check_is_unchanged_whatever_the_flag(org_wired, monkeypatch):
    org_wired["archived_at"] = "2026-10-01"
    for on in (False, True):
        _flag(monkeypatch, on)
        res = _org("GET", "/sites")
        assert res["statusCode"] == 403
        assert json.loads(res["body"]) == {"error": "account archived"}


def test_org_api_device_status_and_heartbeat_stay_before_the_gate(org_wired, monkeypatch):
    _flag(monkeypatch, True)
    _status(monkeypatch, "suspended")
    seen = {}
    monkeypatch.setattr(org.device_heartbeat, "record",
                        lambda conn, ident, sub: seen.setdefault("heartbeat", sub))
    monkeypatch.setattr(org.device_heartbeat, "device_id", lambda conn, ident: "d-1")
    monkeypatch.setattr(org.device_status, "record",
                        lambda conn, did, body: {"recorded": True})
    res = _org("POST", "/device/status")
    assert res["statusCode"] == 200 and json.loads(res["body"]) == {"recorded": True}
    assert seen["heartbeat"] == "sub-1"
    # ... and a blocked caller's heartbeat is still recorded on a refused route.
    seen.clear()
    assert _org("GET", "/sites")["statusCode"] == 403
    assert seen["heartbeat"] == "sub-1"


# --------------------------------------------------------------------------- rag-search

rag = pytest.importorskip("lambda_rag_search", reason="requires psycopg (installed in CI)")

RAG_CALLER = {"id": "u-uuid-1", "cognito_sub": "sub-1", "company_id": "c-uuid-1",
              "global_role": "admin", "archived_at": None}


@pytest.fixture
def rag_wired(monkeypatch):
    monkeypatch.setattr(rag, "get_cached_connection", lambda *a, **k: FakeConn())
    monkeypatch.setattr(rag, "close_cached_connection", lambda: None)
    monkeypatch.setattr(rag.users, "get_user_by_sub",
                        lambda conn, sub: dict(RAG_CALLER) if sub == "sub-1" else None)
    # Deny-by-default reach: every path answers its own empty result without SQL.
    monkeypatch.setattr(rag.scope, "visible_scope",
                        lambda conn, caller: {"site_ids": [], "author_ids": None,
                                              "user_scope": "ALL"})
    return monkeypatch


def test_rag_search_records_matrix(rag_wired, monkeypatch, caplog):
    ev = {"sub": "sub-1", "query_embedding": [0.1] * 1024, "question": "pour"}
    base, shadow, enforced = _run_matrix(monkeypatch, caplog, lambda: rag.lambda_handler(ev, None))
    assert "error" not in base
    assert shadow == base
    assert enforced["chunks"] == [] and enforced["error"] == "company_inactive"
    # The envelope is the not-provisioned one: basis and applied still carried.
    assert set(enforced) == {"chunks", "error", "basis", "applied"}


def test_rag_search_metric_matrix(rag_wired, monkeypatch, caplog):
    ev = {"mode": "metric", "sub": "sub-1", "metric": "duration", "date_from": "2026-10-01"}
    base, shadow, enforced = _run_matrix(monkeypatch, caplog, lambda: rag.lambda_handler(ev, None))
    assert base["value"] == 0 and "error" not in base
    assert shadow == base
    assert enforced["error"] == "company_inactive"
    assert "value" not in enforced and "chunks" not in enforced   # metric's own envelope


def test_rag_search_documents_matrix(rag_wired, monkeypatch, caplog):
    import project_doc_flag
    monkeypatch.setattr(project_doc_flag, "on", lambda: True)
    ev = {"mode": "documents", "sub": "sub-1", "query_embedding": [0.1] * 1024}
    base, shadow, enforced = _run_matrix(monkeypatch, caplog, lambda: rag.lambda_handler(ev, None))
    assert base == {"documents": [], "site_count": 0}
    assert shadow == base
    assert enforced == {"documents": [], "error": "company_inactive"}


def test_rag_search_tools_matrix(rag_wired, monkeypatch, caplog):
    import rag_tools_dispatch
    monkeypatch.setattr(rag_tools_dispatch, "run",
                        lambda conn, caller, sc, event, **kw: {"results": [{"id": "1", "rows": ["r"]}]})
    ev = {"mode": "tools", "sub": "sub-1",
          "calls": [{"id": "1", "name": "search_records"}, {"id": "2", "name": "day_overview"}]}
    base, shadow, enforced = _run_matrix(monkeypatch, caplog, lambda: rag.lambda_handler(ev, None))
    assert base == {"results": [{"id": "1", "rows": ["r"]}]}
    assert shadow == base
    assert enforced == {
        "error": "company_inactive",
        "results": [{"id": "1", "name": "search_records", "rows": [],
                     "meta": {"error": "company_inactive"}},
                    {"id": "2", "name": "day_overview", "rows": [],
                     "meta": {"error": "company_inactive"}}]}


def test_rag_search_platform_admin_is_exempt(rag_wired, monkeypatch, caplog):
    rag_wired.setattr(rag.users, "get_user_by_sub",
                      lambda conn, sub: dict(RAG_CALLER, global_role="platform_admin"))
    ev = {"sub": "sub-1", "query_embedding": [0.1] * 1024}
    _flag(monkeypatch, False)
    base = rag.lambda_handler(ev, None)
    _flag(monkeypatch, True)
    _status(monkeypatch, "exited")
    assert rag.lambda_handler(ev, None) == base
    assert _would_block(caplog) == []


def test_rag_search_archived_caller_is_gated_behind_the_flag(rag_wired, monkeypatch):
    """rag-search never checked archived; it gets that check from the gate, flag on only."""
    rag_wired.setattr(rag.users, "get_user_by_sub",
                      lambda conn, sub: dict(RAG_CALLER, archived_at="2026-10-01"))
    ev = {"sub": "sub-1", "query_embedding": [0.1] * 1024}
    _flag(monkeypatch, False)
    assert "error" not in rag.lambda_handler(ev, None)
    _flag(monkeypatch, True)
    out = rag.lambda_handler(ev, None)
    assert out["chunks"] == [] and out["error"] == "account_archived"


# --------------------------------------------------------------------------- ws-connect

ws = pytest.importorskip("lambda_ws_connect", reason="requires psycopg import path")


def test_ws_connect_matrix(monkeypatch, caplog):
    upserts = []
    monkeypatch.setattr(ws, "get_connection", lambda *a, **k: FakeConn())
    monkeypatch.setattr(ws.users, "get_user_by_sub",
                        lambda c, sub: {"id": "u-1", "company_id": "c-1", "global_role": "worker"})
    monkeypatch.setattr(ws.ws_connections, "upsert_connection",
                        lambda c, cid, uid, coid: upserts.append(cid))
    ev = {"requestContext": {"connectionId": "conn-1", "authorizer": {"sub": "sub-1"}}}
    base, shadow, enforced = _run_matrix(monkeypatch, caplog, lambda: ws.lambda_handler(ev, None))
    assert base == {"statusCode": 200} and shadow == base
    assert enforced == {"statusCode": 403}      # refused like an unprovisioned caller
    assert len(upserts) == 3                    # base, shadow, final active; not enforced


# --------------------------------------------------------------------------- voice-resolve

vr = pytest.importorskip("lambda_voice_resolve", reason="requires psycopg import path")


def test_voice_resolve_matrix(monkeypatch, caplog):
    inserts = []
    monkeypatch.setattr(vr, "get_connection", lambda *a, **k: FakeConn())
    monkeypatch.setattr(vr.users, "get_user_by_sub",
                        lambda c, sub: {"id": "u-1", "company_id": "c-1", "global_role": "worker"})
    monkeypatch.setattr(vr.memberships, "accessible_site_ids", lambda c, uid, role: ["s-1"])

    def insert(c, coid, sid, uid, key, duration_s=None):
        inserts.append(key)
        return {"id": "m-1", "site_id": sid, "s3_key": key, "duration_s": duration_s,
                "created_at": "2026-07-18T00:00:00Z"}
    monkeypatch.setattr(vr.voice_messages, "insert_message", insert)
    monkeypatch.setattr(vr.ws_connections, "recipients_for_site", lambda c, coid, sid, uid: ["b"])
    ev = {"sub": "sub-1", "siteId": "s-1", "s3Key": "voice/c-1/s-1/x.wav", "durationS": 1.0}
    base, shadow, enforced = _run_matrix(monkeypatch, caplog, lambda: vr.lambda_handler(ev, None))
    assert base["statusCode"] == 200 and shadow == base
    assert enforced == {"statusCode": 403}
    assert len(inserts) == 3


# --------------------------------------------------------------------------- check-gaps-db

import lambda_check_gaps_db as hop  # noqa: E402


def test_check_gaps_db_matrix(monkeypatch, caplog):
    monkeypatch.setattr(hop, "get_cached_connection", lambda: FakeConn())
    monkeypatch.setattr(hop, "close_cached_connection", lambda: None)
    monkeypatch.setattr(hop.users, "get_user_by_sub",
                        lambda conn, sub: {"id": "u-1", "company_id": "c-1",
                                           "folder_name": "Ben_Lin", "global_role": "worker"})
    reads = []
    monkeypatch.setattr(hop.meeting_session, "latest_for_user_near",
                        lambda conn, uid, at, window_s=300: reads.append(uid))
    ev = {"caller_sub": "sub-1", "pressed_at": 1760000000}
    base, shadow, enforced = _run_matrix(monkeypatch, caplog, lambda: hop.lambda_handler(ev, None))
    assert base["user"]["id"] == "u-1" and shadow == base
    assert enforced == {"error": "company_inactive"}
    assert len(reads) == 3                       # nothing read for the refused caller


# --------------------------------------------------------------------------- checklist_reports

import checklist_reports as cr  # noqa: E402

CR_USER = {"id": "u-1", "cognito_sub": "sub-1", "company_id": "c-1",
           "folder_name": "Ben_Lin_test2", "global_role": "gm"}
CR_WINDOWS = [{"name": "level 2 pre-pour", "start_at": "11:02:03", "end_at": "11:03:21",
               "template_id": "t-1"},
              {"name": "steel", "start_at": "11:04:00", "end_at": "11:05:00",
               "template_id": "t-2"}]


def test_checklist_auto_generate_matrix(monkeypatch, caplog):
    calls, traces = [], []
    monkeypatch.setattr(cr.users, "get_by_folder_name", lambda c, co, f: CR_USER)
    monkeypatch.setattr(cr.users, "get_user_by_sub", lambda c, sub: dict(CR_USER))
    monkeypatch.setattr(cr.report_templates, "get_any",
                        lambda c, tid: {"id": tid, "name": "Checklist " + tid, "current_version": 1})
    monkeypatch.setattr(cr.topics_repo, "list_day_topics_for_binding", lambda c, f, d: [])
    monkeypatch.setattr(cr, "already_made", lambda *a, **k: False)
    monkeypatch.setattr(cr, "_record", lambda *a, **k: None)
    monkeypatch.setattr(cr.pipeline_trace, "event",
                        lambda stage, what, detail=None, **kw: traces.append((stage, what, detail)))

    def generate(conn, caller, date, event):
        calls.append(caller["id"])
        return {"statusCode": 200, "body": json.dumps({"requestId": "r%d" % len(calls)})}

    def run():
        calls.clear()
        traces.clear()
        out = cr.auto_generate(None, "c-1", "Ben_Lin_test2", "2026-10-06", "sidA", CR_WINDOWS,
                               generate=generate)
        return out, list(calls), list(traces)

    base, shadow, enforced = _run_matrix(monkeypatch, caplog, run)
    assert base[0] == [("level 2 pre-pour", "r1"), ("steel", "r2")]
    assert shadow == base
    out, gen_calls, tr = enforced
    assert out == [("level 2 pre-pour", "company_inactive"), ("steel", "company_inactive")]
    assert gen_calls == []                       # never sent to the worker
    assert tr == [("checklist_report", "company_inactive", {"check": "level 2 pre-pour"}),
                  ("checklist_report", "company_inactive", {"check": "steel"})]


# --------------------------------------------------------------------------- Ask

@pytest.mark.parametrize("reason", ["company_inactive", "account_archived"])
def test_ask_agent_takes_no_web_path_on_company_inactive(monkeypatch, reason):
    """ask-agent's web fallback/offer is skipped on any rag-search `error`
    (lambda_ask_agent ~1498); this locks that in for the gate's blocked shapes."""
    laa = pytest.importorskip("lambda_ask_agent")
    from tests.unit.test_ask_voice_web_gates import WEB_OK, FakeLambdaClient, ask, wire
    for offer in ({}, {"offer_web": True}):
        rig = wire(monkeypatch, chunks=(), web=WEB_OK)
        monkeypatch.setattr(laa, "_get_lambda_client", lambda: FakeLambdaClient(
            [{"chunks": [], "error": reason}]))
        out = ask(rig.cb(), **offer)
        assert "general" not in rig.events and "on_web_start" not in rig.events
        assert "classify" not in rig.events
        assert "from_web" not in out and "web_offer" not in out


@pytest.mark.parametrize("reason", ["company_inactive", "account_archived"])
def test_ask_orchestrator_company_inactive_gives_no_records_and_never_the_web(monkeypatch,
                                                                             reason):
    """The tools path (orchestration v2): rag-search's blocked tools envelope carries
    meta.error = the gate's reason on every call; it must end like not_provisioned."""
    pytest.importorskip("lambda_ask_agent")
    from tests.unit.test_ask_orchestrator_judge import ScriptChat, body, run
    from tests.unit.test_ask_orchestrator_round1 import FakeTools
    chat = ScriptChat()
    out = run(body("What does NZS 3101 say? search online"), chat,
              tools=FakeTools(lambda call: ([], {"error": reason})))
    assert out["answer"] == "No relevant records found for this question."
    assert out["citations"] == [] and "web" not in out and not out.get("from_web")
    assert chat.calls == []
