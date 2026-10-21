"""Unit: the per-caller Ask thread store (spec 2026-10-22 ask conversation history §3)."""
import io
import json
import os
import re
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")

from botocore.exceptions import ClientError  # noqa: E402

import ask_threads  # noqa: E402
import ask_voice_turn as avt  # noqa: E402

SUB = "sub-aaaa-1111"
OTHER = "sub-bbbb-2222"
BUCKET = "test-bucket"
NOW = 1761000000.0
SCOPE = {"site_id": "s1"}


class FakeS3:
    """Same shape as the fake in test_ask_voice_turn.py (copied, not imported:
    importing a test module couples collection order), with a settable clock so
    LastModified can be placed in the past."""

    def __init__(self):
        self.objs = {}
        self.puts = []
        self.deletes = []
        self.clock = datetime.fromtimestamp(NOW, timezone.utc)

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.puts.append((Key, ContentType))
        self.objs[Key] = (Body, self.clock, ContentType)

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

    def doc(self, key):
        return json.loads(self.objs[key][0])


def _turn(i=0, answer="The slab is 200 mm [1]."):
    return {"turn_id": "%032x" % (i + 1), "question": "question %d about the slab" % i,
            "at": NOW + i, "request": {"offer_web": False, "web_intent": False,
                                       "history_len": i},
            "result": {"answer": answer, "citations": []},
            "sources": {"record_keys": ["extractions/ben/2026-10-20/sid%032x" % i],
                        "documents": [], "report_days": []}}


@pytest.fixture
def s3():
    return FakeS3()


def test_new_thread_minted_and_stored(s3):
    out = ask_threads.append_turn(s3, BUCKET, SUB, None, _turn(), SCOPE, NOW)
    assert out["created"] is True and out["rolled_over"] is False
    assert re.fullmatch(r"[0-9a-f]{32}", out["thread_id"])
    key = ask_threads.thread_key(SUB, out["thread_id"])
    doc = s3.doc(key)
    assert doc["v"] == 1 and doc["thread_id"] == out["thread_id"]
    assert doc["scope"] == SCOPE and len(doc["turns"]) == 1
    assert doc["created_at"] == NOW and doc["updated_at"] == NOW
    assert doc["turns"][0] == _turn()


def test_append_to_existing(s3):
    tid = ask_threads.append_turn(s3, BUCKET, SUB, None, _turn(0), SCOPE, NOW)["thread_id"]
    out = ask_threads.append_turn(s3, BUCKET, SUB, tid, _turn(1), {"other": 1}, NOW + 60)
    assert out == {"thread_id": tid, "rolled_over": False, "created": False}
    doc = s3.doc(ask_threads.thread_key(SUB, tid))
    assert [t["turn_id"] for t in doc["turns"]] == [_turn(0)["turn_id"], _turn(1)["turn_id"]]
    # The thread keeps the scope it was opened with.
    assert doc["scope"] == SCOPE and doc["updated_at"] == NOW + 60


def test_missing_supplied_id_mints_new(s3):
    gone = "a" * 32
    out = ask_threads.append_turn(s3, BUCKET, SUB, gone, _turn(), SCOPE, NOW)
    assert out["thread_id"] != gone and out["rolled_over"] is True and out["created"] is True
    assert ask_threads.thread_key(SUB, gone) not in s3.objs
    assert ask_threads.thread_key(SUB, out["thread_id"]) in s3.objs


def test_supplied_id_of_another_caller_is_a_miss(s3):
    tid = ask_threads.append_turn(s3, BUCKET, OTHER, None, _turn(), SCOPE, NOW)["thread_id"]
    out = ask_threads.append_turn(s3, BUCKET, SUB, tid, _turn(1), SCOPE, NOW)
    assert out["thread_id"] != tid and out["rolled_over"] is True
    assert len(s3.doc(ask_threads.thread_key(OTHER, tid))["turns"]) == 1


def test_twenty_turn_rollover(s3):
    tid = ask_threads.append_turn(s3, BUCKET, SUB, None, _turn(0), SCOPE, NOW)["thread_id"]
    for i in range(1, 20):
        assert ask_threads.append_turn(s3, BUCKET, SUB, tid, _turn(i), SCOPE, NOW)["thread_id"] == tid
    out = ask_threads.append_turn(s3, BUCKET, SUB, tid, _turn(20), SCOPE, NOW)
    assert out["thread_id"] != tid and out["rolled_over"] is True and out["created"] is True
    assert len(s3.doc(ask_threads.thread_key(SUB, tid))["turns"]) == 20
    new = s3.doc(ask_threads.thread_key(SUB, out["thread_id"]))
    assert [t["turn_id"] for t in new["turns"]] == [_turn(20)["turn_id"]]


def test_size_cap_truncates(s3):
    big = "x" * (300 * 1024)
    out = ask_threads.append_turn(s3, BUCKET, SUB, None, _turn(answer=big), SCOPE, NOW)
    raw = s3.objs[ask_threads.thread_key(SUB, out["thread_id"])][0]
    assert len(raw) <= ask_threads.MAX_BYTES
    stored = json.loads(raw)["turns"][0]
    assert set(stored["result"]) == {"answer", "truncated"}
    assert stored["result"]["truncated"] is True
    assert stored["result"]["answer"] and big.startswith(stored["result"]["answer"])
    # The manifest is never what gets cut.
    assert stored["sources"] == _turn()["sources"]


def test_size_cap_counts_bytes_not_characters(s3):
    big = "混凝土" * (60 * 1024)        # ~180 K characters, far more bytes once encoded
    out = ask_threads.append_turn(s3, BUCKET, SUB, None, _turn(answer=big), SCOPE, NOW)
    raw = s3.objs[ask_threads.thread_key(SUB, out["thread_id"])][0]
    assert len(raw) <= ask_threads.MAX_BYTES
    assert json.loads(raw)["turns"][0]["result"]["truncated"] is True


def test_full_thread_rolls_over_when_even_a_truncated_turn_cannot_fit(s3):
    tid = ask_threads.append_turn(s3, BUCKET, SUB, None, _turn(0), SCOPE, NOW)["thread_id"]
    key = ask_threads.thread_key(SUB, tid)
    doc = s3.doc(key)
    doc["turns"][0]["question"] = "q" * (ask_threads.MAX_BYTES - 300)
    s3.objs[key] = (json.dumps(doc).encode("utf-8"), s3.clock, "application/json")
    out = ask_threads.append_turn(s3, BUCKET, SUB, tid, _turn(1, answer="a" * 5000), SCOPE, NOW)
    assert out["thread_id"] != tid and out["rolled_over"] is True
    assert len(s3.doc(key)["turns"]) == 1


def test_evict_keeps_five_newest(s3):
    ids = []
    for i in range(7):
        s3.clock = datetime.fromtimestamp(NOW + i, timezone.utc)
        ids.append(ask_threads.append_turn(s3, BUCKET, SUB, None, _turn(i), SCOPE, NOW + i)["thread_id"])
    other = ask_threads.append_turn(s3, BUCKET, OTHER, None, _turn(), SCOPE, NOW)["thread_id"]
    assert ask_threads.evict(s3, BUCKET, SUB) == 2
    left = {k for k in s3.objs if k.startswith(ask_threads.thread_prefix(SUB))}
    assert left == {ask_threads.thread_key(SUB, t) for t in ids[2:]}
    assert ask_threads.thread_key(OTHER, other) in s3.objs
    assert ask_threads.evict(s3, BUCKET, SUB) == 0


def test_list_expires_old_and_sorts(s3):
    s3.clock = datetime.fromtimestamp(NOW - 31 * 86400, timezone.utc)
    old = ask_threads.append_turn(s3, BUCKET, SUB, None, _turn(9), SCOPE,
                                  NOW - 31 * 86400)["thread_id"]
    s3.clock = datetime.fromtimestamp(NOW - 100, timezone.utc)
    a = ask_threads.append_turn(s3, BUCKET, SUB, None, _turn(0), SCOPE, NOW - 100)["thread_id"]
    s3.clock = datetime.fromtimestamp(NOW - 50, timezone.utc)
    long_q = dict(_turn(1), question="Q" * 200)
    b = ask_threads.append_turn(s3, BUCKET, SUB, None, long_q, SCOPE, NOW - 50)["thread_id"]
    ask_threads.append_turn(s3, BUCKET, SUB, b, _turn(2), SCOPE, NOW - 50)

    out = ask_threads.list_threads(s3, BUCKET, SUB, NOW)
    assert [t["thread_id"] for t in out] == [b, a]
    assert out[0] == {"thread_id": b, "title": "Q" * 80, "updated_at": NOW - 50, "turns": 2}
    assert out[1]["title"] == _turn(0)["question"] and out[1]["turns"] == 1
    assert ask_threads.thread_key(SUB, old) not in s3.objs
    assert ask_threads.thread_key(SUB, old) in s3.deletes


def test_list_skips_a_thread_evicted_between_list_and_get(s3):
    a = ask_threads.append_turn(s3, BUCKET, SUB, None, _turn(0), SCOPE, NOW)["thread_id"]
    real_get = s3.get_object

    def racing_get(Bucket, Key):
        if Key == ask_threads.thread_key(SUB, a):
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "x"}}, "GetObject")
        return real_get(Bucket, Key)

    s3.get_object = racing_get
    assert ask_threads.list_threads(s3, BUCKET, SUB, NOW) == []


@pytest.mark.parametrize("op", ["list_objects_v2", "get_object"])
def test_list_raises_on_access_denied(s3, op):
    ask_threads.append_turn(s3, BUCKET, SUB, None, _turn(), SCOPE, NOW)

    def denied(**kw):
        raise ClientError({"Error": {"Code": "AccessDenied", "Message": "x"}}, op)

    setattr(s3, op, denied)
    with pytest.raises(ClientError):
        ask_threads.list_threads(s3, BUCKET, SUB, NOW)


def test_append_raises_on_access_denied_instead_of_minting(s3):
    def denied(**kw):
        raise ClientError({"Error": {"Code": "AccessDenied", "Message": "x"}}, "GetObject")

    s3.get_object = denied
    with pytest.raises(ClientError):
        ask_threads.append_turn(s3, BUCKET, SUB, "b" * 32, _turn(), SCOPE, NOW)
    assert s3.puts == []


def test_get_thread_and_delete(s3):
    tid = ask_threads.append_turn(s3, BUCKET, SUB, None, _turn(), SCOPE, NOW)["thread_id"]
    assert ask_threads.get_thread(s3, BUCKET, SUB, tid, now=NOW)["thread_id"] == tid
    assert ask_threads.get_thread(s3, BUCKET, OTHER, tid, now=NOW) is None
    assert ask_threads.get_thread(s3, BUCKET, SUB, "c" * 32, now=NOW) is None
    for bad in ("../x", "A" * 32, "a" * 31, None):
        assert ask_threads.get_thread(s3, BUCKET, SUB, bad, now=NOW) is None
    # Expired 30 days after the last turn, whether or not the object is still there.
    assert ask_threads.get_thread(s3, BUCKET, SUB, tid, now=NOW + 31 * 86400) is None
    ask_threads.delete_thread(s3, BUCKET, SUB, tid)
    ask_threads.delete_thread(s3, BUCKET, SUB, tid)
    assert ask_threads.get_thread(s3, BUCKET, SUB, tid, now=NOW) is None
    ask_threads.delete_thread(s3, BUCKET, SUB, "../x")
    assert all(k.startswith(ask_threads.thread_prefix(SUB)) for k in s3.deletes)


def test_prefix_is_16_hex_and_not_sub_hash():
    p = ask_threads.thread_prefix(SUB)
    seg = avt.owner_prefix(SUB)[len(avt.PREFIX):]
    assert p == "ask_threads/" + seg
    assert re.fullmatch(r"ask_threads/[0-9a-f]{16}/", p)
    assert p[len("ask_threads/"):-1] != avt.sub_hash(SUB)
    assert ask_threads.thread_prefix(OTHER) != p
    assert ask_threads.thread_key(SUB, "d" * 32) == p + "d" * 32 + ".json"


def test_new_thread_id_shape():
    a, b = ask_threads.new_thread_id(), ask_threads.new_thread_id()
    assert ask_threads.THREAD_RE.match(a) and a != b


def test_enabled_reads_env_per_call(monkeypatch):
    monkeypatch.delenv("ASK_THREADS", raising=False)
    assert ask_threads.enabled() is False
    for v in ("TRUE", "1", "yes", ""):
        monkeypatch.setenv("ASK_THREADS", v)
        assert ask_threads.enabled() is False
    monkeypatch.setenv("ASK_THREADS", "true")
    assert ask_threads.enabled() is True


def test_concurrent_appends_stay_valid(s3):
    tid = ask_threads.append_turn(s3, BUCKET, SUB, None, _turn(0), SCOPE, NOW)["thread_id"]
    key = ask_threads.thread_key(SUB, tid)
    stale = s3.objs[key][0]
    real_get = s3.get_object

    def stale_get(Bucket, Key):
        if Key == key:
            return {"Body": io.BytesIO(stale)}
        return real_get(Bucket, Key)

    s3.get_object = stale_get
    ask_threads.append_turn(s3, BUCKET, SUB, tid, _turn(1), SCOPE, NOW + 1)
    ask_threads.append_turn(s3, BUCKET, SUB, tid, _turn(2), SCOPE, NOW + 2)
    doc = json.loads(s3.objs[key][0])
    assert doc["thread_id"] == tid and len(doc["turns"]) >= 2
    assert all(isinstance(t, dict) and t.get("turn_id") for t in doc["turns"])


def test_store_logs_no_text(s3, caplog):
    caplog.set_level("DEBUG")
    tid = ask_threads.append_turn(s3, BUCKET, SUB, None, _turn(), SCOPE, NOW)["thread_id"]
    ask_threads.list_threads(s3, BUCKET, SUB, NOW)
    ask_threads.evict(s3, BUCKET, SUB)
    ask_threads.get_thread(s3, BUCKET, SUB, tid, now=NOW)
    text = caplog.text
    assert "slab" not in text and "extractions/" not in text and SUB not in text
