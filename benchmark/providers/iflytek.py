"""iFlytek 科大讯飞 语音转写 (LFASR standard v2).

Host: https://raasr.xfyun.cn/v2/api  with endpoints /upload and /getResult.

Auth signature:
    ts       = str(int(time.time()))
    md5      = MD5(appId + ts)                      # 32 hex chars
    signa    = Base64( HMAC_SHA1(secretKey, md5) )

Flow: POST /upload (raw audio bytes as body, metadata in query) -> orderId,
then poll POST /getResult until orderInfo.status == 4 and parse orderResult.
Speaker separation via roleType=2. Handles long audio natively.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import wave

import requests

from .base import ASRProvider, ASRResult, Segment

HOST = "https://raasr.xfyun.cn/v2/api"


class IFlytekProvider(ASRProvider):
    key = "iflytek"
    label = "iFlytek LFASR"
    supports_diarization = True
    max_audio_seconds = None          # long-form transcription service
    homepage = "https://www.xfyun.cn/doc/asr/ifasr_new/API.html"
    notes = "Long-form 转写. Role separation via roleType=2. Async upload+poll."

    def __init__(self, config: dict):
        super().__init__(config)
        self.appid = config.get("XFYUN_APPID", "")
        self.secret = config.get("XFYUN_SECRET_KEY", "")
        self.model = "lfasr-v2"

    def is_configured(self) -> bool:
        return bool(self.appid and self.secret)

    def _signa(self) -> tuple[str, str]:
        ts = str(int(time.time()))
        md5 = hashlib.md5((self.appid + ts).encode("utf-8")).hexdigest()
        signa = base64.b64encode(
            hmac.new(self.secret.encode("utf-8"), md5.encode("utf-8"), hashlib.sha1).digest()
        ).decode("utf-8")
        return ts, signa

    def transcribe_file(self, wav_path, language=None, diarize=False) -> ASRResult:
        if not self.is_configured():
            return self._fail("XFYUN_APPID / XFYUN_SECRET_KEY not set")
        try:
            order_id = self._upload(wav_path, diarize)
            payload = self._poll(order_id)
        except Exception as exc:  # noqa: BLE001
            return self._fail(f"{type(exc).__name__}: {exc}")
        if payload is None:
            return self._fail("transcription failed or timed out")

        text, segments = self._parse(payload, diarize)
        return self._result(
            ok=True, text=text, segments=segments,
            has_diarization=bool(diarize and any(s.speaker for s in segments)),
            raw=payload,
        )

    def _upload(self, wav_path: str, diarize: bool) -> str:
        ts, signa = self._signa()
        size = os.path.getsize(wav_path)
        with wave.open(wav_path, "rb") as w:
            duration_ms = int(1000 * w.getnframes() / float(w.getframerate() or 16000))
        params = {
            "appId": self.appid, "signa": signa, "ts": ts,
            "fileName": os.path.basename(wav_path), "fileSize": size,
            "duration": str(duration_ms), "language": "cn",
        }
        if diarize:
            params["roleType"] = 2     # enable speaker separation
        with open(wav_path, "rb") as fh:
            data = fh.read()
        resp = requests.post(f"{HOST}/upload", params=params, data=data, timeout=300).json()
        if str(resp.get("code")) != "000000":
            raise RuntimeError(f"upload error {resp.get('code')}: {resp.get('descInfo')}")
        return resp["content"]["orderId"]

    def _poll(self, order_id: str, timeout_s=900, interval=5):
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            ts, signa = self._signa()
            params = {"appId": self.appid, "signa": signa, "ts": ts, "orderId": order_id}
            resp = requests.post(f"{HOST}/getResult", params=params, timeout=120).json()
            if str(resp.get("code")) != "000000":
                raise RuntimeError(f"getResult error {resp.get('code')}: {resp.get('descInfo')}")
            info = resp["content"]["orderInfo"]
            status = info.get("status")
            if status == 4:            # done
                return resp["content"]
            if status == -1:           # failed
                raise RuntimeError(f"order failed: {info.get('failType')}")
            time.sleep(interval)
        return None

    @staticmethod
    def _parse(content: dict, diarize: bool):
        order_result = content.get("orderResult")
        if isinstance(order_result, str):
            try:
                order_result = json.loads(order_result)
            except Exception:
                return order_result.strip() if order_result else "", []
        order_result = order_result or {}

        # Prefer lattice2 (object form); fall back to lattice (stringified json_1best).
        items = order_result.get("lattice2") or order_result.get("lattice") or []
        segments: list[Segment] = []
        for item in items:
            best = item.get("json_1best")
            if isinstance(best, str):
                try:
                    best = json.loads(best)
                except Exception:
                    continue
            st = (best or {}).get("st", {})
            words = []
            for rt in st.get("rt", []):
                for ws in rt.get("ws", []):
                    for cw in ws.get("cw", []):
                        if cw.get("w"):
                            words.append(cw["w"])
            text = "".join(words).strip()
            if not text:
                continue
            spk = st.get("rl")
            segments.append(
                Segment(
                    start=float(st.get("bg", 0)) / 1000.0,
                    end=float(st.get("ed", 0)) / 1000.0,
                    text=text,
                    speaker=(f"spk_{spk}" if diarize and spk not in (None, "", "0") else None),
                )
            )
        full = "".join(s.text for s in segments).strip()
        return full, segments
