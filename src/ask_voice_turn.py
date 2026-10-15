"""Device Ask two-phase: turn store, `start`, and the async worker.

Spec docs/superpowers/specs/2026-10-15-device-ask-two-phase-design.md sections 3-5.

* `start(body)` runs inside AskAgent (mode "voice_start"): STT, mint a turn, put
  state "pending", Event-invoke the worker, then restate + TTS the restatement
  inside a 3.0 s window.
* `worker_handler` is the AskVoiceWorker Lambda: RAG (+web) -> TTS -> state "done".
* The store helpers are shared with ApiFunction's result/cancel routes.

Turn state lives under `ask_turns/` in the data bucket (ASK_TURN_BUCKET). Nothing
here logs the transcript or the answer; log lines carry ids, timings, counts and a
hash of the caller.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import datetime

import boto3
from botocore.config import Config

logger = logging.getLogger()

TURN_RE = re.compile(r"^[0-9a-f]{32}$")
PREFIX = "ask_turns/"

# Restate has its own timeout; the TTS of the restatement gets 1.8 s more.
# 1.5, not the spec's first 1.2: measured on TEST 2026-10-15, uncached restates
# took 0.81-1.16 s (n=7), so 1.2 would drop a share of them to the filler.
# 2.0 after the owner's device test (2026-10-15): 2 of 5 restates timed out at
# 1.52 s and 1.83 s, so the owner heard no restatement on those turns.
RESTATE_TIMEOUT_S = 2.0
RESTATE_TTS_TIMEOUT_S = 1.8

# Voice must never speak citation markers such as [1] or [1, 2].
_CITATION = re.compile(r"\s*\[\d+(?:\s*,\s*\d+)*\]")

_MISSING_CODES = ("NoSuchKey", "404", "NotFound")

_s3 = None
_lambda = None


def _get_s3():
    global _s3
    if _s3 is None:
        # Bounded: a hung S3 call must not eat a long-poll's whole wait.
        _s3 = boto3.client("s3", config=Config(
            connect_timeout=1, read_timeout=2, retries={"max_attempts": 1}))
    return _s3


def _get_lambda():
    global _lambda
    if _lambda is None:
        _lambda = boto3.client("lambda")
    return _lambda


def _bucket():
    return os.environ.get("ASK_TURN_BUCKET", "")


_bucket_logged = False


def _bucket_missing():
    global _bucket_logged
    if not _bucket_logged:
        _bucket_logged = True
        logger.error("voice turn: ASK_TURN_BUCKET is not set")


def sub_hash(caller_sub) -> str:
    """Log-safe identifier of the caller (the sub itself never appears in a log)."""
    return hashlib.sha256(str(caller_sub).encode("utf-8")).hexdigest()[:12]


# --------------------------------------------------------------------------
# Turn store
# --------------------------------------------------------------------------

def owner_prefix(caller_sub) -> str:
    return PREFIX + hashlib.sha256(str(caller_sub).encode("utf-8")).hexdigest()[:16] + "/"


def state_key(caller_sub, turn_id) -> str:
    return "%s%s.json" % (owner_prefix(caller_sub), turn_id)


def cancel_key(caller_sub, turn_id) -> str:
    return "%s%s.cancel" % (owner_prefix(caller_sub), turn_id)


def _is_missing(exc) -> bool:
    """A key that is not there. The roles hold s3:ListBucket on ask_turns/*, so a
    missing key is NoSuchKey/404; AccessDenied/403 is a real IAM fault and raises."""
    code = str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))
    return code in _MISSING_CODES


def put_state(s3, bucket, caller_sub, turn_id, state: dict):
    s3.put_object(Bucket=bucket, Key=state_key(caller_sub, turn_id),
                  Body=json.dumps(state).encode("utf-8"),
                  ContentType="application/json")


def get_state(s3, bucket, caller_sub, turn_id):
    try:
        resp = s3.get_object(Bucket=bucket, Key=state_key(caller_sub, turn_id))
    except Exception as exc:  # noqa: BLE001
        if _is_missing(exc):
            return None
        raise
    return json.loads(resp["Body"].read())


def is_cancelled(s3, bucket, caller_sub, turn_id) -> bool:
    try:
        s3.get_object(Bucket=bucket, Key=cancel_key(caller_sub, turn_id))
    except Exception as exc:  # noqa: BLE001
        if _is_missing(exc):
            return False
        raise
    return True


def put_cancel(s3, bucket, caller_sub, turn_id):
    s3.put_object(Bucket=bucket, Key=cancel_key(caller_sub, turn_id), Body=b"")


def delete_turn(s3, bucket, caller_sub, turn_id, keep_marker=False):
    """Delete the turn's state, and its cancel marker unless keep_marker. A turn
    the device stopped waiting for may still have a worker running; the marker is
    the only thing that stops it writing `done` and an audit row, so the caller
    keeps it for those turns and the day-old sweep removes it later."""
    keys = [state_key(caller_sub, turn_id)]
    if not keep_marker:
        keys.append(cancel_key(caller_sub, turn_id))
    for key in keys:
        s3.delete_object(Bucket=bucket, Key=key)


def _epoch(t) -> float:
    return t.timestamp() if isinstance(t, datetime) else float(t)


SWEEP_MAX_DELETES = 50


def sweep_old(s3, bucket, caller_sub, now, max_age_s=86400):
    """Delete this caller's turn objects older than max_age_s. `now` is a datetime
    or epoch seconds. Returns the number deleted."""
    cutoff = _epoch(now) - max_age_s
    deleted = 0
    token = None
    while True:
        kw = {"Bucket": bucket, "Prefix": owner_prefix(caller_sub)}
        if token:
            kw["ContinuationToken"] = token
        resp = s3.list_objects_v2(**kw)
        for obj in resp.get("Contents") or []:
            if _epoch(obj["LastModified"]) < cutoff:
                s3.delete_object(Bucket=bucket, Key=obj["Key"])
                deleted += 1
                # Bounded: this runs before the worker invoke on the user's
                # critical path; whatever is left goes on the next ask.
                if deleted >= SWEEP_MAX_DELETES:
                    return deleted
        if not resp.get("IsTruncated"):
            return deleted
        token = resp.get("NextContinuationToken")
        if not token:
            return deleted


# --------------------------------------------------------------------------
# start (runs inside AskAgent)
# --------------------------------------------------------------------------

def _voice_web_on() -> bool:
    return (os.environ.get("VOICE_WEB_ENABLED", "") == "true"
            and os.environ.get("ENABLE_WEB_ANSWER", "false").lower() == "true")


def _restatement(question, web_intent, laa, t0):
    """(restatement dict | None, reason, restate_s, restate_tts_s). Never raises."""
    import voice_restate
    try:
        r = voice_restate.restate(question, web_intent=web_intent,
                                  timeout_s=RESTATE_TIMEOUT_S)
    except Exception:  # noqa: BLE001 - the device speaks its filler instead
        return None, "restate_error", time.perf_counter() - t0, 0.0
    restate_s = time.perf_counter() - t0
    text = r.get("text")
    if not text:
        return None, r.get("reason"), restate_s, 0.0
    t1 = time.perf_counter()
    ex = ThreadPoolExecutor(max_workers=1)
    try:
        fut = ex.submit(laa._tts, text)
        try:
            wav = fut.result(timeout=RESTATE_TTS_TIMEOUT_S)
        except FutureTimeout:
            return None, "tts_timeout", restate_s, time.perf_counter() - t1
        except Exception:  # noqa: BLE001
            return None, "tts_error", restate_s, time.perf_counter() - t1
    finally:
        # Do not wait for a TTS that has already missed its window.
        ex.shutdown(wait=False, cancel_futures=True)
    if not wav:
        return None, "tts_error", restate_s, time.perf_counter() - t1
    return ({"text": text, "audioBase64": base64.b64encode(wav).decode("ascii"),
             "audioFormat": "wav"},
            r.get("reason"), restate_s, time.perf_counter() - t1)


def start(body) -> dict:
    """Spec 3.1 steps 1-7. Returns the response body; never raises.

    Without RAG_SEARCH_FUNCTION the old handler path falls through to its
    empty-question check, so the equivalent body is {"error": "Missing question"}.
    """
    import lambda_ask_agent as laa
    import voice_web

    t0 = time.perf_counter()
    caller_sub = body.get("caller_sub")
    if not caller_sub:
        return {"error": "sign-in required"}
    if not os.environ.get("RAG_SEARCH_FUNCTION"):
        return {"error": "Missing question"}

    fmt = body.get("format") or "m4a"
    try:
        audio_bytes = base64.b64decode(body.get("audio") or "", validate=True)
    except Exception:  # noqa: BLE001
        return {"error": "Invalid audio encoding"}

    try:
        transcript = laa._stt(audio_bytes, fmt)
    except Exception as e:  # noqa: BLE001
        logger.error("  voice STT failed: %s", type(e).__name__)
        return {"error": "Speech recognition failed"}
    stt_s = time.perf_counter() - t0
    if not transcript or not transcript.strip():
        return {"error": "Empty transcript", "transcript": ""}

    web_intent, question = voice_web.detect_intent(transcript)
    # "Searching online now" is a promise; only make it when the worker can keep
    # it. Both flags gate the web branch (web_answer.enabled() reads the second).
    if not _voice_web_on():
        web_intent, question = False, transcript
    turn_id = secrets.token_hex(16)
    s3, bucket = _get_s3(), _bucket()
    if not bucket:
        _bucket_missing()
        return {"error": "No answer"}

    try:
        sweep_old(s3, bucket, caller_sub, time.time())
    except Exception as e:  # noqa: BLE001 - housekeeping must never fail an ask
        logger.warning("  voice start sweep failed (non-fatal): %s", type(e).__name__)

    payload = {"turn_id": turn_id, "caller_sub": caller_sub,
               "transcript": transcript, "web_intent": web_intent}
    if body.get("tz") is not None:
        payload["tz"] = body.get("tz")
    if "history" in body:
        payload["history"] = body.get("history")

    try:
        put_state(s3, bucket, caller_sub, turn_id,
                  {"status": "pending", "created_at": time.time()})
        _get_lambda().invoke(FunctionName=os.environ.get("ASK_VOICE_WORKER_FUNCTION", ""),
                             InvocationType="Event",
                             Payload=json.dumps(payload).encode("utf-8"))
    except Exception as e:  # noqa: BLE001
        logger.error("  voice start worker invoke failed: %s", type(e).__name__)
        try:
            put_state(s3, bucket, caller_sub, turn_id,
                      {"status": "error", "error": "No answer"})
        except Exception:  # noqa: BLE001
            pass
        return {"error": "No answer", "transcript": transcript}

    t_restate = time.perf_counter()
    restatement, reason, restate_s, restate_tts_s = _restatement(
        question, web_intent, laa, t_restate)

    logger.info(
        "voice start: stt=%.2fs restate=%.2fs restate_tts=%.2fs start_total=%.2fs "
        "restated=%s reason=%s web_intent=%s turn=%s sub_hash=%s",
        stt_s, restate_s, restate_tts_s, time.perf_counter() - t0,
        restatement is not None, reason if restatement is None else None,
        web_intent, turn_id, sub_hash(caller_sub))
    return {"turn_id": turn_id, "transcript": transcript, "web_intent": web_intent,
            "restatement": restatement}


# --------------------------------------------------------------------------
# AskVoiceWorker
# --------------------------------------------------------------------------

def worker_handler(event, context):
    """Spec section 5 steps 0-6. Returns None; the result is the turn state."""
    logging.getLogger().setLevel(logging.INFO)
    import lambda_ask_agent as laa
    import voice_web

    t0 = time.perf_counter()
    turn_id = event.get("turn_id")
    caller_sub = event.get("caller_sub")
    transcript = event.get("transcript") or ""
    web_intent = bool(event.get("web_intent"))
    if not (isinstance(turn_id, str) and TURN_RE.match(turn_id)) or not caller_sub:
        logger.error("voice worker: bad event")
        return None
    s3, bucket = _get_s3(), _bucket()

    if not bucket:
        _bucket_missing()
        return None

    # 0. Async invoke is at-least-once: only a pending turn is worked. A failed
    # read writes nothing -- the turn is not ours until the pending check passes.
    try:
        state = get_state(s3, bucket, caller_sub, turn_id)
    except Exception as e:  # noqa: BLE001
        logger.error("voice worker: state read failed turn=%s err=%s", turn_id, type(e).__name__)
        return None
    if not state or state.get("status") != "pending":
        return None
    created_at = state.get("created_at")

    try:
        # 1. Cancel check.
        if is_cancelled(s3, bucket, caller_sub, turn_id):
            return None

        # 2. RAG body.
        question = voice_web.detect_intent(transcript)[1] if web_intent else transcript
        rag_body = {"question": question, "caller_sub": caller_sub, "mode": "voice",
                    "k": 5, "tz": event.get("tz"),
                    "voice_web": os.environ.get("VOICE_WEB_ENABLED", "") == "true",
                    "web_intent": web_intent}
        if "history" in event:
            rag_body["history"] = event.get("history")

        def _on_web_start():
            if not is_cancelled(s3, bucket, caller_sub, turn_id):
                put_state(s3, bucket, caller_sub, turn_id, {"status": "searching_web",
                                                   "created_at": created_at})

        # 3. RAG (+web).
        t_rag = time.perf_counter()
        rag = laa._rag_answer(rag_body, on_web_start=_on_web_start)
        rag_s = time.perf_counter() - t_rag
        answer_text = _CITATION.sub("", (rag.get("answer") or "")).strip()
        if rag.get("error") or not answer_text:
            put_state(s3, bucket, caller_sub, turn_id,
                      {"status": "error", "error": rag.get("error") or "No answer"})
            return None

        # 4. Cancel check, then TTS.
        if is_cancelled(s3, bucket, caller_sub, turn_id):
            return None
        t_tts = time.perf_counter()
        try:
            audio_out = laa._tts(answer_text)
        except Exception as e:  # noqa: BLE001
            logger.error("  voice TTS failed: %s", type(e).__name__)
            put_state(s3, bucket, caller_sub, turn_id,
                      {"status": "error", "error": "Speech synthesis failed"})
            return None
        tts_s = time.perf_counter() - t_tts

        # 5. Cancel check, then done.
        if is_cancelled(s3, bucket, caller_sub, turn_id):
            return None
        web = bool(rag.get("from_web"))
        put_state(s3, bucket, caller_sub, turn_id, {
            "status": "done", "transcript": transcript, "answerText": answer_text,
            "basis": rag.get("basis"),
            "audioBase64": base64.b64encode(audio_out).decode("ascii"),
            "audioFormat": "wav", "web": web})
        h = event.get("history")
        logger.info(
            "voice ask: stt=%.2fs rag=%.2fs tts=%.2fs total=%.2fs "
            "clip_bytes=%d transcript_words=%d answer_words=%d answer_chars=%d "
            "audio_bytes=%d fmt=%s history_turns=%d turn=%s web=%s sub_hash=%s",
            0.0, rag_s, tts_s, time.perf_counter() - t0, 0,
            len(transcript.split()), len(answer_text.split()), len(answer_text),
            len(audio_out), "-", len(h) if isinstance(h, list) else 0,
            turn_id, web, sub_hash(caller_sub))
        if not is_cancelled(s3, bucket, caller_sub, turn_id):
            laa._invoke_voice_audit(caller_sub, transcript, answer_text)
    except Exception as e:  # noqa: BLE001
        logger.error("voice worker failed: turn=%s err=%s", turn_id, type(e).__name__)
        try:
            put_state(s3, bucket, caller_sub, turn_id,
                      {"status": "error", "error": "No answer"})
        except Exception:  # noqa: BLE001
            pass
    return None
