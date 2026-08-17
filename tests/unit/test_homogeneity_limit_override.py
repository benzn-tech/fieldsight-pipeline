"""Unit: the homogeneity limit is settable from the environment, and says when it has been.

Why the knob exists: 0.35 was measured on read speech and has refused every window of real
site audio ever tried, so no voiceprint exists and everything downstream of enrolment —
matching, automatic naming, harvest — has never run against real data. A night of measurement
produced a candidate number and then three reasons the measurement could not support it, so
the constant did not move. This lets TEST find out whether the rest of the chain works without
shipping a number nobody can defend.

Two things have to hold, and they fail differently:

  * **Every call site honours the override.** Four sites call the guard. One left on the
    default would be a guard that is loosened for enrolment and not for harvest, which is the
    worst of both: samples admitted by a rule the samples around them were not.
  * **An overridden guard is audible.** A loosened guard that logs like a passing one leaves
    "the window held one voice" and "the guard was switched off" looking identical in
    CloudWatch, which is how a diagnostic becomes a lie.
"""
import logging
import os
import subprocess
import sys

import numpy as np
import pytest

se = pytest.importorskip("lambda_speaker_embed")
import voiceprint_utils as vp  # noqa: E402


class FakeS3:
    def __init__(self, objects):
        self.objects = objects

    def get_object(self, Bucket, Key):
        class _B:
            def __init__(self, d):
                self.d = d

            def read(self):
                return self.d
        return {"Body": _B(self.objects[Key])}


def _wav_bytes(seconds=15.0, sr=16000):
    import io
    import wave
    n = int(seconds * sr)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.random.RandomState(0).randint(-3000, 3000, n)
                       ).astype("<i2").tobytes())
    return buf.getvalue()


@pytest.fixture
def stub_embedder(monkeypatch):
    """One vector per call, cycling, so the frames can be made to differ by a chosen amount."""
    calls = {"n": 0, "vectors": None}

    def embed(audio, sr):
        calls["n"] += 1
        vecs = calls["vectors"]
        if vecs is None:
            return np.ones(192, dtype=np.float32)
        return np.asarray(vecs[(calls["n"] - 1) % len(vecs)], dtype=np.float32)

    monkeypatch.setattr(se, "embed_audio", embed)
    return calls



def _frames_at_distance(d):
    """Three frames whose maximum pairwise cosine distance is exactly `d`."""
    a = np.zeros(192, dtype=np.float32)
    a[0] = 1.0
    b = np.zeros(192, dtype=np.float32)
    b[0], b[1] = 1.0 - d, float(np.sqrt(1.0 - (1.0 - d) ** 2))
    return [a, b, a]


def test_the_fixture_lands_between_the_default_and_the_override():
    """Otherwise the two tests below would both pass with the override ignored."""
    spread = vp.frame_spread(_frames_at_distance(0.5))
    assert vp.DEFAULT_MAX_FRAME_SPREAD < spread < 0.7, spread


def _enrol(monkeypatch, seconds=15.0):
    key = "users/u/audio/2026-08-11/x_c0000.wav"
    monkeypatch.setattr(se, "s3", lambda: FakeS3({key: _wav_bytes(seconds=seconds)}))
    return se.lambda_handler({"op": "enrol", "voiceprint_id": "vp1", "user_folder": "u",
                              "date": "2026-08-11", "source_filename": "x_c0000.wav",
                              "start_sec": 0.0, "end_sec": seconds}, None)


def test_a_window_the_default_refuses_is_still_refused_when_nothing_is_set(stub_embedder,
                                                                          monkeypatch):
    stub_embedder["vectors"] = _frames_at_distance(0.5)
    monkeypatch.setattr(se, "MAX_FRAME_SPREAD", vp.DEFAULT_MAX_FRAME_SPREAD)
    assert _enrol(monkeypatch)["status"] == "refused"


def test_the_same_window_is_admitted_when_the_limit_is_raised(stub_embedder, monkeypatch):
    stub_embedder["vectors"] = _frames_at_distance(0.5)
    monkeypatch.setattr(se, "MAX_FRAME_SPREAD", 0.7)
    assert _enrol(monkeypatch)["status"] == "embedded"


def test_the_diagnostic_reports_the_limit_in_force_not_the_compiled_in_one(stub_embedder,
                                                                          monkeypatch):
    """A diagnostic that reports a limit the function is not using is worse than none."""
    stub_embedder["vectors"] = _frames_at_distance(0.5)
    key = "users/u/audio/2026-08-11/x_c0000.wav"
    monkeypatch.setattr(se, "s3", lambda: FakeS3({key: _wav_bytes(seconds=15.0)}))
    monkeypatch.setattr(se, "MAX_FRAME_SPREAD", 0.7)
    out = se.lambda_handler({"op": "spread", "user_folder": "u", "date": "2026-08-11",
                             "source_filename": "x_c0000.wav",
                             "start_sec": 0.0, "end_sec": 15.0}, None)
    assert out["limit"] == 0.7
    assert out["default_limit"] == vp.DEFAULT_MAX_FRAME_SPREAD
    assert out["verdict"] == "homogeneous"


def test_no_call_site_is_left_on_the_default(monkeypatch):
    """The behavioural tests above reach two of the four sites.

    Harvest and the correction-carried enrolment are the other two, and a site left on the
    default would loosen the guard for one kind of sample and not the kind stored beside it.
    This reads the source because that is the only check that covers a site no test drives.
    """
    import inspect
    src = inspect.getsource(se)
    calls = [line for line in src.splitlines() if "vp.window_is_homogeneous(" in line
             and "def " not in line]
    assert len(calls) == 4, "call sites moved; this test counts them on purpose: %r" % calls
    # The argument may be on the following line where the call wraps, so check the source
    # region rather than the single line.
    for i, line in enumerate(src.splitlines()):
        if "vp.window_is_homogeneous(" in line:
            region = "\n".join(src.splitlines()[i:i + 3])
            assert "MAX_FRAME_SPREAD" in region, "left on the default: %s" % line.strip()


def test_the_note_is_silent_at_the_default_and_loud_otherwise(monkeypatch):
    monkeypatch.setattr(se, "MAX_FRAME_SPREAD", vp.DEFAULT_MAX_FRAME_SPREAD)
    assert se._limit_note() == ""
    monkeypatch.setattr(se, "MAX_FRAME_SPREAD", 0.7)
    note = se._limit_note()
    assert "OVERRIDDEN" in note and "0.700" in note


def test_an_admitted_window_says_in_the_log_that_the_guard_was_loosened(stub_embedder,
                                                                       monkeypatch, caplog):
    """The acceptance path, not only the refusal path.

    A guard that speaks only when it refuses leaves "it passed" and "it was switched off"
    indistinguishable — the shape that once produced 1078 uploads and zero log lines.
    """
    stub_embedder["vectors"] = _frames_at_distance(0.5)
    monkeypatch.setattr(se, "MAX_FRAME_SPREAD", 0.7)
    with caplog.at_level(logging.INFO):
        assert _enrol(monkeypatch)["status"] == "embedded"
    assert any("OVERRIDDEN" in r.getMessage() for r in caplog.records), caplog.text


def test_the_environment_variable_actually_reaches_the_constant():
    """The knob is read at import time, so setting it in a running function does nothing.

    Without this the other tests would pass over a variable the deploy never delivers — a
    switch wired at two of its three segments, which fails by silently using the default and
    reporting no error at all.

    In a SUBPROCESS, not by reloading the module in place. Reloading it made two unrelated
    tests in another file fail, because this file sorts first and pytest imports the lambda
    once for the whole session — the import-order-as-hidden-contract trap this repository has
    already been bitten by.
    """
    env = dict(os.environ, VOICEPRINT_MAX_FRAME_SPREAD="0.62",
               PYTHONPATH=os.pathsep.join(sys.path))
    out = subprocess.run(
        [sys.executable, "-c",
         "import lambda_speaker_embed as m; print(m.MAX_FRAME_SPREAD)"],
        capture_output=True, text=True, env=env)
    assert out.returncode == 0, out.stderr[-800:]
    assert float(out.stdout.strip()) == 0.62

    plain = dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path))
    plain.pop("VOICEPRINT_MAX_FRAME_SPREAD", None)
    out = subprocess.run(
        [sys.executable, "-c",
         "import lambda_speaker_embed as m; print(m.MAX_FRAME_SPREAD)"],
        capture_output=True, text=True, env=plain)
    assert out.returncode == 0, out.stderr[-800:]
    assert float(out.stdout.strip()) == vp.DEFAULT_MAX_FRAME_SPREAD
