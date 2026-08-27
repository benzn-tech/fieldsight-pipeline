"""Unit: the enrolment floor must admit exactly what the frame cutter can judge.

The floor was `10.0` on the reasoning that a 5–9.99 s window yields one frame and cannot be
judged. That was true when written. `_frames` then gained its end-anchored tail frame — the
fix for judging a window on two thirds of itself — and from that moment a window longer than
one frame yielded two. The floor went on excluding them for another three weeks, and the
comment beside it preserved the arithmetic that had stopped applying.

Measured cost, 78 prod windows on 2026-08-19: the 10–30 s band the floor admitted passes the
homogeneity guard 5 % and 0 % of the time; the 5–10 s band it excluded passes 83 %. Enrolment's
entire candidate population was the material least suitable for enrolment.

So these tests do not pin a number. They pin the *relationship* — anything the floor admits
must be something `_frames` can produce two frames from — because the number was never the
thing that went wrong.
"""
import numpy as np
import pytest

se = pytest.importorskip("lambda_speaker_embed")


def _silence(seconds, sr=16000):
    # Loud enough to clear FRAME_MIN_DBFS; the point here is frame COUNT, not content.
    rng = np.random.RandomState(0)
    return rng.uniform(-0.2, 0.2, int(seconds * sr)).astype(np.float32)


@pytest.mark.parametrize("seconds", [3.0, 4.9, 5.0, 5.1, 6.0, 7.5, 9.9, 10.0, 12.0, 25.0])
def test_the_floor_admits_exactly_the_durations_frames_can_judge(seconds):
    """The invariant that broke, asserted directly against the cutter rather than a constant.

    If `_frames` changes again — a different stride, a different tail rule — this fails
    instead of the floor silently going back out of step with it.
    """
    admitted = seconds > se.ENROL_MIN_TURN_S
    judgeable = len(se._frames(_silence(seconds), 16000)) >= 2
    assert admitted == judgeable, (
        f"{seconds}s: floor {'admits' if admitted else 'refuses'} it, "
        f"_frames yields {len(se._frames(_silence(seconds), 16000))} frame(s)")


def test_the_floor_is_derived_from_the_frame_length_not_written_beside_it():
    """Two constants that must agree, kept as one expression. They drifted apart once and
    nothing failed — the whole feature just quietly selected its worst material."""
    assert se.ENROL_MIN_TURN_S == se.FRAME_SECONDS
    src = open("src/lambda_speaker_embed.py", encoding="utf-8").read()
    assert "str(FRAME_SECONDS)" in src, (
        "the floor's default is written as a literal again; it must be derived, because a "
        "literal is what let it survive three weeks out of step with the frame cutter")


def test_a_six_second_turn_now_reaches_the_embedder(monkeypatch):
    """The behaviour the change exists for: 5–10 s candidates are read and judged rather than
    skipped before the audio is even fetched."""
    seen = []

    def _window(folder, date, fname, start, end):
        seen.append(round(end - start, 1))
        return "k", _silence(end - start), 16000

    monkeypatch.setattr(se, "_window_audio", _window)
    monkeypatch.setattr(se, "embed_audio", lambda a, sr: np.ones(192, dtype=np.float32))

    cands = [{"turn": {"source_filename": "x.wav", "start_sec": 0.0, "end_sec": 6.0},
              "vector": np.ones(192), "turn_ref": "x@0.0"}]
    admitted = se._admit_harvest("u", "2026-08-13", cands)

    assert seen == [6.0], "a six-second candidate was skipped before its audio was read"
    assert len(admitted) == 1, "identical frames must be judged homogeneous and admitted"


def test_a_turn_at_exactly_one_frame_length_is_still_skipped(monkeypatch):
    """At exactly FRAME_SECONDS the tail branch does not fire and there is one frame. The
    floor exists to skip the S3 read and the ONNX pass for windows that cannot be judged, so
    the boundary belongs on the excluded side — `<=`, not `<`."""
    seen = []
    monkeypatch.setattr(se, "_window_audio",
                        lambda *a: seen.append(1) or ("k", _silence(5.0), 16000))
    monkeypatch.setattr(se, "embed_audio", lambda a, sr: np.ones(192, dtype=np.float32))

    cands = [{"turn": {"source_filename": "x.wav", "start_sec": 0.0, "end_sec": 5.0},
              "vector": np.ones(192), "turn_ref": "x@0.0"}]
    assert se._admit_harvest("u", "2026-08-13", cands) == []
    assert seen == [], "a one-frame window was fetched and embedded before being refused"


def test_the_caps_now_reach_the_thirty_second_target():
    """`ENROL_MAX_SAMPLES = 6` looked arbitrary against a 3 s floor and is right against this
    one: six candidates of 5–10 s is 30–60 s, which is the range enrolment material is meant
    to land in. Recorded as a test because the next person to move the floor moves this too.
    """
    assert se.ENROL_MAX_SAMPLES * se.ENROL_MIN_TURN_S >= 30.0
    assert se.ENROL_MAX_SECONDS >= se.ENROL_MAX_SAMPLES * se.ENROL_MIN_TURN_S
