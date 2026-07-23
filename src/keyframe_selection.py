"""Pure keyframe-selection helpers (video-keyframe-to-photo plan, Task 1).

Everything here is table-testable and AWS-free. Time parsing rides on
photo_binding.parse_time_range (normalized dash) and, in filenames, on
transcript_utils -- never inline regexes (BUG-01/09/11).

Frame rule (2026-07-24 user refinement, supersedes the earlier single-
midpoint spec): the number of keyframes grows one-per-full-2-minutes with
the topic duration -- n_frames = duration_min // 2 (2-3 min -> 1, 4-5 -> 2,
6-7 -> 3 ...). The n frames are placed at the midpoints of n equal
sub-intervals of the window: frame i (0-indexed) is at start + (i+0.5)*D/n.
This degenerates to a single window-midpoint for n == 1, is symmetric, never
lands on an edge, and needs no external state. n is capped at
KEYFRAME_MAX_FRAMES so a hallucinated multi-hour time_range cannot request
hundreds of decodes.
"""
import logging

from photo_binding import parse_time_range

logger = logging.getLogger()

MIN_TOPIC_DURATION_MIN = 2
KEYFRAME_MAX_FRAMES = 10   # ceiling on n_frames; a hallucinated multi-hour
                           # time_range would otherwise request hundreds of
                           # single-GOP decodes
EDGE_MARGIN_S = 1.0        # never seek into the first/last second of a file


def keyframe_seconds(time_range, min_duration_min=MIN_TOPIC_DURATION_MIN):
    """Seconds-since-midnight of each keyframe for this topic window, or an
    empty list when the time_range is missing/unparseable/wrapped or shorter
    than the gate.

    time_range is minute-granular ('HH:MM - HH:MM'). n_frames =
    duration_min // 2 (capped at KEYFRAME_MAX_FRAMES); frame i sits at the
    midpoint of the i-th of n equal sub-intervals: start + (i+0.5)*D/n.
    Example: '10:00 - 10:06' (6 min -> 3 frames) -> 10:01:00, 10:03:00,
    10:05:00. n == 1 degenerates to the single window midpoint."""
    parsed = parse_time_range(time_range)
    if parsed is None:
        return []
    start_min, end_min = parsed
    duration_min = end_min - start_min
    if duration_min < min_duration_min:          # covers wrapped (negative) too
        return []
    n = duration_min // min_duration_min
    if n > KEYFRAME_MAX_FRAMES:
        logger.info("keyframe_seconds: %s -> %d frames clamped to %d",
                    time_range, n, KEYFRAME_MAX_FRAMES)
        n = KEYFRAME_MAX_FRAMES
    start_s = start_min * 60
    duration_s = duration_min * 60
    return [int(start_s + (i + 0.5) * duration_s / n) for i in range(n)]


def select_covering_recording(recordings, topic_start_s, topic_end_s, mid_s):
    """Pick the recording to grab the frame from and the seek offset into it.

    recordings: [{'source_key', 'base_s', 'duration_s'}] (video only).
    Preference order:
      1. a recording whose [base, base+duration] contains mid_s -> seek mid-base
      2. else the covered instant nearest to mid_s within [topic_start_s,
         topic_end_s] (topic spans recordings / mid falls in a gap)
      3. else None (no video overlaps the topic window at all)
    The seek is clamped EDGE_MARGIN_S inside the file so ffmpeg never lands on
    a zero-length tail."""
    best = None  # (distance_to_mid, base_s, source_key, seek)
    for rec in recordings:
        lo, hi = rec["base_s"], rec["base_s"] + rec["duration_s"]
        ov_lo, ov_hi = max(lo, topic_start_s), min(hi, topic_end_s)
        if ov_lo >= ov_hi:
            continue                          # no overlap with the topic window
        instant = min(max(mid_s, ov_lo), ov_hi)
        seek = min(max(instant - lo, EDGE_MARGIN_S),
                   max(rec["duration_s"] - EDGE_MARGIN_S, EDGE_MARGIN_S))
        cand = (abs(instant - mid_s), lo, rec["source_key"], seek)
        if best is None or cand < best:
            best = cand
    if best is None:
        return None
    return best[2], best[3]


def keyframe_filename(device, date, mid_s, session_base):
    """'{device}_{date}_{midHH-MM-SS}_kf_s{sessHHMMSS}.jpg'.
    The per-frame mid time is the FIRST (and only) transcript_utils-parseable
    timestamp -- that is what re-binds the file to the right topic on
    item-writer re-runs. The session marker is digits-only (no hyphens) so it
    can never match either filename-time regex."""
    hh, rem = divmod(int(mid_s), 3600)
    mm, ss = divmod(rem, 60)
    sess = session_base.rsplit("_", 1)[-1].replace("-", "")  # '10-15-34' -> '101534'
    return f"{device}_{date}_{hh:02d}-{mm:02d}-{ss:02d}_kf_s{sess}.jpg"


def ffmpeg_frame_cmd(ffmpeg_path, input_path, seek_s, out_path):
    """Single-frame grab. -ss BEFORE -i = input-side keyframe seek: ffmpeg
    decodes one GOP, not the stream (the BUG-04 memory guard)."""
    return [
        ffmpeg_path, "-y",
        "-ss", f"{seek_s:.1f}",
        "-i", input_path,
        "-frames:v", "1",
        "-q:v", "3",
        out_path,
    ]
