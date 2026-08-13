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


# ============================================================
# Window planning (spec 2026-08-13, plan phase 1)
# ============================================================
# `plan_batches` groups four CONSECUTIVE indices, so a chunk VAD dropped is a gap and the
# batch stops there. Measured over every chunk session in the lake: 37% of sessions with
# four or more chunks lose an interior chunk that way, and 31% shatter into short runs --
# one session of 46 chunks produced batches of [1,1,1,2,1,1]. A one-member batch is a pure
# loss: it saves no request and adds the whole grace wait.
#
# The target was never four chunks. It is two minutes of wall clock, and a VAD-dropped
# chunk inside that window must not end it.

from datetime import datetime, timedelta  # noqa: E402

T0 = datetime(2026, 8, 13, 9, 0, 0)


def _at(*offsets_by_index):
    return {idx: T0 + timedelta(seconds=off) for idx, off in offsets_by_index}


def test_a_vad_dropped_chunk_does_not_split_the_window():
    """The motivating case: c5 was silent, so c4/c6/c7 are one 90-second conversation."""
    got = bs.plan_windows(_at((4, 0), (6, 60), (7, 90)), window_sec=120, cap=4)
    assert got == [[4, 6, 7]]


def test_a_chunk_at_the_window_boundary_starts_the_next_batch():
    """Exclusive end. Four 30 s chunks stay one batch; a fifth at +120 does not make a
    150-second request out of a 120-second rule."""
    got = bs.plan_windows(_at((0, 0), (1, 30), (2, 60), (3, 90), (4, 120)),
                          window_sec=120, cap=8)
    assert got == [[0, 1, 2, 3], [4]]


# `test_the_window_anchors_on_its_first_member_not_on_a_grid` was DELETED on 2026-08-13,
# not edited. It pinned greedy anchoring, chosen hours earlier to save 2% of requests, and
# a real 71-minute replay then showed greedy producing 123 batches for 153 chunks under
# concurrent arrival. The reversal is deliberate; its replacement is
# `test_a_boundary_straddling_run_splits_at_the_bucket_edge`, which asserts the cost the 2%
# was buying and says why it is now being paid.


def test_a_consumed_chunk_is_invisible_to_the_planner():
    """The double-billing pin, at the pure layer.

    Members {4,5} seal. Chunk 3 -- an earlier index -- arrives an hour later. Without
    consumption tracking the planner proposes [3,4,5], which claims a different seal key
    and transcribes 4 and 5 a SECOND time. A late chunk forms its own batch instead.
    """
    got = bs.plan_windows(_at((3, -30), (4, 0), (5, 30)), window_sec=120, cap=4,
                          consumed={4, 5})
    assert got == [[3]]


def test_the_count_cap_still_binds_when_chunks_are_short():
    """The cap is a safety net, not the rule: a device emitting 10 s chunks must not build
    an unbounded request just because they all fit in two minutes."""
    got = bs.plan_windows(_at(*[(i, i * 10) for i in range(12)]),
                          window_sec=120, cap=4)
    assert got == [[0, 1, 2, 3], [4, 5, 6, 7], [8, 9, 10, 11]]


def test_the_map_resolves_correctly_across_a_bridged_gap():
    """Pins the claim that the map format needs no change for gapped batches.

    Each member carries its own absolute start, so the 30 s a dropped chunk occupied is
    simply not addressable: no `t` resolves into it. If this fails, the design decision
    reopens -- do not "fix" the test.
    """
    doc = bs.build_map(SID, [
        bs.member(4, "k4", T0.isoformat(), 0.0, 28.0),
        bs.member(6, "k6", (T0 + timedelta(seconds=60)).isoformat(), 2.0, 30.0),
    ], sealed_by="arrival")
    assert bs.resolve_abs_time(doc, 0.0) == T0
    assert bs.resolve_abs_time(doc, 27.9) == T0 + timedelta(seconds=27.9)
    # the instant the first member's kept audio ends, we are in the SECOND member --
    # 60 s later on the wall clock, not 28 s.
    assert bs.resolve_abs_time(doc, 28.0) == T0 + timedelta(seconds=62.0)
    assert bs.resolve_abs_time(doc, 57.9) == T0 + timedelta(seconds=91.9)
    # nothing lands in the 28 s .. 60 s hole where the dropped chunk used to be
    for t in [i / 10 for i in range(0, 580)]:
        got = bs.resolve_abs_time(doc, t)
        offset = (got - T0).total_seconds()
        assert not (28.0 < offset < 62.0), f"t={t} resolved into the gap at +{offset}"


# ============================================================
# Bucket anchoring (spec 2026-08-13-burst-arrival-defects)
# ============================================================
# Replaying one real 71-minute session into TEST produced 123 batch objects for 153 chunks
# instead of 39, because 153 concurrent workers each planned from a different snapshot of
# "who is left" and so each computed a different anchor. The seal key is the anchor, so two
# anchors covering the same chunks held two different keys and neither saw the other.
#
# A greedy anchor is a function of the snapshot. Under concurrency that is not a function.

def test_the_bucket_is_a_pure_function_of_the_filename():
    """Two workers, different snapshots, same chunk -- one answer. This is the property the
    seal key needs and the greedy anchor cannot provide."""
    t = datetime(2026, 8, 13, 9, 1, 30)
    assert bs.bucket_of(t, 120) == bs.bucket_of(t, 120)
    assert isinstance(bs.bucket_of(t, 120), int)


def test_two_snapshots_plan_the_same_bucket_for_every_common_member():
    """The exact trace from the replay: one worker saw {5,6,7,8}, another saw {0..8}."""
    members = _at(*[(i, i * 30) for i in range(9)])
    a = bs.plan_buckets({i: members[i] for i in (5, 6, 7, 8)}, window_sec=120)
    b = bs.plan_buckets(members, window_sec=120)
    for idx in (5, 6, 7, 8):
        ka = next(k for k, v in a.items() if idx in v)
        kb = next(k for k, v in b.items() if idx in v)
        assert ka == kb, f"chunk {idx} landed in bucket {ka} for one worker and {kb} for another"


def test_naive_base_times_bucket_by_a_fixed_convention_not_the_server_zone(monkeypatch):
    """`datetime.timestamp()` on a naive value interprets it in the SERVER's zone, so the
    same filename would bucket differently on two machines -- which is the determinism this
    whole change exists for, quietly undone."""
    import importlib
    import os as _os
    t = datetime(2026, 8, 13, 9, 1, 30)
    seen = set()
    for tz in ("UTC", "Pacific/Auckland", "America/New_York"):
        monkeypatch.setenv("TZ", tz)
        try:
            import time as _time
            _time.tzset()                      # no-op on Windows; real on the CI runner
        except AttributeError:
            pass
        importlib.reload(bs)
        seen.add(bs.bucket_of(t, 120))
    assert len(seen) == 1, f"the bucket moved with the server timezone: {seen}"
    assert _os.environ  # keep the import meaningful


def test_a_boundary_straddling_run_splits_at_the_bucket_edge():
    """The accepted cost. Greedy would have kept these together; the grid does not, and
    that is the 2% (480 requests against 470) being paid for determinism.

    This REPLACES test_the_window_anchors_on_its_first_member_not_on_a_grid, deleted by name
    in the same commit -- the reversal is deliberate and this pair of edits is its record.
    """
    T = datetime(2026, 8, 13, 9, 0, 0)
    base = bs.bucket_of(T, 120)
    members = {10: T + timedelta(seconds=110), 11: T + timedelta(seconds=140)}
    out = bs.plan_buckets(members, window_sec=120)
    assert len(out) == 2 and bs.bucket_of(members[11], 120) == base + 1


def test_at_most_cap_base_times_fit_one_bucket_at_thirty_seconds():
    """The count cap cannot bind while chunks are 30 s, so a bucket is never split by it."""
    T = datetime(2026, 8, 13, 9, 0, 0)
    members = {i: T + timedelta(seconds=30 * i) for i in range(40)}
    for idxs in bs.plan_buckets(members, window_sec=120).values():
        assert len(idxs) <= 4


def test_a_consumed_member_is_not_placed_in_any_bucket():
    T = datetime(2026, 8, 13, 9, 0, 0)
    members = {i: T + timedelta(seconds=30 * i) for i in range(4)}
    out = bs.plan_buckets(members, window_sec=120, consumed={1, 2})
    assert sorted(i for v in out.values() for i in v) == [0, 3]


# ---- reading a batch window back as raw chunk audio ---------------------
#
# The voiceprint path must read the DEVICE'S OWN upload: every threshold in
# voiceprint_utils was measured on raw audio and does not transfer to the normalised,
# stitched copy under audio_segments/. Batching breaks that by construction -- a batched
# turn's source_filename IS the stitched file and its offsets are batch-relative
# (see `rebase_turns_from_embedded_map`'s docstring) -- so the coordinates have to come back
# map rather than the rule being abandoned.
#
# This arithmetic lives here, next to build_map, for the reason build_map's own docstring
# gives: a caller recomputing offsets independently is a second implementation of the one
# piece of arithmetic that must not drift.


def _map3():
    """Three chunks, 30s each, the first with a 1.5s head trim."""
    ms = [bs.member(0, "audio_segments/u/2026-08-13/x_c0000_off0.0_to30.0_srcwav.wav",
                    "2026-08-13T09:00:00", 1.5, 30.0),
          bs.member(1, "audio_segments/u/2026-08-13/x_c0001_off0.0_to30.0_srcwav.wav",
                    "2026-08-13T09:00:30", 0.5, 30.0),
          bs.member(2, "audio_segments/u/2026-08-13/x_c0002_off0.0_to30.0_srcwav.wav",
                    "2026-08-13T09:01:00", 0.25, 30.0)]
    return bs.build_map("sid1", ms, sealed_by="session_close")


def test_a_window_inside_one_member_maps_to_that_chunk():
    got = bs.locate_in_members(_map3(), 35.0, 40.0)
    assert len(got) == 1
    assert "x_c0001_off" in got[0]["chunk_key"]
    # 35s into the batch is 5s into member 1's KEPT audio, and its kept audio starts
    # trimmed_head_sec into the chunk itself.
    assert got[0]["start_sec"] == pytest.approx(5.0 + 0.5)
    assert got[0]["end_sec"] == pytest.approx(10.0 + 0.5)


def test_the_head_trim_is_added_not_ignored():
    """Member 0 keeps audio from 1.5s in, so batch time 0 is chunk time 1.5 -- dropping the
    trim silently shifts every window by up to a second, which is a different person's
    syllables at a 3s floor."""
    got = bs.locate_in_members(_map3(), 0.0, 4.0)
    assert got[0]["start_sec"] == pytest.approx(1.5)
    assert got[0]["end_sec"] == pytest.approx(5.5)


def test_a_window_spanning_two_members_returns_both_pieces_in_order():
    got = bs.locate_in_members(_map3(), 28.0, 33.0)
    assert [g["chunk_key"].split("_off")[0][-5:] for g in got] == ["c0000", "c0001"]
    assert got[0]["start_sec"] == pytest.approx(28.0 + 1.5)
    assert got[0]["end_sec"] == pytest.approx(30.0 + 1.5)      # to the end of its kept audio
    assert got[1]["start_sec"] == pytest.approx(0.0 + 0.5)     # from the start of the next
    assert got[1]["end_sec"] == pytest.approx(3.0 + 0.5)


def test_a_window_past_the_end_of_the_batch_raises():
    """Silently clipping would hand back a shorter clip than asked for, and a shorter clip
    is a worse voiceprint with nothing anywhere saying why."""
    with pytest.raises(ValueError, match="beyond"):
        bs.locate_in_members(_map3(), 100.0, 110.0)


def test_an_empty_map_raises_rather_than_returning_nothing():
    with pytest.raises(ValueError):
        bs.locate_in_members({"schema": 1, "members": []}, 0.0, 5.0)


def test_the_map_key_for_a_batch_audio_object():
    k = bs.map_key_for_audio(
        "audio_segments/u/2026-08-13/x_c0000_bn4_off0.0_to114.0_srcwav.wav")
    assert k == "audio_segments/u/2026-08-13/x_c0000_bn4_off0.0_to114.0_srcwav_batch_map.json"


def test_a_per_chunk_filename_is_not_batched():
    """The discriminator. A raw per-chunk turn must keep costing nothing -- no map fetch,
    no translation -- and the two shapes are told apart by `_bn`, which build_batch_name
    puts there and nothing else uses."""
    assert bs.is_batched("x_c0000_bn4_off0.0_to114.0_srcwav.wav") is True
    assert bs.is_batched("x_c0000.wav") is False
    assert bs.is_batched("x_c0000_off1.5_to31.5_srcwav.wav") is False
