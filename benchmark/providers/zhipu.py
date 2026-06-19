"""Zhipu / z.ai GLM-ASR-2512 — batch transcription.

POST {base}/audio/transcriptions  (multipart)
  headers: Authorization: Bearer <key>
  form:    model=glm-asr-2512, file, stream=false

Hard limits: wav/mp3, <= 25 MB, <= 30 s per request. We advertise
``max_audio_seconds = 28`` so the runner auto-chunks anything longer and stitches
the text back together. No diarization.
"""
from __future__ import annotations

import requests

from .base import ASRProvider, ASRResult, Segment


class ZhipuProvider(ASRProvider):
    key = "zhipu"
    label = "Zhipu GLM-ASR"
    supports_diarization = False
    max_audio_seconds = 28            # API hard limit is 30s; keep margin
    homepage = "https://docs.z.ai/guides/audio/glm-asr-2512"
    notes = "Strong Mandarin/dialect CER. 30s/25MB cap → long audio is chunked."

    def __init__(self, config: dict):
        super().__init__(config)
        self.api_key = config.get("ZHIPU_API_KEY", "")
        self.base_url = config.get("ZHIPU_BASE_URL", "https://api.z.ai/api/paas/v4").rstrip("/")
        self.model = config.get("ZHIPU_MODEL", "glm-asr-2512")

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def transcribe_file(self, wav_path, language=None, diarize=False) -> ASRResult:
        if not self.is_configured():
            return self._fail("ZHIPU_API_KEY not set")
        url = f"{self.base_url}/audio/transcriptions"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        data = {"model": self.model, "stream": "false"}
        try:
            with open(wav_path, "rb") as fh:
                files = {"file": ("audio.wav", fh, "audio/wav")}
                resp = requests.post(url, headers=headers, data=data, files=files, timeout=300)
            if resp.status_code >= 400:
                return self._fail(f"HTTP {resp.status_code}: {resp.text[:300]}")
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            return self._fail(f"{type(exc).__name__}: {exc}")

        text = self._extract_text(payload)
        return self._result(ok=True, text=text, raw=payload)

    @staticmethod
    def _extract_text(payload: dict) -> str:
        if not isinstance(payload, dict):
            return str(payload).strip()
        if payload.get("text"):
            return str(payload["text"]).strip()
        # OpenAI-style fallbacks
        choices = payload.get("choices") or []
        if choices:
            msg = choices[0].get("message") or {}
            if msg.get("content"):
                return str(msg["content"]).strip()
            if choices[0].get("text"):
                return str(choices[0]["text"]).strip()
        seg = payload.get("segments") or payload.get("result")
        if isinstance(seg, list):
            return " ".join(s.get("text", "") for s in seg).strip()
        return ""
