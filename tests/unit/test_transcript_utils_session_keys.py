"""Tests for the chunk/session key tokens in transcript_utils
(2026-07 voice-timeliness paradigm — mobile-client-session-contract §4).

transcript_utils has no heavy deps (os/re/datetime), so it imports directly.
"""
from datetime import datetime

import transcript_utils as tu

SID = "9f8c1e2a4b6d47f0a1b2c3d4e5f60718"
CHUNK_KEY = f"Benl1_2026-07-28_14-03-00_sid{SID}_c0007.wav"
# A VAD segment carved out of that chunk keeps sid/c and appends the off/to/src tokens.
CHUNK_SEG_KEY = f"Benl1_2026-07-28_14-03-00_sid{SID}_c0007_off3.0_to58.5_srcwav.json"
LEGACY_SEG_KEY = "Benl1_2026-03-20_12-18-34_off1465.8_to1729.8_srcwav.json"
LEGACY_WHOLE_KEY = "Benl1_2026-03-20_12-18-34.json"


class TestSessionId:
    def test_extracts_from_chunk_key(self):
        assert tu.extract_session_id_from_filename(CHUNK_KEY) == SID

    def test_extracts_from_vad_segment_of_a_chunk(self):
        assert tu.extract_session_id_from_filename(CHUNK_SEG_KEY) == SID

    def test_none_on_legacy_segment(self):
        assert tu.extract_session_id_from_filename(LEGACY_SEG_KEY) is None

    def test_none_on_legacy_whole_file(self):
        assert tu.extract_session_id_from_filename(LEGACY_WHOLE_KEY) is None

    def test_full_s3_key_ok(self):
        key = f"transcripts/Ben_UCPK/2026-07-28/{CHUNK_SEG_KEY}"
        assert tu.extract_session_id_from_filename(key) == SID

    def test_rejects_wrong_length_hex(self):
        # 31 hex chars — not a valid session id
        bad = "Benl1_2026-07-28_14-03-00_sid9f8c1e2a4b6d47f0a1b2c3d4e5f6071_c0000.wav"
        assert tu.extract_session_id_from_filename(bad) is None


class TestChunkIndex:
    def test_extracts_from_chunk_key(self):
        assert tu.extract_chunk_index_from_filename(CHUNK_KEY) == 7

    def test_extracts_from_vad_segment_of_a_chunk(self):
        assert tu.extract_chunk_index_from_filename(CHUNK_SEG_KEY) == 7

    def test_index_zero(self):
        key = f"Benl1_2026-07-28_14-03-00_sid{SID}_c0000.wav"
        assert tu.extract_chunk_index_from_filename(key) == 0

    def test_none_on_legacy(self):
        assert tu.extract_chunk_index_from_filename(LEGACY_SEG_KEY) is None
        assert tu.extract_chunk_index_from_filename(LEGACY_WHOLE_KEY) is None

    def test_requires_sid_prefix(self):
        # a stray _c1234 with no preceding _sid must NOT be read as a chunk index
        assert tu.extract_chunk_index_from_filename("weird_c1234_2026-07-28_14-03-00.wav") is None


class TestT1BaseTimeUnchanged:
    """A chunk key's HH-MM-SS is that chunk's TRUE start, so the existing
    base-time parser already yields the chunk's absolute start (T1) — the
    sid/c tokens must not break it."""

    def test_chunk_true_start(self):
        assert tu.extract_base_time_from_filename(CHUNK_KEY) == datetime(2026, 7, 28, 14, 3, 0)

    def test_chunk_segment_true_start(self):
        assert tu.extract_base_time_from_filename(CHUNK_SEG_KEY) == datetime(2026, 7, 28, 14, 3, 0)

    def test_vad_offsets_still_parse_with_sid_c(self):
        # off/to must still be readable when sid/c tokens precede them
        assert tu.extract_vad_offsets_from_filename(CHUNK_SEG_KEY) == (3.0, 58.5)

    def test_legacy_unaffected(self):
        assert tu.extract_base_time_from_filename(LEGACY_SEG_KEY) == datetime(2026, 3, 20, 12, 18, 34)
