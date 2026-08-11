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


# ---- which runs are ready to seal ----

def _rows(indices, at=NOW):
    return [{"chunk_index": i, "chunk_key": f"k{i}", "registered_at": at} for i in indices]


def test_four_consecutive_chunks_seal_immediately():
    assert bl.pending_runs(_rows([4, 5, 6, 7]), NOW, deadline_sec=300) == [[4, 5, 6, 7]]


def test_three_chunks_that_are_still_young_wait_for_a_fourth():
    assert bl.pending_runs(_rows([4, 5, 6]), NOW + 10, deadline_sec=300) == []


def test_three_chunks_past_the_deadline_seal_short():
    """A session that ended mid-batch must not wait for a fourth chunk that will never come."""
    assert bl.pending_runs(_rows([4, 5, 6]), NOW + 400, deadline_sec=300) == [[4, 5, 6]]


def test_chunk_zero_alone_and_young_seals_nothing():
    assert bl.pending_runs(_rows([0]), NOW + 1, deadline_sec=300) == []


def test_a_gap_does_not_seal_the_earlier_run_early_because_the_hole_may_still_arrive():
    """Uploads arrive out of order and can be hours late. Sealing `[4,5]` the moment `7`
    shows up would permanently exclude a chunk 6 that was merely slow — and a sealed batch
    is never reopened, so that exclusion is forever."""
    rows = _rows([4, 5]) + _rows([7, 8], at=NOW + 5)
    assert bl.pending_runs(rows, NOW + 10, deadline_sec=300) == []


def test_after_the_deadline_the_gap_is_accepted_and_both_runs_seal():
    rows = _rows([4, 5]) + _rows([7, 8], at=NOW + 5)
    assert bl.pending_runs(rows, NOW + 400, deadline_sec=300) == [[4, 5], [7, 8]]


def test_a_dropped_index_behaves_exactly_like_a_gap():
    """`DROP_SILENT_CHUNKS` removes a chunk before this stage. Nothing here can tell that
    apart from a lost upload, and nothing should: both mean the batch stops there."""
    rows = _rows([0, 1, 2]) + _rows([4, 5], at=NOW)
    assert bl.pending_runs(rows, NOW + 400, deadline_sec=300) == [[0, 1, 2], [4, 5]]


def test_a_long_unbroken_stretch_is_cut_into_fours_and_the_tail_waits():
    """Eight chunks give two full batches. Nine give two full batches and a remainder that
    is still young, so the remainder waits rather than being sent alone."""
    assert bl.pending_runs(_rows(range(9)), NOW + 10, deadline_sec=300) == \
        [[0, 1, 2, 3], [4, 5, 6, 7]]


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
