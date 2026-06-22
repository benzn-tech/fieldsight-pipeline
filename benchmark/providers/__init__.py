"""ASR provider adapters.

Each provider is a small, self-contained adapter exposing the same interface
(see ``base.ASRProvider``). ``build_providers(config)`` returns every adapter,
already marked configured/unconfigured based on the keys present in ``config``.
"""
from __future__ import annotations

from .base import ASRProvider, ASRResult, Segment
from .cartesia import CartesiaProvider
from .aws_transcribe import AWSTranscribeProvider
from .zhipu import ZhipuProvider
from .qwen import QwenProvider
from .aliyun_funasr import FunASRProvider
from .iflytek import IFlytekProvider

# Order here is the column order in the UI.
PROVIDER_CLASSES = [
    CartesiaProvider,
    AWSTranscribeProvider,
    ZhipuProvider,
    QwenProvider,
    FunASRProvider,
    IFlytekProvider,
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
