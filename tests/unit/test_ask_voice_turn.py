"""Unit: device Ask two-phase turn store, start() and the worker (spec 2026-10-15)."""
import base64
import io
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
from botocore.exceptions import ClientError  # noqa: E402

import ask_voice_turn as avt  # noqa: E402
import voice_restate  # noqa: E402

SUB = "sub-aaaa-1111"
OTHER = "sub-bbbb-2222"
TRANSCRIPT = "zebra giraffe what is the slab thickness"
ANSWER = "The slab is two hundred millimetres thick [1]."
SPOKEN = "The slab is two hundred millimetres thick."
BUCKET = "test-bucket"
CLIP_B64 = base64.b64encode(b"\x00\x01rawaudio").decode("ascii")


class FakeS3:
    def __init__(self):
        self.objs = {}      # key -> (bytes, LastModified, ContentType)
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


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("RAG_SEARCH_FUNCTION", "rag-fn")
    monkeypatch.setenv("ASK_TURN_BUCKET", BUCKET)
    monkeypatch.setenv("ASK_VOICE_WORKER_FUNCTION", "worker-fn")
    s3, events = FakeS3(), []
    lam = FakeLambda(events)
    monkeypatch.setattr(avt, "_get_s3", lambda: s3)
    monkeypatch.setattr(avt, "_get_lambda", lambda: lam)
    monkeypatch.setattr(laa, "_stt", lambda audio, fmt: TRANSCRIPT)
    monkeypatch.setattr(laa, "_tts", lambda text: b"RIFFwav")

    def fake_restate(q, *, web_intent, timeout_s=1.2):
        events.append("restate")
        return {"text": "Got it, you're asking about slab thickness. "
                        "Checking your site records now.", "reason": None, "ms": 5}
    monkeypatch.setattr(voice_restate, "restate", fake_restate)
    return type("E", (), {"s3": s3, "lam": lam, "events": events, "mp": monkeypatch})


def start_body(**over):
    b = {"mode": "voice_start", "audio": CLIP_B64, "format": "m4a", "caller_sub": SUB}
    b.update(over)
    return b


# ---------------------------------------------------------------- store ----

def test_keys_and_prefix():
    assert avt.TURN_RE.match("a" * 32) and not avt.TURN_RE.match("A" * 32)
    assert not avt.TURN_RE.match("a" * 31)
    p = avt.owner_prefix(SUB)
    assert re.fullmatch(r"ask_turns/[0-9a-f]{16}/", p)
    assert SUB not in p
    assert avt.state_key(SUB, "t") == p + "t.json"
    assert avt.cancel_key(SUB, "t") == p + "t.cancel"


def test_store_roundtrip(env):
    s3, t = env.s3, "c" * 32
    assert avt.get_state(s3, BUCKET, SUB, t) is None
    avt.put_state(s3, BUCKET, SUB, t, {"status": "pending"})
    assert s3.puts[-1][1] == "application/json"
    assert avt.get_state(s3, BUCKET, SUB, t) == {"status": "pending"}
    assert avt.get_state(s3, BUCKET, OTHER, t) is None
    assert not avt.is_cancelled(s3, BUCKET, SUB, t)
    avt.put_cancel(s3, BUCKET, SUB, t)
    assert avt.is_cancelled(s3, BUCKET, SUB, t)
    avt.delete_turn(s3, BUCKET, SUB, t)
    assert avt.get_state(s3, BUCKET, SUB, t) is None
    assert not avt.is_cancelled(s3, BUCKET, SUB, t)


@pytest.mark.parametrize("code", ["NoSuchKey", "404", "NotFound"])
def test_missing_key_codes_are_absent(env, code):
    class Missing(FakeS3):
        def get_object(self, Bucket, Key):
            raise ClientError({"Error": {"Code": code, "Message": "x"}}, "GetObject")
    assert avt.get_state(Missing(), BUCKET, SUB, "d" * 32) is None
    assert avt.is_cancelled(Missing(), BUCKET, SUB, "d" * 32) is False


@pytest.mark.parametrize("code", ["AccessDenied", "403"])
def test_access_denied_raises(env, code):
    class Denied(FakeS3):
        def get_object(self, Bucket, Key):
            raise ClientError({"Error": {"Code": code, "Message": "x"}}, "GetObject")
    with pytest.raises(ClientError):
        avt.get_state(Denied(), BUCKET, SUB, "d" * 32)
    with pytest.raises(ClientError):
        avt.is_cancelled(Denied(), BUCKET, SUB, "d" * 32)


def test_other_s3_errors_propagate(env):
    class Broken(FakeS3):
        def get_object(self, Bucket, Key):
            raise ClientError({"Error": {"Code": "InternalError", "Message": "x"}}, "GetObject")
    with pytest.raises(ClientError):
        avt.get_state(Broken(), BUCKET, SUB, "d" * 32)


def test_sweep_deletes_only_this_callers_old_objects(env):
    s3 = env.s3
    now = datetime.now(timezone.utc)
    old_mine, new_mine = avt.state_key(SUB, "1" * 32), avt.state_key(SUB, "2" * 32)
    old_cancel = avt.cancel_key(SUB, "1" * 32)
    old_other = avt.state_key(OTHER, "3" * 32)
    for k in (old_mine, new_mine, old_cancel, old_other):
        s3.put_object(BUCKET, k, b"{}")
    for k in (old_mine, old_cancel, old_other):
        s3.objs[k] = (b"{}", now - timedelta(days=2), None)
    assert avt.sweep_old(s3, BUCKET, SUB, now) == 2
    assert old_mine not in s3.objs and old_cancel not in s3.objs
    assert new_mine in s3.objs and old_other in s3.objs


def test_sweep_is_capped(env):
    s3 = env.s3
    now = datetime.now(timezone.utc)
    for i in range(avt.SWEEP_MAX_DELETES + 7):
        k = avt.state_key(SUB, "%032x" % i)
        s3.objs[k] = (b"{}", now - timedelta(days=2), None)
    assert avt.sweep_old(s3, BUCKET, SUB, now) == avt.SWEEP_MAX_DELETES
    assert len(s3.objs) == 7


def test_delete_turn_can_keep_the_marker(env):
    s3 = env.s3
    for k in (avt.state_key(SUB, "1" * 32), avt.cancel_key(SUB, "1" * 32)):
        s3.put_object(BUCKET, k, b"{}")
    avt.delete_turn(s3, BUCKET, SUB, "1" * 32, keep_marker=True)
    assert list(s3.objs) == [avt.cancel_key(SUB, "1" * 32)]


def test_prefix_overlaps_no_notification_prefix():
    # Hard-coded from spec section 4 and the other S3-triggered prefixes in this repo.
    notification = ["users/", "audio_segments/", "reports/", "transcripts/",
                    "extraction_requests/", "match_requests/", "session_finalize_requests/",
                    "reindex_requests/", "meeting_minutes/", "extractions/", "web_video/"]
    for n in notification:
        assert not avt.PREFIX.startswith(n) and not n.startswith(avt.PREFIX)


# ---------------------------------------------------------------- start ----

def test_start_happy_path(env):
    out = avt.start(start_body(tz="Pacific/Auckland", history=[{"q": "x"}]))
    assert avt.TURN_RE.match(out["turn_id"])
    assert out["transcript"] == TRANSCRIPT and out["web_intent"] is False
    r = out["restatement"]
    assert r["audioFormat"] == "wav"
    assert base64.b64decode(r["audioBase64"]) == b"RIFFwav"
    assert r["text"].startswith("Got it,")
    assert env.s3.state(SUB, out["turn_id"])["status"] == "pending"
    call = env.lam.calls[0]
    assert call["fn"] == "worker-fn" and call["type"] == "Event"
    assert call["payload"] == {"turn_id": out["turn_id"], "caller_sub": SUB,
                               "transcript": TRANSCRIPT, "web_intent": False,
                               "tz": "Pacific/Auckland", "history": [{"q": "x"}]}


def test_start_restate_none(env):
    env.mp.setattr(voice_restate, "restate",
                   lambda q, *, web_intent, timeout_s=1.2: {"text": None, "reason": "off", "ms": 1})
    assert avt.start(start_body())["restatement"] is None


def test_start_slow_tts_gives_null_restatement_in_time(env):
    env.mp.setattr(laa, "_tts", lambda text: time.sleep(2.5) or b"RIFF")
    t = time.perf_counter()
    out = avt.start(start_body())
    assert time.perf_counter() - t < 3.5
    assert out["restatement"] is None and out["turn_id"]


def test_start_tts_error_gives_null_restatement(env):
    def bad(text):
        raise RuntimeError("tts down")
    env.mp.setattr(laa, "_tts", bad)
    assert avt.start(start_body())["restatement"] is None


def test_start_empty_stt_writes_nothing(env):
    env.mp.setattr(laa, "_stt", lambda a, f: "  ")
    out = avt.start(start_body())
    assert out == {"error": "Empty transcript", "transcript": ""}
    assert env.s3.objs == {} and env.lam.calls == []


def test_start_stt_failure_and_bad_audio(env):
    def bad(a, f):
        raise RuntimeError("stt down")
    env.mp.setattr(laa, "_stt", bad)
    assert avt.start(start_body()) == {"error": "Speech recognition failed"}
    assert avt.start(start_body(audio="!!notb64")) == {"error": "Invalid audio encoding"}
    assert env.s3.objs == {}


def test_start_requires_sign_in_and_rag_function(env):
    assert avt.start(start_body(caller_sub=None)) == {"error": "sign-in required"}
    env.mp.delenv("RAG_SEARCH_FUNCTION")
    # Same effective body as the old handler path, which falls through to its
    # empty-question check when RAG_SEARCH_FUNCTION is unset.
    assert avt.start(start_body()) == {"error": "Missing question"}
    assert env.s3.objs == {}


def test_start_invoke_failure_writes_error_state(env):
    env.lam.fail = True
    out = avt.start(start_body())
    assert out == {"error": "No answer", "transcript": TRANSCRIPT}
    key = [k for k in env.s3.objs if k.endswith(".json")][0]
    assert json.loads(env.s3.objs[key][0]) == {"status": "error", "error": "No answer"}


def test_start_invokes_worker_before_restate(env):
    avt.start(start_body())
    assert env.events == ["invoke", "restate"]


def test_start_web_intent(env):
    env.mp.setenv("VOICE_WEB_ENABLED", "true")
    env.mp.setenv("ENABLE_WEB_ANSWER", "true")
    env.mp.setattr(laa, "_stt", lambda a, f: "what is the NZS 4541 spacing, search online")
    out = avt.start(start_body())
    assert out["web_intent"] is True
    assert env.lam.calls[0]["payload"]["web_intent"] is True
    assert env.lam.calls[0]["payload"]["transcript"] == "what is the NZS 4541 spacing, search online"


def test_start_sweeps_callers_old_turns(env):
    old = avt.state_key(SUB, "9" * 32)
    env.s3.objs[old] = (b"{}", datetime.now(timezone.utc) - timedelta(days=3), None)
    avt.start(start_body())
    assert old not in env.s3.objs


def test_start_survives_sweep_failure(env):
    def boom(*a, **k):
        raise RuntimeError("list denied")
    env.mp.setattr(avt, "sweep_old", boom)
    assert avt.start(start_body())["turn_id"]


# --------------------------------------------------------------- worker ----

TURN = "e" * 32


@pytest.fixture
def wk(env):
    env.rag_calls, env.tts_calls, env.audits = [], [], []

    def rag(body, on_web_start=None):
        env.rag_calls.append(body)
        if env.__dict__.get("web_cb") and on_web_start:
            on_web_start()
        return {"answer": ANSWER, "basis": {"k": 5}, "from_web": env.__dict__.get("from_web", False)}

    def tts(text):
        env.tts_calls.append(text)
        return b"RIFFanswer"
    env.mp.setattr(laa, "_rag_answer", rag)
    env.mp.setattr(laa, "_tts", tts)
    env.mp.setattr(laa, "_invoke_voice_audit", lambda *a: env.audits.append(a))
    avt.put_state(env.s3, BUCKET, SUB, TURN, {"status": "pending", "created_at": time.time()})
    env.s3.puts.clear()
    return env


def event(**over):
    e = {"turn_id": TURN, "caller_sub": SUB, "transcript": TRANSCRIPT, "web_intent": False}
    e.update(over)
    return e


def test_worker_happy_path(wk):
    avt.worker_handler(event(tz="Pacific/Auckland", history=[]), None)
    st = wk.s3.state(SUB, TURN)
    assert st == {"status": "done", "transcript": TRANSCRIPT, "answerText": SPOKEN,
                  "basis": {"k": 5},
                  "audioBase64": base64.b64encode(b"RIFFanswer").decode("ascii"),
                  "audioFormat": "wav", "web": False}
    assert wk.tts_calls == [SPOKEN]                      # citation marker never spoken
    assert wk.audits == [(SUB, TRANSCRIPT, SPOKEN)]
    body = wk.rag_calls[0]
    assert body["question"] == TRANSCRIPT and body["mode"] == "voice" and body["k"] == 5
    assert body["caller_sub"] == SUB and body["tz"] == "Pacific/Auckland"
    assert body["web_intent"] is False and body["history"] == []


def test_worker_strips_multi_citation_markers(wk):
    wk.mp.setattr(laa, "_rag_answer", lambda b, on_web_start=None: {
        "answer": "Use grade 500 [1, 2] bars [3].", "basis": None})
    avt.worker_handler(event(), None)
    assert wk.tts_calls == ["Use grade 500 bars."]


def test_worker_web_flag_follows_the_route(wk):
    wk.from_web = True
    avt.worker_handler(event(), None)
    assert wk.s3.state(SUB, TURN)["web"] is True


def test_worker_web_intent_strips_phrase_from_question(wk):
    t = "what is the NZS 4541 spacing, search online"
    avt.worker_handler(event(transcript=t, web_intent=True), None)
    q = wk.rag_calls[0]["question"]
    assert "online" not in q and "4541" in q
    assert wk.rag_calls[0]["web_intent"] is True
    assert wk.s3.state(SUB, TURN)["transcript"] == t


def test_worker_voice_web_flag_from_env(wk):
    wk.mp.setenv("VOICE_WEB_ENABLED", "true")
    avt.worker_handler(event(), None)
    assert wk.rag_calls[0]["voice_web"] is True


def test_worker_step0_non_pending_does_nothing(wk):
    for st in ({"status": "done"}, {"status": "searching_web"}, {"status": "error", "error": "x"}):
        avt.put_state(wk.s3, BUCKET, SUB, TURN, st)
        wk.s3.puts.clear()
        avt.worker_handler(event(), None)
        assert wk.s3.puts == [] and wk.rag_calls == []
    wk.s3.objs.clear()                                   # unknown turn
    avt.worker_handler(event(), None)
    assert wk.s3.puts == [] and wk.rag_calls == []


def test_worker_cancelled_before_rag(wk):
    avt.put_cancel(wk.s3, BUCKET, SUB, TURN)
    avt.worker_handler(event(), None)
    assert wk.rag_calls == [] and wk.audits == []


def test_worker_on_web_start_puts_searching_web(wk):
    wk.web_cb = True
    seen, searching = [], []
    orig = avt.put_state

    def spy(s3, bucket, sub, turn, state):
        seen.append(state["status"])
        if state["status"] == "searching_web":
            searching.append(state)
        return orig(s3, bucket, sub, turn, state)
    wk.mp.setattr(avt, "put_state", spy)
    avt.put_state(wk.s3, BUCKET, SUB, TURN, {"status": "pending", "created_at": 123.5})
    avt.worker_handler(event(), None)
    assert seen[1:] == ["searching_web", "done"]
    assert searching[0]["created_at"] == 123.5


def test_worker_cancel_during_rag_skips_tts_done_and_audit(wk):
    def rag(body, on_web_start=None):
        avt.put_cancel(wk.s3, BUCKET, SUB, TURN)
        return {"answer": ANSWER, "basis": None}
    wk.mp.setattr(laa, "_rag_answer", rag)
    avt.worker_handler(event(), None)
    assert wk.tts_calls == [] and wk.audits == []
    assert wk.s3.state(SUB, TURN)["status"] == "pending"


def test_worker_cancel_during_tts_writes_no_done_no_audit(wk):
    def tts(text):
        avt.put_cancel(wk.s3, BUCKET, SUB, TURN)
        return b"RIFF"
    wk.mp.setattr(laa, "_tts", tts)
    avt.worker_handler(event(), None)
    assert wk.s3.state(SUB, TURN)["status"] == "pending" and wk.audits == []


def test_worker_cancel_after_done_skips_audit(wk):
    orig = avt.put_state

    def spy(s3, bucket, sub, turn, state):
        orig(s3, bucket, sub, turn, state)
        if state["status"] == "done":
            avt.put_cancel(s3, bucket, sub, turn)
    wk.mp.setattr(avt, "put_state", spy)
    avt.worker_handler(event(), None)
    assert wk.audits == []


def test_worker_exception_writes_error_state(wk):
    def rag(body, on_web_start=None):
        raise RuntimeError("model down")
    wk.mp.setattr(laa, "_rag_answer", rag)
    avt.worker_handler(event(), None)
    assert wk.s3.state(SUB, TURN) == {"status": "error", "error": "No answer"}
    assert wk.audits == []


def test_worker_rag_error_and_tts_error(wk):
    wk.mp.setattr(laa, "_rag_answer", lambda b, on_web_start=None: {"error": "rag failed"})
    avt.worker_handler(event(), None)
    assert wk.s3.state(SUB, TURN) == {"status": "error", "error": "rag failed"}
    avt.put_state(wk.s3, BUCKET, SUB, TURN, {"status": "pending"})
    wk.mp.setattr(laa, "_rag_answer", lambda b, on_web_start=None: {"answer": ""})
    avt.worker_handler(event(), None)
    assert wk.s3.state(SUB, TURN) == {"status": "error", "error": "No answer"}
    avt.put_state(wk.s3, BUCKET, SUB, TURN, {"status": "pending"})
    wk.mp.setattr(laa, "_rag_answer", lambda b, on_web_start=None: {"answer": "ok"})

    def bad(text):
        raise RuntimeError("tts")
    wk.mp.setattr(laa, "_tts", bad)
    avt.worker_handler(event(), None)
    assert wk.s3.state(SUB, TURN) == {"status": "error", "error": "Speech synthesis failed"}


def test_worker_step0_read_failure_writes_nothing(wk):
    def boom(*a):
        raise RuntimeError("s3 down")
    wk.mp.setattr(avt, "get_state", boom)
    avt.worker_handler(event(), None)
    assert wk.s3.puts == [] and wk.rag_calls == []


def test_restate_exception_gives_null_restatement(env):
    def boom(q, *, web_intent, timeout_s=1.2):
        raise RuntimeError("x")
    env.mp.setattr(voice_restate, "restate", boom)
    out = avt.start(start_body())
    assert out["restatement"] is None and out["turn_id"]


def test_missing_bucket_fails_fast(wk, caplog):
    wk.mp.delenv("ASK_TURN_BUCKET")
    wk.lam.calls.clear()
    assert avt.start(start_body()) == {"error": "No answer"}
    assert wk.lam.calls == []
    avt.worker_handler(event(), None)
    assert wk.rag_calls == []
    assert "ASK_TURN_BUCKET" in caplog.text


def test_worker_ignores_malformed_event(wk):
    avt.worker_handler({"turn_id": "../x", "caller_sub": SUB}, None)
    avt.worker_handler({"turn_id": TURN}, None)
    assert wk.rag_calls == [] and wk.s3.puts == []


# ----------------------------------------------------------------- logs ----

START_FIELDS = ["stt=", "restate=", "restate_tts=", "start_total=", "restated=", "reason=",
                "web_intent=", "turn=", "sub_hash="]
ASK_FIELDS = ["stt=", "rag=", "tts=", "total=", "clip_bytes=", "transcript_words=",
              "answer_words=", "answer_chars=", "audio_bytes=", "fmt=", "history_turns=",
              "turn=", "web=", "sub_hash="]


def test_logs_carry_documented_fields_and_no_text(wk, caplog):
    caplog.set_level(logging.DEBUG)
    out = avt.start(start_body())
    avt.put_state(wk.s3, BUCKET, SUB, TURN, {"status": "pending"})
    avt.worker_handler(event(), None)
    wk.mp.setattr(laa, "_rag_answer", lambda b, on_web_start=None: (_ for _ in ()).throw(
        RuntimeError(TRANSCRIPT + " " + ANSWER)))
    avt.put_state(wk.s3, BUCKET, SUB, TURN, {"status": "pending"})
    avt.worker_handler(event(), None)
    text = "\n".join(r.getMessage() for r in caplog.records)
    for forbidden in (TRANSCRIPT, "slab", "zebra", SPOKEN, SUB):
        assert forbidden not in text
    start_line = next(r.getMessage() for r in caplog.records
                      if r.getMessage().startswith("voice start:"))
    ask_line = next(r.getMessage() for r in caplog.records
                    if r.getMessage().startswith("voice ask:"))
    for f in START_FIELDS:
        assert f in start_line
    for f in ASK_FIELDS:
        assert f in ask_line
    assert "turn=%s" % out["turn_id"] in start_line
    assert "turn=%s" % TURN in ask_line and "web=False" in ask_line
    assert "sub_hash=%s" % avt.sub_hash(SUB) in start_line


@pytest.mark.parametrize("voice_web,web_answer", [("false", "true"), ("true", "false"), ("", "")])
def test_start_does_not_promise_a_search_it_cannot_do(env, voice_web, web_answer):
    # "Searching online now" is spoken before the worker runs; with either flag
    # off the worker never searches, so the restatement must not say it will.
    env.mp.setenv("VOICE_WEB_ENABLED", voice_web)
    env.mp.setenv("ENABLE_WEB_ANSWER", web_answer)
    env.mp.setattr(laa, "_stt", lambda a, f: "what is the NZS 4541 spacing, search online")
    out = avt.start(start_body())
    assert out["web_intent"] is False
    assert env.lam.calls[0]["payload"]["web_intent"] is False


# ------------------------------------------- conversation memory + web offer ----

import voice_web as vw  # noqa: E402

try:
    from lambda_fieldsight_api import _DONE_FIELDS
except Exception:  # noqa: BLE001 - the api module may not import in a bare env
    _DONE_FIELDS = ('transcript', 'answerText', 'basis', 'audioBase64', 'audioFormat', 'web')

OFFERED = "what is the maximum span for a purlin"


def _put_json(s3, key, obj, age_s=0):
    s3.objs[key] = (json.dumps(obj).encode("utf-8"),
                    datetime.now(timezone.utc) - timedelta(seconds=age_s), None)


def seed_offer(s3, question=OFFERED, age_s=5):
    _put_json(s3, avt.offer_key(SUB), {"question": question, "at": time.time() - age_s})


def seed_convo(s3, ages=(30,)):
    now = time.time()
    _put_json(s3, avt.convo_key(SUB), {
        "updated_at": now,
        "turns": [{"question": "zq%d" % i, "answer": "za%d" % i, "at": now - a}
                  for i, a in enumerate(ages)]})


@pytest.fixture
def cv(env):
    env.mp.setenv("ASK_VOICE_CONVO", "true")
    env.mp.setenv("VOICE_WEB_ENABLED", "true")
    env.mp.setenv("ENABLE_WEB_ANSWER", "true")
    env.restated = []
    env.tts = []

    def fake_restate(q, *, web_intent, timeout_s=1.2):
        env.restated.append((q, web_intent))
        return {"text": "Checking.", "reason": None, "ms": 5}
    env.mp.setattr(voice_restate, "restate", fake_restate)

    def tts(text):
        env.tts.append(text)
        return b"RIFFwav"
    env.mp.setattr(laa, "_tts", tts)

    def say(text):
        env.mp.setattr(laa, "_stt", lambda a, f: text)
    env.say = say
    return env


def test_convo_keys_cannot_be_addressed_as_a_turn():
    assert avt.convo_key(SUB) == avt.owner_prefix(SUB) + "convo.json"
    assert avt.offer_key(SUB) == avt.owner_prefix(SUB) + "offer.json"
    assert not avt.TURN_RE.match("convo") and not avt.TURN_RE.match("offer")


def test_sweep_keeps_young_convo_and_offer_and_drops_old(cv):
    seed_offer(cv.s3)
    seed_convo(cv.s3)
    old = avt.owner_prefix(SUB) + "stale.json"
    cv.s3.objs[old] = (b"{}", datetime.now(timezone.utc) - timedelta(days=2), None)
    assert avt.sweep_old(cv.s3, BUCKET, SUB, time.time()) == 1
    assert avt.convo_key(SUB) in cv.s3.objs and avt.offer_key(SUB) in cv.s3.objs


def test_yes_to_a_fresh_offer_searches_the_offered_question(cv):
    seed_offer(cv.s3)
    seed_convo(cv.s3)
    cv.say("Yes.")
    out = avt.start(start_body())
    p = cv.lam.calls[0]["payload"]
    assert p["question"] == OFFERED and p["web_intent"] is True
    assert "history" not in p and p["transcript"] == "Yes."
    assert out["transcript"] == "Yes." and out["web_intent"] is True
    assert cv.restated == [(OFFERED, True)]
    assert avt.offer_key(SUB) not in cv.s3.objs


def test_a_bare_search_online_with_a_fresh_offer_is_a_yes(cv):
    seed_offer(cv.s3)
    cv.say("Search online.")
    avt.start(start_body())
    p = cv.lam.calls[0]["payload"]
    assert p["question"] == OFFERED and p["web_intent"] is True


def test_search_online_with_a_real_question_is_an_ordinary_web_question(cv):
    seed_offer(cv.s3)
    cv.say("search online for the slump of 25 MPa concrete")
    avt.start(start_body())
    p = cv.lam.calls[0]["payload"]
    assert "question" not in p and p["web_intent"] is True
    assert avt.offer_key(SUB) not in cv.s3.objs


def test_yes_to_a_stale_offer_is_an_ordinary_question(cv):
    seed_offer(cv.s3, age_s=avt.OFFER_WINDOW_S + 5)
    cv.say("yes")
    avt.start(start_body())
    p = cv.lam.calls[0]["payload"]
    assert "question" not in p and p["web_intent"] is False
    assert avt.offer_key(SUB) not in cv.s3.objs


def test_yes_with_the_web_off_is_an_ordinary_question(cv):
    cv.mp.setenv("VOICE_WEB_ENABLED", "false")
    seed_offer(cv.s3)
    cv.say("yes")
    avt.start(start_body())
    assert "question" not in cv.lam.calls[0]["payload"]


def test_no_to_a_fresh_offer_finishes_at_start(cv):
    seed_offer(cv.s3)
    seed_convo(cv.s3)
    cv.say("no thanks")
    out = avt.start(start_body())
    assert cv.lam.calls == [] and cv.tts == [vw.DECLINED]
    st = cv.s3.state(SUB, out["turn_id"])
    assert st["status"] == "done"
    for k in _DONE_FIELDS:
        assert k in st, k
    assert st["transcript"] == "no thanks" and st["answerText"] == "Okay."
    assert st["basis"] is None and st["audioFormat"] == "wav" and st["web"] is False
    assert base64.b64decode(st["audioBase64"]) == b"RIFFwav"
    assert isinstance(st["created_at"], float)
    assert out["web_intent"] is False and out["restatement"] is None
    assert out["transcript"] == "no thanks"
    assert avt.offer_key(SUB) not in cv.s3.objs


def test_no_with_a_failed_tts_writes_an_error_state(cv):
    seed_offer(cv.s3)
    cv.say("no")

    def boom(text):
        raise RuntimeError("tts down")
    cv.mp.setattr(laa, "_tts", boom)
    out = avt.start(start_body())
    assert cv.s3.state(SUB, out["turn_id"]) == {"status": "error",
                                                "error": "Speech synthesis failed"}
    assert cv.lam.calls == []


def test_no_without_an_offer_is_an_ordinary_question(cv):
    cv.say("no")
    out = avt.start(start_body())
    assert cv.lam.calls and cv.s3.state(SUB, out["turn_id"])["status"] == "pending"


def test_thank_you_after_an_answer_is_a_new_question_with_history(cv):
    seed_offer(cv.s3)
    seed_convo(cv.s3)
    cv.say("thank you")
    avt.start(start_body())
    p = cv.lam.calls[0]["payload"]
    assert "question" not in p and p["history"] == [{"question": "zq0", "answer": "za0"}]


def test_history_is_fresh_turns_only_capped_at_three(cv):
    seed_convo(cv.s3, ages=(500, 170, 100, 60, 10))
    avt.start(start_body())
    h = cv.lam.calls[0]["payload"]["history"]
    assert [t["question"] for t in h] == ["zq2", "zq3", "zq4"]
    assert all(set(t) == {"question", "answer"} for t in h)


def test_a_body_history_still_wins(cv):
    seed_convo(cv.s3)
    avt.start(start_body(history=[{"question": "x", "answer": "y"}]))
    assert cv.lam.calls[0]["payload"]["history"] == [{"question": "x", "answer": "y"}]


@pytest.mark.parametrize("body", [None, b"not json"])
def test_missing_or_garbage_convo_is_the_ordinary_path(cv, body):
    if body is not None:
        cv.s3.objs[avt.convo_key(SUB)] = (body, datetime.now(timezone.utc), None)
    avt.start(start_body())
    assert "history" not in cv.lam.calls[0]["payload"]


@pytest.mark.parametrize("key_of", [avt.convo_key, avt.offer_key])
def test_a_403_is_logged_as_an_error_not_treated_as_absence(cv, caplog, key_of):
    class Denied(FakeS3):
        def get_object(self, Bucket, Key):
            if Key == key_of(SUB):
                raise ClientError({"Error": {"Code": "AccessDenied", "Message": "x"}}, "GetObject")
            return super().get_object(Bucket, Key)
    s3 = Denied()
    cv.mp.setattr(avt, "_get_s3", lambda: s3)
    caplog.set_level(logging.INFO)
    out = avt.start(start_body())
    errs = [r for r in caplog.records
            if r.levelno >= logging.ERROR and "voice convo read denied" in r.getMessage()]
    assert errs
    assert out["turn_id"] and "history" not in cv.lam.calls[0]["payload"]


def test_offer_web_is_set_unless_the_caller_asked_for_the_web(cv):
    avt.start(start_body())
    assert cv.lam.calls[-1]["payload"]["offer_web"] is True
    cv.say("what is the NZS 4541 spacing, search online")
    avt.start(start_body())
    assert cv.lam.calls[-1]["payload"]["offer_web"] is False


def test_offer_web_is_false_when_the_web_is_off(cv):
    cv.mp.setenv("VOICE_WEB_ENABLED", "false")
    avt.start(start_body())
    assert cv.lam.calls[0]["payload"]["offer_web"] is False


def test_start_log_gains_counts_and_no_text(cv, caplog):
    seed_offer(cv.s3)
    seed_convo(cv.s3, ages=(30, 20))
    cv.say("what about the roof")
    caplog.set_level(logging.DEBUG)
    avt.start(start_body())
    text = "\n".join(r.getMessage() for r in caplog.records)
    line = next(r.getMessage() for r in caplog.records
                if r.getMessage().startswith("voice start:"))
    assert "history_turns=2" in line and "offer=cleared" in line
    for forbidden in (OFFERED, "zq0", "za1", "roof", SUB):
        assert forbidden not in text


@pytest.mark.parametrize("said,expected", [("yes", "consumed_yes"), ("no", "consumed_no")])
def test_start_log_names_the_consumed_offer(cv, caplog, said, expected):
    seed_offer(cv.s3)
    cv.say(said)
    caplog.set_level(logging.INFO)
    avt.start(start_body())
    line = next(r.getMessage() for r in caplog.records
                if r.getMessage().startswith("voice start:"))
    assert "offer=" + expected in line


def test_flag_off_makes_no_convo_or_offer_call_and_adds_no_payload_keys(env):
    env.mp.setenv("VOICE_WEB_ENABLED", "true")
    env.mp.setenv("ENABLE_WEB_ANSWER", "true")
    seen = []

    class Spy(FakeS3):
        def get_object(self, Bucket, Key):
            seen.append(("get", Key))
            return super().get_object(Bucket, Key)

        def delete_object(self, Bucket, Key):
            seen.append(("del", Key))
            return super().delete_object(Bucket, Key)

        def put_object(self, Bucket, Key, Body, ContentType=None):
            seen.append(("put", Key))
            return super().put_object(Bucket, Key, Body, ContentType)
    s3 = Spy()
    seed_offer(s3)
    seed_convo(s3)
    env.mp.setattr(avt, "_get_s3", lambda: s3)
    env.mp.setattr(laa, "_stt", lambda a, f: "yes")
    avt.start(start_body(tz="Pacific/Auckland"))
    assert not [k for _, k in seen if k.endswith(("convo.json", "offer.json"))]
    assert set(env.lam.calls[0]["payload"]) == {
        "turn_id", "caller_sub", "transcript", "web_intent", "tz"}


# ---- worker ----

def _flag(wk, on=True):
    wk.mp.setenv("ASK_VOICE_CONVO", "true" if on else "false")


def _convo(wk):
    k = avt.convo_key(SUB)
    return json.loads(wk.s3.objs[k][0]) if k in wk.s3.objs else None


def _offer(wk):
    k = avt.offer_key(SUB)
    return json.loads(wk.s3.objs[k][0]) if k in wk.s3.objs else None


def _rag_returning(wk, **extra):
    def rag(body, on_web_start=None):
        wk.rag_calls.append(body)
        return dict({"answer": ANSWER, "basis": {"k": 5}}, **extra)
    wk.mp.setattr(laa, "_rag_answer", rag)


def test_worker_prefers_the_event_question_and_audits_it(wk):
    _flag(wk)
    avt.worker_handler(event(transcript="yes", web_intent=True, question=OFFERED,
                             offer_web=False), None)
    body = wk.rag_calls[0]
    assert body["question"] == OFFERED and body["web_intent"] is True
    assert wk.audits == [(SUB, OFFERED, SPOKEN)]
    assert wk.s3.state(SUB, TURN)["transcript"] == "yes"


def test_worker_passes_offer_web_and_history_through(wk):
    _flag(wk)
    h = [{"question": "q", "answer": "a"}]
    avt.worker_handler(event(offer_web=True, history=h), None)
    assert wk.rag_calls[0]["offer_web"] is True and wk.rag_calls[0]["history"] == h


def test_worker_flag_off_adds_no_rag_keys_and_writes_no_objects(wk):
    _flag(wk, on=False)
    avt.worker_handler(event(), None)
    assert "offer_web" not in wk.rag_calls[0]
    assert [k for k, _ in wk.s3.puts] == [avt.state_key(SUB, TURN)]
    assert _convo(wk) is None and _offer(wk) is None


def test_worker_done_writes_the_convo_and_the_offer_when_offered(wk):
    _flag(wk)
    _rag_returning(wk, web_offer=True, asked="the standalone version")
    avt.worker_handler(event(), None)
    c = _convo(wk)
    assert [t["question"] for t in c["turns"]] == [TRANSCRIPT]
    assert c["turns"][0]["answer"] == SPOKEN
    assert _offer(wk)["question"] == "the standalone version"


def test_worker_offer_falls_back_to_the_question_asked(wk):
    _flag(wk)
    _rag_returning(wk, web_offer=True)
    avt.worker_handler(event(), None)
    assert _offer(wk)["question"] == TRANSCRIPT


def test_worker_done_without_an_offer_never_deletes(wk):
    # Start consumes any earlier offer; the worker role has no DeleteObject.
    _flag(wk)
    deletes = []
    real = wk.s3.delete_object
    wk.s3.delete_object = lambda **kw: (deletes.append(kw["Key"]), real(**kw))
    avt.worker_handler(event(), None)
    assert deletes == []
    assert _offer(wk) is None
    assert _convo(wk) is not None


def test_worker_trims_the_convo_to_three_and_drops_stale_turns(wk):
    _flag(wk)
    seed_convo(wk.s3, ages=(500, 100, 60, 40))
    avt.worker_handler(event(), None)
    qs = [t["question"] for t in _convo(wk)["turns"]]
    assert qs == ["zq2", "zq3", TRANSCRIPT]


def test_worker_garbage_convo_is_replaced(wk):
    _flag(wk)
    wk.s3.objs[avt.convo_key(SUB)] = (b"{{", datetime.now(timezone.utc), None)
    avt.worker_handler(event(), None)
    assert len(_convo(wk)["turns"]) == 1


def test_worker_cancel_before_the_final_check_writes_neither(wk):
    _flag(wk)
    seed_offer(wk.s3)

    def tts(text):
        avt.put_cancel(wk.s3, BUCKET, SUB, TURN)
        return b"RIFF"
    wk.mp.setattr(laa, "_tts", tts)
    avt.worker_handler(event(), None)
    assert _convo(wk) is None and _offer(wk) is not None


def test_worker_cancel_right_after_done_writes_neither(wk):
    _flag(wk)
    orig = avt.put_state

    def spy(s3, bucket, sub, turn, state):
        orig(s3, bucket, sub, turn, state)
        if state["status"] == "done":
            avt.put_cancel(s3, bucket, sub, turn)
    wk.mp.setattr(avt, "put_state", spy)
    avt.worker_handler(event(), None)
    assert _convo(wk) is None and _offer(wk) is None


def test_worker_error_writes_neither(wk):
    _flag(wk)
    wk.mp.setattr(laa, "_rag_answer", lambda b, on_web_start=None: {"error": "boom"})
    avt.worker_handler(event(), None)
    assert wk.s3.state(SUB, TURN)["status"] == "error"
    assert _convo(wk) is None and _offer(wk) is None


def test_worker_convo_write_failure_still_leaves_done(wk, caplog):
    _flag(wk)
    orig = wk.s3.put_object

    def put(Bucket, Key, Body, ContentType=None):
        if Key.endswith(("convo.json", "offer.json")):
            raise RuntimeError(TRANSCRIPT)
        return orig(Bucket, Key, Body, ContentType)
    wk.s3.put_object = put
    caplog.set_level(logging.INFO)
    avt.worker_handler(event(), None)
    assert wk.s3.state(SUB, TURN)["status"] == "done"
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "RuntimeError" in text and "zebra" not in text


def test_worker_audits_the_transcript_when_no_question_was_carried(wk):
    _flag(wk)
    avt.worker_handler(event(web_intent=True, transcript="what is the span, search online"), None)
    assert wk.audits == [(SUB, "what is the span, search online", SPOKEN)]
