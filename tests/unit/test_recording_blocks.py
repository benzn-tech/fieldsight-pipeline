"""Unit: recording_blocks -- merge, read-time filtering (F1), topic placement.

Pure module; every test drives the real functions.

The 2026-09-02 fixture is CONSTRUCTED: three chunk sessions whose segments are
consistent with the blocks design §4.1 measured for that day on Ben_UCPK2
(10:55–11:35, 17:14–17:47, 17:57–18:15). The full-day check was measured separately
against the day's real manifest; this fixture pins the merge rule, not the data.
"""
import pytest

import recording_blocks as rb

SID_A = "81a64d850bb34d8bbf01ec11fd2af02f"
SID_B = "c3d1e0a2b4f64e5f9a7b8c9d0e1f2a3b"
SID_C = "5e6f7a8b9c0d4e1f8a2b3c4d5e6f7a8b"
PREFIX = "transcripts/Ben_UCPK2/2026-09-02/"


def _seg(start, end, sid, name):
    return {"start": start, "end": end, "session_id": sid, "key": PREFIX + name}


# Seconds since midnight, derived by hand from the key names beside them.
DAY_0902 = [
    _seg(39318.0, 39323.0, SID_A, f"ben_ucpk2_2026-09-02_10-55-18_sid{SID_A}_c0001_off0.0_to5.0_srcwav.json"),
    _seg(39902.0, 39930.0, SID_A, f"ben_ucpk2_2026-09-02_11-05-02_sid{SID_A}_c0021_off0.0_to28.0_srcwav.json"),
    _seg(40482.0, 40510.0, SID_A, f"ben_ucpk2_2026-09-02_11-14-40_sid{SID_A}_c0041_off2.0_to30.0_srcwav.json"),
    _seg(41050.0, 41080.0, SID_A, f"ben_ucpk2_2026-09-02_11-24-10_sid{SID_A}_c0061_off0.0_to30.0_srcwav.json"),
    _seg(41630.0, 41660.0, SID_A, f"ben_ucpk2_2026-09-02_11-33-50_sid{SID_A}_c0079_off0.0_to30.0_srcwav.json"),
    _seg(41712.0, 41732.0, SID_A, f"ben_ucpk2_2026-09-02_11-35-12_sid{SID_A}_c0081_off0.0_to20.0_srcwav.json"),
    _seg(62045.0, 62075.0, SID_B, f"ben_ucpk2_2026-09-02_17-14-05_sid{SID_B}_c0001_off0.0_to30.0_srcwav.json"),
    _seg(62620.0, 62650.0, SID_B, f"ben_ucpk2_2026-09-02_17-23-40_sid{SID_B}_c0019_off0.0_to30.0_srcwav.json"),
    _seg(63175.0, 63205.0, SID_B, f"ben_ucpk2_2026-09-02_17-32-55_sid{SID_B}_c0037_off0.0_to30.0_srcwav.json"),
    _seg(63691.5, 63720.0, SID_B, f"ben_ucpk2_2026-09-02_17-41-30_sid{SID_B}_c0055_off1.5_to30.0_srcwav.json"),
    _seg(64000.0, 64025.0, SID_B, f"ben_ucpk2_2026-09-02_17-46-40_sid{SID_B}_c0065_off0.0_to25.0_srcwav.json"),
    _seg(64650.0, 64680.0, SID_C, f"ben_ucpk2_2026-09-02_17-57-30_sid{SID_C}_c0001_off0.0_to30.0_srcwav.json"),
    _seg(65170.0, 65200.0, SID_C, f"ben_ucpk2_2026-09-02_18-06-10_sid{SID_C}_c0017_off0.0_to30.0_srcwav.json"),
    _seg(65690.0, 65710.0, SID_C, f"ben_ucpk2_2026-09-02_18-14-50_sid{SID_C}_c0033_off0.0_to20.0_srcwav.json"),
]


# ---- merge_segments ------------------------------------------------------

def test_the_0902_day_merges_into_the_three_blocks_the_design_measured():
    blocks = rb.merge_segments(DAY_0902, 600, 5400)
    assert [(b["from"], b["to"]) for b in blocks] == [
        ("10:55", "11:35"), ("17:14", "17:47"), ("17:57", "18:15")]
    assert [b["minutes"] for b in blocks] == [40, 33, 18]
    assert [b["session_ids"] for b in blocks] == [[SID_A], [SID_B], [SID_C]]
    assert all(b["selectable_as_whole"] for b in blocks)


def test_input_order_does_not_matter():
    shuffled = list(reversed(DAY_0902))
    assert rb.merge_segments(shuffled, 600, 5400) == rb.merge_segments(DAY_0902, 600, 5400)


def test_a_gap_of_exactly_the_threshold_merges_and_one_second_more_splits():
    a = {"start": 0.0, "end": 100.0, "session_id": None, "key": "k1"}
    b_at = {"start": 700.0, "end": 710.0, "session_id": None, "key": "k2"}
    b_after = {"start": 701.0, "end": 710.0, "session_id": None, "key": "k3"}
    assert len(rb.merge_segments([a, b_at], 600, 5400)) == 1
    assert len(rb.merge_segments([a, b_after], 600, 5400)) == 2


def test_the_17_47_to_17_57_silence_is_over_ten_minutes_so_those_stay_apart():
    # 17:47:05 -> 17:57:30 is 625 s. At a 630 s gap the two afternoon blocks join.
    joined = rb.merge_segments(DAY_0902, 630, 5400)
    assert [(b["from"], b["to"]) for b in joined] == [("10:55", "11:35"), ("17:14", "18:15")]
    assert joined[1]["session_ids"] == sorted([SID_B, SID_C])


def test_a_block_longer_than_the_long_threshold_is_not_selectable_as_a_whole():
    blocks = rb.merge_segments(DAY_0902, 600, 1980)
    # 40 min, 33 min (exactly 1980 s), 18 min
    assert [b["selectable_as_whole"] for b in blocks] == [False, True, True]


def test_a_contained_segment_does_not_shorten_the_block():
    long_seg = {"start": 0.0, "end": 1000.0, "session_id": None, "key": "a"}
    inner = {"start": 10.0, "end": 20.0, "session_id": None, "key": "b"}
    [block] = rb.merge_segments([long_seg, inner], 600, 5400)
    assert block["end"] == 1000.0


def test_null_session_ids_are_not_listed_and_duplicates_collapse():
    segs = [{"start": 0.0, "end": 5.0, "session_id": None, "key": "a"},
            {"start": 6.0, "end": 9.0, "session_id": SID_A, "key": "b"},
            {"start": 10.0, "end": 12.0, "session_id": SID_A, "key": "c"}]
    [block] = rb.merge_segments(segs, 600, 5400)
    assert block["session_ids"] == [SID_A]


def test_segments_without_numeric_times_are_ignored_not_guessed():
    segs = [{"start": None, "end": 5.0, "session_id": None, "key": "a"},
            {"start": True, "end": 5.0, "session_id": None, "key": "b"},
            {"start": 60.0, "end": 90.0, "session_id": None, "key": "c"}]
    [block] = rb.merge_segments(segs, 600, 5400)
    assert (block["start"], block["end"]) == (60.0, 90.0)


def test_no_segments_is_no_blocks():
    assert rb.merge_segments([], 600, 5400) == []


def test_a_segment_past_midnight_renders_as_23_59_not_24():
    [block] = rb.merge_segments(
        [{"start": 86390.0, "end": 86420.0, "session_id": None, "key": "a"}], 600, 5400)
    assert (block["from"], block["to"]) == ("23:59", "23:59")
    assert block["end"] == 86420.0


# ---- filter_segments (F1) --------------------------------------------------

def test_a_session_excluded_by_build_day_sessions_is_dropped():
    kept = rb.filter_segments(DAY_0902, {SID_B}, [])
    assert {s["session_id"] for s in kept} == {SID_A, SID_C}


def test_a_deleted_chunk_session_is_dropped_by_its_extraction_tombstone():
    tombstone = f"extractions/Ben_UCPK2/2026-09-02/sid{SID_C}"
    kept = rb.filter_segments(DAY_0902, set(), [tombstone])
    assert {s["session_id"] for s in kept} == {SID_A, SID_B}


def test_a_tombstone_for_another_folder_or_date_does_not_match():
    kept = rb.filter_segments(DAY_0902, set(), [
        f"extractions/Someone_Else/2026-09-02/sid{SID_C}",
        f"extractions/Ben_UCPK2/2026-09-03/sid{SID_C}",
    ])
    assert len(kept) == len(DAY_0902)


def test_a_legacy_whole_file_recording_is_dropped_by_its_base_tombstone():
    legacy = {"start": 45779.8, "end": 46043.8, "session_id": None,
              "key": "transcripts/Benl1/2026-03-20/Benl1_2026-03-20_12-18-34_off1465.8_to1729.8_srcwav.json"}
    other = {"start": 50000.0, "end": 50010.0, "session_id": None,
             "key": "transcripts/Benl1/2026-03-20/Benl1_2026-03-20_13-50-00.json"}
    kept = rb.filter_segments([legacy, other], set(),
                              ["extractions/Benl1/2026-03-20/Benl1_2026-03-20_12-18-34"])
    assert kept == [other]


def test_a_tombstone_on_the_raw_transcript_key_also_matches():
    target = DAY_0902[0]["key"]
    kept = rb.filter_segments(DAY_0902, set(), [target])
    assert target not in {s["key"] for s in kept}
    assert len(kept) == len(DAY_0902) - 1


def test_empty_prefixes_never_match_everything():
    assert len(rb.filter_segments(DAY_0902, None, ["", None])) == len(DAY_0902)


# ---- topic_ids_in_block ------------------------------------------------------

TOPICS = [
    {"id": "t-a1", "time_range": "10:56 – 11:10"},
    {"id": "t-a2", "time_range": "11:30 – 11:36"},
    {"id": "t-b1", "time_range": "17:30 – 17:45"},
    {"id": "t-edge", "time_range": "17:47 – 17:50"},   # starts inside 17:47:05's minute
    {"id": "t-gap", "time_range": "17:50 – 17:55"},    # wholly in the silence between blocks
    {"id": "t-c1", "time_range": "18:00 – 18:14"},
    {"id": "t-empty", "time_range": ""},
    {"id": "t-none", "time_range": None},
    {"id": "t-backwards", "time_range": "11:10 – 10:56"},
]


def test_topics_are_placed_by_overlap_with_minute_precision():
    blocks = rb.merge_segments(DAY_0902, 600, 5400)
    placed = [rb.topic_ids_in_block(b, TOPICS) for b in blocks]
    assert placed == [["t-a1", "t-a2"], ["t-b1", "t-edge"], ["t-c1"]]


def test_unparseable_and_backwards_topics_are_in_no_block():
    blocks = rb.merge_segments(DAY_0902, 600, 5400)
    everywhere = {tid for b in blocks for tid in rb.topic_ids_in_block(b, TOPICS)}
    assert not everywhere & {"t-empty", "t-none", "t-backwards", "t-gap"}


def test_a_single_minute_topic_is_not_zero_width():
    block = {"start": 43650.0, "end": 43660.0}          # 12:07:30 - 12:07:40
    assert rb.topic_ids_in_block(block, [{"id": 7, "time_range": "12:07 – 12:07"}]) == ["7"]


def test_the_parser_is_photo_bindings_and_accepts_its_dash_forms():
    block = {"start": 0.0, "end": 86399.0}
    rows = [{"id": "hyphen", "time_range": "08:19 - 08:48"},
            {"id": "en", "time_range": "08:19 – 08:48"},
            {"id": "em", "time_range": "08:19 — 08:48"}]
    assert rb.topic_ids_in_block(block, rows) == ["hyphen", "en", "em"]
    assert rb.parse_time_range is __import__("photo_binding").parse_time_range
