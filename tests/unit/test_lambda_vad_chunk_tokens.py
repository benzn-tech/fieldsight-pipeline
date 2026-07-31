"""VAD chunk/session token propagation + T2 timestamp fields
(2026-07 voice-timeliness paradigm — spec §8.1/§8.2).

lambda_vad builds an S3 client at import time, so give boto3 dummy env creds
before importing (no network is touched by the pure functions under test).
"""
import os

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")

import pytest

lv = pytest.importorskip("lambda_vad", reason="requires boto3 (installed in CI)")
import transcript_utils as tu  # pure module — cross-check the round-trip

SID = "9f8c1e2a4b6d47f0a1b2c3d4e5f60718"
CHUNK_KEY = f"users/Ben_UCPK/audio/2026-07-28/Benl1_2026-07-28_14-03-00_sid{SID}_c0007.wav"
LEGACY_KEY = "users/Jarley_Trainor/audio/2026-03-20/Benl1_2026-03-20_12-18-34.wav"


class TestParseSourceFilename:
    def test_parses_sid_and_chunk_index(self):
        info = lv.parse_source_filename(CHUNK_KEY)
        assert info["device"] == "Benl1"
        assert info["date"] == "2026-07-28"
        assert info["time"] == "14-03-00"          # chunk true start (T1)
        assert info["session_id"] == SID
        assert info["chunk_index"] == 7

    def test_legacy_key_has_none_tokens(self):
        info = lv.parse_source_filename(LEGACY_KEY)
        assert info["device"] == "Benl1"
        assert info["session_id"] is None
        assert info["chunk_index"] is None


class TestSegmentNaming:
    def test_stem_carries_sid_c(self):
        info = lv.parse_source_filename(CHUNK_KEY)
        assert lv._segment_stem(info) == f"Benl1_2026-07-28_14-03-00_sid{SID}_c0007"

    def test_legacy_stem_unchanged(self):
        info = lv.parse_source_filename(LEGACY_KEY)
        assert lv._segment_stem(info) == "Benl1_2026-03-20_12-18-34"

    def test_segment_filename_shape(self):
        info = lv.parse_source_filename(CHUNK_KEY)
        name = lv.build_segment_filename(info, 3.0, 58.5)
        assert name == f"Benl1_2026-07-28_14-03-00_sid{SID}_c0007_off3.0_to58.5_srcwav.wav"

    def test_roundtrip_into_transcript_utils(self):
        # The VAD segment name must be parseable back by the downstream module,
        # so session grouping + T1 timing hold end to end.
        info = lv.parse_source_filename(CHUNK_KEY)
        name = lv.build_segment_filename(info, 3.0, 58.5)
        assert tu.extract_session_id_from_filename(name) == SID
        assert tu.extract_chunk_index_from_filename(name) == 7
        assert tu.extract_vad_offsets_from_filename(name) == (3.0, 58.5)


class TestAbsoluteStart:
    def test_absolute_start_adds_offset(self):
        info = lv.parse_source_filename(CHUNK_KEY)
        assert lv._absolute_start_iso(info, 3.0) == "2026-07-28T14:03:03"

    def test_absolute_start_zero_offset_is_chunk_start(self):
        info = lv.parse_source_filename(CHUNK_KEY)
        assert lv._absolute_start_iso(info, 0) == "2026-07-28T14:03:00"

    def test_absolute_start_none_on_bad_time(self):
        assert lv._absolute_start_iso({"date": "", "time": ""}, 5.0) is None


class TestPlanEmission:
    """Per-chunk vs per-segment emission decision (whole-chunk transcription mode)."""

    def test_segment_mode_returns_merged_unchanged(self):
        segs = [(1.0, 5.0), (12.0, 18.0)]
        assert lv.plan_emission(segs, 30.0, whole_chunk=False) == segs

    def test_segment_mode_is_the_default(self):
        segs = [(1.0, 5.0)]
        assert lv.plan_emission(segs, 30.0) == segs

    def test_whole_chunk_emits_one_full_unit_when_speech(self):
        # any speech -> a single unit spanning the whole chunk (0 -> duration)
        assert lv.plan_emission([(1.0, 5.0), (12.0, 18.0)], 29.7, whole_chunk=True) == [(0.0, 29.7)]

    def test_whole_chunk_emits_nothing_when_no_speech(self):
        # a fully-silent chunk -> [] (the no-speech fallback handles it separately)
        assert lv.plan_emission([], 30.0, whole_chunk=True) == []

    def test_segment_mode_empty_stays_empty(self):
        assert lv.plan_emission([], 30.0, whole_chunk=False) == []
