"""Cartesia Ink STT — batch transcription.

POST https://api.cartesia.ai/stt  (multipart)
  headers: Authorization: Bearer <key>, Cartesia-Version: <date>
  form:    file, model=ink-whisper, [language], [timestamp_granularities[]=word]

Handles long audio itself (Ink has built-in smart VAD / endpointing), so we send
the whole file. No speaker diarization — Ink does turn detection, not multi-
speaker labelling, so ``supports_diarization`` is False.
"""
from __future__ import annotations

import requests

from .base import ASRProvider, ASRResult, Segment

STT_URL = "https://api.cartesia.ai/stt"


class CartesiaProvider(ASRProvider):
    key = "cartesia"
    label = "Cartesia Ink"
    supports_diarization = False
    max_audio_seconds = None          # smart VAD handles long audio
    homepage = "https://docs.cartesia.ai/api-reference/stt/transcribe"
    notes = "Real-time-focused; built-in smart VAD. No multi-speaker diarization."

    def __init__(self, config: dict):
        super().__init__(config)
        self.api_key = config.get("CARTESIA_API_KEY", "")
        self.version = config.get("CARTESIA_VERSION", "2025-04-16")
        self.model = config.get("CARTESIA_MODEL", "ink-whisper")

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def transcribe_file(self, wav_path, language=None, diarize=False) -> ASRResult:
        if not self.is_configured():
            return self._fail("CARTESIA_API_KEY not set")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Cartesia-Version": self.version,
        }
        data = [
            ("model", self.model),
            ("timestamp_granularities[]", "word"),
        ]
        if language:
            data.append(("language", language))
        try:
            with open(wav_path, "rb") as fh:
                files = {"file": ("audio.wav", fh, "audio/wav")}
                resp = requests.post(
                    STT_URL, headers=headers, data=data, files=files, timeout=600
                )
            if resp.status_code >= 400:
                return self._fail(f"HTTP {resp.status_code}: {resp.text[:300]}")
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            return self._fail(f"{type(exc).__name__}: {exc}")

        text = (payload.get("text") or "").strip()
        segments: list[Segment] = []
        # Word-level timestamps come back as `words: [{word,start,end}]`.
        words = payload.get("words") or []
        if words:
            segments = [
                Segment(
                    start=float(w.get("start", 0.0)),
                    end=float(w.get("end", 0.0)),
                    text=w.get("word", ""),
                )
                for w in words
            ]
        return self._result(ok=True, text=text, segments=segments, raw=payload)
