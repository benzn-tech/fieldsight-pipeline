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
from io import BytesIO
from urllib.parse import unquote_plus

import boto3

from lambda_meeting_minutes import generate_word_document
from email_sender import get_sender

logger = logging.getLogger()
logger.setLevel(logging.INFO)

S3_BUCKET = os.environ.get("S3_BUCKET", "")
DOCX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# Photo evidence budgets. Two caps, because either one alone leaves a hole: a
# per-topic cap does not bound a forty-topic day, and a total budget alone lets
# one photo-heavy topic eat everything before the later topics are reached.
# The numbers keep the doc something a site manager actually opens on a phone,
# and keep the render inside the Lambda's memory.
MAX_PHOTOS_PER_TOPIC = 4
MAX_PHOTO_BYTES_TOTAL = 12 * 1024 * 1024

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


def _fetch_photos(folder, date, filenames, budget):
    """Download a topic's photos as open streams, newest failure tolerated.

    The renderer does no I/O and must stay that way, so the bytes are fetched
    here and handed over. `budget` is a one-element list carrying the REMAINING
    total allowance, mutated as it is spent — the caller walks the topics in
    order, so an early photo-heavy topic cannot silently starve a later one of
    its cap, only of the shared budget.

    A photo that cannot be read costs only itself. The prose is the
    deliverable; the pictures support it, and losing the report because one
    object was deleted would be the wrong trade."""
    streams = []
    for name in (filenames or [])[:MAX_PHOTOS_PER_TOPIC]:
        if budget[0] <= 0:
            logger.info("photo budget spent; skipping %s", name)
            break
        key = f"users/{folder}/pictures/{date}/{name}"
        try:
            body = s3().get_object(Bucket=S3_BUCKET, Key=key)["Body"].read()
        except Exception:
            logger.warning("could not read photo %s; leaving it out", key)
            continue
        streams.append(BytesIO(body))
        budget[0] -= len(body)
    return streams


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

    folder, date = artifact.get("folder"), content.get("date") or artifact.get("date")
    budget = [MAX_PHOTO_BYTES_TOTAL]

    topics = []
    for t in (content.get("topics") or []):
        photos = _fetch_photos(folder, date, t.get("related_photos"), budget)
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
            # Absent, not empty, when there is nothing — so the renderer's
            # `if topic.get('photo_streams')` needs no second check.
            **({"photo_streams": photos} if photos else {}),
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


def _session_was_deleted(artifact):
    """Is this session in the day's deletion mirror? Never raises.

    Sibling of `lambda_session_finalize._session_was_deleted`, and lenient for
    the same reason: an unreadable mirror must not cost a requester the report
    they asked for, and this worker RECORDS errors rather than retrying them.
    The strict counterpart is `lambda_org_api._session_was_removed`, which backs
    read endpoints where a failed check costs one reader one refresh -- see its
    docstring for why the two postures are deliberate and must not be merged.

    Both spellings are compared: the mirror carries whatever `sessionBase` the
    delete endpoint had, and this artifact's `sessionId` is bare hex.

    Logged on failure, because a permission fault here looks exactly like
    "nothing was deleted".
    """
    folder, date = artifact.get("folder"), artifact.get("date")
    sid = (artifact.get("sessionId") or "").strip()
    if not (folder and date and sid):
        return False
    try:
        import boto3

        import deletion_mirror
        deleted = deletion_mirror.deleted_sessions(
            boto3.client("s3"), S3_BUCKET, folder, date)
    except Exception:
        logger.exception("report: deletion mirror unreadable for %s/%s -- proceeding as "
                         "if nothing was deleted, which may mail a removed recording",
                         folder, date)
        return False
    return sid in deleted or f"sid{sid}" in deleted


def process_request(artifact):
    """Render one enqueued request → Word doc (+ optional email) → result JSON."""
    result_key = artifact["resultKey"]
    request_id = artifact.get("requestId")

    # A session deleted before this request is rendered must not become a DOCX in
    # S3, and must not be emailed.
    #
    # `GET /report/status` stops the POLL from serving a removed session, but this
    # worker is S3-triggered: a delete landing between org-api's enqueue and this
    # run -- or an event redelivery hours later -- reaches neither guard. It is
    # the same defect fixed in `lambda_session_finalize` one lambda over, and it
    # was the last surface in the deletion enumeration still carrying it.
    #
    # Checked BEFORE the render, so nothing is produced from content that must
    # not leave. The outcome is RECORDED because the requester polls `resultKey`;
    # a silent skip leaves that poll spinning forever, which is a different bug
    # wearing this fix's clothes.
    if _session_was_deleted(artifact):
        logger.info("report: %s was deleted -- not rendering, not sending", request_id)
        _write_result(result_key, {"status": "skipped", "requestId": request_id,
                                   "reason": "recording deleted"})
        return

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
