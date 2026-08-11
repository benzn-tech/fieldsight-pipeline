"""Unit: a batched transcript's word times are re-based through its map.

Plan: docs/superpowers/plans/2026-08-11-batched-transcription.md phase 5.

The provider returns `word.start` counted from the first sample of the concatenated file —
an origin that appears in no filename. Filename arithmetic gets the batch's *start* right
and then drifts by however much overlap was trimmed at each earlier seam. The drift is a
second or two, reports still render, and nothing fails. So the map is authoritative, and
these tests compare every re-based time against what the per-chunk path would have produced
for the same words.

Missing map → keep filename arithmetic and say so loudly. Never drop the transcript: a
bounded time error is recoverable, a missing transcript is not.
"""
from datetime import datetime

import pytest

es = pytest.importorskip("lambda_extract_session")
import batch_stitch as bs  # noqa: E402

SID = "9f8c1e2a4b6d47f0a1b2c3d4e5f60718"
PREFIX = "transcripts/Ben_UCPK/2026-08-11"
STEM = f"Benl1_2026-08-11_14-18-47_sid{SID}_c0004"


def _map_doc():
    """Four 30 s chunks, 2 s trimmed off each seam — 28 s of kept audio after the first."""
    members = [
        bs.member(4, "k4", "2026-08-11T14:18:47", 0.0, 30.0),
        bs.member(5, "k5", "2026-08-11T14:19:15", 2.0, 28.0),
        bs.member(6, "k6", "2026-08-11T14:19:43", 2.0, 28.0),
        bs.member(7, "k7", "2026-08-11T14:20:11", 2.0, 28.0),
    ]
    return bs.build_map(SID, members, sealed_by="arrival")


def _turns(*offsets):
    return [{"speaker": "spk_0", "text": "x", "start_sec": o, "end_sec": o + 1.0,
             "abs_start": datetime(2026, 8, 11, 14, 18, 47), "abs_end": None}
            for o in offsets]


class FakeS3:
    def __init__(self, objects):
        self.objects = objects
        self.gets = []

    def get_object(self, Bucket, Key):
        self.gets.append(Key)
        if Key not in self.objects:
            raise KeyError(Key)

        class B:
            def __init__(self, d):
                self._d = d

            def read(self):
                return self._d
        import json
        return {"Body": B(json.dumps(self.objects[Key]).encode())}


@pytest.fixture
def batch_key():
    return bs.build_batch_name(STEM, 4, 0.0, 114.0)


def test_every_rebased_time_matches_what_the_per_chunk_path_would_say(monkeypatch, batch_key):
    """Spec verification №1, at the level the pipeline actually consumes."""
    import json
    map_key = f"{PREFIX}/{batch_key[:-4]}_batch_map.json"
    monkeypatch.setattr(es, "s3", lambda: FakeS3({map_key: _map_doc()}))
    normalized = {"speaker_turns": _turns(0.0, 12.5, 30.0, 58.0, 86.0)}
    out = es._rebase_batch_turns("b", f"{PREFIX}/{batch_key}", normalized)
    got = [t["abs_start"].strftime("%H:%M:%S.%f")[:-3] for t in out["speaker_turns"]]
    assert got == ["14:18:47.000", "14:18:59.500", "14:19:17.000",
                   "14:19:45.000", "14:20:13.000"]
    assert json.dumps  # keep the import meaningful under -O


def test_the_end_of_a_turn_is_rebased_too(monkeypatch, batch_key):
    map_key = f"{PREFIX}/{batch_key[:-4]}_batch_map.json"
    monkeypatch.setattr(es, "s3", lambda: FakeS3({map_key: _map_doc()}))
    out = es._rebase_batch_turns("b", f"{PREFIX}/{batch_key}",
                                 {"speaker_turns": _turns(30.0)})
    turn = out["speaker_turns"][0]
    assert turn["abs_end"].strftime("%H:%M:%S") == "14:19:18"
    assert turn["abs_start_str"] == "14:19:17", "the rendered strings must follow"


def test_a_non_batch_transcript_is_left_completely_alone(monkeypatch):
    s3 = FakeS3({})
    monkeypatch.setattr(es, "s3", lambda: s3)
    normalized = {"speaker_turns": _turns(5.0)}
    out = es._rebase_batch_turns("b", f"{PREFIX}/{STEM}_off0.0_to30.0_srcwav.json",
                                 normalized)
    assert out is normalized
    assert s3.gets == [], "a per-chunk transcript must not cost an S3 read"


def test_a_missing_map_keeps_the_filename_times_rather_than_dropping_the_transcript(
        monkeypatch, batch_key):
    """A bounded time error is recoverable. A dropped transcript is silence."""
    monkeypatch.setattr(es, "s3", lambda: FakeS3({}))
    before = _turns(30.0)
    out = es._rebase_batch_turns("b", f"{PREFIX}/{batch_key}",
                                 {"speaker_turns": before})
    assert out["speaker_turns"][0]["abs_start"] == before[0]["abs_start"]


def test_a_turn_with_no_offset_is_not_invented_a_time(monkeypatch, batch_key):
    map_key = f"{PREFIX}/{batch_key[:-4]}_batch_map.json"
    monkeypatch.setattr(es, "s3", lambda: FakeS3({map_key: _map_doc()}))
    out = es._rebase_batch_turns("b", f"{PREFIX}/{batch_key}",
                                 {"speaker_turns": [{"speaker": "s", "text": "x",
                                                     "abs_start": None}]})
    assert out["speaker_turns"][0]["abs_start"] is None


def test_the_in_file_offsets_are_left_batch_relative(monkeypatch, batch_key):
    """`start_sec` is what the evidence and playback paths use to seek inside the audio
    file named by `source_filename` — and that file is the batch WAV. Re-basing it would
    point every quote at a position that does not exist in the object it names."""
    map_key = f"{PREFIX}/{batch_key[:-4]}_batch_map.json"
    monkeypatch.setattr(es, "s3", lambda: FakeS3({map_key: _map_doc()}))
    out = es._rebase_batch_turns("b", f"{PREFIX}/{batch_key}",
                                 {"speaker_turns": _turns(58.0)})
    assert out["speaker_turns"][0]["start_sec"] == 58.0
