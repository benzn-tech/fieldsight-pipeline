"""email_sender.py — pluggable outbound-email channel for the voice-timeliness
confirmation email (spec §3.2/§3.8).

The finalize / Tier-2-lite / rolling-summary logic is CHANNEL-AGNOSTIC: it calls
`get_sender().send(...)` and never knows how the mail leaves. Swapping the channel
(no-DNS verified mailbox -> custom-domain identity with DKIM, once DNS lands) is a
config change, not a code change:

    EMAIL_SENDER = ses | stub        (default 'stub' — logs, sends nothing)
    SENDER_EMAIL = an SES-*verified* identity. With NO domain this is just a
                   mailbox you own (e.g. fieldsight.noreply@gmail.com) that you
                   click-verified in SES; later it becomes no-reply@app.fieldsightai.com.
    AWS_REGION   = SES region (default ap-southeast-2)

No-DNS note: SES needs a *verified identity*, not a domain. A verified email
address works with zero DNS; a verified domain (SPF/DKIM/DMARC) only improves
deliverability. So `ses` is usable today; the domain swap is purely `SENDER_EMAIL`.
"""
import logging
import os

logger = logging.getLogger()
logger.setLevel(logging.INFO)

EMAIL_SENDER = os.environ.get("EMAIL_SENDER", "stub").lower()
SENDER_EMAIL = os.environ.get("SENDER_EMAIL", "")
SES_REGION = os.environ.get("AWS_REGION", "ap-southeast-2")


class EmailSender:
    """Channel interface. `send` returns a provider message id (or a sentinel);
    raises on hard failure so the caller can mark the session 'failed' and retry."""

    def send(self, to, subject, body_text, body_html=None) -> str:
        raise NotImplementedError


class StubEmailSender(EmailSender):
    """Sends nothing — logs the envelope. The safe default before an SES identity
    is verified (and for local/CI). Lets the whole finalize path run end to end
    without a live channel."""

    def send(self, to, subject, body_text, body_html=None) -> str:
        logger.info("[email:stub] to=%s subject=%r bytes=%d (NOT SENT — EMAIL_SENDER=stub)",
                    to, subject, len(body_text or ""))
        return "stub-not-sent"


class SesEmailSender(EmailSender):
    """AWS SES. Requires SENDER_EMAIL to be a verified identity (email or domain).
    In the SES sandbox the recipient must also be verified — fine here, since the
    confirmation goes to the recorder themselves."""

    def __init__(self, client=None, sender=None, region=None):
        self._client = client
        self._sender = sender or SENDER_EMAIL
        self._region = region or SES_REGION

    def _ses(self):
        if self._client is None:
            import boto3  # lazy: keep import cost off the stub/default path
            self._client = boto3.client("ses", region_name=self._region)
        return self._client

    def send(self, to, subject, body_text, body_html=None) -> str:
        if not self._sender:
            raise RuntimeError("SENDER_EMAIL not set — cannot send via SES")
        body = {"Text": {"Data": body_text or "", "Charset": "UTF-8"}}
        if body_html:
            body["Html"] = {"Data": body_html, "Charset": "UTF-8"}
        resp = self._ses().send_email(
            Source=self._sender,
            Destination={"ToAddresses": [to] if isinstance(to, str) else list(to)},
            Message={
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": body,
            },
        )
        mid = resp.get("MessageId", "")
        logger.info("[email:ses] sent to=%s messageId=%s", to, mid)
        return mid


def get_sender() -> EmailSender:
    """Factory: resolve the active channel from env. Unknown value -> stub (fail
    safe: never silently attempt a misconfigured live send)."""
    if EMAIL_SENDER == "ses":
        return SesEmailSender()
    if EMAIL_SENDER != "stub":
        logger.warning("Unknown EMAIL_SENDER=%r — falling back to stub", EMAIL_SENDER)
    return StubEmailSender()
