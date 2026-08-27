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


def test_the_default_did_not_move(stub_embedder, monkeypatch):
    """Named for what it actually guards.

    A review pointed out that this test passes on the pre-change code, so it proves nothing
    about the override. Kept anyway, under an honest name: the whole premise of this change is
    that the constant does NOT move, and a later edit that quietly relaxed it would otherwise
    turn every "the override works" test into a tautology without any of them going red.
    """
    stub_embedder["vectors"] = _frames_at_distance(0.5)
    monkeypatch.setattr(se, "MAX_FRAME_SPREAD", vp.DEFAULT_MAX_FRAME_SPREAD)
    assert vp.DEFAULT_MAX_FRAME_SPREAD == 0.35
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
            # Whitespace-collapsed, because a wrapped call puts the comma at the end of one
            # line and the argument at the start of the next.
            region = " ".join(" ".join(src.splitlines()[i:i + 3]).split())
            # `, MAX_FRAME_SPREAD` — passed as an ARGUMENT. Bare "MAX_FRAME_SPREAD" is also
            # satisfied by the substring inside DEFAULT_MAX_FRAME_SPREAD, and this grep is
            # the only coverage two of the four sites have, so its weakest reading is the
            # one that matters.
            assert ", MAX_FRAME_SPREAD" in region, "left on the default: %s" % line.strip()


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


# ---- the value that arrives is not always a number -----------------------


@pytest.mark.parametrize("raw", ["off", "", "true", "Default", "0.35abc", None])
def test_a_value_that_is_not_a_number_falls_back_instead_of_killing_the_function(raw, caplog):
    """`float(raw)` at import time is not a bad enrolment — it is Runtime.ImportModuleError
    on EVERY invocation, including match, propagation and the S3-triggered correction path,
    while the deploy stays green and nothing reports it.

    'Default' is in the list on purpose: the template's sentinel comparison is
    case-sensitive, so a repo variable set to 'Default' passes the condition and arrives here
    as a string to parse.
    """
    with caplog.at_level(logging.ERROR):
        assert se._read_limit(raw) == vp.DEFAULT_MAX_FRAME_SPREAD
    if raw is not None:
        assert any("VOICEPRINT_MAX_FRAME_SPREAD" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("raw", ["2", "1.5", "0", "-0.4"])
def test_a_limit_outside_the_range_is_refused(raw, caplog):
    """Cosine distance runs to 2.0 and the check is `spread <= limit`, so 2 admits ANY window
    — the guard switched off rather than loosened, with nothing in the response to say so.
    Zero and negatives refuse everything, which is the same class of mistake pointing the
    other way."""
    with caplog.at_level(logging.ERROR):
        assert se._read_limit(raw) == vp.DEFAULT_MAX_FRAME_SPREAD


def test_a_value_inside_the_range_is_taken_as_given():
    assert se._read_limit("0.7") == 0.7
    assert se._read_limit("1.0") == 1.0


def test_the_template_pattern_admits_exactly_what_the_code_admits():
    """Two guards, one rule, written in different languages in different files. A template
    that accepted a value the code then rejects would deploy a stack whose guard is quietly
    back at the default — a switch wired at all three segments and disagreeing with itself.

    The expectation is DERIVED from `_read_limit`, not written out by hand. The first version
    of this test compared the two over a hand-written list of good and bad values, and the
    list happened not to contain `0` — which the pattern accepted and the code rejected. A
    deploy setting the limit to 0 would have been accepted by CloudFormation and then run at
    0.35, the opposite of the "refuse everything" the operator asked for. A test that checks
    agreement against a list only checks the values somebody thought of.
    """
    import re
    text = open("src/template.yaml", encoding="utf-8").read()
    pattern = re.search(r"AllowedPattern: '(\^\(default[^']+)'", text).group(1)
    rx = re.compile(pattern)

    for value in ("default", "0.7", "0.35", "1", "1.0", "0.01", "0.999",
                  "0", "0.0", "0.00", "2", "2.0", "1.5", "-0.4", "off", "", "Default",
                  "0.35abc", " 0.7"):
        allowed = bool(rx.match(value))
        if value == "default":
            assert allowed, "the sentinel must survive its own pattern"
            continue
        # The code takes a value exactly when it parses and lands in (0, 1.0]. Anything the
        # pattern lets through that the code then discards is a deploy that succeeds and a
        # guard that is not what was asked for, with only a log line to say so.
        try:
            parsed = float(value)
        except ValueError:
            parsed = None
        # "did the function keep what I gave it", asked without re-implementing its range
        # rule. Comparing against DEFAULT_MAX_FRAME_SPREAD instead would call an explicit
        # 0.35 a fallback, which is how the first attempt at this test failed.
        taken = parsed is not None and se._read_limit(value) == parsed
        # ONE direction. A template stricter than the code is fine — it is the outer guard,
        # and it rejecting " 0.7" (which float() would happily accept) costs a clearer error
        # at deploy time. The direction that hurts is the other one: a value CloudFormation
        # accepts and the function then discards deploys a stack whose guard is not the one
        # that was asked for, with a log line as the only trace.
        if allowed:
            assert taken, (f"{value!r}: the template allows it and the function discards it "
                           f"-> the deploy succeeds and the guard is not what was asked for")


def test_the_between_voices_refusal_says_the_guard_was_loosened_too():
    """That branch is reached ONLY when the homogeneity guard passed — possibly because it was
    loosened — and propagation then disagreed. It is the line where the override is most
    diagnostic, and it was the one line missing the note."""
    import inspect
    src = inspect.getsource(se)
    i = src.index("enrolment refused: the corrected window holds more than one ")
    assert "_limit_note()" in src[i:i + 260], src[i:i + 260]
