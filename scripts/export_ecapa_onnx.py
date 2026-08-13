#!/usr/bin/env python3
"""Export ECAPA-TDNN to ONNX, and record the reference vectors that prove it did not drift.

Runs on a dev box, never in CI:

    uv run --with numpy --with torch --with speechbrain --with soundfile \
           --with onnx --with onnxruntime --with onnxscript \
           python scripts/export_ecapa_onnx.py --enrol <dir of wavs> --out <dir>

Why an export at all: the Lambda cannot carry torch + speechbrain, and a model runtime is
not something an inference function should pay for. onnxruntime alone is the plan's option A.

**Point `--enrol` at the synthetic fixtures in tests/fixtures/voiceprint_parity/, not at real
recordings.** This script used to be run against six colleagues' enrolment audio, and the
result — their voices and their voiceprints — was committed to the repository, where it sat
outside consent, expiry and withdrawal until 2026-08-14. Parity asserts that the exported
ONNX answers what SpeechBrain answered, which holds for any audio; there is no reason for a
person to be in it.

Why reference vectors are committed and the model is not: the model is 84 MB (742 KB graph
plus an 83 MB external `.data` sidecar) and lives in S3 under `models/`, the same place and
for the same reason as the Silero VAD model — BUG-02 records that loading that one from a
Lambda Layer shipped a DIFFERENT VERSION whose output was silently wrong. The reference
vectors are a few KB and are the only thing a test needs to detect the same class of drift.

Measured 2026-08-13 on four real enrolments: cosine(speechbrain, onnx) = 1.000000 on all
four. The gate is 0.999; this is not a near miss, it is bit-level agreement.

Three obstacles, recorded because each cost a run:

  * speechbrain registers LazyModule shells in `sys.modules`. `torch.onnx.export` walks
    them through `inspect.getframeinfo`, touches `__file__`, and the shell tries to import
    an optional dependency (k2) that is not installed. The export dies on a module nothing
    here uses. They are dropped before tracing.
  * The current torch exporter needs `onnxscript`, which is not a torch dependency.
  * The export SUCCEEDS and then the process dies printing a tick character, because the
    Windows console is GBK. Set `PYTHONUTF8=1`. The traceback points at the export.
"""
import argparse
import glob
import json
import os
import sys
import wave

import numpy as np

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))
sys.path.insert(0, os.path.join(REPO, "src"))

GATE = 0.999
REF_SECONDS = 5.0        # enough to be a real embedding, short enough to keep fixtures small


def read_wav(path):
    with wave.open(path, "rb") as w:
        sr, n = w.getframerate(), w.getnframes()
        raw = w.readframes(n)
    if w.getsampwidth() != 2:
        raise SystemExit(f"{path}: expected 16-bit PCM")
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0, sr


def drop_lazy_shells():
    """See the module docstring — otherwise the export dies on an unrelated optional dep."""
    import speechbrain.utils.importutils as iu
    lazy = getattr(iu, "LazyModule", ())
    for name in [n for n, m in list(sys.modules.items()) if isinstance(m, lazy)]:
        sys.modules.pop(name, None)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--enrol", required=True, help="dir of 16 kHz mono wavs for references")
    ap.add_argument("--out", required=True, help="dir to write the .onnx and its .data into")
    ap.add_argument("--fixtures", default=os.path.join(REPO, "tests", "fixtures",
                                                       "voiceprint_parity"))
    args = ap.parse_args()

    import torch
    import speaker_phase0 as sp
    import voiceprint_utils as vp

    os.makedirs(args.out, exist_ok=True)
    os.makedirs(args.fixtures, exist_ok=True)
    onnx_path = os.path.join(args.out, "ecapa_tdnn.onnx")

    emb = sp.Embedder(cache_dir=os.path.join(args.out, "ecapa"))

    class Wrap(torch.nn.Module):
        """Export `encode_batch` — what the harness calls — not the raw encoder."""

        def __init__(self, m):
            super().__init__()
            self.m = m

        def forward(self, wav, lens):
            return self.m.encode_batch(wav, lens)

    drop_lazy_shells()
    torch.onnx.export(
        Wrap(emb.model).eval(),
        (torch.randn(1, 16000 * 3), torch.tensor([1.0])),
        onnx_path,
        input_names=["wav", "wav_lens"], output_names=["embedding"],
        dynamic_axes={"wav": {0: "batch", 1: "samples"}, "embedding": {0: "batch"}},
        opset_version=17,
    )
    print(f"exported {onnx_path}")

    import onnxruntime as ort
    sess = ort.InferenceSession(onnx_path, providers=["CPUExecutionProvider"])

    refs, worst = {}, 1.0
    for p in sorted(glob.glob(os.path.join(args.enrol, "*.wav"))):
        name = os.path.basename(p)[:-4]
        audio, sr = read_wav(p)
        if sr != 16000:
            print(f"  {name}: {sr} Hz, skipped — the model is 16 kHz only")
            continue
        clip = audio[: int(REF_SECONDS * sr)]
        ref = emb.embed(clip, sr)
        got = sess.run(None, {"wav": clip[None, :].astype(np.float32),
                              "wav_lens": np.array([1.0], dtype=np.float32)})[0].ravel()
        c = vp.cosine(ref, got)
        worst = min(worst, c)
        print(f"  {name:14s} cosine(speechbrain, onnx) = {c:.6f}")
        refs[name] = {"embedding": [round(float(x), 8) for x in ref],
                      "seconds": REF_SECONDS, "sample_rate": sr}
        # the audio the vector came from, so the test does not depend on an external path
        np.save(os.path.join(args.fixtures, f"{name}.npy"), clip.astype(np.float32))

    with open(os.path.join(args.fixtures, "references.json"), "w", encoding="utf-8") as fh:
        json.dump(refs, fh, indent=2)

    print(f"\nworst {worst:.6f}   gate {GATE}   -> {'PASS' if worst >= GATE else 'FAIL'}")
    print(f"references written to {args.fixtures}")
    if worst < GATE:
        raise SystemExit("export drifted — do not ship this model")


if __name__ == "__main__":
    main()
