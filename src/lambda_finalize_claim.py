"""lambda_finalize_claim.py — the in-VPC grace SWEEP for Tier-0 finalize.

A scheduled EventBridge rule (~1 min) invokes this; it finds every session whose
grace window has elapsed (meeting_session.list_due_finalize: still `pending_close`,
and either a deliberate End — grace 0 — or an idle stop older than the grace window)
and finalizes each. A scheduled invoke is INBOUND, so an in-VPC lambda is fine — the
outbound SES send is the non-VPC worker's job. A sweep (not a per-session one-shot)
because session_close is in org-api which, in-VPC, can reach S3 + Aurora but NOT the
EventBridge Scheduler API to arm a timer (CLAUDE.md BUG-36).

For each due session it CAS-claims (meeting_session.claim_finalize: pending_close ->
finalizing ONLY if `version` is unchanged) — the idempotency guard: a mis-touch
stop->resume bumps `version`, so a session that resumed between the sweep's query and
its claim simply no-ops. When claimed it gathers, in-VPC, the recipient + folder/
date/site (Aurora) and the rolling summary (S3, written by the Tier-1 lambda), and
enqueues a request under session_finalize_requests/; the non-VPC worker
(lambda_session_finalize) builds + SES-sends the confirmation email.

finalize_claim / sweep take injected collaborators so they're unit testable without
a DB/S3; lambda_handler supplies the real ones.
"""
import json
import logging
import os

from repositories import meeting_session, users, sites
from session_scope import SESSION_GAP_MINUTES

logger = logging.getLogger()
logger.setLevel(logging.INFO)

S3_BUCKET = os.environ.get("S3_BUCKET", "")
FINALIZE_REQUESTS_PREFIX = "session_finalize_requests/"
# The idle-stop grace window (a deliberate End is grace 0). Mirrors org-api's
# STOP_GRACE_SECONDS so the sweep's due-check matches what session_close intended.
STOP_GRACE_SECONDS = int(os.environ.get("STOP_GRACE_SECONDS", "30"))
# §8.4: a session with no new chunk for SESSION_GAP_MINUTES is INFERRED closed
# (the device stopped/crashed without a /close). Reuses the one "one session"
# inactivity constant rather than a second literal.
IDLE_CLOSE_SECONDS = SESSION_GAP_MINUTES * 60
# Gated OFF until SessionActivityFunction is live to keep last_segment_at fresh:
# without per-chunk touch, a still-active /open session (last_segment_at NULL ->
# COALESCE falls back to opened_at) would be closed prematurely. Flipped on WITH
# that lambda's deploy.
INFER_IDLE_CLOSE = os.environ.get("INFER_IDLE_CLOSE", "false").lower() == "true"


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
        # Named so prod ops can see WHICH recorder has no email on file (a
        # device-mapping identity, or a real user missing an email) instead of a
        # silent count — this recording gets no confirmation email.
        logger.warning("finalize: no recipient email for session %s (folder=%s) — "
                       "marking failed, no email sent", session_id, ctx.get("folder"))
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
    # DATE = the session's recording start (opened_at, device wall-clock) — this is
    # the {date} the S3 transcript/rolling keys live under. Prefer it over closed_at,
    # which for an INFERRED idle-close is a server-time last-activity that could
    # cross a day boundary and mis-key the summary re-gather.
    day = row.get("opened_at") or row.get("closed_at")
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


def infer_idle_closes(conn, idle_seconds):
    """§8.4 close inference: an `open` session with no new chunk for idle_seconds is
    treated as closed (the device stopped/crashed without a /close). Move each to
    pending_close (intent 'idle'), anchored at its last activity, so the existing
    grace + finalize path emails it. Returns the count moved. Keys on last_segment_at
    (list_idle_open) so a still-active meeting is never closed early."""
    closed = 0
    for row in meeting_session.list_idle_open(conn, idle_seconds):
        if meeting_session.mark_pending_close(conn, row["session_id"], row["last_activity"], "idle"):
            closed += 1
    return closed


def sweep(conn, grace_seconds=None, idle_seconds=None, infer_idle=None):
    """Finalize every session on `conn` whose grace has elapsed, reusing
    finalize_claim per session (CAS-claim -> gather -> enqueue). First (when
    enabled) infers idle closes (§8.4): an `open` session gone quiet becomes
    pending_close and is then picked up by the same due-check. Returns the list of
    per-session status dicts. Separated from lambda_handler so the sweep loop is
    testable without a real DB connection; `infer_idle` overrides the env gate."""
    grace = STOP_GRACE_SECONDS if grace_seconds is None else grace_seconds
    idle = IDLE_CLOSE_SECONDS if idle_seconds is None else idle_seconds
    do_infer = INFER_IDLE_CLOSE if infer_idle is None else infer_idle
    if do_infer:
        infer_idle_closes(conn, idle)
    results = []
    for row in meeting_session.list_due_finalize(conn, grace):
        results.append(finalize_claim(
            conn, row["session_id"], row["version"],
            resolve_context=_resolve_context, read_rolling=_read_rolling, enqueue=_enqueue))
    return results


def reconcile(conn, read_result):
    """Move claimed ('finalizing') sessions to their terminal state once the non-VPC
    send worker has recorded an outcome under session_finalize_results/: sent ->
    mark_sent, error -> mark_failed, no result yet -> leave for a later tick. Runs in
    the same in-VPC sweep because the worker can't touch Aurora itself (BUG-36).
    read_result(session_id) -> the worker's outcome dict or None. Returns
    [(session_id, state)]."""
    out = []
    for row in meeting_session.list_finalizing(conn):
        sid = row["session_id"]
        res = read_result(sid)
        if not res:
            continue
        status = res.get("status")
        if status == "sent":
            meeting_session.mark_sent(conn, sid)
            out.append((sid, "sent"))
        elif status == "error":
            meeting_session.mark_failed(conn, sid)
            out.append((sid, "failed"))
    return out


def _read_result(session_id):
    """The send worker's recorded outcome for a session, or None if not written yet."""
    import boto3
    key = f"session_finalize_results/{session_id}.json"
    try:
        obj = boto3.client("s3").get_object(Bucket=S3_BUCKET, Key=key)
        return json.loads(obj["Body"].read().decode("utf-8"))
    except Exception:
        return None


def lambda_handler(event, context):
    """Scheduled (~1 min) grace sweep + reconcile — see the module docstring. Opens an
    in-VPC Aurora connection, finalizes every due session, and moves already-sent
    claimed sessions to their terminal state."""
    from db.connection import get_connection
    with get_connection() as conn:
        swept = sweep(conn)
        reconciled = reconcile(conn, _read_result)
    # Observability: this fires every minute, so log ONLY when it did something
    # (no per-idle-minute noise). enqueued = sessions handed to the send worker;
    # reconciled = sessions moved to sent/failed this tick.
    if swept or reconciled:
        logger.info("finalize sweep: enqueued=%d reconciled=%d", len(swept), len(reconciled))
    return {"swept": len(swept), "reconciled": len(reconciled)}
