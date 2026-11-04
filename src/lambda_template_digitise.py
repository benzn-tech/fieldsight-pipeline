"""lambda_template_digitise.py -- read a checklist form into a draft template.

Non-VPC (it calls OpenRouter; org-api is in-VPC with no outbound, BUG-36). Triggered by
org-api writing template_digitise_requests/{company_id}/{id}.json (wired by
scripts/wire-s3-events.sh, BUG-33). Reads the sources under template_sources/{company_id}/,
routes a Word file to python-docx and a PDF or photos to one vision call, validates in
code (template_draft), and writes template_digitise_results/{company_id}/{id}.json for the
poll. Writes a result for EVERY request it accepts, including failures: an async invoke
that raises is retried twice by Lambda, which would spend two more model calls on a form
that already failed.

Design: docs/superpowers/specs/2026-10-16-template-digitise-design.md section 3.
"""
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from io import BytesIO
from urllib.parse import unquote_plus

import boto3

import template_docx
import template_draft
import template_types
import template_vision
from lake_put import lake_put

logger = logging.getLogger()
logger.setLevel(logging.INFO)

S3_BUCKET = os.environ.get("S3_BUCKET", "")
REQUEST_RE = re.compile(
    r"^template_digitise_requests/(?P<company>[0-9a-fA-F-]{36})/(?P<id>[0-9a-f]{32})\.json$")
SHAPE_ERROR = template_types.SHAPE_ERROR
MSG_WORD_DISABLED = "Word files cannot be read on this deployment — upload a PDF."
MSG_WORD_NO_TABLE = ("No checklist table found in the Word file — upload a PDF export "
                     "of it instead.")
MSG_NO_ROWS = "No checklist rows found — is this a checklist form?"
MSG_FAILED = "Something went wrong reading the form — try again."
MSG_TOO_LARGE = "This file is larger than 20 MB — upload a smaller file."
# Leave the runtime this much after the model call for the result write: the deadline
# handed to template_vision is the invocation's remaining time minus this, so a run that
# meets a slow model still writes its result instead of being killed with the poll pending.
WRITE_RESERVE_SECONDS = 30

logger.info("docx reader: %s", "enabled" if template_docx.AVAILABLE else "DISABLED")

_s3_client = None


def s3():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3")
    return _s3_client


def result_key(company_id, request_id):
    return f"template_digitise_results/{company_id}/{request_id}.json"


def _empty(status, **extra):
    return {"status": status, "sections": [], "counts": {"sections": 0, "items": 0},
            "flags": [], "route": None, "model": None, "pages": None, **extra}


def _read(key):
    return s3().get_object(Bucket=S3_BUCKET, Key=key)["Body"].read()


class _TooLarge(Exception):
    pass


def _read_source(key):
    """A source's bytes, refused BEFORE they are read when S3 says the object is over
    template_types.MAX_BYTES. The presigned PUT names a size but S3 does not hold the
    uploader to it, so the object in the bucket can be any size; reading it first would
    put the whole thing in this function's memory to find that out."""
    obj = s3().get_object(Bucket=S3_BUCKET, Key=key)
    size = obj.get("ContentLength")
    if size is not None and size > template_types.MAX_BYTES:
        obj["Body"].close()
        logger.warning("template digitise: %s is %s bytes, over %s -- not read",
                       key, size, template_types.MAX_BYTES)
        raise _TooLarge(key)
    return obj["Body"].read()


def digitise(request, deadline=None):
    """The result JSON for one request (without the id/name/user fields _process adds).
    `deadline` is seconds from this call; the time spent reading the sources comes off it
    before the model is asked."""
    started = time.monotonic()
    company = request["companyId"]
    keys = list(request.get("sourceKeys") or [])
    prefix = f"template_sources/{company}/"
    if not keys or any(not isinstance(k, str) or not k.startswith(prefix) for k in keys):
        logger.error("template digitise %s: a source is outside %s -- refused", request["id"], prefix)
        return _empty("error", error=SHAPE_ERROR)
    exts = [template_types.ext_of(k) for k in keys]
    if template_types.source_shape_error(exts):
        return _empty("error", error=SHAPE_ERROR)

    if exts == ["docx"]:
        if not template_docx.AVAILABLE:
            return _empty("error", route="docx", error=MSG_WORD_DISABLED)
        try:
            body = _read_source(keys[0])
        except _TooLarge:
            return _empty("error", route="docx", error=MSG_TOO_LARGE)
        raw = template_docx.read(BytesIO(body))
        draft = template_draft.clean(raw)
        if draft["counts"]["items"] == 0:
            return _empty("unreadable", route="docx", reason=MSG_WORD_NO_TABLE)
        return {"status": "done", **draft, "route": "docx", "model": None, "pages": None}

    try:
        pages = [{"body": _read_source(k), "content_type": template_types.EXT_TYPES[e],
                  "name": k.rsplit("/", 1)[-1]} for k, e in zip(keys, exts)]
    except _TooLarge:
        return _empty("error", route="vision", error=MSG_TOO_LARGE)
    why = template_vision.check(pages)
    if why:
        return _empty("unreadable", route="vision", reason=why)
    page_count = (template_vision.pdf_page_count(pages[0]["body"]) or None
                  if exts == ["pdf"] else len(pages))
    left = None if deadline is None else max(1.0, deadline - (time.monotonic() - started))
    raw, err = template_vision.read(pages, deadline=left)
    # The model that read THESE pages: a PDF can go to TEMPLATE_DIGITISE_PDF_MODEL.
    used = template_vision.model_for(pages)
    if err:
        return _empty("error", route="vision", model=used, error=err)
    draft = template_draft.clean(raw)
    if draft["counts"]["items"] == 0:
        return _empty("unreadable", route="vision", model=used,
                      pages=page_count, reason=MSG_NO_ROWS)
    return {"status": "done", **draft, "route": "vision", "model": used,
            "pages": page_count}


def _deadline(context):
    if context is None:
        return None
    return max(1.0, context.get_remaining_time_in_millis() / 1000.0 - WRITE_RESERVE_SECONDS)


def _process(key, context):
    m = REQUEST_RE.match(key)
    if not m:
        logger.warning("template digitise: ignoring %s (not a request key)", key)
        return
    # The key's own company and id decide where the result goes: the poll looks only there.
    company, request_id = m.group("company"), m.group("id")
    deadline = _deadline(context)
    started = time.monotonic()
    request = {}
    try:
        request = json.loads(_read(key).decode("utf-8"))
        if (not isinstance(request, dict) or str(request.get("companyId")) != company
                or request.get("id") != request_id):
            logger.error("template digitise: %s does not match its own key -- refused", key)
            request = {}  # echo nothing from a request that is not this key's
            result = _empty("error", error=MSG_FAILED)
        else:
            left = None if deadline is None else max(1.0, deadline - (time.monotonic() - started))
            result = digitise(request, deadline=left)
    except Exception:  # noqa: BLE001 - the person must get an answer, not a pending poll
        logger.exception("template digitise %s failed", request_id)
        result = _empty("error", error=MSG_FAILED)
    result.update({"id": request_id, "companyId": company, "userId": request.get("userId"),
                   "name": request.get("name"), "sourceKeys": request.get("sourceKeys") or [],
                   "finishedAt": datetime.now(timezone.utc).isoformat()})
    lake_put(S3_BUCKET, result_key(company, request_id), json.dumps(result, ensure_ascii=False),
             owner_hint={"company_id": company}, client=s3(), ContentType="application/json")
    logger.info("template digitise %s: %s route=%s sections=%s items=%s", request_id,
                result["status"], result.get("route"), result["counts"]["sections"],
                result["counts"]["items"])


def lambda_handler(event, context):
    for rec in event.get("Records", []):
        key = ((rec.get("s3") or {}).get("object") or {}).get("key")
        if key:
            _process(unquote_plus(key), context)
    return {"ok": True}
