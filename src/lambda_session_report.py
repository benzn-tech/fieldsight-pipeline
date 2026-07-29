"""lambda_session_report.py — Delivery-C generate worker (Tier-2 T3).

Non-VPC (has the python-docx layer + can reach SES). Triggered by an org-api
enqueue writing `session_report_requests/{...}.json`: org-api is in-VPC and so
can neither render docx nor reach SES (CLAUDE.md BUG-36), so it hands the work
off via an S3 request artifact (the match_requests/ pattern). This worker reads
the artifact, renders the reviewed session content into a Word doc (reusing
`lambda_meeting_minutes.generate_word_document`), writes it under
`session_reports/`, optionally emails it, and writes the result the frontend
polls for at the artifact's `resultKey`.

Design: docs/superpowers/specs/2026-07-28-session-report-review-export-design.md §6.
"""
import json
import logging
import os
from urllib.parse import unquote_plus

import boto3

from lambda_meeting_minutes import generate_word_document
from email_sender import get_sender

logger = logging.getLogger()
logger.setLevel(logging.INFO)

S3_BUCKET = os.environ.get("S3_BUCKET", "")
DOCX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

_s3_client = None


def s3():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3")
    return _s3_client


def _doc_key(artifact):
    """The Word doc lives under a DEDICATED session_reports/ prefix — NOT the
    nightly meeting_minutes/ path — so this on-demand Delivery-C artifact never
    collides with the report-generator's manifest machinery (BUG-18 sidestepped;
    authority-flip already de-dupes the nightly path)."""
    return (f"session_reports/{artifact['folder']}/{artifact['date']}/"
            f"{artifact['sessionId']}/{artifact['requestId']}.docx")


def _humanize(key):
    return str(key).replace("_", " ").title()


def _content_to_minutes(artifact):
    """Map the reviewed session content + the user's confirmed fields into the
    `minutes_data` shape `generate_word_document` consumes (T4).

    The fixed meeting-minutes layout has no arbitrary-placeholder slots (a real
    per-company template engine is the fast-follow, once the template store moves
    server-side — spec §2/§10.6), so the user's declared fields render generically
    as labeled Executive-Summary lines: any field the modal collects shows up with
    zero per-field code (spec §5). Our topic shape (`render_report_shape`) maps to
    the doc's shape 1:1 except action items' `responsible` -> `owner`."""
    content = artifact.get("content") or {}
    fields = artifact.get("fields") or {}

    topics = []
    for t in (content.get("topics") or []):
        topics.append({
            "topic_title": t.get("topic_title"),
            "category": t.get("category") or "general",
            "time_range": t.get("time_range") or "",
            "participants": t.get("participants") or [],
            "summary": t.get("summary") or "",
            "key_decisions": t.get("key_decisions") or [],
            "action_items": [{"action": a.get("action"),
                              "owner": a.get("responsible"),      # our shape -> the doc's shape
                              "deadline": a.get("deadline"),
                              "priority": a.get("priority") or "medium"}
                             for a in (t.get("action_items") or [])],
            "open_questions": [],
        })

    minutes = {
        "meeting_date": content.get("date"),
        "attendees": artifact.get("attendees") or content.get("participants") or [],
        "topics": topics,
    }
    exec_summary = [f"{_humanize(k)}: {v}" for k, v in fields.items()
                    if v not in (None, "", [], {})]
    if exec_summary:
        minutes["executive_summary"] = exec_summary

    title = artifact.get("title") or content.get("title") or "Session report"
    return minutes, title


def _write_result(result_key, payload):
    s3().put_object(Bucket=S3_BUCKET, Key=result_key,
                    Body=json.dumps(payload), ContentType="application/json")


def _send_email(artifact):
    recipients = artifact.get("recipients") or []
    title = artifact.get("title") or "Site report"
    subject = f"FieldSight report — {title}"
    body_text = (f"The report \"{title}\" for {artifact.get('date', '')} is ready in FieldSight.")
    sender = get_sender()
    for to in recipients:
        sender.send(to, subject, body_text)


def process_request(artifact):
    """Render one enqueued request → Word doc (+ optional email) → result JSON."""
    result_key = artifact["resultKey"]
    request_id = artifact.get("requestId")
    try:
        minutes, title = _content_to_minutes(artifact)
        buf = generate_word_document(minutes, title)
        if buf is None:
            # DOCX layer missing / disabled — record it, don't crash the trigger.
            _write_result(result_key, {"status": "error", "requestId": request_id,
                                       "error": "document generation unavailable"})
            return
        doc_key = _doc_key(artifact)
        s3().put_object(Bucket=S3_BUCKET, Key=doc_key,
                        Body=buf.getvalue(), ContentType=DOCX_CONTENT_TYPE)
        emailed = False
        if artifact.get("deliver") == "email":
            _send_email(artifact)
            emailed = True
        _write_result(result_key, {"status": "done", "requestId": request_id,
                                   "docKey": doc_key, "emailed": emailed})
    except Exception as e:
        logger.exception("session report generation failed for %s", request_id)
        _write_result(result_key, {"status": "error", "requestId": request_id, "error": str(e)})


def lambda_handler(event, context):
    for rec in event.get("Records", []):
        s3rec = rec.get("s3") or {}
        key = (s3rec.get("object") or {}).get("key")
        bucket = (s3rec.get("bucket") or {}).get("name") or S3_BUCKET
        if not key:
            continue
        key = unquote_plus(key)          # S3 notifications URL-encode the key
        obj = s3().get_object(Bucket=bucket, Key=key)
        artifact = json.loads(obj["Body"].read().decode("utf-8"))
        process_request(artifact)
    return {"ok": True}
