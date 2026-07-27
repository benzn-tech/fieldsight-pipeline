"""ByteDance Volcengine Doubao Seed-ASR — 大模型录音文件识别 (async submit+query).

The bigmodel file-recognition API takes an audio **URL**, so we reuse the proven
S3 presign (SigV4 + regional endpoint, same as Fun-ASR). Flow:

    POST /api/v3/auc/bigmodel/submit  (task)   -> X-Api-Status-Code: 20000000
    POST /api/v3/auc/bigmodel/query   (poll)   -> 20000001/20000002 = working,
                                                  20000000 = done, 20000003 = silent

Auth supports both Volcengine credential styles: the newer single API key
(``x-api-key``) or the classic speech-app pair (``X-Api-App-Key`` = APP ID,
``X-Api-Access-Key`` = Access Token). Resource id picks the model/tier:
``volc.seedasr.auc`` (Seed-ASR 2.0 standard, default) or ``volc.bigasr.auc``
(1.0). Language is auto-detected (Mandarin/dialects + 13+ languages).
Diarization via ``enable_speaker_info`` -> per-utterance ``speaker``.
"""
from __future__ import annotations

import time
import uuid

import requests

from .base import ASRProvider, ASRResult, Segment
from ._aws import aws_creds_available

_SUBMIT = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit"
_QUERY = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/query"
_OK = "20000000"
_WORKING = {"20000001", "20000002"}
_SILENT = "20000003"
_POLL_TIMEOUT_S = 900


class DoubaoProvider(ASRProvider):
    key = "doubao"
    label = "Doubao Seed-ASR"
    supports_diarization = True
    max_audio_seconds = None          # server-side, files up to ~5 h
    homepage = "https://www.volcengine.com/docs/6561/1354868"
    notes = "Seed-ASR 2.0 (volc.seedasr.auc). Async submit+query on an audio URL (S3 presign)."

    def __init__(self, config: dict):
        super().__init__(config)
        self.api_key = config.get("DOUBAO_API_KEY", "")
        self.app_id = config.get("DOUBAO_APP_ID", "")
        self.token = config.get("DOUBAO_ACCESS_TOKEN", "")
        self.resource_id = config.get("DOUBAO_RESOURCE_ID", "volc.seedasr.auc")
        self.model = self.resource_id
        # S3 reused to host the audio URL (same creds as AWS/Fun-ASR/Plaud).
        self.aws_region = config.get("AWS_REGION", "ap-southeast-2")
        self.bucket = config.get("AWS_TRANSCRIBE_BUCKET", "")
        self.prefix = config.get("AWS_TRANSCRIBE_PREFIX", "asr-benchmark/")
        self.access_key = config.get("AWS_ACCESS_KEY_ID", "")
        self.secret_key = config.get("AWS_SECRET_ACCESS_KEY", "")

    def _has_auth(self) -> bool:
        return bool(self.api_key or (self.app_id and self.token))

    def is_configured(self) -> bool:
        return self._has_auth() and bool(self.bucket) and aws_creds_available(self.config)

    def _headers(self, request_id: str) -> dict:
        h = {
            "Content-Type": "application/json",
            "X-Api-Resource-Id": self.resource_id,
            "X-Api-Request-Id": request_id,
        }
        if self.api_key:
            h["x-api-key"] = self.api_key
        else:
            h["X-Api-App-Key"] = self.app_id
            h["X-Api-Access-Key"] = self.token
        return h

    # ------------------------------------------------------------------ run - #
    def transcribe_file(self, wav_path, language=None, diarize=False) -> ASRResult:
        if not self._has_auth():
            return self._fail("set DOUBAO_API_KEY or DOUBAO_APP_ID + DOUBAO_ACCESS_TOKEN")
        if not (self.bucket and aws_creds_available(self.config)):
            return self._fail("needs AWS creds + bucket to host the audio URL (S3 presign)")

        try:
            url, s3, key = self._presign(wav_path)
        except Exception as exc:  # noqa: BLE001
            return self._fail(f"S3 presign failed: {exc}")

        try:
            return self._transcribe_url(url, diarize)
        finally:
            try:
                s3.delete_object(Bucket=self.bucket, Key=key)
            except Exception:  # noqa: BLE001
                pass

    def _transcribe_url(self, url: str, diarize: bool) -> ASRResult:
        request_id = str(uuid.uuid4())
        body = {
            "user": {"uid": "fieldsight-benchmark"},
            "audio": {"url": url, "format": "wav"},
            "request": {
                "model_name": "bigmodel",
                "enable_itn": True,
                "enable_punc": True,
                "enable_ddc": True,
                "show_utterances": True,
                "enable_speaker_info": bool(diarize),
            },
        }
        headers = {**self._headers(request_id), "X-Api-Sequence": "-1"}
        try:
            r = requests.post(_SUBMIT, headers=headers, json=body, timeout=60)
        except Exception as exc:  # noqa: BLE001
            return self._fail(f"submit failed: {type(exc).__name__}: {exc}")
        code = r.headers.get("X-Api-Status-Code", "")
        if code != _OK:
            msg = r.headers.get("X-Api-Message", "") or r.text[:300]
            return self._fail(f"submit status {code or r.status_code}: {msg}")

        payload, code = self._poll(request_id)
        if payload is None:
            return self._fail(f"query failed or timed out (last status {code})")
        if code == _SILENT:
            return self._result(ok=True, text="", segments=[], raw={"status": "silent"})

        result = payload.get("result") or {}
        segments = [
            Segment(
                start=float(u.get("start_time", 0) or 0) / 1000.0,
                end=float(u.get("end_time", 0) or 0) / 1000.0,
                text=u.get("text", "") or "",
                speaker=_speaker_of(u),
            )
            for u in (result.get("utterances") or [])
        ]
        text = (result.get("text") or " ".join(s.text for s in segments)).strip()
        has_diar = bool(diarize and any(s.speaker for s in segments))
        return self._result(ok=True, text=text, segments=segments,
                            has_diarization=has_diar, raw=payload)

    def _poll(self, request_id: str):
        deadline = time.time() + _POLL_TIMEOUT_S
        delay, code = 2.0, ""
        while time.time() < deadline:
            time.sleep(delay)
            delay = min(delay * 1.5, 10.0)
            try:
                r = requests.post(_QUERY, headers=self._headers(request_id), json={}, timeout=60)
            except Exception:  # noqa: BLE001
                continue
            code = r.headers.get("X-Api-Status-Code", "")
            if code == _OK:
                try:
                    return r.json(), code
                except Exception:  # noqa: BLE001
                    return None, code
            if code == _SILENT:
                return {}, code
            if code not in _WORKING:
                return None, f"{code}: {r.headers.get('X-Api-Message', '') or r.text[:200]}"
        return None, code or "timeout"

    def _presign(self, wav_path: str):
        import boto3
        from botocore.config import Config as BotoConfig

        sess = {"region_name": self.aws_region}
        if self.access_key and self.secret_key:
            sess["aws_access_key_id"] = self.access_key
            sess["aws_secret_access_key"] = self.secret_key
        s3 = boto3.session.Session(**sess).client(
            "s3", region_name=self.aws_region,
            endpoint_url=f"https://s3.{self.aws_region}.amazonaws.com",
            config=BotoConfig(signature_version="s3v4", s3={"addressing_style": "virtual"}),
        )
        key = f"{self.prefix}doubao-{uuid.uuid4().hex[:16]}.wav"
        s3.upload_file(wav_path, self.bucket, key)
        url = s3.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=3600)
        return url, s3, key


def _speaker_of(u: dict):
    v = u.get("speaker")
    if v is None:
        v = (u.get("additions") or {}).get("speaker")
    if v is None or v == "":
        return None
    v = str(v)
    return f"Speaker {v}" if v.isdigit() else v
