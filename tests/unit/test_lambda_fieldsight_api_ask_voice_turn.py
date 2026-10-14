"""Device Ask two-phase: ApiFunction routes start / result / cancel (spec 3.1-3.3)."""
import io
import json
import os

import pytest

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")

fapi = pytest.importorskip("lambda_fieldsight_api", reason="requires boto3 (installed in CI)")
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
        self.fail_delete = False

    def put_object(self, Bucket, Key, Body=b"", **kw):
        self.objs[(Bucket, Key)] = Body

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objs:
            raise ClientError("NoSuchKey")
        return {"Body": io.BytesIO(self.objs[(Bucket, Key)])}

    def delete_object(self, Bucket, Key):
        if self.fail_delete:
            raise ClientError("AccessDenied")
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


# ---- start ----

class FakeLambda:
    def __init__(self):
        self.calls = []

    def invoke(self, FunctionName, InvocationType, Payload):
        self.calls.append((FunctionName, InvocationType, json.loads(Payload)))
        return {"Payload": io.BytesIO(json.dumps({"turn_id": TURN}).encode())}


def test_start_payload_keys(monkeypatch):
    fl = FakeLambda()
    monkeypatch.setattr(fapi, "lambda_client", fl)
    r = fapi.ask_voice_start({"audio": "QUJD"}, CALLER)
    assert r["statusCode"] == 200 and body(r) == {"turn_id": TURN}
    assert fl.calls[0][1] == "RequestResponse"
    assert set(fl.calls[0][2]) == {"mode", "audio", "format", "caller_sub"}
    assert fl.calls[0][2]["mode"] == "voice_start"
    assert fl.calls[0][2]["caller_sub"] == "sub-1"


def test_start_forwards_tz_and_history(monkeypatch):
    fl = FakeLambda()
    monkeypatch.setattr(fapi, "lambda_client", fl)
    hist = [{"question": "a", "answer": "b"}]
    fapi.ask_voice_start({"audio": "QUJD", "tz": "Pacific/Auckland", "history": hist}, CALLER)
    keys = set(fl.calls[0][2])
    assert "tz" in keys and "history" in keys
    assert keys == {"mode", "audio", "format", "caller_sub", "tz", "history"}


def test_start_validation(monkeypatch):
    monkeypatch.setattr(fapi, "lambda_client", FakeLambda())
    assert fapi.ask_voice_start({"audio": "x"}, NOSUB)["statusCode"] == 401
    assert fapi.ask_voice_start({}, CALLER)["statusCode"] == 400
    big = {"audio": "A" * (fapi.MAX_VOICE_AUDIO_B64 + 1)}
    assert fapi.ask_voice_start(big, CALLER)["statusCode"] == 413


# ---- result ----

def test_result_401_without_sub(s3):
    assert result({"turn": TURN}, NOSUB)["statusCode"] == 401


def test_result_bad_turn_and_since(s3):
    assert result({"turn": "nope"})["statusCode"] == 400
    assert result({})["statusCode"] == 400
    assert result({"turn": TURN, "since": "done"})["statusCode"] == 400


def test_result_404_missing_and_other_users_turn(s3):
    assert result({"turn": TURN})["statusCode"] == 404
    put(s3, {"status": "done"}, sub="someone-else")
    assert result({"turn": TURN})["statusCode"] == 404


def test_result_returns_on_change_vs_since(s3):
    c = Clock()
    put(s3, {"status": "searching_web", "created_at": c.t})
    r = result({"turn": TURN, "since": "pending"}, clock=c)
    assert r["statusCode"] == 200 and body(r) == {"status": "searching_web"}
    assert c.sleeps == []
    assert (BUCKET, avt.state_key("sub-1", TURN)) in s3.objs  # not terminal: kept


def test_result_wait_capped_by_clock(s3):
    c = Clock()
    put(s3, {"status": "pending", "created_at": c.t})
    r = result({"turn": TURN}, clock=c)
    assert body(r) == {"status": "pending"}
    assert set(c.sleeps) == {0.4}
    assert 18.5 <= sum(c.sleeps) <= 19.5  # stops 1 s early


def test_result_wait_env(s3, monkeypatch):
    monkeypatch.setenv("RESULT_WAIT_S", "2")
    c = Clock()
    put(s3, {"status": "pending", "created_at": c.t})
    result({"turn": TURN}, clock=c)
    assert 0.7 <= sum(c.sleeps) <= 1.5


def test_result_done_passes_fields_and_deletes(s3):
    c = Clock()
    put(s3, {"status": "done", "transcript": "t", "answerText": "a", "basis": "b",
             "audioBase64": "QQ==", "audioFormat": "wav", "web": True, "created_at": c.t})
    r = result({"turn": TURN}, clock=c)
    assert body(r) == {"status": "done", "transcript": "t", "answerText": "a", "basis": "b",
                       "audioBase64": "QQ==", "audioFormat": "wav", "web": True}
    assert not s3.objs
    assert len(s3.deleted) == 2


def test_result_error_deletes(s3):
    put(s3, {"status": "error", "error": "No answer"})
    r = result({"turn": TURN})
    assert body(r) == {"status": "error", "error": "No answer"}
    assert not s3.objs


def test_result_cancel_wins_over_done(s3):
    put(s3, {"status": "done", "answerText": "a"})
    avt.put_cancel(s3, BUCKET, "sub-1", TURN)
    r = result({"turn": TURN})
    assert body(r) == {"status": "cancelled"}
    # The marker outlives the answer to the device: a worker still running must
    # keep seeing it, or it writes `done` and an audit row for a cancelled turn.
    assert list(s3.objs) == [(BUCKET, avt.cancel_key("sub-1", TURN))]


def test_result_stale_becomes_error(s3):
    c = Clock()
    put(s3, {"status": "pending", "created_at": c.t - 91})
    r = result({"turn": TURN}, clock=c)
    assert body(r) == {"status": "error", "error": "No answer"}
    # Given up on: a marker is written so a slow worker stops instead of
    # writing an orphan `done`.
    assert list(s3.objs) == [(BUCKET, avt.cancel_key("sub-1", TURN))]


def test_result_not_stale_at_60s(s3):
    c = Clock()
    put(s3, {"status": "searching_web", "created_at": c.t - 60})
    r = result({"turn": TURN, "since": "searching_web"}, clock=c)
    assert body(r) == {"status": "searching_web"}


def test_result_delete_failure_swallowed(s3, caplog):
    put(s3, {"status": "error", "error": "x"})
    s3.fail_delete = True
    with caplog.at_level("WARNING"):
        r = result({"turn": TURN})
    assert r["statusCode"] == 200
    assert "ClientError" in caplog.text


def test_result_access_denied_raises(s3, monkeypatch):
    def boom(*a, **k):
        raise ClientError("AccessDenied")
    monkeypatch.setattr(avt, "get_state", boom)
    with pytest.raises(ClientError):
        result({"turn": TURN})


def test_result_no_bucket_is_503(s3, monkeypatch):
    monkeypatch.delenv("ASK_TURN_BUCKET")
    assert result({"turn": TURN})["statusCode"] == 503


def test_result_searching_web_older_than_90s_is_error(s3):
    c = Clock()
    put(s3, {"status": "searching_web", "created_at": c.t - 91})
    assert body(result({"turn": TURN, "since": "searching_web"}, clock=c)) == {
        "status": "error", "error": "No answer"}


def test_result_cancel_marker_without_state_is_404(s3):
    avt.put_cancel(s3, BUCKET, "sub-1", TURN)
    assert result({"turn": TURN})["statusCode"] == 404


def test_result_s3_time_counts_against_wait(s3):
    c = Clock()
    put(s3, {"status": "pending", "created_at": c.t})
    orig = s3.get_object

    def slow(Bucket, Key):
        c.t += 0.5
        return orig(Bucket=Bucket, Key=Key)
    s3.get_object = slow
    r = result({"turn": TURN}, clock=c)
    assert body(r) == {"status": "pending"}
    assert c.t - 1000.0 <= 20.0  # never overruns the wait cap


@pytest.mark.parametrize("raw,expect", [("999", 25.0), ("-5", 0.0), ("nan", 20.0),
                                        ("abc", 20.0), ("7", 7.0)])
def test_wait_clamp(monkeypatch, raw, expect):
    monkeypatch.setenv("RESULT_WAIT_S", raw)
    assert fapi._result_wait_s() == expect


def test_turn_s3_client_is_bounded():
    avt._s3 = None
    try:
        cfg = avt._get_s3().meta.config
        assert cfg.connect_timeout == 1 and cfg.read_timeout == 2
        assert cfg.retries["total_max_attempts"] == 2 or cfg.retries["max_attempts"] == 1
    finally:
        avt._s3 = None


# ---- cancel ----

def test_cancel(s3):
    assert fapi.ask_voice_cancel({"turn_id": TURN}, NOSUB)["statusCode"] == 401
    assert fapi.ask_voice_cancel({"turn_id": "bad"}, CALLER)["statusCode"] == 400
    assert fapi.ask_voice_cancel({}, CALLER)["statusCode"] == 400
    r = fapi.ask_voice_cancel({"turn_id": TURN}, CALLER)  # unknown turn
    assert r["statusCode"] == 204 and r["body"] == ""
    assert r["headers"]["Access-Control-Allow-Origin"] == "*"
    assert avt.is_cancelled(s3, BUCKET, "sub-1", TURN)


# ---- dispatch ----

def ev(method, path, qs=None, b=None, sub="sub-1"):
    return {"httpMethod": method, "path": path, "queryStringParameters": qs,
            "body": json.dumps(b) if b is not None else None,
            "requestContext": {"authorizer": {"claims": {"sub": sub}}}}


def test_dispatch_and_known_routes(s3):
    for p in ("/api/ask/voice/start", "/api/ask/voice/result", "/api/ask/voice/cancel"):
        assert p in fapi.KNOWN_ROUTES
    other = "f" * 32
    assert fapi.lambda_handler(
        ev("POST", "/api/ask/voice/cancel", b={"turn_id": other}), None)["statusCode"] == 204
    put(s3, {"status": "error", "error": "x"})
    r = fapi.lambda_handler(ev("GET", "/api/ask/voice/result", qs={"turn": TURN}), None)
    assert body(r)["status"] == "error"
    assert fapi.lambda_handler(ev("GET", "/api/ask/voice/start"), None)["statusCode"] == 404
    assert fapi.lambda_handler(
        ev("GET", "/api/ask/voice/result", qs=None), None)["statusCode"] == 400
