"""Unit: which chunks are in a batch, and who gets to seal it.

Plan: docs/superpowers/plans/2026-08-11-batched-transcription.md phase 2.

The ledger's whole job is to make two races safe, and both of them are races whose losing
side is silent:

* **duplicate S3 delivery** — S3 event notifications are at-least-once. A chunk registered
  twice must not become two members, and must not cost a second paid transcription.
* **two sealers** — a batch can be sealed by the arrival that completes it or by the sweep
  that notices it timed out, and those can happen at the same instant. Two winners means the
  same two minutes of audio transcribed twice, billed twice, and written to two artifacts.

There is no DynamoDB here. The fake below implements exactly the two conditional writes the
module performs, and nothing else.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "src"))

import batch_ledger as bl  # noqa: E402

SID = "9f8c1e2a4b6d47f0a1b2c3d4e5f60718"
NOW = 1_754_870_000


class ConditionalCheckFailedException(Exception):
    """Same name as boto3's, because that name is how the module recognises a lost race."""


class FakeTable:
    """The smallest thing that behaves like the real table for these two writes.

    `attribute_not_exists(SK)` is honoured literally: the write fails if the item is already
    there. Anything else raises, so a condition the module invents without a test is loud
    rather than quietly permissive.
    """

    def __init__(self):
        self.items = {}
        self.writes = 0

    def put_item(self, Item=None, ConditionExpression=None):
        key = (Item["PK"], Item["SK"])
        if ConditionExpression == "attribute_not_exists(SK)":
            if key in self.items:
                raise ConditionalCheckFailedException(key)
        elif ConditionExpression is not None:
            raise AssertionError(f"unfaked condition: {ConditionExpression!r}")
        self.items[key] = dict(Item)
        self.writes += 1

    def query(self, KeyConditionExpression=None, ExpressionAttributeValues=None):
        pk = ExpressionAttributeValues[":pk"]
        prefix = (ExpressionAttributeValues or {}).get(":sk", "")
        return {"Items": [v for (k, sk), v in sorted(self.items.items())
                          if k == pk and sk.startswith(prefix)]}


@pytest.fixture
def table():
    return FakeTable()


# ---- registration is idempotent ----

def test_a_chunk_registers_once(table):
    assert bl.register_chunk(table, SID, 4, "users/…/c0004.wav", NOW) == "registered"
    assert table.writes == 1


def test_the_same_chunk_delivered_twice_is_not_two_members(table):
    """S3 event notifications are at-least-once. A second delivery that added a member would
    put the same audio in the batch twice and pay to transcribe it."""
    bl.register_chunk(table, SID, 4, "users/…/c0004.wav", NOW)
    assert bl.register_chunk(table, SID, 4, "users/…/c0004.wav", NOW + 5) == "already_present"
    assert table.writes == 1


def test_members_come_back_in_index_order_however_they_arrived(table):
    for i in (7, 4, 6, 5):
        bl.register_chunk(table, SID, i, f"k{i}", NOW)
    assert [m["chunk_index"] for m in bl.list_members(table, SID)] == [4, 5, 6, 7]


def test_one_session_never_sees_another_session_s_chunks(table):
    other = "0" * 32
    bl.register_chunk(table, SID, 1, "a", NOW)
    bl.register_chunk(table, other, 2, "b", NOW)
    assert [m["chunk_index"] for m in bl.list_members(table, SID)] == [1]


# ---- which windows are ready to seal ----
#
# The consecutive-index tests that lived here were removed with `pending_runs` on
# 2026-08-13. They asserted that a gap ends the run, which is the behaviour this
# change exists to remove; keeping them would have pinned the old rule in place.
# Their replacements are in the window section below.


# ---- sealing is single-winner ----

def test_the_first_sealer_wins(table):
    claim = bl.claim_seal(table, SID, 4, [4, 5, 6, 7], NOW)
    assert claim is not None and claim["status"] == "sealing"


def test_the_second_sealer_loses_cleanly_rather_than_double_billing(table):
    """Arrival-sealing and sweep-sealing can fire at the same instant. Two winners means the
    same two minutes transcribed twice and two artifacts written."""
    assert bl.claim_seal(table, SID, 4, [4, 5, 6, 7], NOW) is not None
    assert bl.claim_seal(table, SID, 4, [4, 5, 6, 7], NOW + 1) is None


def test_a_claim_abandoned_mid_way_can_be_re_driven_after_the_retry_window(table):
    """The order is claim → write map → write WAV → mark sealed, because the WAV event is
    what triggers transcription. A crash in the middle leaves a `sealing` claim and no
    artifact, and nothing else would ever look at that batch again."""
    bl.claim_seal(table, SID, 4, [4, 5, 6, 7], NOW)
    late = NOW + bl.SEAL_RETRY_SECONDS + 1
    assert bl.claim_seal(table, SID, 4, [4, 5, 6, 7], late) is not None


def test_a_claim_that_already_finished_is_never_re_driven(table):
    """Re-driving a completed seal buys a second paid transcription for a batch that is
    already correct."""
    bl.claim_seal(table, SID, 4, [4, 5, 6, 7], NOW)
    bl.mark_sealed(table, SID, 4, NOW + 5)
    late = NOW + bl.SEAL_RETRY_SECONDS + 1000
    assert bl.claim_seal(table, SID, 4, [4, 5, 6, 7], late) is None


def test_two_different_batches_of_one_session_do_not_block_each_other(table):
    assert bl.claim_seal(table, SID, 0, [0, 1, 2, 3], NOW) is not None
    assert bl.claim_seal(table, SID, 4, [4, 5, 6, 7], NOW) is not None


# ============================================================
# Windows (spec 2026-08-13): consumed members, two clocks, early seal
# ============================================================
# `pending_runs` groups consecutive indices; a VAD-dropped chunk therefore ends the batch,
# which shatters 31% of real sessions into short runs. `pending_windows` groups by wall
# clock instead. Three things it must get right, each of which fails silently:
#
#   * a member sealed into a batch is never planned again -- otherwise a late chunk with an
#     EARLIER index shifts the window's first index, claims a seal key nobody holds, and
#     bills that window's audio a second time;
#   * membership is device-clock, readiness is server-clock. They are ~13 hours apart;
#   * the common case still seals on arrival. Making everything wait for grace would add
#     the 150 s this change exists to remove -- to 383 batches, to save it on 26.

from datetime import datetime, timedelta  # noqa: E402

BASE = datetime(2026, 8, 13, 9, 0, 0)


def _wrows(offsets_by_index, at=NOW, base=BASE, sealed=()):
    """Ledger rows whose chunk_key carries a real device base time.

    The base time comes out of the filename, exactly as it does in production -- passing a
    parsed datetime in here instead would let a filename format the parser cannot read pass
    a test it should fail.
    """
    rows = []
    for idx, off in offsets_by_index:
        t = (base + timedelta(seconds=off)).strftime("%Y-%m-%d_%H-%M-%S")
        d, hms = t.split("_")
        row = {"chunk_index": idx,
               "chunk_key": f"audio_segments/Ben_UCPK/{d}/Benl1_{d}_{hms}_sid{SID}"
                            f"_c{idx:04d}_off0.0_to30.0_srcwav.wav",
               "registered_at": at}
        if idx in sealed:
            row["sealed_into"] = min(sealed)
        rows.append(row)
    return rows


def test_a_late_chunk_never_rebuilds_a_batch_that_already_sealed():
    """The double-billing pin.

    Members 4 and 5 sealed as SEAL#0004. Chunk 3 arrives an hour later. The old planner
    proposes [3,4,5], which claims SEAL#0003 -- a key nobody holds -- and transcribes,
    bills and renders 4 and 5 a second time. Reproduced against the real `pending_runs`
    before this test was written.
    """
    rows = (_wrows([(4, 0), (5, 30)], at=NOW, sealed={4, 5})
            + _wrows([(3, -30)], at=NOW + 3600))
    got = bl.pending_windows(rows, NOW + 4000, grace_sec=150, window_sec=120)
    assert got == [[3]], f"a sealed window must never be re-planned, got {got}"


def test_readiness_is_judged_on_registration_time_not_the_device_clock():
    """A backlogged session uploads hours after it was recorded. Judging readiness on the
    filename clock would find the window long closed and seal the first chunk alone with
    zero effective grace -- excluding sisters that upload seconds later."""
    rows = _wrows([(0, 0)], at=NOW, base=BASE - timedelta(hours=10))
    assert bl.pending_windows(rows, NOW + 10, grace_sec=150, window_sec=120) == []
    assert bl.pending_windows(rows, NOW + 200, grace_sec=150, window_sec=120) == [[0]]


def test_a_full_window_with_its_successor_registered_seals_without_waiting():
    """Nothing more can join a contiguous window whose successor is already outside it, so
    making it wait for grace would add latency to the common case for no gain."""
    rows = _wrows([(0, 0), (1, 30), (2, 60), (3, 90), (4, 120)])
    got = bl.pending_windows(rows, NOW, grace_sec=150, window_sec=120, cap=8)
    assert got[0] == [0, 1, 2, 3]


def test_the_count_cap_also_seals_without_waiting():
    rows = _wrows([(0, 0), (1, 10), (2, 20), (3, 30)])
    assert bl.pending_windows(rows, NOW, grace_sec=150, window_sec=120, cap=4) == \
        [[0, 1, 2, 3]]


def test_a_window_with_an_interior_hole_always_waits_for_grace():
    """5 is either a VAD drop that is never coming or an upload that is merely slow, and no
    event distinguishes them. Grace is the only arbiter, so the hole must not seal early
    even though 7 proves the window is over."""
    rows = _wrows([(4, 0), (6, 60)]) + _wrows([(7, 150)], at=NOW + 5)
    assert bl.pending_windows(rows, NOW + 10, grace_sec=150, window_sec=120) == []
    got = bl.pending_windows(rows, NOW + 400, grace_sec=150, window_sec=120)
    assert got[0] == [4, 6], "past grace the gap is accepted and they travel together"


def test_a_vad_dropped_chunk_no_longer_ends_the_batch(table):
    """The whole point, at the ledger layer: `pending_runs` returns [[4],[6,7]] for this."""
    rows = _wrows([(4, 0), (6, 60), (7, 90)])
    assert bl.pending_windows(rows, NOW + 400, grace_sec=150, window_sec=120) == [[4, 6, 7]]


def test_sealing_marks_every_member_consumed(table):
    for i in (4, 5, 6, 7):
        bl.register_chunk(table, SID, i, f"k{i}", NOW)
    bl.mark_members_consumed(table, SID, [4, 5, 6, 7], 4)
    rows = bl.list_members(table, SID)
    assert all(r.get("sealed_into") == 4 for r in rows)
    assert bl.consumed_indices(rows) == {4, 5, 6, 7}


def test_marking_consumed_keeps_the_member_row_readable(table):
    """The row is the only record of which chunk_key a batch member was. Overwriting it
    with a bare marker would make a sealed batch un-diagnosable."""
    bl.register_chunk(table, SID, 4, "audio_segments/x/c0004.wav", NOW)
    bl.mark_members_consumed(table, SID, [4], 4)
    row = bl.list_members(table, SID)[0]
    assert row["chunk_key"] == "audio_segments/x/c0004.wav"
    assert row["registered_at"] == NOW


def test_a_member_whose_filename_cannot_be_read_is_still_planned():
    """It must not vanish.

    Membership needs a base time out of the filename. A key the parser cannot read has no
    time, and dropping it from the plan would leave a registered chunk that nothing ever
    seals and nothing ever transcribes — audio gone, with no error anywhere. It gets its
    own window instead: one extra request, and the audio survives.
    """
    rows = _wrows([(4, 0), (5, 30)]) + [
        {"chunk_index": 6, "chunk_key": "audio_segments/x/not-a-chunk-name.wav",
         "registered_at": NOW}]
    got = bl.pending_windows(rows, NOW + 400, grace_sec=150, window_sec=120)
    planned = sorted(i for w in got for i in w)
    assert planned == [4, 5, 6], f"chunk 6 disappeared from the plan: {got}"


def test_an_unplaceable_member_beside_a_placeable_successor_does_not_crash():
    """`_cannot_grow` looked up the window's anchor in the base-time table, and an
    unplaceable window's anchor is not in it.

    The consequence is not one lost chunk: `pending_windows` raises, every arrival for that
    session errors, and `_seal_tail_batches` swallows the exception -- so the whole
    session's pending audio is never sealed and never transcribed. The earlier guard test
    passed only because its fixture happened to have no successor index.
    """
    rows = ([{"chunk_index": 6, "chunk_key": "audio_segments/x/not-a-chunk-name.wav",
              "registered_at": NOW}]
            + _wrows([(7, 30)]))
    got = bl.pending_windows(rows, NOW + 400, grace_sec=150, window_sec=120)
    assert sorted(i for w in got for i in w) == [6, 7]
