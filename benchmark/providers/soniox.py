"""Soniox STT — async file transcription via the official ``soniox`` SDK.

One call does the whole flow: ``stt.transcribe_and_wait_with_tokens`` uploads the
file, creates the transcription (model default **stt-async-v5**, files up to
~5 h), polls to completion, returns ``TranscriptionTranscript(text, tokens[])``,
and with ``delete_after=True`` removes the transcription + uploaded file
server-side afterwards.

Tokens are subword pieces carrying ``start_ms/end_ms``, ``speaker`` (when
``enable_speaker_diarization``) and ``language`` (when language identification is
on) — we group consecutive same-speaker tokens into segments.
"""
from __future__ import annotations

from .base import ASRProvider, ASRResult, Segment

_WAIT_TIMEOUT_S = 900


class SonioxProvider(ASRProvider):
    key = "soniox"
    label = "Soniox"
    supports_diarization = True
    max_audio_seconds = None          # async API takes files up to ~5 h
    homepage = "https://soniox.com/docs/stt/async/async-transcription"
    notes = "stt-async-v5: 60+ langs, language ID + diarization, token timestamps. Async upload+poll via SDK."

    def __init__(self, config: dict):
        super().__init__(config)
        self.api_key = config.get("SONIOX_API_KEY", "")
        self.model = config.get("SONIOX_MODEL", "stt-async-v5")

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def transcribe_file(self, wav_path, language=None, diarize=False) -> ASRResult:
        if not self.is_configured():
            return self._fail("SONIOX_API_KEY not set")
        try:
            from soniox import SonioxClient
            from soniox.types import CreateTranscriptionConfig
        except Exception:  # noqa: BLE001
            return self._fail("soniox SDK not installed (pip install soniox)")

        cfg = CreateTranscriptionConfig(
            enable_speaker_diarization=bool(diarize),
            enable_language_identification=True,
            # Hints (not forced): our audio is NZ English + Mandarin.
            language_hints=[language] if language else ["en", "zh"],
        )
        client = SonioxClient(api_key=self.api_key)
        try:
            result = client.stt.transcribe_and_wait_with_tokens(
                model=self.model, file=wav_path, delete_after=True,
                wait_timeout_sec=_WAIT_TIMEOUT_S, config=cfg,
            )
        except Exception as exc:  # noqa: BLE001
            return self._fail(f"{type(exc).__name__}: {exc}")
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

        text = (getattr(result, "text", "") or "").strip()
        segments = _tokens_to_segments(getattr(result, "tokens", None) or [])
        has_diar = bool(diarize and any(s.speaker for s in segments))
        return self._result(ok=True, text=text, segments=segments,
                            has_diarization=has_diar,
                            raw=_safe_dump(result))


def _tokens_to_segments(tokens) -> list[Segment]:
    """Group consecutive same-speaker tokens into segments (token.text pieces
    concatenate directly — Soniox includes spacing in the token text)."""
    segments: list[Segment] = []
    cur = None
    for t in tokens:
        spk = _speaker_label(getattr(t, "speaker", None))
        start = (getattr(t, "start_ms", None) or 0) / 1000.0
        end = (getattr(t, "end_ms", None) or 0) / 1000.0
        piece = getattr(t, "text", "") or ""
        if cur is None or cur.speaker != spk:
            cur = Segment(start=start, end=end, text=piece, speaker=spk)
            segments.append(cur)
        else:
            cur.text += piece
            cur.end = end or cur.end
    for s in segments:
        s.text = s.text.strip()
    return [s for s in segments if s.text]


def _speaker_label(v):
    if v is None or v == "":
        return None
    v = str(v)
    return f"Speaker {v}" if v.isdigit() else v


def _safe_dump(result):
    for attr in ("model_dump", "dict"):
        fn = getattr(result, attr, None)
        if fn:
            try:
                return fn()
            except Exception:  # noqa: BLE001
                pass
    return {"text": getattr(result, "text", None)}
