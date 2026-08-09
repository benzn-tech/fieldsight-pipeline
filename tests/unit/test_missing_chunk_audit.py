"""Unit: telling a hole in a recording from a pause in it.

Chunk indices are allocated in sequence, so a hole is a chunk that existed and
never reached S3 — no audio, no sidecar, no transcript, and no artifact anywhere
saying the record is incomplete.

Three ways of asking produce three different answers, and two of them are wrong.
Each is pinned here because each was actually believed first:

1. **Timestamp arithmetic says 64% of everything is lost.** Summing
   `next_start - (start + duration)` over the prod bucket gives 12,463 s, and its
   two largest entries are someone pausing and resuming ninety minutes later. A
   pause keeps the session id and continues the index sequence.
2. **Chunk sizes find a different, rarer event.** Counting chunks shorter than
   30 s finds recorder restarts, and misses absence entirely — a chunk that never
   arrived has no size to be short.
3. **The transcript list would report every silent chunk as lost.** VAD drops
   silent chunks before transcription, so they have no transcript either. Only
   the audio object proves arrival.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "scripts"))

import missing_chunk_audit as a  # noqa: E402

SID = "a1b2c3d4e5f60718293a4b5c6d7e8f90"


def line(folder, date, hhmmss, idx, seconds=30.0):
    size = int(seconds * a.BYTES_PER_SEC) + a.WAV_HEADER
    name = f"ben_x_{date}_{hhmmss}_sid{SID}_c{idx:04d}.wav"
    return f"2026-08-07 10:00:00 {size} users/{folder}/audio/{date}/{name}"


# ---- parsing --------------------------------------------------------------

def test_parse_pulls_identity_time_and_duration_from_the_listing():
    got = a.parse_line(line("Ben_UCPK", "2026-08-07", "14-38-51", 46))
    assert got == ("Ben_UCPK", "2026-08-07", SID, 46, 14 * 3600 + 38 * 60 + 51, 30.0)


def test_parse_derives_duration_from_size_without_downloading():
    got = a.parse_line(line("Ben_UCPK", "2026-08-07", "14-50-59", 72, seconds=5.0))
    assert got[5] == 5.0


def test_parse_ignores_everything_that_is_not_a_session_chunk():
    assert a.parse_line("2026-08-07 10:00:00 100 users/x/audio/2026-08-07/nochunk.wav") is None
    assert a.parse_line("2026-08-07 10:00:00 100 reports/2026-08-07/x/daily_report.json") is None
    assert a.parse_line("") is None


# ---- holes ----------------------------------------------------------------

def test_a_gap_in_the_index_sequence_is_a_chunk_that_never_arrived():
    chunks = {0: (0, 30.0), 1: (28, 30.0), 3: (84, 30.0), 4: (112, 30.0)}
    assert a.holes(chunks) == [2]


def test_the_real_session_shape_is_recovered():
    """Ben_UCPK sid39ad6c92: 129 chunks present, five indices absent."""
    chunks = {i: (i * 28, 30.0) for i in range(0, 134)}
    for i in (10, 28, 30, 37, 42):
        del chunks[i]
    assert a.holes(chunks) == [10, 28, 30, 37, 42]


def test_a_session_that_starts_late_is_not_all_holes():
    """Indices are compared from the first one PRESENT, not from zero — a session
    whose earliest chunk is c0005 has not lost five chunks."""
    assert a.holes({5: (0, 30.0), 6: (28, 30.0)}) == []


# ---- pause is not loss ----------------------------------------------------

def test_a_ninety_minute_jump_with_contiguous_indices_is_a_pause():
    chunks = {0: (0, 30.0), 1: (5544 + 30, 30.0)}
    assert a.holes(chunks) == []
    assert a.looks_like_pause(chunks, 0) is True


def test_the_ordinary_two_second_overlap_is_not_a_pause():
    chunks = {0: (0, 30.0), 1: (28, 30.0)}
    assert a.looks_like_pause(chunks, 0) is False


# ---- short chunks ---------------------------------------------------------

def test_a_short_final_chunk_is_normal_and_never_reported():
    """Recording stopped mid-chunk. Warning about it would bury the real case."""
    assert a.short_mid_session({0: (0, 30.0), 1: (28, 30.0), 2: (56, 8.0)}) == []


def test_a_short_chunk_in_the_middle_is_a_recorder_restart():
    chunks = {71: (0, 30.0), 72: (28, 5.0), 73: (36, 30.0), 74: (66, 30.0)}
    assert a.short_mid_session(chunks) == [(72, 5.0)]


# ---- the report -----------------------------------------------------------

def test_a_complete_session_produces_no_finding():
    rows = [a.parse_line(line("Ben_UCPK", "2026-08-07", "14-00-00", i)) for i in range(5)]
    assert a.audit(a.group_sessions(rows)) == []


def test_sessions_are_kept_apart_by_folder_date_and_sid():
    rows = [a.parse_line(line("Ben_UCPK", "2026-08-07", "14-00-00", 0)),
            a.parse_line(line("Ben_UCPK2", "2026-08-07", "14-00-00", 0))]
    assert len(a.group_sessions(rows)) == 2
