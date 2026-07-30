"""lambda_finalize_claim.py — the in-VPC grace-timer target for Tier-0 finalize.

When a recording's grace window elapses, the EventBridge one-shot armed in
`session_close` fires this with the session_id + the `version` it was scheduled
against. It CAS-claims the session (repositories.meeting_session.claim_finalize:
pending_close -> finalizing ONLY if the version is unchanged) — the idempotency
guard: a mis-touch stop->resume bumps `version`, so a stale one-shot no-ops here.

When claimed it gathers, in-VPC, the recipient + folder/date/site (Aurora) and the
rolling summary (S3, written by the Tier-1 lambda), and enqueues a request under
session_finalize_requests/; the non-VPC worker (lambda_session_finalize) builds +
SES-sends the confirmation email — this in-VPC step can't reach SES itself
(CLAUDE.md BUG-36), same hand-off shape as the session-report worker.

The orchestration (`finalize_claim`) takes injected collaborators so it's unit
testable without a DB/S3; `lambda_handler` supplies the real ones.
"""
import json
import logging
import os

from repositories import meeting_session, users, sites

logger = logging.getLogger()
logger.setLevel(logging.INFO)

S3_BUCKET = os.environ.get("S3_BUCKET", "")
FINALIZE_REQUESTS_PREFIX = "session_finalize_requests/"


def finalize_claim(conn, session_id, expected_version, *, resolve_context, read_rolling, enqueue):
    """CAS-claim the session at expected_version, then gather + enqueue a finalize
    request. Returns a small status dict:
      noop         — claim failed (a resume bumped version, or it already moved on)
      no_recipient — claimed but the recorder has no email (session marked failed)
      enqueued     — request written for the non-VPC send worker
    Collaborators are injected: resolve_context(conn, row) -> {recipient, folder,
    date, siteName}; read_rolling(folder, date, session_id) -> {summary, open_todos};
    enqueue(artifact)."""
    row = meeting_session.claim_finalize(conn, session_id, expected_version)
    if row is None:
        return {"status": "noop", "sessionId": session_id}
    ctx = resolve_context(conn, row) or {}
    recipient = (ctx.get("recipient") or "").strip()
    if not recipient:
        meeting_session.mark_failed(conn, session_id)
        return {"status": "no_recipient", "sessionId": session_id}
    rolling = read_rolling(ctx.get("folder"), ctx.get("date"), session_id) or {}
    artifact = {
        "sessionId": session_id,
        "version": expected_version,
        "recipient": recipient,
        "folder": ctx.get("folder"),
        "date": ctx.get("date"),
        "siteName": ctx.get("siteName"),
        "summary": rolling.get("summary", ""),
        "openTodos": rolling.get("open_todos", []),
    }
    enqueue(artifact)
    return {"status": "enqueued", "sessionId": session_id, "recipient": recipient}


# ----- real (Aurora + S3) collaborators the handler wires -----------------

def _resolve_context(conn, row):
    """Recipient email + folder + date + site name for a claimed session, from
    Aurora. date = the session's close (or open) day; siteName from the site pick."""
    user = users.get_by_id(conn, row["user_id"]) or {}
    day = row.get("closed_at") or row.get("opened_at")
    date = day.date().isoformat() if hasattr(day, "date") else (str(day)[:10] if day else None)
    site_name = None
    if row.get("site_id"):
        site = sites.get_site(conn, row["site_id"])
        site_name = (site or {}).get("name")
    return {"recipient": user.get("email"), "folder": user.get("folder_name"),
            "date": date, "siteName": site_name}


def _read_rolling(folder, date, session_id):
    """The Tier-1 rolling summary the rolling lambda wrote to S3, or {} if none.
    Its session_base is `sid`+session_id (extract_session's grouping key)."""
    if not folder or not date:
        return {}
    import boto3
    key = f"session_rolling/{folder}/{date}/sid{session_id}/latest.json"
    try:
        obj = boto3.client("s3").get_object(Bucket=S3_BUCKET, Key=key)
        return json.loads(obj["Body"].read().decode("utf-8"))
    except Exception:
        return {}


def _enqueue(artifact):
    """Write the finalize request for the non-VPC send worker to pick up."""
    import boto3
    key = f"{FINALIZE_REQUESTS_PREFIX}{artifact['sessionId']}.json"
    boto3.client("s3").put_object(
        Bucket=S3_BUCKET, Key=key,
        Body=json.dumps(artifact, ensure_ascii=False), ContentType="application/json")


def lambda_handler(event, context):
    """EventBridge one-shot target. The schedule's input carries {sessionId,
    version}. Opens an Aurora connection (in-VPC) and runs the claim."""
    session_id = event.get("sessionId")
    version = event.get("version")
    if not session_id or version is None:
        logger.warning("finalize claim: missing sessionId/version in event: %r", event)
        return {"status": "bad_event"}
    from db.connection import get_connection
    with get_connection() as conn:
        return finalize_claim(conn, session_id, int(version),
                              resolve_context=_resolve_context,
                              read_rolling=_read_rolling, enqueue=_enqueue)
