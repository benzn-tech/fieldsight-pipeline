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
        # Kept so the deadline can be asserted. Erased once already, together with the
        # fix it guarded — see the last test in this file.
        self.seal_args, self.seal_kwargs = a, kw
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


def test_a_closing_session_seals_its_tail_now_rather_than_waiting_out_the_deadline(swept):
    """The deadline exists so a run of 1-3 does not seal while a fourth chunk might still
    arrive. At session close nothing more can arrive, so waiting is not caution — it is the
    failure this phase was written to prevent.

    This test has been written twice. The first time it was erased together with the fix it
    guarded, by a whole-file rewrite in an unrelated commit — so CI stayed green while the
    path was dead, and the next real recording (6 minutes, 13 chunks) lost its last 19
    seconds with nothing failing anywhere.
    """
    mp, rec = swept
    mod.sweep(FakeConn(), grace_seconds=0, infer_idle=False)
    deadline = rec.seal_args[5] if len(rec.seal_args) > 5 else rec.seal_kwargs.get("deadline_sec")
    assert deadline == 0, \
        "sealing at close must use a zero deadline; nothing else is coming"


def test_the_tail_seal_uses_the_same_window_as_the_transcriber(swept):
    """Both sides group the same chunks into the same windows, or they disagree in silence.

    The sweep decides a window is complete and seals it; if it measured the window with a
    different width than the transcriber, it would seal one the transcriber was still
    filling — or leave one the transcriber thought was already handled. Same reason
    BatchMaxChunks has always had to match, now for the parameter that actually decides
    membership.
    """
    mp, rec = swept
    mod.sweep(FakeConn(), grace_seconds=0, infer_idle=False)
    window = rec.seal_kwargs.get("window_sec")
    assert window == mod.BATCH_WINDOW_SEC, \
        f"the tail seal grouped by {window}, the transcriber by {mod.BATCH_WINDOW_SEC}"
    assert mod.BATCH_WINDOW_SEC == 120


def test_the_sweep_redrives_batches_that_were_never_transcribed(swept, monkeypatch):
    """Somebody has to come back, and nothing did.

    A batch whose transcription was rejected (27 of them on one replay, all HTTP 429 from
    the provider's concurrency ceiling) leaves a WAV with no transcript. The per-record
    `except` recorded `status: error` and returned 200, so S3's async retry never fired;
    there is no DLQ; and the ledger says `sealed` so no planner looks at it again. About 54
    minutes of audio, recovered by hand.

    The per-minute sweep already visits every session. This asserts it now looks.
    """
    mp, rec = swept
    monkeypatch.setattr(mod, "_batch_prefix_for",
                        lambda sid, table=None: "audio_segments/Ben/2026-08-13/")
    seen = []
    import batch_redrive
    monkeypatch.setattr(batch_redrive, "redrive_untranscribed",
                        lambda *a, **k: seen.append(a[2]) or
                        {"candidates": 0, "redriven": 0, "exhausted": 0})
    mod.sweep(FakeConn(), grace_seconds=0, infer_idle=False)
    assert seen, "the sweep never asked whether any sealed batch is missing its transcript"


def test_the_sweep_visits_active_sessions_that_no_database_row_knows_about(swept, monkeypatch):
    """The prod blocker, made structural.

    Sealing runs on chunk arrival; the close pass runs once, for sessions in
    `pending_close`. A device whose `/open` never reached the server has NO row -- so after
    a backlog burst every bucket sits registered and unsealed forever, with zero transcripts
    and zero errors. The acceptance replay is exactly that shape and needed a manual poke.

    The sweep now also walks the ledger's own list of sessions that still owe work, which is
    one Query and is true whatever the database thinks.
    """
    mp, rec = swept
    import batch_ledger
    monkeypatch.setattr(batch_ledger, "active_sessions", lambda t: ["deadbeef" * 4])
    seen = []
    import batch_seal
    monkeypatch.setattr(batch_seal, "seal_ready_runs",
                        lambda *a, **k: seen.append(a[2]) or [])
    mod.sweep(FakeConn(), grace_seconds=0, infer_idle=False)
    assert "deadbeef" * 4 in seen, \
        "a session the database has never heard of still owes its audio a batch"


def test_the_redrive_listing_is_scoped_to_the_session_folder(swept, monkeypatch):
    """Prod's audio_segments/ already holds 5,542 objects and nothing expires them.

    Walking the whole prefix once per session, inside a 120-second sweep that also does
    finalize, is a timeout waiting to happen -- and a timeout there kills the tick before
    `finalize_claim`, so the session stays pending_close and the next tick repeats it.
    Confirmation emails would stall behind a listing.
    """
    mp, rec = swept
    monkeypatch.setattr(mod, "_batch_prefix_for",
                        lambda sid, table=None: "audio_segments/Ben/2026-08-13/")
    prefixes = []
    import batch_redrive
    monkeypatch.setattr(batch_redrive, "redrive_untranscribed",
                        lambda *a, **k: prefixes.append(k.get("prefix")) or
                        {"candidates": 0, "redriven": 0, "exhausted": 0})
    mod.sweep(FakeConn(), grace_seconds=0, infer_idle=False)
    assert prefixes and all(p and p != "audio_segments/" for p in prefixes), \
        f"the sweep listed the whole lake: {prefixes}"


def test_a_session_with_no_ledger_rows_is_not_redriven_at_all(swept, monkeypatch):
    """No prefix means nothing of this session's to re-drive. Calling anyway would list
    everything under audio_segments/ -- 5,542 objects on prod, growing, inside a 120 s
    sweep whose timeout would strand every confirmation email behind it."""
    mp, rec = swept
    monkeypatch.setattr(mod, "_batch_prefix_for", lambda sid, table=None: None)
    called = []
    import batch_redrive
    monkeypatch.setattr(batch_redrive, "redrive_untranscribed",
                        lambda *a, **k: called.append(k.get("prefix")) or {})
    mod.sweep(FakeConn(), grace_seconds=0, infer_idle=False)
    assert called == [], "an unscoped re-drive is worse than no re-drive"
