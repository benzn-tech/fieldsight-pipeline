"""AWS Transcribe — the incumbent FieldSight uses today (the thing we may replace).

Batch flow: upload WAV to S3 -> StartTranscriptionJob -> poll -> download the
result JSON from the presigned TranscriptFileUri. Supports speaker diarization
via Settings.ShowSpeakerLabels. Handles long audio natively (no chunking).

Requires AWS creds + an S3 bucket the creds can read/write (defaults to the
FieldSight data bucket).
"""
from __future__ import annotations

import time
import uuid

import requests

from .base import ASRProvider, ASRResult, Segment
from ._aws import aws_creds_available

# Language identification pool for NZ construction (English + Mandarin workers).
_LANG_OPTIONS = ["en-NZ", "en-AU", "en-US", "zh-CN"]
_LANG_MAP = {"en": "en-NZ", "zh": "zh-CN", "zh-cn": "zh-CN", "en-nz": "en-NZ"}


class AWSTranscribeProvider(ASRProvider):
    key = "aws"
    label = "AWS Transcribe"
    supports_diarization = True
    max_audio_seconds = None          # handles up to ~4h
    homepage = "https://docs.aws.amazon.com/transcribe/"
    notes = "Incumbent. Async batch via S3 → latency includes job queue time."

    def __init__(self, config: dict):
        super().__init__(config)
        self.region = config.get("AWS_REGION", "ap-southeast-2")
        self.bucket = config.get("AWS_TRANSCRIBE_BUCKET", "")
        self.prefix = config.get("AWS_TRANSCRIBE_PREFIX", "asr-benchmark/")
        self.access_key = config.get("AWS_ACCESS_KEY_ID", "")
        self.secret_key = config.get("AWS_SECRET_ACCESS_KEY", "")
        self.model = "transcribe-batch"

    def is_configured(self) -> bool:
        # Needs a bucket AND resolvable credentials (explicit, env, profile, or role).
        return bool(self.bucket) and aws_creds_available(self.config)

    def _clients(self):
        import boto3
        kwargs = {"region_name": self.region}
        if self.access_key and self.secret_key:
            kwargs["aws_access_key_id"] = self.access_key
            kwargs["aws_secret_access_key"] = self.secret_key
        session = boto3.session.Session(**kwargs)
        return session.client("s3"), session.client("transcribe")

    def transcribe_file(self, wav_path, language=None, diarize=False) -> ASRResult:
        if not self.bucket:
            return self._fail("AWS_TRANSCRIBE_BUCKET not set")
        try:
            s3, transcribe = self._clients()
        except Exception as exc:  # noqa: BLE001
            return self._fail(f"boto3 init failed: {exc}")

        job = "asrbench-" + uuid.uuid4().hex[:16]
        key = f"{self.prefix}{job}.wav"
        try:
            s3.upload_file(wav_path, self.bucket, key)
        except Exception as exc:  # noqa: BLE001
            return self._fail(f"S3 upload failed: {exc}")

        settings = {}
        if diarize:
            settings = {"ShowSpeakerLabels": True, "MaxSpeakerLabels": 10}
        params = {
            "TranscriptionJobName": job,
            "Media": {"MediaFileUri": f"s3://{self.bucket}/{key}"},
            "MediaFormat": "wav",
        }
        if settings:
            params["Settings"] = settings
        lang = _LANG_MAP.get((language or "").lower()) if language else None
        if lang:
            params["LanguageCode"] = lang
        else:
            params["IdentifyLanguage"] = True
            params["LanguageOptions"] = _LANG_OPTIONS

        try:
            transcribe.start_transcription_job(**params)
            payload = self._poll(transcribe, job)
        except Exception as exc:  # noqa: BLE001
            self._cleanup(s3, key, transcribe, job)
            return self._fail(f"{type(exc).__name__}: {exc}")

        self._cleanup(s3, key, transcribe, job)
        if payload is None:
            return self._fail("transcription job failed or timed out")

        text, segments = self._parse(payload, diarize)
        return self._result(
            ok=True, text=text, segments=segments,
            has_diarization=bool(diarize and segments and any(s.speaker for s in segments)),
            raw=payload,
        )

    def _poll(self, transcribe, job, timeout_s=900, interval=4):
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            resp = transcribe.get_transcription_job(TranscriptionJobName=job)
            status = resp["TranscriptionJob"]["TranscriptionJobStatus"]
            if status == "COMPLETED":
                uri = resp["TranscriptionJob"]["Transcript"]["TranscriptFileUri"]
                return requests.get(uri, timeout=120).json()
            if status == "FAILED":
                return None
            time.sleep(interval)
        return None

    def _cleanup(self, s3, key, transcribe, job):
        for fn in (
            lambda: s3.delete_object(Bucket=self.bucket, Key=key),
            lambda: transcribe.delete_transcription_job(TranscriptionJobName=job),
        ):
            try:
                fn()
            except Exception:
                pass

    @staticmethod
    def _parse(payload: dict, diarize: bool):
        results = payload.get("results", {})
        transcripts = results.get("transcripts", [])
        full_text = transcripts[0]["transcript"].strip() if transcripts else ""

        if not diarize or "speaker_labels" not in results:
            return full_text, []

        # Map each pronunciation item's start_time -> speaker label.
        spk_by_start = {}
        for seg in results["speaker_labels"].get("segments", []):
            for it in seg.get("items", []):
                spk_by_start[it.get("start_time")] = it.get("speaker_label")

        segments: list[Segment] = []
        cur = None
        for item in results.get("items", []):
            if item.get("type") != "pronunciation":
                # attach punctuation to the current word
                if cur and item.get("alternatives"):
                    cur.text += item["alternatives"][0].get("content", "")
                continue
            start = item.get("start_time")
            spk = spk_by_start.get(start)
            word = item["alternatives"][0]["content"] if item.get("alternatives") else ""
            if cur is None or cur.speaker != spk:
                cur = Segment(start=float(start or 0), end=float(item.get("end_time") or 0),
                              text=word, speaker=spk)
                segments.append(cur)
            else:
                cur.text += " " + word
                cur.end = float(item.get("end_time") or cur.end)
        return full_text, segments
