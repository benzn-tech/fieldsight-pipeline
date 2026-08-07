"""Unit: the audio handed to ASR is loudness-normalised; the upload is not.

Measured on a real site recording (UCPK2, 2026-08-07, 4:49 of ordinary
conversation): mean -39.7 dBFS, median second at -46. Seven identical
transcription requests on that file returned 173-815 words, and two of the
seven began at 01:57 — silently discarding the first two minutes. No error, no
log line, no retry. With `acompressor` + `loudnorm` applied first, three runs
returned 415/410/465 words, all starting at 13 s, and all three resolved three
speakers, which no other treatment managed.

So the properties pinned here are:

  1. the filter chain runs before anything reaches `audio_segments/`,
  2. `users/.../audio/` — the raw upload, which is evidence — is untouched,
  3. the output stays 16 kHz mono PCM. `loudnorm` in single-pass mode emits
     192 kHz unless the rate is pinned on the command line, and a 192 kHz
     segment would be handed to VAD offsets computed at 16 kHz,
  4. normalisation is best-effort: if ffmpeg fails, the chunk still ships at
     its original loudness rather than being lost,
  5. a length or rate mismatch is refused rather than sliced, because every
     `audio_segments/` offset is computed against the pre-normalisation sample
     array and a shifted array would silently misplace every timestamp, and
  6. what happened is written to the sidecar. All three bugs found this week
     (dropped first two minutes, missing last ten, 47% truncation) were silent;
     a treatment that leaves no record is the same failure shape.

This is NOT noise reduction. Pre-ASR noise suppression is known here to cost
accuracy and that finding stands — NS discards information, gain and
compression redistribute it. Neither result licenses the other.
"""
import os
import shutil
import struct
import subprocess
import wave

import pytest

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")

import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import lambda_vad  # noqa: E402


SRC = os.path.join(os.path.dirname(__file__), "..", "..", "src", "lambda_vad.py")


def _source():
    with open(SRC, encoding="utf-8") as fh:
        return fh.read()


# ------------------------------------------------------------------
# The command
# ------------------------------------------------------------------

def test_the_filter_chain_is_compression_then_loudnorm():
    """Order matters: compressing first lifts the quiet 96% toward the loud 4%,
    and loudnorm then places the result at a known target. Reversed, loudnorm
    normalises a 44 dB spread and the quiet speech stays quiet."""
    chain = lambda_vad.NORMALISE_FILTER
    assert chain.index("acompressor") < chain.index("loudnorm")
    assert "threshold=-30dB" in chain and "ratio=4" in chain
    assert "I=-16" in chain


def test_the_output_rate_is_pinned_to_the_vad_rate():
    """`loudnorm` emits 192 kHz when the output rate is left to ffmpeg — this
    is a real default, not a hypothetical, and it would hand the transcriber a
    file whose sample offsets no longer mean what the filenames say."""
    cmd = lambda_vad.build_normalise_cmd("in.wav", "out.wav", 16000)
    assert "-ar" in cmd, "the sample rate must be explicit"
    assert cmd[cmd.index("-ar") + 1] == "16000"
    assert cmd[cmd.index("-ac") + 1] == "1"
    assert cmd[cmd.index("-acodec") + 1] == "pcm_s16le"
    assert cmd[cmd.index("-af") + 1] == lambda_vad.NORMALISE_FILTER


# ------------------------------------------------------------------
# Best-effort behaviour
# ------------------------------------------------------------------

def _silence_wav(path, seconds=1.0, rate=16000, amplitude=300):
    frames = int(seconds * rate)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", amplitude if i % 400 < 200 else -amplitude)
                               for i in range(frames)))


def test_an_ffmpeg_failure_ships_the_original_rather_than_losing_the_chunk(tmp_path, monkeypatch):
    """A chunk transcribed quietly is worse than one transcribed loudly, and
    far better than one that never arrives."""
    src = str(tmp_path / "audio_16k.wav")
    _silence_wav(src)
    samples, sr = lambda_vad.read_wav_pcm(src)

    def boom(*a, **k):
        raise RuntimeError("ffmpeg exploded")

    monkeypatch.setattr(lambda_vad, "normalise_audio_ffmpeg", boom)
    path, out_samples, applied = lambda_vad.normalise_for_asr(
        src, str(tmp_path), len(samples), sr)

    assert applied is False
    assert path == src
    assert out_samples == samples


def test_a_length_mismatch_is_refused(tmp_path, monkeypatch):
    """Every `audio_segments/` offset is an index into the pre-normalisation
    array. A filter that shifted or trimmed the audio would keep producing
    plausible files with every timestamp wrong — the worst kind of wrong,
    because nothing downstream can detect it."""
    src = str(tmp_path / "audio_16k.wav")
    _silence_wav(src, seconds=1.0)
    samples, sr = lambda_vad.read_wav_pcm(src)

    def shorter(input_path, output_path, sample_rate=16000, timeout=120):
        _silence_wav(output_path, seconds=0.5)
        return output_path

    monkeypatch.setattr(lambda_vad, "normalise_audio_ffmpeg", shorter)
    path, out_samples, applied = lambda_vad.normalise_for_asr(
        src, str(tmp_path), len(samples), sr)

    assert applied is False
    assert path == src
    assert out_samples == samples, "the original audio, not the shortened one"


def test_disabled_by_configuration_does_not_run_ffmpeg(tmp_path, monkeypatch):
    src = str(tmp_path / "audio_16k.wav")
    _silence_wav(src)
    samples, sr = lambda_vad.read_wav_pcm(src)

    called = []
    monkeypatch.setattr(lambda_vad, "NORMALISE_AUDIO", False)
    monkeypatch.setattr(lambda_vad, "normalise_audio_ffmpeg",
                        lambda *a, **k: called.append(1))
    path, out_samples, applied = lambda_vad.normalise_for_asr(
        src, str(tmp_path), len(samples), sr)

    assert called == [] and applied is False and path == src
    assert out_samples == samples


def test_it_takes_a_count_not_the_array(tmp_path):
    """`read_wav_pcm` returns a Python list at ~32 bytes a sample (BUG-04: a
    2-hour WAV is 3.3 GB against a 3008 MB lambda). Holding the original and the
    normalised copy at once would halve the longest file this function can
    process — so it is handed the expected COUNT, and the caller drops its
    reference before the replacement is read. Pinned because the natural
    refactor is to pass the array, and nothing would fail until a customer
    uploaded a long enough recording."""
    import inspect

    params = list(inspect.signature(lambda_vad.normalise_for_asr).parameters)
    assert params[2] == "expected_len", f"third parameter is {params[2]!r}"

    body = _source()
    call = body[body.index("audio_samples = None"):]
    assert "n_samples = len(audio_samples)" in body
    assert call.index("normalise_for_asr(") < call.index("dbfs_after"), (
        "the original must be released before the replacement is read")


# ------------------------------------------------------------------
# Wiring: the raw upload stays evidence, the sidecar records the treatment
# ------------------------------------------------------------------

def test_the_raw_upload_is_never_rewritten():
    """`users/.../audio/` is the object the device uploaded. It is evidence in
    the sense that matters on a construction site, and the pipeline may derive
    from it but must not edit it in place."""
    body = _source()
    assert "normalise_for_asr" in body
    # the only ffmpeg writes are into tmp_dir, never back to an S3 media key
    assert "normalise_audio_ffmpeg(key" not in body
    # normalisation feeds the export path, not the download path
    assert body.index("normalise_for_asr") > body.index("extract_audio_ffmpeg(input_path")


def test_both_upload_paths_ship_the_normalised_audio():
    """There are two ways audio leaves this function — the per-segment export
    and the whole-chunk fallback used when DROP_SILENT_CHUNKS=false. A fix
    applied to only one of them is the shape of half this repo's bug list."""
    body = _source()
    assert "upload_file(\n                export_wav_path" in body or \
           "export_wav_path, bucket, seg_s3_key" in body, \
           "the whole-audio fallback must upload the normalised file"
    assert "write_wav_segment(segment_samples" in body


def test_the_sidecar_records_whether_normalisation_happened():
    body = _source()
    assert "'normalised':" in body
    assert "'loudness_dbfs_before'" in body
    assert "'loudness_dbfs_after'" in body


def test_mean_dbfs_of_silence_is_reported_not_crashed():
    """A fully silent chunk is common (45 of 129 on one real meeting). log(0)
    must not be the thing that takes the lambda down."""
    assert lambda_vad.mean_dbfs([0.0] * 100) == -120.0
    assert lambda_vad.mean_dbfs([]) == -120.0
    full_scale = lambda_vad.mean_dbfs([1.0, -1.0] * 50)
    assert -0.01 < full_scale < 0.01


# ------------------------------------------------------------------
# Against real ffmpeg, when one is available
# ------------------------------------------------------------------

@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not on PATH")
def test_real_ffmpeg_keeps_the_rate_and_the_sample_count(tmp_path):
    """The claim the whole change rests on, checked against the actual binary
    rather than against a belief about it: same sample count, same rate, and
    measurably louder."""
    src = str(tmp_path / "audio_16k.wav")
    _silence_wav(src, seconds=3.0, amplitude=300)  # ~ -40 dBFS, site-quiet
    out = str(tmp_path / "audio_16k_norm.wav")

    monkey = lambda_vad.FFMPEG_PATH
    assert subprocess.run([monkey, "-version"], capture_output=True).returncode == 0

    lambda_vad.normalise_audio_ffmpeg(src, out, 16000)

    before, sr_a = lambda_vad.read_wav_pcm(src)
    after, sr_b = lambda_vad.read_wav_pcm(out)
    assert sr_a == sr_b == 16000
    assert len(before) == len(after), "a shifted array would misplace every offset"
    assert lambda_vad.mean_dbfs(after) > lambda_vad.mean_dbfs(before) + 10
