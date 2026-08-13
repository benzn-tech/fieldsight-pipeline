"""Unit: the transcript viewer must not render a batched session two minutes early.

Plan: docs/superpowers/plans/2026-08-13-batch-by-wall-clock.md phase 5, item 5 — the one
consumer the first pass missed.

The viewer does not call `normalize_transcript`; it has its own
`file_time_sec + segment.start_time` arithmetic and its own per-file window prefilter. So
the AST invariant in `test_batch_map_travels.py`, which asserts that every
`normalize_transcript` caller rebases, is **structurally blind** to it: the property that
matters is "every absolute-time resolver rebases", and the invariant can only see one shape
of resolver. These tests cover the other shape directly.

Two distinct wrongs, both silent:

* **the span** — a batch's `_off/_to` tokens describe the CONCATENATED audio, which for a
  window bridging a VAD-dropped chunk is shorter than the wall clock it covers. The
  prefilter compares that short span against the topic window and can drop the whole file.
* **the times** — every segment after a bridged gap renders up to a window early.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "src"))

mod = pytest.importorskip("lambda_org_api")
import batch_stitch as bs  # noqa: E402

SID = "9f8c1e2a4b6d47f0a1b2c3d4e5f60718"
BATCH = f"Benl1_2026-08-13_09-00-00_sid{SID}_c0004_bn3_off0.0_to88.0_srcwav.json"
PLAIN = f"Benl1_2026-08-13_09-00-00_sid{SID}_c0004_off0.0_to30.0_srcwav.json"


def _gapped_map():
    """4, 6, 7 — chunk 5 dropped. 88 s of audio spanning 120 s of wall clock."""
    return bs.build_map(SID, [
        bs.member(4, "k4", "2026-08-13T09:00:00", 0.0, 30.0, seam="first"),
        bs.member(6, "k6", "2026-08-13T09:01:00", 0.0, 30.0, seam="gap"),
        bs.member(7, "k7", "2026-08-13T09:01:30", 2.0, 28.0, seam="adjacent"),
    ], sealed_by="arrival")


def test_the_prefilter_spans_the_wall_clock_not_the_concatenated_audio():
    """`_to88.0` describes 88 s of audio. The file really covers 09:00:00 to 09:02:00.

    A topic window over the last half-minute compares against the short span, decides the
    file ended at 09:01:28, and excludes it — the Transcript tab loses a whole file for a
    topic the Audio tab plays quite happily.
    """
    end = mod._org_transcript_file_end_sec(BATCH, 0.0, batch_map=_gapped_map())
    # The last member starts 09:01:30; its first 2 s are trimmed and 28 s kept, so its audio
    # ends where its nominal 30 s chunk ended. Trimming shortens the head, not the tail.
    assert end == pytest.approx(120.0), \
        "the end must come from the map's last member, not from the _to token"
    assert mod._org_transcript_file_end_sec(BATCH, 0.0) == pytest.approx(88.0), \
        "and the filename alone says 88 — 32 seconds of meeting the prefilter would lose"


def test_a_per_chunk_file_is_unchanged():
    assert mod._org_transcript_file_end_sec(PLAIN, 100.0) == pytest.approx(130.0)


def test_a_batch_with_no_map_keeps_the_filename_span():
    """A pre-change batch transcript has no embedded map. A bounded span error is
    recoverable; refusing to render the file is not."""
    assert mod._org_transcript_file_end_sec(BATCH, 0.0) == pytest.approx(88.0)


def test_a_segment_after_the_gap_renders_at_its_real_wall_clock_time():
    """Segment at 45 s into the concatenated audio is 15 s into the SECOND member, which
    began 60 s along the wall clock — not 45."""
    got = mod._org_segment_abs_sec(100.0, 45.0, _gapped_map())
    assert got == pytest.approx(175.0), \
        "45 s of audio past a bridged gap is 75 s of wall clock"


def test_a_segment_before_the_gap_is_unaffected():
    assert mod._org_segment_abs_sec(100.0, 10.0, _gapped_map()) == pytest.approx(110.0)


def test_without_a_map_the_arithmetic_is_exactly_what_it_was():
    assert mod._org_segment_abs_sec(100.0, 45.0, None) == pytest.approx(145.0)


def test_the_word_level_filter_uses_the_map_too():
    """The render path was wired; the word-level in-range filter was not.

    `filtered_text` and `in_range_count` are built by comparing each word's absolute time
    against the topic window. On a batched file that arithmetic is early by the bridged gap,
    so words that belong to the topic are dropped and earlier ones let in — a quietly wrong
    quote rather than a visibly wrong timestamp.

    It was also invisible to the invariant that forbids bare `file_time_sec + seg_*`: this
    one adds `word_start`. A guard that names the variable it saw last time only catches
    the regression that already happened.
    """
    import ast
    import os as _os
    src = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.dirname(
        _os.path.abspath(__file__)))), "src", "lambda_org_api.py")
    tree = ast.parse(open(src, encoding="utf-8").read())
    names = {"_org_segment_abs_sec", "_org_transcript_file_end_sec"}
    helpers = [n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name in names]
    assert len(helpers) == len(names)
    inside = {ln for h in helpers
              for ln in range(h.lineno, (h.end_lineno or h.lineno) + 1)}
    bare = [n.lineno for n in ast.walk(tree)
            if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Add)
            and n.lineno not in inside
            and getattr(n.left, "id", None) == "file_time_sec"]
    assert not bare, (
        f"lambda_org_api.py:{bare} adds an offset to the file time directly; every one of "
        f"them needs _org_segment_abs_sec so a bridged gap is accounted for")
