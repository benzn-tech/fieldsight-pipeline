"""Qwen3-ASR-Flash via Alibaba DashScope (MultiModalConversation).

    dashscope.base_http_api_url = <intl|cn endpoint>
    dashscope.MultiModalConversation.call(
        model="qwen3-asr-flash",
        messages=[{"role":"user","content":[{"audio": "file://<abs path>"}]}],
        result_format="message",
        asr_options={"language": "zh"},   # optional
    )

Limits: <= 3 min, <= 10 MB per call → runner chunks longer audio. No diarization.
``dashscope`` is imported lazily so the app still loads if the SDK is absent.
"""
from __future__ import annotations

import os

from .base import ASRProvider, ASRResult

_REGION_URL = {
    "intl": "https://dashscope-intl.aliyuncs.com/api/v1",
    "cn": "https://dashscope.aliyuncs.com/api/v1",
}


class QwenProvider(ASRProvider):
    key = "qwen"
    label = "Qwen3-ASR"
    supports_diarization = False
    max_audio_seconds = 170           # API hard limit is 180s; keep margin
    homepage = "https://www.alibabacloud.com/help/en/model-studio/qwen-speech-recognition"
    notes = "Qwen3-ASR-Flash. 3min/10MB cap → long audio chunked. No diarization."

    def __init__(self, config: dict):
        super().__init__(config)
        self.api_key = config.get("DASHSCOPE_API_KEY", "")
        self.region = (config.get("DASHSCOPE_REGION") or "intl").lower()
        self.model = config.get("QWEN_ASR_MODEL", "qwen3-asr-flash")

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def transcribe_file(self, wav_path, language=None, diarize=False) -> ASRResult:
        if not self.is_configured():
            return self._fail("DASHSCOPE_API_KEY not set")
        try:
            import dashscope
        except Exception:  # noqa: BLE001
            return self._fail("dashscope SDK not installed (pip install dashscope)")

        dashscope.base_http_api_url = _REGION_URL.get(self.region, _REGION_URL["intl"])
        asr_options = {}
        if language:
            asr_options["language"] = language
        messages = [
            {"role": "user", "content": [{"audio": "file://" + os.path.abspath(wav_path)}]}
        ]
        try:
            resp = dashscope.MultiModalConversation.call(
                api_key=self.api_key,
                model=self.model,
                messages=messages,
                result_format="message",
                asr_options=asr_options or None,
            )
        except Exception as exc:  # noqa: BLE001
            return self._fail(f"{type(exc).__name__}: {exc}")

        if getattr(resp, "status_code", 200) != 200:
            return self._fail(f"status {resp.status_code}: {getattr(resp, 'message', '')}")

        text = self._extract_text(resp)
        raw = resp.output if hasattr(resp, "output") else resp
        return self._result(ok=True, text=text, raw=_to_dict(raw))

    @staticmethod
    def _extract_text(resp) -> str:
        try:
            content = resp.output.choices[0].message.content
        except Exception:
            return ""
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get("text"):
                    parts.append(item["text"])
                elif isinstance(item, str):
                    parts.append(item)
            return " ".join(parts).strip()
        return str(content).strip()


def _to_dict(obj):
    try:
        if hasattr(obj, "to_dict"):
            return obj.to_dict()
        import json
        return json.loads(json.dumps(obj, default=lambda o: getattr(o, "__dict__", str(o))))
    except Exception:
        return str(obj)
