"""Contact FieldSight: the pure part (spec docs/superpowers/specs/2026-11-08-contact-fieldsight-design.md).

Validation of the two request bodies, the S3 key shape, the rolling-hour count and
the MIME email. Standard library only, no AWS: `lambda_contact` does the I/O.

Identity (sub, email) is never read from a body here. Everything under `context` is
"as reported by the browser" and is labelled that way in the email.
"""
import base64
import binascii
import html
import re
import uuid
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.policy import SMTP

MAX_TEXT = 4000
MAX_TRANSCRIPT = 8000
MAX_AUDIO_BYTES = 3 * 1024 * 1024
# The longest base64 string that can decode to MAX_AUDIO_BYTES (plus padding slack).
# Checked BEFORE decoding, so an oversized body never costs a decode.
MAX_AUDIO_B64 = 4 * MAX_AUDIO_BYTES // 3 + 4
ALLOWED_MIME = {"audio/webm;codecs=opus", "audio/webm", "audio/mp4"}
MSG_PER_HOUR = 10
TRANSCRIBE_PER_HOUR = 20
MAX_CONTEXT_VALUE = 200
CONTEXT_KEYS = ("name", "company", "project", "page")

PREFIX = "contact_messages"
_STAMP_FMT = "%Y-%m-%dT%H%M%SZ"
_RECORD_RE = re.compile(r"/(\d{4}-\d{2}-\d{2}T\d{6}Z)_[^/]+\.json$")
_DATA_URL_RE = re.compile(r"^data:[^,]*;base64,", re.IGNORECASE)


def _ext_for(mime):
    return "m4a" if mime == "audio/mp4" else "webm"


def _one_line(value):
    """CR/LF (and other line breaks) become spaces; a header can never be split."""
    s = "" if value is None else str(value)
    s = re.sub(r"[\r\n  \x0b\x0c\x85]+", " ", s)
    return s.strip()


def clean_context(ctx):
    """Browser-reported context: only the four known keys, one line each, capped.
    `page` loses any query string or fragment (a search term or token in the URL
    must not reach the email)."""
    ctx = ctx if isinstance(ctx, dict) else {}
    out = {}
    for k in CONTEXT_KEYS:
        v = _one_line(ctx.get(k))
        if k == "page":
            v = v.split("?", 1)[0].split("#", 1)[0]
        out[k] = v[:MAX_CONTEXT_VALUE]
    return out


def validate_audio(b64, mime):
    """(bytes, None) | (None, None) when there is no audio | (None, message)."""
    if b64 is None or b64 == "":
        return None, None
    if not isinstance(b64, str):
        return None, "audio must be a base64 string"
    if mime not in ALLOWED_MIME:
        return None, "audio type not supported (use audio/webm or audio/mp4)"
    s = _DATA_URL_RE.sub("", b64, count=1)
    if len(s) > MAX_AUDIO_B64:
        return None, "recording is over 3 MB"
    try:
        data = base64.b64decode(s, validate=True)
    except (binascii.Error, ValueError):
        return None, "audio is not valid base64"
    if len(data) > MAX_AUDIO_BYTES:
        return None, "recording is over 3 MB"
    if not data:
        return None, None
    return data, None


def _is_uuid4(value):
    if not isinstance(value, str):
        return False
    try:
        u = uuid.UUID(value)
    except (ValueError, AttributeError):
        return False
    return u.version == 4 and str(u) == value.lower()


def validate_message(body):
    """(clean, None) or (None, "plain message")."""
    if not isinstance(body, dict):
        return None, "request body must be a JSON object"
    mid = body.get("id")
    if not _is_uuid4(mid):
        return None, "id must be a uuid4"
    text = body.get("text")
    text = "" if text is None else text
    transcript = body.get("transcript")
    transcript = "" if transcript is None else transcript
    if not isinstance(text, str) or not isinstance(transcript, str):
        return None, "text and transcript must be strings"
    if len(text) > MAX_TEXT:
        return None, f"text is over {MAX_TEXT} characters"
    if len(transcript) > MAX_TRANSCRIPT:
        return None, f"transcript is over {MAX_TRANSCRIPT} characters"
    mime = body.get("mime")
    audio, err = validate_audio(body.get("audio"), mime)
    if err:
        return None, err
    if not text.strip() and audio is None:
        return None, "a message needs text or a recording"
    return {
        "id": mid.lower(),
        "text": text,
        "transcript": transcript,
        "audio_bytes": audio,
        "mime": mime if audio is not None else None,
        "ext": _ext_for(mime) if audio is not None else None,
        "context": clean_context(body.get("context")),
    }, None


def record_stem(sub, at_utc, id):
    """contact_messages/{sub}/{yyyy-mm-ddTHHMMSSZ}_{id} -- the record adds .json,
    the audio .webm/.m4a."""
    at = at_utc.astimezone(timezone.utc) if at_utc.tzinfo else at_utc
    return f"{PREFIX}/{sub}/{at.strftime(_STAMP_FMT)}_{id}"


def recent_times(keys, now_utc, window=timedelta(hours=1)):
    """Sorted UTC timestamps of the message records in `keys` that fall inside the
    rolling window. Audio files and the `_transcribe/` counters are not records."""
    now = now_utc if now_utc.tzinfo else now_utc.replace(tzinfo=timezone.utc)
    out = []
    for k in keys:
        if "/_transcribe/" in k:
            continue
        m = _RECORD_RE.search(k)
        if not m:
            continue
        ts = datetime.strptime(m.group(1), _STAMP_FMT).replace(tzinfo=timezone.utc)
        if now - window < ts <= now + timedelta(minutes=5):
            out.append(ts)
    return sorted(out)


def count_recent(keys, now_utc, window=timedelta(hours=1)):
    return len(recent_times(keys, now_utc, window))


def _subject(context, stage, claim_name=None):
    # The account's own name (authorizer claim) when there is one; the browser's
    # report otherwise.
    name = _one_line(claim_name) or _one_line(context.get("name")) or "(no name)"
    company = _one_line(context.get("company")) or "no company"
    prefix = "" if stage == "prod" else "[TEST] "
    return _one_line(f"{prefix}FieldSight message from {name} ({company})")


def build_email(*, record, audio, ext, sender, to, stage, now_nz_label):
    """The raw MIME bytes for ses.send_raw_email.

    multipart/mixed: [multipart/alternative: text/plain, text/html] + the audio
    as message.webm / message.m4a when there is one. The plain text comes first.

    The body is the text the person sent and nothing else. The machine transcript
    is never in the email: next to the edited text it read as two overlapping
    versions of one message (owner, 2026-11-09). The recording is attached, and
    the unedited transcript stays in the S3 record."""
    ctx = record.get("context") or {}
    text = record.get("text") or ""
    has_transcript = bool((record.get("transcript") or "").strip())

    details = [
        ("Email", record.get("email") or ""),
        ("Cognito sub", record.get("sub") or ""),
        ("Name (account)", _one_line(record.get("claim_name"))),
        ("Name (as reported by the browser)", ctx.get("name") or ""),
        ("Company (as reported by the browser)", ctx.get("company") or ""),
        ("Project (as reported by the browser)", ctx.get("project") or ""),
        ("Page (as reported by the browser)", ctx.get("page") or ""),
        ("Environment", record.get("env") or ""),
        ("Time (NZ)", now_nz_label or ""),
        ("Message id", record.get("id") or ""),
        ("S3 record", record.get("record_key") or ""),
    ]
    if audio:
        details.append(("Recording", f"attached as message.{ext}"))
    if has_transcript:
        details.append(("Machine transcript", "in the S3 record (not shown here)"))

    message = text.strip() or "(no typed text; see the attached recording)"
    plain = ["Message:", message, ""]
    plain.append("---")
    plain += [f"{k}: {v}" for k, v in details]
    plain_body = "\n".join(plain) + "\n"

    def para(s):
        return html.escape(s).replace("\n", "<br>")

    h = ["<html><body style=\"font-family:sans-serif\">",
         f"<p><b>Message:</b><br>{para(message)}</p>",
         "<hr><table>"]
    h += [f"<tr><td><b>{html.escape(k)}</b></td><td>{html.escape(str(v))}</td></tr>"
          for k, v in details]
    h.append("</table></body></html>")

    msg = EmailMessage()
    msg["Subject"] = _subject(ctx, stage, record.get("claim_name"))
    msg["From"] = sender
    msg["To"] = to
    reply_to = _one_line(record.get("email"))
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(plain_body)
    msg.add_alternative("\n".join(h), subtype="html")
    if audio:
        sub = "mp4" if ext == "m4a" else "webm"
        msg.add_attachment(audio, maintype="audio", subtype=sub,
                           filename=f"message.{ext}")
    return msg.as_bytes(policy=SMTP)
