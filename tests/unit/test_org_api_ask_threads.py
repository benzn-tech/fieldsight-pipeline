"""org-api: GET/DELETE /api/org/ask/threads[/{id}] (spec 2026-10-22 ask conversation
history §5).

1. Identity is the authorizer sub only: a caller sees and deletes only their own threads.
2. The source manifest never leaves the server, and a turn the §6 re-check withholds
   carries `withheld: true` instead of its result.
3. An S3 AccessDenied is a 5xx, never "no conversations".
4. Flag off: every route is a 404. An archived caller is refused by the inherited guard.
"""
import io
import json
import time
from datetime import datetime, timezone

import pytest

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")
from botocore.exceptions import ClientError  # noqa: E402

import ask_thread_check  # noqa: E402
import ask_threads  # noqa: E402

SUB, OTHER_SUB = "sub-1", "sub-2"
CALLER = {
    "id": "u-uuid-1", "cognito_sub": SUB, "company_id": "c-uuid-1",
    "email": "a@x.nz", "first_name": "Ada", "last_name": "L", "folder_name": "Ada_L",
    "avatar_s3_key": None, "global_role": "member", "created_at": "2026-08-04",
    "archived_at": None,
}
KEY = "extractions/Ada_L/2026-10-20/sid" + "a" * 32


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class FakeS3:
    def __init__(self):
        self.objs = {}
        self.deletes = []
        self.denied = None

    def _deny(self, op):
        if self.denied == op:
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": "x"}}, op)

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.objs[Key] = (Body, datetime.now(timezone.utc))

    def get_object(self, Bucket, Key):
        self._deny("GetObject")
        if Key not in self.objs:
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "x"}}, "GetObject")
        return {"Body": io.BytesIO(self.objs[Key][0])}

    def delete_object(self, Bucket, Key):
        self._deny("DeleteObject")
        self.deletes.append(Key)
        self.objs.pop(Key, None)

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):
        self._deny("ListObjectsV2")
        return {"Contents": [{"Key": k, "LastModified": v[1]}
                             for k, v in sorted(self.objs.items()) if k.startswith(Prefix)],
                "IsTruncated": False}


def _turn(i, answer="The slab is 200 mm [1]."):
    return {"turn_id": "%032x" % (i + 1), "question": "question %d" % i, "at": time.time(),
            "request": {"offer_web": False, "web_intent": False, "history_len": i},
            "result": {"answer": answer, "citations": [{"source_s3_key": KEY}]},
            "sources": {"record_keys": [KEY], "documents": [], "report_days": []}}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("ASK_THREADS", "true")
    fake = FakeS3()
    monkeypatch.setattr(org, "get_connection", lambda *a, **k: FakeConn())
    monkeypatch.setattr(org.device_heartbeat, "record", lambda *a, **k: None)
    monkeypatch.setattr(org, "s3", lambda: fake)
    monkeypatch.setattr(org, "S3_BUCKET", "data-bucket")
    state = type("E", (), {})()
    state.s3, state.caller, state.withheld, state.checked = fake, dict(CALLER), set(), []

    def withheld(conn, caller, turns):
        state.checked.append((caller["id"], [t.get("turn_id") for t in turns]))
        return set(state.withheld)

    monkeypatch.setattr(ask_thread_check, "withheld_turns", withheld)
    monkeypatch.setattr(org.users, "get_user_by_sub",
                        lambda conn, sub: dict(state.caller, cognito_sub=sub))
    return state


def call(method, path, sub=SUB):
    resp = org.lambda_handler({
        "httpMethod": method, "path": "/api/org" + path, "queryStringParameters": None,
        "body": None, "headers": {}, "requestContext": {"authorizer": {"claims": {"sub": sub}}},
    }, None)
    body = json.loads(resp["body"]) if resp.get("body") else None
    return resp["statusCode"], body


def seed(env, sub=SUB, n_turns=1):
    tid = None
    for i in range(n_turns):
        tid = ask_threads.append_turn(env.s3, "data-bucket", sub, tid, _turn(i),
                                      {"site_id": "s-1"}, time.time())["thread_id"]
    return tid


def test_flag_off_404_all_three(env, monkeypatch):
    tid = seed(env)
    monkeypatch.setenv("ASK_THREADS", "false")
    assert call("GET", "/ask/threads")[0] == 404
    assert call("GET", "/ask/threads/" + tid)[0] == 404
    assert call("DELETE", "/ask/threads/" + tid)[0] == 404
    assert ask_threads.thread_key(SUB, tid) in env.s3.objs


def test_list_returns_callers_threads_only(env):
    mine = seed(env, SUB, n_turns=2)
    theirs = seed(env, OTHER_SUB)
    status, body = call("GET", "/ask/threads")
    assert status == 200
    assert [t["thread_id"] for t in body["threads"]] == [mine]
    assert body["threads"][0]["title"] == "question 0" and body["threads"][0]["turns"] == 2
    status, body = call("GET", "/ask/threads", sub=OTHER_SUB)
    assert [t["thread_id"] for t in body["threads"]] == [theirs]


def test_list_empty_is_an_empty_list(env):
    assert call("GET", "/ask/threads") == (200, {"threads": []})


def test_get_strips_sources_and_marks_withheld(env):
    tid = seed(env, n_turns=2)
    env.withheld = {"%032x" % 2}
    status, body = call("GET", "/ask/threads/" + tid)
    assert status == 200
    assert body["thread_id"] == tid and body["scope"] == {"site_id": "s-1"}
    first, second = body["turns"]
    assert set(first) == {"turn_id", "question", "at", "request", "result"}
    assert first["result"]["answer"] == "The slab is 200 mm [1]."
    assert set(second) == {"turn_id", "question", "at", "request", "withheld"}
    assert second["withheld"] is True and second["question"] == "question 1"
    assert "sources" not in json.dumps(body) and "record_keys" not in json.dumps(body)
    # The re-check ran for this caller, over every stored turn.
    assert env.checked == [(CALLER["id"], ["%032x" % 1, "%032x" % 2])]


def test_get_unknown_404(env):
    seed(env)
    assert call("GET", "/ask/threads/" + "c" * 32)[0] == 404


def test_get_another_callers_thread_404(env):
    theirs = seed(env, OTHER_SUB)
    assert call("GET", "/ask/threads/" + theirs)[0] == 404
    assert call("DELETE", "/ask/threads/" + theirs)[0] == 204
    assert ask_threads.thread_key(OTHER_SUB, theirs) in env.s3.objs


@pytest.mark.parametrize("bad", ["..%2Fx", "C" * 32, "c" * 31, "x"])
def test_get_bad_id_404(env, bad):
    assert call("GET", "/ask/threads/" + bad)[0] == 404
    assert call("DELETE", "/ask/threads/" + bad)[0] == 404


def test_delete_idempotent_204(env):
    tid = seed(env)
    status, body = call("DELETE", "/ask/threads/" + tid)
    assert status == 204 and body is None
    assert ask_threads.thread_key(SUB, tid) not in env.s3.objs
    assert call("DELETE", "/ask/threads/" + tid)[0] == 204
    assert call("GET", "/ask/threads/" + tid)[0] == 404


def test_other_methods_404(env):
    tid = seed(env)
    assert call("POST", "/ask/threads")[0] == 404
    assert call("DELETE", "/ask/threads")[0] == 404
    assert call("PATCH", "/ask/threads/" + tid)[0] == 404


def test_archived_caller_403(env):
    tid = seed(env)
    env.caller = dict(CALLER, archived_at="2026-10-01")
    for method, path in (("GET", "/ask/threads"), ("GET", "/ask/threads/" + tid),
                         ("DELETE", "/ask/threads/" + tid)):
        status, body = call(method, path)
        assert status == 403 and body == {"error": "account archived"}
    assert ask_threads.thread_key(SUB, tid) in env.s3.objs


@pytest.mark.parametrize("op,method,path", [("ListObjectsV2", "GET", "/ask/threads"),
                                             ("GetObject", "GET", "/ask/threads"),
                                             ("GetObject", "GET", "/ask/threads/{tid}"),
                                             ("DeleteObject", "DELETE", "/ask/threads/{tid}")])
def test_access_denied_is_5xx_not_empty(env, op, method, path):
    tid = seed(env)
    env.s3.denied = op
    status, body = call(method, path.format(tid=tid))
    assert status >= 500
    assert body != {"threads": []} and "threads" not in (body or {})


def test_routes_log_no_text(env, caplog):
    caplog.set_level("DEBUG")
    tid = seed(env)
    call("GET", "/ask/threads")
    call("GET", "/ask/threads/" + tid)
    assert "question 0" not in caplog.text and KEY not in caplog.text
    assert "200 mm" not in caplog.text


# ---------------------------------------------------------------- template

import re  # noqa: E402
from pathlib import Path  # noqa: E402

TEMPLATE = Path(__file__).resolve().parents[2] / "src" / "template.yaml"


def _org_api_block():
    text = TEMPLATE.read_text(encoding="utf-8")
    start = text.index("\n  OrgApiFunction:\n")
    nxt = re.search(r"\n  [A-Za-z][A-Za-z0-9]*:\n", text[start + 1:])
    return text[start:start + 1 + nxt.start()] if nxt else text[start:]


def test_org_api_reads_the_flag_and_the_data_bucket():
    block = _org_api_block()
    assert "ASK_THREADS: !Ref AskThreads" in block
    assert "S3_BUCKET: !Ref DataBucketName" in block


def test_org_api_may_read_and_delete_threads_but_not_write_them():
    block = _org_api_block()
    m = re.search(r"Action:\s*\n\s*- s3:GetObject\s*\n\s*- s3:DeleteObject\s*\n"
                  r"\s*Resource: !Sub arn:aws:s3:::\$\{DataBucketName\}/ask_threads/\*", block)
    assert m, "OrgApiFunction has no Get/Delete grant on ask_threads/*"
    puts = re.findall(r"s3:PutObject[^\n]*\n(?:\s*- s3:\w+\n)*\s*Resource: !Sub (\S+)", block)
    assert not [r for r in puts if "ask_threads" in r]


def test_org_api_lists_the_thread_prefix():
    block = _org_api_block()
    m = re.search(r"Action: s3:ListBucket\s*\n\s*Resource: !Sub arn:aws:s3:::"
                  r"\$\{DataBucketName\}\s*\n\s*Condition:\s*\n\s*StringLike:\s*\n"
                  r"\s*s3:prefix:\s*\n((?:\s*(?:- \S+|#[^\n]*)\n)+)", block)
    assert m
    prefixes = {line.strip().lstrip("- ").strip('"') for line in m.group(1).splitlines()
                if line.strip() and not line.strip().startswith("#")}
    assert "ask_threads/*" in prefixes


# ------------------------------------------------------- lifecycle backstop

def test_lifecycle_script_expires_ask_threads_at_31_days():
    """The data bucket lives outside the stack, so the 31-day backstop (spec §3) is in
    scripts/wire-bucket-lifecycle.sh. The script refuses rules it does not manage,
    so its own rule must also be in the exclusion query."""
    script = (Path(__file__).resolve().parents[2] / "scripts" / "wire-bucket-lifecycle.sh"
              ).read_text(encoding="utf-8")
    body = re.search(r"--lifecycle-configuration '(\{.*?\})'", script, re.S).group(1)
    rules = {r["ID"]: r for r in json.loads(body)["Rules"]}
    rule = rules.get("ask-threads-expiry")
    assert rule and rule["Status"] == "Enabled"
    assert rule["Filter"]["Prefix"] == "ask_threads/"
    assert rule["Expiration"]["Days"] == 31
    assert "ID!=`ask-threads-expiry`" in script



@pytest.mark.parametrize("bad_id", [None, "", 7])
def test_turn_without_a_turn_id_is_withheld(env, bad_id):
    """Fail closed: a stored turn the re-check cannot address is never shown."""
    tid = seed(env, n_turns=2)
    key = ask_threads.thread_key(SUB, tid)
    doc = json.loads(env.s3.objs[key][0])
    if bad_id is None:
        del doc["turns"][0]["turn_id"]
    else:
        doc["turns"][0]["turn_id"] = bad_id
    env.s3.objs[key] = (json.dumps(doc).encode("utf-8"), env.s3.objs[key][1])
    status, body = call("GET", "/ask/threads/" + tid)
    assert status == 200
    first, second = body["turns"]
    assert first["withheld"] is True and "result" not in first
    assert second["result"]["answer"] == "The slab is 200 mm [1]."
