"""ElevenLabs Scribe STT — batch transcription.

Uses the official ``elevenlabs`` SDK (``client.speech_to_text.convert``, which
posts to ``/v1/speech-to-text``). **Scribe v2** is the latest batch model:
90+ languages with automatic detection (English + Mandarin in one model, no
pre-routing), word-level timestamps, and speaker diarization (``speaker_id`` per
word). It handles long audio server-side (up to ~10 h / 3 GB), so we send the
whole file and let Scribe auto-detect the language — the most interesting thing
to benchmark for our bilingual (NZ English + Mandarin) site audio.
"""
from __future__ import annotations

from .base import ASRProvider, ASRResult, Segment


class ElevenLabsProvider(ASRProvider):
    key = "elevenlabs"
    label = "ElevenLabs Scribe"
    supports_diarization = True
    max_audio_seconds = None          # handles up to ~10 h server-side
    homepage = "https://elevenlabs.io/docs/api-reference/speech-to-text/convert"
    notes = "Scribe v2: 90+ langs auto-detect (en+zh in one model), word timestamps, diarization. Batch."

    def __init__(self, config: dict):
        super().__init__(config)
        self.api_key = config.get("ELEVENLABS_API_KEY", "")
        self.model = config.get("ELEVENLABS_MODEL", "scribe_v2")

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def transcribe_file(self, wav_path, language=None, diarize=False) -> ASRResult:
        if not self.is_configured():
            return self._fail("ELEVENLABS_API_KEY not set")
        try:
            from elevenlabs import ElevenLabs
        except Exception:  # noqa: BLE001
            return self._fail("elevenlabs SDK not installed (pip install elevenlabs)")

        try:
            client = ElevenLabs(api_key=self.api_key)
            with open(wav_path, "rb") as fh:
                # Omit language_code on purpose: Scribe auto-detects, which is the
                # behaviour we want to test on mixed en/zh audio.
                resp = client.speech_to_text.convert(
                    file=fh,
                    model_id=self.model,
                    diarize=diarize,
                    timestamps_granularity="word",
                    tag_audio_events=False,
                )
        except Exception as exc:  # noqa: BLE001
            return self._fail(f"{type(exc).__name__}: {exc}")

        text = (getattr(resp, "text", "") or "").strip()
        segments: list[Segment] = []
        for w in (getattr(resp, "words", None) or []):
            if getattr(w, "type", "word") != "word":
                continue   # skip 'spacing' and 'audio_event' markers
            segments.append(Segment(
                start=float(getattr(w, "start", 0.0) or 0.0),
                end=float(getattr(w, "end", 0.0) or 0.0),
                text=getattr(w, "text", "") or "",
                speaker=getattr(w, "speaker_id", None),
            ))
        has_diar = bool(diarize and any(s.speaker for s in segments))
        return self._result(ok=True, text=text, segments=segments,
                            has_diarization=has_diar, raw=_safe_dump(resp))


def _safe_dump(resp):
    for attr in ("model_dump", "dict"):
        fn = getattr(resp, attr, None)
        if fn:
            try:
                return fn()
            except Exception:  # noqa: BLE001
                pass
    return {"text": getattr(resp, "text", None)}
