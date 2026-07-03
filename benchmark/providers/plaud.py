"""Plaud ASR (Transcription API) — async submit + poll.

Plaud's Transcription API takes a **URL** to the audio (not a file upload), runs
as an async job, and you poll for the result. We hold a local WAV, so we first
turn it into a URL. Plaud's transcription backend wants a **Plaud-hosted** URL —
external URLs (S3 presign / public) come back as HTTP 500 — so:

  * **Plaud native upload** (default): multipart-upload through Plaud's own File
    Upload API (needs ``PLAUD_SECRET_KEY``) to get a 24 h DownloadUrl, then
    transcribe that. Mirrors the official developer-playground flow.
  * **S3 presign** (opt-in via ``PLAUD_AUDIO_SOURCE=s3``): hand Plaud a presigned
    URL from your S3 — kept for if/when external URLs get accepted.

Auth gotcha: the Transcription API uses ``X-Client-Id`` + ``X-Client-Api-Key``,
where the api-key is generated in the Plaud portal (App Settings -> API Keys) and
is **NOT** the client_secret. The secret_key only mints the upload token.

Diarization returns per-segment ``speaker`` labels (e.g. "Speaker 1").
"""
from __future__ import annotations

import base64
import os
import time
import uuid

import requests

from .base import ASRProvider, ASRResult, Segment
from ._aws import aws_creds_available

_POLL_TIMEOUT_S = 900
_DONE = {"SUCCESS"}
_FAIL = {"FAILURE", "REVOKED", "FAILED", "ERROR"}
_PENDING = {"PENDING", "RECEIVED", "STARTED", "PROGRESS"}


def _ok(r, step):
    """Raise with the response body (not just the status) so 4xx/5xx are diagnosable."""
    if r.status_code >= 400:
        raise RuntimeError(f"{step} HTTP {r.status_code}: {r.text[:400]}")
    return r


class PlaudProvider(ASRProvider):
    key = "plaud"
    label = "Plaud"
    supports_diarization = True
    max_audio_seconds = None          # async job, handles long audio server-side
    homepage = "https://docs.plaud.ai/plaud-embedded/starter-app-guide"
    notes = "Transcription API (plaud-fast-whisper). Async submit+poll; uploads via Plaud (wav auto-transcoded to mp3 — upload API rejects wav)."

    def __init__(self, config: dict):
        super().__init__(config)
        self.client_id = config.get("PLAUD_CLIENT_ID", "")
        self.api_key = config.get("PLAUD_API_KEY", "")
        self.secret_key = config.get("PLAUD_SECRET_KEY", "")
        self.host = config.get("PLAUD_REGION_HOST",
                               "https://platform-us.plaud.ai/developer/api").rstrip("/")
        self.model = config.get("PLAUD_MODEL", "plaud-fast-whisper")
        self.audio_source = (config.get("PLAUD_AUDIO_SOURCE") or "auto").lower()  # auto | s3 | upload
        # S3 (reused to presign a public URL, same as Fun-ASR).
        self.aws_region = config.get("AWS_REGION", "ap-southeast-2")
        self.bucket = config.get("AWS_TRANSCRIBE_BUCKET", "")
        self.prefix = config.get("AWS_TRANSCRIBE_PREFIX", "asr-benchmark/")
        self.access_key = config.get("AWS_ACCESS_KEY_ID", "")
        self.secret = config.get("AWS_SECRET_ACCESS_KEY", "")

    def _can_s3(self) -> bool:
        return bool(self.bucket) and aws_creds_available(self.config)

    def is_configured(self) -> bool:
        # Transcription needs client_id + api_key, plus *some* way to get a URL.
        return bool(self.client_id and self.api_key) and (self._can_s3() or bool(self.secret_key))

    # ----------------------------------------------------------------- run -- #
    def transcribe_file(self, wav_path, language=None, diarize=False) -> ASRResult:
        if not (self.client_id and self.api_key):
            return self._fail("PLAUD_CLIENT_ID / PLAUD_API_KEY not set")

        cleanup = None
        try:
            # Plaud transcription wants a Plaud-hosted URL (external URLs 500), so
            # default to Plaud's own upload when a secret_key is present; S3 is
            # opt-in via PLAUD_AUDIO_SOURCE=s3.
            use_upload = self.audio_source == "upload" or (self.audio_source == "auto" and bool(self.secret_key))
            if use_upload:
                if not self.secret_key:
                    return self._fail("Plaud upload needs PLAUD_SECRET_KEY")
                url = self._plaud_upload(wav_path)
            else:
                if not self._can_s3():
                    return self._fail("no audio URL: set PLAUD_SECRET_KEY (Plaud upload) or AWS creds (S3 presign)")
                url, cleanup = self._presign_s3(wav_path)
        except Exception as exc:  # noqa: BLE001
            return self._fail(f"audio upload failed: {type(exc).__name__}: {exc}")

        try:
            return self._transcribe_url(url, language, diarize)
        finally:
            if cleanup:
                cleanup()

    # ----------------------------------------------------- Flow A: ASR ------ #
    def _headers(self) -> dict:
        return {"X-Client-Id": self.client_id, "X-Client-Api-Key": self.api_key}

    def _transcribe_url(self, url, language, diarize) -> ASRResult:
        body = {
            "file_url": url,
            "params": {
                "transcribe": {"language": language or "auto", "model": self.model},
                # VAD on: decode_silence=False means silent stretches are detected
                # and skipped rather than force-decoded.
                "vad": {"decode_silence": False},
                "diarization": {"enabled": bool(diarize), "return_embedding": False},
            },
        }
        try:
            r = requests.post(f"{self.host}/open/partner/ai/transcriptions/",
                              headers={**self._headers(), "Content-Type": "application/json"},
                              json=body, timeout=60)
            if r.status_code >= 400:
                rid = (r.headers.get("x-request-id") or r.headers.get("X-Request-Id")
                       or r.headers.get("X-Amzn-Trace-Id") or "")
                return self._fail(f"submit HTTP {r.status_code}: {r.text[:400]} "
                                  f"| req-id={rid} | file_url={url.split('?', 1)[0]}")
            tid = r.json().get("transcription_id")
        except Exception as exc:  # noqa: BLE001
            return self._fail(f"submit failed: {type(exc).__name__}: {exc}")
        if not tid:
            return self._fail("no transcription_id returned")

        payload = self._poll(tid)
        if payload is None:
            return self._fail("transcription timed out")
        status = payload.get("status")
        if status in _FAIL:
            return self._fail(f"transcription {status}")
        data = payload.get("data") or {}

        raw_segs = data.get("segments") or data.get("results") or []
        segments = [
            Segment(start=float(s.get("start", 0.0) or 0.0),
                    end=float(s.get("end", 0.0) or 0.0),
                    text=s.get("text", "") or "",
                    speaker=s.get("speaker"))
            for s in raw_segs
        ]
        text = (data.get("text") or " ".join(s.text for s in segments)).strip()
        has_diar = bool(diarize and any(s.speaker for s in segments))
        return self._result(ok=True, text=text, segments=segments,
                            has_diarization=has_diar, raw=payload)

    def _poll(self, tid):
        url = f"{self.host}/open/partner/ai/transcriptions/{tid}"
        deadline = time.time() + _POLL_TIMEOUT_S
        delay = 1.0
        while time.time() < deadline:
            time.sleep(delay)
            delay = min(delay * 2, 10.0)
            try:
                r = requests.get(url, headers=self._headers(), timeout=60)
                if r.status_code >= 400:
                    continue
                payload = r.json()
            except Exception:  # noqa: BLE001
                continue
            if payload.get("status") in _DONE | _FAIL:
                return payload
        return None

    # ------------------------------------------- URL source 1: S3 presign --- #
    def _presign_s3(self, wav_path):
        import boto3
        from botocore.config import Config as BotoConfig

        sess = {"region_name": self.aws_region}
        if self.access_key and self.secret:
            sess["aws_access_key_id"] = self.access_key
            sess["aws_secret_access_key"] = self.secret
        s3 = boto3.session.Session(**sess).client(
            "s3", region_name=self.aws_region,
            endpoint_url=f"https://s3.{self.aws_region}.amazonaws.com",
            config=BotoConfig(signature_version="s3v4", s3={"addressing_style": "virtual"}),
        )
        key = f"{self.prefix}plaud-{uuid.uuid4().hex[:16]}.wav"
        s3.upload_file(wav_path, self.bucket, key)
        url = s3.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=3600)

        def _cleanup():
            try:
                s3.delete_object(Bucket=self.bucket, Key=key)
            except Exception:  # noqa: BLE001
                pass
        return url, _cleanup

    # ------------------------------------- URL source 2: Plaud native upload  #
    def _plaud_upload(self, wav_path) -> str:
        # Plaud's upload API rejects "wav" (FILE_TYPE_INVALID); mp3/m4a/opus pass.
        # Our runner hands every provider the normalized 16k WAV, so transcode it.
        filetype = os.path.splitext(wav_path)[1].lstrip(".").lower() or "wav"
        tmp_mp3 = None
        if filetype not in {"mp3", "m4a", "opus"}:
            from core.audio import transcode_to_mp3
            tmp_mp3 = wav_path + ".plaud.mp3"
            transcode_to_mp3(wav_path, tmp_mp3)
            wav_path, filetype = tmp_mp3, "mp3"
        try:
            return self._plaud_upload_raw(wav_path, filetype)
        finally:
            if tmp_mp3:
                try:
                    os.remove(tmp_mp3)
                except OSError:
                    pass

    def _plaud_upload_raw(self, wav_path, filetype) -> str:
        # B0: partner token (Basic base64(client_id:secret_key), no body — matches playground)
        basic = base64.b64encode(f"{self.client_id}:{self.secret_key}".encode()).decode()
        r = _ok(requests.post(f"{self.host}/oauth/partner/access-token",
                             headers={"Authorization": f"Basic {basic}",
                                      "Content-Type": "application/x-www-form-urlencoded"},
                             timeout=30), "partner-token")
        partner = r.json()["access_token"]

        # B1: user token (Bearer partner token)
        r = _ok(requests.post(f"{self.host}/open/partner/users/access-token",
                             headers={"Authorization": f"Bearer {partner}",
                                      "Content-Type": "application/json"},
                             json={"user_id": "fieldsight-benchmark", "expires_in": 86400},
                             timeout=30), "user-token")
        user = r.json()["access_token"]
        bearer = {"Authorization": f"Bearer {user}", "Content-Type": "application/json"}

        # B2: presigned multipart URLs
        size = os.path.getsize(wav_path)
        r = _ok(requests.post(f"{self.host}/open/partner/files/upload/generate-presigned-urls",
                             headers=bearer, json={"filesize": size, "filetype": filetype},
                             timeout=30), "generate-presigned-urls")
        up = r.json()
        chunk = int(up.get("ChunkSize") or 5 * 1024 * 1024)

        # B3: PUT each chunk straight to S3, capture ETag
        part_list = []
        with open(wav_path, "rb") as f:
            for part in sorted(up["Parts"], key=lambda p: p["PartNumber"]):
                data = f.read(chunk)
                pr = _ok(requests.put(part["PresignedUrl"], data=data, timeout=300), "upload-part")
                part_list.append({"PartNumber": part["PartNumber"], "ETag": pr.headers.get("ETag")})

        # B4: complete upload -> DownloadUrl
        r = _ok(requests.post(f"{self.host}/open/partner/files/upload/complete-upload",
                             headers=bearer,
                             json={"file_id": up["FileId"], "upload_id": up["UploadId"],
                                   "filetype": filetype, "part_list": part_list},
                             timeout=60), "complete-upload")
        return r.json()["DownloadUrl"]
