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
from urllib.parse import unquote_plus


def _clean_todos(open_todos):
    """Keep only to-dos with real text; normalise responsible to a name or None."""
    out = []
    for t in (open_todos or []):
        text = (t.get("text") or "").strip()
        if text:
            out.append({"text": text, "responsible": (t.get("responsible") or None)})
    return out


def build_confirmation_email(*, date=None, site_name=None, summary=None, open_todos=None):
    """(subject, body_text, body_html) for the recorder's confirmation email, built
    from the session's rolling summary + still-open to-dos. Pure — no I/O. A blank
    summary renders a placeholder (the email is never empty); to-dos with no text are
    dropped; all HTML content is escaped so transcript text can't inject markup."""
    subject = "FieldSight — your site notes" + (f" ({date})" if date else "")
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
        lines += ["", "Open action items"]
        for t in todos:
            owner = f" — {t['responsible']}" if t["responsible"] else ""
            lines.append(f"  • {t['text']}{owner}")
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
        items = "".join(
            f"<li>{esc(t['text'])}" + (f" — {esc(t['responsible'])}" if t["responsible"] else "") + "</li>"
            for t in todos)
        parts.append(f"<h3>Open action items</h3><ul>{items}</ul>")
    body_html = "\n".join(parts)

    return subject, body_text, body_html


# ============================================================
# Non-VPC send worker — S3-triggered on session_finalize_requests/
# ============================================================

def process_finalize_request(artifact, *, send=None):
    """Build + SES-send the recorder's confirmation email from one enqueued finalize
    request (the in-VPC claim step wrote it). `send(to, subject, text, html)` is
    injectable; defaults to email_sender.get_sender().send (imported lazily so this
    module stays pure at import). A request with no recipient is skipped — the claim
    step already marked that session failed."""
    recipient = (artifact.get("recipient") or "").strip()
    if not recipient:
        return {"status": "skipped", "reason": "no recipient"}
    subject, text, html = build_confirmation_email(
        date=artifact.get("date"), site_name=artifact.get("siteName"),
        summary=artifact.get("summary"), open_todos=artifact.get("openTodos"))
    if send is None:
        from email_sender import get_sender
        send = get_sender().send
    send(recipient, subject, text, html)
    return {"status": "sent", "recipient": recipient, "sessionId": artifact.get("sessionId")}


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
