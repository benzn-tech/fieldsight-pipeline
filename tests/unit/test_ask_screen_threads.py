"""Unit: the AskVoiceWorker stores successful palette turns in the caller's thread
before it writes `done` (spec 2026-10-22 ask conversation history §4, D4/D7/D8)."""
import io
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy-key")

laa = pytest.importorskip("lambda_ask_agent", reason="requires boto3/urllib3 (installed in CI)")

from botocore.exceptions import ClientError  # noqa: E402

import ask_threads  # noqa: E402
import ask_voice_turn as avt  # noqa: E402

SUB = "sub-aaaa-1111"
BUCKET = "test-bucket"
TURN = "f" * 32
QUESTION = "zebra giraffe what is the slab thickness"
ANSWER = "The slab is two hundred millimetres thick [1]."
SOURCE_KEY = "extractions/ben/2026-10-20/sid" + "a" * 32
SOURCES = {"record_keys": [SOURCE_KEY], "documents": [], "report_days": []}


class FakeS3:
    """Same shape as the fake in test_ask_screen_turn.py (copied, not imported),
    plus an ordered op log so the append/done/evict order can be asserted."""

    def __init__(self):
        self.objs = {}
        self.puts = []
        self.deletes = []
        self.ops = []

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.ops.append(("put", Key))
        self.puts.append((Key, ContentType))
        self.objs[Key] = (Body, datetime.now(timezone.utc), ContentType)

    def get_object(self, Bucket, Key):
        if Key not in self.objs:
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "x"}}, "GetObject")
        return {"Body": io.BytesIO(self.objs[Key][0])}

    def delete_object(self, Bucket, Key):
        self.ops.append(("delete", Key))
        self.deletes.append(Key)
        self.objs.pop(Key, None)

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):
        return {"Contents": [{"Key": k, "LastModified": v[1]}
                             for k, v in sorted(self.objs.items()) if k.startswith(Prefix)],
                "IsTruncated": False}

    def state(self, sub, turn):
        k = avt.state_key(sub, turn)
        return json.loads(self.objs[k][0]) if k in self.objs else None

    def threads(self, sub):
        p = ask_threads.thread_prefix(sub)
        return {k: json.loads(v[0]) for k, v in self.objs.items() if k.startswith(p)}


@pytest.fixture
def wk(monkeypatch):
    monkeypatch.setenv("ASK_TURN_BUCKET", BUCKET)
    monkeypatch.setenv("ASK_THREADS", "true")
    monkeypatch.delenv("ASK_AGENTIC", raising=False)
    s3 = FakeS3()
    monkeypatch.setattr(avt, "_get_s3", lambda: s3)
    env = type("E", (), {})()
    env.s3, env.mp = s3, monkeypatch
    env.rag_result = {"answer": ANSWER, "citations": [{"source_s3_key": SOURCE_KEY}],
                      "grounded": True, "_sources": SOURCES}
    env.during_rag = None

    def rag(body, on_web_start=None):
        if env.during_rag:
            env.during_rag()
        return json.loads(json.dumps(env.rag_result))

    monkeypatch.setattr(laa, "_rag_answer", rag)
    return env


def request(**over):
    r = {"question": QUESTION, "tz": "Pacific/Auckland", "site_id": "s-7",
         "history": [{"question": "q0", "answer": "a0"}], "new_thread": True}
    r.update(over)
    return r


def run(env, req=None):
    avt.put_state(env.s3, BUCKET, SUB, TURN, {"status": "pending", "channel": "screen",
                                             "created_at": 123.5,
                                             "request": req if req is not None else request()})
    env.s3.ops.clear()
    avt.worker_handler({"turn_id": TURN, "caller_sub": SUB, "channel": "screen"}, None)
    return env.s3.state(SUB, TURN)


def test_success_with_new_thread_stores_and_returns_id(wk):
    st = run(wk, request(offer_web=True))
    assert st["status"] == "done"
    thread = st["result"]["thread"]
    assert thread["rolled_over"] is False
    docs = wk.s3.threads(SUB)
    assert list(docs) == [ask_threads.thread_key(SUB, thread["thread_id"])]
    doc = next(iter(docs.values()))
    assert doc["scope"] == {"site_id": "s-7"}
    (turn,) = doc["turns"]
    assert set(turn) == {"turn_id", "question", "at", "request", "result", "sources"}
    assert turn["turn_id"] == TURN and turn["question"] == QUESTION
    assert isinstance(turn["at"], float)
    assert turn["request"] == {"offer_web": True, "web_intent": False, "history_len": 1}
    assert turn["sources"] == SOURCES
    # The stored result is what the client renders: no manifest, no thread block.
    assert turn["result"] == {"answer": ANSWER, "citations": [{"source_s3_key": SOURCE_KEY}],
                              "grounded": True}


def test_follow_up_appends_to_the_supplied_thread(wk):
    tid = run(wk)["result"]["thread"]["thread_id"]
    st = run(wk, request(new_thread=None, thread_id=tid,
                         history=[{"question": "a", "answer": "b"}] * 2))
    assert st["result"]["thread"] == {"thread_id": tid, "rolled_over": False}
    doc = wk.s3.threads(SUB)[ask_threads.thread_key(SUB, tid)]
    assert len(doc["turns"]) == 2 and doc["turns"][1]["request"]["history_len"] == 2


def test_append_happens_before_done(wk):
    st = run(wk)
    key = ask_threads.thread_key(SUB, st["result"]["thread"]["thread_id"])
    puts = [k for op, k in wk.s3.ops if op == "put"]
    assert puts == [key, avt.state_key(SUB, TURN)]


@pytest.mark.parametrize("bad", [{"answer": "", "error": "x", "_sources": SOURCES},
                                 {"answer": "Partial", "error": "x"},
                                 {"answer": "", "citations": []},
                                 {"answer": "   ", "citations": []},
                                 {"answer": None}])
def test_error_or_empty_result_not_stored(wk, bad):
    wk.rag_result = bad
    st = run(wk)
    assert st["status"] == "done" and "thread" not in st["result"]
    assert "_sources" not in st["result"]
    assert wk.s3.threads(SUB) == {}


def test_no_thread_fields_not_stored(wk):
    req = request()
    del req["new_thread"]
    st = run(wk, req)
    assert st["status"] == "done" and "thread" not in st["result"]
    assert wk.s3.threads(SUB) == {}


@pytest.mark.parametrize("over", [{"new_thread": False}, {"new_thread": "true"},
                                  {"new_thread": None, "thread_id": "../x"}])
def test_non_true_new_thread_or_bad_id_not_stored(wk, over):
    st = run(wk, request(**over))
    assert "thread" not in st["result"] and wk.s3.threads(SUB) == {}


def test_flag_off_not_stored_and_sources_stripped(wk):
    wk.mp.setenv("ASK_THREADS", "false")
    st = run(wk)
    assert st["status"] == "done" and st["result"]["answer"] == ANSWER
    assert "thread" not in st["result"] and "_sources" not in st["result"]
    assert wk.s3.threads(SUB) == {}


def test_cancelled_before_append_not_stored(wk):
    wk.during_rag = lambda: avt.put_cancel(wk.s3, BUCKET, SUB, TURN)
    st = run(wk)
    assert st["status"] == "pending"
    assert wk.s3.threads(SUB) == {}


def test_append_failure_logs_type_only_and_done_written(wk, caplog):
    real_put = wk.s3.put_object

    def put(Bucket, Key, Body, ContentType=None):
        if Key.startswith(ask_threads.PREFIX):
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": QUESTION}},
                              "PutObject")
        return real_put(Bucket, Key, Body, ContentType)

    wk.s3.put_object = put
    caplog.set_level(logging.INFO)
    st = run(wk)
    assert st["status"] == "done" and st["result"]["answer"] == ANSWER
    assert "thread" not in st["result"] and "_sources" not in st["result"]
    assert "ClientError" in caplog.text
    for text in (QUESTION, ANSWER, "slab", SOURCE_KEY, SUB):
        assert text not in caplog.text


def test_evicted_id_returns_new_id(wk):
    gone = "a" * 32
    st = run(wk, request(new_thread=None, thread_id=gone))
    thread = st["result"]["thread"]
    assert thread["thread_id"] != gone and thread["rolled_over"] is True
    assert ask_threads.thread_key(SUB, gone) not in wk.s3.objs
    assert ask_threads.thread_key(SUB, thread["thread_id"]) in wk.s3.objs


def _seed_threads(env, n):
    keys = []
    for i in range(n):
        tid = "%032x" % (i + 1)
        key = ask_threads.thread_key(SUB, tid)
        now = time.time()
        doc = {"v": 1, "thread_id": tid, "created_at": now, "updated_at": now, "scope": {},
               "turns": [{"turn_id": tid, "question": "q", "at": now}]}
        env.s3.objs[key] = (json.dumps(doc).encode("utf-8"),
                            datetime.now(timezone.utc) - timedelta(hours=10 - i), None)
        keys.append(key)
    return keys


def test_eviction_runs_after_done(wk):
    old = _seed_threads(wk, 5)
    st = run(wk)
    new_key = ask_threads.thread_key(SUB, st["result"]["thread"]["thread_id"])
    ops = wk.s3.ops
    done_at = ops.index(("put", avt.state_key(SUB, TURN)))
    assert ops.index(("put", new_key)) < done_at
    assert ops.index(("delete", old[0])) > done_at
    assert set(wk.s3.threads(SUB)) == set(old[1:]) | {new_key}


def test_append_to_existing_thread_does_not_evict(wk):
    old = _seed_threads(wk, 6)
    st = run(wk, request(new_thread=None, thread_id=old[-1].rsplit("/", 1)[1][:-5]))
    assert st["result"]["thread"]["rolled_over"] is False
    assert not [k for op, k in wk.s3.ops if op == "delete"]


def test_eviction_failure_never_turns_done_into_error(wk, caplog):
    _seed_threads(wk, 5)

    def boom(**kw):
        raise ClientError({"Error": {"Code": "AccessDenied", "Message": "x"}}, "ListObjectsV2")

    wk.s3.list_objects_v2 = boom
    caplog.set_level(logging.INFO)
    st = run(wk)
    assert st["status"] == "done" and st["result"]["thread"]["thread_id"]
    assert "ClientError" in caplog.text


@pytest.mark.parametrize("flag", ["true", "false"])
def test_done_never_contains_sources(wk, flag):
    wk.mp.setenv("ASK_THREADS", flag)
    st = run(wk)
    assert "_sources" not in json.dumps(st) and SOURCE_KEY in json.dumps(st)   # cited card only
    wk.rag_result = {"answer": ANSWER, "citations": [], "_sources": SOURCES}
    st = run(wk)
    assert "_sources" not in json.dumps(st) and SOURCE_KEY not in json.dumps(st)


def test_orchestrator_path_also_strips_and_stores(wk):
    import ask_orchestrator
    wk.mp.setenv("ASK_AGENTIC", "screen")
    wk.mp.setattr(ask_orchestrator, "answer",
                  lambda body, **kw: {"answer": ANSWER, "citations": [], "_sources": SOURCES})
    st = run(wk)
    assert "_sources" not in st["result"]
    doc = wk.s3.threads(SUB)[ask_threads.thread_key(SUB, st["result"]["thread"]["thread_id"])]
    assert doc["turns"][0]["sources"] == SOURCES


def test_missing_manifest_stores_an_empty_one(wk):
    wk.rag_result = {"answer": ANSWER, "citations": []}
    st = run(wk)
    doc = wk.s3.threads(SUB)[ask_threads.thread_key(SUB, st["result"]["thread"]["thread_id"])]
    assert doc["turns"][0]["sources"] == {"record_keys": [], "documents": [], "report_days": [],
                                          "topic_ids": []}


# ------------------------------------------------- template and deploy wiring

import re  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEMPLATE = os.path.join(REPO, "src", "template.yaml")
TEST_WORKFLOW = os.path.join(REPO, ".github", "workflows", "deploy.yml")
PROD_WORKFLOW = os.path.join(REPO, ".github", "workflows", "deploy-prod.yml")

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


def _statements(fn):
    out = []
    for pol in fn["Properties"].get("Policies") or []:
        if isinstance(pol, dict) and "Statement" in pol:
            out.extend(pol["Statement"])
    return out


def test_ask_threads_parameter_defaults_off(tmpl):
    p = tmpl["Parameters"]["AskThreads"]
    assert p["Type"] == "String" and p["Default"] == ""
    assert p["AllowedPattern"] == "^(|true|false)$"


def test_worker_reads_the_flag(tmpl):
    env = tmpl["Resources"]["AskVoiceWorkerFunction"]["Properties"]["Environment"]["Variables"]
    assert env["ASK_THREADS"] == {"Ref": "AskThreads"}
    assert env["ASK_TURN_BUCKET"] == {"Ref": "DataBucketName"}


def test_worker_s3_grant_on_ask_threads(tmpl):
    stmts = _statements(tmpl["Resources"]["AskVoiceWorkerFunction"])
    obj = [s for s in stmts if s.get("Resource") == {"Sub": "arn:aws:s3:::${DataBucketName}/ask_threads/*"}]
    assert len(obj) == 1
    assert sorted(obj[0]["Action"]) == ["s3:DeleteObject", "s3:GetObject", "s3:PutObject"]
    lst = [s for s in stmts if s.get("Action") == "s3:ListBucket"
           and s["Condition"]["StringLike"]["s3:prefix"] == ["ask_threads/*"]]
    assert len(lst) == 1 and lst[0]["Resource"] == {"Sub": "arn:aws:s3:::${DataBucketName}"}
    # The turn store's grant is unchanged: still no DeleteObject on ask_turns/*.
    turns = [s for s in stmts if s.get("Resource") == {"Sub": "arn:aws:s3:::${DataBucketName}/ask_turns/*"}]
    assert sorted(turns[0]["Action"]) == ["s3:GetObject", "s3:PutObject"]


@pytest.mark.parametrize("workflow,prefix", [(TEST_WORKFLOW, "TEST_"),
                                              (PROD_WORKFLOW, "PROD_")])
def test_workflow_passes_ask_threads_only_when_set(workflow, prefix):
    text = io.open(workflow, encoding="utf-8").read()
    var = prefix + "ASK_THREADS"
    assert re.search(r'ASK_THREADS_PARAM=""\s+if \[ -n "\$\{\{ vars\.%s \}\}" \]; then\s+'
                     r'ASK_THREADS_PARAM="AskThreads=\$\{\{ vars\.%s \}\}"' % (var, var), text)
    assert re.search(r"^\s+\$ASK_THREADS_PARAM \\$", text, re.M)



def test_turn_whose_answer_truncates_to_nothing_is_not_stored(wk):
    # D8 holds for the STORED answer: a question that fills the object leaves no room
    # for any answer text, so the turn is not stored rather than stored empty.
    st = run(wk, request(question="q" * ask_threads.MAX_BYTES))
    assert st["status"] == "done" and st["result"]["answer"] == ANSWER
    assert "thread" not in st["result"]
    assert wk.s3.threads(SUB) == {}
