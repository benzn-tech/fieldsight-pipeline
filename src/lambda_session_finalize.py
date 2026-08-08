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
                        "due": (t.get("due") or None)})
    return out


def build_confirmation_email(*, date=None, time_range=None, site_name=None,
                             summary=None, open_todos=None):
    """(subject, body_text, body_html) for the recorder's confirmation email, built
    from the session's rolling summary + still-open to-dos. Pure — no I/O. A blank
    summary renders a placeholder (the email is never empty); to-dos with no text are
    dropped; all HTML content is escaped so transcript text can't inject markup. The
    subject carries date + meeting time range so the recorder can tell WHICH meeting
    it is (multiple recordings a day otherwise share one subject)."""
    stamp = " ".join(p for p in (date, time_range) if p)
    subject = "FieldSight — your site notes" + (f" ({stamp})" if stamp else "")
    summary_text = (summary or "").strip() or "No summary was generated for this recording."
    todos = _clean_todos(open_todos)

    lines = ["Here's what we captured from your recording — reply or open FieldSight "
             "to correct anything before you leave site.", ""]
    if site_name:
        lines.append(f"Site: {site_name}")
    if date:
        lines.append(f"Date: {date}")
    lines += ["", "Summary", summary_text]
    if todos:
        lines += ["", "Action items"]
        for t in todos:
            who = t["responsible"] or "Unassigned"
            due = f" (due {t['due']})" if t["due"] else ""
            lines.append(f"  • {t['text']} — {who}{due}")
    body_text = "\n".join(lines).rstrip() + "\n"

    esc = _html.escape
    parts = ["<p>Here's what we captured from your recording — reply or open FieldSight "
             "to correct anything before you leave site.</p>"]
    meta = []
    if site_name:
        meta.append(f"<strong>Site:</strong> {esc(site_name)}")
    if date:
        meta.append(f"<strong>Date:</strong> {esc(date)}")
    if meta:
        parts.append("<p>" + "<br>".join(meta) + "</p>")
    parts.append(f"<h3>Summary</h3><p>{esc(summary_text)}</p>")
    if todos:
        rows = "".join(
            "<tr>"
            f'<td style="padding:6px;border-bottom:1px solid #eee">{esc(t["text"])}</td>'
            f'<td style="padding:6px;border-bottom:1px solid #eee">'
            f'{esc(t["responsible"]) if t["responsible"] else "—"}</td>'
            f'<td style="padding:6px;border-bottom:1px solid #eee">'
            f'{esc(t["due"]) if t["due"] else "—"}</td>'
            "</tr>"
            for t in todos)
        parts.append(
            "<h3>Action items</h3>"
            '<table role="presentation" cellspacing="0" cellpadding="0" '
            'style="border-collapse:collapse;width:100%;font-size:14px">'
            '<thead><tr style="text-align:left;border-bottom:2px solid #ccc">'
            '<th style="padding:6px">Task</th><th style="padding:6px">Assignee</th>'
            '<th style="padding:6px">Due</th></tr></thead>'
            f"<tbody>{rows}</tbody></table>")
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
            import lambda_rolling_summary as rs
            summarize = rs.summarize_turns
        return summarize(turns)
    except Exception:
        logger.exception("finalize: complete re-summary failed for %s — using rolling summary", sid)
        return None


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
