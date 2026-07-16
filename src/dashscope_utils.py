"""
dashscope_utils.py — DashScope (Alibaba Cloud) text-embedding client
(Phase 4d, Task 2).

Bedrock is account-level blocked, so Phase 4d's report-chunk embeddings come
from Alibaba Cloud's international DashScope endpoint instead (schema
unchanged: still a 1024-float vector per chunk). Copies the urllib3 HTTP call
pattern from claude_utils.py / lambda_report_generator.py (call_claude /
call_claude_structured :410-441) -- same non-VPC public-internet style, just
a different provider and request/response shape.

embed() batches the input list in groups of <= 10 (DashScope's per-request
cap for text-embedding-v4) and retries transient failures (HTTP 429 rate
limit, 500/503 server errors) with exponential backoff, up to 4 attempts per
batch. Any other HTTP status, or a batch that is still failing after 4
attempts, raises RuntimeError -- there is no "return a zero vector" fallback,
since a silently-wrong embedding is worse than a loud failure here (the
caller, lambda_embed_report, writes nothing to the sidecar if this raises).

Environment Variables:
    DASHSCOPE_API_KEY    - DashScope API key (required -- embed()/stt()/tts() raise if unset)
    DASHSCOPE_BASE_URL   - API base (default: DashScope intl compatible-mode v1)
    DASHSCOPE_EMBED_MODEL - embedding model id (default: text-embedding-v4)
    DASHSCOPE_EMBED_DIM  - embedding dimensionality (default: 1024)
    DASHSCOPE_AIGC_URL   - native multimodal-generation endpoint used by stt() (default: intl)
    DASHSCOPE_ASR_MODEL / DASHSCOPE_ASR_LANG - stt() model id / language (default: qwen3-asr-flash / en)
    DASHSCOPE_TTS_MODEL / DASHSCOPE_TTS_VOICE - tts() Qwen-TTS-Realtime model id / voice
        (default: qwen3-tts-flash-realtime / Cherry -- model retires ~2025-10-10, temporary)
    DASHSCOPE_TTS_WS_URL - Qwen-TTS-Realtime WebSocket base (default: intl realtime endpoint)
    DASHSCOPE_TTS_TIMEOUT_SECONDS - tts() max wait for session.finished (default: 20)
"""
import base64
import json
import logging
import os
import struct
import threading
import time

import urllib3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
DASHSCOPE_BASE_URL = os.environ.get(
    "DASHSCOPE_BASE_URL", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
)
DASHSCOPE_EMBED_MODEL = os.environ.get("DASHSCOPE_EMBED_MODEL", "text-embedding-v4")
DASHSCOPE_EMBED_DIM = int(os.environ.get("DASHSCOPE_EMBED_DIM", "1024"))

# --- SP-Ask: STT (Qwen ASR, HTTP) + TTS (Qwen-TTS-Realtime, WebSocket) ------
# Native (NOT compatible-mode) DashScope multimodal endpoint: audio in/out
# models are exposed here, unlike embeddings which use /compatible-mode/v1.
DASHSCOPE_AIGC_URL = os.environ.get(
    "DASHSCOPE_AIGC_URL",
    "https://dashscope-intl.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation",
)
DASHSCOPE_ASR_MODEL = os.environ.get("DASHSCOPE_ASR_MODEL", "qwen3-asr-flash")
DASHSCOPE_ASR_LANG = os.environ.get("DASHSCOPE_ASR_LANG", "en")

# TTS moved off the multimodal-generation HTTP endpoint (DashScope rejected
# model "qwen-tts" there with HTTP 400 InvalidParameter: Model not exist) to
# the Qwen-TTS-Realtime SDK, which streams synthesized audio over a
# WebSocket. qwen3-tts-flash-realtime / Cherry per the vendor's official SDK
# example. NOTE: this model is flagged by the vendor to retire ~2025-10-10 --
# temporary, revisit before then.
DASHSCOPE_TTS_MODEL = os.environ.get("DASHSCOPE_TTS_MODEL", "qwen3-tts-flash-realtime")
DASHSCOPE_TTS_VOICE = os.environ.get("DASHSCOPE_TTS_VOICE", "Cherry")
# Realtime WS base -- MUST match the API key's region, same as
# DASHSCOPE_AIGC_URL/DASHSCOPE_BASE_URL above (both dashscope-intl). Passed
# directly to QwenTtsRealtime(... url=...) in tts(). VERIFY AT DEPLOY against
# live DashScope -- taken from the vendor's international-region doc, not
# independently confirmed against this account's key.
DASHSCOPE_TTS_WS_URL = os.environ.get(
    "DASHSCOPE_TTS_WS_URL", "wss://dashscope-intl.aliyuncs.com/api-ws/v1/realtime"
)
DASHSCOPE_TTS_TIMEOUT_SECONDS = float(os.environ.get("DASHSCOPE_TTS_TIMEOUT_SECONDS", "20"))

BATCH_SIZE = 10
MAX_ATTEMPTS = 4
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}  # 502/504 common on cross-border gateway (Fable M2)
BACKOFF_BASE_SECONDS = 1.0


def _batches(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _embed_batch(http, batch, dim):
    body = json.dumps({
        "model": DASHSCOPE_EMBED_MODEL,
        "input": batch,
        "dimensions": dim,
        "encoding_format": "float",
    })
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {DASHSCOPE_API_KEY}",
    }

    last_error = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            resp = http.request(
                "POST", f"{DASHSCOPE_BASE_URL}/embeddings",
                body=body, headers=headers, timeout=60.0,
            )
        except Exception as e:
            last_error = str(e)
            logger.warning("DashScope embed request failed (attempt %d): %s", attempt + 1, last_error)
            if attempt < MAX_ATTEMPTS - 1:
                time.sleep(BACKOFF_BASE_SECONDS * (2 ** attempt))
                continue
            raise RuntimeError(
                f"DashScope embed request failed after {MAX_ATTEMPTS} attempts: {last_error}"
            )

        if resp.status == 200:
            data = json.loads(resp.data.decode("utf-8"))
            ranked = sorted(data["data"], key=lambda d: d["index"])
            # Length guard (Fable review M1): a 200 that returns fewer vectors
            # than inputs would misalign the caller's hash<->vector zip and
            # insert WRONG vectors with no error — silent RAG corruption.
            if len(ranked) != len(batch):
                raise RuntimeError(
                    f"DashScope returned {len(ranked)} embeddings for {len(batch)} inputs"
                )
            return [d["embedding"] for d in ranked]

        if resp.status in RETRYABLE_STATUSES and attempt < MAX_ATTEMPTS - 1:
            logger.warning(
                "DashScope embed HTTP %d, retrying (attempt %d/%d)",
                resp.status, attempt + 1, MAX_ATTEMPTS,
            )
            time.sleep(BACKOFF_BASE_SECONDS * (2 ** attempt))
            continue

        body_preview = resp.data.decode("utf-8", "replace")[:500]
        raise RuntimeError(f"DashScope embed API error: HTTP {resp.status}: {body_preview}")

    raise RuntimeError(f"DashScope embed request failed after {MAX_ATTEMPTS} attempts: {last_error}")


def embed(texts, dim=None):
    """Embed a list of texts via DashScope text-embedding-v4, batching in
    groups of <= 10 and returning vectors in the SAME order as `texts`."""
    if not DASHSCOPE_API_KEY:
        raise RuntimeError("DASHSCOPE_API_KEY not set")
    if not texts:
        return []

    dim = dim or DASHSCOPE_EMBED_DIM
    http = urllib3.PoolManager()

    vectors = []
    for batch in _batches(texts, BATCH_SIZE):
        vectors.extend(_embed_batch(http, batch, dim))
    return vectors


def _aigc_request(body):
    """POST a JSON body to the DashScope native multimodal-generation endpoint,
    with the SAME retry posture as _embed_batch (transient statuses + request
    exceptions backed off up to MAX_ATTEMPTS). Returns the parsed 200 JSON;
    raises RuntimeError on a permanent status or exhausted retries."""
    http = urllib3.PoolManager()
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {DASHSCOPE_API_KEY}",
    }
    last_error = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            resp = http.request("POST", DASHSCOPE_AIGC_URL, body=body,
                                headers=headers, timeout=60.0)
        except Exception as e:
            last_error = str(e)
            logger.warning("DashScope aigc request failed (attempt %d): %s", attempt + 1, last_error)
            if attempt < MAX_ATTEMPTS - 1:
                time.sleep(BACKOFF_BASE_SECONDS * (2 ** attempt))
                continue
            raise RuntimeError(
                f"DashScope aigc request failed after {MAX_ATTEMPTS} attempts: {last_error}")
        if resp.status == 200:
            return json.loads(resp.data.decode("utf-8"))
        if resp.status in RETRYABLE_STATUSES and attempt < MAX_ATTEMPTS - 1:
            logger.warning("DashScope aigc HTTP %d, retrying (attempt %d/%d)",
                           resp.status, attempt + 1, MAX_ATTEMPTS)
            time.sleep(BACKOFF_BASE_SECONDS * (2 ** attempt))
            continue
        body_preview = resp.data.decode("utf-8", "replace")[:500]
        raise RuntimeError(f"DashScope aigc API error: HTTP {resp.status}: {body_preview}")
    raise RuntimeError(f"DashScope aigc request failed after {MAX_ATTEMPTS} attempts: {last_error}")


def _asr_response_preview(data):
    """Best-effort short text preview of an ASR response body, for the
    structure-missing warning below (never raises)."""
    try:
        return json.dumps(data)[:500]
    except (TypeError, ValueError):
        return str(data)[:500]


def _extract_asr_text(data):
    """Pull the transcript out of a multimodal-generation ASR response.
    Expected: output.choices[0].message.content is a list of parts, each maybe
    {"text": ...}; tolerant of a plain-string content too. "" if not present.

    Fail-soft by design (never raises -- a genuinely silent clip also yields
    "" and must not fail the request). But the DashScope response shape here
    is an UNVERIFIED guess (see stt()'s docstring); if the real shape differs,
    every call would silently return "" and be indistinguishable from "user
    said nothing". So when the structure itself doesn't match (not just an
    empty transcript within an otherwise-well-formed structure), log a
    WARNING -- this is the only CloudWatch signal that tells "broken
    integration" apart from "silent clip" during device validation. The
    return value is still "" either way; callers' fail-soft behavior is
    unchanged."""
    try:
        content = data["output"]["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        logger.warning(
            "DashScope ASR response missing expected structure: %s",
            _asr_response_preview(data),
        )
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict)).strip()
    logger.warning(
        "DashScope ASR response missing expected structure: %s",
        _asr_response_preview(data),
    )
    return ""


def stt(audio_bytes, fmt="m4a"):
    """Single-shot DashScope ASR: transcribe a COMPLETE short clip to text.
    Mirrors embed()'s urllib3 + DASHSCOPE_API_KEY + retry pattern. Returns the
    transcript ("" when the model heard nothing). Raises RuntimeError on a
    missing key / permanent HTTP error / exhausted retries.

    spec §11: this is the single-shot multimodal call, NOT the Qwen-ASR-Realtime
    websocket — correct for a finished recording. Verify the exact model id /
    request nesting / response path against live DashScope in Task 2 Step 5."""
    if not DASHSCOPE_API_KEY:
        raise RuntimeError("DASHSCOPE_API_KEY not set")
    if not audio_bytes:
        return ""
    b64 = base64.b64encode(audio_bytes).decode("ascii")
    body = json.dumps({
        "model": DASHSCOPE_ASR_MODEL,
        "input": {"messages": [{"role": "user", "content": [
            {"audio": f"data:audio/{fmt};base64,{b64}"},
        ]}]},
        "parameters": {"asr_options": {"language": DASHSCOPE_ASR_LANG, "enable_lid": False}},
    })
    return _extract_asr_text(_aigc_request(body))


# Lazy-loaded handles for the DashScope realtime SDK. Stay None until the
# first tts() call does the real import (see "LAZY import" note in tts()'s
# docstring -- the prod minimal zip does NOT contain the `dashscope` package,
# only the DashScopeLayer-equipped AskAgentFunction does). Tests monkeypatch
# these three names directly on this module instead of installing the SDK.
QwenTtsRealtime = None
QwenTtsRealtimeCallback = None
AudioFormat = None


def _pcm_to_wav(pcm, sample_rate=24000, channels=1, bits=16):
    """Wrap raw signed-16-bit-LE PCM in a minimal 44-byte RIFF/WAVE header.
    Pure function (no I/O) so it's directly unit-testable. Needed because
    Qwen-TTS-Realtime streams bare PCM, and Android's MediaPlayer (the
    consumer of tts()'s return value) can't play headerless PCM -- it needs
    a container it recognizes. WAV is the simplest one; the SP-Ask API
    contract's audioFormat stays "wav" unchanged."""
    byte_rate = sample_rate * channels * bits // 8
    block_align = channels * bits // 8
    data_size = len(pcm)
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + data_size, b"WAVE",
        b"fmt ", 16, 1, channels, sample_rate, byte_rate, block_align, bits,
        b"data", data_size,
    )
    return header + pcm


def tts(text):
    """DashScope TTS via the Qwen-TTS-Realtime SDK (WebSocket streaming),
    replacing the old multimodal-generation HTTP call -- DashScope rejected
    model "qwen-tts" there with HTTP 400 InvalidParameter: Model not exist.
    Synthesizes `text` and returns WAV bytes (b"" for empty text). Raises
    RuntimeError on a missing key, any SDK/connection failure, a timeout
    waiting for completion, or a session that finishes with no audio.

    model=qwen3-tts-flash-realtime, voice=Cherry, format=PCM 24kHz mono
    16-bit -- per the vendor's official SDK example. That model is flagged
    by the vendor to retire ~2025-10-10; temporary, revisit before then.

    The `dashscope` package is imported lazily (inside this function, first
    call only -- see the module-level QwenTtsRealtime/... globals above) so
    that importing this module never requires the SDK to be installed. The
    prod minimal zip (deploy-lambda-code.sh) does NOT bundle it; only
    AskAgentFunction, which carries DashScopeLayer, does."""
    if not DASHSCOPE_API_KEY:
        raise RuntimeError("DASHSCOPE_API_KEY not set")
    if not text or not text.strip():
        return b""

    global QwenTtsRealtime, QwenTtsRealtimeCallback, AudioFormat
    if QwenTtsRealtime is None:
        import dashscope as _dashscope
        from dashscope.audio.qwen_tts_realtime import (
            QwenTtsRealtime as _QwenTtsRealtime,
            QwenTtsRealtimeCallback as _QwenTtsRealtimeCallback,
            AudioFormat as _AudioFormat,
        )
        _dashscope.api_key = DASHSCOPE_API_KEY
        QwenTtsRealtime = _QwenTtsRealtime
        QwenTtsRealtimeCallback = _QwenTtsRealtimeCallback
        AudioFormat = _AudioFormat

    class _TtsCallback(QwenTtsRealtimeCallback):
        def __init__(self):
            self.buf = bytearray()
            self.finished = threading.Event()
            self.error = None

        def on_open(self):
            pass

        def on_close(self, close_status_code, close_msg=None):
            # A close before session.finished (e.g. auth failure, server
            # error) is the only failure signal for some error modes --
            # surface it instead of letting wait_for_finished time out blind.
            if not self.finished.is_set() and close_status_code not in (1000, None):
                self.error = f"DashScope TTS WS closed abnormally: {close_status_code} {close_msg}"
                self.finished.set()

        def on_event(self, response):
            event_type = response.get("type")
            if event_type == "response.audio.delta":
                self.buf += base64.b64decode(response["delta"])
            elif event_type == "session.finished":
                self.finished.set()
            elif event_type in ("response.error", "error"):
                self.error = f"DashScope TTS error event: {response}"
                self.finished.set()

        def wait_for_finished(self, timeout):
            if not self.finished.wait(timeout):
                raise RuntimeError(
                    f"DashScope TTS timed out after {timeout}s waiting for session.finished"
                )

    cb = _TtsCallback()
    # url=DASHSCOPE_TTS_WS_URL: must match the API key's region, same as
    # DASHSCOPE_AIGC_URL/DASHSCOPE_BASE_URL above (both dashscope-intl).
    # VERIFY AT DEPLOY against live DashScope.
    client = QwenTtsRealtime(model=DASHSCOPE_TTS_MODEL, callback=cb, url=DASHSCOPE_TTS_WS_URL)
    try:
        client.connect()
        client.update_session(
            voice=DASHSCOPE_TTS_VOICE,
            response_format=AudioFormat.PCM_24000HZ_MONO_16BIT,
            mode="server_commit",
        )
        client.append_text(text)
        client.finish()
        cb.wait_for_finished(DASHSCOPE_TTS_TIMEOUT_SECONDS)
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"DashScope TTS failed: {e}")
    finally:
        try:
            client.close()
        except Exception:
            logger.warning("DashScope TTS: error closing WS connection", exc_info=True)

    if cb.error:
        raise RuntimeError(cb.error)
    if not cb.buf:
        raise RuntimeError("DashScope TTS session finished with no audio")

    return _pcm_to_wav(bytes(cb.buf))
