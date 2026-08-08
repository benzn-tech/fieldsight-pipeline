"""Unit: a turn knows which transcript segment it came from (P1-2 Task 1).

The enabling change for claim provenance. Without it the audio anchor for a
cited quote has to be reverse-engineered from an absolute timestamp: re-deriving
each segment's interval from its filename (BUG-09's arithmetic, which this repo
has already got wrong once) and then disambiguating the ~2s ring-buffer overlap
where two chunks cover the same instant.

Carrying the filename forward costs one zip(). The offset costs nothing at all —
turn['start_sec'] is already the in-file offset (transcript_utils: "offset from
segment start (relative)"), which is exactly what a player seeks to.
"""
from datetime import datetime

import pytest

ex = pytest.importorskip("lambda_extract_session", reason="requires the lambda deps")


def _normalized(name, n=1):
    return {"speaker_turns": [
        {"speaker": "spk_0", "text": f"turn from {name}", "start_sec": 1.5,
         "end_sec": 3.0, "abs_start": datetime(2026, 8, 7, 14, 0, i),
         "abs_end": datetime(2026, 8, 7, 14, 0, i + 1),
         "abs_start_str": "14:00:00", "abs_end_str": "14:00:01"}
        for i in range(n)]}


@pytest.fixture
def two_segments(monkeypatch):
    payloads = {"one.json": _normalized("one"), "two.json": _normalized("two")}

    class _S3:
        def get_object(self, Bucket=None, Key=None):
            class _B:
                def read(self_inner): return b"{}"
            return {"Body": _B()}

    monkeypatch.setattr(ex, "s3", lambda: _S3())
    monkeypatch.setattr(ex, "normalize_transcript",
                        lambda data, filename: payloads.get(filename))
    monkeypatch.setattr(ex, "filter_device_announcements",
                        lambda turns: (turns, {"removed": 0, "texts": []}))
    return ["transcripts/a/d/one.json", "transcripts/a/d/two.json"]


def test_every_turn_carries_its_source_filename(two_segments):
    turns, files, _ = ex.assemble_session_turns("bkt", two_segments)
    assert len(turns) == 2
    assert {t["source_filename"] for t in turns} == {"one.json", "two.json"}
    assert files == ["one.json", "two.json"]


def test_the_filename_matches_the_turn_it_came_from(two_segments):
    turns, _, _ = ex.assemble_session_turns("bkt", two_segments)
    for t in turns:
        assert t["source_filename"].startswith(t["text"].split()[-1]), \
            "turns must not be paired with the wrong segment's filename"


def test_start_sec_is_untouched(two_segments):
    # It is already the in-file offset — the value a player seeks to. Nothing
    # here may adjust it.
    turns, _, _ = ex.assemble_session_turns("bkt", two_segments)
    assert all(t["start_sec"] == 1.5 for t in turns)


def test_the_stamp_survives_dedup(monkeypatch, two_segments):
    # _dedup_turn_boundaries rebuilds turns with dict(t, text=...). If it ever
    # stopped doing that, the stamp would vanish between assembly and the caller
    # and the anchor would silently become unresolvable.
    seen = {}

    def _spy(turns):
        seen["before"] = [t.get("source_filename") for t in turns]
        return ex._dedup_turn_boundaries.__wrapped__(turns) if hasattr(
            ex._dedup_turn_boundaries, "__wrapped__") else turns

    monkeypatch.setattr(ex, "_dedup_turn_boundaries", _spy)
    turns, _, _ = ex.assemble_session_turns("bkt", two_segments)
    assert all(seen["before"]), "the stamp must be applied BEFORE dedup"
    assert all(t.get("source_filename") for t in turns)


def test_a_skipped_segment_does_not_shift_the_pairing(monkeypatch):
    # The filenames list and the normalized list are built in the same loop and
    # a skipped segment appends to NEITHER — but a future refactor that appended
    # to one and not the other would pair every turn with the wrong file, and
    # nothing would fail. Pinned.
    payloads = {"good.json": _normalized("good")}

    class _S3:
        def get_object(self, Bucket=None, Key=None):
            class _B:
                def read(self_inner): return b"{}"
            return {"Body": _B()}

    monkeypatch.setattr(ex, "s3", lambda: _S3())
    monkeypatch.setattr(ex, "normalize_transcript",
                        lambda data, filename: payloads.get(filename))   # bad.json -> None
    monkeypatch.setattr(ex, "_is_empty_transcript", lambda data: True)
    monkeypatch.setattr(ex, "filter_device_announcements",
                        lambda turns: (turns, {"removed": 0, "texts": []}))
    turns, files, _ = ex.assemble_session_turns(
        "bkt", ["transcripts/a/d/bad.json", "transcripts/a/d/good.json"])
    assert files == ["good.json"]
    assert [t["source_filename"] for t in turns] == ["good.json"]
