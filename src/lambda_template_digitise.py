"""lambda_template_digitise.py -- read a checklist form into a draft template.

Non-VPC (it calls OpenRouter; org-api is in-VPC with no outbound, BUG-36). Triggered by
org-api writing template_digitise_requests/{company_id}/{id}.json (wired by
scripts/wire-s3-events.sh, BUG-33). Reads the sources under template_sources/{company_id}/,
routes a Word file to python-docx (or, when no rule finds a row, its text to the model
reader) and a PDF or photos to one vision call, validates in code (template_draft), and
writes template_digitise_results/{company_id}/{id}.json for the poll. Writes a result for
EVERY request it accepts, including failures, so the poll never stays pending: async
retries are off (EventInvokeConfig MaximumRetryAttempts 0), so a raise would only lose the
answer, never re-run the read.

Then, with TEMPLATE_DIGITISE_AUDIT=true, a second model audits the read against the
source this invocation already holds (template_audit), the verdict is stored under
template_digitise_audits/{company_id}/{id}.json, and a `problems` / `missed` verdict is
published to the alert topic for the owner. The audit runs after the result is written
and its failure is only logged: it never changes what the person polls. Beside each
record goes a stats line under template_digitise_audit_stats/{YYYY-MM}/{company_id}/{id}.json:
counts and labels only, no customer text, so it is kept when the record expires (90
days) and the read rate can be followed for as long as the reader keeps changing.

Design: docs/superpowers/specs/2026-10-16-template-digitise-design.md section 3, and
2026-11-06-digitise-reader-and-audit-design.md R4 for the Word fallback, A1-A5 for the
audit.
"""
import copy
import json
import logging
import os
import re
import time
import unicodedata
from datetime import datetime, timezone
from io import BytesIO
from urllib.parse import unquote_plus

import boto3

import template_audit
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
# Under this much of the invocation left (after the reserve) the audit does not start (A1).
AUDIT_MIN_SECONDS = 60
# Why a result was not audited, by the message it carries: the worker never read the
# source, or never sent it to the reader, so the auditor would have nothing to compare.
_SKIP_REASONS = {MSG_TOO_LARGE: "too_large", MSG_WORD_DISABLED: "word_disabled",
                 SHAPE_ERROR: "bad_request", MSG_FAILED: "not_read",
                 template_vision.MSG_PDF_REFUSED: "pdf_refused",
                 template_vision.MSG_TOO_MANY_PAGES: "too_many_pages"}
_ALERT_VERDICTS = ("problems", "missed")
_MAX_LINE = 300

logger.info("docx reader: %s", "enabled" if template_docx.AVAILABLE else "DISABLED")

_s3_client = None
_sns_client = None


def s3():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3")
    return _s3_client


def sns():
    global _sns_client
    if _sns_client is None:
        _sns_client = boto3.client("sns")
    return _sns_client


def result_key(company_id, request_id):
    return f"template_digitise_results/{company_id}/{request_id}.json"


def audit_key(company_id, request_id):
    return f"template_digitise_audits/{company_id}/{request_id}.json"


def stats_key(company_id, request_id, audited_at):
    return f"template_digitise_audit_stats/{audited_at[:7]}/{company_id}/{request_id}.json"


def _ext(key):
    tail = key.rsplit("/", 1)[-1]
    return tail.rsplit(".", 1)[-1].lower() if "." in tail else ""


def audit_stats(record):
    """The kept part of an audit record: counts and labels, never a string the customer
    wrote. No name, no claim text, no summary, no error (it can quote the answer), and
    the file types from the stored keys, whose names are ours, not the upload's."""
    counts = record.get("counts") or {}

    def n(field):
        value = record.get(field)
        return len(value) if isinstance(value, list) else 0

    return {"v": 1, "id": record.get("id"), "companyId": record.get("companyId"),
            "auditedAt": record.get("auditedAt"), "verdict": record.get("verdict"),
            "reason": record.get("reason"), "status": record.get("status"),
            "route": record.get("route"), "readModel": record.get("readModel"),
            "auditModel": record.get("model"),
            "fileTypes": sorted({_ext(k) for k in record.get("sourceKeys") or []}),
            "files": len(record.get("sourceKeys") or []),
            "sections": counts.get("sections"), "items": counts.get("items"),
            "sourceItems": record.get("source_items"), "missing": n("missing"),
            "extra": n("extra"), "reworded": n("reworded"),
            "splitOrMerged": n("split_or_merged"), "droppedClaims": n("dropped_claims"),
            "elapsedS": record.get("elapsed_s")}


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


def digitise(request, deadline=None, source=None):
    """The result JSON for one request (without the id/name/user fields _process adds).
    `deadline` is seconds from this call; the time spent reading the sources comes off it
    before the model is asked. `source`, when given, is filled with what was read
    (kind, and the Word body and text or the pages) for the audit, so it never reads S3
    a second time; it stays empty when the reader never got the source."""
    started = time.monotonic()
    source = {} if source is None else source
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
        source.update(kind="docx", body=body)
        draft = template_draft.clean(template_docx.read(BytesIO(body)))
        if draft["counts"]["items"]:
            return {"status": "done", **draft, "route": "docx", "model": None, "pages": None}
        # No rule found a row: the same model reader as a PDF, on the file as text
        # (spec 2026-11-06 R4). Only on zero items -- a partial rule read is the audit's.
        text = template_docx.render_text(BytesIO(body))
        source["text"] = text
        if len(text) > template_vision.MAX_TEXT_CHARS:
            return _empty("unreadable", route="docx", reason=MSG_WORD_NO_TABLE)
        used = template_vision.model()
        left = None if deadline is None else max(1.0, deadline - (time.monotonic() - started))
        raw, err = template_vision.read_text(text, deadline=left)
        if err:
            return _empty("error", route="docx_model", model=used, error=err)
        draft = template_draft.clean(raw)
        if draft["counts"]["items"] == 0:
            return _empty("unreadable", route="docx_model", model=used, reason=MSG_NO_ROWS)
        return {"status": "done", **draft, "route": "docx_model", "model": used, "pages": None}

    try:
        pages = [{"body": _read_source(k), "content_type": template_types.EXT_TYPES[e],
                  "name": k.rsplit("/", 1)[-1]} for k, e in zip(keys, exts)]
    except _TooLarge:
        return _empty("error", route="vision", error=MSG_TOO_LARGE)
    why = template_vision.check(pages)
    if why:
        return _empty("unreadable", route="vision", reason=why)
    source.update(kind="pdf" if exts == ["pdf"] else "images", pages=pages)
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
    request, source = {}, {}
    try:
        request = json.loads(_read(key).decode("utf-8"))
        if (not isinstance(request, dict) or str(request.get("companyId")) != company
                or request.get("id") != request_id):
            logger.error("template digitise: %s does not match its own key -- refused", key)
            request = {}  # echo nothing from a request that is not this key's
            result = _empty("error", error=MSG_FAILED)
        else:
            left = None if deadline is None else max(1.0, deadline - (time.monotonic() - started))
            result = digitise(request, deadline=left, source=source)
    except Exception:  # noqa: BLE001 - the person must get an answer, not a pending poll
        logger.exception("template digitise %s failed", request_id)
        result = _empty("error", error=MSG_FAILED)
        source.clear()  # a read that raised part-way: nothing whole to audit against
    result.update({"id": request_id, "companyId": company, "userId": request.get("userId"),
                   "name": request.get("name"), "sourceKeys": request.get("sourceKeys") or [],
                   "finishedAt": datetime.now(timezone.utc).isoformat()})
    lake_put(S3_BUCKET, result_key(company, request_id), json.dumps(result, ensure_ascii=False),
             owner_hint={"company_id": company}, client=s3(), ContentType="application/json")
    logger.info("template digitise %s: %s route=%s sections=%s items=%s", request_id,
                result["status"], result.get("route"), result["counts"]["sections"],
                result["counts"]["items"])
    if os.environ.get("TEMPLATE_DIGITISE_AUDIT", "").strip().lower() == "true":
        try:
            _audit(company, request_id, copy.deepcopy(result), source, context)
        except Exception:  # noqa: BLE001 - the result is written; the audit only reports
            logger.exception("template audit %s failed", request_id)


def _skip_reason(result, source):
    if not source.get("kind"):
        return _SKIP_REASONS.get(result.get("error") or result.get("reason"), "not_read")
    if source["kind"] == "docx" and len(source["text"]) > template_vision.MAX_TEXT_CHARS:
        return "too_long"  # the reader was not sent it either (R4.5)
    return None


def _audit(company, request_id, result, source, context):
    """Audit one written result (spec 2026-11-06 A1-A5): store the verdict, log it, and
    publish `problems` / `missed` to the alert topic."""
    left = None
    if context is not None:
        left = context.get_remaining_time_in_millis() / 1000.0 - WRITE_RESERVE_SECONDS
    if source.get("kind") == "docx" and source.get("text") is None:
        # A rule read never rendered the file; the auditor sees the rendering only (A2).
        source["text"] = template_docx.render_text(BytesIO(source["body"]))
    base = {"id": request_id, "companyId": company, "name": result.get("name"),
            "userId": result.get("userId"), "sourceKeys": result.get("sourceKeys") or [],
            "route": result.get("route"), "status": result.get("status"),
            "counts": result.get("counts"), "readModel": result.get("model"),
            "auditedAt": datetime.now(timezone.utc).isoformat()}
    reason = _skip_reason(result, source)
    if reason is None and left is not None and left < AUDIT_MIN_SECONDS:
        logger.info("template audit %s: audit skipped: no time (%.0fs left)", request_id, left)
        reason = "no_time"
    elif reason is not None:
        logger.info("template audit %s: skipped (%s)", request_id, reason)
    if reason is not None:
        # Still a record, so the listing shows every upload.
        _store(company, request_id, {"verdict": "skipped", "reason": reason, **base})
        return
    record = template_audit.run({"kind": source["kind"], "text": source.get("text"),
                                 "pages": source.get("pages")}, result, deadline=left)
    record.update(base)
    key = _store(company, request_id, record)
    logger.info("template audit %s: %s route=%s items=%s source_items=%s missing=%d extra=%d "
                "reworded=%d", request_id, record["verdict"], record["route"],
                (record["counts"] or {}).get("items"), record["source_items"],
                len(record["missing"]), len(record["extra"]), len(record["reworded"]))
    if record["verdict"] in _ALERT_VERDICTS:
        _publish(record, key)


def _store(company, request_id, record):
    key = audit_key(company, request_id)
    lake_put(S3_BUCKET, key, json.dumps(record, ensure_ascii=False),
             owner_hint={"company_id": company}, client=s3(), ContentType="application/json")
    try:
        lake_put(S3_BUCKET, stats_key(company, request_id, record["auditedAt"]),
                 json.dumps(audit_stats(record)), owner_hint={"company_id": company},
                 client=s3(), ContentType="application/json")
    except Exception:  # noqa: BLE001 - the record is stored; the stats line only counts
        logger.exception("template audit %s: could not store the stats line", request_id)
    return key


def _subject(record):
    stage = os.environ.get("STAGE", "").strip() or "unknown"
    text = "[FieldSight %s] Template upload %s: %s" % (
        stage, record["verdict"], record.get("name") or "(no name)")
    # SNS takes an ASCII subject under 100 characters with no line breaks: a customer's
    # file name in any other script would fail the publish, not just look odd.
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return " ".join(text.split())[:99]


def _line(text):
    text = " ".join(str(text).split())
    return text if len(text) <= _MAX_LINE else text[:_MAX_LINE - 3] + "..."


def _body(record, key):
    counts = record.get("counts") or {}
    lines = [
        "Template upload audit: %s" % record["verdict"],
        "Company: %s" % record["companyId"],
        "File: %s" % _line(record.get("name") or "(no name)"),
        "Sources: %s" % ", ".join(record.get("sourceKeys") or []),
        "Read: route=%s status=%s reader model=%s" % (
            record.get("route"), record.get("status"), record.get("readModel")),
        "Audit model: %s (%ss)" % (record.get("model"), record.get("elapsed_s")),
        "Draft: %s items in %s sections; the source has %s (the auditor's count)" % (
            counts.get("items"), counts.get("sections"), record.get("source_items")),
        "Summary: %s" % _line(record.get("summary") or ""),
    ]
    for title, entries, show in (
            ("Missing", record["missing"], lambda e: "[%s] %s" % (e["section"], e["text"])),
            ("Extra", record["extra"], lambda e: "#%s %s -- %s" % (e["n"], e["text"], e["why"])),
            ("Reworded", record["reworded"],
             lambda e: '#%s draft "%s" / source "%s"' % (e["n"], e["draft_text"], e["source_text"])),
            ("Split or merged", record["split_or_merged"], lambda e: "#%s %s" % (e["n"], e["note"]))):
        if entries:
            lines.append("")
            lines.append("%s (%d):" % (title, len(entries)))
            lines.extend("  - " + _line(show(e)) for e in entries[:template_audit.MAX_FINDINGS])
    lines += ["", "Claims dropped by the code checks: %d" % len(record.get("dropped_claims") or []),
              "Audit record: s3://%s/%s" % (S3_BUCKET, key)]
    return "\n".join(lines)


def _publish(record, key):
    topic = os.environ.get("ALERT_TOPIC_ARN", "").strip()
    if not topic:
        logger.info("template audit %s: %s, no topic -- not emailed", record["id"], record["verdict"])
        return
    try:
        sns().publish(TopicArn=topic, Subject=_subject(record), Message=_body(record, key))
    except Exception:  # noqa: BLE001 - the record is stored; a lost email is only logged
        logger.exception("template audit %s: could not publish to the alert topic", record["id"])


def lambda_handler(event, context):
    for rec in event.get("Records", []):
        key = ((rec.get("s3") or {}).get("object") or {}).get("key")
        if key:
            _process(unquote_plus(key), context)
    return {"ok": True}
