"""Cross-session status reconcile R0, Task 5: the worker inside MatcherFunction.

process_request reads THIS session's transcript (extract_session's own turn helpers), asks the
model which of the offered open items it reports a status for, and hands the hits to the pure
judge. It fails closed: a model error or an unparseable answer raises so the S3 event retries;
zero hits is an ordinary, empty result.

The turn helpers are faked on the lambda_extract_session module object (they are imported
lazily inside process_request), so these tests never touch S3.
"""
import json
import os
import subprocess
import sys

import pytest

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy-key")
os.environ.setdefault("DASHSCOPE_API_KEY", "dashscope-test-dummy-key")

les = pytest.importorskip("lambda_extract_session", reason="requires boto3 (installed in CI)")
import reconcile_worker as rw  # noqa: E402

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                   "src")
BUCKET = "lake-bucket"
KEY = "reconcile_requests/site-1/2026-11-10/0123456789abcdef.json"
SEG = "transcripts/Ben/2026-11-10/F2SP_20261110_100000_sidabc.json"
QUOTE = "the west block scaffold is finished now"


def _req(**over):
    r = {
        "site_id": "site-1", "date": "2026-11-10", "user_folder": "Ben",
        "session_base": "sidabc", "extraction_key": "extractions/Ben/2026-11-10/sidabc.json",
        "extracted_at": "2026-11-10T10:30:00Z", "rung": "R0",
        "candidates": [
            {"kind": "action_item", "stable_id": "11111111-1111-1111-1111-111111111111",
             "text": "Finish west block scaffold", "status": "open", "responsible": None,
             "deadline": None, "first_seen_date": "2026-11-10", "recorded_by": "Ben",
             "score": 1.0, "matched_terms": [], "place": None},
            {"kind": "action_item", "stable_id": "22222222-2222-2222-2222-222222222222",
             "text": "Order timber for decking", "status": "open", "responsible": None,
             "deadline": None, "first_seen_date": "2026-11-10", "recorded_by": "Ben",
             "score": 0.5, "matched_terms": [], "place": None},
        ],
    }
    r.update(over)
    return r


TURNS = [{"abs_start_str": "10:00:05", "speaker": "Ben", "text": QUOTE,
          "source_filename": "F2SP_20261110_100000_sidabc.json"}]


@pytest.fixture
def faked(monkeypatch):
    """Fakes the three turn helpers; records what they were called with."""
    calls = {}

    def gather(bucket, user_folder, date, session_base):
        calls["gather"] = (bucket, user_folder, date, session_base)
        return [SEG]

    def assemble(bucket, keys):
        calls["assemble"] = (bucket, list(keys))
        return list(TURNS), ["F2SP_20261110_100000_sidabc.json"], {}

    def check(quote, at, turns, date, user_folder):
        calls.setdefault("check", []).append((quote, at, date, user_folder))
        return {"status": "verified", "segment_key": "audio_segments/Ben/2026-11-10/x.wav",
                "offset_sec": 5.0, "quote": quote}

    monkeypatch.setattr(les, "gather_session_segments", gather)
    monkeypatch.setattr(les, "assemble_session_turns", assemble)
    monkeypatch.setattr(les, "_check_status_quote", check)
    return calls


def _call_returning(raw, error=None, sink=None):
    def call(prompt, **kw):
        if sink is not None:
            sink.append((prompt, kw))
        return raw, error
    return call


def test_import_graph_has_no_psycopg():
    env = dict(os.environ, PYTHONPATH=SRC, AWS_DEFAULT_REGION="ap-southeast-2",
               AWS_ACCESS_KEY_ID="x", AWS_SECRET_ACCESS_KEY="x",
               ANTHROPIC_API_KEY="sk-ant-x", DASHSCOPE_API_KEY="x")
    code = ("import sys, reconcile_worker, lambda_extract_session, session_brief\n"
            "print(sorted(m for m in sys.modules if m.startswith('psycopg')))\n")
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                         text=True, cwd=SRC)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]"


def test_process_request_happy_path(faked):
    sent = []
    raw = json.dumps({"hits": [{"candidate": "C1", "status": "done", "at": "10:00:05",
                                "quote": QUOTE}]})
    out = rw.process_request(_req(), KEY, call=_call_returning(raw, sink=sent), bucket=BUCKET)

    assert out["pick_cards"] == []
    assert out["status_suggestions"] == [{
        "stable_id": "11111111-1111-1111-1111-111111111111", "site_id": "site-1",
        "suggested_status": "done", "quote": QUOTE,
        "segment_key": "audio_segments/Ben/2026-11-10/x.wav", "offset_sec": 5.0,
        "run": "reconcile:R0:2026-11-10T10:30:00Z"}]
    assert out["counts"]["kept"] == 1

    # The session's own transcript, from the matcher's bucket.
    assert faked["gather"] == (BUCKET, "Ben", "2026-11-10", "sidabc")
    assert faked["assemble"] == (BUCKET, [SEG])
    assert faked["check"] == [(QUOTE, "10:00:05", "2026-11-10", "Ben")]

    # One call, with the brief's exact arguments; the prompt carries the rendered lines and
    # aliases, never a stable_id.
    assert len(sent) == 1
    prompt, kw = sent[0]
    assert kw == {"max_tokens": 4000, "force_json": True, "enable_thinking": True,
                  "deadline": None, "caller": "status_reconcile"}
    assert f"[10:00:05] Ben: {QUOTE}" in prompt
    assert "C1: Finish west block scaffold" in prompt
    assert "11111111" not in prompt


def test_pick_card_carries_site_and_run(faked):
    req = _req(candidates=[
        dict(_req()["candidates"][0], text="Finish scaffold west"),
        dict(_req()["candidates"][1], text="Finish scaffold east",
             stable_id="33333333-3333-3333-3333-333333333333"),
    ])
    raw = json.dumps({"hits": [{"candidate": "C1", "status": "done", "at": "10:00:05",
                                "quote": "the scaffold is finished"}]})
    out = rw.process_request(req, KEY, call=_call_returning(raw), bucket=BUCKET)
    assert out["status_suggestions"] == []
    assert len(out["pick_cards"]) == 1
    card = out["pick_cards"][0]
    assert card["site_id"] == "site-1"
    assert card["run"] == "reconcile:R0:2026-11-10T10:30:00Z"
    assert set(card["candidates"]) == {"11111111-1111-1111-1111-111111111111",
                                       "33333333-3333-3333-3333-333333333333"}
    assert set(card) == {"site_id", "suggested_status", "quote", "segment_key", "offset_sec",
                         "candidates", "run"}


def test_llm_error_raises(faked):
    with pytest.raises(RuntimeError):
        rw.process_request(_req(), KEY, call=_call_returning(None, "boom"), bucket=BUCKET)


def test_unparseable_raises(faked):
    with pytest.raises(RuntimeError):
        rw.process_request(_req(), KEY, call=_call_returning("not json at all"), bucket=BUCKET)


def test_unparseable_error_message_carries_no_answer_text(faked):
    secret = "the secret scaffold words"
    with pytest.raises(RuntimeError) as exc:
        rw.process_request(_req(), KEY, call=_call_returning(secret), bucket=BUCKET)
    assert secret not in str(exc.value)


def test_fenced_json_is_accepted(faked):
    raw = "```json\n" + json.dumps({"hits": []}) + "\n```"
    out = rw.process_request(_req(), KEY, call=_call_returning(raw), bucket=BUCKET)
    assert out["status_suggestions"] == [] and out["pick_cards"] == []


def test_zero_hits_returns_empty(faked):
    out = rw.process_request(_req(), KEY, call=_call_returning(json.dumps({"hits": []})),
                             bucket=BUCKET)
    assert out["status_suggestions"] == []
    assert out["pick_cards"] == []
    assert out["counts"]["offered"] == 0


def test_no_candidates_makes_no_call(faked):
    def call(*a, **k):
        raise AssertionError("no model call without candidates")
    out = rw.process_request(_req(candidates=[]), KEY, call=call, bucket=BUCKET)
    assert out["status_suggestions"] == [] and out["pick_cards"] == []


def test_no_transcript_keys_returns_empty_without_a_call(faked, monkeypatch):
    monkeypatch.setattr(les, "gather_session_segments", lambda *a: [])

    def call(*a, **k):
        raise AssertionError("no model call without a transcript")
    out = rw.process_request(_req(), KEY, call=call, bucket=BUCKET)
    assert out["status_suggestions"] == [] and out["pick_cards"] == []
    assert out["counts"].get("no_transcript") == 1


def test_keys_but_no_turns_raises(faked, monkeypatch):
    """Keys listed but every read came back empty: assemble_session_turns swallows a denied
    GetObject, so this is more likely a grant problem than silence. Fail closed."""
    monkeypatch.setattr(les, "assemble_session_turns", lambda bucket, keys: ([], [], {}))

    def call(*a, **k):
        raise AssertionError("no model call over an empty transcript")
    with pytest.raises(RuntimeError):
        rw.process_request(_req(), KEY, call=call, bucket=BUCKET)


def test_logs_carry_counts_not_text(faked, caplog):
    raw = json.dumps({"hits": [{"candidate": "C1", "status": "done", "at": "10:00:05",
                                "quote": QUOTE}]})
    with caplog.at_level("DEBUG"):
        rw.process_request(_req(), KEY, call=_call_returning(raw), bucket=BUCKET)
    text = caplog.text
    assert QUOTE not in text
    assert "scaffold" not in text.lower()
    assert "decking" not in text.lower()


def test_prefix_matches_the_emitter():
    rr = pytest.importorskip("reconcile_request", reason="requires psycopg (installed in CI)")
    assert rw.PREFIX == rr.PREFIX


def test_deadline_reaches_the_call(faked):
    sent = []
    rw.process_request(_req(), KEY, call=_call_returning(json.dumps({"hits": []}), sink=sent),
                       bucket=BUCKET, deadline_s=123.0)
    assert sent[0][1]["deadline"] == 123.0


def test_non_positive_deadline_raises_without_calling(faked):
    def call(*a, **k):
        raise AssertionError("no model call without time left")
    for d in (0, -5.0):
        with pytest.raises(RuntimeError):
            rw.process_request(_req(), KEY, call=call, bucket=BUCKET, deadline_s=d)
