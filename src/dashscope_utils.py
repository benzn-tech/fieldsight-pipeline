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
