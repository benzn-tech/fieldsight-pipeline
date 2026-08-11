"""Unit: the pure arithmetic of batching four chunks into one transcription request.

Plan: docs/superpowers/plans/2026-08-11-batched-transcription.md phase 1.
Spec: docs/superpowers/specs/2026-08-11-batched-transcription.md.

Nothing here touches S3, DynamoDB or a provider. What is tested is the two things that fail
silently if they are wrong:

* **the time map** — the provider returns `word.start` counted from the first sample of the
  concatenated file, an origin that appears in no filename. Get the map wrong and every
  timestamp in the meeting shifts by a second or two while the report still reads fine.
* **the filename contract** — the batch key must be read correctly by every parser that
  already exists, none of which knows batching happened. So the round-trip is asserted
  against the real parsers, not against a copy of the format.
"""
import os
import random
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "src"))

import batch_stitch as bs  # noqa: E402
import chunk_stitch  # noqa: E402
import transcript_utils as tu  # noqa: E402

RATE = 16000
SID = "9f8c1e2a4b6d47f0a1b2c3d4e5f60718"
STEM = f"Benl1_2026-08-11_14-18-47_sid{SID}_c0004"


def pcm(seconds, value=1000, rate=RATE):
    """Deterministic, non-repeating PCM.

    A plain ramp will not do: a ramp is self-similar, so a suffix of one ramp equals a
    prefix of another by coincidence, and the fixture would manufacture the very false match
    this module exists to avoid. Seeded noise has no such structure — and `randbytes` is C,
    which matters: generating this sample by sample in Python dominated the runtime of the
    whole file.
    """
    return random.Random(value).randbytes(int(seconds * rate) * 2)


def silence(seconds, rate=RATE):
    """Digital silence — what a refused microphone actually produces: full buffers of
    zeros, returned as a positive read length, not as an error."""
    return b"\x00\x00" * int(seconds * rate)


# ---- measuring the overlap (never trusting a constant) ----

def test_a_two_second_identical_tail_is_measured_as_two_seconds():
    tail_only = pcm(2.0, value=500)
    prev = pcm(28.0) + tail_only
    nxt = tail_only + pcm(28.0, value=9000)
    assert bs.measure_overlap(prev, nxt, RATE) == pytest.approx(2.0, abs=1e-6)


def test_a_one_and_a_half_second_tail_is_measured_as_one_and_a_half():
    """The 2026-08-10 measurement said 1.50 s while the mobile code says 2.0. The point of
    measuring is that the harness does not need that argument settled."""
    tail_only = pcm(1.5, value=700)
    assert bs.measure_overlap(pcm(20.0) + tail_only, tail_only + pcm(20.0, value=9000),
                              RATE) == pytest.approx(1.5, abs=1e-6)


def test_no_shared_tail_measures_zero_and_the_caller_must_not_trim():
    assert bs.measure_overlap(pcm(5.0, value=100), pcm(5.0, value=20000), RATE) == 0.0


def test_an_overlap_longer_than_the_ceiling_reads_as_unmeasured_not_as_a_capped_trim():
    """An overlap is a suffix/prefix match, so one longer than the ceiling cannot be seen at
    all — every truncated comparison inside the ceiling fails. It therefore measures 0, which
    the caller reads as "unmeasured, keep the audio". That is the safe direction: the
    duplicate survives into the transcript and `_dedup_turn_boundaries` still removes it,
    whereas capping would trim a length nobody verified."""
    shared = pcm(4.0, value=333)
    got = bs.measure_overlap(pcm(10.0) + shared, shared + pcm(10.0, value=9000),
                             RATE, ceiling_sec=2.0)
    assert got == 0.0


def test_digital_silence_at_a_seam_is_not_mistaken_for_the_overlap():
    """Two chunks that both run into silence match for as long as the silence lasts, and
    that match is not the ring-buffer copy. Trimming it deletes real audio — and this device
    produces exactly that: a refused microphone returns a positive length and a buffer of
    zeros, which is indistinguishable from a quiet room until something like this trims on
    it."""
    assert bs.measure_overlap(pcm(3.0) + silence(1.5), silence(1.5) + pcm(3.0, value=77),
                              RATE) == 0.0


def test_a_real_overlap_that_merely_ends_in_silence_is_still_measured():
    """The guard rejects a match that carries no information, not a match that happens to
    contain some quiet."""
    shared = pcm(1.0, value=612) + silence(0.5)
    assert bs.measure_overlap(pcm(3.0) + shared, shared + pcm(3.0, value=88),
                              RATE) == pytest.approx(1.5, abs=1e-6)


def test_an_odd_trailing_byte_does_not_shift_the_sample_grid():
    """A truncated upload can leave half a sample. Reading it as a whole one moves every
    later sample by half a period and the comparison silently finds nothing."""
    shared = pcm(1.0, value=222)
    assert bs.measure_overlap(pcm(3.0) + shared, shared + pcm(3.0, value=800) + b"\x01",
                              RATE) == pytest.approx(1.0, abs=1e-6)


def test_the_measurement_is_in_seconds_so_a_different_rate_still_answers_in_seconds():
    shared = pcm(2.0, value=444, rate=8000)
    assert bs.measure_overlap(pcm(4.0, rate=8000) + shared,
                              shared + pcm(4.0, value=9000, rate=8000),
                              8000) == pytest.approx(2.0, abs=1e-6)


# ---- which chunks may travel together ----

def test_consecutive_chunks_group_into_fours():
    assert bs.plan_batches([0, 1, 2, 3, 4, 5, 6, 7]) == [[0, 1, 2, 3], [4, 5, 6, 7]]


def test_a_gap_splits_the_run_because_a_batch_never_spans_one():
    """A missing index must not be spliced shut, and must not merely be recorded — every
    consumer except extract resolves time by filename arithmetic and would be a whole chunk
    wrong past the gap."""
    assert bs.plan_batches([4, 5, 7, 8, 9, 10, 11]) == [[4, 5], [7, 8, 9, 10], [11]]


def test_a_lone_chunk_is_a_batch_of_one():
    assert bs.plan_batches([9]) == [[9]]


def test_nothing_present_is_no_batches():
    assert bs.plan_batches([]) == []


def test_indices_arriving_out_of_order_are_still_grouped_by_sequence():
    """Uploads arrive out of order routinely — a retry can land hours after its successor."""
    assert bs.plan_batches([5, 4, 7, 6]) == [[4, 5, 6, 7]]


# ---- the filename contract, asserted against the real parsers ----

def test_the_batch_name_is_read_correctly_by_every_existing_parser():
    name = bs.build_batch_name(STEM, count=4, head_trim_sec=0.0, duration_sec=114.0)
    assert tu.extract_session_id_from_filename(name) == SID
    assert tu.extract_chunk_index_from_filename(name) == 4
    assert tu.extract_device_from_filename(name) == "Benl1"
    assert tu.extract_base_time_from_filename(name).strftime("%H:%M:%S") == "14:18:47"
    assert chunk_stitch.parse_chunk_key(name) == (SID, 4)


def test_the_head_trim_rides_in_off_so_filename_arithmetic_starts_at_the_true_sample():
    name = bs.build_batch_name(STEM, count=4, head_trim_sec=1.5, duration_sec=112.5)
    off_start, off_end = tu.extract_vad_offsets_from_filename(name)
    assert (off_start, off_end) == (1.5, 114.0)
    base = tu.compute_segment_base_time(name)
    assert base.strftime("%H:%M:%S.%f")[:-3] == "14:18:48.500"


def test_a_batch_key_is_recognisable_and_a_member_key_is_not():
    batch = bs.build_batch_name(STEM, count=4, head_trim_sec=0.0, duration_sec=114.0)
    assert bs.is_batch_key(batch) is True
    assert bs.is_batch_key(f"{STEM}.wav") is False
    assert bs.is_batch_key(f"{STEM}_off0.0_to30.0_srcwav.wav") is False
    assert bs.is_batch_key("Benl1_2026-03-20_12-18-34_off1465.8_to1729.8_srcwav.json") is False


def test_a_device_name_containing_bn_is_not_mistaken_for_a_batch():
    """`_bn{K}` is the only thing separating two minutes of audio from thirty seconds of it.
    A false positive here means a batch is never transcribed and the session simply reads
    quieter, with no error anywhere."""
    assert bs.is_batch_key(f"x_bn2_y_2026-08-11_14-18-47_sid{SID}_c0004.wav") is False


# ---- the map: the only sanctioned way to turn a batch offset into a real time ----

def _members():
    return [
        bs.member(4, "k4", "2026-08-11T14:18:47", trimmed_head_sec=0.0, kept_duration_sec=30.0),
        bs.member(5, "k5", "2026-08-11T14:19:15", trimmed_head_sec=2.0, kept_duration_sec=28.0),
        bs.member(6, "k6", "2026-08-11T14:19:43", trimmed_head_sec=2.0, kept_duration_sec=28.0),
        bs.member(7, "k7", "2026-08-11T14:20:11", trimmed_head_sec=2.0, kept_duration_sec=28.0),
    ]


def test_every_batch_offset_resolves_to_what_the_per_chunk_path_would_have_said():
    """Spec verification №1. `t` is seconds into the concatenated file; the answer must equal
    the chunk's own clock plus the position inside that chunk."""
    m = bs.build_map(SID, _members(), sealed_by="arrival")
    cases = [
        (0.0, "14:18:47.000"),      # first sample of chunk 4
        (12.5, "14:18:59.500"),     # inside chunk 4
        (30.0, "14:19:17.000"),     # first KEPT sample of chunk 5 = its clock + 2 s trim
        (58.0, "14:19:45.000"),     # first kept sample of chunk 6
        (86.0, "14:20:13.000"),     # first kept sample of chunk 7
        (113.9, "14:20:40.900"),    # last moment of the batch
    ]
    for t, want in cases:
        got = bs.resolve_abs_time(m, t)
        assert got.strftime("%H:%M:%S.%f")[:-3] == want, f"t={t}"


def test_a_boundary_belongs_to_the_chunk_that_starts_there():
    m = bs.build_map(SID, _members(), sealed_by="arrival")
    assert bs.resolve_abs_time(m, 30.0).strftime("%H:%M:%S") == "14:19:17"


def test_a_time_past_the_end_clamps_instead_of_inventing_a_chunk():
    """The provider can return a word fractionally past the declared duration. Extrapolating
    would place it in a chunk that does not exist."""
    m = bs.build_map(SID, _members(), sealed_by="arrival")
    assert bs.resolve_abs_time(m, 9999.0) == bs.resolve_abs_time(m, 114.0)


def test_unequal_chunk_lengths_resolve_correctly():
    """segmentSeconds is a device setting, so 30 s is not guaranteed."""
    members = [
        bs.member(0, "k0", "2026-08-11T09:00:00", trimmed_head_sec=0.0, kept_duration_sec=20.0),
        bs.member(1, "k1", "2026-08-11T09:00:20", trimmed_head_sec=1.0, kept_duration_sec=44.0),
    ]
    m = bs.build_map(SID, members, sealed_by="gap")
    assert bs.resolve_abs_time(m, 20.0).strftime("%H:%M:%S") == "09:00:21"
    assert bs.resolve_abs_time(m, 25.0).strftime("%H:%M:%S") == "09:00:26"


def test_a_one_member_batch_is_the_per_chunk_path_and_must_agree_with_it():
    m = bs.build_map(SID, [_members()[0]], sealed_by="session_close")
    assert bs.resolve_abs_time(m, 7.25).strftime("%H:%M:%S.%f")[:-3] == "14:18:54.250"


def test_a_seam_that_measured_zero_is_recorded_as_unmeasured_and_keeps_its_audio():
    """Measured-zero is not proof of no overlap; it is proof the assumption broke. The safe
    response is to keep the audio and say so."""
    m = bs.build_map(SID, [
        bs.member(0, "k0", "2026-08-11T09:00:00", trimmed_head_sec=0.0, kept_duration_sec=30.0),
        bs.member(1, "k1", "2026-08-11T09:00:28", trimmed_head_sec=0.0,
                  kept_duration_sec=30.0, trim_measured=False),
    ], sealed_by="arrival")
    second = m["members"][1]
    assert second["trim_measured"] is False
    assert second["trimmed_head_sec"] == 0.0
    assert second["kept_duration_sec"] == 30.0


def test_the_map_records_why_the_batch_was_sealed():
    """`gap` and `session_close` mean different things when a transcript looks short."""
    m = bs.build_map(SID, _members(), sealed_by="gap")
    assert m["sealed_by"] == "gap"
    assert m["schema"] == 1
    assert m["session_id"] == SID


# ---- concatenation ----

def test_concatenating_keeps_every_kept_sample_and_nothing_else():
    a, b = pcm(2.0, value=100), pcm(3.0, value=5000)
    out, sr = bs.concat_wavs([(a, RATE), (b, RATE)], trims_sec=[0.0, 1.0])
    assert sr == RATE
    assert len(out) == len(a) + len(b) - RATE * 2      # 1.0 s of b trimmed, 2 bytes/sample
    assert out.startswith(a)


def test_concatenation_refuses_to_mix_sample_rates():
    """Resampling here would be a silent quality change inside a function whose job is to
    join bytes."""
    with pytest.raises(ValueError):
        bs.concat_wavs([(pcm(1.0), RATE), (pcm(1.0, rate=8000), 8000)], trims_sec=[0.0, 0.0])


def test_a_trim_longer_than_the_chunk_is_refused_rather_than_emptying_it():
    with pytest.raises(ValueError):
        bs.concat_wavs([(pcm(1.0), RATE), (pcm(1.0), RATE)], trims_sec=[0.0, 5.0])
