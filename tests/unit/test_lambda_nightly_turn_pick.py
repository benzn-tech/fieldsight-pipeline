"""Nightly re-match J2: the non-VPC worker (lambda_nightly_turn_pick) and the session_turns lift.

The worker reads a request artifact, rebuilds THIS session's turns with the extractor's own
chain (session_turns), makes one model call, parses and guards the answer, and invokes the
in-VPC writer. Fakes only: an Anthropic-shaped client, an S3 client and a Lambda client --
no network. All text here is synthetic.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import types

import pytest

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy-key")
os.environ.setdefault("DASHSCOPE_API_KEY", "dashscope-test-dummy-key")

pytest.importorskip("boto3", reason="requires boto3 (installed in CI)")
import model_registry  # noqa: E402
import nightly_turn_pick as ntp  # noqa: E402
import session_turns  # noqa: E402
import lambda_nightly_turn_pick as worker  # noqa: E402

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                   "src")
BUCKET = "lake-bucket"
FOLDER, DATE = "NightlyF", "2026-11-09"
SID = "a" * 32
OTHER_SID = "b" * 32
BASE = f"sid{SID}"
KEY = f"nightly_requests/site-1/{DATE}/0123456789abcdef.json"
SEG = f"transcripts/{FOLDER}/{DATE}/{FOLDER}_{DATE}_10-00-00_sid{SID}_c0000.json"
OTHER_SEG = f"transcripts/{FOLDER}/{DATE}/{FOLDER}_{DATE}_10-01-00_sid{OTHER_SID}_c0000.json"
LEGACY_SEG = f"transcripts/{FOLDER}/{DATE}/{FOLDER}_{DATE}_10-02-00_off0.0_to30.0_srcwav.json"
OWN_TEXT = "pour the slab at grid four tomorrow morning"
OTHER_TEXT = "zebracrossing otherworldly marmalade"
LEGACY_TEXT = "quixotic legacy porcupine"
ORIGIN_TEXT = "secretoriginquote never leaves"
MODEL = model_registry.get("nightly_turn_pick")["model"]


def _transcribe_json(pairs):
    items, t = [], 0.0
    for spk, text in pairs:
        for w in text.split():
            items.append({"type": "pronunciation", "start_time": f"{t:.3f}",
                          "end_time": f"{t + 0.4:.3f}", "speaker_label": spk,
                          "alternatives": [{"content": w, "confidence": "0.9"}]})
            t += 0.5
    return {"results": {"transcripts": [{"transcript": " ".join(p[1] for p in pairs)}],
                        "items": items}}


class FakeS3:
    def __init__(self, objects):
        self.objects = dict(objects)
        self.gets = []

    def get_object(self, Bucket, Key):
        self.gets.append(Key)
        if Key not in self.objects:
            raise KeyError(Key)
        return {"Body": io.BytesIO(self.objects[Key].encode("utf-8"))}

    def get_paginator(self, op):
        objects = self.objects

        class P:
            def paginate(self, Bucket, Prefix):
                yield {"Contents": [{"Key": k} for k in sorted(objects) if k.startswith(Prefix)]}
        return P()


class FakeLambda:
    def __init__(self, function_error=None, result=None):
        self.calls = []
        self.function_error = function_error
        self.result = result or {"written": 1, "records": 1,
                                 "skipped": {"gone": 0, "linked": 0, "moved_site": 0}}

    def invoke(self, **kw):
        self.calls.append(kw)
        resp = {"StatusCode": 200,
                "Payload": io.BytesIO(json.dumps(self.result).encode("utf-8"))}
        if self.function_error:
            resp["FunctionError"] = self.function_error
        return resp


class FakeMessages:
    def __init__(self, text, stop_reason="end_turn"):
        self.text, self.stop_reason, self.calls = text, stop_reason, []

    def create(self, **kw):
        self.calls.append(kw)
        return types.SimpleNamespace(
            content=[types.SimpleNamespace(type="thinking", thinking=""),
                     types.SimpleNamespace(type="text", text=self.text)],
            stop_reason=self.stop_reason,
            usage=types.SimpleNamespace(input_tokens=1500, output_tokens=200))


class FakeClient:
    def __init__(self, text, stop_reason="end_turn"):
        self.messages = FakeMessages(text, stop_reason)


def _request(**over):
    r = {"site_id": "site-1", "night": DATE, "run_id": "11111111-1111-1111-1111-111111111111",
         "flag": "on", "extraction_key": f"extractions/{FOLDER}/{DATE}/{BASE}.json",
         "session_base": BASE, "user_folder": FOLDER, "date": DATE,
         "items": [{"stable_id": "22222222-2222-2222-2222-222222222222",
                    "row_id": "33333333-3333-3333-3333-333333333333", "kind": "action_item",
                    "text": "pour slab grid four", "topic_index": 0,
                    "time_range": "10:00 – 10:05",
                    "origin": {"quote": ORIGIN_TEXT}}]}
    r.update(over)
    return r


def _s3(request=None):
    return FakeS3({
        KEY: json.dumps(request or _request()),
        SEG: json.dumps(_transcribe_json([("spk_0", OWN_TEXT)])),
        OTHER_SEG: json.dumps(_transcribe_json([("spk_1", OTHER_TEXT)])),
        LEGACY_SEG: json.dumps(_transcribe_json([("spk_2", LEGACY_TEXT)])),
    })


ANSWER = json.dumps({"picks": [{"item": "I1", "turn": "T1"}]})


def _run(s3=None, client=None, lam=None, request=None):
    s3 = s3 or _s3(request)
    client = client or FakeClient(ANSWER)
    lam = lam or FakeLambda()
    out = worker.process_artifact(BUCKET, KEY, s3_client=s3, lambda_client=lam,
                                  model_client=client)
    return out, s3, client, lam


# ---------------------------------------------------------------------------------------
# The brief's worker tests
# ---------------------------------------------------------------------------------------

def test_worker_fails_closed_on_parse_error():
    lam = FakeLambda()
    with pytest.raises(RuntimeError, match="parse"):
        _run(client=FakeClient("this is not json"), lam=lam)
    assert lam.calls == []


def test_worker_raises_on_writer_function_error():
    with pytest.raises(RuntimeError, match="FunctionError"):
        _run(lam=FakeLambda(function_error="Unhandled"))


def test_worker_sends_no_origin_or_other_session_text():
    out, s3, client, lam = _run()
    [call] = client.messages.calls
    prompt = call["messages"][0]["content"]
    assert "pour the slab at grid four" in prompt          # this session's turn
    assert "pour slab grid four" in prompt                  # the item text may leave
    for leak in (ORIGIN_TEXT, "secretoriginquote", OTHER_TEXT, "zebracrossing",
                 LEGACY_TEXT, "porcupine", "origin"):
        assert leak not in prompt
    # Only this session's transcript object was read.
    assert OTHER_SEG not in s3.gets and LEGACY_SEG not in s3.gets
    # The writer payload carries no text at all.
    payload = lam.calls[0]["Payload"]
    for leak in (OWN_TEXT, "pour slab grid four", ORIGIN_TEXT, OTHER_TEXT):
        assert leak not in payload


# ---------------------------------------------------------------------------------------
# Worker contract details
# ---------------------------------------------------------------------------------------

def test_worker_one_call_with_registry_params_and_writer_payload():
    out, s3, client, lam = _run()
    entry = model_registry.get("nightly_turn_pick")
    [call] = client.messages.calls
    assert call["model"] == entry["model"] and call["max_tokens"] == entry["max_tokens"]
    assert call["output_config"] == {"effort": entry["effort"]}
    assert "temperature" not in call and "thinking" not in call

    [inv] = lam.calls
    assert inv["InvocationType"] == "RequestResponse"
    p = json.loads(inv["Payload"])
    assert {k for k in p} == {"site_id", "run_id", "extraction_key", "session_base",
                              "question_set", "input_key", "rows", "usage"}
    assert p["site_id"] == "site-1" and p["run_id"] == _request()["run_id"]
    assert p["question_set"] == ntp.PROMPT_HASH and p["input_key"] == KEY
    assert p["session_base"] == BASE
    assert p["usage"] == {"tokens_in": 1500, "tokens_out": 200, "model": MODEL,
                          "cost_usd": None}
    [row] = p["rows"]
    assert row["stable_id"] == _request()["items"][0]["stable_id"]
    assert row["row_id"] == _request()["items"][0]["row_id"]
    assert row["turn"] == "T1" and row["outcome"] in ("accepted", "rejected")
    assert row["locator"]["source_stem"] and row["locator"]["start_sec"] is not None
    assert "text" not in row and "turn_index" not in row
    assert out["writer"]["written"] == 1


def test_worker_idle_unless_flag_on():
    lam, client = FakeLambda(), FakeClient(ANSWER)
    out, s3, _, _ = _run(client=client, lam=lam, request=_request(flag="shadow"))
    assert client.messages.calls == [] and lam.calls == []
    assert s3.gets == [KEY]
    assert out["idle"] == 1


@pytest.mark.parametrize("stop", ["max_tokens", "refusal"])
def test_worker_raises_on_truncated_or_refused_answer(stop):
    lam = FakeLambda()
    with pytest.raises(RuntimeError, match=stop):
        _run(client=FakeClient(ANSWER, stop_reason=stop), lam=lam)
    assert lam.calls == []


def test_worker_no_windowed_items_makes_no_call():
    req = _request()
    req["items"][0]["time_range"] = "not a range"
    lam, client = FakeLambda(), FakeClient(ANSWER)
    out, *_ = _run(client=client, lam=lam, request=req)
    assert client.messages.calls == [] and lam.calls == []
    assert out["skipped"]["no_window"] == 1


def test_worker_listed_segments_but_no_turns_fails_closed():
    s3 = _s3()
    s3.objects[SEG] = "{not json"
    with pytest.raises(RuntimeError, match="turns"):
        _run(s3=s3)


def test_worker_group_session_makes_no_call():
    lam, client = FakeLambda(), FakeClient(ANSWER)
    out, *_ = _run(client=client, lam=lam, request=_request(session_base="grp" + "c" * 32))
    assert client.messages.calls == [] and lam.calls == []
    assert out["unsupported_session"] == 1


def test_make_client_reads_key_and_disables_sdk_retries(monkeypatch):
    seen = {}

    class Anthropic:
        def __init__(self, **kw):
            seen.update(kw)

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=Anthropic))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-unit")
    worker.make_model_client()
    assert seen["api_key"] == "sk-ant-unit" and seen["max_retries"] == 0
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        worker.make_model_client()


def test_handler_reads_the_s3_event_key(monkeypatch):
    seen = []
    monkeypatch.setattr(worker, "process_artifact",
                        lambda bucket, key, **kw: seen.append((bucket, key)) or {})
    worker.lambda_handler({"Records": [{"s3": {"bucket": {"name": BUCKET},
                                               "object": {"key": KEY.replace("/", "%2F", 0)}}}]},
                          None)
    assert seen == [(BUCKET, KEY)]


# ---------------------------------------------------------------------------------------
# The lift
# ---------------------------------------------------------------------------------------

def test_extractor_still_imports_assemble_session_turns(monkeypatch):
    les = pytest.importorskip("lambda_extract_session")
    assert callable(les.assemble_session_turns) and callable(les.gather_session_segments)
    # The extractor delegates to session_turns, passing its own (patchable) S3 client.
    fake = object()
    seen = {}
    monkeypatch.setattr(les, "s3", lambda: fake)
    monkeypatch.setattr(session_turns, "assemble_session_turns",
                        lambda bucket, keys, s3_client=None: seen.update(
                            a=(bucket, list(keys), s3_client)) or ([], [], {}))
    monkeypatch.setattr(session_turns, "gather_session_segments",
                        lambda bucket, u, d, b, s3_client=None: seen.update(
                            g=(bucket, u, d, b, s3_client)) or [])
    les.assemble_session_turns("b", ["k"])
    les.gather_session_segments("b", "u", "d", "base")
    assert seen == {"a": ("b", ["k"], fake), "g": ("b", "u", "d", "base", fake)}
    # The chain's helpers are session_turns' own objects, re-exported.
    for name in ("session_base_from_key", "filter_device_announcements",
                 "filter_audio_event_tags", "_dedup_turn_boundaries",
                 "_dedup_batch_window_repeats", "_is_empty_transcript",
                 "is_device_announcement", "_user_and_date_from_key"):
        assert getattr(les, name) is getattr(session_turns, name), name


def test_import_graph_has_no_psycopg():
    env = dict(os.environ, PYTHONPATH=SRC, AWS_DEFAULT_REGION="ap-southeast-2",
               AWS_ACCESS_KEY_ID="x", AWS_SECRET_ACCESS_KEY="x",
               ANTHROPIC_API_KEY="sk-ant-x", DASHSCOPE_API_KEY="x")
    code = ("import sys, lambda_nightly_turn_pick, session_turns, model_registry\n"
            "print(sorted(m for m in sys.modules if m.startswith('psycopg')"
            " or m == 'anthropic'))\n")
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                         text=True, cwd=SRC)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]"
