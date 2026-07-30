"""Rolling Tier-1 summary (voice-timeliness Point 1): every ~1-2 min the LLM
re-summarizes the session-so-far into a short running summary + open to-dos,
stored as the meeting's short-term memory (S3), polled by the mobile app mid-
meeting and read by the Tier-2 finalize. This tests the pure core (prompt build
+ response parse + summarize with the LLM injected); the S3-triggered handler +
wiring land separately. Pure at import (boto3/llm_utils are lazy)."""
import lambda_rolling_summary as rs


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
