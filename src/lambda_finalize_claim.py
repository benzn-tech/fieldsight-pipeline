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
# Asks the (non-VPC) extraction lambda for this session's FINAL, thinking-mode
# pass. Same artifact-on-S3 channel as FINALIZE_REQUESTS_PREFIX and for the same
# reason: this sweep is in-VPC and cannot invoke another lambda (no NAT, no
# lambda VPC endpoint -> the call black-holes until timeout, CLAUDE.md BUG-36),
# but it CAN write to S3 through the gateway endpoint.
# Must stay identical to lambda_extract_session.FINAL_REQUESTS_PREFIX.
EXTRACTION_REQUESTS_PREFIX = "extraction_requests/"
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
# Phase C multi-device merge. Off means the standing scan never runs, so no
# group is ever claimed and every downstream branch stays inert.
ENABLE_GROUP_MERGE = os.environ.get("ENABLE_GROUP_MERGE", "false").lower() == "true"
# The longest a group may span before it is refused as stale. A device that kept
# a group overnight presents it again the next day and nothing on the device
# objects — merging yesterday into today reads perfectly fluently, which is why
# the guard is unconditional. Measured on opened_at, the SERVER's timestamp:
# these ROMs have been seen 12 hours out (BUG-37).
GROUP_MAX_SPAN_SECONDS = int(os.environ.get("GROUP_MAX_SPAN_SECONDS", "43200"))


def finalize_claim(conn, session_id, expected_version, *, resolve_context, read_rolling,
                   enqueue, request_extraction=None):
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

    # Requested BEFORE the recipient check on purpose: the final extraction is
    # what puts this session's content on the WEBSITE, which a recorder with no
    # email on file needs just as much. Best-effort — a failure here must never
    # block finalize — but never silent (CLAUDE.md BUG-40: a swallowed except is
    # how a whole feature stays broken unnoticed).
    if request_extraction and ctx.get("folder") and ctx.get("date"):
        try:
            request_extraction(session_id, ctx["folder"], ctx["date"])
        except Exception:
            logger.exception("finalize: could not request final extraction for %s",
                             session_id)

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
        "timeRange": ctx.get("timeRange"),
        "siteName": ctx.get("siteName"),
        "summary": rolling.get("summary", ""),
        "openTodos": rolling.get("open_todos", []),
    }
    enqueue(artifact)
    return {"status": "enqueued", "sessionId": session_id, "recipient": recipient}


# ----- real (Aurora + S3) collaborators the handler wires -----------------

def _to_nz(dt):
    """meeting_session opened_at/closed_at are stored in UTC (the device's /open
    sends a UTC startedAt), but the S3 transcript/rolling keys and the topic times
    are the device's NZ wall clock. Return `dt` as Pacific/Auckland LOCAL (naive),
    DST-aware, so (a) the email subject shows NZ time and (b) `.date()` matches the
    device-date the transcripts live under (a morning-NZ recording is the PREVIOUS
    UTC day — using the UTC date gathered the wrong day and produced "No summary").

    Prefer the IANA tz database (exact to the hour); if the runtime lacks tzdata,
    fall back to NZ's fixed rule: NZDT (UTC+13) from the last Sunday of September to
    the first Sunday of April, else NZST (UTC+12)."""
    from datetime import datetime, timedelta, timezone
    if not hasattr(dt, "strftime"):
        return None
    aware = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    try:
        from zoneinfo import ZoneInfo
        return aware.astimezone(ZoneInfo("Pacific/Auckland")).replace(tzinfo=None)
    except Exception:
        import calendar
        u = aware.astimezone(timezone.utc).replace(tzinfo=None)
        y = u.year

        def _last_sun(mo):
            d = datetime(y, mo, calendar.monthrange(y, mo)[1])
            return d - timedelta(days=(d.weekday() - 6) % 7)

        def _first_sun(mo):
            d = datetime(y, mo, 1)
            return d + timedelta(days=(6 - d.weekday()) % 7)

        dst_start = _last_sun(9) + timedelta(hours=2) - timedelta(hours=12)   # -> UTC
        dst_end = _first_sun(4) + timedelta(hours=3) - timedelta(hours=13)    # -> UTC
        return u + timedelta(hours=13 if (u >= dst_start or u < dst_end) else 12)


def _resolve_context(conn, row):
    """Recipient email + folder + date + site name for a claimed session, from
    Aurora. date = the session's close (or open) day; siteName from the site pick."""
    user = users.get_by_id(conn, row["user_id"]) or {}
    # Work in NZ local time (opened_at/closed_at are UTC): the DATE must be the
    # device's NZ day — that is the {date} the S3 transcript/rolling keys live under,
    # so the summary re-gather finds them (using the UTC day mis-keyed morning-NZ
    # recordings, which are the PREVIOUS UTC day, and yielded "No summary").
    opened_nz, closed_nz = _to_nz(row.get("opened_at")), _to_nz(row.get("closed_at"))
    day = opened_nz or closed_nz
    date = day.date().isoformat() if day else None
    site_name = None
    if row.get("site_id"):
        site = sites.get_site(conn, row["site_id"])
        site_name = (site or {}).get("name")
    # Meeting time window for the email subject — "which meeting was this?". NZ
    # wall-clock HH:MM of open→close (start only if the close time is unknown).
    def _hm(dt):
        return dt.strftime("%H:%M") if dt else None
    start_hm, end_hm = _hm(opened_nz), _hm(closed_nz)
    time_range = f"{start_hm}–{end_hm}" if start_hm and end_hm else (start_hm or None)
    return {"recipient": user.get("email"), "folder": user.get("folder_name"),
            "date": date, "siteName": site_name, "timeRange": time_range}


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


def _request_extraction(session_id, folder, date):
    """Ask lambda_extract_session for this session's FINAL (thinking-mode,
    unthrottled) pass now that the session has closed.

    Why the sweep owns this: the live passes that ran during recording are
    throttled, so the last window of transcripts can land INSIDE the throttle
    and never make it into an extraction. Only a close-driven pass can
    guarantee the published extraction covers the whole session. `sid`+
    session_id is extract_session's grouping key (same as _read_rolling)."""
    import boto3
    boto3.client("s3").put_object(
        Bucket=S3_BUCKET,
        Key=f"{EXTRACTION_REQUESTS_PREFIX}{session_id}.json",
        Body=json.dumps({"userFolder": folder, "date": date,
                         "sessionBase": f"sid{session_id}"}),
        ContentType="application/json")


def group_merged_key(user_folder, date, group_id):
    """Where the merged extraction is written.

    Deliberately NOT extraction_key(..., "sid"+lead): that is byte-identical to
    the LEAD's own final-pass key, and the final pass writes blind (no supersede
    read). A lead-solo final landing after the merge would overwrite it,
    item-writer would then delete the MERGED topics as that key's previous
    output, and every joiner's content would be gone from Aurora — with the
    members' own topics already deleted. `grp` collides with nothing."""
    return f"extractions/{user_folder}/{date}/grp{group_id}.json"


def _member_context(conn, row, resolve_ctx):
    """One member as the merge request needs it, or None if unresolvable.

    Reuses the finalize path's own context resolver so the DATE comes from the
    same NZ conversion — a UTC date here would name a folder that does not
    exist, and the merge would silently gather nothing (the BUG-37 family that
    already produced "No summary" once)."""
    ctx = resolve_ctx(conn, row) or {}
    if not ctx.get("folder") or not ctx.get("date"):
        return None
    return {"userFolder": ctx["folder"], "date": ctx["date"],
            "sessionBase": "sid" + row["session_id"]}


def sweep_groups(conn, *, list_due, claim, mark_result, span_ok, members_of,
                 resolve_ctx, enqueue):
    """Claim every settled, unmerged group and ask for its merged extraction.

    A STANDING scan, run every tick regardless of whether any session was due.
    That is the whole correction over the first design: on the tick that
    finalizes a group's last member, that member is `finalizing` with a fresh
    last_segment_at, so the group cannot be settled yet; it settles a tick later
    when reconcile marks it `sent`, and by then sweep() has no due session to
    hang a check on. The lead makes it worse — it carries no group_id of its
    own, so a check reading the group off the finalizing row had nothing to look
    up when the lead stopped last.
    """
    results = []
    for row in list_due(conn):
        gid = row["group_id"]
        members = members_of(conn, gid)
        # Both guards the parent spec called unconditional and that Phases A/B
        # never wired. A wrong merge mixes two meetings into one report and
        # sends it to both sets of people — contamination plus disclosure, and
        # hard to notice after the fact, which is why the bias is under-merge.
        companies = {str(m.get("company_id")) for m in members if m.get("company_id")}
        stale = not span_ok(conn, gid)
        if stale or len(companies) > 1:
            mark_result(conn, gid, "rejected")
            logger.warning("group %s rejected: stale=%s companies=%s",
                           gid, stale, sorted(companies))
            results.append({"group_id": gid, "status": "rejected"})
            continue
        contexts, by_base = [], {}
        for m in members:
            c = _member_context(conn, m, resolve_ctx)
            if c:
                contexts.append(c)
                by_base[c["sessionBase"]] = c
        if not contexts:
            # Settled with nothing usable. Marked terminal so it leaves the
            # candidate set instead of being re-read every minute forever.
            mark_result(conn, gid, "empty")
            results.append({"group_id": gid, "status": "empty"})
            continue
        # The artifact takes the LEAD's folder and date. A group can straddle NZ
        # midnight, so members legitimately differ; each member's own date is
        # used for its own key, which is extract-session's job. A lead that
        # never uploaded has no context at all — fall back to a member rather
        # than discard the meeting, since the group id is an identifier, not a
        # claim of authorship.
        lead = by_base.get("sid" + gid) or contexts[0]
        merged_key = group_merged_key(lead["userFolder"], lead["date"], gid)
        if not claim(conn, gid, merged_key):
            results.append({"group_id": gid, "status": "lost-claim"})
            continue
        enqueue({"groupId": gid, "leadSessionId": gid, "mergedKey": merged_key,
                 "members": contexts})
        results.append({"group_id": gid, "status": "claimed"})
    return results


def _sweep_groups_contained(conn, scan=None):
    """The group scan, unable to take the tick's real work down with it.

    sweep, reconcile and this share ONE transaction. Without containment a group
    scan that raises rolls the whole tick back -- and reconcile is what moves a
    session to `sent`, so a scan that failed every tick would present as PROD
    EMAILS STOPPING, with a feature flag as the only thing standing in the way.
    Merging is a convenience; delivering the email is not.

    The savepoint is not decoration. A raised exception poisons the entire
    psycopg transaction, so catching it WITHOUT a savepoint leaves the
    connection in a failed state and every later statement in the tick fails
    anyway -- the same lesson as the recurring-item proposal pass.

    Never silent: a merge that stops happening is otherwise invisible, since
    the sweep logs only when it did something.

    This is the ONLY flag gate for the group scan. An earlier version had a
    separate `sweep_groups_if_enabled` as well; two gates for one flag is an
    invitation to change one of them.
    """
    if not ENABLE_GROUP_MERGE:
        return []
    try:
        with conn.transaction():      # savepoint: roll back to here, keep conn usable
            return (scan or _real_group_scan)(conn)
    except Exception:
        logger.exception("group sweep failed -- finalize continues and the "
                         "merge is retried next tick")
        return []


def _real_group_scan(conn):
    from repositories import session_group
    return sweep_groups(
        conn,
        list_due=lambda c: session_group.list_due(c, SESSION_GAP_MINUTES * 60),
        claim=session_group.claim,
        mark_result=session_group.mark_result,
        span_ok=lambda c, gid: meeting_session.group_span_ok(
            c, gid, GROUP_MAX_SPAN_SECONDS),
        members_of=meeting_session.list_group_members,
        resolve_ctx=_resolve_context,
        enqueue=_enqueue_group)


def _enqueue_group(artifact):
    """Ask extract-session to merge this group.

    Same S3-artifact crossing as every other in-VPC -> non-VPC hop: this sweep
    cannot invoke a lambda (BUG-36 — no NAT, the call black-holes until
    timeout), but it can write to S3 through the gateway endpoint. The `group-`
    prefix is what routes it away from the solo final-pass path."""
    import boto3
    boto3.client("s3").put_object(
        Bucket=S3_BUCKET,
        Key=f"{EXTRACTION_REQUESTS_PREFIX}group-{artifact['groupId']}.json",
        Body=json.dumps(artifact, ensure_ascii=False),
        ContentType="application/json")


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
            resolve_context=_resolve_context, read_rolling=_read_rolling, enqueue=_enqueue,
            request_extraction=_request_extraction))
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
        # AFTER reconcile, deliberately. reconcile is what moves a claimed
        # session to `sent`, and `sent` is what makes its group settled — so
        # running the group scan first would always be one tick behind.
        groups = _sweep_groups_contained(conn)
    # Observability: this fires every minute, so log ONLY when it did something
    # (no per-idle-minute noise). enqueued = sessions handed to the send worker;
    # reconciled = sessions moved to sent/failed this tick.
    if swept or reconciled or groups:
        logger.info("finalize sweep: enqueued=%d reconciled=%d groups=%d",
                    len(swept), len(reconciled), len(groups))
    return {"swept": len(swept), "reconciled": len(reconciled),
            "groups": len(groups)}
