"""Unit: the last two minutes of a session get transcribed too.

Plan: docs/superpowers/plans/2026-08-11-batched-transcription.md phase 4.

A session's final 1–3 chunks never see a fourth arrival, so nothing in the transcriber will
ever seal them. If the sweep finalizes the session without sealing first, the final
extraction is requested against transcripts that are missing the tail — and the confirmation
email goes out reading perfectly well, minus the end of the meeting. That is the failure this
phase exists to prevent, and it is the reason the ordering is asserted rather than assumed.

Flag off, none of this happens: the sweep is byte-for-byte what it was.
"""
import pytest

mod = pytest.importorskip("lambda_finalize_claim")


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Recorder:
    """One ordered log of everything the sweep did, so ordering is testable."""

    def __init__(self):
        self.calls = []

    def seal(self, *a, **kw):
        self.calls.append(("seal", kw.get("session_id") or a[2]))
        return ["audio_segments/u/d/x_bn2_off0.0_to60.0_srcwav.wav"]

    def finalize(self, conn, session_id, version, **kw):
        self.calls.append(("finalize", session_id))
        return {"session_id": session_id, "status": "claimed"}


S1 = "a" * 32


@pytest.fixture
def swept(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(mod.meeting_session, "list_due_finalize",
                        lambda conn, grace: [{"session_id": S1, "version": 1}])
    monkeypatch.setattr(mod, "finalize_claim", rec.finalize)
    import batch_seal
    monkeypatch.setattr(batch_seal, "seal_ready_runs", rec.seal)
    monkeypatch.setattr(mod, "BATCH_TRANSCRIPTION", True, raising=False)
    return monkeypatch, rec


def test_with_the_flag_off_the_sweep_is_unchanged(swept):
    mp, rec = swept
    mp.setattr(mod, "BATCH_TRANSCRIPTION", False)
    mod.sweep(FakeConn(), grace_seconds=0, infer_idle=False)
    assert rec.calls == [("finalize", S1)], "no sealing, no extra S3 work"


def test_the_tail_is_sealed_before_the_session_is_finalized(swept):
    """Order is the whole point. Finalizing first requests the extraction against
    transcripts that do not yet include the last chunks, and the email that follows is
    complete-looking and short."""
    mp, rec = swept
    mod.sweep(FakeConn(), grace_seconds=0, infer_idle=False)
    assert rec.calls == [("seal", S1), ("finalize", S1)]


def test_a_session_with_no_pending_run_writes_no_batch(swept):
    mp, rec = swept
    import batch_seal
    mp.setattr(batch_seal, "seal_ready_runs",
               lambda *a, **kw: rec.calls.append(("seal", "none")) or [])
    mod.sweep(FakeConn(), grace_seconds=0, infer_idle=False)
    assert ("finalize", S1) in rec.calls


def test_a_seal_that_raises_does_not_stop_the_session_finalizing(swept):
    """A missing tail costs the end of one email. A sweep that dies costs every email
    after it — the same ordering the group scan already learned (BUG-43's family)."""
    mp, rec = swept
    import batch_seal

    def boom(*a, **kw):
        rec.calls.append(("seal", "boom"))
        raise RuntimeError("S3 went away")

    mp.setattr(batch_seal, "seal_ready_runs", boom)
    mod.sweep(FakeConn(), grace_seconds=0, infer_idle=False)
    assert ("finalize", S1) in rec.calls


def test_the_claim_being_lost_is_not_an_error(swept):
    """The transcriber may have sealed the same run between the sweep's read and its
    claim. The loser walks away; it must not write a second batch or fail the sweep."""
    mp, rec = swept
    import batch_seal
    mp.setattr(batch_seal, "seal_ready_runs", lambda *a, **kw: [])
    out = mod.sweep(FakeConn(), grace_seconds=0, infer_idle=False)
    assert out and out[0]["session_id"] == S1
