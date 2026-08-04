"""Tell someone, but only when there is something to tell.

The Notion table is always current, so a push is not "here is the state" — it
is "something needs a decision". Anything that does not need a decision stays
in the table: pushing context trains people to ignore the channel, and then the
alerts that matter are ignored too.

`site_mismatch_flag` is deliberately absent from the headings below for exactly
that reason — it is shown on the row and never pushed.
"""

import json
import logging

import urllib3

logger = logging.getLogger()
_default_http = urllib3.PoolManager()

# Ordered most-actionable first.
_HEADINGS = [
    ("due_back", "该回收"),
    ("never_activated", "发出未上线"),
    ("quiet", "使用中失联"),
    ("outdated_version", "版本落后"),
]


def format_message(results, database_url):
    """The push body, or None when nothing needs a decision."""
    lines = []
    for key, heading in _HEADINGS:
        hit = sorted(r["device"] for r in results if key in r["alerts"])
        if hit:
            lines.append(f"{heading} ({len(hit)}): " + " · ".join(hit))
    if not lines:
        return None
    lines.append("")
    lines.append(database_url)
    return "\n".join(lines)


def _default_mailer(**kwargs):
    import email_sender

    email_sender.send(**kwargs)


def push(text, teams_webhook, email_to, ses_sender, http=None, mailer=None):
    """Fan the message out. Each channel fails independently — one being down
    must not silence the other."""
    if not text:
        return
    http = http or _default_http
    mailer = mailer or _default_mailer

    if teams_webhook:
        try:
            http.request(
                "POST", teams_webhook,
                headers={"Content-Type": "application/json"},
                body=json.dumps({"text": text}).encode(),
            )
        except Exception:
            logger.exception("teams push failed")

    if email_to and ses_sender:
        try:
            mailer(
                sender=ses_sender,
                to=email_to,
                subject="FieldSight 设备台账 — 有设备需要处理",
                body_text=text,
            )
        except Exception:
            logger.exception("email push failed")
