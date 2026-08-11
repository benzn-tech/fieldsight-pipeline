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

# --- File transcription (async) --------------------------------------------
# A DIFFERENT API from DASHSCOPE_AIGC_URL above, with a different body shape.
# Conflating the two is how four separate sessions wrote a broken call: the
# single-shot path takes base64 under input.messages, this one takes a URL
# under input.file_urls and answers with a task id to poll.
DASHSCOPE_TRANSCRIPTION_URL = os.environ.get(
    "DASHSCOPE_TRANSCRIPTION_URL",
    "https://dashscope-intl.aliyuncs.com/api/v1/services/audio/asr/transcription",
)
DASHSCOPE_TASK_URL = os.environ.get(
    "DASHSCOPE_TASK_URL", "https://dashscope-intl.aliyuncs.com/api/v1/tasks/{task_id}",
)
# Only this id accepts INLINE hotwords. Others take a precompiled vocabulary_id
# or nothing; an id without "filetrans" is rejected as "Model not exist." on
# this endpoint, because it belongs to the other API.
DASHSCOPE_FILETRANS_MODEL = os.environ.get(
    "DASHSCOPE_FILETRANS_MODEL", "qwen-audio-3.0-asr-flash-filetrans",
)
# Vendor capacity, not a bad request -- the identical body succeeds minutes
# later. Treated as retryable so a queue spike is never read as a model verdict.
TRANSCRIPTION_RETRYABLE_CODES = {"INSTANCE_POOL_EXHAUSTED"}
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


def transcribe_file(file_url, diarize=True, speaker_count=None, hotwords=None,
                    language_hints=("zh", "en"), model=None, poll_seconds=3.0,
                    budget_seconds=300.0, _sleep=time.sleep):
    """Transcribe an audio file by URL. Returns the parsed transcription payload.

    This is the ASYNC file-transcription API, not stt() above. The differences
    are the ones that have repeatedly been guessed wrong, so they are encoded
    here once instead of being remembered:

      - the input field is `file_urls` (plural, a list). Passing the singular
        name is reported as `InvalidParameter.MalformedURL` -- "A valid file
        URL is required" -- which points at the URL and not at the field.
      - diarization is OFF unless `diarization_enabled` is sent. A run without
        it says nothing about whether the model supports speakers; that
        mistake is how "qwen does not diarize" became a believed fact.
      - `hotwords` go inline as {term: weight 1-5}, and ONLY
        qwen-audio-3.0-asr-flash-filetrans accepts them inline.
      - the task payload does NOT contain the transcript. It contains a
        `transcription_url` that must be fetched separately.

    A caller passing an expired presigned URL gets `FILE_DOWNLOAD_FAILED`,
    which also reads like a model fault. Note that an S3 URL signed with
    TEMPORARY credentials dies with the session token, not at --expires-in.
    """
    if not DASHSCOPE_API_KEY:
        raise RuntimeError("DASHSCOPE_API_KEY not set")
    params = {}
    if diarize:
        params["diarization_enabled"] = True
        if speaker_count:
            params["speaker_count"] = int(speaker_count)
    if language_hints:
        params["language_hints"] = list(language_hints)
    if hotwords:
        params["vocabulary"] = {
            str(t): int(w) for t, w in (hotwords.items() if isinstance(hotwords, dict)
                                        else ((t, 5) for t in hotwords))
        }
    body = json.dumps({
        "model": model or DASHSCOPE_FILETRANS_MODEL,
        "input": {"file_urls": [file_url]},
        "parameters": params,
    })
    http = urllib3.PoolManager()
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {DASHSCOPE_API_KEY}",
        "X-DashScope-Async": "enable",
    }
    for attempt in range(MAX_ATTEMPTS):
        resp = http.request("POST", DASHSCOPE_TRANSCRIPTION_URL, body=body,
                            headers=headers, timeout=60.0)
        if resp.status != 200:
            raise RuntimeError(
                f"DashScope transcription submit HTTP {resp.status}: {resp.data[:300]}")
        task_id = json.loads(resp.data.decode("utf-8")).get("output", {}).get("task_id")
        if not task_id:
            raise RuntimeError(f"no task_id in submit response: {resp.data[:300]}")
        code, payload = _await_transcription(http, headers, task_id, poll_seconds,
                                             budget_seconds, _sleep)
        if code == "SUCCEEDED":
            return payload
        if code in TRANSCRIPTION_RETRYABLE_CODES and attempt < MAX_ATTEMPTS - 1:
            logger.warning("DashScope transcription %s (attempt %d), retrying", code, attempt + 1)
            _sleep(BACKOFF_BASE_SECONDS * (2 ** attempt))
            continue
        raise RuntimeError(f"DashScope transcription failed: {code}")
    raise RuntimeError("DashScope transcription failed after retries")


def _await_transcription(http, headers, task_id, poll_seconds, budget_seconds, _sleep):
    """Poll one task. Returns (code, payload); payload is None unless SUCCEEDED."""
    waited = 0.0
    poll_headers = {"Authorization": headers["Authorization"]}
    while waited < budget_seconds:
        _sleep(poll_seconds)
        waited += poll_seconds
        r = http.request("GET", DASHSCOPE_TASK_URL.format(task_id=task_id),
                         headers=poll_headers, timeout=60.0)
        out = json.loads(r.data.decode("utf-8")).get("output", {})
        status = out.get("task_status")
        if status == "SUCCEEDED":
            return "SUCCEEDED", _fetch_transcription(http, out)
        if status in ("FAILED", "CANCELED"):
            return out.get("code") or status, None
    return "TIMEOUT", None


def _fetch_transcription(http, output):
    """The transcript lives behind a URL in the task result, not in the task.

    Two shapes in the wild: qwen models put it at output.result, fun-asr at
    output.results[0]. Both are checked so a model swap does not silently
    return nothing."""
    url = (output.get("result") or {}).get("transcription_url")
    if not url:
        results = output.get("results") or []
        url = (results[0] if results else {}).get("transcription_url")
    if not url:
        raise RuntimeError(f"SUCCEEDED but no transcription_url: {json.dumps(output)[:300]}")
    r = http.request("GET", url, timeout=60.0)
    return json.loads(r.data.decode("utf-8", "replace"))


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
