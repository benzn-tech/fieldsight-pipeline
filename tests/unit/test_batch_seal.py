"""Unit: the sealer, driven directly rather than through the transcriber.

Plan: docs/superpowers/plans/2026-08-13-batch-by-wall-clock.md phase 3.
Spec: docs/superpowers/specs/2026-08-13-batch-by-wall-clock.md.

`batch_seal` had no test file of its own — it was exercised only through
`lambda_transcribe` and the finalize sweep, both of which mock the pieces this module is
made of. Two behaviours only appear when the real thing runs:

* **a gap seam has nothing to measure.** Two chunks that were never adjacent share no
  ring-buffer copy, so the overlap between them is genuinely zero. Recording that as
  `trim_measured: false` would be a lie in the one direction that matters: a wall of
  `false` is the alarm for "the byte comparison is broken", and filling it with expected
  zeroes destroys the signal.
* **a window of one is not a batch.** It buys nothing — no request saved, no shared
  speaker namespace — and costs the whole grace wait plus a map and a seal record. 10% of
  batches in the lake are this.

The S3 double records what is written, so a test can never agree with the reader against
the writer — the mistake that let every batched TEST session fall back to filename
arithmetic with the suite green.
"""
import io
import os
import sys
import wave

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "src"))

import batch_seal  # noqa: E402
import batch_stitch as bs  # noqa: E402

SID = "9f8c1e2a4b6d47f0a1b2c3d4e5f60718"
PREFIX = "audio_segments/Ben_UCPK/2026-08-13"
RATE = 16000


def wav(seconds=30.0, value=1000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(value.to_bytes(2, "little", signed=True) * int(seconds * RATE))
    return buf.getvalue()


def unit_key(index, hms="09-00-00"):
    return (f"{PREFIX}/Benl1_2026-08-13_{hms}_sid{SID}_c{index:04d}"
            f"_off0.0_to30.0_srcwav.wav")


def raw_key(index, hms="09-00-00"):
    return f"users/Ben_UCPK/audio/2026-08-13/Benl1_2026-08-13_{hms}_sid{SID}_c{index:04d}.wav"


class _Body:
    def __init__(self, d):
        self._d = d

    def read(self):
        return self._d


class RecordingS3:
    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.puts = []
        self.copies = []

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise KeyError(Key)
        return {"Body": _Body(self.objects[Key])}

    def put_object(self, Bucket, Key, Body, **kw):
        self.puts.append(Key)
        self.objects[Key] = Body

    def copy_object(self, Bucket, Key, CopySource, **kw):
        self.copies.append((Key, CopySource))

    def written(self, suffix):
        return [k for k in self.puts if k.endswith(suffix)]


class FakeTable:
    def __init__(self):
        self.items = {}

    def put_item(self, Item=None, ConditionExpression=None):
        self.items[(Item["PK"], Item["SK"])] = dict(Item)

    def delete_item(self, Key=None, ConditionExpression=None,
                    ExpressionAttributeNames=None, ExpressionAttributeValues=None):
        k = (Key["PK"], Key["SK"])
        item = self.items.get(k)
        vals = ExpressionAttributeValues or {}
        if ConditionExpression and (item is None
                                    or item.get("status") != vals.get(":sealing")
                                    or item.get("claimed_at") != vals.get(":mine")):
            raise type("ConditionalCheckFailedException", (Exception,), {})()
        self.items.pop(k, None)

    def query(self, KeyConditionExpression=None, ExpressionAttributeValues=None):
        pk = ExpressionAttributeValues[":pk"]
        sk = ExpressionAttributeValues.get(":sk", "")
        return {"Items": [v for (p, s), v in sorted(self.items.items())
                          if p == pk and s.startswith(sk)]}


@pytest.fixture
def s3():
    objs = {}
    for i, hms in ((4, "09-00-00"), (6, "09-01-00"), (7, "09-01-30")):
        objs[unit_key(i, hms)] = wav(value=1000 + i)
        objs[raw_key(i, hms)] = wav(value=1000 + i)
    return RecordingS3(objs)


def _by_index(*pairs):
    return {i: {"chunk_key": unit_key(i, hms)} for i, hms in pairs}


# ---- a gap seam is not an unmeasured seam ----

def test_a_gap_seam_is_not_measured_and_not_reported_as_unmeasured(s3, monkeypatch):
    """4 and 6 were never adjacent — 5 was dropped — so there is no overlap to find.

    Reporting `trim_measured: false` here would put an expected zero into the counter
    that exists to detect the byte comparison being broken.
    """
    calls = []
    real = batch_seal.measure_trim
    monkeypatch.setattr(batch_seal, "measure_trim",
                        lambda *a, **k: calls.append(a[3]) or real(*a, **k))

    batch_seal.seal_batch(s3, "b", SID, [4, 6],
                          _by_index((4, "09-00-00"), (6, "09-01-00")),
                          0, FakeTable())

    assert calls == [], f"measure_trim was called across a gap: {calls}"
    doc = _map_written(s3)
    seams = [(m["chunk_index"], m.get("seam"), m["trim_measured"], m["trimmed_head_sec"])
             for m in doc["members"]]
    assert seams == [(4, "first", True, 0.0), (6, "gap", True, 0.0)]


def test_an_adjacent_seam_is_still_measured(s3, monkeypatch):
    """The gap rule must not switch trimming off for the seams that do have an overlap —
    'measured zero means do not trim' makes that failure invisible."""
    calls = []
    monkeypatch.setattr(batch_seal, "measure_trim",
                        lambda *a, **k: calls.append(a[3]) or (2.0, True))
    batch_seal.seal_batch(s3, "b", SID, [6, 7],
                          _by_index((6, "09-01-00"), (7, "09-01-30")),
                          0, FakeTable())
    assert len(calls) == 1, "the 6->7 seam is adjacent and must be measured"
    doc = _map_written(s3)
    assert doc["members"][1]["seam"] == "adjacent"
    assert doc["members"][1]["trimmed_head_sec"] == 2.0


def _map_written(s3):
    import json
    keys = [k for k in s3.puts if k.endswith("_batch_map.json")]
    assert len(keys) == 1, f"expected exactly one map, got {s3.puts}"
    return json.loads(s3.objects[keys[0]])


# ---- a window of one is not a batch ----

def test_a_window_of_one_is_bypassed_not_sealed(s3):
    """No `_bn1` WAV, no map, no second paid request — the chunk goes down the ordinary
    per-chunk path it would have taken if batching were off."""
    table = FakeTable()
    out = batch_seal.seal_batch(s3, "b", SID, [4], _by_index((4, "09-00-00")), 0, table)

    assert s3.written(".wav") == [], f"a batch WAV was written for one chunk: {s3.puts}"
    assert s3.written("_batch_map.json") == []
    assert out is None, "a bypassed window has no batch key"
    seal = [v for (p, sk), v in table.items.items() if sk.startswith("SEAL#")]
    assert len(seal) == 1 and seal[0]["status"] == "bypassed"


def test_a_bypassed_member_is_re_driven_so_its_audio_is_not_lost(s3):
    """Bypassing must hand the chunk back, not drop it.

    The re-drive is a copy onto its own key: the fresh S3 event re-enters the transcriber
    through the ordinary path. Calling the transcriber directly would drag its client into
    the sweep's import graph, which runs every minute.
    """
    batch_seal.seal_batch(s3, "b", SID, [4], _by_index((4, "09-00-00")), 0, FakeTable())
    assert len(s3.copies) == 1
    key, src = s3.copies[0]
    assert key == unit_key(4)
    assert src["Key"] == unit_key(4) and src["Bucket"] == "b"


def test_a_bypassed_member_is_marked_consumed_so_it_is_never_planned_again(s3):
    """Without this the next sweep plans it again, copies it again, and pays again."""
    table = FakeTable()
    import batch_ledger as bl
    bl.register_chunk(table, SID, 4, unit_key(4), 0)
    batch_seal.seal_batch(s3, "b", SID, [4], _by_index((4, "09-00-00")), 0, table)
    rows = bl.list_members(table, SID)
    assert bl.consumed_indices(rows) == {4}


def test_a_window_of_two_is_still_a_real_batch(s3):
    """The bypass is for one member only. Two chunks still share a speaker namespace,
    which is the entire measured benefit of batching."""
    out = batch_seal.seal_batch(s3, "b", SID, [6, 7],
                                _by_index((6, "09-01-00"), (7, "09-01-30")),
                                0, FakeTable())
    assert out is not None and bs.is_batch_key(out)
    assert len(s3.written("_batch_map.json")) == 1


def test_the_re_drive_copy_is_one_s3_will_accept(s3):
    """S3 REFUSES a copy of an object onto its own key unless something changes:

        This copy request is illegal because it is trying to copy an object to itself
        without changing the object's metadata, storage class, ...

    Without `MetadataDirective=REPLACE` the bypass raises on every singleton, the caller's
    guard logs one line, and the chunk is never transcribed.
    """
    captured = {}
    s3.copy_object = lambda **kw: captured.update(kw)
    batch_seal.seal_batch(s3, "b", SID, [4], _by_index((4, "09-00-00")), 0, FakeTable())
    assert captured.get("MetadataDirective") == "REPLACE"
    assert captured.get("ContentType") == "audio/wav", \
        "REPLACE drops the existing metadata, so the type must be restated"


# ---- the seal must mark its members consumed (the writer half of the §0.2 fix) ----

def test_sealing_a_multi_member_window_marks_every_member_consumed(s3):
    """The half that was missing.

    `pending_windows` excludes consumed members and its test proved that — by fabricating
    `sealed_into` on hand-built rows. Nothing checked that the SEALER writes it, and it did
    not: only the singleton bypass did. So the planner kept re-planning sealed members, and
    both consequences are live:

      * a late EARLIER chunk shifts the window's first index, claims a seal key nobody
        holds, and transcribes and bills the whole window a second time;
      * a late INTERIOR chunk re-plans a window whose key IS held, loses the claim, and is
        then never transcribed by anything -- including the session-close sweep, forever.

    The reading side never asked the writer. That is the exact shape of
    `ci-green-over-a-dead-path`.
    """
    import batch_ledger as bl
    table = FakeTable()
    for i, hms in ((6, "09-01-00"), (7, "09-01-30")):
        bl.register_chunk(table, SID, i, unit_key(i, hms), 0)

    batch_seal.seal_batch(s3, "b", SID, [6, 7],
                          _by_index((6, "09-01-00"), (7, "09-01-30")), 0, table)

    assert bl.consumed_indices(bl.list_members(table, SID)) == {6, 7}


def test_a_late_earlier_chunk_cannot_rebuild_a_sealed_window(s3):
    """End to end through the real sealer and the real planner -- the assertion the
    fabricated-rows test could not make."""
    import batch_ledger as bl
    table = FakeTable()
    for i, hms in ((6, "09-01-00"), (7, "09-01-30")):
        bl.register_chunk(table, SID, i, unit_key(i, hms), 0)
    batch_seal.seal_batch(s3, "b", SID, [6, 7],
                          _by_index((6, "09-01-00"), (7, "09-01-30")), 0, table)

    bl.register_chunk(table, SID, 4, unit_key(4, "09-00-00"), 5000)   # an hour late
    got = bl.pending_windows(bl.list_members(table, SID), 9000, 150)
    assert got == [[4]], f"the sealed window must not be re-planned, got {got}"


# ---- a failed seal must not strand its claim ----

def test_a_failed_seal_releases_its_claim_so_the_next_tick_can_retry(s3):
    """A seal that raises leaves a `sealing` claim, and that claim blocks every retry for
    `SEAL_RETRY_SECONDS` — fifteen minutes.

    For a session's TAIL that is not a delay, it is permanent: `_seal_tail_batches` runs
    once per session at finalize and nothing ever calls it again, so the 900-second takeover
    never fires and those chunks are never transcribed. It has happened twice on TEST, both
    times a missing S3 grant, both times found by reading logs rather than by anything
    failing.

    Releasing on failure makes a transient fault retryable on the NEXT tick instead of
    after 900 seconds, which is what makes any retry design possible at all.
    """
    import batch_ledger as bl
    table = FakeTable()
    for i, hms in ((6, "09-01-00"), (7, "09-01-30")):
        bl.register_chunk(table, SID, i, unit_key(i, hms), 0)

    class Exploding(RecordingS3):
        def put_object(self, Bucket, Key, Body, **kw):
            raise RuntimeError("AccessDenied")

    with pytest.raises(RuntimeError):
        batch_seal.seal_batch(Exploding(s3.objects), "b", SID, [6, 7],
                              _by_index((6, "09-01-00"), (7, "09-01-30")), 100, table)

    assert bl.seal_status(table, SID, 6) is None, \
        "the claim must be gone, not left as `sealing` for another 900 seconds"
    assert bl.consumed_indices(bl.list_members(table, SID)) == set(), \
        "and nothing may be marked consumed by a seal that produced no artifact"

    # the retry, on the very next tick rather than in fifteen minutes
    out = batch_seal.seal_batch(s3, "b", SID, [6, 7],
                                _by_index((6, "09-01-00"), (7, "09-01-30")), 101, table)
    assert out is not None and bs.is_batch_key(out)


def test_a_release_does_not_touch_a_claim_someone_else_now_holds(s3):
    """The release is conditional on the claim still being ours. Deleting unconditionally
    would let a slow failing worker wipe the claim of the worker that took over from it,
    and then both would seal."""
    import batch_ledger as bl
    table = FakeTable()
    bl.claim_seal(table, SID, 6, [6, 7], 100)
    table.items[(f"BATCH#{SID}", "SEAL#0006")]["claimed_at"] = 999   # taken over
    bl.release_claim(table, SID, 6, 100)
    assert bl.seal_status(table, SID, 6) == "sealing", "the newer claim must survive"


# ============================================================
# Bucket keys end to end (2026-08-13-burst-arrival-defects, phase 3)
# ============================================================

def test_a_burst_of_interleaved_workers_produces_one_seal_per_bucket(s3, monkeypatch):
    """The 123-vs-39 defect as a unit test.

    The defect needs a STALE snapshot: a worker that plans from the member list as it was
    when its own event arrived, while other members have registered since. A loop that
    registers and then immediately seals cannot produce it -- every worker sees everything,
    the greedy anchor is stable, and the test passes for the wrong reason. (It did, first
    time round.) So each worker's snapshot is frozen at its own step and replayed
    afterwards, which is what 153 concurrent Lambdas actually do.

    Under the greedy anchor this writes overlapping batches. Under bucket keys it writes
    exactly one per bucket, and every member is accounted for exactly once.
    """
    import random

    import batch_ledger as bl
    table = FakeTable()
    n = 12
    keys = {}
    for i in range(n):
        hms = f"09-{i // 2:02d}-{(i % 2) * 30:02d}"
        keys[i] = unit_key(i, hms)
        s3.objects[keys[i]] = wav()

    order = list(range(n))
    random.Random(7).shuffle(order)
    snapshots = []
    for i in order:
        bl.register_chunk(table, SID, i, keys[i], 100 + i)
        snapshots.append(list(bl.list_members(table, SID)))     # frozen, as this worker saw it

    real = bl.list_members
    for snap in snapshots:
        monkeypatch.setattr(bl, "list_members", lambda t, sid, _s=snap: _s)
        batch_seal.seal_ready_runs(s3, "b", SID, table, 500, 150, 4, window_sec=120.0)
    monkeypatch.setattr(bl, "list_members", real)
    batch_seal.seal_ready_runs(s3, "b", SID, table, 99999, 0, 4, window_sec=120.0)

    wavs = [k for k in s3.puts if k.endswith(".wav") and "_bn" in k]
    expected = len({bl.bucket_for_index({"chunk_key": keys[i]}) for i in range(n)})
    assert len(wavs) == expected, f"expected {expected} batches, wrote {len(wavs)}: {wavs}"
    assert bl.consumed_indices(bl.list_members(table, SID)) == set(range(n)),         "every member must be accounted for exactly once"


def test_a_straggler_of_a_sealed_bucket_is_redriven_not_left(s3):
    """The half that makes the bucket key safe. Without it this member is orphaned."""
    import batch_ledger as bl
    table = FakeTable()
    keys = {i: unit_key(i, f"09-00-{i * 30:02d}" if i < 2 else f"09-01-{(i - 2) * 30:02d}")
            for i in range(4)}
    for i, k in keys.items():
        s3.objects[k] = wav()
    for i in (0, 1, 2):
        bl.register_chunk(table, SID, i, keys[i], 100)
    batch_seal.seal_ready_runs(s3, "b", SID, table, 9999, 0, 4, window_sec=120.0)

    bl.register_chunk(table, SID, 3, keys[3], 200)      # arrives after its bucket sealed
    batch_seal.seal_ready_runs(s3, "b", SID, table, 9999, 0, 4, window_sec=120.0)

    assert 3 in bl.consumed_indices(bl.list_members(table, SID)), \
        "the straggler must be consumed, not left to be re-planned forever"
    assert bl.bypass_status(table, SID, 3) == "bypassed"
    assert any(k == keys[3] for k, _ in s3.copies), "and re-driven down the per-chunk path"


# ---- a batch member is a VAD unit, not the device's upload ---------------
#
# Found by invoking the deployed function, not by a test: the batch map's `chunk_key` points
# at `audio_segments/…_off{T}_to{E}_srcwav.wav`, the VAD unit — NOT at `users/…`. The unit
# tests had fabricated maps carrying `users/` keys, so they agreed with the assumption rather
# than with what the seal actually writes. `map_key_for_audio`'s own docstring records the
# same shape of accident happening once before.
#
# Two conversions, and the second is the one that would have gone unnoticed: the unit itself
# begins `_off{T}` into the raw chunk, so a position inside the unit is NOT a position inside
# the chunk. Every member in test currently carries off0.0 (whole-chunk transcription is on),
# which is exactly why a test using a real-looking zero would have proved nothing.


def test_a_position_inside_a_unit_becomes_a_position_inside_the_raw_chunk():
    key, s, e = batch_seal.raw_window_for_member(
        "audio_segments/u/2026-08-13/x_c0001_off12.5_to42.5_srcwav.wav", 5.0, 9.0)
    assert key == "users/u/audio/2026-08-13/x_c0001.wav"
    assert (s, e) == (17.5, 21.5), "the unit's own offset was dropped"


def test_the_zero_offset_case_that_test_currently_produces():
    key, s, e = batch_seal.raw_window_for_member(
        "audio_segments/u/2026-08-13/x_c0000_off0.0_to30.0_srcwav.wav", 5.0, 9.0)
    assert key == "users/u/audio/2026-08-13/x_c0000.wav"
    assert (s, e) == (5.0, 9.0)


def test_a_member_key_that_is_not_a_unit_raises():
    """Silently returning the key unchanged would read the normalised audio, on which none
    of the voiceprint thresholds hold — plausible numbers instead of an error."""
    with pytest.raises(ValueError):
        batch_seal.raw_window_for_member("nonsense.wav", 0.0, 1.0)


def test_a_singleton_bypass_closes_the_bucket_claim_it_took(s3):
    """The claim is on the BUCKET; the bypass records are on the MEMBER. Nothing closed the
    claim, so it sat at `sealing` forever.

    Consequence: a sibling chunk of that same bucket arriving later plans, finds a
    fresh-looking `sealing` record, and `claim_seal` refuses. Within the 900-second takeover
    window -- which includes the one-shot close pass -- that chunk is never sealed, never
    bypassed and never transcribed.
    """
    import batch_ledger as bl
    table = FakeTable()
    bl.register_chunk(table, SID, 4, unit_key(4, "09-00-00"), 0)
    bucket = bl.bucket_for_index({"chunk_key": unit_key(4, "09-00-00")})

    batch_seal.seal_batch(s3, "b", SID, [4], _by_index((4, "09-00-00")), 100, table,
                          claim_key=bucket)

    assert bl.seal_status(table, SID, bucket) == "bypassed", \
        "the bucket claim must be closed, not left in flight"

    # and a sibling that turns up later can still be dealt with
    bl.register_chunk(table, SID, 5, unit_key(5, "09-00-30"), 200)
    rows = bl.list_members(table, SID)
    out = bl.pending_buckets(rows, 100000, grace_sec=150, table=table, session_id=SID)
    assert out["stragglers"] == [5] or any(5 in v for _, v in out["ready"]), \
        f"chunk 5 must be actionable, not stuck behind a dead claim: {out}"
