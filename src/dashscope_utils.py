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
    DASHSCOPE_API_KEY    - DashScope API key (required -- embed() raises if unset)
    DASHSCOPE_BASE_URL   - API base (default: DashScope intl compatible-mode v1)
    DASHSCOPE_EMBED_MODEL - embedding model id (default: text-embedding-v4)
    DASHSCOPE_EMBED_DIM  - embedding dimensionality (default: 1024)
"""
import base64
import json
import logging
import os
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

# --- SP-Ask: STT (Qwen ASR) + TTS (qwen-tts) ---------------------------------
# Native (NOT compatible-mode) DashScope multimodal endpoint: audio in/out
# models are exposed here, unlike embeddings which use /compatible-mode/v1.
DASHSCOPE_AIGC_URL = os.environ.get(
    "DASHSCOPE_AIGC_URL",
    "https://dashscope-intl.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation",
)
DASHSCOPE_ASR_MODEL = os.environ.get("DASHSCOPE_ASR_MODEL", "qwen3-asr-flash")
DASHSCOPE_ASR_LANG = os.environ.get("DASHSCOPE_ASR_LANG", "en")
DASHSCOPE_TTS_MODEL = os.environ.get("DASHSCOPE_TTS_MODEL", "qwen-tts")
DASHSCOPE_TTS_VOICE = os.environ.get("DASHSCOPE_TTS_VOICE", "Chelsie")

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


def _extract_asr_text(data):
    """Pull the transcript out of a multimodal-generation ASR response.
    Expected: output.choices[0].message.content is a list of parts, each maybe
    {"text": ...}; tolerant of a plain-string content too. "" if not present."""
    try:
        content = data["output"]["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict)).strip()
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


def tts(text):
    """DashScope TTS (qwen-tts): synthesize `text` to WAV bytes. Mirrors
    embed()'s urllib3 + DASHSCOPE_API_KEY pattern via _aigc_request. Returns
    raw WAV bytes (b"" for empty text). The model returns audio either inline
    (base64) or as a short-lived URL -- handle both. Raises RuntimeError on a
    missing key or a response carrying neither.

    spec §11: verify the qwen-tts request nesting + output container (wav vs
    mp3) against live DashScope in Task 3 Step 5."""
    if not DASHSCOPE_API_KEY:
        raise RuntimeError("DASHSCOPE_API_KEY not set")
    if not text or not text.strip():
        return b""
    body = json.dumps({
        "model": DASHSCOPE_TTS_MODEL,
        "input": {"text": text, "voice": DASHSCOPE_TTS_VOICE},
        "parameters": {"format": "wav"},
    })
    data = _aigc_request(body)
    audio = (data.get("output") or {}).get("audio") or {}
    inline = audio.get("data")
    if inline:
        return base64.b64decode(inline)
    url = audio.get("url")
    if url:
        resp = urllib3.PoolManager().request("GET", url, timeout=60.0)
        if resp.status != 200:
            raise RuntimeError(f"DashScope TTS audio fetch failed: HTTP {resp.status}")
        return resp.data
    raise RuntimeError("DashScope TTS response missing audio data/url")
