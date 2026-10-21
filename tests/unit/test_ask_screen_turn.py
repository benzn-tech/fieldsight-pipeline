"""Unit: screen Ask on the async turn store (spec 2026-10-15 ask orchestration v2, phase 1).

`start_screen` mints a screen turn inside AskAgent; the AskVoiceWorker's
`channel == "screen"` branch answers it without TTS, audit or memory.
"""
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy-key")

laa = pytest.importorskip("lambda_ask_agent", reason="requires boto3/urllib3 (installed in CI)")

import io  # noqa: E402

from botocore.exceptions import ClientError  # noqa: E402

import ask_voice_turn as avt  # noqa: E402

SUB = "sub-aaaa-1111"
OTHER = "sub-bbbb-2222"
BUCKET = "test-bucket"


class FakeS3:
    """Same shape as the fake in test_ask_voice_turn.py (copied, not imported:
    importing a test module couples collection order)."""

    def __init__(self):
        self.objs = {}
        self.puts = []
        self.deletes = []

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.puts.append((Key, ContentType))
        self.objs[Key] = (Body, datetime.now(timezone.utc), ContentType)

    def get_object(self, Bucket, Key):
        if Key not in self.objs:
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "x"}}, "GetObject")
        return {"Body": io.BytesIO(self.objs[Key][0])}

    def delete_object(self, Bucket, Key):
        self.deletes.append(Key)
        self.objs.pop(Key, None)

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):
        return {"Contents": [{"Key": k, "LastModified": v[1]}
                             for k, v in sorted(self.objs.items()) if k.startswith(Prefix)],
                "IsTruncated": False}

    def state(self, sub, turn):
        k = avt.state_key(sub, turn)
        return json.loads(self.objs[k][0]) if k in self.objs else None


class FakeLambda:
    def __init__(self, events, fail=False):
        self.events, self.fail, self.calls = events, fail, []

    def invoke(self, FunctionName, InvocationType, Payload):
        self.events.append("invoke")
        if self.fail:
            raise RuntimeError("boom")
        self.calls.append({"fn": FunctionName, "type": InvocationType,
                           "payload": json.loads(Payload)})
        return {"StatusCode": 202}

QUESTION = "zebra giraffe what is the slab thickness"
ANSWER = "The slab is two hundred millimetres thick [1]."
TURN = "f" * 32


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("RAG_SEARCH_FUNCTION", "rag-fn")
    monkeypatch.setenv("ASK_TURN_BUCKET", BUCKET)
    monkeypatch.setenv("ASK_VOICE_WORKER_FUNCTION", "worker-fn")
    s3, events = FakeS3(), []
    lam = FakeLambda(events)
    monkeypatch.setattr(avt, "_get_s3", lambda: s3)
    monkeypatch.setattr(avt, "_get_lambda", lambda: lam)
    return type("E", (), {"s3": s3, "lam": lam, "events": events, "mp": monkeypatch})


def screen_body(**over):
    b = {"mode": "turn_start", "question": QUESTION, "caller_sub": SUB,
         "tz": "Pacific/Auckland", "history": [{"question": "q0", "answer": "a0"}],
         "site_id": 7}
    b.update(over)
    return b


def _states(s3):
    return {k: json.loads(v[0]) for k, v in s3.objs.items() if k.endswith(".json")}


# ---------------------------------------------------------------- start ----

def test_start_screen_requires_sign_in_and_writes_nothing(env):
    for sub in (None, ""):
        out = avt.start_screen(screen_body(caller_sub=sub))
        assert out == {"error": "sign-in required"}
    body = screen_body()
    del body["caller_sub"]
    assert avt.start_screen(body) == {"error": "sign-in required"}
    assert env.s3.puts == [] and env.lam.calls == [] and env.events == []


@pytest.mark.parametrize("q", [None, "", "   "])
def test_start_screen_empty_question(env, q):
    assert avt.start_screen(screen_body(question=q)) == {"error": "Missing question"}
    assert env.s3.puts == [] and env.lam.calls == []


def test_start_screen_happy_path(env):
    body = screen_body()
    out = avt.start_screen(dict(body))
    assert set(out) == {"turn_id"} and re.fullmatch(r"[0-9a-f]{32}", out["turn_id"])
    turn = out["turn_id"]

    # Exactly one state write: pending, screen, with the request minus identity and mode.
    assert len(env.s3.puts) == 1
    assert env.s3.puts[0][0] == avt.state_key(SUB, turn)
    st = env.s3.state(SUB, turn)
    assert set(st) == {"status", "channel", "created_at", "request"}
    assert st["status"] == "pending" and st["channel"] == "screen"
    assert isinstance(st["created_at"], float) and abs(st["created_at"] - time.time()) < 5
    expected = {k: v for k, v in body.items() if k not in ("caller_sub", "mode")}
    assert st["request"] == expected
    assert "caller_sub" not in st["request"] and SUB not in json.dumps(st)

    # Exactly one Event invoke of the worker, carrying only the turn, identity and channel.
    assert env.lam.calls == [{"fn": "worker-fn", "type": "Event",
                              "payload": {"turn_id": turn, "caller_sub": SUB,
                                          "channel": "screen"}}]


def test_start_screen_turn_ids_are_unique(env):
    a = avt.start_screen(screen_body())["turn_id"]
    b = avt.start_screen(screen_body())["turn_id"]
    assert a != b


def test_start_screen_invoke_failure_writes_error_state(env):
    env.lam.fail = True
    out = avt.start_screen(screen_body())
    assert out == {"error": "Ask temporarily unavailable"}
    states = list(_states(env.s3).values())
    assert len(states) == 1
    assert states[0]["status"] == "error" and states[0]["error"] == "No answer"
    assert states[0]["channel"] == "screen"


def test_start_screen_missing_bucket(env):
    env.mp.delenv("ASK_TURN_BUCKET")
    # The once-only log latch is module state; restore it so the voice suite's
    # own missing-bucket test still sees its log line.
    env.mp.setattr(avt, "_bucket_logged", False)
    assert avt.start_screen(screen_body()) == {"error": "Ask temporarily unavailable"}
    assert env.s3.puts == [] and env.lam.calls == []


def test_start_screen_sweeps_once(env):
    calls = []
    orig = avt.sweep_old

    def spy(*a, **k):
        calls.append(a[2])
        return orig(*a, **k)
    env.mp.setattr(avt, "sweep_old", spy)
    old = avt.state_key(SUB, "9" * 32)
    env.s3.objs[old] = (b"{}", datetime.now(timezone.utc) - timedelta(days=3), None)
    avt.start_screen(screen_body())
    assert calls == [SUB]
    assert old not in env.s3.objs


def test_start_screen_survives_sweep_failure(env):
    def boom(*a, **k):
        raise RuntimeError("list denied")
    env.mp.setattr(avt, "sweep_old", boom)
    assert avt.start_screen(screen_body())["turn_id"]


def test_lambda_handler_routes_turn_start(env):
    seen = []
    env.mp.setattr(avt, "start_screen", lambda body: seen.append(body) or {"turn_id": "x"})
    resp = laa.lambda_handler({"body": json.dumps(screen_body())}, None)
    assert resp["statusCode"] == 200 and json.loads(resp["body"]) == {"turn_id": "x"}
    assert seen and seen[0]["mode"] == "turn_start"


def test_start_screen_logs_no_text(env, caplog):
    caplog.set_level(logging.DEBUG)
    avt.start_screen(screen_body())
    env.lam.fail = True
    avt.start_screen(screen_body())
    text = caplog.text
    assert QUESTION not in text and SUB not in text and "q0" not in text


# --------------------------------------------------------------- worker ----

RAG_RESULT = {"answer": ANSWER, "sources": [{"n": 1, "site": "A"}], "basis": {"k": 5},
              "from_web": False}


@pytest.fixture
def wk(env):
    env.rag_calls, env.tts_calls, env.audits, env.remembered = [], [], [], []
    env.rag_result = dict(RAG_RESULT)

    def rag(body, on_web_start=None):
        env.rag_calls.append(body)
        if env.__dict__.get("web_cb") and on_web_start:
            on_web_start()
        return env.rag_result

    env.mp.setattr(laa, "_rag_answer", rag)
    env.mp.setattr(laa, "_tts", lambda text: env.tts_calls.append(text) or b"RIFF")
    env.mp.setattr(laa, "_invoke_voice_audit", lambda *a: env.audits.append(a))
    env.mp.setattr(avt, "_remember", lambda *a: env.remembered.append(a))
    env.request = {"question": QUESTION, "tz": "Pacific/Auckland",
                   "history": [{"question": "q0", "answer": "a0"}], "site_id": 7}
    avt.put_state(env.s3, BUCKET, SUB, TURN, {"status": "pending", "channel": "screen",
                                             "created_at": 123.5, "request": env.request})
    env.s3.puts.clear()
    return env


def sevent(**over):
    e = {"turn_id": TURN, "caller_sub": SUB, "channel": "screen"}
    e.update(over)
    return e


def test_worker_screen_happy_path(wk):
    avt.worker_handler(sevent(), None)
    assert wk.rag_calls == [dict(wk.request, caller_sub=SUB)]
    body = wk.rag_calls[0]
    for k in ("mode", "voice_web", "web_intent", "offer_web"):
        assert k not in body
    st = wk.s3.state(SUB, TURN)
    assert st == {"status": "done", "channel": "screen", "result": RAG_RESULT,
                  "created_at": 123.5}
    assert st["result"]["answer"] == ANSWER          # citation markers kept on screen
    assert wk.tts_calls == [] and wk.audits == [] and wk.remembered == []


def test_worker_screen_never_tts_audit_or_memory_with_convo_on(wk):
    wk.mp.setenv("ASK_VOICE_CONVO", "true")
    wk.mp.setenv("VOICE_WEB_ENABLED", "true")
    wk.rag_result = dict(RAG_RESULT, web_offer=True, asked="standalone")
    avt.worker_handler(sevent(), None)
    assert wk.s3.state(SUB, TURN)["status"] == "done"
    assert wk.tts_calls == [] and wk.audits == [] and wk.remembered == []
    keys = set(wk.s3.objs)
    assert avt.convo_key(SUB) not in keys and avt.offer_key(SUB) not in keys
    assert [p[0] for p in wk.s3.puts] == [avt.state_key(SUB, TURN)]


def test_worker_screen_ignores_caller_sub_and_voice_keys_in_request(wk):
    req = dict(wk.request, caller_sub=OTHER, mode="voice", voice_web=True,
               web_intent=True, offer_web=True)
    avt.put_state(wk.s3, BUCKET, SUB, TURN, {"status": "pending", "channel": "screen",
                                             "created_at": 1.0, "request": req})
    avt.worker_handler(sevent(), None)
    body = wk.rag_calls[0]
    assert body["caller_sub"] == SUB
    for k in ("mode", "voice_web"):
        assert k not in body
    # The screen web opt-in passes through (spec 2026-10-16 ask-screen-web-offer §2).
    assert body["web_intent"] is True and body["offer_web"] is True


def test_worker_screen_on_web_start_puts_searching_web(wk):
    wk.web_cb = True
    seen = []
    orig = avt.put_state

    def spy(s3, bucket, sub, turn, state):
        seen.append(dict(state))
        return orig(s3, bucket, sub, turn, state)
    wk.mp.setattr(avt, "put_state", spy)
    avt.worker_handler(sevent(), None)
    assert [s["status"] for s in seen] == ["searching_web", "done"]
    assert seen[0] == {"status": "searching_web", "channel": "screen", "created_at": 123.5}


def test_worker_screen_cancel_before_rag(wk):
    avt.put_cancel(wk.s3, BUCKET, SUB, TURN)
    avt.worker_handler(sevent(), None)
    assert wk.rag_calls == []
    assert wk.s3.state(SUB, TURN)["status"] == "pending"


def test_worker_screen_cancel_during_rag_writes_no_done(wk):
    def rag(body, on_web_start=None):
        avt.put_cancel(wk.s3, BUCKET, SUB, TURN)
        return dict(RAG_RESULT)
    wk.mp.setattr(laa, "_rag_answer", rag)
    avt.worker_handler(sevent(), None)
    assert wk.s3.state(SUB, TURN)["status"] == "pending"


def test_worker_screen_cancel_suppresses_searching_web(wk):
    def rag(body, on_web_start=None):
        avt.put_cancel(wk.s3, BUCKET, SUB, TURN)
        on_web_start()
        return dict(RAG_RESULT)
    wk.mp.setattr(laa, "_rag_answer", rag)
    avt.worker_handler(sevent(), None)
    assert wk.s3.state(SUB, TURN)["status"] == "pending"


def test_worker_screen_rag_raising_writes_error(wk):
    def rag(body, on_web_start=None):
        raise RuntimeError("model down")
    wk.mp.setattr(laa, "_rag_answer", rag)
    avt.worker_handler(sevent(), None)
    st = wk.s3.state(SUB, TURN)
    assert st["status"] == "error" and st["error"] == "No answer"
    assert st["channel"] == "screen"


def test_worker_screen_rag_error_dict_is_done_with_the_error(wk):
    wk.rag_result = {"error": "x"}
    avt.worker_handler(sevent(), None)
    assert wk.s3.state(SUB, TURN) == {"status": "done", "channel": "screen",
                                      "result": {"error": "x"}, "created_at": 123.5}


def test_worker_screen_missing_request_writes_error(wk):
    avt.put_state(wk.s3, BUCKET, SUB, TURN, {"status": "pending", "channel": "screen",
                                             "created_at": 1.0})
    avt.worker_handler(sevent(), None)
    assert wk.rag_calls == []
    st = wk.s3.state(SUB, TURN)
    assert st["status"] == "error" and st["channel"] == "screen"


def test_worker_screen_non_pending_does_nothing(wk):
    avt.put_state(wk.s3, BUCKET, SUB, TURN, {"status": "done", "channel": "screen",
                                             "result": {}, "created_at": 1.0})
    wk.s3.puts.clear()
    avt.worker_handler(sevent(), None)
    assert wk.rag_calls == [] and wk.s3.puts == []


def test_worker_screen_non_json_values_are_stringified(wk):
    when = datetime(2026, 10, 16, tzinfo=timezone.utc)
    wk.rag_result = dict(RAG_RESULT, when=when)
    avt.worker_handler(sevent(), None)
    st = wk.s3.state(SUB, TURN)
    assert st["status"] == "done" and st["result"]["when"] == str(when)


def test_worker_screen_log_line_has_no_text(wk, caplog):
    caplog.set_level(logging.INFO)
    avt.worker_handler(sevent(), None)
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("screen ask:")]
    assert len(lines) == 1
    assert re.fullmatch(r"screen ask: rag=\d+\.\d\ds total=\d+\.\d\ds turn=f{32} "
                        r"web=False sub_hash=[0-9a-f]{12}", lines[0])
    assert QUESTION not in caplog.text and SUB not in caplog.text and "slab" not in caplog.text


# ------------------------------------------------- Ask threads (spec 2026-10-22) ----

def test_start_screen_keeps_thread_fields_in_the_stored_request(env):
    out = avt.start_screen(screen_body(thread_id="a" * 32, new_thread=True))
    st = env.s3.state(SUB, out["turn_id"])
    assert st["request"]["thread_id"] == "a" * 32 and st["request"]["new_thread"] is True


def test_screen_rag_body_excludes_thread_fields(wk):
    req = dict(wk.request, thread_id="a" * 32, new_thread=True)
    avt.put_state(wk.s3, BUCKET, SUB, TURN, {"status": "pending", "channel": "screen",
                                             "created_at": 1.0, "request": req})
    avt.worker_handler(sevent(), None)
    body = wk.rag_calls[0]
    assert "thread_id" not in body and "new_thread" not in body
    assert body == dict(wk.request, caller_sub=SUB)
