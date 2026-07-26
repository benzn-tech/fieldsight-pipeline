"""Tests for src/keyframe_selection.py -- video-keyframe-to-photo plan, Task 1.

All pure; no AWS, no subprocess. Table style mirrors
tests/unit/test_photo_binding.py.

NOTE (2026-07-24 rule correction): the frame rule is no longer a single
midpoint. n_frames = duration_min // 2 (2-3min->1, 4-5->2, 6-7->3 ...),
frames at the midpoints of n equal sub-intervals -- start + (i+0.5)*D/n --
with n capped at KEYFRAME_MAX_FRAMES (10). The helper is
keyframe_seconds(time_range) -> list[int] (empty list when gated
out/unparseable/wrapped).
"""
import pytest

import keyframe_selection as ks


# ---- keyframe_seconds: the >=2-minute gate + per-frame midpoints ----

def _hms(h, m, s=0):
    return h * 3600 + m * 60 + s


@pytest.mark.parametrize("time_range,expected", [
    # 2 min -> exactly 1 frame at the window midpoint (degenerates to old rule)
    ("10:15 – 10:17", [_hms(10, 16, 0)]),
    # 3 min -> still 1 frame (3 // 2 == 1); midpoint at :30
    ("10:15 – 10:18", [_hms(10, 16, 30)]),
    # 6 min -> 3 frames at 10:01:00 / 10:03:00 / 10:05:00 (the plan's worked example)
    ("10:00 – 10:06", [_hms(10, 1, 0), _hms(10, 3, 0), _hms(10, 5, 0)]),
    # 5 min -> 2 frames (5 // 2 == 2), midpoints of the two halves
    ("10:15 – 10:20", [_hms(10, 16, 15), _hms(10, 18, 45)]),
    # 4 min -> 2 frames
    ("10:00 – 10:04", [_hms(10, 1, 0), _hms(10, 3, 0)]),
    # gated out (below the 2-minute threshold) -> empty
    ("12:12 – 12:13", []),          # 1 min (prod-majority case)
    ("12:14 – 12:14", []),          # same-minute
    # ASCII hyphen accepted (phase2 normalized dash)
    ("10:15-10:17", [_hms(10, 16, 0)]),
    # wrapped/overnight -> empty (never negative)
    ("23:59 – 00:05", []),
    ("garbage", []),
    (None, []),
    ("", []),
])
def test_keyframe_seconds(time_range, expected):
    assert ks.keyframe_seconds(time_range) == expected


def test_keyframe_seconds_scales_one_frame_per_full_2_minutes():
    # 2-3 min -> 1, 4-5 -> 2, 6-7 -> 3, 8-9 -> 4 ...
    assert len(ks.keyframe_seconds("10:00 – 10:02")) == 1
    assert len(ks.keyframe_seconds("10:00 – 10:03")) == 1
    assert len(ks.keyframe_seconds("10:00 – 10:04")) == 2
    assert len(ks.keyframe_seconds("10:00 – 10:05")) == 2
    assert len(ks.keyframe_seconds("10:00 – 10:06")) == 3
    assert len(ks.keyframe_seconds("10:00 – 10:07")) == 3
    assert len(ks.keyframe_seconds("10:00 – 10:08")) == 4


def test_keyframe_seconds_caps_at_ten_frames():
    # A hallucinated multi-hour range must not request hundreds of decodes.
    # 10:00 -> 13:00 is 180 min -> 90 frames uncapped; clamp to the ceiling.
    frames = ks.keyframe_seconds("10:00 – 13:00")
    assert ks.KEYFRAME_MAX_FRAMES == 10
    assert len(frames) == 10
    # still strictly increasing and inside the window
    assert frames == sorted(frames)
    assert _hms(10, 0) < frames[0] and frames[-1] < _hms(13, 0)


def test_keyframe_seconds_frames_are_symmetric_and_never_on_edges():
    frames = ks.keyframe_seconds("10:00 – 10:06")
    start, end = _hms(10, 0), _hms(10, 6)
    assert all(start < f < end for f in frames)
    # symmetric about the window centre
    centre = (start + end) / 2
    assert frames[0] - start == pytest.approx(end - frames[-1])


# ---- select_covering_recording ----

REC_A = {"source_key": "users/U/video/2026-07-23/Benl1_2026-07-23_10-00-00.mp4",
         "base_s": 10 * 3600, "duration_s": 1200.0}          # 10:00:00-10:20:00
REC_B = {"source_key": "users/U/video/2026-07-23/Benl1_2026-07-23_10-25-00.mp4",
         "base_s": 10 * 3600 + 25 * 60, "duration_s": 600.0}  # 10:25:00-10:35:00


def test_mid_inside_one_recording():
    # topic 10:05-10:15, mid 10:10:00 -> REC_A at seek 600s
    key, seek = ks.select_covering_recording([REC_A, REC_B],
                                             10 * 3600 + 5 * 60, 10 * 3600 + 15 * 60,
                                             10 * 3600 + 10 * 60)
    assert key == REC_A["source_key"]
    assert seek == pytest.approx(600.0)


def test_mid_in_gap_clamps_to_nearest_covered_instant_in_window():
    # topic 10:18-10:28, mid 10:23:00 falls in the 10:20-10:25 gap.
    # Covered instants inside the window: REC_A gives [10:18,10:20), REC_B
    # gives [10:25,10:28]. Nearest to 10:23 is REC_B's edge 10:25 (clamped 1s
    # inside -> seek 1.0).
    key, seek = ks.select_covering_recording([REC_A, REC_B],
                                             10 * 3600 + 18 * 60, 10 * 3600 + 28 * 60,
                                             10 * 3600 + 23 * 60)
    assert key == REC_B["source_key"]
    assert seek == pytest.approx(1.0)


def test_no_video_coverage_returns_none():
    # audio-only day (empty), and a video entirely outside the window
    assert ks.select_covering_recording([], 36000, 36600, 36300) is None
    assert ks.select_covering_recording([REC_B], 9 * 3600, 9 * 3600 + 300, 9 * 3600 + 150) is None


def test_seek_clamped_inside_file_edges():
    # mid exactly at REC_A start -> seek clamped to >= 1.0s, never 0/negative
    key, seek = ks.select_covering_recording([REC_A], 10 * 3600, 10 * 3600 + 240, 10 * 3600)
    assert key == REC_A["source_key"]
    assert seek >= 1.0


# ---- keyframe_filename ----

def test_keyframe_filename_parses_to_mid_topic_time():
    from transcript_utils import extract_base_time_from_filename
    name = ks.keyframe_filename("Benl1", "2026-07-23", 10 * 3600 + 17 * 60 + 30,
                                "Benl1_2026-07-23_10-15-34")
    assert name == "Benl1_2026-07-23_10-17-30_kf_s101534.jpg"
    # THE contract: the only parseable timestamp is the per-frame MID time,
    # so item-writer re-runs re-bind this file to the right topic (BUG-01-safe).
    parsed = extract_base_time_from_filename(name)
    assert parsed is not None
    assert parsed.strftime("%H-%M-%S") == "10-17-30"


def test_keyframe_filename_session_marker_is_digits_only():
    # The session marker is digits-only (hyphens stripped) so it can never
    # match either timestamp regex in transcript_utils -- the first and only
    # parseable timestamp is the frame mid time.
    name = ks.keyframe_filename("Benl1", "2026-07-23", 3 * 3600 + 5 * 60, "Benl1_2026-07-23_10-15-34")
    assert name.endswith("_kf_s101534.jpg")
    assert "s101534" in name


# ---- ffmpeg_frame_cmd: BUG-04 guard is a hard assertion ----

def test_ffmpeg_cmd_seeks_before_input():
    cmd = ks.ffmpeg_frame_cmd("/opt/bin/ffmpeg", "/tmp/in.mp4", 600.0, "/tmp/out.jpg")
    assert cmd.index("-ss") < cmd.index("-i"), "input-side seek (BUG-04): -ss MUST precede -i"
    assert cmd[cmd.index("-ss") + 1] == "600.0"
    assert cmd[cmd.index("-i") + 1] == "/tmp/in.mp4"
    assert "-frames:v" in cmd and cmd[cmd.index("-frames:v") + 1] == "1"
    assert cmd[-1] == "/tmp/out.jpg"


def test_ffmpeg_cmd_seek_formatted_one_decimal():
    # the seek is emitted with .1f precision (fractional-second seeks land the
    # right GOP; asserts the exact argv the handler tests also pin)
    cmd = ks.ffmpeg_frame_cmd("/opt/bin/ffmpeg", "/tmp/in.mp4", 1.0, "/tmp/out.jpg")
    assert cmd[cmd.index("-ss") + 1] == "1.0"
