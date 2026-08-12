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
AUDIO_PREFIX = "audio_segments/Ben_UCPK/2026-08-11"
STEM = f"Benl1_2026-08-11_14-18-47_sid{SID}_c0004"


def _map_key_the_seal_writes(batch_key):
    """Where `batch_seal.seal_batch` actually puts the map: beside the batch WAV.

    Derived here the same way the seal derives it, so these tests can never again agree
    with the reader against the writer. They did exactly that until 2026-08-12: the map
    was asserted under `transcripts/`, the reader looked under `transcripts/`, the seal
    wrote under `audio_segments/`, and every batched session in TEST silently fell back
    to filename arithmetic with the whole suite green.
    """
    return bs.map_key_for_audio(f"{AUDIO_PREFIX}/{batch_key}")


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
    map_key = _map_key_the_seal_writes(batch_key)
    monkeypatch.setattr(es, "s3", lambda: FakeS3({map_key: _map_doc()}))
    normalized = {"speaker_turns": _turns(0.0, 12.5, 30.0, 58.0, 86.0)}
    out = es._rebase_batch_turns("b", f"{PREFIX}/{batch_key}", normalized)
    got = [t["abs_start"].strftime("%H:%M:%S.%f")[:-3] for t in out["speaker_turns"]]
    assert got == ["14:18:47.000", "14:18:59.500", "14:19:17.000",
                   "14:19:45.000", "14:20:13.000"]
    assert json.dumps  # keep the import meaningful under -O


def test_the_end_of_a_turn_is_rebased_too(monkeypatch, batch_key):
    map_key = _map_key_the_seal_writes(batch_key)
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
    map_key = _map_key_the_seal_writes(batch_key)
    monkeypatch.setattr(es, "s3", lambda: FakeS3({map_key: _map_doc()}))
    out = es._rebase_batch_turns("b", f"{PREFIX}/{batch_key}",
                                 {"speaker_turns": [{"speaker": "s", "text": "x",
                                                     "abs_start": None}]})
    assert out["speaker_turns"][0]["abs_start"] is None


def test_the_reader_looks_where_the_seal_writes(batch_key):
    """The one assertion that ties the two halves together.

    Everything else here mocks S3, so a reader and a writer that disagree about the key
    both pass. This drives the REAL seal against a recording S3 double and then asks the
    reader for the key it would fetch — the only shape of test that could have caught the
    prefix mismatch.
    """
    import batch_seal

    written = []

    class RecordingS3:
        def put_object(self, Bucket, Key, Body, ContentType=None):
            written.append(Key)

        def get_object(self, Bucket, Key):
            raise KeyError(Key)          # forces measure_trim's unmeasured path

    class FakeTable:
        def put_item(self, Item, ConditionExpression=None):
            return {}

    run = [4, 5, 6, 7]
    by_index = {}
    for i in run:
        stem = f"Benl1_2026-08-11_14-18-47_sid{SID}_c{i:04d}"
        by_index[i] = {"chunk_key": f"{AUDIO_PREFIX}/{stem}_off0.0_to30.0_srcwav.wav"}

    def fake_wav_pcm(_data):
        return b"\x00\x00" * 16000, 16000

    import unittest.mock as mock
    with mock.patch.object(batch_seal, "_wav_pcm", fake_wav_pcm), \
            mock.patch.object(batch_seal, "_wav_bytes", lambda pcm, rate: b"RIFF"):
        class GettableS3(RecordingS3):
            def get_object(self, Bucket, Key):
                class B:
                    def read(self_inner):
                        return b""
                return {"Body": B()}
        batch_seal.seal_batch(GettableS3(), "b", SID, run, by_index, 0, FakeTable())

    map_written = [k for k in written if k.endswith("_batch_map.json")]
    assert len(map_written) == 1, f"expected one map, got {written}"
    audio_written = [k for k in written if k.endswith(".wav")][0]

    # The transcriber names the transcript after the batch audio, under transcripts/.
    # Now drive the REAL reader with a bucket holding only what the seal wrote: if it
    # reaches for any other key it gets nothing, and the times stay un-rebased.
    transcript_key = f"{PREFIX}/{audio_written.rsplit('/', 1)[1][:-4]}.json"
    only_what_was_written = FakeS3({map_written[0]: _map_doc()})
    import unittest.mock as mock2
    with mock2.patch.object(es, "s3", lambda: only_what_was_written):
        out = es._rebase_batch_turns("b", transcript_key,
                                     {"speaker_turns": _turns(30.0)})
    assert out["speaker_turns"][0]["abs_start"].strftime("%H:%M:%S") == "14:19:17", (
        "the extractor fetched a key the seal never wrote — batched sessions would "
        f"silently keep filename arithmetic. It asked for {only_what_was_written.gets}, "
        f"the seal wrote {map_written[0]}")


def test_a_transcript_key_too_short_to_carry_user_and_date_is_refused():
    assert bs.map_key_for_transcript("x.json") is None
    assert bs.map_key_for_transcript("transcripts/x.json") is None


def test_the_in_file_offsets_are_left_batch_relative(monkeypatch, batch_key):
    """`start_sec` is what the evidence and playback paths use to seek inside the audio
    file named by `source_filename` — and that file is the batch WAV. Re-basing it would
    point every quote at a position that does not exist in the object it names."""
    map_key = _map_key_the_seal_writes(batch_key)
    monkeypatch.setattr(es, "s3", lambda: FakeS3({map_key: _map_doc()}))
    out = es._rebase_batch_turns("b", f"{PREFIX}/{batch_key}",
                                 {"speaker_turns": _turns(58.0)})
    assert out["speaker_turns"][0]["start_sec"] == 58.0
