"""Cartesia Ink STT.

Two paths, chosen by the configured model:

  * **ink-2** (default) — streaming over WebSocket via the official ``cartesia``
    SDK's *auto-finalize* endpoint. ink-2 is streaming-only (the batch ``/stt``
    endpoint accepts ink-whisper only). We feed the canonical 16 kHz mono s16le
    WAV as raw PCM, send ``{"type": "close"}`` to flush buffered audio, and
    collect the definitive ``turn.end`` transcript of every detected turn.

  * **ink-whisper** — batch ``POST https://api.cartesia.ai/stt`` (multipart),
    which also returns word-level timestamps.

Ink does turn detection, not multi-speaker labelling, so ``supports_diarization``
is False either way.
"""
from __future__ import annotations

import threading
import wave

import requests

from .base import ASRProvider, ASRResult, Segment

STT_URL = "https://api.cartesia.ai/stt"
_SAMPLE_RATE = 16000
_CHUNK_BYTES = 3200          # 100 ms of 16 kHz mono s16le (16000 * 0.1 * 2 bytes)


class CartesiaProvider(ASRProvider):
    key = "cartesia"
    label = "Cartesia Ink"
    supports_diarization = False
    max_audio_seconds = None          # smart VAD / turn detection handles long audio
    homepage = "https://docs.cartesia.ai/build-with-cartesia/models/stt"
    notes = "ink-2 streaming (WebSocket, built-in turn detection); ink-whisper batch. No multi-speaker diarization."

    def __init__(self, config: dict):
        super().__init__(config)
        self.api_key = config.get("CARTESIA_API_KEY", "")
        self.version = config.get("CARTESIA_VERSION", "2025-04-16")
        self.model = config.get("CARTESIA_MODEL", "ink-2")

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def transcribe_file(self, wav_path, language=None, diarize=False) -> ASRResult:
        if not self.is_configured():
            return self._fail("CARTESIA_API_KEY not set")
        # ink-whisper is batch-only; everything else (ink-2) is streaming-only.
        if self.model.startswith("ink-whisper"):
            return self._transcribe_batch(wav_path, language)
        return self._transcribe_stream(wav_path)

    # --- ink-2: streaming WebSocket (auto-finalize) ------------------------- #
    def _transcribe_stream(self, wav_path) -> ASRResult:
        try:
            from cartesia import Cartesia
        except Exception:  # noqa: BLE001
            return self._fail("cartesia SDK not installed (pip install cartesia)")

        finals: list[str] = []
        send_err: list[Exception] = []
        try:
            client = Cartesia(api_key=self.api_key)
            with client.stt.auto_finalize.websocket(
                model=self.model,
                encoding="pcm_s16le",
                sample_rate=_SAMPLE_RATE,
            ) as conn:
                # Stream audio from a worker so a slow/full send can't deadlock the
                # receive loop (websockets allows concurrent send + recv).
                def _pump() -> None:
                    try:
                        for chunk in _pcm_chunks(wav_path, _CHUNK_BYTES):
                            conn.send_raw(chunk)
                        conn.send({"type": "close"})   # flush buffered audio, close cleanly
                    except Exception as exc:  # noqa: BLE001
                        send_err.append(exc)
                        try:
                            conn.close()               # unblock the recv iterator
                        except Exception:  # noqa: BLE001
                            pass

                pump = threading.Thread(target=_pump, daemon=True)
                pump.start()
                for resp in conn:
                    rtype = getattr(resp, "type", None)
                    if rtype == "turn.end":
                        txt = (getattr(resp, "transcript", "") or "").strip()
                        if txt:
                            finals.append(txt)
                    elif rtype == "error":
                        return self._fail(
                            f"cartesia stream error: {getattr(resp, 'message', None) or resp}"
                        )
                pump.join(timeout=10)
        except Exception as exc:  # noqa: BLE001
            return self._fail(f"{type(exc).__name__}: {exc}")
        if send_err:
            return self._fail(f"audio send failed: {send_err[0]}")

        return self._result(ok=True, text=" ".join(finals).strip(),
                            segments=[], raw={"turns": finals})

    # --- ink-whisper: batch multipart --------------------------------------- #
    def _transcribe_batch(self, wav_path, language) -> ASRResult:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Cartesia-Version": self.version,
        }
        data = [("model", self.model), ("timestamp_granularities[]", "word")]
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
        segments = [
            Segment(start=float(w.get("start", 0.0)), end=float(w.get("end", 0.0)),
                    text=w.get("word", ""))
            for w in (payload.get("words") or [])
        ]
        return self._result(ok=True, text=text, segments=segments, raw=payload)


def _pcm_chunks(wav_path: str, chunk_bytes: int):
    """Yield raw PCM_s16le byte chunks from a mono 16-bit WAV (header stripped)."""
    with wave.open(wav_path, "rb") as wf:
        width = max(1, wf.getsampwidth() * wf.getnchannels())
        frames = max(1, chunk_bytes // width)
        while True:
            data = wf.readframes(frames)
            if not data:
                break
            yield data
