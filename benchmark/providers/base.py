"""Common provider interface + result types.

A provider knows how to transcribe ONE already-normalized 16 kHz mono WAV file
that is within its own length limit. Chunking of long audio (for the providers
that need it) is handled centrally by ``core.runner`` so each adapter stays
small. Timing is also measured by the runner, not the adapter.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Segment:
    start: float                 # seconds from start of full audio
    end: float
    text: str
    speaker: Optional[str] = None


@dataclass
class ASRResult:
    provider: str
    model: str = ""
    ok: bool = False
    text: str = ""
    segments: list[Segment] = field(default_factory=list)
    has_diarization: bool = False
    raw: object = None           # raw provider payload (persisted as json)
    error: str = ""

    # filled in by the runner / scorer
    latency_s: float = 0.0
    audio_duration_s: float = 0.0
    rtf: Optional[float] = None
    n_chunks: int = 1
    chunked: bool = False
    wer: Optional[float] = None
    cer: Optional[float] = None
    metric_name: Optional[str] = None
    metric_value: Optional[float] = None
    judge_score: Optional[float] = None
    judge_comment: str = ""

    @property
    def n_speakers(self) -> int:
        return len({s.speaker for s in self.segments if s.speaker})

    @property
    def char_count(self) -> int:
        return len(self.text or "")

    def to_row(self) -> dict:
        return {
            "provider": self.provider,
            "model": self.model,
            "ok": self.ok,
            "text": self.text,
            "latency_s": round(self.latency_s, 3),
            "rtf": round(self.rtf, 3) if self.rtf is not None else None,
            "audio_duration_s": round(self.audio_duration_s, 3),
            "n_chunks": self.n_chunks,
            "chunked": self.chunked,
            "has_diarization": self.has_diarization,
            "n_speakers": self.n_speakers,
            "wer": self.wer,
            "cer": self.cer,
            "metric_name": self.metric_name,
            "metric_value": self.metric_value,
            "judge_score": self.judge_score,
            "judge_comment": self.judge_comment,
            "error": self.error,
            "segments": [s.__dict__ for s in self.segments],
        }


class ASRProvider(ABC):
    # --- describe the provider (override in subclasses) ---
    key: str = "base"                 # stable id used in config/db
    label: str = "Base"               # display name
    supports_diarization: bool = False
    max_audio_seconds: Optional[float] = None   # None = handles long audio itself
    needs_public_url: bool = False    # provider can only fetch a URL (e.g. DashScope file API)
    homepage: str = ""
    notes: str = ""

    def __init__(self, config: dict):
        self.config = config or {}
        self.model = ""

    @abstractmethod
    def is_configured(self) -> bool:
        """True if the required credentials are present."""

    @abstractmethod
    def transcribe_file(
        self, wav_path: str, language: Optional[str] = None, diarize: bool = False
    ) -> ASRResult:
        """Transcribe a single in-limit WAV file. Must NOT raise — return an
        ASRResult with ok=False and an error message instead."""

    # convenience for adapters
    def _result(self, **kw) -> ASRResult:
        kw.setdefault("model", self.model)
        return ASRResult(provider=self.label, **kw)

    def _fail(self, msg: str) -> ASRResult:
        return ASRResult(provider=self.label, model=self.model, ok=False, error=msg)
