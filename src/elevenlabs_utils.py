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


# --- Short-clip STT for the voice Ask path ---------------------------------
#
# A SEPARATE KEY, deliberately, and the toggle cannot be switched on without it.
#
# EL credit is a shared per-key pool and exhaustion presents as transcription
# simply STOPPING -- no error, no alarm. Production's recording pipeline already
# runs on ELEVENLABS_API_KEY. If Ask shared it, an afternoon of voice questions
# could silently stop the recording pipeline, and a heavy recording day could
# silently stop Ask. Falling back to the shared key "for now" is exactly how
# that coupling would ship unnoticed, so there is no fallback: no key, no
# provider.
ELEVENLABS_ASK_API_KEY = os.environ.get("ELEVENLABS_ASK_API_KEY", "")
# 10s, not the 280s the batch path uses. This call sits in front of a user
# holding a device, inside a chain with a 29s API Gateway ceiling. Measured
# median is 0.9-1.4s on 3-14s clips, so 10s is already ~7x headroom; anything
# longer is a hung request pretending to be a slow one.
ELEVENLABS_ASK_TIMEOUT_SECONDS = float(
    os.environ.get("ELEVENLABS_ASK_TIMEOUT_SECONDS", "10"))


def stt_short(audio_bytes, filename="clip.wav"):
    """Transcribe one short spoken question. Returns text, "" if nothing heard.

    Measured 2026-09-09 against the same three real site clips as the incumbent,
    three runs each:

        clip     DashScope    ElevenLabs
         3s        3.73s        0.88s
         8s        6.39s        1.01s
        14s        7.72s        1.36s

    4-6x, and the spread collapses with it (DashScope ran 3.15-6.85 on the 8s
    clip; EL 0.98-1.16). That is the entire reason this exists.

    NO DIARISATION. The batch path asks for `diarize` because a site recording
    has several speakers worth separating; a question asked into a push-to-talk
    key has one. Measured to cost nothing either way (0.78 vs 0.88, 1.02 vs
    1.01, 1.53 vs 1.36), so this is not a speed decision -- it drops a
    multi-tenancy exposure for free, since speaker features are the part of this
    vendor whose scoping we have never verified.

    ONE retry, not four. `transcribe_segment` backs off 1+2+4s because a lost
    recording is unrecoverable; a lost question is re-askable, and seven seconds
    of sleeps against a 29s ceiling would turn a slow answer into no answer.

    Raises RuntimeError so the caller's existing `except` maps it to the device's
    error cue exactly as the incumbent's failures do."""
    if not ELEVENLABS_ASK_API_KEY:
        raise RuntimeError("ELEVENLABS_ASK_API_KEY not set")
    if not audio_bytes:
        return ""

    fields = [
        ("model_id", ELEVENLABS_STT_MODEL),
        ("file", (filename, audio_bytes, "application/octet-stream")),
    ]
    if ELEVENLABS_LANGUAGE:
        fields.append(("language_code", ELEVENLABS_LANGUAGE))

    http = urllib3.PoolManager()
    last = None
    for attempt in (1, 2):
        try:
            resp = http.request(
                "POST", ELEVENLABS_STT_URL, fields=fields,
                headers={"xi-api-key": ELEVENLABS_ASK_API_KEY},
                timeout=ELEVENLABS_ASK_TIMEOUT_SECONDS)
        except Exception as e:
            last = "request failed: %r" % (e,)
        else:
            if resp.status == 200:
                try:
                    return (json.loads(resp.data).get("text") or "").strip()
                except Exception as e:
                    raise RuntimeError("ElevenLabs STT: unreadable 200: %r" % (e,))
            # 4xx other than 429 is permanent -- a bad key or a rejected file
            # will not become valid on a second try, and retrying spends the
            # user's remaining seconds proving it.
            if resp.status != 429 and resp.status < 500:
                raise RuntimeError("ElevenLabs STT: HTTP %d %s"
                                   % (resp.status, resp.data[:200]))
            last = "HTTP %d" % resp.status
        if attempt == 1:
            time.sleep(1.0)
    raise RuntimeError("ElevenLabs STT failed after 2 attempts: %s" % last)


# --- TTS for the voice Ask path --------------------------------------------
#
# ITS OWN KEY AGAIN, for the reason stt_short states: EL credit is a shared
# per-key pool and running out looks like the feature simply stopping. Three
# consumers now want EL -- the recording pipeline's transcription, Ask's STT,
# and this -- and a shared pool would let any one of them silently stop the
# other two. No key, no provider.
ELEVENLABS_TTS_API_KEY = os.environ.get("ELEVENLABS_API_KEY_TTS", "")
ELEVENLABS_TTS_URL = os.environ.get(
    "ELEVENLABS_TTS_URL", "https://api.elevenlabs.io/v1/text-to-speech")
ELEVENLABS_TTS_VOICE = os.environ.get(
    "ELEVENLABS_TTS_VOICE", "bPkjmCb0W1xUBvyH2Afs")
# v3 conversational, chosen on measurement rather than the docs. Measured
# 2026-09-09 on this endpoint, one two-sentence answer:
#
#   DashScope (incumbent)      2.07s
#   eleven_v3_conversational   1.01s   <- twice as fast, and the expressive model
#   eleven_v3                  2.83s
#   eleven_flash_v2_5          0.51s   <- fastest, least expressive
#
# The vendor documents v3-conversational on the Text-to-Dialogue WebSocket, so
# the obvious reading is that it needs a second client. It does not: the plain
# HTTP /stream endpoint accepts it, verified with a 200 and real audio. Flash is
# half a second quicker and is the fallback if expressiveness stops mattering,
# but this is a voice a person on a site listens to, and one second is already
# well inside the wait the rest of the chain imposes.
ELEVENLABS_TTS_MODEL = os.environ.get(
    "ELEVENLABS_TTS_MODEL", "eleven_v3_conversational")
# 1.2 = 20% faster than written, at the owner's request. Verified to take
# effect rather than be silently accepted: the same sentence rendered 5.36s at
# default and 5.20s at 1.2 on v3-conversational, 4.64 -> 3.81 on flash. A
# vendor that ignores an unknown field returns 200 either way, so the audio
# length is the only proof.
ELEVENLABS_TTS_SPEED = float(os.environ.get("ELEVENLABS_TTS_SPEED", "1.2"))
ELEVENLABS_TTS_TIMEOUT_SECONDS = float(
    os.environ.get("ELEVENLABS_TTS_TIMEOUT_SECONDS", "10"))


def tts(text):
    """Synthesize one spoken answer. Returns raw PCM 24k mono 16-bit.

    Returns PCM rather than WAV so the caller wraps it with the same
    `_pcm_to_wav` the incumbent uses -- the device is handed an identical
    container either way and never learns which vendor spoke.

    The incumbent measured 2.07s total for a two-sentence answer: 1.0s of
    connection and 0.75s of model. Pre-opening its socket during STT+LLM was
    tested and saved NOTHING (2.07 -> 2.08), because connect() is synchronous.
    So this is not expected to be dramatically faster; it is here because the
    owner asked for one voice vendor, and because the incumbent's model carries
    a vendor retirement note. Measure before switching a stack -- the repo's one
    EL timing data point on long audio runs the OTHER way (86.7s vs 50.7s).

    ONE retry on 429/5xx only, like stt_short: a permanent 4xx will not become
    valid on a second try, and spending the user's remaining seconds proving it
    is how a slow answer becomes no answer against a 29s ceiling."""
    if not ELEVENLABS_TTS_API_KEY:
        raise RuntimeError("ELEVENLABS_API_KEY_TTS not set")
    if not text or not text.strip():
        return b""

    url = "%s/%s/stream?output_format=pcm_24000" % (
        ELEVENLABS_TTS_URL.rstrip("/"), ELEVENLABS_TTS_VOICE)
    payload = {"text": text, "model_id": ELEVENLABS_TTS_MODEL}
    if ELEVENLABS_TTS_SPEED and ELEVENLABS_TTS_SPEED != 1.0:
        payload["voice_settings"] = {"speed": ELEVENLABS_TTS_SPEED}
    body = json.dumps(payload).encode()
    headers = {"xi-api-key": ELEVENLABS_TTS_API_KEY,
               "Content-Type": "application/json",
               "Accept": "audio/pcm"}

    http = urllib3.PoolManager()
    last = None
    for attempt in (1, 2):
        try:
            resp = http.request("POST", url, body=body, headers=headers,
                                timeout=ELEVENLABS_TTS_TIMEOUT_SECONDS)
        except Exception as e:
            last = "request failed: %r" % (e,)
        else:
            if resp.status == 200:
                audio = resp.data or b""
                if not audio:
                    raise RuntimeError("ElevenLabs TTS returned no audio")
                logger.info("tts: provider=elevenlabs model=%s bytes=%d "
                            "audio_seconds=%.2f chars=%d voice=%s speed=%.2f",
                            ELEVENLABS_TTS_MODEL, len(audio),
                            len(audio) / 48000.0, len(text),
                            ELEVENLABS_TTS_VOICE, ELEVENLABS_TTS_SPEED)
                return audio
            if resp.status != 429 and resp.status < 500:
                raise RuntimeError("ElevenLabs TTS: HTTP %d %s"
                                   % (resp.status, resp.data[:200]))
            last = "HTTP %d" % resp.status
        if attempt == 1:
            time.sleep(1.0)
    raise RuntimeError("ElevenLabs TTS failed after 2 attempts: %s" % last)
