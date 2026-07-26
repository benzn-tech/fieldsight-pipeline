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
    50 chars and the list at 1000 (scribe_v2 limits). Missing file -> []."""
    terms = []
    try:
        with open(vocab_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                phrase = line.split("\t")[0].strip()
                if phrase:
                    terms.append(phrase[:50])
    except OSError:
        logger.warning(f"keyterms vocab not found: {vocab_path}")
        return []
    return terms[:1000]


def transcribe_segment(audio_bytes, filename, num_speakers=5, keyterms=None):
    """POST one audio segment to scribe_v2; return AWS-Transcribe-shaped dict.

    Raises RuntimeError on missing key or after MAX_ATTEMPTS failed attempts."""
    if not ELEVENLABS_API_KEY:
        raise RuntimeError("ELEVENLABS_API_KEY not set")
    fields = {
        "model_id": ELEVENLABS_STT_MODEL,
        "diarize": "true",
        "num_speakers": str(num_speakers),
        "timestamps_granularity": "word",
        "file": (filename, audio_bytes, "application/octet-stream"),
    }
    if ELEVENLABS_LANGUAGE:
        fields["language_code"] = ELEVENLABS_LANGUAGE
    if keyterms:
        # scribe_v2 keyterms: JSON array string. Confirmed against a live
        # response during Phase-2 validation (OI-2); adjust encoding if needed.
        fields["keyterms"] = json.dumps(keyterms)

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
        raise RuntimeError(f"ElevenLabs STT error HTTP {resp.status}: {resp.data[:300]}")
    raise RuntimeError(f"ElevenLabs STT failed after {MAX_ATTEMPTS} attempts: {last_error}")
