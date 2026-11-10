"""Unit: rendering reports exactly which turns the model was shown.

`elide_middle` drops middle lines of a transcript that does not fit the prompt.
A resolver that maps a model-quoted `[HH:MM:SS]` stamp back to a turn must only
accept turns whose line the model actually saw, so rendering has to say which
turn indices made it into the text -- a count of omitted lines is not enough.

The existing stats keys are persisted in the extraction artifact
(`transcript_stats`), so their values are pinned to the numbers the code
produced before this change.
"""
import os

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")
os.environ.setdefault("S3_BUCKET", "test-bucket")
# `llm_utils` reads the key once at import; see test_extraction_transcript_limit.py.
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy-key")

import lambda_extract_session as les
import transcript_utils as tu


def _turns(n, width):
    """n synthetic turns; with width=82 each rendered line is exactly 100 chars
    (the "[10:MM:SS] spk_0: " prefix is 18)."""
    return [{"abs_start_str": "10:%02d:%02d" % (i // 60, i % 60),
             "speaker": "spk_0",
             "text": (("Synthetic line %d " % i) + "x" * width)[:width]}
            for i in range(n)]


def _short():
    return _turns(3, 20)


def _long():
    return _turns(50, 82)


def test_untruncated_shows_every_turn():
    text, stats = les.render_transcript(_short())
    assert stats["truncated"] is False
    assert stats["shown"] == [0, 1, 2]
    assert stats["head_lines"] == 3
    assert stats["tail_lines"] == 0


def test_truncated_shown_excludes_the_middle():
    turns = _long()
    text, stats = les.render_transcript(turns, limit=2000)
    assert stats["truncated"] is True
    h, t = stats["head_lines"], stats["tail_lines"]
    assert h > 0 and t > 0
    assert stats["shown"] == list(range(h)) + list(range(50 - t, 50))
    assert len(stats["shown"]) == 50 - stats["lines_omitted"]
    # Independent check against the rendered text itself: every shown turn's
    # stamp is printed, every omitted turn's stamp is not.
    shown = set(stats["shown"])
    for i, turn in enumerate(turns):
        printed = f"[{turn['abs_start_str']}]" in text
        assert printed == (i in shown), i


def test_existing_stats_keys_unchanged():
    _, short = les.render_transcript(_short())
    assert {k: short[k] for k in ("chars", "lines", "lines_omitted", "truncated")} == {
        "chars": 116, "lines": 3, "lines_omitted": 0, "truncated": False}
    _, long_ = les.render_transcript(_long(), limit=2000)
    assert {k: long_[k] for k in ("chars", "lines", "lines_omitted", "truncated")} == {
        "chars": 1904, "lines": 50, "lines_omitted": 32, "truncated": True}


def test_elide_middle_reports_head_and_tail_counts():
    lines = ["L%03d " % i + "y" * 95 for i in range(50)]
    _, stats = tu.elide_middle(lines, 2000)
    assert stats["head_lines"] + stats["tail_lines"] + stats["lines_omitted"] == 50
    _, stats = tu.elide_middle(["a", "b"], 1000)
    assert (stats["head_lines"], stats["tail_lines"]) == (2, 0)
