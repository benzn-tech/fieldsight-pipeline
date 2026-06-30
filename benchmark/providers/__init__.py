"""ASR provider adapters.

Each provider is a small, self-contained adapter exposing the same interface
(see ``base.ASRProvider``). ``build_providers(config)`` returns every adapter,
already marked configured/unconfigured based on the keys present in ``config``.
"""
from __future__ import annotations

from .base import ASRProvider, ASRResult, Segment
from .cartesia import CartesiaProvider
from .elevenlabs import ElevenLabsProvider
from .plaud import PlaudProvider
from .aws_transcribe import AWSTranscribeProvider
from .zhipu import ZhipuProvider
from .qwen import QwenProvider
from .aliyun_funasr import FunASRProvider

# Order here is the column order in the UI.
PROVIDER_CLASSES = [
    CartesiaProvider,
    ElevenLabsProvider,
    PlaudProvider,
    AWSTranscribeProvider,
    ZhipuProvider,
    QwenProvider,
    FunASRProvider,
]


def build_providers(config: dict) -> list[ASRProvider]:
    return [cls(config) for cls in PROVIDER_CLASSES]


__all__ = [
    "ASRProvider",
    "ASRResult",
    "Segment",
    "PROVIDER_CLASSES",
    "build_providers",
]
