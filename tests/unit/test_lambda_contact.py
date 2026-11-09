"""Unit: ContactFunction handler (spec 2026-11-08-contact-fieldsight-design).

Fake S3 (a dict) and fake SES; the speech-to-text call is a module-level factory
the tests replace. Identity comes only from the authorizer claims."""
import base64
import email
import email.policy
import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from botocore.exceptions import ClientError

import lambda_contact as lc

NOW = datetime(2026, 11, 8, 3, 4, 5, tzinfo=timezone.utc)
SUB = "sub-abc"
CLAIM_EMAIL = "ann@acme.co"
SECRET_TEXT = "my secret problem text"
SECRET_TRANSCRIPT = "a secret machine transcript"


class FakeS3:
    def __init__(self):
        self.store = {}
        self.puts = []

    def get_object(self, Bucket, Key):
        if Key not in self.store:
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "x"}}, "GetObject")
        data = self.store[Key]

        class _B:
            def read(self_inner):
                return data
        return {"Body": _B()}

    def put_object(self, Bucket, Key, Body, **kw):
        self.store[Key] = Body if isinstance(Body, bytes) else Body.encode()
        self.puts.append(Key)
        return {}

    def list_objects_v2(self, Bucket, Prefix, **kw):
        keys = sorted(k for k in self.store if k.startswith(Prefix))
        return {"Contents": [{"Key": k} for k in keys], "IsTruncated": False}


class FakeSES:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    def send_raw_email(self, **kw):
        if self.fail:
            raise ClientError({"Error": {"Code": "MessageRejected", "Message": "no"}},
                              "SendRawEmail")
        self.sent.append(kw)
        return {"MessageId": "m-1"}


@pytest.fixture
def env(monkeypatch):
    s3, ses = FakeS3(), FakeSES()
    state = {"now": NOW, "stt_calls": 0, "stt": lambda audio, fn: "words from the clip"}

    def stt(audio, fn):
        state["stt_calls"] += 1
        return state["stt"](audio, fn)

    monkeypatch.setenv("CONTACT_FIELDSIGHT", "true")
    monkeypatch.setenv("STAGE", "test")
    monkeypatch.setenv("S3_BUCKET", "bucket")
    monkeypatch.setenv("SENDER_EMAIL", "sales@fieldsightai.com")
    monkeypatch.setattr(lc, "_s3", lambda: s3)
    monkeypatch.setattr(lc, "_ses", lambda: ses)
    monkeypatch.setattr(lc, "_stt", stt)
    monkeypatch.setattr(lc, "_now", lambda: state["now"])
    state.update(s3=s3, ses=ses)
    return state


def _event(path, body=None, method="POST", claims=None):
    if claims is None:
        claims = {"sub": SUB, "email": CLAIM_EMAIL, "name": "Ann Claim"}
    return {
        "httpMethod": method,
        "path": path,
        "pathParameters": {"proxy": path.split("/api/contact/", 1)[-1]},
        "requestContext": {"authorizer": {"claims": claims}},
        "body": json.dumps(body) if body is not None else None,
    }


def _msg(**kw):
    b = {"id": str(uuid.uuid4()), "text": SECRET_TEXT, "audio": None, "mime": None,
         "transcript": None,
         "context": {"name": "Ann", "company": "Acme", "project": "Site 1",
                     "page": "/timeline?q=x"}}
    b.update(kw)
    return b


def _audio_body():
    return {"audio": base64.b64encode(b"\x1a\x45\xdf\xa3 fake webm").decode(),
            "mime": "audio/webm;codecs=opus"}


def _json(resp):
    return json.loads(resp["body"])


def _records(s3):
    return {k: json.loads(v) for k, v in s3.store.items()
            if k.endswith(".json") and "/_transcribe/" not in k}


# --- flag, CORS, identity ---------------------------------------------------

def test_flag_off_404(env, monkeypatch):
    monkeypatch.setenv("CONTACT_FIELDSIGHT", "false")
    for path in ("/api/contact/messages", "/api/contact/transcribe", "/api/contact/x"):
        r = lc.lambda_handler(_event(path, _msg()), None)
        assert r["statusCode"] == 404 and _json(r) == {"error": "not_found"}
    monkeypatch.delenv("CONTACT_FIELDSIGHT")
    assert lc.lambda_handler(_event("/api/contact/messages", _msg()), None)["statusCode"] == 404
    assert env["ses"].sent == [] and env["s3"].store == {}


def test_options_ok_when_off(env, monkeypatch):
    monkeypatch.setenv("CONTACT_FIELDSIGHT", "false")
    r = lc.lambda_handler(_event("/api/contact/messages", method="OPTIONS", claims={}), None)
    assert r["statusCode"] == 200
    assert r["headers"]["Access-Control-Allow-Origin"] == "*"


def test_missing_claims_401(env):
    r = lc.lambda_handler(_event("/api/contact/messages", _msg(), claims={"sub": SUB}), None)
    assert r["statusCode"] == 401
    r = lc.lambda_handler(_event("/api/contact/messages", _msg(),
                                 claims={"email": CLAIM_EMAIL}), None)
    assert r["statusCode"] == 401


def test_identity_from_claims_not_body(env):
    body = _msg(email="attacker@evil.com", sub="other-sub")
    r = lc.lambda_handler(_event("/api/contact/messages", body), None)
    assert r["statusCode"] == 201
    raw = env["ses"].sent[0]["RawMessage"]["Data"]
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    assert msg["Reply-To"] == CLAIM_EMAIL
    assert "attacker@evil.com" not in raw.decode("utf-8", "replace")
    (key, rec), = _records(env["s3"]).items()
    assert key.startswith(f"contact_messages/{SUB}/")
    assert rec["sub"] == SUB and rec["email"] == CLAIM_EMAIL
    # the page query string never reaches the record or the email
    assert rec["context"]["page"] == "/timeline"


# --- transcribe -------------------------------------------------------------

def test_transcribe_ok(env):
    r = lc.lambda_handler(_event("/api/contact/transcribe", _audio_body()), None)
    assert r["statusCode"] == 200 and _json(r) == {"text": "words from the clip"}
    # stores nothing but the counter
    assert [k for k in env["s3"].store if "/_transcribe/" not in k] == []
    assert f"contact_messages/{SUB}/_transcribe/2026-11-08T03.json" in env["s3"].store


def test_transcribe_bad_audio_400(env):
    r = lc.lambda_handler(_event("/api/contact/transcribe", {"audio": "", "mime": "audio/webm"}),
                          None)
    assert r["statusCode"] == 400
    r = lc.lambda_handler(_event("/api/contact/transcribe",
                                 {"audio": "AAAA", "mime": "audio/ogg"}), None)
    assert r["statusCode"] == 400
    assert env["stt_calls"] == 0


def test_transcribe_provider_error_is_200_unavailable(env, caplog):
    def boom(audio, fn):
        raise RuntimeError("ElevenLabs STT: HTTP 401")
    env["stt"] = boom
    with caplog.at_level(logging.WARNING, logger=lc.logger.name):
        r = lc.lambda_handler(_event("/api/contact/transcribe", _audio_body()), None)
    assert r["statusCode"] == 200 and _json(r) == {"text": "", "error": "unavailable"}
    assert any(SUB in rec.getMessage() for rec in caplog.records
               if rec.levelno == logging.WARNING)


def test_transcribe_timeout_is_200_unavailable(env, monkeypatch):
    monkeypatch.setattr(lc, "TRANSCRIBE_DEADLINE_S", 0.1)

    def slow(audio, fn):
        time.sleep(1)
        return "too late"
    env["stt"] = slow
    t0 = time.monotonic()
    r = lc.lambda_handler(_event("/api/contact/transcribe", _audio_body()), None)
    assert time.monotonic() - t0 < 0.8
    assert r["statusCode"] == 200 and _json(r) == {"text": "", "error": "unavailable"}


def test_transcribe_rate_limit_21st_is_429(env):
    for _ in range(20):
        r = lc.lambda_handler(_event("/api/contact/transcribe", _audio_body()), None)
        assert r["statusCode"] == 200
    r = lc.lambda_handler(_event("/api/contact/transcribe", _audio_body()), None)
    assert r["statusCode"] == 429
    body = _json(r)
    assert body["error"] == "rate_limited"
    assert isinstance(body["retry_after_s"], int) and body["retry_after_s"] >= 60
    assert env["stt_calls"] == 20
    # the next hour is a fresh counter
    env["now"] = NOW + timedelta(hours=1)
    assert lc.lambda_handler(_event("/api/contact/transcribe", _audio_body()),
                             None)["statusCode"] == 200


def test_transcribe_counter_other_s3_error_propagates(env, monkeypatch):
    def denied(Bucket, Key):
        raise ClientError({"Error": {"Code": "AccessDenied", "Message": "x"}}, "GetObject")
    monkeypatch.setattr(env["s3"], "get_object", denied)
    r = lc.lambda_handler(_event("/api/contact/transcribe", _audio_body()), None)
    assert r["statusCode"] == 500
    assert env["stt_calls"] == 0


# --- messages ---------------------------------------------------------------

def test_message_writes_record_then_email(env, monkeypatch):
    order = []
    s3, ses = env["s3"], env["ses"]
    real_put, real_send = s3.put_object, ses.send_raw_email

    def put(**kw):
        order.append(("put", json.loads(kw["Body"]).get("email_status")
                      if kw["Key"].endswith(".json") else "audio"))
        return real_put(**kw)

    def send(**kw):
        order.append(("send", None))
        return real_send(**kw)
    monkeypatch.setattr(s3, "put_object", put)
    monkeypatch.setattr(ses, "send_raw_email", send)

    body = _msg(transcript="the raw words", **_audio_body())
    r = lc.lambda_handler(_event("/api/contact/messages", body), None)
    assert r["statusCode"] == 201 and _json(r) == {"id": body["id"]}
    assert order == [("put", "audio"), ("put", "pending"), ("send", None), ("put", "sent")]
    (key, rec), = _records(s3).items()
    assert rec["email_status"] == "sent"
    assert rec["text"] == SECRET_TEXT and rec["transcript"] == "the raw words"
    assert rec["env"] == "test" and rec["mime"] == "audio/webm;codecs=opus"
    assert rec["audio_key"] in s3.store and rec["audio_key"].endswith(".webm")
    sent = ses.sent[0]
    assert sent["Destinations"] == ["sales@fieldsightai.com"]
    assert sent["Source"] == "sales@fieldsightai.com"
    msg = email.message_from_bytes(sent["RawMessage"]["Data"], policy=email.policy.default)
    assert msg["Subject"].startswith("[TEST] FieldSight message from ")
    assert [a.get_filename() for a in msg.iter_attachments()] == ["message.webm"]


def test_prod_stage_has_no_test_prefix(env, monkeypatch):
    monkeypatch.setenv("STAGE", "prod")
    lc.lambda_handler(_event("/api/contact/messages", _msg()), None)
    msg = email.message_from_bytes(env["ses"].sent[0]["RawMessage"]["Data"],
                                   policy=email.policy.default)
    assert not msg["Subject"].startswith("[TEST]")


def test_same_id_twice_one_email(env):
    body = _msg()
    r1 = lc.lambda_handler(_event("/api/contact/messages", body), None)
    env["now"] = NOW + timedelta(seconds=7)
    r2 = lc.lambda_handler(_event("/api/contact/messages", body), None)
    assert r1["statusCode"] == r2["statusCode"] == 201
    assert _json(r1) == _json(r2) == {"id": body["id"]}
    assert len(env["ses"].sent) == 1
    assert len(_records(env["s3"])) == 1


def test_ses_failure_still_201_and_marked(env, caplog):
    env["ses"].fail = True
    body = _msg(transcript=SECRET_TRANSCRIPT)
    with caplog.at_level(logging.DEBUG):
        r = lc.lambda_handler(_event("/api/contact/messages", body), None)
    assert r["statusCode"] == 201 and _json(r) == {"id": body["id"]}
    (key, rec), = _records(env["s3"]).items()
    assert rec["email_status"] == "email_failed"
    errors = [x for x in caplog.records if x.levelno >= logging.ERROR]
    assert errors, "no ERROR log line for the failed email"
    for x in errors:
        line = x.getMessage() + (x.exc_text or "")
        assert body["id"] in x.getMessage() and SUB in x.getMessage()
    every = " ".join(x.getMessage() + str(x.exc_text or "") for x in caplog.records)
    assert SECRET_TEXT not in every and SECRET_TRANSCRIPT not in every


def test_message_rate_limit_11th_is_429(env):
    for i in range(10):
        env["now"] = NOW + timedelta(minutes=i)
        assert lc.lambda_handler(_event("/api/contact/messages", _msg()),
                                 None)["statusCode"] == 201
    env["now"] = NOW + timedelta(minutes=10)
    r = lc.lambda_handler(_event("/api/contact/messages", _msg()), None)
    assert r["statusCode"] == 429
    body = _json(r)
    assert body["error"] == "rate_limited"
    # the oldest (at NOW) ages out at NOW+60min, i.e. 50 minutes from now
    assert body["retry_after_s"] == 50 * 60
    assert len(env["ses"].sent) == 10
    # once the oldest has aged out, one more is allowed
    env["now"] = NOW + timedelta(minutes=60, seconds=1)
    assert lc.lambda_handler(_event("/api/contact/messages", _msg()),
                             None)["statusCode"] == 201


def test_record_and_audio_keys_under_sub_prefix(env):
    body = _msg(**{"audio": base64.b64encode(b"m4a bytes").decode(), "mime": "audio/mp4"})
    lc.lambda_handler(_event("/api/contact/messages", body), None)
    stem = f"contact_messages/{SUB}/2026-11-08T030405Z_{body['id']}"
    assert set(env["s3"].store) == {stem + ".json", stem + ".m4a"}
    rec = json.loads(env["s3"].store[stem + ".json"])
    assert rec["audio_key"] == stem + ".m4a" and rec["record_key"] == stem + ".json"


def test_responses_are_json_with_cors(env, monkeypatch):
    cases = [
        lc.lambda_handler(_event("/api/contact/messages", {"id": "nope"}), None),   # 400
        lc.lambda_handler(_event("/api/contact/unknown", {}), None),                # 404
    ]
    for i in range(10):
        lc.lambda_handler(_event("/api/contact/messages", _msg()), None)
    cases.append(lc.lambda_handler(_event("/api/contact/messages", _msg()), None))  # 429
    r_bad_json = lc.lambda_handler({**_event("/api/contact/messages"), "body": "{not json"},
                                   None)
    cases.append(r_bad_json)                                                       # 400
    assert [c["statusCode"] for c in cases] == [400, 404, 429, 400]
    for c in cases:
        assert c["headers"]["Content-Type"] == "application/json"
        assert c["headers"]["Access-Control-Allow-Origin"] == "*"
        assert isinstance(json.loads(c["body"]), dict)
