"""Unit: the prompt limits are sized to the model in front of them, not to a
model three vendor changes ago.

Every number touched here was correct when it was written and silently wrong
afterwards. `60000` chars of daily transcript was sized for a 200K-token Claude
context; on 2026-09-03 it elided **257 lines out of the middle** of a 505-minute
day while the model it was feeding accepts 1,048,576 tokens. The output budgets
had the same history in reverse: `max_tokens` was a no-op on DashScope (dropped
whenever `force_json` was set) so nobody had to think about it, and the move to
OpenRouter -- which always sends it -- turned four stale numbers into four
places an answer can be cut off mid-sentence.

Three properties are pinned.

1. **One ceiling, not four.** `ANSWER_TOKEN_CEILING` lives in `llm_utils`
   because that is the module that puts it on the wire. Three lambdas each had
   their own `16000`; a vendor change has to move all of them or none.

2. **The ceiling fits the smallest model this deploy can reach.** Reasoning
   tokens are completion tokens, so what travels is ceiling + headroom. Against
   `gemini-3.8-flash` -- 65,536 output cap, the smallest of the three models on
   the table -- that sum has to clear.

3. **Truncation, when it still happens, keeps the head AND the tail.** Two bare
   `[:N]` slices survived in the ask path long after the same defect was fixed
   in extraction, the rolling summary and the minutes. A session's decisions are
   at its END.

What is deliberately NOT pinned: the specific limits. They are env-overridable
by design, and a test asserting `== 300000` would just be a fifth copy of a
number to keep in sync.
"""
import os


os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")
os.environ.setdefault("S3_BUCKET", "test-bucket")
# `llm_utils` reads its key once at import; see the note in
# test_extraction_transcript_limit.py about import order.
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy-key")

import llm_utils


# ------------------------------------------------------------------
# One ceiling
# ------------------------------------------------------------------

# The completion caps OpenRouter reports for the three models this deploy can
# reach, read from its /models endpoint on 2026-09-07. gemini is the binding
# one; the other two are here so a future swap can be checked against the list
# rather than against memory.
VENDOR_OUTPUT_CAPS = {
    "google/gemini-3.8-flash": 65_536,
    "qwen/qwen3.8-flash": 131_072,
    "meta/muse-spark-1.3-contributor": 943_718,
}


def test_the_ceiling_clears_the_smallest_model_on_the_table():
    """Ceiling + headroom is what goes on the wire, and it must fit the SMALLEST
    reachable model -- not the one deployed today. Sending a max_tokens above a
    vendor's cap is a 400 on some vendors and a silent clamp on others, and the
    silent clamp is worse: it truncates an answer while every field says OK."""
    on_the_wire = llm_utils.ANSWER_TOKEN_CEILING + llm_utils.REASONING_HEADROOM_TOKENS
    smallest = min(VENDOR_OUTPUT_CAPS.values())
    assert on_the_wire <= smallest, (
        "%d exceeds %s" % (on_the_wire, min(VENDOR_OUTPUT_CAPS, key=VENDOR_OUTPUT_CAPS.get)))


def test_the_ceiling_is_far_above_the_widest_real_report():
    """3,386 completion tokens is the measured peak across every daily report on
    prod, from the widest day in the corpus (2026-09-02, 505 minutes of audio).
    The budget is not sized to be tight -- it is sized so that when an answer
    stops, the reason is the model finishing, never the cap."""
    assert llm_utils.ANSWER_TOKEN_CEILING >= 4 * 3_386


def test_every_caller_shares_the_one_ceiling():
    """A caller carrying its own copy is a caller that gets left behind."""
    import lambda_extract_session as les
    import lambda_meeting_minutes  # noqa: F401  (import-time wiring only)
    import lambda_report_generator  # noqa: F401

    # The extraction budget is the one that scales with input, so it is the one
    # that can actually reach the ceiling. A huge session must land ON it.
    assert les.max_tokens_for(n_segments=10_000) == llm_utils.ANSWER_TOKEN_CEILING
    # ...and a small one must not be pinned to the ceiling, or the scaling rule
    # is decorative.
    assert les.max_tokens_for(n_segments=1) < llm_utils.ANSWER_TOKEN_CEILING


# ------------------------------------------------------------------
# Head and tail, everywhere a limit still bites
# ------------------------------------------------------------------

def _long_turns(n):
    return ["[09:%02d:%02d] spk_0: line number %d about the scaffold" % (i // 60, i % 60, i)
            for i in range(n)]


def test_the_ask_transcript_keeps_the_end_of_the_session(monkeypatch):
    """`format_transcripts_for_prompt` returned `result[:MAX_TRANSCRIPT_CHARS]`.

    Ask is the one path where a person is waiting for the answer, and "what did
    we agree at the end" is the question it is most often asked. A head slice
    answers it from the part of the day that does not contain the answer, and
    reports nothing.
    """
    import lambda_ask_agent as aa
    lines = _long_turns(4000)
    monkeypatch.setattr(aa, "format_turns_for_prompt", lambda norm, **kw: lines)
    monkeypatch.setattr(aa, "MAX_TRANSCRIPT_CHARS", 5_000)

    out = aa.format_transcripts_for_prompt([{"any": "shape"}])

    assert lines[0] in out, "the opening is gone"
    assert lines[-1] in out, "the ending is gone -- this is the defect"
    assert "omitted" in out, "the model was not told anything was dropped"


def test_the_ask_report_summary_keeps_its_tail(monkeypatch):
    """Same slice, same fix, different function: next steps and quality items are
    rendered last, so a head slice drops exactly the actionable half."""
    import lambda_ask_agent as aa
    monkeypatch.setattr(aa, "MAX_REPORT_CHARS", 400)
    report = {
        "executive_summary": "x" * 600,
        "quality_and_compliance": [
            {"status": "concern", "item": "UNIQUE_TAIL_ITEM", "details": "d"}],
    }

    out = aa.format_report_for_prompt(report, "daily")

    assert "UNIQUE_TAIL_ITEM" in out, "the tail of the report is gone"


def test_an_ask_transcript_under_the_limit_is_untouched(monkeypatch):
    """Elision must not announce itself on a session that fits."""
    import lambda_ask_agent as aa
    lines = _long_turns(3)
    monkeypatch.setattr(aa, "format_turns_for_prompt", lambda norm, **kw: lines)
    out = aa.format_transcripts_for_prompt([{"any": "shape"}])
    assert out == "\n".join(lines)
    assert "omitted" not in out
