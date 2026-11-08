"""ContactFunction: the in-page "Contact FieldSight" window's backend.

Spec: docs/superpowers/specs/2026-11-08-contact-fieldsight-design.md

Routes on FieldSightApi at /api/contact/{proxy+} (default Cognito authorizer):

    POST /api/contact/transcribe   {audio, mime} -> 200 {text} | 200 {text:"", error:"unavailable"}
    POST /api/contact/messages     {id, text, audio, mime, transcript, context} -> 201 {id}

Out of the VPC on purpose: it calls SES and ElevenLabs, and org-api (in the VPC, no
NAT) cannot reach either (CLAUDE.md BUG-36).

Flag CONTACT_FIELDSIGHT, read on every call: anything but "true" answers 404
{"error":"not_found"} for every route except OPTIONS.

Identity is the authorizer claims only. Logs carry the message id and sub, never
text, transcript or audio.
"""
import base64
import concurrent.futures
import json
import logging
import os
from datetime import datetime, timedelta, timezone

from botocore.exceptions import ClientError

import contact_messages as cm

logger = logging.getLogger(__name__)
# Lambda's root logger drops INFO; be explicit so the WARNING/ERROR lines this
# module relies on are never a configuration accident.
logger.setLevel(logging.INFO)

TO_ADDRESS = "sales@fieldsightai.com"
# Outer deadline on one transcribe call. stt_short makes two attempts at
# ELEVENLABS_ASK_TIMEOUT_SECONDS (12 on this function) with a 1 s sleep between,
# so this is what keeps the call inside the 28 s function timeout.
TRANSCRIBE_DEADLINE_S = 25

_clients = {}


def _s3():
    if "s3" not in _clients:
        import boto3
        _clients["s3"] = boto3.client("s3")
    return _clients["s3"]


def _ses():
    if "ses" not in _clients:
        import boto3
        _clients["ses"] = boto3.client("ses")
    return _clients["ses"]


def _stt(audio_bytes, filename):
    # Imported here: elevenlabs_utils reads its key and timeout at import time.
    import elevenlabs_utils
    return elevenlabs_utils.stt_short(audio_bytes, filename)


def _now():
    return datetime.now(timezone.utc)


# --- responses (copied from lambda_fieldsight_api.ok/error) --------------------

def ok(body, status=200):
    return {
        'statusCode': status,
        'headers': {
            'Content-Type': 'application/json',
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Headers': 'Content-Type,Authorization',
            'Access-Control-Allow-Methods': 'GET,POST,PATCH,OPTIONS',
        },
        'body': json.dumps(body, default=str),
    }


def error(message, status=400):
    return ok({'error': message}, status)


def _rate_limited(retry_after_s):
    return ok({"error": "rate_limited", "retry_after_s": int(max(60, retry_after_s))}, 429)


# --- helpers -------------------------------------------------------------------

def _bucket():
    return os.environ.get("S3_BUCKET", "")


def _stage():
    return os.environ.get("STAGE", "test")


def _route(event):
    proxy = (event.get("pathParameters") or {}).get("proxy")
    if proxy is None:
        path = event.get("path") or ""
        proxy = path.split("/api/contact", 1)[-1]
    return "/" + proxy.strip("/")


def _body(event):
    raw = event.get("body")
    if raw is None or raw == "":
        return {}
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    body = json.loads(raw)
    if not isinstance(body, dict):
        raise ValueError("not an object")
    return body


def _claims(event):
    claims = ((event.get("requestContext") or {}).get("authorizer") or {}).get("claims") or {}
    return {"sub": claims.get("sub") or "", "email": claims.get("email") or "",
            "name": claims.get("name") or ""}


def _is_missing(err):
    code = (err.response.get("Error") or {}).get("Code")
    status = (err.response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
    return code in ("NoSuchKey", "404", "NotFound") or status == 404


def _list_keys(prefix):
    s3, keys, token = _s3(), [], None
    while True:
        kw = {"Bucket": _bucket(), "Prefix": prefix}
        if token:
            kw["ContinuationToken"] = token
        resp = s3.list_objects_v2(**kw)
        keys += [o["Key"] for o in resp.get("Contents") or []]
        if not resp.get("IsTruncated"):
            return keys
        token = resp.get("NextContinuationToken")


def _nz_label(now_utc):
    try:
        import nz_time
        nz = nz_time.to_nz(now_utc)
        return nz.strftime("%d %b %Y %H:%M") + " NZ"
    except Exception:
        return now_utc.strftime("%Y-%m-%d %H:%M") + " UTC"


# --- routes --------------------------------------------------------------------

def _transcribe(body, who):
    audio, err = cm.validate_audio(body.get("audio"), body.get("mime"))
    if err:
        return error(err)
    if audio is None:
        return error("no recording to transcribe")

    now = _now()
    key = f"{cm.PREFIX}/{who['sub']}/_transcribe/{now.strftime('%Y-%m-%dT%H')}.json"
    s3 = _s3()
    try:
        n = int(json.loads(s3.get_object(Bucket=_bucket(), Key=key)["Body"].read()).get("n", 0))
    except ClientError as e:
        if not _is_missing(e):
            raise
        n = 0
    if n >= cm.TRANSCRIBE_PER_HOUR:
        next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        return _rate_limited((next_hour - now).total_seconds())
    # Counted before the provider call: an attempt spends the credit pool whether
    # or not it returns words. A concurrent burst can overshoot by a few (accepted).
    s3.put_object(Bucket=_bucket(), Key=key, Body=json.dumps({"n": n + 1}),
                  ContentType="application/json")

    ext = "m4a" if body.get("mime") == "audio/mp4" else "webm"
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        fut = pool.submit(_stt, audio, f"clip.{ext}")
        text = fut.result(timeout=TRANSCRIBE_DEADLINE_S)
    except concurrent.futures.TimeoutError:
        logger.warning("contact transcribe timed out sub=%s", who["sub"])
        return ok({"text": "", "error": "unavailable"})
    except Exception as e:
        logger.warning("contact transcribe failed sub=%s err=%s", who["sub"], type(e).__name__)
        return ok({"text": "", "error": "unavailable"})
    finally:
        # Never wait for a hung provider call; the function timeout ends it.
        pool.shutdown(wait=False)
    return ok({"text": text or ""})


def _messages(body, who):
    clean, err = cm.validate_message(body)
    if err:
        return error(err)
    mid, sub = clean["id"], who["sub"]
    prefix = f"{cm.PREFIX}/{sub}/"
    now = _now()

    keys = _list_keys(prefix)
    if any(k.endswith(f"_{mid}.json") for k in keys):
        logger.info("contact message duplicate id=%s sub=%s", mid, sub)
        return ok({"id": mid}, 201)

    recent = cm.recent_times(keys, now)
    if len(recent) >= cm.MSG_PER_HOUR:
        oldest = recent[0]
        return _rate_limited((oldest + timedelta(hours=1) - now).total_seconds())

    s3, bucket = _s3(), _bucket()
    stem = cm.record_stem(sub, now, mid)
    audio_key = None
    if clean["audio_bytes"] is not None:
        audio_key = f"{stem}.{clean['ext']}"
        s3.put_object(Bucket=bucket, Key=audio_key, Body=clean["audio_bytes"],
                      ContentType=clean["mime"].split(";", 1)[0])
    record_key = f"{stem}.json"
    record = {
        "id": mid, "at": now.isoformat(), "sub": sub, "email": who["email"],
        "claim_name": who["name"], "context": clean["context"],
        "text": clean["text"], "transcript": clean["transcript"],
        "audio_key": audio_key, "mime": clean["mime"], "env": _stage(),
        "record_key": record_key, "email_status": "pending",
    }

    def put_record():
        s3.put_object(Bucket=bucket, Key=record_key, Body=json.dumps(record),
                      ContentType="application/json")

    put_record()
    sender = os.environ.get("SENDER_EMAIL") or TO_ADDRESS
    try:
        raw = cm.build_email(record=record, audio=clean["audio_bytes"], ext=clean["ext"],
                             sender=sender, to=TO_ADDRESS, stage=_stage(),
                             now_nz_label=_nz_label(now))
        _ses().send_raw_email(Source=sender, Destinations=[TO_ADDRESS],
                              RawMessage={"Data": raw})
        record["email_status"] = "sent"
    except Exception as e:
        # The record is already stored; the team recovers from S3. 201 regardless.
        logger.error("contact message email failed id=%s sub=%s err=%s",
                     mid, sub, type(e).__name__)
        record["email_status"] = "email_failed"
    put_record()
    logger.info("contact message stored id=%s sub=%s email_status=%s",
                mid, sub, record["email_status"])
    return ok({"id": mid}, 201)


_ROUTES = {("POST", "/transcribe"): _transcribe, ("POST", "/messages"): _messages}


def lambda_handler(event, context):
    method = (event.get("httpMethod") or "GET").upper()
    if method == "OPTIONS":
        return ok({"message": "CORS OK"})
    if os.environ.get("CONTACT_FIELDSIGHT") != "true":
        return error("not_found", 404)
    handler = _ROUTES.get((method, _route(event)))
    if handler is None:
        return error("not_found", 404)
    who = _claims(event)
    if not who["sub"] or not who["email"]:
        return error("unauthorized", 401)
    try:
        body = _body(event)
    except Exception:
        return error("request body must be a JSON object")
    try:
        return handler(body, who)
    except Exception as e:
        # No body content in the log: id and sub only (the id may be absent here).
        logger.error("contact %s failed sub=%s err=%s", _route(event), who["sub"],
                     type(e).__name__)
        return error("internal_error", 500)
