"""Screen Ask async turn: ApiFunction routes /api/ask/start, /result, /cancel
(plan 2026-10-16 ask-orchestration-v2, Task 2)."""
import io
import json
import os

import pytest

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")

fapi = pytest.importorskip("lambda_fieldsight_api", reason="requires boto3 (installed in CI)")
import botocore.exceptions  # noqa: E402
import ask_voice_turn as avt  # noqa: E402

CALLER = {"sub": "sub-1", "email": "", "name": "", "role": "worker", "display_name": ""}
NOSUB = dict(CALLER, sub="")
TURN = "0123456789abcdef0123456789abcdef"
BUCKET = "turn-bucket"


class ClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeS3:
    def __init__(self):
        self.objs = {}
        self.deleted = []

    def put_object(self, Bucket, Key, Body=b"", **kw):
        self.objs[(Bucket, Key)] = Body

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objs:
            raise ClientError("NoSuchKey")
        return {"Body": io.BytesIO(self.objs[(Bucket, Key)])}

    def delete_object(self, Bucket, Key):
        self.deleted.append(Key)
        self.objs.pop((Bucket, Key), None)


class Clock:
    def __init__(self):
        self.t = 1000.0
        self.sleeps = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


@pytest.fixture
def s3(monkeypatch):
    fake = FakeS3()
    monkeypatch.setattr(fapi, "_turn_s3", lambda: fake)
    monkeypatch.setenv("ASK_TURN_BUCKET", BUCKET)
    monkeypatch.delenv("RESULT_WAIT_S", raising=False)
    return fake


def put(s3, state, sub="sub-1", turn=TURN):
    avt.put_state(s3, BUCKET, sub, turn, state)


def result(q, caller=CALLER, clock=None):
    c = clock or Clock()
    return fapi.ask_voice_result(q, caller, clock=c.now, sleep=c.sleep, wall=c.now)


def body(r):
    return json.loads(r["body"])


class FakeLambda:
    def __init__(self, reply=None, function_error=None, raise_exc=None):
        self.calls = []
        self.reply = reply if reply is not None else {"turn_id": TURN}
        self.function_error = function_error
        self.raise_exc = raise_exc

    def invoke(self, FunctionName, InvocationType, Payload):
        self.calls.append((FunctionName, InvocationType, json.loads(Payload)))
        if self.raise_exc is not None:
            raise self.raise_exc
        resp = {"Payload": io.BytesIO(json.dumps(self.reply).encode())}
        if self.function_error:
            resp["FunctionError"] = self.function_error
        return resp


@pytest.fixture
def start_client(monkeypatch):
    fl = FakeLambda()
    monkeypatch.setattr(fapi, "ask_start_client", fl)
    return fl


@pytest.fixture
def ask_client(monkeypatch):
    fl = FakeLambda(reply={"answer": "a"})
    monkeypatch.setattr(fapi, "ask_lambda_client", fl)
    return fl


FULL_BODY = {
    "question": "  what happened?  ",
    "date": "2026-10-16",
    "user": "Ben",
    "scope": "both",
    "topic_id": "t-1",
    "tz": "Pacific/Auckland",
    "site_id": "s-1",
    "author_folder": "Ben_L",
    "topic_row_id": "r-1",
    "scoped": True,
    "history": [{"question": "q", "answer": "a"}, {"bad": 1}],
}


# ---- the shared builder ----

def test_build_payload_equals_what_ask_question_sends(ask_client):
    fapi.ask_question(dict(FULL_BODY), CALLER)
    sent = ask_client.calls[0][2]
    payload, err = fapi._build_ask_payload(dict(FULL_BODY), CALLER)
    assert err is None
    assert payload == sent
    assert payload["question"] == "what happened?"
    assert payload["history"] == [{"question": "q", "answer": "a"}]


def test_build_payload_whitelists_client_fields():
    hostile = {"question": "q", "now": "2020-01-01T00:00:00", "prompt_only": True,
               "mode": "voice", "caller_sub": "evil", "voice_web": True}
    payload, err = fapi._build_ask_payload(hostile, CALLER)
    assert err is None
    for k in ("now", "prompt_only", "mode", "voice_web"):
        assert k not in payload
    assert payload["caller_sub"] == "sub-1"


def test_build_payload_missing_question():
    payload, err = fapi._build_ask_payload({"question": "  "}, CALLER)
    assert payload is None and err["statusCode"] == 400


# ---- POST /api/ask/start ----

def test_start_payload_is_ask_payload_plus_mode(start_client, ask_client):
    r = fapi.ask_start(dict(FULL_BODY), CALLER)
    assert r["statusCode"] == 200 and body(r) == {"turn_id": TURN}
    fn, itype, sent = start_client.calls[0]
    assert fn == fapi.ASK_AGENT_FUNCTION and itype == "RequestResponse"
    fapi.ask_question(dict(FULL_BODY), CALLER)
    expected = dict(ask_client.calls[0][2], mode="turn_start")
    assert sent == expected


def test_start_hostile_body_cannot_set_mode_or_sub(start_client):
    hostile = {"question": "q", "now": "x", "prompt_only": True,
               "mode": "voice", "caller_sub": "evil"}
    fapi.ask_start(hostile, CALLER)
    sent = start_client.calls[0][2]
    assert sent["mode"] == "turn_start"
    assert sent["caller_sub"] == "sub-1"
    assert "now" not in sent and "prompt_only" not in sent


def test_start_no_sub_is_401_and_no_invoke(start_client):
    assert fapi.ask_start({"question": "q"}, NOSUB)["statusCode"] == 401
    assert start_client.calls == []


def test_start_missing_question_is_400(start_client):
    assert fapi.ask_start({"question": ""}, CALLER)["statusCode"] == 400
    assert start_client.calls == []


def test_start_passes_agent_error_through(monkeypatch):
    fl = FakeLambda(reply={"statusCode": 200, "body": json.dumps({"error": "Missing question"})})
    monkeypatch.setattr(fapi, "ask_start_client", fl)
    r = fapi.ask_start({"question": "q"}, CALLER)
    assert body(r) == {"error": "Missing question"}


def test_start_passes_api_format_reply_through(monkeypatch):
    fl = FakeLambda(reply={"statusCode": 200, "body": json.dumps({"turn_id": TURN})})
    monkeypatch.setattr(fapi, "ask_start_client", fl)
    r = fapi.ask_start({"question": "q"}, CALLER)
    assert r["statusCode"] == 200 and body(r) == {"turn_id": TURN}


def test_start_function_error_is_500_without_leak(monkeypatch):
    fl = FakeLambda(reply={"errorMessage": "secret", "stackTrace": ["x"]},
                    function_error="Unhandled")
    monkeypatch.setattr(fapi, "ask_start_client", fl)
    r = fapi.ask_start({"question": "q"}, CALLER)
    assert r["statusCode"] == 500
    assert "secret" not in r["body"] and "stackTrace" not in r["body"]


def test_start_read_timeout_is_504(monkeypatch):
    fl = FakeLambda(raise_exc=botocore.exceptions.ReadTimeoutError(endpoint_url="lambda"))
    monkeypatch.setattr(fapi, "ask_start_client", fl)
    assert fapi.ask_start({"question": "q"}, CALLER)["statusCode"] == 504


def test_start_other_exception_is_502(monkeypatch):
    fl = FakeLambda(raise_exc=RuntimeError("boom"))
    monkeypatch.setattr(fapi, "ask_start_client", fl)
    r = fapi.ask_start({"question": "q"}, CALLER)
    assert r["statusCode"] == 502 and "boom" not in r["body"]


def test_start_client_is_its_own_short_timeout_client():
    c = fapi.ask_start_client
    assert c is not fapi.ask_lambda_client and c is not fapi.lambda_client
    cfg = c.meta.config
    assert cfg.read_timeout == 8 and cfg.connect_timeout == 2
    # botocore normalises max_attempts=0 to total_max_attempts=1 (no retry).
    assert cfg.retries.get("total_max_attempts") == 1


# ---- GET /api/ask/result ----

def test_result_screen_done_returns_result_dict(s3):
    rag = {"answer": "a [1]", "sources": [{"id": 1}], "kind": "records", "web_offer": True}
    c = Clock()
    put(s3, {"status": "done", "channel": "screen", "result": rag, "created_at": c.t,
             "answerText": "must-not-leak"})
    r = result({"turn": TURN}, clock=c)
    assert r["statusCode"] == 200
    assert body(r) == {"status": "done", "result": rag}
    assert not s3.objs  # terminal turns deleted


def test_result_screen_done_with_rag_error_passes_through(s3):
    put(s3, {"status": "done", "channel": "screen", "result": {"error": "x"}})
    assert body(result({"turn": TURN})) == {"status": "done", "result": {"error": "x"}}


def test_result_voice_done_unchanged(s3):
    put(s3, {"status": "done", "answerText": "a", "transcript": "t", "result": {"x": 1}})
    assert body(result({"turn": TURN})) == {"status": "done", "answerText": "a", "transcript": "t"}


def test_result_screen_error(s3):
    put(s3, {"status": "error", "channel": "screen", "error": "No answer"})
    assert body(result({"turn": TURN})) == {"status": "error", "error": "No answer"}
    assert not s3.objs


def test_result_screen_progress_and_since(s3):
    c = Clock()
    put(s3, {"status": "searching_web", "channel": "screen", "created_at": c.t})
    assert body(result({"turn": TURN, "since": "pending"}, clock=c)) == {"status": "searching_web"}
    r = result({"turn": TURN, "since": "searching_web"}, clock=c)
    assert body(r) == {"status": "searching_web"}
    assert (BUCKET, avt.state_key("sub-1", TURN)) in s3.objs


# ---- dispatch / cancel / routes ----

def ev(method, path, qs=None, b=None, sub="sub-1"):
    return {"httpMethod": method, "path": path, "queryStringParameters": qs,
            "body": json.dumps(b) if b is not None else None,
            "requestContext": {"authorizer": {"claims": {"sub": sub}}}}


def test_known_routes():
    for p in ("/api/ask/start", "/api/ask/result", "/api/ask/cancel"):
        assert p in fapi.KNOWN_ROUTES


def test_dispatch_start(start_client):
    r = fapi.lambda_handler(ev("POST", "/api/ask/start", b={"question": "q"}), None)
    assert body(r) == {"turn_id": TURN}
    assert start_client.calls[0][2]["mode"] == "turn_start"
    assert fapi.lambda_handler(ev("POST", "/api/ask/start", b={"question": "q"}, sub=""),
                               None)["statusCode"] == 401
    assert fapi.lambda_handler(ev("GET", "/api/ask/start"), None)["statusCode"] == 404


def test_dispatch_result_and_cancel(s3):
    put(s3, {"status": "done", "channel": "screen", "result": {"answer": "a"}})
    r = fapi.lambda_handler(ev("GET", "/api/ask/result", qs={"turn": TURN}), None)
    assert body(r) == {"status": "done", "result": {"answer": "a"}}
    assert fapi.lambda_handler(ev("GET", "/api/ask/result", qs={"turn": TURN}),
                               None)["statusCode"] == 404
    r = fapi.lambda_handler(ev("POST", "/api/ask/cancel", b={"turn_id": TURN}), None)
    assert r["statusCode"] == 204
    assert avt.is_cancelled(s3, BUCKET, "sub-1", TURN)
    assert fapi.lambda_handler(ev("POST", "/api/ask/cancel", b={"turn_id": "bad"}),
                               None)["statusCode"] == 400
    assert fapi.lambda_handler(ev("POST", "/api/ask/cancel", b={"turn_id": TURN}, sub=""),
                               None)["statusCode"] == 401
