"""org-api: the action pack routes and internal tasks (spec 2026-11-01 background action agent
§7, §10, §14 S1/S9/S13/S15; Task 6).

Pinned here with fakes (no SQL runs -- tests/integration/test_org_api_action_packs.py runs
the real queries):
1. ACTION_PACKS off / unset / unknown -> every pack route 404s exactly like an unknown route;
   internal -> platform_admin only; on -> everyone. Internal tasks refuse unless internal/on.
2. The create body is whitelisted; the instruction is 1..2000 chars after strip; site_id is
   required when the caller sees more than one site and an unseen site is a 404.
3. The S3 kickoff object never carries the instruction; a failed put fails the pack.
4. Cancel: creator or admin/GM of the site's company only; archive/dismiss for readers.
5. The store task rejects a payload over the size cap with an error code.
"""
import json

import pytest

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")
import action_pack_view  # noqa: E402

SITE_A = "11111111-1111-1111-1111-111111111111"
SITE_B = "22222222-2222-2222-2222-222222222222"
PACK = "33333333-3333-3333-3333-333333333333"
ACTION = "44444444-4444-4444-4444-444444444444"
CO = "c0000000-0000-0000-0000-000000000001"


def _caller(role="worker", uid="u-1"):
    return {"id": uid, "cognito_sub": "sub-" + uid, "company_id": CO, "email": "a@x.nz",
            "first_name": "Ada", "last_name": "L", "folder_name": "Ada_L",
            "global_role": role, "archived_at": None}


class FakeConn:
    def __init__(self):
        self.commits = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def commit(self):
        self.commits += 1

    def transaction(self):
        import contextlib
        return contextlib.nullcontext()


SNAP_KEY = f"action_packs/{CO}/{PACK}/web/W1.txt"
WEB = {"ref": "W1", "kind": "web_source", "provenance": "web", "url": "https://e.x/a",
       "fetched_at": "2026-11-01T00:00:00+00:00", "snapshot_s3_key": SNAP_KEY,
       "excerpt": "page"}


class FakeS3:
    def __init__(self, fail=False):
        self.puts = []
        self.fail = fail
        self.objs = {SNAP_KEY: b"stored page text"}
        self.get_error = None
        self.gets = []

    def get_object(self, Bucket, Key):
        import io
        from botocore.exceptions import ClientError
        if self.get_error:
            raise ClientError({"Error": {"Code": self.get_error, "Message": "x"}}, "GetObject")
        if Key not in self.objs:
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "x"}}, "GetObject")
        self.gets.append((Bucket, Key))
        return {"Body": io.BytesIO(self.objs[Key])}

    def put_object(self, Bucket, Key, Body, ContentType=None):
        if self.fail:
            raise RuntimeError("s3 down")
        self.puts.append((Bucket, Key, Body))


def _pack(**over):
    p = {"id": PACK, "site_id": SITE_A, "company_id": CO, "created_by": "u-1",
         "title": "T", "instruction": "secret instruction text", "reading": "R",
         "conclusion": [], "state": "ready", "failure": None, "archived_at": None,
         "as_of": None, "created_at": "2026-11-01", "updated_at": "2026-11-01",
         "sources": [dict(WEB)], "actions": []}
    p.update(over)
    return p


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("ACTION_PACKS", "on")
    st = type("S", (), {})()
    st.caller = _caller()
    st.s3 = FakeS3()
    st.conn = FakeConn()
    st.sites = [SITE_A]
    st.pack = _pack()
    st.calls = []
    monkeypatch.setattr(org.device_heartbeat, "record", lambda *a, **k: None)
    monkeypatch.setattr(org.users, "get_user_by_sub", lambda conn, sub: st.caller)
    monkeypatch.setattr(org.tenancy_status, "caller_blocked", lambda *a, **k: None)
    monkeypatch.setattr(org.tenancy_status, "write_refusal", lambda *a, **k: None)
    monkeypatch.setattr(org, "s3", lambda: st.s3)
    monkeypatch.setattr(org, "S3_BUCKET", "data-bucket")
    monkeypatch.setattr(org, "LAKE_BUCKET", "lake-bucket")
    monkeypatch.setattr(org.scope, "visible_scope",
                        lambda conn, caller: {"site_ids": set(st.sites), "author_ids": None})
    monkeypatch.setattr(org.memberships, "caller_site_roles", lambda conn, uid: {})
    monkeypatch.setattr(org.sites, "get_site",
                        lambda conn, sid: {"id": sid, "company_id": CO, "name": "Site"})
    monkeypatch.setattr(org.sites, "list_sites_by_ids",
                        lambda conn, ids: [{"id": i, "name": "Site"} for i in ids])

    def create_queued(conn, *, site_id, created_by, instruction):
        st.calls.append(("create", site_id, created_by, instruction))
        return {"id": PACK, "site_id": site_id, "company_id": CO, "state": "queued"}

    def fail(conn, pack_id, reason, run_trace=None):
        st.calls.append(("fail", pack_id, reason))
        return True

    ap = org.action_packs
    monkeypatch.setattr(ap, "create_queued", create_queued)
    monkeypatch.setattr(ap, "fail", fail)
    monkeypatch.setattr(ap, "get_for_reader", lambda conn, reader, pid: st.pack)
    monkeypatch.setattr(ap, "list_for_reader", lambda conn, reader, **kw: [])
    monkeypatch.setattr(ap, "sweep_stale", lambda conn, older_than: 0)
    monkeypatch.setattr(ap, "archive", lambda conn, pid, archived: True)
    monkeypatch.setattr(ap, "cancel", lambda conn, pid: True)
    monkeypatch.setattr(ap, "set_action_state",
                        lambda conn, pid, aid, s, **kw: ("ok", {"id": aid, "state": s}))
    return st


def _call(st, method, route, body=None, query=None):
    ev = {"httpMethod": method, "path": "/api/org" + route, "queryStringParameters": query,
          "body": None if body is None else json.dumps(body), "headers": {},
          "requestContext": {"authorizer": {"claims": {"sub": st.caller["cognito_sub"]}}}}
    res = org.dispatch(st.conn, ev, method, route)
    return res["statusCode"], json.loads(res["body"]) if res.get("body") else None


ROUTES = [("POST", "/action-packs", {"instruction": "x"}),
          ("GET", "/action-packs", None),
          ("GET", f"/action-packs/{PACK}", None),
          ("POST", f"/action-packs/{PACK}/cancel", {}),
          ("POST", f"/action-packs/{PACK}/archive", {"archived": True}),
          ("POST", f"/action-packs/{PACK}/actions/{ACTION}/dismiss", {}),
          ("GET", f"/action-packs/{PACK}/export", None),
          ("GET", f"/action-packs/{PACK}/sources/W1/snapshot", None)]


# ---------------------------------------------------------------- 1. the switch

@pytest.mark.parametrize("flag", [None, "off", "", "true", "ON-ish", "yes"])
@pytest.mark.parametrize("role", ["platform_admin", "admin", "worker"])
@pytest.mark.parametrize("method,route,body", ROUTES)
def test_flag_off_every_route_is_an_unknown_route(env, monkeypatch, flag, role, method,
                                                  route, body):
    if flag is None:
        monkeypatch.delenv("ACTION_PACKS", raising=False)
    else:
        monkeypatch.setenv("ACTION_PACKS", flag)
    env.caller = _caller(role)
    unknown = _call(env, method, "/no-such-route-at-all", body)
    assert _call(env, method, route, body) == unknown == (404, {"error": "not found"})
    assert env.calls == [] and env.s3.puts == []


@pytest.mark.parametrize("method,route,body", ROUTES)
@pytest.mark.parametrize("role,allowed", [("platform_admin", True), ("admin", False),
                                          ("worker", False)])
def test_flag_internal_is_platform_admin_only(env, monkeypatch, method, route, body, role,
                                              allowed):
    monkeypatch.setenv("ACTION_PACKS", "internal")
    env.caller = _caller(role)
    status, payload = _call(env, method, route, body, query={"format": "md"})
    if allowed:
        assert (status, payload) != (404, {"error": "not found"})
    else:
        assert (status, payload) == (404, {"error": "not found"})


@pytest.mark.parametrize("method,route,body", ROUTES)
@pytest.mark.parametrize("role", ["platform_admin", "admin", "worker"])
def test_flag_on_everyone_reaches_the_routes(env, method, route, body, role):
    env.caller = _caller(role)
    status, payload = _call(env, method, route, body, query={"format": "md"})
    assert (status, payload) != (404, {"error": "not found"})


@pytest.mark.parametrize("method,route", [("PUT", "/action-packs"), ("DELETE", f"/action-packs/{PACK}"),
                                          ("GET", f"/action-packs/{PACK}/cancel"),
                                          ("POST", f"/action-packs/{PACK}/actions/{ACTION}/nope"),
                                          ("GET", "/action-packs/not-a-uuid"),
                                          ("POST", f"/action-packs/{PACK}/sources/W1/snapshot"),
                                          ("GET", f"/action-packs/{PACK}/sources/../snapshot"),
                                          ("GET", f"/action-packs/{PACK}/sources/W%2F1/snapshot")])
def test_unknown_pack_subroutes_and_malformed_ids_404(env, method, route):
    assert _call(env, method, route, {})[0] == 404


@pytest.mark.parametrize("flag", [None, "off", "garbage"])
@pytest.mark.parametrize("task", ["action_pack_claim", "action_pack_heartbeat",
                                  "action_pack_store", "action_pack_fail"])
def test_internal_tasks_refuse_when_off_and_open_no_connection(monkeypatch, flag, task):
    if flag is None:
        monkeypatch.delenv("ACTION_PACKS", raising=False)
    else:
        monkeypatch.setenv("ACTION_PACKS", flag)
    monkeypatch.setattr(org, "get_connection", lambda *a, **k: pytest.fail("connected"))
    res = org.lambda_handler({"task": task, "pack_id": PACK, "payload": {}, "reason": "internal"},
                             None)
    assert res["error"] == "disabled"
    if task == "action_pack_claim":
        assert res["claimed"] is False


def test_internal_task_is_not_reachable_through_the_gateway(env):
    # An API Gateway proxy event never has a top-level `task`; a body field is just a body.
    status, _ = _call(env, "POST", "/action-packs", {"task": "action_pack_claim"})
    assert status == 400


# ---------------------------------------------------------------- 2. create body

def test_create_writes_the_pack_then_the_request_object_without_the_instruction(env):
    status, body = _call(env, "POST", "/action-packs",
                         {"instruction": "  secret instruction text  "})
    assert status == 202 and body == {"id": PACK, "state": "queued"}
    assert env.calls == [("create", SITE_A, "u-1", "secret instruction text")]
    assert env.conn.commits == 1                      # durable before the worker is told
    [(bucket, key, raw)] = env.s3.puts
    assert bucket == "data-bucket" and key == f"action_pack_requests/{PACK}.json"
    obj = json.loads(raw)
    assert obj == {"pack_id": PACK, "caller_sub": "sub-u-1", "site_id": SITE_A}
    assert "secret" not in raw.decode() if isinstance(raw, bytes) else "secret" not in raw


def test_create_put_failure_fails_the_pack(env):
    env.s3.fail = True
    status, body = _call(env, "POST", "/action-packs", {"instruction": "do it"})
    assert status == 503
    assert ("fail", PACK, "internal") in env.calls


@pytest.mark.parametrize("body,status", [
    ({"instruction": "x", "extra": 1}, 400),
    ({"instruction": "x", "site_id": SITE_A, "company_id": CO}, 400),
    ({}, 400),
    ({"instruction": ""}, 400),
    ({"instruction": "   \n "}, 400),
    ({"instruction": 5}, 400),
    ({"instruction": "x" * 2001}, 400),
    ({"instruction": "x", "site_id": 7}, 400),
])
def test_create_body_whitelist(env, body, status):
    assert _call(env, "POST", "/action-packs", body)[0] == status
    assert env.calls == [] and env.s3.puts == []


def test_create_unknown_fields_are_named(env):
    status, body = _call(env, "POST", "/action-packs", {"instruction": "x", "zz": 1, "aa": 2})
    assert status == 400 and body["fields"] == ["aa", "zz"]


def test_create_malformed_json_is_400(env):
    ev = {"httpMethod": "POST", "path": "/api/org/action-packs", "body": "{nope",
          "headers": {}, "requestContext": {"authorizer": {"claims": {"sub": "sub-u-1"}}}}
    assert org.dispatch(env.conn, ev, "POST", "/action-packs")["statusCode"] == 400


def test_create_exactly_2000_chars_is_allowed(env):
    assert _call(env, "POST", "/action-packs", {"instruction": "x" * 2000})[0] == 202


def test_create_site_required_when_more_than_one_site(env):
    env.sites = [SITE_A, SITE_B]
    assert _call(env, "POST", "/action-packs", {"instruction": "x"})[0] == 400
    status, _ = _call(env, "POST", "/action-packs", {"instruction": "x", "site_id": SITE_B})
    assert status == 202 and env.calls[0][1] == SITE_B


@pytest.mark.parametrize("site", ["99999999-9999-9999-9999-999999999999", "not-a-uuid"])
def test_create_unseen_or_malformed_site_is_404(env, site):
    assert _call(env, "POST", "/action-packs", {"instruction": "x", "site_id": site}) == (
        404, {"error": "site not found"})
    assert env.calls == []


def test_create_no_visible_site_is_404(env):
    env.sites = []
    assert _call(env, "POST", "/action-packs", {"instruction": "x"})[0] == 404


def test_create_second_active_pack_is_409(env, monkeypatch):
    def busy(conn, **kw):
        raise org.action_packs.ActivePackExists("u-1")
    monkeypatch.setattr(org.action_packs, "create_queued", busy)
    assert _call(env, "POST", "/action-packs", {"instruction": "x"})[0] == 409
    assert env.s3.puts == []


def test_create_inactive_company_is_refused_before_any_write(env, monkeypatch):
    seen = []

    def refuse(conn, caller, company_id, where=""):
        seen.append(company_id)
        return "company_inactive"
    monkeypatch.setattr(org.tenancy_status, "write_refusal", refuse)
    assert _call(env, "POST", "/action-packs", {"instruction": "x"}) == (
        409, {"error": "company_inactive"})
    assert seen == [CO] and env.calls == [] and env.s3.puts == []


# ---------------------------------------------------------------- 4. ACL

def test_get_one_non_reader_is_404(env, monkeypatch):
    monkeypatch.setattr(org.action_packs, "get_for_reader", lambda conn, r, pid: None)
    for method, route, body in ROUTES[2:]:
        assert _call(env, method, route, body, query={"format": "md"})[0] == 404, route


@pytest.mark.parametrize("role,creator,status", [
    ("worker", True, 200), ("worker", False, 403), ("pm", False, 403),
    ("site_manager", False, 403), ("admin", False, 200), ("gm", False, 200),
    ("regional_manager", False, 403)])
def test_cancel_creator_or_company_admin_only(env, monkeypatch, role, creator, status):
    env.caller = _caller(role, uid="u-1" if creator else "u-2")
    env.pack = _pack(state="running")
    cancelled = []
    monkeypatch.setattr(org.action_packs, "cancel",
                        lambda conn, pid: cancelled.append(pid) or True)
    got, body = _call(env, "POST", f"/action-packs/{PACK}/cancel", {})
    assert got == status
    if status == 200:
        assert body == {"id": PACK, "state": "cancelled"} and cancelled == [PACK]
        assert env.s3.puts[0][1] == f"action_pack_requests/{PACK}.cancel"
        assert env.conn.commits == 1
    else:
        assert cancelled == [] and env.s3.puts == []


def test_cancel_admin_of_another_company_is_403(env, monkeypatch):
    env.caller = _caller("admin", uid="u-2")
    monkeypatch.setattr(org.sites, "get_site",
                        lambda conn, sid: {"id": sid, "company_id": "other-co", "name": "S"})
    monkeypatch.setattr(org.action_packs, "cancel", lambda conn, pid: pytest.fail("cancelled"))
    assert _call(env, "POST", f"/action-packs/{PACK}/cancel", {})[0] == 403


def test_cancel_finished_pack_is_409(env, monkeypatch):
    env.pack = _pack(state="ready")
    monkeypatch.setattr(org.action_packs, "cancel", lambda conn, pid: False)
    status, body = _call(env, "POST", f"/action-packs/{PACK}/cancel", {})
    assert status == 409 and body["state"] == "ready" and env.s3.puts == []


def test_cancel_marker_failure_still_cancels(env, monkeypatch):
    env.pack = _pack(state="queued")
    env.s3.fail = True
    monkeypatch.setattr(org.action_packs, "cancel", lambda conn, pid: True)
    assert _call(env, "POST", f"/action-packs/{PACK}/cancel", {}) == (
        200, {"id": PACK, "state": "cancelled"})


@pytest.mark.parametrize("body", [{}, {"archived": "yes"}, {"archived": True, "x": 1}])
def test_archive_body_whitelist(env, monkeypatch, body):
    monkeypatch.setattr(org.action_packs, "archive", lambda *a: pytest.fail("archived"))
    assert _call(env, "POST", f"/action-packs/{PACK}/archive", body)[0] == 400


def test_archive_reader_sets_the_flag(env, monkeypatch):
    env.caller = _caller("pm", uid="u-9")                 # any reader
    seen = []
    monkeypatch.setattr(org.action_packs, "archive",
                        lambda conn, pid, archived: seen.append((pid, archived)) or True)
    assert _call(env, "POST", f"/action-packs/{PACK}/archive", {"archived": False}) == (
        200, {"id": PACK, "archived": False})
    assert seen == [(PACK, False)]


def _dismiss_env(env, monkeypatch, outcome):
    env.pack = _pack(actions=[{"id": ACTION, "seq": 1, "type": "note", "refs": ["R1"],
                               "verifier": "supported", "state": "proposed"}],
                     sources=[{"ref": "R1", "kind": "web_source", "provenance": "web"}])
    seen = []

    def set_state(conn, pid, aid, new_state, applied_by=None, applied_result=None,
                  created_item_stable_id=None):
        seen.append((pid, aid, new_state, applied_by, applied_result))
        return outcome
    monkeypatch.setattr(org.action_packs, "set_action_state", set_state)
    return seen


def test_dismiss_ok(env, monkeypatch):
    seen = _dismiss_env(env, monkeypatch, ("ok", {"id": ACTION, "state": "dismissed"}))
    assert _call(env, "POST", f"/action-packs/{PACK}/actions/{ACTION}/dismiss", {}) == (
        200, {"id": ACTION, "state": "dismissed"})
    assert seen == [(PACK, ACTION, "dismissed", "u-1", None)]


def test_dismiss_not_found_and_illegal(env, monkeypatch):
    _dismiss_env(env, monkeypatch, ("not_found", None))
    other = "55555555-5555-5555-5555-555555555555"
    assert _call(env, "POST", f"/action-packs/{PACK}/actions/{other}/dismiss", {})[0] == 404
    _dismiss_env(env, monkeypatch, ("illegal", "applied"))
    assert _call(env, "POST", f"/action-packs/{PACK}/actions/{ACTION}/dismiss", {}) == (
        409, {"error": "action is applied", "state": "applied"})


def test_dismiss_on_archived_pack_is_409(env, monkeypatch):
    seen = _dismiss_env(env, monkeypatch, ("ok", {}))
    env.pack["archived_at"] = "2026-11-01"
    assert _call(env, "POST", f"/action-packs/{PACK}/actions/{ACTION}/dismiss", {})[0] == 409
    assert seen == []


def test_dismiss_body_must_be_empty(env, monkeypatch):
    seen = _dismiss_env(env, monkeypatch, ("ok", {}))
    assert _call(env, "POST", f"/action-packs/{PACK}/actions/{ACTION}/dismiss",
                 {"state": "applied"})[0] == 400
    assert seen == []


# ---------------------------------------------------------------- list / export

def test_list_sweep_failure_does_not_fail_the_list(env, monkeypatch):
    def boom(conn, older_than):
        raise RuntimeError("sweep broke")
    monkeypatch.setattr(org.action_packs, "sweep_stale", boom)
    assert _call(env, "GET", "/action-packs") == (200, {"packs": []})


@pytest.mark.parametrize("query", [{"state": "bogus"}, {"site_id": "x"}, {"archived": "maybe"}])
def test_list_bad_filters_are_400(env, query):
    assert _call(env, "GET", "/action-packs", query=query)[0] == 400


def test_export_docx_is_400_and_md_is_text(env):
    assert _call(env, "GET", f"/action-packs/{PACK}/export", query={"format": "docx"})[0] == 400
    assert _call(env, "GET", f"/action-packs/{PACK}/export", query={"format": "pdf"})[0] == 400
    status, body = _call(env, "GET", f"/action-packs/{PACK}/export", query={"format": "md"})
    assert status == 200 and body["format"] == "md" and body["content"].startswith("# T")
    assert body["filename"].endswith(".md")


# ---------------------------------------------------------------- 5. internal tasks

@pytest.fixture
def task_env(monkeypatch):
    monkeypatch.setenv("ACTION_PACKS", "internal")
    conn = FakeConn()
    monkeypatch.setattr(org, "get_connection", lambda *a, **k: conn)
    return conn


def test_store_rejects_an_oversized_payload_before_the_database(task_env, monkeypatch):
    monkeypatch.setattr(org.action_packs, "store_result",
                        lambda *a: pytest.fail("stored"))
    big = {"sources": [{"ref": "R1", "kind": "topic", "provenance": "records",
                        "excerpt": "x" * (action_pack_view.STORE_PAYLOAD_MAX_BYTES + 1)}]}
    assert org.lambda_handler({"task": "action_pack_store", "pack_id": PACK, "payload": big},
                              None) == {"error": "payload_too_large"}


def test_store_size_counts_escaped_non_ascii(task_env, monkeypatch):
    monkeypatch.setattr(org.action_packs, "store_result", lambda *a: pytest.fail("stored"))
    # 1.0 MB of CJK is 6 MB as \\uXXXX escapes -- the encoding the invoke carries.
    big = {"reading": "中" * 1_000_000}
    assert org.lambda_handler({"task": "action_pack_store", "pack_id": PACK, "payload": big},
                              None) == {"error": "payload_too_large"}


@pytest.mark.parametrize("outcome,expected", [("ok", {"ok": True}),
                                              ("refused", {"error": "refused"})])
def test_store_outcomes(task_env, monkeypatch, outcome, expected):
    monkeypatch.setattr(org.action_packs, "store_result", lambda conn, pid, p: outcome)
    assert org.lambda_handler({"task": "action_pack_store", "pack_id": PACK,
                               "payload": {"title": "t"}}, None) == expected


def test_store_invalid_payload_is_a_code(task_env, monkeypatch):
    def bad(conn, pid, p):
        raise ValueError("unknown pack keys: ['zz']")
    monkeypatch.setattr(org.action_packs, "store_result", bad)
    assert org.lambda_handler({"task": "action_pack_store", "pack_id": PACK,
                               "payload": {"zz": 1}}, None) == {
        "error": "invalid", "detail": "unknown_keys"}
    assert org.lambda_handler({"task": "action_pack_store", "pack_id": PACK,
                               "payload": []}, None) == {
        "error": "invalid", "detail": "payload_not_object"}


@pytest.mark.parametrize("task", ["action_pack_claim", "action_pack_heartbeat",
                                  "action_pack_store", "action_pack_fail"])
def test_tasks_reject_a_malformed_pack_id_before_sql(task_env, task):
    res = org.lambda_handler({"task": task, "pack_id": "nope", "payload": {},
                              "reason": "internal"}, None)
    assert res["error"] == "invalid" and res["detail"] == "pack_id"


def test_fail_task_rejects_an_unknown_reason(task_env, monkeypatch):
    monkeypatch.setattr(org.action_packs, "fail", lambda *a, **k: pytest.fail("failed"))
    assert org.lambda_handler({"task": "action_pack_fail", "pack_id": PACK,
                               "reason": "because"}, None) == {
        "error": "invalid", "detail": "reason"}


def test_heartbeat_caps_plan_strings(task_env, monkeypatch):
    monkeypatch.setattr(org.action_packs, "set_plan", lambda *a: pytest.fail("set"))
    res = org.lambda_handler({"task": "action_pack_heartbeat", "pack_id": PACK,
                              "title": "t" * 201}, None)
    assert res == {"error": "invalid", "detail": "title"}


# ---------------------------------------------------------------- template

def test_template_wires_the_switch_and_the_one_put_grant():
    import os
    path = os.path.join(os.path.dirname(org.__file__), "template.yaml")
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    start = text.index("\n  OrgApiFunction:")
    block = text[start:text.index("\n      Events:", start)]
    assert "ACTION_PACKS: !Ref ActionPacks" in block
    assert "${DataBucketName}/action_pack_requests/*" in block
    param = text[text.index("\n  ActionPacks:"):]
    param = param[:param.index("\n\n")]
    assert "Default: 'off'" in param and "AllowedValues: ['off', 'internal', 'on']" in param


# ---------------------------------------------------------------- fix round 1

def test_snapshot_returns_the_stored_text_from_the_lake(env):
    assert _call(env, "GET", f"/action-packs/{PACK}/sources/W1/snapshot") == (200, {
        "ref": "W1", "url": "https://e.x/a", "fetched_at": "2026-11-01T00:00:00+00:00",
        "text": "stored page text", "truncated": False})
    assert env.s3.gets == [("lake-bucket", SNAP_KEY)]


def test_snapshot_is_capped(env):
    env.s3.objs[SNAP_KEY] = b"x" * (action_pack_view.SNAPSHOT_MAX_BYTES + 10)
    status, body = _call(env, "GET", f"/action-packs/{PACK}/sources/W1/snapshot")
    assert status == 200 and body["truncated"] is True
    assert len(body["text"]) == action_pack_view.SNAPSHOT_MAX_BYTES


@pytest.mark.parametrize("source", [
    dict(WEB, kind="topic", provenance="records"),                          # not web
    dict(WEB, snapshot_s3_key=None),                                        # never stored
    dict(WEB, snapshot_s3_key=f"action_packs/{CO}/other-pack/web/W1.txt"),  # another pack
    dict(WEB, snapshot_s3_key="reports/secret.json"),                       # outside prefix
    dict(WEB, snapshot_s3_key=f"action_packs/{CO}/{PACK}/web/../../x.txt"),
])
def test_snapshot_refuses_non_web_or_foreign_keys(env, source):
    env.pack = _pack(sources=[source])
    assert _call(env, "GET", f"/action-packs/{PACK}/sources/W1/snapshot")[0] == 404
    assert env.s3.gets == []


def test_snapshot_missing_object_is_404_and_access_denied_is_not_swallowed(env):
    env.s3.objs.clear()
    assert _call(env, "GET", f"/action-packs/{PACK}/sources/W1/snapshot") == (
        404, {"error": "snapshot not found"})
    env.s3.get_error = "AccessDenied"
    from botocore.exceptions import ClientError
    with pytest.raises(ClientError):
        _call(env, "GET", f"/action-packs/{PACK}/sources/W1/snapshot")


def test_snapshot_unknown_ref_is_404(env):
    assert _call(env, "GET", f"/action-packs/{PACK}/sources/W9/snapshot")[0] == 404


def test_claim_with_the_creator_row_gone_fails_the_pack(task_env, monkeypatch):
    seen = []
    monkeypatch.setattr(org.action_packs, "claim", lambda conn, pid: True)
    monkeypatch.setattr(org.action_packs, "get_for_worker", lambda conn, pid: None)
    monkeypatch.setattr(org.action_packs, "fail",
                        lambda conn, pid, reason, run_trace=None: seen.append(reason) or True)
    assert org.lambda_handler({"task": "action_pack_claim", "pack_id": PACK}, None) == {
        "claimed": False, "reason": "internal"}
    assert seen == ["internal"]


GOOD_SOURCE = {"ref": "R1", "kind": "topic", "provenance": "records"}
GOOD_ACTION = {"seq": 1, "type": "note", "refs": ["R1"], "verifier": "weak"}


@pytest.mark.parametrize("payload,detail", [
    ({"sources": "R1"}, "pack_field_type"),
    ({"actions": {"seq": 1}}, "pack_field_type"),
    ({"title": 5}, "pack_field_type"),
    ({"as_of": "yesterday"}, "pack_field_type"),
    ({"conclusion": ["text"]}, "conclusion_shape"),
    ({"conclusion": [{"text": "t", "refs": "R1"}]}, "conclusion_shape"),
    ({"conclusion": [{"text": "t", "confidence": "high"}]}, "conclusion_shape"),
    ({"sources": ["R1"]}, "source_shape"),
    ({"sources": [{"ref": "R1", "kind": "topic"}]}, "source_required"),
    ({"sources": [dict(GOOD_SOURCE, topic_id="not-a-uuid")]}, "source_field_type"),
    ({"sources": [dict(GOOD_SOURCE, revision="2")]}, "source_field_type"),
    ({"sources": [dict(GOOD_SOURCE, page_from=True)]}, "source_field_type"),
    ({"sources": [dict(GOOD_SOURCE, date="30/10/2026")]}, "source_field_type"),
    ({"sources": [dict(GOOD_SOURCE, excerpt=["x"])]}, "source_field_type"),
    ({"actions": [7]}, "action_shape"),
    ({"actions": [dict(GOOD_ACTION, seq="1")]}, "action_required"),
    ({"actions": [dict(GOOD_ACTION, seq=2**40)]}, "action_required"),
    ({"actions": [dict(GOOD_ACTION, refs="R1")]}, "action_required"),
    ({"actions": [dict(GOOD_ACTION, refs=[])]}, "action_required"),
    ({"actions": [dict(GOOD_ACTION, refs=[1])]}, "action_required"),
    ({"actions": [dict(GOOD_ACTION, proposed="done")]}, "action_field_type"),
    ({"actions": [dict(GOOD_ACTION, target_stable_id="69")]}, "action_field_type"),
])
def test_store_malformed_shapes_are_invalid_never_raised(task_env, monkeypatch, payload,
                                                          detail):
    monkeypatch.setattr(org.action_packs, "store_result", lambda *a: pytest.fail("stored"))
    assert org.lambda_handler({"task": "action_pack_store", "pack_id": PACK,
                               "payload": payload}, None) == {"error": "invalid",
                                                             "detail": detail}


def test_store_type_error_backstop(task_env, monkeypatch):
    def boom(conn, pid, p):
        raise TypeError("unexpected")
    monkeypatch.setattr(org.action_packs, "store_result", boom)
    assert org.lambda_handler({"task": "action_pack_store", "pack_id": PACK,
                               "payload": {"sources": [GOOD_SOURCE],
                                           "actions": [GOOD_ACTION]}}, None) == {
        "error": "invalid", "detail": "invalid_payload"}


def test_well_formed_payload_passes_the_shape_check():
    assert action_pack_view.payload_shape_error({
        "title": "t", "reading": "r", "as_of": "2026-11-01T00:00:00Z",
        "conclusion": [{"text": "c", "refs": ["R1"], "confidence": "supported"}],
        "sources": [dict(GOOD_SOURCE, topic_id="11111111-1111-1111-1111-111111111111",
                         date="2026-10-30", revision=None, page_from=3)],
        "actions": [dict(GOOD_ACTION, proposed={"status": "done"})]}) is None


def test_create_and_get_one_run_the_sweep(env, monkeypatch):
    swept = []
    monkeypatch.setattr(org.action_packs, "sweep_stale",
                        lambda conn, older_than: swept.append(older_than) or 0)
    _call(env, "POST", "/action-packs", {"instruction": "x"})
    _call(env, "GET", f"/action-packs/{PACK}")
    assert len(swept) == 2


def test_get_one_and_create_survive_a_sweep_error(env, monkeypatch):
    def boom(conn, older_than):
        raise RuntimeError("sweep broke")
    monkeypatch.setattr(org.action_packs, "sweep_stale", boom)
    assert _call(env, "GET", f"/action-packs/{PACK}")[0] == 200
    assert _call(env, "POST", "/action-packs", {"instruction": "x"})[0] == 202
