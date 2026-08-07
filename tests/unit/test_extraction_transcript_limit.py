"""Unit: a long session reaches the model whole, and a truncated one says so.

`TRANSCRIPT_TEXT_LIMIT = 60000` was a bare slice on the joined transcript. A
real 2-hour session renders to **128,427 characters** — 838 turn lines — so
**47%** of it, 387 lines, was all the authoritative extraction ever saw. The
second half of every long meeting was missing: no error, no log line, no field
in the artifact, and an extraction that looked exactly as complete as a short
one. CLAUDE.md BUG-15, recurring in a different lambda.

Three properties are pinned here.

1. **A real 2-hour session fits.** The limit is sized against a measured
   session, not a guess, and it is read from the environment so a pathological
   one can be walked back without a code deploy.

2. **Truncation keeps the head AND the tail.** A site session says where it is
   and who is present at the start, and lands its decisions and action items at
   the end. Head-only truncation discarded precisely the half worth extracting.

3. **Truncation is loud in all three places it can be noticed**: the log, the
   stored artifact (`transcript_stats.truncated`), and the prompt itself. The
   third matters more than it looks — shown a transcript that simply stops, the
   model will reasonably report the session as having ended there, which
   converts a missing half into a confident false statement.

Raising the cap rather than chunking is deliberate; the reasoning lives next to
the constant. The short version: chunking means map-reduce with cross-chunk
topic dedup inside the function that livelocked (BUG-43), and output tokens do
not scale with input on the prod path anyway — `llm_utils` sends no
`max_tokens` at all under `force_json`.
"""
import os

import pytest

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")
os.environ.setdefault("S3_BUCKET", "test-bucket")
# `llm_utils` reads the key ONCE at import. This file sorts before
# test_lambda_extract_session.py, so without this line importing it here first
# caches an empty key and takes 16 of that file's tests down with it — a new
# test file breaking unrelated tests purely through import order.
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy-key")

import lambda_extract_session as les


def _turns(n, text="We need the scaffold tags checked before the pour"):
    return [{"abs_start_str": "09:%02d:%02d" % (i // 60, i % 60),
             "speaker": "spk_%d" % (i % 3),
             "text": f"{text} ({i})"} for i in range(n)]


# ------------------------------------------------------------------
# The measured session fits
# ------------------------------------------------------------------

def test_a_two_hour_session_is_not_truncated():
    """128,427 chars is the real number from the 2026-08-07 session. Under the
    old 60,000 cap this test would fail by construction."""
    turns = _turns(838)
    text, stats = les.render_transcript(turns)
    assert stats["chars"] > 60000, "the fixture must be bigger than the old cap"
    assert stats["truncated"] is False
    assert stats["lines_omitted"] == 0
    assert text.count("\n") + 1 == 838


def test_the_limit_covers_the_measured_session_with_room():
    assert les.TRANSCRIPT_TEXT_LIMIT >= 128427, (
        "a real 2-hour session renders to 128,427 chars")


def test_the_limit_is_environment_tunable():
    """A prompt that turns out too big for the provider must be walkable-back
    without shipping code — the previous incident in this lambda cost a night."""
    import inspect
    src = inspect.getsource(les)
    assert "os.environ.get('TRANSCRIPT_TEXT_LIMIT'" in src


# ------------------------------------------------------------------
# When it does not fit
# ------------------------------------------------------------------

def test_truncation_keeps_the_opening_and_the_close():
    turns = _turns(2000)
    text, stats = les.render_transcript(turns, limit=5000)

    assert stats["truncated"] is True
    assert stats["lines_omitted"] > 0
    assert "(0)" in text, "the session opening must survive"
    assert "(1999)" in text, "the session close must survive — the old slice dropped it"
    assert "omitted" in text, "the gap must be visible in the text itself"


def test_truncation_respects_the_limit():
    turns = _turns(2000)
    for limit in (2000, 5000, 20000):
        text, stats = les.render_transcript(turns, limit=limit)
        assert len(text) <= limit, f"rendered {len(text)} > limit {limit}"
        assert stats["chars"] == len(text)


def test_truncation_cuts_on_line_boundaries():
    """A half-line ending mid-word is a turn the model has to guess at, and the
    guess lands in an action item."""
    turns = _turns(2000)
    text, _ = les.render_transcript(turns, limit=5000)
    for line in text.split("\n"):
        assert line.startswith("[") or line.startswith("[..."), line


def test_the_line_count_adds_up():
    """kept + omitted == total. Without this the omitted number is decoration."""
    turns = _turns(2000)
    text, stats = les.render_transcript(turns, limit=5000)
    kept = len([l for l in text.split("\n") if not l.startswith("[...")])
    assert kept + stats["lines_omitted"] == stats["lines"] == 2000


def test_truncation_is_logged(caplog):
    caplog.set_level("WARNING")
    les.render_transcript(_turns(2000), limit=5000)
    assert any("truncated" in r.message.lower() or "truncated" in r.getMessage().lower()
               for r in caplog.records), "a silent drop is the bug, not the fix"


# ------------------------------------------------------------------
# The prompt tells the model
# ------------------------------------------------------------------

def test_a_complete_transcript_carries_no_gap_note():
    prompt, stats = les.build_extraction_prompt("U", "2026-08-08", "sess", _turns(10), 1)
    assert stats["truncated"] is False
    assert "INCOMPLETE" not in prompt


def test_a_truncated_transcript_warns_the_model_in_the_prompt(monkeypatch):
    """Shown a transcript that just stops, the model reports the session as
    having ended there — a missing half becomes a confident false statement."""
    monkeypatch.setattr(les, "TRANSCRIPT_TEXT_LIMIT", 5000)
    prompt, stats = les.build_extraction_prompt("U", "2026-08-08", "sess", _turns(2000), 40)
    assert stats["truncated"] is True
    assert "INCOMPLETE" in prompt
    assert "do not conclude anything about what happened during the gap" in prompt.lower()


def test_the_gap_note_sits_with_the_transcript_not_after_the_rules():
    """An instruction placed after 3,000 characters of schema is an instruction
    the model has stopped weighting."""
    import inspect
    src = inspect.getsource(les.build_extraction_prompt)
    assert src.index("{gap_note}") < src.index("{transcript_text}")


# ------------------------------------------------------------------
# The artifact records it
# ------------------------------------------------------------------

def test_the_extraction_records_what_the_model_was_shown():
    import inspect
    src = inspect.getsource(les)
    assert "'transcript_stats': transcript_stats" in src, (
        "an extraction covering half a session must be distinguishable from one "
        "covering all of it")
