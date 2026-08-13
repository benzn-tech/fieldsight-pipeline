"""Unit: batching four chunks into one transcription request, inside lambda_transcribe.

Plan: docs/superpowers/plans/2026-08-11-batched-transcription.md phase 3.

The flag defaults off in code, template and both workflows, so merging this changes nothing
anywhere. What these tests pin is the two directions of mis-detection, because both fail in
silence:

* a **member** key transcribed while batching is on → the audio is transcribed twice and
  paid for twice, and the batch that was supposed to replace it still arrives;
* a **batch** key registered as a member → two minutes of audio are never transcribed at
  all, with no error anywhere. The session simply reads quieter.

Assertions are on the fakes' call lists, never on log text.
"""
import io
import json
import wave

import pytest

mod = pytest.importorskip("lambda_transcribe")
import batch_stitch as bs  # noqa: E402

SID = "9f8c1e2a4b6d47f0a1b2c3d4e5f60718"
USER, DATE = "Ben_UCPK", "2026-08-11"


def unit_key(idx, hh="14-18-47"):
    """A VAD unit for one chunk — whole-chunk mode, so one unit per chunk."""
    return (f"audio_segments/{USER}/{DATE}/Benl1_{DATE}_{hh}_sid{SID}_c{idx:04d}"
            f"_off0.0_to30.0_srcwav.wav")


def raw_key(idx, hh="14-18-47"):
    return f"users/{USER}/audio/{DATE}/Benl1_{DATE}_{hh}_sid{SID}_c{idx:04d}.wav"


def _results(resp):
    """The handler returns {statusCode, body:json}; the per-key results live inside."""
    return json.loads(resp["body"])["results"]


def _event(key):
    return {"Records": [{"s3": {"bucket": {"name": "b"}, "object": {"key": key}}}]}


def _wav(seed, seconds=2.0, rate=16000):
    """A real 16 kHz mono WAV — the implementation parses these, and a fixture that only
    looks like one would test a different code path."""
    import random
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(random.Random(seed).randbytes(int(seconds * rate) * 2))
    return buf.getvalue()


class _Body:
    def __init__(self, data):
        self._d = data

    def read(self):
        return self._d


class FakeS3:
    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.puts = []
        self.gets = []
        self.copies = []

    def get_object(self, Bucket, Key):
        self.gets.append(Key)
        if Key not in self.objects:
            raise KeyError(Key)
        return {"Body": _Body(self.objects[Key])}

    def put_object(self, Bucket, Key, Body, **kw):
        self.puts.append({"Key": Key, "Body": Body})
        self.objects[Key] = Body

    def copy_object(self, Bucket, Key, CopySource, **kw):
        self.copies.append(Key)

    def put_keys(self, suffix=None):
        return [p["Key"] for p in self.puts
                if suffix is None or p["Key"].endswith(suffix)]


class FakeLedger:
    """Stands in for batch_ledger: same call shapes, in-memory."""

    def __init__(self):
        self.members = {}
        self.seals = {}
        self.registered = []
        self.bypassed = set()

    def register_chunk(self, table, session_id, index, chunk_key, now):
        self.registered.append(index)
        if index in self.members:
            return "already_present"
        self.members[index] = {"chunk_index": index, "chunk_key": chunk_key,
                               "registered_at": now}
        return "registered"

    def list_members(self, table, session_id):
        return [self.members[i] for i in sorted(self.members)]

    def pending_windows(self, rows, now, grace_sec, window_sec=120.0, cap=4):
        import batch_ledger
        return batch_ledger.pending_windows(rows, now, grace_sec,
                                            window_sec=window_sec, cap=cap)

    def pending_buckets(self, rows, now, grace_sec, window_sec=120.0, cap=4,
                        table=None, session_id=None):
        """Real planner, but the straggler lookup is answered from this fake's own seals.

        `seal_ready_runs` hands through the module-level DynamoDB table, which in this
        fixture is a real boto3 resource — letting the real `seal_status` reach for it would
        make these tests depend on AWS."""
        import batch_ledger

        class _T:
            def __init__(self, seals): self.seals = seals
            def query(self_inner, KeyConditionExpression=None, ExpressionAttributeValues=None):
                sk = (ExpressionAttributeValues or {}).get(":sk", "")
                for k, v in self_inner.seals.items():
                    if sk.endswith(f"{k:04d}"):
                        return {"Items": [v]}
                return {"Items": []}
        return batch_ledger.pending_buckets(rows, now, grace_sec, window_sec=window_sec,
                                            cap=cap, table=_T(self.seals),
                                            session_id=session_id)

    def bucket_for_index(self, row, window_sec=120.0):
        import batch_ledger
        return batch_ledger.bucket_for_index(row, window_sec)

    def seal_status(self, table, session_id, key):
        return (self.seals.get(key) or {}).get("status")

    def consumed_indices(self, rows):
        import batch_ledger
        return batch_ledger.consumed_indices(rows)

    def mark_members_consumed(self, table, session_id, indices, first_index):
        for i in indices:
            self.members[i]["sealed_into"] = first_index

    def mark_bypassed(self, table, session_id, index, now):
        self.seals[index] = {"status": "bypassed"}
        self.bypassed.add(index)

    def claim_seal(self, table, session_id, first_index, members, now):
        if first_index in self.seals:
            return None
        self.seals[first_index] = {"status": "sealing"}
        return self.seals[first_index]

    def mark_sealed(self, table, session_id, first_index, now):
        self.seals[first_index] = {"status": "sealed"}

    def bypass_status(self, table, session_id, index):
        return "bypassed" if index in self.bypassed else None


@pytest.fixture
def wired(monkeypatch):
    """Batching ON, whole-chunk ON, a fake S3 holding four units and four raws."""
    objects = {}
    for i in range(4):
        objects[unit_key(i)] = _wav(i)
        objects[raw_key(i)] = _wav(i)
    s3 = FakeS3(objects)
    ledger = FakeLedger()
    monkeypatch.setattr(mod, "s3", s3)
    monkeypatch.setattr(mod, "BATCH_TRANSCRIPTION", True)
    monkeypatch.setattr(mod, "TRANSCRIBE_WHOLE_CHUNK", True)
    monkeypatch.setattr(mod, "BATCH_MAX_CHUNKS", 4)
    # Both: the handler registers members through its own import, and `batch_seal` holds a
    # separate one. Patching only the handler's leaves the seal talking to real DynamoDB —
    # which is how this fixture first failed, with an auth error rather than a wrong answer.
    import batch_seal
    monkeypatch.setattr(mod, "batch_ledger", ledger, raising=False)
    monkeypatch.setattr(batch_seal, "batch_ledger", ledger, raising=False)
    calls = []
    monkeypatch.setattr(mod, "ASR_PROVIDER", "elevenlabs")
    import elevenlabs_utils
    monkeypatch.setattr(elevenlabs_utils, "transcribe_segment",
                        lambda *a, **k: calls.append(a[1]) or
                        {"results": {"transcripts": [{"transcript": "hi"}], "items": []}})
    return monkeypatch, s3, ledger, calls


# ---- the flag is genuinely a rollback ----

def test_with_the_flag_off_a_member_transcribes_exactly_as_today(wired):
    mp, s3, ledger, calls = wired
    mp.setattr(mod, "BATCH_TRANSCRIPTION", False)
    mod.lambda_handler(_event(unit_key(0)), None)
    assert len(calls) == 1, "the existing path must be untouched"
    assert ledger.registered == [], "nothing may be registered when the flag is off"
    assert s3.put_keys("_batch_map.json") == []


# ---- accumulating ----

def test_an_incomplete_run_registers_and_transcribes_nothing(wired):
    mp, s3, ledger, calls = wired
    out = _results(mod.lambda_handler(_event(unit_key(0)), None))
    assert ledger.registered == [0]
    assert calls == [], "transcribing the member defeats the whole point"
    assert s3.put_keys() == []
    assert out[0]["status"] == "batched_pending"


def test_the_fourth_member_produces_a_batch_and_its_map(wired):
    mp, s3, ledger, calls = wired
    for i in range(4):
        mod.lambda_handler(_event(unit_key(i)), None)
    # Grace elapses only now. Setting it to zero BEFORE the loop makes the first chunk a
    # bucket of one and bypasses it -- which is correct behaviour and the wrong test.
    mp.setattr(mod, "BATCH_SEAL_DEADLINE_SEC", 0)
    mod.lambda_handler(_event(unit_key(3)), None)
    maps = s3.put_keys("_batch_map.json")
    wavs = [k for k in s3.put_keys(".wav") if bs.is_batch_key(k)]
    assert len(maps) == 1 and len(wavs) == 1
    assert calls == [], "the batch WAV is transcribed by its own S3 event, not here"
    doc = json.loads(s3.puts[[p["Key"] for p in s3.puts].index(maps[0])]["Body"])
    assert [m["chunk_index"] for m in doc["members"]] == [0, 1, 2, 3]


def test_the_map_is_written_before_the_wav_that_triggers_transcription(wired):
    """The WAV's S3 event is what starts the transcription, so a crash between the
    two must leave no batch whose map is missing."""
    mp, s3, ledger, calls = wired
    for i in range(4):
        mod.lambda_handler(_event(unit_key(i)), None)
    mp.setattr(mod, "BATCH_SEAL_DEADLINE_SEC", 0)
    mod.lambda_handler(_event(unit_key(3)), None)
    keys = s3.put_keys()
    # `is_batch_key` matches the map too — it carries the same `_bn{K}` stem — so the WAV
    # has to be selected by extension as well. Harmless in the handler (a .json has no
    # media format and is skipped), but it would make this ordering test pass vacuously.
    assert keys.index([k for k in keys if k.endswith("_batch_map.json")][0]) < \
        keys.index([k for k in keys if bs.is_batch_key(k) and k.endswith(".wav")][0])


# ---- the two mis-detections, both silent ----

def test_a_batch_object_is_transcribed_and_never_registered_as_a_member(wired):
    mp, s3, ledger, calls = wired
    stem = f"Benl1_{DATE}_14-18-47_sid{SID}_c0000"
    bkey = f"audio_segments/{USER}/{DATE}/" + bs.build_batch_name(stem, 4, 0.0, 114.0)
    s3.objects[bkey] = _wav(9)
    mod.lambda_handler(_event(bkey), None)
    assert len(calls) == 1, "a batch that is never transcribed is two silent minutes"
    assert ledger.registered == []


def test_a_member_is_never_transcribed_while_batching_is_on(wired):
    mp, s3, ledger, calls = wired
    mod.lambda_handler(_event(unit_key(1)), None)
    assert calls == []


# ---- the shapes batching must not touch ----

def test_a_legacy_key_with_no_session_id_transcribes_normally(wired):
    mp, s3, ledger, calls = wired
    key = f"audio_segments/{USER}/{DATE}/Benl1_{DATE}_10-30-00_off0.0_to5.0_srcwav.wav"
    s3.objects[key] = _wav(8)
    mod.lambda_handler(_event(key), None)
    assert len(calls) == 1 and ledger.registered == []


def test_segment_mode_falls_back_to_per_chunk_rather_than_batching_fragments(wired):
    """Batching assumes one unit per chunk. With whole-chunk off a chunk yields several
    speech islands, and treating one of them as 'the chunk' would silently drop the rest."""
    mp, s3, ledger, calls = wired
    mp.setattr(mod, "TRANSCRIBE_WHOLE_CHUNK", False)
    mod.lambda_handler(_event(unit_key(0)), None)
    assert len(calls) == 1 and ledger.registered == []


# ---- duplicates and damage ----

def test_a_duplicate_delivery_of_the_same_member_is_a_no_op(wired):
    mp, s3, ledger, calls = wired
    mod.lambda_handler(_event(unit_key(0)), None)
    out = _results(mod.lambda_handler(_event(unit_key(0)), None))
    assert ledger.registered == [0, 0]
    assert len(ledger.members) == 1
    assert out[0]["status"] == "batched_pending"
    assert s3.put_keys() == []


def test_an_unreadable_raw_upload_does_not_block_the_batch(wired):
    """The trim is measured on the raw upload. If it is missing — and 0.9% of them have
    been — the batch must still go out with nothing trimmed at that seam."""
    mp, s3, ledger, calls = wired
    del s3.objects[raw_key(2)]
    for i in range(4):
        mod.lambda_handler(_event(unit_key(i)), None)
    mp.setattr(mod, "BATCH_SEAL_DEADLINE_SEC", 0)      # the bucket is over
    mod.lambda_handler(_event(unit_key(3)), None)
    maps = s3.put_keys("_batch_map.json")
    assert len(maps) == 1
    doc = json.loads(s3.puts[[p["Key"] for p in s3.puts].index(maps[0])]["Body"])
    third = doc["members"][2]
    assert third["trimmed_head_sec"] == 0.0 and third["trim_measured"] is False


# ---- a bypassed singleton must come back as a transcription, not as a member ----

def test_a_bypassed_member_is_transcribed_when_its_event_comes_back(wired):
    """The half of the singleton bypass that lives on this side.

    `bypass_singleton` copies the unit onto its own key so a fresh S3 event re-enters this
    handler. Without a check here the handler registers it, finds it already consumed,
    plans no window for it, and returns "batched_pending" — so the chunk is neither
    batched nor transcribed, which is audio gone with no error anywhere. The consumed mark
    is what makes it silent: it stops the planner, which is exactly what it is for.
    """
    monkeypatch, s3, ledger, calls = wired
    ledger.members[0] = {"chunk_index": 0, "chunk_key": unit_key(0),
                         "registered_at": 0, "sealed_into": 0}
    # Member-scoped since the seal key became the wall-clock bucket: a lookup by chunk
    # index against the seal record no longer finds anything.
    ledger.bypassed.add(0)

    results = []
    handled = mod._maybe_batch("b", unit_key(0), results)

    assert handled is False, "a bypassed member must fall through to transcription"
    assert results == [], "and must not be reported as batched"


# ============================================================
# The window rule (spec 2026-08-13, plan phase 4)
# ============================================================

def test_the_window_not_the_count_decides_membership(wired, monkeypatch):
    """The motivating case, end to end through the handler.

    c5 was dropped by VAD. Under the consecutive-index rule this produces two batches --
    [4] and [6,7] -- so the same speaker gets an unrelated label on each side of a 30-second
    silence. One window, one request, one namespace.
    """
    monkeypatch, s3, ledger, calls = wired
    for i in (4, 6, 7):
        s3.objects[unit_key(i)] = _wav(i)
        s3.objects[raw_key(i)] = _wav(i)
        mod._maybe_batch("b", unit_key(i), [])

    monkeypatch.setattr(mod, "BATCH_SEAL_DEADLINE_SEC", 0)   # grace elapsed
    mod._maybe_batch("b", unit_key(7), [])

    wavs = [k for k in s3.put_keys(".wav")]
    assert len(wavs) == 1, f"expected one bridging batch, got {wavs}"
    assert "_bn3_" in wavs[0], f"the window must hold all three survivors: {wavs[0]}"


def test_no_bn1_object_is_ever_written(wired, monkeypatch):
    """A window that ends up with one member goes down the per-chunk path instead."""
    monkeypatch, s3, ledger, calls = wired
    s3.objects[unit_key(9)] = _wav(9)
    s3.objects[raw_key(9)] = _wav(9)
    mod._maybe_batch("b", unit_key(9), [])
    monkeypatch.setattr(mod, "BATCH_SEAL_DEADLINE_SEC", 0)
    mod._maybe_batch("b", unit_key(9), [])
    assert [k for k in s3.put_keys(".wav") if "_bn1_" in k] == []




# ---- the map travels inside the transcript (phase 5b) ----

def test_a_batch_transcript_carries_its_map(wired):
    """Without this the four consumers that resolve absolute times have nothing to correct
    with, and a batched turn renders early -- by the trimmed overlap, and by the whole of
    any VAD-dropped chunk the window bridged."""
    mp, s3, ledger, calls = wired
    batch_key = (f"audio_segments/{USER}/{DATE}/Benl1_{DATE}_14-18-47_sid{SID}_c0000"
                 f"_bn4_off0.0_to114.0_srcwav.wav")
    s3.objects[batch_key] = _wav(0)
    s3.objects[bs.map_key_for_audio(batch_key)] = json.dumps(
        {"schema": 1, "session_id": SID, "members": [], "sealed_by": "arrival"})

    mod.lambda_handler(_event(batch_key), None)

    written = [p for p in s3.puts if p["Key"].startswith("transcripts/")]
    assert len(written) == 1, f"the batch must be transcribed: {s3.put_keys()}"
    assert bs.EMBEDDED_MAP_KEY in json.loads(written[0]["Body"])


def test_a_batch_whose_map_is_missing_fails_loudly(wired):
    """The seal writes the map BEFORE the WAV so that the WAV's existence implies the
    map's. An absent map is therefore a real fault, and this feature's whole history is
    faults that were logged and stepped over."""
    mp, s3, ledger, calls = wired
    batch_key = (f"audio_segments/{USER}/{DATE}/Benl1_{DATE}_14-18-47_sid{SID}_c0000"
                 f"_bn4_off0.0_to114.0_srcwav.wav")
    s3.objects[batch_key] = _wav(0)          # no sidecar alongside it

    out = _results(mod.lambda_handler(_event(batch_key), None))
    assert out and out[0].get("status") == "error", \
        "a missing map must not produce a silently un-rebasable transcript"
    assert [p for p in s3.puts if p["Key"].startswith("transcripts/")] == []


def test_a_per_chunk_transcript_gains_nothing_and_reads_nothing(wired):
    """The common case must cost neither a key in the object nor an S3 GET."""
    mp, s3, ledger, calls = wired
    mp.setattr(mod, "BATCH_TRANSCRIPTION", False)
    mod.lambda_handler(_event(unit_key(0)), None)
    written = [p for p in s3.puts if p["Key"].startswith("transcripts/")][0]
    assert bs.EMBEDDED_MAP_KEY not in json.loads(written["Body"])
    assert not any(k.endswith("_batch_map.json") for k in s3.gets)


# ---- batching only works on a provider whose output we write ourselves ----

def test_batching_refuses_the_async_transcribe_provider(wired):
    """AWS Transcribe writes its own output object; there is no hook to embed the map in it.

    Batching plus that provider therefore produces batch transcripts with no
    `fieldsight_batch_map`, and every consumer silently falls back to filename arithmetic --
    mis-timed by the trimmed overlap and by the whole of any bridged gap, with no error
    anywhere. The plan promised this refusal and it was never written.
    """
    mp, s3, ledger, calls = wired
    mp.setattr(mod, "ASR_PROVIDER", "transcribe")
    handled = mod._maybe_batch("b", unit_key(0), [])
    assert handled is False, "must fall through to per-chunk, not accumulate a batch"
    assert ledger.registered == [], "nothing may be registered under a provider we cannot embed for"


def test_the_window_length_is_read_from_the_environment_not_hardcoded(monkeypatch):
    """The previous version of this asserted `BATCH_WINDOW_SEC == 120`, which passes just as
    well if the env read is deleted and 120 written in its place -- so it guarded the value
    and not the wiring. Reload the module with the variable set to something else."""
    import importlib
    monkeypatch.setenv("BATCH_WINDOW_SEC", "45")
    reloaded = importlib.reload(mod)
    try:
        assert reloaded.BATCH_WINDOW_SEC == 45
    finally:
        monkeypatch.delenv("BATCH_WINDOW_SEC")
        importlib.reload(mod)


def test_the_bypass_check_reads_the_member_record_not_the_seal_key(wired):
    """Under bucket seal keys, `seal_status(sid, chunk_index)` misses.

    The copy-to-self event for a bypassed chunk would then re-enter batching, find itself
    consumed, and report `batched_pending` — the exact vanish the bypass exists to prevent.
    """
    monkeypatch, s3, ledger, calls = wired
    ledger.members[0] = {"chunk_index": 0, "chunk_key": unit_key(0),
                         "registered_at": 0, "sealed_into": 0}
    ledger.bypassed.add(0)
    assert mod._maybe_batch("b", unit_key(0), []) is False
