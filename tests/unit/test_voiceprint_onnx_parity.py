"""Unit: the exported ONNX model must answer exactly what SpeechBrain answered.

This is the gate the whole Lambda-side design rests on. The function cannot carry torch and
speechbrain, so it runs the ONNX export instead — and an export that drifts would move every
similarity score by a little, silently. Every threshold measured before the drift
(`DEFAULT_MIN_MARGIN`, `DEFAULT_MAX_FRAME_SPREAD`, the Phase 0 separation) would quietly stop
meaning what it says, and nothing would fail.

What is committed here is the REFERENCE VECTORS, not the model. The model is 84 MB — a
742 KB graph plus an 83 MB external `.data` sidecar — and lives in S3 under `models/`,
beside the Silero VAD model and for the same recorded reason (BUG-02: loading that one from
a Layer shipped a different version whose output was silently wrong). A few KB of reference
vectors detect the same class of drift.

Skipped when the model or onnxruntime is absent, so CI stays green without an 84 MB
download. **Phase 5 may not start until this has actually run and passed** — a permanently
skipped gate is not a gate.

Measured 2026-08-13: 6 of 6 real enrolments at cosine 1.000000. The 0.999 threshold is the
tolerance the design allows, not the accuracy observed.
"""
import json
import os

import numpy as np
import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FIXTURES = os.path.join(REPO, "tests", "fixtures", "voiceprint_parity")
REFS = os.path.join(FIXTURES, "references.json")
MODEL = os.environ.get("ECAPA_ONNX_PATH", os.path.join(REPO, "models", "ecapa_tdnn.onnx"))
GATE = 0.999

pytestmark = pytest.mark.skipif(
    not os.path.exists(MODEL),
    reason=f"no ONNX model at {MODEL}; export it with scripts/export_ecapa_onnx.py or set "
           f"ECAPA_ONNX_PATH. The references are committed, so this skip is about the "
           f"84 MB artifact and nothing else.")


def _session():
    ort = pytest.importorskip("onnxruntime")
    return ort.InferenceSession(MODEL, providers=["CPUExecutionProvider"])


def test_the_references_exist_and_name_real_audio():
    """A parity test with no fixtures passes vacuously, which is the failure mode this
    whole file exists to prevent one layer up."""
    assert os.path.exists(REFS), "references.json is missing — re-run the export script"
    refs = json.load(open(REFS, encoding="utf-8"))
    assert refs, "no reference vectors recorded"
    for name in refs:
        assert os.path.exists(os.path.join(FIXTURES, f"{name}.npy")), (
            f"{name} has a reference vector but not the audio it came from; the test "
            f"could not re-derive it")


def test_every_reference_survives_the_export():
    import voiceprint_utils as vp

    sess = _session()
    refs = json.load(open(REFS, encoding="utf-8"))
    worst, worst_name = 1.0, None
    for name, rec in refs.items():
        clip = np.load(os.path.join(FIXTURES, f"{name}.npy"))
        got = sess.run(None, {"wav": clip[None, :].astype(np.float32),
                              "wav_lens": np.array([1.0], dtype=np.float32)})[0].ravel()
        c = vp.cosine(np.asarray(rec["embedding"], dtype=np.float64), got)
        if c < worst:
            worst, worst_name = c, name
    assert worst >= GATE, (
        f"the ONNX export no longer matches SpeechBrain: worst cosine {worst:.6f} on "
        f"{worst_name!r}, gate {GATE}. Every threshold in voiceprint_utils was measured "
        f"against the SpeechBrain embedding; a drifted export moves them all with no error.")


def test_the_model_is_not_committed_to_the_repo():
    """84 MB of weights in git is a decision, not an accident. It is deliberately NOT taken:
    the model ships in S3 under `models/`, downloaded at cold start like the VAD model.
    This pins that so a future export does not quietly land in a commit."""
    tracked = os.path.join(REPO, "models", "ecapa_tdnn.onnx.data")
    assert not os.path.exists(tracked), (
        "the 83 MB weight sidecar is in the working tree — it belongs in S3 under models/, "
        "beside silero_vad.onnx, not in the repository")
