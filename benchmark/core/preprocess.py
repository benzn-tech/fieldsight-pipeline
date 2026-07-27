"""Optional audio preprocessing for A/B experiments.

Group B of the site-noise experiment: strip background noise with the
ElevenLabs **Voice Isolator** before EVERY engine transcribes, so the only
variable between groups is the preprocessing. Uses the same
``ELEVENLABS_API_KEY`` as the Scribe provider. The isolated audio comes back as
encoded bytes (mp3), so we re-normalize to the canonical 16 kHz mono WAV.
"""
from __future__ import annotations

import os

from . import audio as A


def voice_isolate(config: dict, wav_path: str, workdir: str) -> str:
    """Run ElevenLabs Voice Isolator on ``wav_path``; return a new 16 kHz WAV."""
    api_key = config.get("ELEVENLABS_API_KEY")
    if not api_key:
        raise RuntimeError("Voice Isolator needs ELEVENLABS_API_KEY")
    from elevenlabs import ElevenLabs

    client = ElevenLabs(api_key=api_key)
    with open(wav_path, "rb") as fh:
        raw = b"".join(client.audio_isolation.convert(audio=fh))
    if not raw:
        raise RuntimeError("Voice Isolator returned no audio")

    iso_src = os.path.join(workdir, "isolated_src.mp3")
    with open(iso_src, "wb") as f:
        f.write(raw)
    out = os.path.join(workdir, "isolated16k.wav")
    A.normalize_to_wav16k(iso_src, out)
    return out
