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

    def delete_item(self, Key=None, ConditionExpression=None,
                    ExpressionAttributeNames=None, ExpressionAttributeValues=None):
        k = (Key["PK"], Key["SK"])
        item = self.items.get(k)
        vals = ExpressionAttributeValues or {}
        if ConditionExpression and (item is None
                                    or item.get("status") != vals.get(":sealing")
                                    or item.get("claimed_at") != vals.get(":mine")):
            raise ConditionalCheckFailedException(k)
        self.items.pop(k, None)

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
    assert len(bl.list_members(table, SID)) == 1


def test_the_same_chunk_delivered_twice_is_not_two_members(table):
    """S3 event notifications are at-least-once. A second delivery that added a member would
    put the same audio in the batch twice and pay to transcribe it.

    Asserted on the MEMBER COUNT, not on the table's write count: registration also writes
    the active-session index, and a raw write count would make that look like a duplicate
    member. What matters is how many members exist."""
    bl.register_chunk(table, SID, 4, "users/…/c0004.wav", NOW)
    assert bl.register_chunk(table, SID, 4, "users/…/c0004.wav", NOW + 5) == "already_present"
    assert len(bl.list_members(table, SID)) == 1


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


def test_a_fresh_bypass_record_is_not_instantly_stale(table):
    """The retry window has to apply to `bypassed` too, and it did not.

    `claim_seal` may retake a stale `bypassed` record because that means the copy-to-self
    never completed. It reads staleness from `claimed_at` -- which `mark_bypassed` never
    wrote, so `now - 0` was always past the window and EVERY bypassed record read as
    abandoned the instant it existed. A singleton bypass at session close coinciding with a
    late arrival is exactly the two-sealers race the window exists to bound: the second
    worker copies again and the chunk is transcribed and paid for twice.
    """
    bl.mark_bypassed(table, SID, 4, NOW)
    assert bl.claim_seal(table, SID, 4, [4], NOW + 400) is None, \
        "a bypass 400s old is still in flight; the window is 900"
    assert bl.claim_seal(table, SID, 4, [4], NOW + 1000) is not None, \
        "past the window it must still be retakeable -- a failed copy has to be recoverable"


# ============================================================
# Bucket keys and the straggler rule (2026-08-13-burst-arrival-defects)
# ============================================================

def test_two_workers_with_different_snapshots_contend_for_one_key(table):
    """Defect A at the ledger layer.

    Under first-index keys, a worker seeing {5,6,7,8} claims SEAL#0005 and a worker seeing
    {0..8} claims SEAL#0004 -- both granted, both sealing chunks 5,6,7. Under bucket keys
    they compute the same SK and exactly one wins.
    """
    rows = _wrows([(i, i * 30) for i in range(9)])
    snap_a = [r for r in rows if int(r["chunk_index"]) in (5, 6, 7, 8)]
    a = bl.pending_buckets(snap_a, NOW + 400, grace_sec=150)
    b = bl.pending_buckets(rows, NOW + 400, grace_sec=150)
    key_a = next(k for k, v in a["ready"] if 6 in v)
    key_b = next(k for k, v in b["ready"] if 6 in v)
    assert key_a == key_b, "the same chunk must be under the same claim for both workers"
    assert bl.claim_seal(table, SID, key_a, [6], NOW) is not None
    assert bl.claim_seal(table, SID, key_b, [6], NOW) is None, "only one may win"


def test_a_member_of_a_sealed_bucket_is_redriven_not_orphaned(table):
    """The trap in the fix. Without this rule the bucket key is WORSE than what it replaces.

    A worker that saw only 3 of a bucket's 4 chunks seals it and marks those consumed. The
    4th then plans into the same bucket, whose claim is `sealed` -- and `claim_seal` refuses
    a sealed record forever, by design. That chunk would never be transcribed by anything:
    duplication traded for silent loss, which is the worse direction.
    """
    rows = _wrows([(8, 0), (9, 30), (10, 60), (11, 90)])
    bucket = next(k for k, v in bl.pending_buckets(rows, NOW + 400, 150)["ready"] if 8 in v)
    bl.claim_seal(table, SID, bucket, [8, 9, 10], NOW)
    bl.mark_sealed(table, SID, bucket, NOW)
    for r in rows[:3]:
        r["sealed_into"] = bucket

    out = bl.pending_buckets(rows, NOW + 400, grace_sec=150, table=table, session_id=SID)
    assert out["stragglers"] == [11], f"chunk 11 must be redriven, got {out}"
    assert not any(11 in v for _, v in out["ready"]), "and never proposed as a claim"


def test_a_bypass_record_is_member_scoped_not_bucket_scoped(table):
    """`_maybe_batch` asks "was THIS chunk bypassed" to decide whether a copy-to-self event
    falls through to transcription. Under bucket keys a lookup by chunk index misses the
    claim entirely, and the re-driven audio vanishes -- so the bypass record has to carry
    the member, not the window."""
    bl.mark_bypassed(table, SID, 11, NOW)
    assert bl.bypass_status(table, SID, 11) == "bypassed"
    assert bl.bypass_status(table, SID, 12) is None


def test_a_legacy_first_index_seal_cannot_shadow_a_bucket_claim(table):
    """Records written before this change use SEAL#0000..SEAL#9999. Bucket numbers are
    epoch/120 -- about 14.9 million -- so the two ranges cannot collide. Tested, because
    'cannot collide' is the kind of claim that is wrong once."""
    bl.claim_seal(table, SID, 5, [5, 6], NOW)           # legacy shape
    bucket = bl.bucket_for_index(_wrows([(5, 0)])[0])
    assert bucket > 9999
    assert bl.claim_seal(table, SID, bucket, [5, 6], NOW) is not None


def test_reaching_the_cap_does_not_seal_a_bucket_that_can_still_grow():
    """Real chunks arrive 28 s apart, not 30, so FIVE fit in a 120 s bucket.

    Treating `len(members) >= cap` as "ready" seals the first four and orphans the fifth
    into a straggler redrive -- one wasted per-chunk transcription per bucket, and on the
    real 153-chunk session ten buckets hold five. The cap bounds the REQUEST; what makes a
    bucket ready is that nothing can still join it.

    Found by computing the buckets offline before re-running the replay, which is the only
    reason it was not paid for again.
    """
    rows = _wrows([(i, i * 28) for i in range(5)])          # all five inside one 120 s bucket
    out = bl.pending_buckets(rows, NOW + 10, grace_sec=150, cap=4)
    assert out["ready"] == [], "the bucket can still grow; the cap is not a reason to seal"

    # A member of the NEXT bucket does NOT make this one ready either -- that rule was
    # tried and removed the same day: during a burst a worker can see a later chunk before
    # this bucket's own members have registered, and it sealed them one at a time.
    rows += _wrows([(5, 140)], at=NOW)
    assert bl.pending_buckets(rows, NOW + 10, grace_sec=150, cap=4)["ready"] == []

    ready = {k: v for k, v in bl.pending_buckets(rows, NOW + 400, grace_sec=150, cap=4)["ready"]}
    assert [0, 1, 2, 3, 4] in ready.values(), f"past grace it seals WHOLE, all five: {ready}"


def test_a_partial_snapshot_spanning_buckets_does_not_seal_an_incomplete_one():
    """The acceptance replay failed on exactly this, and it is a defect I introduced.

    153 chunks arrived at once. Each worker sees only part of the session -- not merely
    because DynamoDB reads are eventually consistent, but because chunk 100's event can be
    processed before chunk 5 has registered at all. A worker holding {5, 100} concluded that
    bucket(5) was over, because it could see a LATER bucket, and sealed it with one member.
    153 chunks became 72 singleton bypasses and ZERO batches.

    Seeing a later member cannot prove this bucket is complete. Only elapsed quiet can.
    """
    rows = _wrows([(5, 150)]) + _wrows([(100, 3000)])
    out = bl.pending_buckets(rows, NOW + 10, grace_sec=150)
    assert out["ready"] == [], \
        f"an incomplete bucket must wait, whatever else is visible: {out}"

    out = bl.pending_buckets(rows, NOW + 400, grace_sec=150)
    assert len(out["ready"]) == 2, "past grace, both seal"


# ============================================================
# The index of sessions that still owe work (prod blocker #1)
# ============================================================

def test_registering_a_chunk_puts_its_session_on_the_active_list(table):
    """Nothing periodic knows which sessions have unfinished batching.

    Sealing runs on chunk arrival; the close pass runs once, and only for sessions in
    `pending_close`. A device whose `/open` never reached the server has no row at all, so
    after a backlog burst every bucket sits registered and unsealed forever -- zero
    transcripts, zero errors. That is the acceptance replay, and it is a real device
    behaviour, not a test artefact.

    A one-partition index makes the periodic pass cheap and precise: no table scan, no S3
    walk, just "who still owes work".
    """
    bl.register_chunk(table, SID, 4, "audio_segments/U/2026-08-13/x_c0004.wav", NOW)
    assert bl.active_sessions(table) == [SID]


def test_a_session_leaves_the_active_list_once_every_member_is_consumed(table):
    """Otherwise the list grows forever and the periodic pass gets slower every day."""
    for i in (4, 5):
        bl.register_chunk(table, SID, i, f"audio_segments/U/2026-08-13/x_c{i:04d}.wav", NOW)
    bl.mark_members_consumed(table, SID, [4, 5], 4)
    bl.retire_if_finished(table, SID)
    assert bl.active_sessions(table) == []


def test_a_session_with_one_unconsumed_member_stays_active(table):
    for i in (4, 5):
        bl.register_chunk(table, SID, i, f"audio_segments/U/2026-08-13/x_c{i:04d}.wav", NOW)
    bl.mark_members_consumed(table, SID, [4], 4)
    bl.retire_if_finished(table, SID)
    assert bl.active_sessions(table) == [SID], "chunk 5 still owes a batch"


def test_the_active_list_survives_a_duplicate_registration(table):
    bl.register_chunk(table, SID, 4, "audio_segments/U/2026-08-13/x_c0004.wav", NOW)
    bl.register_chunk(table, SID, 4, "audio_segments/U/2026-08-13/x_c0004.wav", NOW)
    assert bl.active_sessions(table) == [SID]
