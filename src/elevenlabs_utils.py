"""
elevenlabs_utils.py — ElevenLabs scribe_v2 STT client + AWS-Transcribe adapter.

Synchronous batch transcription (multipart POST) plus adapt_to_transcribe_json,
which reshapes the scribe_v2 response into the exact raw AWS Transcribe JSON
that transcript_utils.parse_transcribe_json already consumes — so every
downstream transcript consumer is untouched. Mirrors dashscope_utils.py:
urllib3, env-var key, MAX_ATTEMPTS=4 exponential backoff, loud RuntimeError.

Environment Variables:
    ELEVENLABS_API_KEY   - xi-api-key (required — transcribe_segment raises if unset)
    ELEVENLABS_STT_URL   - endpoint (default: https://api.elevenlabs.io/v1/speech-to-text)
    ELEVENLABS_STT_MODEL - model id (default: scribe_v2)
    ELEVENLABS_LANGUAGE  - ISO 639-3 code to pin language; empty = auto-detect
"""
import json
import logging
import os
import time

import urllib3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVENLABS_STT_URL = os.environ.get(
    "ELEVENLABS_STT_URL", "https://api.elevenlabs.io/v1/speech-to-text"
)
ELEVENLABS_STT_MODEL = os.environ.get("ELEVENLABS_STT_MODEL", "scribe_v2")
ELEVENLABS_LANGUAGE = os.environ.get("ELEVENLABS_LANGUAGE", "")

MAX_ATTEMPTS = 4
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
BACKOFF_BASE_SECONDS = 1.0
# scribe_v2 requires each keyword strictly < 50 chars; slice to 49 so the
# boundary term ([:50] would be exactly 50) does not trip "less than 50".
MAX_KEYTERM_LEN = 49
MAX_KEYTERMS = 1000
# scribe_v2 splits 8min+ audio into up to 4 parallel internal jobs; VAD segments
# are short, but allow generous headroom below the Lambda's own timeout.
HTTP_TIMEOUT = 280.0


def adapt_to_transcribe_json(el_response):
    """Reshape a scribe_v2 response into raw AWS Transcribe JSON.

    Only type=="word" entries become pronunciation items (spacing/audio_event
    dropped — full text comes from the top-level `text`). speaker_id values are
    mapped to spk_0, spk_1, ... in first-seen order; if no word carries a
    speaker_id, no speaker_label is emitted (transcript_utils then treats the
    whole clip as a single 'unknown' turn, matching its no-diarization path).
    Word confidence is a "1.0" placeholder — no downstream consumer reads it.
    """
    text = el_response.get("text", "")
    speaker_map = {}
    items = []
    for w in el_response.get("words", []):
        if w.get("type") != "word":
            continue
        item = {
            "type": "pronunciation",
            "start_time": str(w.get("start", 0.0)),
            "end_time": str(w.get("end", 0.0)),
            "alternatives": [{"content": w.get("text", ""), "confidence": "1.0"}],
        }
        sid = w.get("speaker_id")
        if sid is not None:
            if sid not in speaker_map:
                speaker_map[sid] = f"spk_{len(speaker_map)}"
            item["speaker_label"] = speaker_map[sid]
        items.append(item)
    return {"results": {"transcripts": [{"transcript": text}], "items": items}}


def load_keyterms(vocab_path):
    """Parse the tab-separated NZ construction vocab into a keyterms list.

    Takes the first (Phrase) column of each non-comment line, caps each term at
    49 chars (scribe_v2 requires strictly < 50) and the list at 1000 (scribe_v2
    limits). Missing file -> []."""
    terms = []
    try:
        with open(vocab_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                phrase = line.split("\t")[0].strip()
                if phrase:
                    terms.append(phrase[:MAX_KEYTERM_LEN])
    except OSError:
        logger.warning(f"keyterms vocab not found: {vocab_path}")
        return []
    return terms[:MAX_KEYTERMS]


def _build_fields(audio_bytes, filename, num_speakers, keyterms, include_keyterms):
    """Build the scribe_v2 multipart body as a list of (name, value) tuples.

    A list (not a dict) is used so `keyterms` can be *repeated* — one form entry
    per term — which is how scribe_v2 receives a keyword list. See the keyterms
    reasoning in transcribe_segment. When include_keyterms is False the keyterms
    entries are omitted entirely (the Part-1 fallback path)."""
    fields = [
        ("model_id", ELEVENLABS_STT_MODEL),
        ("diarize", "true"),
        ("num_speakers", str(num_speakers)),
        ("timestamps_granularity", "word"),
        ("file", (filename, audio_bytes, "application/octet-stream")),
    ]
    if ELEVENLABS_LANGUAGE:
        fields.append(("language_code", ELEVENLABS_LANGUAGE))
    if include_keyterms and keyterms:
        for term in keyterms[:MAX_KEYTERMS]:
            term = (term or "")[:MAX_KEYTERM_LEN]
            if term:
                fields.append(("keyterms", term))
    return fields


def _keyterms_rejection_message(body_bytes):
    """Return the API message if a 400 body is a keyterms/keywords validation
    error, else None. Recognizes the scribe_v2 signals: status
    `invalid_keyword_length`, `param` in {keywords, keyterms}, or a message that
    mentions a keyword/keyterm. Robust to non-JSON bodies."""
    try:
        detail = json.loads(body_bytes.decode("utf-8")).get("detail", {})
    except Exception:  # noqa: BLE001 - malformed body is simply "not recognized"
        detail = None
    if not isinstance(detail, dict):
        # Fall back to a raw substring probe so we still catch the signal.
        raw = body_bytes.decode("utf-8", "replace").lower()
        if "invalid_keyword_length" in raw or "keyword" in raw or "keyterm" in raw:
            return raw[:300]
        return None
    status_field = str(detail.get("status", "")).lower()
    param = str(detail.get("param", "")).lower()
    message = str(detail.get("message", ""))
    if (
        status_field == "invalid_keyword_length"
        or param in ("keywords", "keyterms")
        or "keyword" in message.lower()
        or "keyterm" in message.lower()
    ):
        return message or status_field or "keyterms rejected"
    return None


def transcribe_segment(audio_bytes, filename, num_speakers=5, keyterms=None):
    """POST one audio segment to scribe_v2; return AWS-Transcribe-shaped dict.

    keyterms are a best-effort accuracy enhancement, never required: if scribe_v2
    rejects them with a keyterms/keywords validation 400, the request is retried
    ONCE without any keyterms field so the core audio->transcript path always
    works. A 400 that is not keyterms-related still surfaces as a RuntimeError.

    Raises RuntimeError on missing key or after MAX_ATTEMPTS failed attempts."""
    if not ELEVENLABS_API_KEY:
        raise RuntimeError("ELEVENLABS_API_KEY not set")

    include_keyterms = bool(keyterms)
    fields = _build_fields(audio_bytes, filename, num_speakers, keyterms, include_keyterms)

    http = urllib3.PoolManager()
    last_error = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            resp = http.request(
                "POST", ELEVENLABS_STT_URL, fields=fields,
                headers={"xi-api-key": ELEVENLABS_API_KEY}, timeout=HTTP_TIMEOUT,
            )
        except Exception as e:  # noqa: BLE001 - network errors are retryable
            last_error = str(e)
            if attempt < MAX_ATTEMPTS - 1:
                time.sleep(BACKOFF_BASE_SECONDS * (2 ** attempt))
                continue
            raise RuntimeError(f"ElevenLabs STT failed after {MAX_ATTEMPTS} attempts: {last_error}")
        if resp.status == 200:
            return adapt_to_transcribe_json(json.loads(resp.data.decode("utf-8")))
        if resp.status in RETRYABLE_STATUSES and attempt < MAX_ATTEMPTS - 1:
            last_error = f"HTTP {resp.status}"
            time.sleep(BACKOFF_BASE_SECONDS * (2 ** attempt))
            continue
        # keyterms are optional: drop them and retry once rather than fail ASR.
        if resp.status == 400 and include_keyterms:
            api_msg = _keyterms_rejection_message(resp.data)
            if api_msg is not None:
                logger.warning(
                    "ElevenLabs rejected keyterms (%s); retrying once without keyterms",
                    api_msg,
                )
                include_keyterms = False
                fields = _build_fields(
                    audio_bytes, filename, num_speakers, keyterms, include_keyterms
                )
                continue
        raise RuntimeError(f"ElevenLabs STT error HTTP {resp.status}: {resp.data[:300]}")
    raise RuntimeError(f"ElevenLabs STT failed after {MAX_ATTEMPTS} attempts: {last_error}")
