"""Offline smoke test — no API keys, no network.

Validates the plumbing: audio normalize+chunking (needs ffmpeg), WER/CER metrics,
SQLite persistence, provider construction, and the runner's chunk/merge/score
orchestration via a fake provider.

Run:  python benchmark/tests/smoke_test.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Convenience: if system ffmpeg is missing but static_ffmpeg is installed, use it.
try:
    import shutil
    if not shutil.which("ffmpeg"):
        import static_ffmpeg
        static_ffmpeg.add_paths()
except Exception:
    pass

from core import audio as A
from core import metrics as M
from core import storage
from core.config import load_config
from core.runner import run_provider, run_benchmark
from providers import build_providers
from providers.base import ASRProvider, ASRResult, Segment

PASS, FAIL = "✅", "❌"
_failures = []


def check(name, cond):
    print(f"{PASS if cond else FAIL} {name}")
    if not cond:
        _failures.append(name)


# --- metrics ----------------------------------------------------------------
def test_metrics():
    print("\n[metrics]")
    check("perfect WER == 0", M.compute_wer("the quick brown fox", "the quick brown fox") == 0.0)
    wer = M.compute_wer("the quick brown fox", "the quick brown dog")
    check("one-word error WER == 0.25", abs(wer - 0.25) < 1e-9)
    check("punctuation/case ignored", M.compute_wer("Hello, world.", "hello world") == 0.0)
    cer = M.compute_cer("今天天气很好", "今天天气不好")
    check("Chinese CER ~1/6", abs(cer - (1 / 6)) < 1e-6)
    name, _ = M.primary_metric("今天天气很好啊", "x")
    check("Chinese ref -> CER headline", name == "CER")
    name2, _ = M.primary_metric("the quick brown fox jumps", "x")
    check("English ref -> WER headline", name2 == "WER")
    check("RTF math", M.real_time_factor(5.0, 10.0) == 0.5)


# --- audio (needs ffmpeg) ---------------------------------------------------
def test_audio():
    print("\n[audio]")
    if not A.ffmpeg_available():
        print("⏭️  ffmpeg not on PATH — skipping audio/chunk tests")
        return None
    d = tempfile.mkdtemp(prefix="asrbench_test_")
    raw = os.path.join(d, "tone.wav")
    # 5s 440Hz tone @ 44.1k stereo -> exercises normalization to 16k mono.
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=5",
         "-ac", "2", "-ar", "44100", raw],
        capture_output=True, check=True,
    )
    dur = A.probe_duration_seconds(raw)
    check("probe duration ~5s", 4.8 < dur < 5.2)
    wav = A.normalize_to_wav16k(raw, os.path.join(d, "n.wav"))
    import wave
    with wave.open(wav, "rb") as w:
        check("normalized to 16k mono", w.getframerate() == 16000 and w.getnchannels() == 1)
    chunks = A.split_wav(wav, 2.0, os.path.join(d, "chunks"))
    check("5s split into 3 chunks @2s", len(chunks) == 3)
    check("chunk offsets increase", chunks[0].offset_s == 0 and chunks[1].offset_s > 0)
    return wav


# --- storage ----------------------------------------------------------------
def test_storage():
    print("\n[storage]")
    storage.init_db()
    rid = storage.new_run_id()
    storage.save_run({"run_id": rid, "audio_filename": "t.wav", "audio_duration": 5.0,
                      "reference_text": "ref", "language_hint": "en", "diarize": True})
    storage.save_result(rid, {"provider": "Fake", "model": "m", "ok": True, "text": "hi",
                              "latency_s": 1.0, "rtf": 0.2, "audio_duration_s": 5.0,
                              "n_chunks": 1, "chunked": False, "has_diarization": False,
                              "n_speakers": 0, "wer": 0.1, "cer": 0.05, "metric_name": "WER",
                              "metric_value": 0.1, "judge_score": None, "judge_comment": "",
                              "error": "", "segments": []}, raw={"x": 1})
    got = storage.get_run(rid)
    check("run round-trips", got and got["audio_filename"] == "t.wav" and len(got["results"]) == 1)
    check("appears in list", any(r["run_id"] == rid for r in storage.list_runs()))
    storage.delete_run(rid)
    check("delete works", storage.get_run(rid) is None)


# --- providers --------------------------------------------------------------
def test_providers():
    print("\n[providers]")
    ps = build_providers({})  # empty config -> nothing configured (deterministic)
    check("6 providers built", len(ps) == 6)
    check("all unconfigured w/o keys", all(not p.is_configured() for p in ps))
    labels = {p.label for p in ps}
    for expect in ["Cartesia Ink", "ElevenLabs Scribe", "AWS Transcribe", "Zhipu GLM-ASR",
                   "Qwen3-ASR", "Ali Fun-ASR"]:
        check(f"has {expect}", expect in labels)
    diar = {p.label for p in ps if p.supports_diarization}
    check("diarization set correct",
          diar == {"AWS Transcribe", "Ali Fun-ASR", "ElevenLabs Scribe"})


# --- runner orchestration (fake provider, no network) -----------------------
class _Fake(ASRProvider):
    key, label = "fake", "Fake"
    supports_diarization = False
    max_audio_seconds = 2.0       # force chunking on >2s audio

    def is_configured(self):
        return True

    def transcribe_file(self, wav_path, language=None, diarize=False):
        return ASRResult(provider=self.label, model="fake", ok=True, text="hello",
                         segments=[Segment(0.0, 0.5, "hello")])


def test_runner(wav):
    print("\n[runner]")
    if not wav:
        print("⏭️  no wav (ffmpeg missing) — skipping runner chunk test")
        return
    d = tempfile.mkdtemp(prefix="asrbench_run_")
    fake = _Fake({})
    res = run_provider(fake, wav, 5.0, None, False, d)
    check("chunked into 3", res.n_chunks == 3 and res.chunked)
    check("merged text", res.text == "hello hello hello")
    check("offsets applied", any(s.start >= 2.0 for s in res.segments))
    check("rtf computed", res.rtf is not None and res.rtf > 0)

    out = run_benchmark([fake], wav, 5.0, "hello hello hello", None, False, {}, d)
    check("reference -> WER 0", out[0].wer == 0.0)
    out2 = run_benchmark([fake], wav, 5.0, "", None, False, {}, d)  # no key -> judge skipped
    check("no ref + no key -> no score", out2[0].metric_value is None)


def main():
    test_metrics()
    wav = test_audio()
    test_storage()
    test_providers()
    test_runner(wav)
    print("\n" + ("=" * 40))
    if _failures:
        print(f"{FAIL} {len(_failures)} check(s) failed: {_failures}")
        sys.exit(1)
    print(f"{PASS} all checks passed")


if __name__ == "__main__":
    main()
