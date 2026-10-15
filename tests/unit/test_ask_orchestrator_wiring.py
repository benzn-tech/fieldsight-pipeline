"""Ask orchestrator v2 phase 3, Task 4: worker wiring, the ASK_AGENTIC flag, template.

Spec: docs/superpowers/specs/2026-10-15-ask-orchestration-v2-design.md §4.4, §6, §10.
Plan: docs/superpowers/plans/2026-10-16-ask-orchestration-v2-phase3.md Task 4.

* Flag off (or the channel not listed): zero calls into ask_orchestrator.
* Flag on: the worker calls ask_orchestrator.answer with the rag body it builds
  today, and filters on_state per channel. Voice writes only `searching_web`
  (GrandTime fails a turn on any status it does not know).
* The result route accepts `searching_records` / `writing` as `since` for
  screen turns only.
"""
import io
import json
import os
import re
import sys
import types
from datetime import datetime, timezone

import pytest

laa = pytest.importorskip("lambda_ask_agent", reason="requires boto3/urllib3 (installed in CI)")
fapi = pytest.importorskip("lambda_fieldsight_api", reason="requires boto3 (installed in CI)")

from botocore.exceptions import ClientError  # noqa: E402

import ask_voice_turn as avt  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEMPLATE = os.path.join(REPO, "src", "template.yaml")
PROD_WORKFLOW = os.path.join(REPO, ".github", "workflows", "deploy-prod.yml")
TEST_WORKFLOW = os.path.join(REPO, ".github", "workflows", "deploy.yml")

SUB = "sub-aaaa-1111"
BUCKET = "test-bucket"
TURN = "e" * 32
RAG_RESULT = {"answer": "B2 was poured [1].", "citations": [{"topic_title": "Pour"}],
              "grounded": True, "basis": {"from": None, "to": None}}


class FakeS3:
    """Same shape as the fake in test_ask_screen_turn.py (copied, not imported)."""

    def __init__(self):
        self.objs = {}
        self.puts = []

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.puts.append(Key)
        self.objs[Key] = (Body, datetime.now(timezone.utc))

    def get_object(self, Bucket, Key):
        if Key not in self.objs:
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "x"}}, "GetObject")
        return {"Body": io.BytesIO(self.objs[Key][0])}

    def delete_object(self, Bucket, Key):
        self.objs.pop(Key, None)

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):
        return {"Contents": [], "IsTruncated": False}


class Recorder(FakeS3):
    """Records every state status written for the turn, in order."""

    def __init__(self):
        super().__init__()
        self.statuses = []

    def put_object(self, Bucket, Key, Body, ContentType=None):
        super().put_object(Bucket, Key, Body, ContentType)
        if Key == avt.state_key(SUB, TURN):
            self.statuses.append(json.loads(Body)["status"])


class Raising(types.ModuleType):
    """A stand-in ask_orchestrator whose every attribute access fails the test."""

    def __getattr__(self, name):
        raise AssertionError(f"ask_orchestrator.{name} touched with the flag off")


@pytest.fixture
def wk(monkeypatch):
    monkeypatch.setenv("RAG_SEARCH_FUNCTION", "rag-fn")
    monkeypatch.setenv("ASK_TURN_BUCKET", BUCKET)
    monkeypatch.delenv("ASK_AGENTIC", raising=False)
    s3 = Recorder()
    monkeypatch.setattr(avt, "_get_s3", lambda: s3)
    env = types.SimpleNamespace(s3=s3, mp=monkeypatch, rag_calls=[], orch_calls=[])

    def rag(body, on_web_start=None):
        env.rag_calls.append(body)
        return dict(RAG_RESULT)
    monkeypatch.setattr(laa, "_rag_answer", rag)
    monkeypatch.setattr(laa, "_tts", lambda text: b"RIFF")
    monkeypatch.setattr(laa, "_invoke_voice_audit", lambda *a: None)
    monkeypatch.setattr(avt, "_remember", lambda *a: None)
    return env


def fake_orchestrator(env, states=("searching_records", "searching_web", "writing")):
    mod = types.ModuleType("ask_orchestrator")

    def answer(body, *, channel, on_state, tools=None, chat=None):
        env.orch_calls.append({"body": body, "channel": channel})
        for s in states:
            on_state(s)
        return dict(RAG_RESULT)
    mod.answer = answer
    env.mp.setitem(sys.modules, "ask_orchestrator", mod)
    return mod


def pending(env, channel=None, request=None):
    state = {"status": "pending", "created_at": 123.5}
    if channel:
        state["channel"] = channel
        state["request"] = request or {"question": "What did we pour?", "tz": "Pacific/Auckland"}
    avt.put_state(env.s3, BUCKET, SUB, TURN, state)
    env.s3.statuses.clear()


def screen_event():
    return {"turn_id": TURN, "caller_sub": SUB, "channel": "screen"}


def voice_event(**over):
    e = {"turn_id": TURN, "caller_sub": SUB, "transcript": "what did we pour",
         "tz": "Pacific/Auckland", "history": [{"question": "q0", "answer": "a0"}],
         "offer_web": True}
    e.update(over)
    return e


# --------------------------------------------------------------- _agentic

@pytest.mark.parametrize("raw,screen,voice", [
    ("", False, False), ("screen", True, False), ("voice", False, True),
    ("screen,voice", True, True), ("voice,screen", True, True), (" screen , voice ", True, True),
    ("screenvoice", False, False),
])
def test_agentic_reads_the_comma_list(monkeypatch, raw, screen, voice):
    monkeypatch.setenv("ASK_AGENTIC", raw)
    assert avt._agentic("screen") is screen and avt._agentic("voice") is voice


def test_agentic_unset_is_off(monkeypatch):
    monkeypatch.delenv("ASK_AGENTIC", raising=False)
    assert avt._agentic("screen") is False and avt._agentic("voice") is False


# --------------------------------------------------------- flag off: no calls

@pytest.mark.parametrize("flag", [None, "", "voice"])
def test_flag_off_screen_never_touches_the_orchestrator(wk, flag):
    if flag is not None:
        wk.mp.setenv("ASK_AGENTIC", flag)
    wk.mp.setitem(sys.modules, "ask_orchestrator", Raising("ask_orchestrator"))
    pending(wk, channel="screen")
    avt.worker_handler(screen_event(), None)
    assert len(wk.rag_calls) == 1
    assert wk.s3.statuses == ["done"]


@pytest.mark.parametrize("flag", [None, "", "screen"])
def test_flag_off_voice_never_touches_the_orchestrator(wk, flag):
    if flag is not None:
        wk.mp.setenv("ASK_AGENTIC", flag)
    wk.mp.setitem(sys.modules, "ask_orchestrator", Raising("ask_orchestrator"))
    pending(wk)
    avt.worker_handler(voice_event(), None)
    assert len(wk.rag_calls) == 1
    assert wk.s3.statuses == ["done"]


# --------------------------------------------------------------- flag on

def test_screen_on_calls_the_orchestrator_with_the_rag_body(wk):
    wk.mp.setenv("ASK_AGENTIC", "screen")
    fake_orchestrator(wk)
    pending(wk, channel="screen", request={"question": "What did we pour?",
                                           "tz": "Pacific/Auckland", "mode": "voice",
                                           "caller_sub": "evil", "site_id": "s"})
    avt.worker_handler(screen_event(), None)
    assert wk.rag_calls == []
    (call,) = wk.orch_calls
    assert call["channel"] == "screen"
    assert call["body"] == {"question": "What did we pour?", "tz": "Pacific/Auckland",
                            "site_id": "s", "caller_sub": SUB}
    assert wk.s3.statuses == ["searching_records", "searching_web", "writing", "done"]
    st = avt.get_state(wk.s3, BUCKET, SUB, TURN)
    assert st["channel"] == "screen" and st["result"]["answer"] == RAG_RESULT["answer"]


def test_screen_progress_states_carry_channel_and_created_at(wk):
    wk.mp.setenv("ASK_AGENTIC", "screen")
    fake_orchestrator(wk, states=("writing",))
    pending(wk, channel="screen")
    seen = []
    real_put = avt.put_state

    def spy(s3, bucket, sub, turn, state):
        seen.append(dict(state))
        return real_put(s3, bucket, sub, turn, state)
    wk.mp.setattr(avt, "put_state", spy)
    avt.worker_handler(screen_event(), None)
    assert seen[0] == {"status": "writing", "channel": "screen", "created_at": 123.5}


def test_voice_on_writes_only_searching_web(wk):
    """The named test (spec §4.4): under channel=voice, on_state writes no status
    the device does not know."""
    wk.mp.setenv("ASK_AGENTIC", "voice")
    fake_orchestrator(wk, states=("searching_records", "searching_web", "writing",
                                  "searching_records"))
    pending(wk)
    avt.worker_handler(voice_event(), None)
    progress = wk.s3.statuses[:-1]
    assert set(progress) <= {"searching_web"} and progress == ["searching_web"]
    assert wk.s3.statuses[-1] == "done"


def test_voice_on_passes_todays_rag_body(wk):
    wk.mp.setenv("ASK_AGENTIC", "voice")
    wk.mp.setenv("VOICE_WEB_ENABLED", "true")
    fake_orchestrator(wk, states=())
    pending(wk)
    avt.worker_handler(voice_event(web_intent=True, question="What cover does B2 need?"),
                       None)
    (call,) = wk.orch_calls
    assert call["channel"] == "voice"
    assert call["body"] == {"question": "What cover does B2 need?", "caller_sub": SUB,
                            "mode": "voice", "k": 5, "tz": "Pacific/Auckland",
                            "voice_web": True, "web_intent": True,
                            "history": [{"question": "q0", "answer": "a0"}],
                            "offer_web": True}


def test_on_state_is_silent_once_cancelled(wk):
    wk.mp.setenv("ASK_AGENTIC", "screen,voice")
    fake_orchestrator(wk)
    pending(wk, channel="screen")
    avt.put_cancel(wk.s3, BUCKET, SUB, TURN)
    wk.s3.statuses.clear()
    avt.worker_handler(screen_event(), None)
    assert wk.s3.statuses == []


def test_on_state_write_failure_never_ends_the_turn(wk):
    wk.mp.setenv("ASK_AGENTIC", "screen")
    real_put = avt.put_state

    def flaky(s3, bucket, sub, turn, state):
        if state.get("status") == "writing":
            raise RuntimeError("s3 down")
        return real_put(s3, bucket, sub, turn, state)
    wk.mp.setattr(avt, "put_state", flaky)
    fake_orchestrator(wk, states=("writing",))
    pending(wk, channel="screen")
    avt.worker_handler(screen_event(), None)
    assert avt.get_state(wk.s3, BUCKET, SUB, TURN)["status"] == "done"


# ------------------------------------------------------ result route `since`

class ApiS3:
    def __init__(self):
        self.objs = {}

    def put_object(self, Bucket, Key, Body=b"", **kw):
        self.objs[(Bucket, Key)] = Body

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objs:
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "x"}}, "GetObject")
        return {"Body": io.BytesIO(self.objs[(Bucket, Key)])}

    def delete_object(self, Bucket, Key):
        self.objs.pop((Bucket, Key), None)


@pytest.fixture
def api_s3(monkeypatch):
    fake = ApiS3()
    monkeypatch.setattr(fapi, "_turn_s3", lambda: fake)
    monkeypatch.setenv("ASK_TURN_BUCKET", BUCKET)
    monkeypatch.setenv("RESULT_WAIT_S", "0")
    return fake


def _result(since):
    caller = {"sub": SUB, "email": "", "name": "", "role": "worker", "display_name": ""}
    return fapi.ask_voice_result({"turn": TURN, "since": since}, caller,
                                 clock=lambda: 1000.0, sleep=lambda s: None,
                                 wall=lambda: 1000.0)


@pytest.mark.parametrize("since", ["searching_records", "writing"])
def test_screen_turn_accepts_the_new_since_values(api_s3, since):
    avt.put_state(api_s3, BUCKET, SUB, TURN, {"status": since, "channel": "screen",
                                             "created_at": 999.0})
    r = _result(since)
    assert r["statusCode"] == 200 and json.loads(r["body"]) == {"status": since}


@pytest.mark.parametrize("state", [{"status": "searching_web", "created_at": 999.0},
                                   {"status": "searching_web", "channel": "voice",
                                    "created_at": 999.0}])
def test_voice_turn_rejects_since_writing(api_s3, state):
    avt.put_state(api_s3, BUCKET, SUB, TURN, state)
    r = _result("writing")
    assert r["statusCode"] == 400 and json.loads(r["body"])["error"] == "Invalid since"
    assert _result("searching_records")["statusCode"] == 400


def test_voice_turn_keeps_its_old_since_values(api_s3):
    avt.put_state(api_s3, BUCKET, SUB, TURN, {"status": "searching_web",
                                             "created_at": 999.0})
    assert _result("pending")["statusCode"] == 200
    assert _result("searching_web")["statusCode"] == 200


def test_garbage_since_is_rejected_before_any_read(api_s3):
    assert _result("done")["statusCode"] == 400
    assert _result("bogus")["statusCode"] == 400


# ----------------------------------------------------------------- template

_yaml = pytest.importorskip("yaml")


class _Loader(_yaml.SafeLoader):
    pass


_Loader.add_multi_constructor(
    "!", lambda loader, suffix, node: {suffix: loader.construct_scalar(node)}
    if isinstance(node, _yaml.ScalarNode) else
    {suffix: loader.construct_sequence(node, deep=True)}
    if isinstance(node, _yaml.SequenceNode) else
    {suffix: loader.construct_mapping(node, deep=True)})


@pytest.fixture(scope="module")
def tmpl():
    with io.open(TEMPLATE, encoding="utf-8") as f:
        return _yaml.load(f.read(), Loader=_Loader)


def test_ask_agentic_parameter(tmpl):
    p = tmpl["Parameters"]["AskAgentic"]
    assert p["Type"] == "String" and p["Default"] == ""
    assert p["AllowedPattern"] == "^(|screen|voice|screen,voice|voice,screen)$"
    for name in ("AskJudgeModel", "AskJudgeFallbacks"):
        assert tmpl["Parameters"][name]["Type"] == "String"
        assert tmpl["Parameters"][name]["Default"] == ""


def test_judge_env_is_on_the_worker_only(tmpl):
    res = tmpl["Resources"]
    worker = res["AskVoiceWorkerFunction"]["Properties"]["Environment"]["Variables"]
    assert worker["ASK_AGENTIC"] == {"Ref": "AskAgentic"}
    assert worker["ASK_JUDGE_MODEL"] == {"Ref": "AskJudgeModel"}
    assert worker["ASK_JUDGE_FALLBACKS"] == {"Ref": "AskJudgeFallbacks"}
    for name, r in res.items():
        if name == "AskVoiceWorkerFunction":
            continue
        env = ((r.get("Properties") or {}).get("Environment") or {}).get("Variables") or {}
        assert not {"ASK_AGENTIC", "ASK_JUDGE_MODEL", "ASK_JUDGE_FALLBACKS"} & set(env), name


def test_test_workflow_passes_the_flag_only_when_set():
    """`"AskAgentic=${{ vars.X || '' }}"` would be a bare `AskAgentic=` when the
    variable is unset, which SAM rejects (test_no_override_can_evaluate_to_a_bare_key).
    So both use the conditional $PARAM idiom the layer ARNs use."""
    text = io.open(TEST_WORKFLOW, encoding="utf-8").read()
    for var, param, shell in (("TEST_ASK_AGENTIC", "AskAgentic", "ASK_AGENTIC_PARAM"),
                              ("TEST_ASK_JUDGE_MODEL", "AskJudgeModel",
                               "ASK_JUDGE_MODEL_PARAM")):
        assert re.search(r'if \[ -n "\$\{\{ vars\.%s \}\}" \]; then\s+%s="%s=\$\{\{ vars\.%s \}\}"'
                         % (var, shell, param, var), text), param
        assert re.search(r"^\s+\$%s \\$" % shell, text, re.M), shell


def test_prod_workflow_never_passes_the_flag():
    """Prod holds the template default (the flag off) until the owner flips it.
    Not in UNWIRED_BY_DESIGN: that list is for boolean parameters only
    (test_the_exception_list_does_not_rot), and AskAgentic is a pattern."""
    text = io.open(PROD_WORKFLOW, encoding="utf-8").read()
    for name in ("AskAgentic", "AskJudgeModel", "AskJudgeFallbacks", "ASK_AGENTIC"):
        assert name not in text
