"""Unit: a turn too wide to judge is narrowed, not refused — and never spliced.

A correction names a TURN, because the propagation half needs those exact boundaries to match
anything. Under batching a turn is a whole chunk. So the same two numbers were being asked to
be a 109-second span for one consumer and a 10-second span for the other, and both live
attempts on TEST show one half working while the other fails:

    start=10, end=18      enrol accepted (spread 0.198), matched no turn
    start=3.16, end=112.66  4 turns matched, 3 named, enrol refused (spread 0.755)

Measured over 78 prod windows on 2026-08-19: a 5–10 s window is homogeneous 83 % of the time,
a 20–30 s window 0 %. Two frames is 10 s — the top of the band that works. So enrolment now
re-tests the tightest 10 s inside a window it could not accept whole.

**What these tests do not claim.** The pair is chosen by minimising the very statistic the
guard then applies, so a pass is not evidence the guard works — it is the guard being handed
the best candidate the window holds. What is asserted is the part that is not circular: the
guard still refuses when even that candidate is too wide, and the narrowing never invents
audio.
"""
import numpy as np
import pytest

se = pytest.importorskip("lambda_speaker_embed")
vp = pytest.importorskip("voiceprint_utils")

SR = 16000


def _tone(freq, seconds, sr=SR, amp=0.3):
    """Loud enough to clear the dBFS floor, so `_frames_at` keeps it."""
    t = np.arange(int(seconds * sr)) / sr
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def test_a_dropped_silent_frame_never_becomes_a_splice(monkeypatch):
    """The one failure that cannot be undone.

    `_frames_at` drops frames below the dBFS floor, so neighbours in the list are not always
    neighbours in time. Joining two separated stretches would store, as one continuous
    sample of somebody's voice, ten seconds of audio that never existed — and it would look
    exactly like a normal enrolment afterwards.
    """
    step = int(se.FRAME_SECONDS * SR)
    # Frames at 0 and at 2*step: adjacent in the list, a gap of one frame in time.
    frames = [(0, _tone(200, se.FRAME_SECONDS)), (2 * step, _tone(200, se.FRAME_SECONDS))]
    embs = [np.ones(192, dtype=np.float32), np.ones(192, dtype=np.float32)]

    span, spread = se._tightest_pair(frames, embs, SR)
    assert span is None, (
        "two frames with a dropped frame between them were treated as one continuous "
        "window; the stored sample would be a splice")
    assert spread is None


def test_contiguous_frames_are_offered_as_a_window():
    step = int(se.FRAME_SECONDS * SR)
    frames = [(0, _tone(200, se.FRAME_SECONDS)), (step, _tone(200, se.FRAME_SECONDS))]
    embs = [np.ones(192, dtype=np.float32), np.ones(192, dtype=np.float32)]

    span, spread = se._tightest_pair(frames, embs, SR)
    assert span == (0, 2 * step)
    assert spread == pytest.approx(0.0, abs=1e-6)


def test_the_tightest_pair_is_the_one_returned():
    """Three frames, one pair much closer than the other. The window that gets stored is the
    one the audio actually supports, not the first one encountered."""
    step = int(se.FRAME_SECONDS * SR)
    frames = [(i * step, _tone(200, se.FRAME_SECONDS)) for i in range(3)]
    a = np.ones(192, dtype=np.float32)
    far = np.concatenate([np.ones(96), -np.ones(96)]).astype(np.float32)
    embs = [a, far, far]                      # pair (1,2) agree; pair (0,1) do not

    span, _ = se._tightest_pair(frames, embs, SR)
    assert span == (step, 3 * step)


def test_one_frame_offers_nothing():
    """A single frame is trivially consistent with itself. `frame_spread` answers None for
    fewer than two, and that must stay 'cannot tell' rather than becoming a window."""
    span, spread = se._tightest_pair([(0, _tone(200, se.FRAME_SECONDS))],
                                     [np.ones(192, dtype=np.float32)], SR)
    assert (span, spread) == (None, None)


def test_a_window_whose_best_pair_is_still_too_wide_is_refused(monkeypatch):
    """The property this change is allowed to claim.

    Narrowing hands the guard a better candidate; it does not overrule it. If even the
    tightest ten seconds hold more than one voice, the answer is still no — otherwise the
    narrowing would be a second, quieter way to admit a window the guard rejected.
    """
    step = int(se.FRAME_SECONDS * SR)
    clip = np.concatenate([_tone(200, se.FRAME_SECONDS), _tone(900, se.FRAME_SECONDS),
                           _tone(200, se.FRAME_SECONDS)])

    monkeypatch.setattr(se, "_window_audio", lambda *a, **k: ("k.wav", clip, SR))
    # Every frame embeds far from its neighbours: no pair can pass.
    seq = iter([np.eye(192, dtype=np.float32)[i] for i in range(50)])
    monkeypatch.setattr(se, "embed_audio", lambda f, sr: next(seq))

    out = se._enrol({"start_sec": 0.0, "end_sec": 15.0, "user_folder": "F", "date": "D",
                     "source_filename": "s.json", "voiceprint_id": "vp-1"})
    assert out["status"] == "refused"
    assert "homogeneous" in out["reason"]


def test_a_narrowed_enrolment_reports_the_window_it_actually_used(monkeypatch):
    """The stored `window` must be the narrowed one, not the turn.

    It is what a withdrawal and an audit are keyed to, and it is what a human would re-listen
    to when asking whether this sample should ever have been stored. Reporting the whole turn
    would point them at 109 seconds of which 10 were used.
    """
    clip = np.concatenate([_tone(200, se.FRAME_SECONDS) for _ in range(3)])
    monkeypatch.setattr(se, "_window_audio", lambda *a, **k: ("k.wav", clip, SR))

    same = np.ones(192, dtype=np.float32)
    other = np.concatenate([np.ones(96), -np.ones(96)]).astype(np.float32)
    # Whole window: frames [other, same, same] -> too wide. Tightest pair: frames 1 and 2.
    whole = iter([other, same, same])
    monkeypatch.setattr(se, "embed_audio",
                        lambda f, sr: next(whole, same))

    out = se._enrol({"start_sec": 100.0, "end_sec": 115.0, "user_folder": "F", "date": "D",
                     "source_filename": "s.json", "voiceprint_id": "vp-1"})
    assert out["status"] == "embedded", out
    lo, hi = out["window"]
    assert (lo, hi) == pytest.approx((105.0, 115.0)), (
        "the artifact reports the whole turn rather than the ten seconds actually enrolled")
