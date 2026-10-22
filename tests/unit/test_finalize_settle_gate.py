"""Unit: the claim waits until the session's uploads have reached the pipeline.

TEST 2026-10-22 (M2 Task 9, Step 4). A phone offline for 20 minutes came back,
uploaded c0001-c0003 in seven seconds and sent `close(end)` at once. The sweep
tick three seconds later claimed the session: `_seal_tail_batches` ran with
deadline 0, but VAD had not finished those chunks and the transcriber had not
registered them in the batch ledger, so it sealed nothing, and the final
extraction and the email went out from c0000 alone -- 30 s of a 2-minute
meeting. Nothing re-sends an `end` session.

The rule pinned here: a due session is not sealed or claimed while one of its
recently uploaded chunks has no VAD sidecar yet, or has speech but is not yet
in the ledger. A silent chunk (VAD dropped it, so it never reaches the ledger)
is settled by its sidecar alone. Recordings older than FINALIZE_SETTLE_MAX_SEC
are not asked about, so a chunk VAD never processes delays one email by at most
that long instead of forever. A failing check fails OPEN: today's behaviour.
"""
import pytest

mod = pytest.importorskip("lambda_finalize_claim")

SID = "f59eb9274e8741a695ed6b5e36e0b605"


def _key(i):
    return (f"users/Ben_Lin_test2/audio/2026-10-23/"
            f"ben_lin_2026-10-23_00-26-{i:02d}_sid{SID}_c{i:04d}.wav")


class FakeConn:
    """Only what the gate touches: the savepoint around its one read."""

    def transaction(self):
        import contextlib
        return contextlib.nullcontext()


@pytest.fixture
def world(monkeypatch):
    """recent uploads, ledger members and sidecars, all controllable."""
    w = {"recent": [_key(0), _key(1), _key(2), _key(3)],
         "members": {0, 1, 2, 3},
         "sidecars": {},           # recording key -> sidecar dict (absent = not written)
         "calls": []}
    monkeypatch.setattr(mod.recordings, "recent_session_chunks",
                        lambda conn, sid, within: list(w["recent"]))
    monkeypatch.setattr(mod, "_ledger_indices", lambda sid: set(w["members"]))
    monkeypatch.setattr(mod, "_read_sidecar",
                        lambda s3, key: w["sidecars"].get(key))
    monkeypatch.setattr(mod, "_s3_client", lambda: object())
    monkeypatch.setattr(mod, "BATCH_TRANSCRIPTION", True)
    monkeypatch.setattr(mod.meeting_session, "list_due_finalize",
                        lambda conn, grace: [{"session_id": SID, "version": 1}])
    monkeypatch.setattr(mod, "revisit_active_batch_sessions", lambda: [])
    monkeypatch.setattr(mod, "_seal_tail_batches",
                        lambda sid: w["calls"].append(("seal", sid)))

    def _finalize(conn, sid, version, **kw):
        w["calls"].append(("finalize", sid))
        return {"status": "waiting", "sessionId": sid}

    monkeypatch.setattr(mod, "finalize_claim", _finalize)
    return w


def _sweep():
    return mod.sweep(FakeConn(), grace_seconds=0, infer_idle=False, reopen=False)


def test_everything_in_the_ledger_is_claimed_at_once(world):
    out = _sweep()
    assert world["calls"] == [("seal", SID), ("finalize", SID)]
    assert out == [{"status": "waiting", "sessionId": SID}]


def test_step4_replay_the_reconnect_chunks_are_not_through_vad_yet(world):
    """The exact shape that sent a 30-second email: c0000 registered long ago,
    c0001-c0003 uploaded seconds before the tick, no sidecar, no ledger row."""
    world["members"] = {0}
    out = _sweep()
    assert world["calls"] == [], "no seal and no claim while the tail is in flight"
    assert out == []

    # Next tick: VAD has run and the transcriber has registered them.
    world["members"] = {0, 1, 2, 3}
    _sweep()
    assert world["calls"] == [("seal", SID), ("finalize", SID)], \
        "the tail is sealed with every member present, then claimed"


def test_sidecar_with_speech_but_not_yet_registered_waits(world):
    """The sidecar is written after the segment WAV; the transcriber registers
    the segment about a second later. A tick in that second must still wait."""
    world["members"] = {0, 1, 2}
    world["sidecars"][_key(3)] = {"segments": [{"s3_key": "audio_segments/x"}]}
    _sweep()
    assert world["calls"] == []


def test_a_silent_chunk_is_settled_by_its_sidecar(world):
    """VAD drops a chunk with no speech: no segment, so never a ledger row. Its
    sidecar is the only evidence it was processed, and it is enough."""
    world["members"] = {0, 1, 2}
    world["sidecars"][_key(3)] = {"vad_result": "no_speech_dropped", "segments": []}
    _sweep()
    assert world["calls"] == [("seal", SID), ("finalize", SID)]


def test_without_batching_the_sidecar_alone_settles_a_chunk(world, monkeypatch):
    monkeypatch.setattr(mod, "BATCH_TRANSCRIPTION", False)
    world["members"] = set()
    world["sidecars"] = {k: {"segments": [{"s3_key": "s"}]} for k in world["recent"]}
    _sweep()
    assert ("finalize", SID) in world["calls"]

    world["calls"].clear()
    world["sidecars"].pop(_key(3))
    _sweep()
    assert world["calls"] == [], "a chunk VAD has not finished still waits"


def test_no_recent_uploads_means_nothing_to_wait_for(world):
    world["recent"] = []
    world["members"] = set()
    _sweep()
    assert world["calls"] == [("seal", SID), ("finalize", SID)]


def test_the_window_asked_about_is_the_settle_bound(world, monkeypatch):
    seen = {}
    monkeypatch.setattr(mod.recordings, "recent_session_chunks",
                        lambda conn, sid, within: seen.setdefault("within", within) and [])
    _sweep()
    assert seen["within"] == mod.FINALIZE_SETTLE_MAX_SEC
    assert 60 <= mod.FINALIZE_SETTLE_MAX_SEC <= 600


def test_a_failing_check_fails_open(world, monkeypatch):
    """A sweep that dies stops every email after it; a check that cannot run
    must leave the session exactly where it was before this gate existed."""
    def boom(*a, **kw):
        raise RuntimeError("DynamoDB went away")

    monkeypatch.setattr(mod, "_ledger_indices", boom)
    world["members"] = set()
    _sweep()
    assert world["calls"] == [("seal", SID), ("finalize", SID)]


def test_one_deferred_session_does_not_hold_up_another(world, monkeypatch):
    other = "a" * 32
    monkeypatch.setattr(mod.meeting_session, "list_due_finalize",
                        lambda conn, grace: [{"session_id": SID, "version": 1},
                                             {"session_id": other, "version": 1}])
    monkeypatch.setattr(mod.recordings, "recent_session_chunks",
                        lambda conn, sid, within: list(world["recent"]) if sid == SID else [])
    world["members"] = {0}
    _sweep()
    assert world["calls"] == [("seal", other), ("finalize", other)]


def test_sidecar_key_mirrors_lambda_vad():
    """The sidecar's key is built by lambda_vad.parse_source_filename: folder
    from the path, DATE FROM THE FILENAME (not the path), name without ext."""
    import lambda_vad  # module-level imports only; onnxruntime is loaded lazily
    info = lambda_vad.parse_source_filename(_key(3))
    expected = (f"{lambda_vad.OUTPUT_PREFIX}{info['user_name']}/{info['date']}/"
                f"{info['basename_no_ext']}_vad_metadata.json")
    assert mod._sidecar_key(_key(3)) == expected
    video = f"users/Ann/video/2026-10-22/F2SP_2026-10-23_00-00-05_sid{SID}_c0012.mp4"
    info = lambda_vad.parse_source_filename(video)
    assert mod._sidecar_key(video) == (
        f"audio_segments/Ann/2026-10-23/{info['basename_no_ext']}_vad_metadata.json")
