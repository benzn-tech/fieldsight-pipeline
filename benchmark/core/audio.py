"""Audio helpers built on ffmpeg/ffprobe.

We deliberately shell out to ffmpeg instead of using pydub so we don't depend on
the stdlib ``audioop`` module (removed in Python 3.13) and so we can handle any
container the field devices throw at us (mp4/wav/m4a/ogg/...).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import wave
from dataclasses import dataclass


class FFmpegMissing(RuntimeError):
    pass


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _require_ffmpeg() -> None:
    if not ffmpeg_available():
        raise FFmpegMissing(
            "ffmpeg/ffprobe not found on PATH. Install it "
            "(macOS: `brew install ffmpeg`, Debian/Ubuntu: `apt-get install ffmpeg`)."
        )


def probe_duration_seconds(path: str) -> float:
    """Return audio duration in seconds (works on any ffprobe-readable file)."""
    _require_ffmpeg()
    out = subprocess.run(
        [
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_format", "-show_streams", path,
        ],
        capture_output=True, text=True, check=True,
    )
    meta = json.loads(out.stdout or "{}")
    if "format" in meta and meta["format"].get("duration"):
        return float(meta["format"]["duration"])
    for stream in meta.get("streams", []):
        if stream.get("duration"):
            return float(stream["duration"])
    return 0.0


def normalize_to_wav16k(src_path: str, dst_path: str) -> str:
    """Convert any input to 16 kHz mono 16-bit PCM WAV (the FieldSight pipeline
    canonical format). Returns dst_path."""
    _require_ffmpeg()
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", src_path,
            "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
            "-vn", dst_path,
        ],
        capture_output=True, text=True, check=True,
    )
    return dst_path


@dataclass
class Chunk:
    path: str
    index: int
    offset_s: float       # start time of this chunk inside the full audio
    duration_s: float


def split_wav(path: str, max_seconds: float, out_dir: str) -> list[Chunk]:
    """Split a WAV file into <= max_seconds chunks using ffmpeg's segment muxer.

    Returns chunks in order with their absolute time offsets so transcript
    timestamps can be re-based after recombining.
    """
    _require_ffmpeg()
    os.makedirs(out_dir, exist_ok=True)
    pattern = os.path.join(out_dir, "chunk_%04d.wav")
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", path,
            "-f", "segment", "-segment_time", str(max_seconds),
            "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
            "-reset_timestamps", "1", pattern,
        ],
        capture_output=True, text=True, check=True,
    )
    chunks: list[Chunk] = []
    offset = 0.0
    for i, name in enumerate(sorted(os.listdir(out_dir))):
        if not name.startswith("chunk_"):
            continue
        cpath = os.path.join(out_dir, name)
        dur = _wav_duration(cpath)
        chunks.append(Chunk(path=cpath, index=i, offset_s=offset, duration_s=dur))
        offset += dur
    return chunks


def _wav_duration(path: str) -> float:
    with wave.open(path, "rb") as w:
        frames = w.getnframes()
        rate = w.getframerate() or 16000
        return frames / float(rate)


def make_workdir(prefix: str = "asrbench_") -> str:
    return tempfile.mkdtemp(prefix=prefix)
