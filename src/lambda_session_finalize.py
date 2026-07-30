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
