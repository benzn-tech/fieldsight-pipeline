"""Unit: the pure part of Contact FieldSight (spec 2026-11-08-contact-fieldsight-design).

Validation, S3 key shape, the rolling-hour count and the MIME email. No AWS here;
the handler (lambda_contact) is tested separately with fakes."""
import base64
import email
import email.policy
import uuid
from datetime import datetime, timedelta, timezone

import pytest

import contact_messages as cm

GOOD_ID = str(uuid.uuid4())
NOW = datetime(2026, 11, 8, 3, 4, 5, tzinfo=timezone.utc)


def _body(**kw):
    b = {"id": GOOD_ID, "text": "hello", "audio": None, "mime": None,
         "transcript": None, "context": {"name": "Ann", "company": "Acme",
                                         "project": "Site 1", "page": "/timeline"}}
    b.update(kw)
    return b


# --- validation -------------------------------------------------------------

def test_validate_requires_text_or_audio():
    clean, err = cm.validate_message(_body(text="   ", audio=None))
    assert clean is None and err
    clean, err = cm.validate_message(_body(text="", audio=base64.b64encode(b"abc").decode(),
                                           mime="audio/webm"))
    assert err is None and clean["audio_bytes"] == b"abc" and clean["ext"] == "webm"


def test_text_over_4000_rejected():
    clean, err = cm.validate_message(_body(text="x" * 4001))
    assert clean is None and "4000" in err
    clean, err = cm.validate_message(_body(text="x" * 4000))
    assert err is None
    clean, err = cm.validate_message(_body(transcript="x" * 8001))
    assert clean is None and "8000" in err


def test_bad_uuid_rejected():
    for bad in (None, "", "not-a-uuid", str(uuid.uuid1()), 12345):
        clean, err = cm.validate_message(_body(id=bad))
        assert clean is None and err, bad


def test_mime_not_allowed():
    b64 = base64.b64encode(b"abc").decode()
    clean, err = cm.validate_message(_body(audio=b64, mime="audio/ogg;codecs=opus"))
    assert clean is None and err
    clean, err = cm.validate_message(_body(audio=b64, mime=None))
    assert clean is None and err
    clean, err = cm.validate_message(_body(audio=b64, mime="audio/mp4"))
    assert err is None and clean["ext"] == "m4a" and clean["mime"] == "audio/mp4"
    clean, err = cm.validate_message(_body(audio=b64, mime="audio/webm;codecs=opus"))
    assert err is None and clean["ext"] == "webm"


def test_b64_length_checked_before_decode(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("decoded before the length check")
    monkeypatch.setattr(cm.base64, "b64decode", boom)
    too_long = "A" * (4 * cm.MAX_AUDIO_BYTES // 3 + 8)
    audio, err = cm.validate_audio(too_long, "audio/webm")
    assert audio is None and "3 MB" in err


def test_garbage_base64_rejected():
    audio, err = cm.validate_audio("!!!not base64!!!", "audio/webm")
    assert audio is None and err


def test_data_url_prefix_tolerated():
    b64 = "data:audio/webm;codecs=opus;base64," + base64.b64encode(b"abc").decode()
    audio, err = cm.validate_audio(b64, "audio/webm;codecs=opus")
    assert err is None and audio == b"abc"


def test_context_strips_crlf_and_query():
    ctx = cm.clean_context({"name": "A\r\nBcc: x", "company": "C" * 500,
                            "project": None, "page": "/timeline?q=secret",
                            "extra": "dropped"})
    assert ctx["name"] == "A Bcc: x"
    assert len(ctx["company"]) == 200
    assert ctx["project"] == ""
    assert ctx["page"] == "/timeline"
    assert set(ctx) == {"name", "company", "project", "page"}
    assert cm.clean_context(None) == {"name": "", "company": "", "project": "", "page": ""}


# --- keys and counting ------------------------------------------------------

def test_record_stem_shape():
    assert cm.record_stem("sub-1", NOW, GOOD_ID) == \
        f"contact_messages/sub-1/2026-11-08T030405Z_{GOOD_ID}"


def test_count_recent_ignores_old_and_counter_keys():
    p = "contact_messages/s/"
    keys = [
        p + "2026-11-08T025000Z_a.json",          # 14 min ago: counts
        p + "2026-11-08T020500Z_b.json",          # 59 min ago: counts
        p + "2026-11-08T020000Z_c.json",          # 64 min ago: old
        p + "2026-11-08T025000Z_a.webm",          # audio, not a record
        p + "_transcribe/2026-11-08T03.json",     # counter object
        p + "garbage.json",                       # unparseable
    ]
    assert cm.count_recent(keys, NOW) == 2


# --- email ------------------------------------------------------------------

def _record(**kw):
    r = {"id": GOOD_ID, "at": NOW.isoformat(), "sub": "sub-1", "email": "ann@acme.co",
         "context": {"name": "Ann", "company": "Acme", "project": "Site 1",
                     "page": "/timeline"},
         "text": "hello", "transcript": "", "audio_key": None, "mime": None,
         "env": "test", "record_key": f"contact_messages/sub-1/x_{GOOD_ID}.json"}
    r.update(kw)
    return r


def _build(record=None, audio=None, ext=None, stage="test"):
    raw = cm.build_email(record=record or _record(), audio=audio, ext=ext,
                         sender="sales@fieldsightai.com", to="sales@fieldsightai.com",
                         stage=stage, now_nz_label="8 Nov 2026 16:04 NZDT")
    assert isinstance(raw, bytes)
    return email.message_from_bytes(raw, policy=email.policy.default)


def _plain(msg):
    return msg.get_body(preferencelist=("plain",)).get_content()


def test_subject_test_prefix_and_no_newline():
    rec = _record(context=cm.clean_context({"name": "A\r\nBcc: evil@x.com",
                                            "company": "Acme\nX"}))
    msg = _build(rec, stage="test")
    subj = msg["Subject"]
    assert subj.startswith("[TEST] ")
    assert "\n" not in subj and "\r" not in subj
    assert subj == "[TEST] FieldSight message from A Bcc: evil@x.com (Acme X)"
    assert msg["Bcc"] is None
    assert not _build(stage="prod")["Subject"].startswith("[TEST]")


def test_reply_to_is_claim_email():
    msg = _build()
    assert msg["Reply-To"] == "ann@acme.co"
    assert msg["To"] == "sales@fieldsightai.com"
    assert msg["From"] == "sales@fieldsightai.com"


def test_transcript_section_only_when_edited():
    marker = "Transcript (machine, before the person edited it)"
    assert marker not in _plain(_build(_record(transcript="")))
    assert marker not in _plain(_build(_record(text=" hello ", transcript="hello")))
    body = _plain(_build(_record(text="hello there", transcript="hello their")))
    assert marker in body and "hello their" in body
    # text first
    assert body.index("hello there") < body.index(marker)


def test_plain_body_carries_who_and_where():
    body = _plain(_build())
    for s in ("ann@acme.co", "sub-1", "Site 1", "/timeline", "test",
              "8 Nov 2026 16:04 NZDT", f"contact_messages/sub-1/x_{GOOD_ID}.json"):
        assert s in body, s


def test_audio_attached_with_ext():
    msg = _build(audio=b"\x1a\x45\xdf\xa3", ext="webm")
    atts = list(msg.iter_attachments())
    assert len(atts) == 1
    assert atts[0].get_filename() == "message.webm"
    assert atts[0].get_content() == b"\x1a\x45\xdf\xa3"
    msg = _build(audio=b"abc", ext="m4a")
    assert [a.get_filename() for a in msg.iter_attachments()] == ["message.m4a"]
    assert list(_build().iter_attachments()) == []


def test_html_part_escapes():
    msg = _build(_record(text="<script>alert(1)</script>"))
    html = msg.get_body(preferencelist=("html",)).get_content()
    assert "&lt;script&gt;" in html
    assert "<script>" not in html
