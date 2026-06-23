"""Alibaba DashScope Fun-ASR — recording file recognition.

This API only accepts a PUBLIC file URL (no local upload / base64), so we push
the WAV to S3 and hand DashScope a presigned GET URL. It handles long audio and
supports speaker diarization (``diarization_enabled`` → ``speaker_id``).

Flow:
    Transcription.async_call(model, file_urls=[url], language_hints=[...],
                             diarization_enabled=bool) -> task_id
    Transcription.wait(task_id) -> results[].transcription_url -> download JSON
"""
from __future__ import annotations

import uuid

import requests

from .base import ASRProvider, ASRResult, Segment
from ._aws import aws_creds_available

_REGION_URL = {
    "intl": "https://dashscope-intl.aliyuncs.com/api/v1",
    "cn": "https://dashscope.aliyuncs.com/api/v1",
}


class FunASRProvider(ASRProvider):
    key = "funasr"
    label = "Ali Fun-ASR"
    supports_diarization = True
    max_audio_seconds = None          # designed for long recordings
    needs_public_url = True
    homepage = "https://www.alibabacloud.com/help/en/model-studio/recording-file-recognition"
    notes = "Fun-ASR (FunAudioLLM). Needs a public URL → uses your S3 to presign."

    def __init__(self, config: dict):
        super().__init__(config)
        self.api_key = config.get("DASHSCOPE_API_KEY", "")
        self.region = (config.get("DASHSCOPE_REGION") or "intl").lower()
        self.model = config.get("FUNASR_MODEL", "fun-asr")
        # S3 reused for presigning the public URL.
        self.aws_region = config.get("AWS_REGION", "ap-southeast-2")
        self.bucket = config.get("AWS_TRANSCRIBE_BUCKET", "")
        self.prefix = config.get("AWS_TRANSCRIBE_PREFIX", "asr-benchmark/")
        self.access_key = config.get("AWS_ACCESS_KEY_ID", "")
        self.secret_key = config.get("AWS_SECRET_ACCESS_KEY", "")

    def is_configured(self) -> bool:
        # DashScope key + an S3 bucket with resolvable AWS creds to presign a URL.
        return bool(self.api_key and self.bucket) and aws_creds_available(self.config)

    def _presign(self, wav_path: str) -> tuple[str, object, str]:
        import boto3
        from botocore.config import Config as BotoConfig

        sess_kwargs = {"region_name": self.aws_region}
        if self.access_key and self.secret_key:
            sess_kwargs["aws_access_key_id"] = self.access_key
            sess_kwargs["aws_secret_access_key"] = self.secret_key
        # Force SigV4 + the regional virtual-hosted endpoint. boto3's default here
        # yields a SigV2 global "s3.amazonaws.com" URL; a remote fetcher (DashScope's
        # server) can't follow it for an ap-southeast-2 bucket and downloads an S3
        # redirect XML instead of audio -> ASR_RESPONSE_HAVE_NO_WORDS.
        s3 = boto3.session.Session(**sess_kwargs).client(
            "s3",
            region_name=self.aws_region,
            endpoint_url=f"https://s3.{self.aws_region}.amazonaws.com",
            config=BotoConfig(signature_version="s3v4", s3={"addressing_style": "virtual"}),
        )
        key = f"{self.prefix}funasr-{uuid.uuid4().hex[:16]}.wav"
        s3.upload_file(wav_path, self.bucket, key)
        url = s3.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=3600
        )
        return url, s3, key

    def transcribe_file(self, wav_path, language=None, diarize=False) -> ASRResult:
        if not self.api_key:
            return self._fail("DASHSCOPE_API_KEY not set")
        if not self.bucket:
            return self._fail("AWS_TRANSCRIBE_BUCKET not set (needed to host a public URL)")
        try:
            import dashscope
            from dashscope.audio.asr import Transcription
        except Exception:  # noqa: BLE001
            return self._fail("dashscope SDK not installed (pip install dashscope)")

        dashscope.base_http_api_url = _REGION_URL.get(self.region, _REGION_URL["intl"])
        dashscope.api_key = self.api_key

        try:
            url, s3, key = self._presign(wav_path)
        except Exception as exc:  # noqa: BLE001
            return self._fail(f"S3 presign failed: {exc}")

        kwargs = {"model": self.model, "file_urls": [url]}
        if diarize:
            kwargs["diarization_enabled"] = True
        # Fun-ASR is zh/en focused; hint both unless the caller fixes a language.
        kwargs["language_hints"] = [language] if language else ["zh", "en"]

        try:
            task = Transcription.async_call(**kwargs)
            resp = Transcription.wait(task=task.output.task_id)
        except Exception as exc:  # noqa: BLE001
            self._delete(s3, key)
            return self._fail(f"{type(exc).__name__}: {exc}")
        finally:
            pass

        payload = self._download_result(resp)
        self._delete(s3, key)
        if payload is None:
            status = getattr(resp, "output", None)
            return self._fail(f"no transcription result (status={status})")

        text, segments = self._parse(payload)
        return self._result(
            ok=True, text=text, segments=segments,
            has_diarization=bool(diarize and any(s.speaker for s in segments)),
            raw=payload,
        )

    def _delete(self, s3, key):
        try:
            s3.delete_object(Bucket=self.bucket, Key=key)
        except Exception:
            pass

    @staticmethod
    def _download_result(resp):
        try:
            results = resp.output["results"]
            for r in results:
                if r.get("transcription_url"):
                    return requests.get(r["transcription_url"], timeout=120).json()
        except Exception:
            return None
        return None

    @staticmethod
    def _parse(payload: dict):
        text_parts, segments = [], []
        for tr in payload.get("transcripts", []):
            if tr.get("text"):
                text_parts.append(tr["text"])
            for sent in tr.get("sentences", []):
                spk = sent.get("speaker_id")
                segments.append(
                    Segment(
                        start=float(sent.get("begin_time", 0)) / 1000.0,
                        end=float(sent.get("end_time", 0)) / 1000.0,
                        text=sent.get("text", ""),
                        speaker=(f"spk_{spk}" if spk is not None else None),
                    )
                )
        full = " ".join(text_parts).strip()
        if not full and segments:
            full = " ".join(s.text for s in segments).strip()
        return full, segments
