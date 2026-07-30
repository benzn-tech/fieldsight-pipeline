"""Rolling Tier-1 summary (voice-timeliness Point 1): every ~1-2 min the LLM
re-summarizes the session-so-far into a short running summary + open to-dos,
stored as the meeting's short-term memory (S3), polled by the mobile app mid-
meeting and read by the Tier-2 finalize. This tests the pure core (prompt build
+ response parse + summarize with the LLM injected) AND the S3-triggered handler
(gather / assemble collaborators, the LLM, and the S3 client all injected). The
pure core is pure at import; the handler reuses extract_session, which reads AWS
creds + ANTHROPIC_API_KEY at import, so set dummies before importing it."""
import io
import json
import os
from datetime import datetime, timedelta

import pytest

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy-key")

import lambda_rolling_summary as rs

ex = pytest.importorskip("lambda_extract_session")


def turn(spk, text, hhmmss="10:00:00"):
    return {"speaker": spk, "text": text, "abs_start_str": hhmmss}


# ---- parse_rolling_summary ----------------------------------------------

def test_parse_valid_json():
    raw = '{"summary": "Discussed the slab pour.", "open_todos": [{"text": "Fix rebar", "responsible": "Neil"}]}'
    out = rs.parse_rolling_summary(raw)
    assert out["summary"] == "Discussed the slab pour."
    assert out["open_todos"] == [{"text": "Fix rebar", "responsible": "Neil"}]


def test_parse_strips_markdown_fences():
    raw = '```json\n{"summary": "S", "open_todos": []}\n```'
    assert rs.parse_rolling_summary(raw) == {"summary": "S", "open_todos": []}


def test_parse_normalizes_string_and_missing_responsible_todos():
    raw = '{"summary": "S", "open_todos": ["call supplier", {"text": "order steel"}]}'
    out = rs.parse_rolling_summary(raw)
    assert out["open_todos"] == [
        {"text": "call supplier", "responsible": None},
        {"text": "order steel", "responsible": None},
    ]


def test_parse_extracts_json_embedded_in_prose():
    raw = 'Sure — here it is: {"summary":"S","open_todos":[]} hope that helps.'
    assert rs.parse_rolling_summary(raw)["summary"] == "S"


def test_parse_returns_none_on_garbage_or_non_object():
    assert rs.parse_rolling_summary("not json at all") is None
    assert rs.parse_rolling_summary("") is None
    assert rs.parse_rolling_summary(None) is None
    assert rs.parse_rolling_summary("[1, 2, 3]") is None   # a list, not a summary object


# ---- build_rolling_prompt -----------------------------------------------

def test_prompt_includes_transcript_lines_and_the_json_schema():
    p = rs.build_rolling_prompt([turn("spk_0", "pour the slab", "10:01:02")])
    assert "pour the slab" in p
    assert "10:01:02" in p
    assert "open_todos" in p and "summary" in p   # asks for the structured shape


# ---- summarize_turns (LLM injected) -------------------------------------

def test_summarize_calls_llm_once_and_parses():
    calls = []

    def fake_llm(prompt, **kw):
        calls.append(prompt)
        return ('{"summary":"running so far","open_todos":[{"text":"x","responsible":null}]}', None)

    out = rs.summarize_turns([turn("spk_0", "hi")], call_llm=fake_llm)
    assert out == {"summary": "running so far", "open_todos": [{"text": "x", "responsible": None}]}
    assert len(calls) == 1


def test_summarize_empty_turns_returns_none_without_calling_llm():
    called = []
    out = rs.summarize_turns([], call_llm=lambda *a, **k: (called.append(1), ("", None))[1])
    assert out is None and called == []


def test_summarize_llm_failure_returns_none():
    out = rs.summarize_turns([turn("spk_0", "hi")], call_llm=lambda *a, **k: (None, "boom"))
    assert out is None


# ---- process_transcript_key + lambda_handler (S3-triggered) -------------
#
# On a new transcript chunk the handler rebuilds the whole session-so-far
# (reusing extract_session's gather + assemble_deduped_turns), summarises it, and
# writes session_rolling/{folder}/{date}/{session}/latest.json — UNLESS it
# summarised this session < MIN_RESUMMARY_INTERVAL_S ago (cadence/cost throttle).
# Collaborators, the LLM, and the S3 client are injected — no real S3.

TRANSCRIPT_KEY = "transcripts/Ada_L/2026-07-25/Benl1_2026-07-25_13-00-11_sidABC_c0000.json"
ROLLING_OUT_KEY = "session_rolling/Ada_L/2026-07-25/sidABC/latest.json"
NOW = datetime(2026, 7, 25, 13, 2, 0)


class _FakeS3:
    """Minimal S3 for the handler: get_object serves a preset latest.json body
    (or raises 'no summary yet'); put_object records the writes."""

    def __init__(self, existing=None):
        self._existing = existing            # dict already at ROLLING_OUT_KEY, or None
        self.puts = []

    def get_object(self, Bucket, Key):
        if self._existing is None:
            raise Exception("NoSuchKey")     # nothing summarised yet -> not throttled
        return {"Body": io.BytesIO(json.dumps(self._existing).encode("utf-8"))}

    def put_object(self, **kw):
        self.puts.append(kw)


def _wire_session(monkeypatch, turns):
    monkeypatch.setattr(ex, "S3_BUCKET", "test-bucket")
    monkeypatch.setattr(ex, "session_base_from_key", lambda k: ("Ada_L", "2026-07-25", "sidABC"))
    monkeypatch.setattr(ex, "gather_session_segments", lambda b, u, d, s: [TRANSCRIPT_KEY])
    monkeypatch.setattr(ex, "assemble_deduped_turns", lambda b, keys: (turns, ["x"]))


def _summary_llm(prompt, **kw):
    return ('{"summary":"pouring the slab","open_todos":'
            '[{"text":"order steel","responsible":"Neil"}]}', None)


def test_handler_writes_rolling_summary_to_the_session_key(monkeypatch):
    _wire_session(monkeypatch, [turn("spk_0", "pour the slab", "13:00:12")])
    s3c = _FakeS3()                          # no prior summary -> not throttled -> writes
    out = rs.process_transcript_key(TRANSCRIPT_KEY, s3_client=s3c, call_llm=_summary_llm, now=NOW)
    assert out == ROLLING_OUT_KEY
    assert len(s3c.puts) == 1
    put = s3c.puts[0]
    assert put["Bucket"] == "test-bucket"
    assert put["Key"] == ROLLING_OUT_KEY
    body = json.loads(put["Body"])
    assert body["summary"] == "pouring the slab"
    assert body["open_todos"] == [{"text": "order steel", "responsible": "Neil"}]
    assert body["session_base"] == "sidABC"
    assert body["turn_count"] == 1
    assert body["updated_at"] == "2026-07-25T13:02:00Z"


def test_handler_skips_non_transcript_keys(monkeypatch):
    monkeypatch.setattr(ex, "session_base_from_key", lambda k: None)
    s3c = _FakeS3()
    out = rs.process_transcript_key("extractions/Ada_L/2026-07-25/sidABC.json",
                                    s3_client=s3c, call_llm=lambda *a, **k: ("", None))
    assert out is None and s3c.puts == []


def test_handler_writes_nothing_for_an_empty_session(monkeypatch):
    _wire_session(monkeypatch, [])           # no turns -> summarize returns None -> no write
    s3c = _FakeS3()
    out = rs.process_transcript_key(TRANSCRIPT_KEY, s3_client=s3c,
                                    call_llm=lambda *a, **k: ("", None), now=NOW)
    assert out is None and s3c.puts == []


def test_handler_throttles_when_summarised_recently(monkeypatch):
    # latest.json was written 30s ago (< 75s): a chunk landing now must NOT
    # re-summarise (cost + the user's 1-2 min cadence; chunks arrive ~30s apart).
    # The LLM raises if called, proving the throttle short-circuits before it.
    _wire_session(monkeypatch, [turn("spk_0", "pour the slab")])
    s3c = _FakeS3(existing={"updated_at": (NOW - timedelta(seconds=30)).isoformat() + "Z"})

    def _boom(*a, **k):
        raise AssertionError("LLM must not be called when throttled")

    out = rs.process_transcript_key(TRANSCRIPT_KEY, s3_client=s3c, call_llm=_boom, now=NOW)
    assert out is None and s3c.puts == []


def test_handler_resummarises_when_last_summary_is_stale(monkeypatch):
    # latest.json is 5 min old (>= 75s): a new chunk re-summarises.
    _wire_session(monkeypatch, [turn("spk_0", "pour the slab", "13:00:12")])
    s3c = _FakeS3(existing={"updated_at": (NOW - timedelta(seconds=300)).isoformat() + "Z"})
    out = rs.process_transcript_key(TRANSCRIPT_KEY, s3_client=s3c, call_llm=_summary_llm, now=NOW)
    assert out == ROLLING_OUT_KEY and len(s3c.puts) == 1


def test_lambda_handler_unquotes_keys_and_collects_writes(monkeypatch):
    seen = []
    monkeypatch.setattr(rs, "process_transcript_key",
                        lambda key: (seen.append(key), f"out::{key}")[1])
    event = {"Records": [{"s3": {"object": {"key": "transcripts/Ada_L/2026-07-25/a+b.json"}}}]}
    out = rs.lambda_handler(event, None)
    assert seen == ["transcripts/Ada_L/2026-07-25/a b.json"]   # unquote_plus turns + into space
    assert out["written"] == ["out::transcripts/Ada_L/2026-07-25/a b.json"]


def test_lambda_handler_reads_the_eventbridge_object_created_shape(monkeypatch):
    # RollingSummaryFunction is wired to EventBridge (not an S3 notification), so its
    # event is {detail:{object:{key}}} with the key already decoded. Regression guard:
    # the handler used to read only `Records`, so every real EventBridge invocation
    # found no key and did nothing (1-2 ms no-op, no summary ever written).
    seen = []
    monkeypatch.setattr(rs, "process_transcript_key",
                        lambda key: (seen.append(key), f"out::{key}")[1])
    event = {"source": "aws.s3", "detail-type": "Object Created",
             "detail": {"bucket": {"name": "b"},
                        "object": {"key": "transcripts/Ada_L/2026-07-25/x_sidABC_c0000.json"}}}
    out = rs.lambda_handler(event, None)
    assert seen == ["transcripts/Ada_L/2026-07-25/x_sidABC_c0000.json"]
    assert out["written"] == ["out::transcripts/Ada_L/2026-07-25/x_sidABC_c0000.json"]
