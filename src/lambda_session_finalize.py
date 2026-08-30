"""lambda_session_finalize.py — Tier-0 auto-finalize: the ≤2-min post-meeting
confirmation email (voice-timeliness spec 2026-07-27, §Tier-0).

When a recording's grace window elapses without a resume (state machine
open→pending_close→finalizing→sent, repositories/meeting_session.py), the recorder
gets a short email summarising what was captured — so they can confirm or correct
it before leaving site.

Split (CLAUDE.md BUG-36: in-VPC lambdas can't reach SES): an in-VPC CLAIM step —
fired by the grace one-shot — does the version-CAS `claim_finalize` (the idempotency
guard: a mis-touch stop→resume bumps `version`, so the scheduled finalize no-ops)
and enqueues a request; a non-VPC WORKER builds this email from the session's rolling
summary + still-open to-dos and sends it via SES (email_sender). This module holds
the pure email builder now; the claim step, the worker, and the one-shot wiring land
in following slices. Pure at import — boto3 / email_sender are imported lazily by the
worker, never at module load.
"""
import html as _html
import json
import logging
import os
from urllib.parse import unquote_plus

logger = logging.getLogger()

S3_BUCKET = os.environ.get("S3_BUCKET", "")

# Which summariser the finalize re-summary uses. Off = the terse rolling
# summariser this has always used. On = session_brief, which writes the
# narrative first and derives the to-dos from it; it returns the same
# {summary, open_todos} keys, so the email below is unchanged either way.
SESSION_BRIEF = os.environ.get("SESSION_BRIEF", "false").lower() == "true"
BRIEF_PREFIX = "session_brief/"
FINALIZE_RESULTS_PREFIX = "session_finalize_results/"


def _clean_todos(open_todos):
    """Keep only to-dos with real text; normalise responsible + due to a value or
    None. Each item carries {text, responsible, due} for the structured render."""
    out = []
    for t in (open_todos or []):
        text = (t.get("text") or "").strip()
        if text:
            out.append({"text": text,
                        "responsible": (t.get("responsible") or None),
                        "due": (t.get("due") or None),
                        # Absent from the rolling summariser and present in a brief. Kept
                        # optional rather than required so the same cleaner serves both, and
                        # so a brief whose model omitted it degrades to today's behaviour
                        # instead of dropping the item.
                        "why": (t.get("why") or None),
                        "at": (t.get("at") or None)})
    return out


def build_confirmation_email(*, date=None, time_range=None, site_name=None,
                             summary=None, open_todos=None):
    """(subject, body_text, body_html) for the recorder's confirmation email, built
    from the session's still-open to-dos. Pure — no I/O. To-dos with no text are
    dropped; all HTML content is escaped so transcript text can't inject markup. The
    subject carries date + meeting time range so the recorder can tell WHICH meeting
    it is (multiple recordings a day otherwise share one subject), and the same stamp
    repeats on the body's Date line so the email states WHEN without relying on the
    client showing the subject.

    2026-08-10: the narrative summary is NO LONGER RENDERED. What the recorder acts
    on is the action table; the prose restated it at length and pushed the table
    below the fold. `summary` is still accepted — process_finalize_request still
    chooses between the fresh and the rolling one, and it still reaches the stored
    result — it simply does not appear in the email. A session with no to-dos now
    says so explicitly: with the prose gone it would otherwise be a header and
    nothing else, which reads as a broken send."""
    stamp = " ".join(p for p in (date, time_range) if p)
    subject = "FieldSight — your site notes" + (f" ({stamp})" if stamp else "")
    todos = _clean_todos(open_todos)
    no_todos_note = "No action items were captured for this recording."

    lines = ["Here's what we captured from your recording — reply or open FieldSight "
             "to correct anything before you leave site.", ""]
    if site_name:
        lines.append(f"Site: {site_name}")
    if stamp:
        lines.append(f"Date: {stamp}")
    if todos:
        lines += ["", "Action items"]
        for t in todos:
            who = t["responsible"] or "Unassigned"
            due = f" (due {t['due']})" if t["due"] else ""
            lines.append(f"  • {t['text']} — {who}{due}")
            # The line that makes the list readable a day later. The title is written to
            # survive truncation, so it identifies the task and cannot also say why it
            # exists; without this the reader goes back to the timeline and opens the topic.
            # Indented under its item rather than appended to it, so scanning the titles
            # still works and the context is there when the eye stops.
            if t.get("why"):
                lines.append(f"      {t['why']}")
    else:
        lines += ["", no_todos_note]
    body_text = "\n".join(lines).rstrip() + "\n"

    esc = _html.escape
    parts = ["<p>Here's what we captured from your recording — reply or open FieldSight "
             "to correct anything before you leave site.</p>"]
    meta = []
    if site_name:
        meta.append(f"<strong>Site:</strong> {esc(site_name)}")
    if stamp:
        meta.append(f"<strong>Date:</strong> {esc(stamp)}")
    if meta:
        parts.append("<p>" + "<br>".join(meta) + "</p>")
    if todos:
        def _row(t):
            # `why` under the title inside the SAME cell, not a fourth column. A column
            # would be empty for every to-do the rolling summariser produces and for any
            # brief whose model omitted it, and an empty column reads as missing data
            # rather than as an absent explanation.
            why = (f'<div style="color:#666;font-size:13px;padding-top:2px">'
                   f'{esc(t["why"])}</div>') if t.get("why") else ""
            return ("<tr>"
                    f'<td style="padding:6px;border-bottom:1px solid #eee">'
                    f'{esc(t["text"])}{why}</td>'
                    f'<td style="padding:6px;border-bottom:1px solid #eee;'
                    f'vertical-align:top">'
                    f'{esc(t["responsible"]) if t["responsible"] else "—"}</td>'
                    f'<td style="padding:6px;border-bottom:1px solid #eee;'
                    f'vertical-align:top">'
                    f'{esc(t["due"]) if t["due"] else "—"}</td>'
                    "</tr>")

        rows = "".join(_row(t) for t in todos)
        parts.append(
            "<h3>Action items</h3>"
            '<table role="presentation" cellspacing="0" cellpadding="0" '
            'style="border-collapse:collapse;width:100%;font-size:14px">'
            '<thead><tr style="text-align:left;border-bottom:2px solid #ccc">'
            '<th style="padding:6px">Task</th><th style="padding:6px">Assignee</th>'
            '<th style="padding:6px">Due</th></tr></thead>'
            f"<tbody>{rows}</tbody></table>")
    else:
        parts.append(f"<p>{esc(no_todos_note)}</p>")
    body_html = "\n".join(parts)

    return subject, body_text, body_html


# ============================================================
# Non-VPC send worker — S3-triggered on session_finalize_requests/
# ============================================================

def _default_write_result(session_id, payload):
    """Record the send outcome the in-VPC sweep's reconcile pass reads."""
    import boto3
    boto3.client("s3").put_object(
        Bucket=S3_BUCKET, Key=f"{FINALIZE_RESULTS_PREFIX}{session_id}.json",
        Body=json.dumps(payload), ContentType="application/json")


def _complete_summary(artifact, summarize=None):
    """Re-summarise the WHOLE session from its transcripts at finalize time. The
    enqueued rolling summary can be stale/partial — A throttles mid-meeting and the
    session may grow after its last tick, and C can claim before the next one. But
    finalize runs after close + grace, so every transcript is in by now; re-gathering
    yields a COMPLETE summary. Returns {summary, open_todos} or None (missing keys /
    no turns / LLM failure) so the caller falls back to the rolling summary. Imports
    are lazy (extract_session/rolling pull boto3 + llm_utils) — this worker is non-VPC
    so the LLM is reachable; SessionFinalizeFunction carries the LLM env."""
    folder, date, sid = artifact.get("folder"), artifact.get("date"), artifact.get("sessionId")
    if not (folder and date and sid):
        return None
    try:
        import lambda_extract_session as ex
        keys = ex.gather_session_segments(S3_BUCKET, folder, date, "sid" + sid)
        turns, _sources = ex.assemble_deduped_turns(S3_BUCKET, keys)
        if not turns:
            return None
        if summarize is None:
            if SESSION_BRIEF:
                import session_brief
                summarize = session_brief.brief_from_turns
            else:
                import lambda_rolling_summary as rs
                summarize = rs.summarize_turns
        result = summarize(turns)
        # A brief is worth more than the two keys the email reads, and this is
        # the only point in the pipeline where the whole session exists as one
        # clean turn stream. Store it before handing the caller its two keys.
        # Best-effort: the email must still go out if the write fails.
        if result and result.get("sections"):
            _store_brief(folder, date, sid, result)
        return result
    except Exception:
        logger.exception("finalize: complete re-summary failed for %s — using rolling summary", sid)
        return None


def _store_brief(folder, date, session_id, brief):
    """Write the session brief to S3, mirroring session_rolling/'s layout so the
    read path is the one already proven for the rolling summary. Never raises:
    losing the stored copy must not cost the recorder their email."""
    try:
        import boto3
        key = f"{BRIEF_PREFIX}{folder}/{date}/sid{session_id}/latest.json"
        boto3.client("s3").put_object(
            Bucket=S3_BUCKET, Key=key,
            Body=json.dumps(brief, ensure_ascii=False).encode("utf-8"),
            ContentType="application/json")
        logger.info("session brief stored at %s", key)
    except Exception:
        logger.exception("session brief could not be stored for %s "
                         "-- the email is unaffected", session_id)


def _session_was_deleted(artifact):
    """Is this session in the day's deletion mirror? Never raises.

    THERE ARE TWO OF THESE AND THE DIFFERENCE IS DELIBERATE. `lambda_org_api.
    _session_was_removed` answers the same question for the three read endpoints
    (brief, rolling, report status) and RAISES when the mirror is unreadable.
    This one returns False and proceeds.

    Neither is the mistake, and neither should be "made consistent" with the
    other:

    * There, a failed check costs one reader one refresh, and answering "removed"
      when the truth is "could not check" would turn a broken grant on
      `redactions/` into a silent total outage wearing the costume of normal
      operation.
    * Here, it costs a recorder their ONLY confirmation email for a session that
      was probably never deleted -- and this worker records failures instead of
      retrying them, so failing closed loses the email permanently.

    They are separate functions rather than one because this lambda is non-VPC
    and cannot import org-api's module; the shared part is `deletion_mirror`,
    which both use. If a third caller appears with the strict posture, it belongs
    in org-api's helper, not here.

    The mirror is exactly what this worker is supposed to read: it is the copy of
    the answer written for the lambdas that hold no database connection, and this
    one is non-VPC. Both spellings are matched -- the mirror carries whatever
    `sessionBase` the delete endpoint had, and the artifact's `sessionId` is bare
    hex -- because two spellings of a session are equal as sessions and not as
    strings.

    Failure reads as "not deleted", which is the lenient direction and is argued
    at the call site. It is LOGGED, because a permission fault here looks exactly
    like "nothing was deleted" and that indistinguishability has cost this
    project three separate silent breakages.
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
        logger.exception("finalize: deletion mirror unreadable for %s/%s -- proceeding "
                         "as if nothing was deleted, which may mail a removed recording",
                         folder, date)
        return False
    return sid in deleted or f"sid{sid}" in deleted


def process_finalize_request(artifact, *, send=None, write_result=None, complete_summary=None):
    """Build + SES-send the recorder's confirmation email from one enqueued finalize
    request (the in-VPC claim step wrote it), then record the outcome to
    session_finalize_results/{sid}.json — the in-VPC sweep's reconcile pass reads it
    and moves the session finalizing -> sent/failed (this non-VPC worker can't touch
    Aurora, CLAUDE.md BUG-36). A send failure is RECORDED (status 'error'), not
    re-raised: re-raising would S3-retry the trigger and risk a double-send. `send` /
    `write_result` are injectable; they default to email_sender + an S3 write (both
    lazy so the module stays pure at import). A request with no recipient is skipped —
    the claim step already marked that session failed."""
    recipient = (artifact.get("recipient") or "").strip()
    if not recipient:
        return {"status": "skipped", "reason": "no recipient"}
    session_id = artifact.get("sessionId")

    # A recording deleted before this request is processed must not be emailed,
    # and must not have a fresh brief written from its transcripts.
    #
    # There was no coordination between the two paths at all: neither this module
    # nor `lambda_finalize_claim` held a deletion check, and
    # `delete_recordings_endpoint` does not touch `meeting_session` or the
    # enqueued request. `_complete_summary` re-gathers through
    # `gather_session_segments`, which does not filter either -- so the deleted
    # session would be rebuilt in full and mailed.
    #
    # The window is narrow and real: the recordings list is populated at upload,
    # well before finalize runs, and the sweep re-drives a finalize that failed.
    #
    # SEVERITY, so it is neither over- nor under-read: the recipient is the
    # RECORDER, the person who deleted it. Not a disclosure to a third party --
    # the product telling someone their recording was removed and then mailing it
    # back. A promise broken, not a breach.
    #
    # LENIENT, unlike the brief endpoint, and the asymmetry is deliberate. There
    # a failed check costs one reader one refresh. Here it costs a recorder their
    # only confirmation for a session that was probably never deleted -- and this
    # worker RECORDS failures rather than retrying them (re-raising would
    # S3-retry the trigger and risk a double-send), so failing closed would lose
    # the email permanently. Logged loudly instead.
    if _session_was_deleted(artifact):
        logger.info("finalize: %s was deleted -- not sending, not storing a brief",
                    session_id)
        outcome = {"status": "skipped", "reason": "recording deleted",
                   "sessionId": session_id}
        # RECORDED, not merely returned. The in-VPC sweep reconciles `finalizing`
        # from this file; a skip that writes nothing leaves the session stuck in
        # `finalizing` forever, which is a different bug wearing this fix's
        # clothes.
        (write_result if write_result is not None else _default_write_result)(
            session_id, outcome)
        return outcome

    # Prefer a FRESH complete summary re-derived from the full transcript over the
    # enqueued rolling summary (which can be stale/partial — see _complete_summary);
    # fall back to the rolling summary on any failure.
    summary, todos = artifact.get("summary"), artifact.get("openTodos")
    is_updated = artifact.get("kind") == "updated"
    # An `updated` request already carries the ONE merged summary every member
    # must receive. Re-deriving would summarise this member's own SOLO
    # transcripts (_complete_summary re-gathers `sid{sessionId}`), so the N
    # members would each get a summary of what THEY heard under a subject saying
    # the meeting was merged -- N different bodies, N LLM calls, and the one
    # thing the merge promised quietly not delivered.
    if not is_updated:
        fresh = (complete_summary if complete_summary is not None else _complete_summary)(artifact)
        if fresh:
            summary, todos = fresh.get("summary", summary), fresh.get("open_todos", todos)
    subject, text, html = build_confirmation_email(
        date=artifact.get("date"), time_range=artifact.get("timeRange"),
        site_name=artifact.get("siteName"), summary=summary, open_todos=todos)
    if is_updated:
        # A second email with a different body must not read as a duplicate of
        # the first. The recipient already had one for this meeting.
        subject = f"Updated: {subject}"
        # And its result goes to its own key. reconcile reads
        # session_finalize_results/{sessionId}.json to settle a CLAIMED session;
        # a member can be counted settled by quietness while still `finalizing`,
        # so an updated result on the solo key could move that session to `sent`
        # on the wrong evidence.
        session_id = f"{session_id}-updated"
    if send is None:
        from email_sender import get_sender
        send = get_sender().send
    if write_result is None:
        write_result = _default_write_result
    try:
        send(recipient, subject, text, html)
    except Exception as e:
        write_result(session_id, {"status": "error", "sessionId": session_id, "error": str(e)})
        return {"status": "error", "recipient": recipient, "sessionId": session_id}
    write_result(session_id, {"status": "sent", "sessionId": session_id, "recipient": recipient})
    return {"status": "sent", "recipient": recipient, "sessionId": session_id}


def lambda_handler(event, context):
    """S3 event on session_finalize_requests/*.json — send each enqueued
    confirmation email. Non-VPC (reaches SES, CLAUDE.md BUG-36)."""
    import boto3
    s3 = boto3.client("s3")
    results = []
    for rec in event.get("Records", []):
        s3rec = rec.get("s3") or {}
        key = (s3rec.get("object") or {}).get("key")
        bucket = (s3rec.get("bucket") or {}).get("name")
        if not key:
            continue
        key = unquote_plus(key)                 # S3 notifications URL-encode the key
        obj = s3.get_object(Bucket=bucket, Key=key)
        artifact = json.loads(obj["Body"].read().decode("utf-8"))
        results.append(process_finalize_request(artifact))
    return {"results": results}
