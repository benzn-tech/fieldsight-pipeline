"""External-member notices: org-api (in the VPC, no SES) leaves
notices/external_member/{membership_id}.json; the non-VPC backlog lambda emails it
exactly once and moves it to notices/sent/."""
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

bl = pytest.importorskip("lambda_extraction_backlog")

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
KEY = "notices/external_member/m-1.json"
NOTICE = {"to_email": "ext@home.nz", "to_name": "Eve Xu", "site_name": "Tower A",
          "inviting_company": "Site Co", "role": "pm", "created_at": "2026-10-09T11:50:00+00:00"}


class S3:
    def __init__(self, objs):
        self.objs = dict(objs)      # key -> (LastModified, body)

    def get_paginator(self, name):
        outer = self

        class _P:
            def paginate(self, Bucket=None, Prefix=""):
                yield {"Contents": [{"Key": k, "LastModified": v[0]}
                                    for k, v in sorted(outer.objs.items()) if k.startswith(Prefix)]}
        return _P()

    def get_object(self, Bucket=None, Key=None):
        body = json.dumps(self.objs[Key][1]).encode()
        return {"Body": type("B", (), {"read": lambda s: body})()}

    def copy_object(self, Bucket=None, Key=None, CopySource=None):
        self.objs[Key] = self.objs[CopySource["Key"]]

    def delete_object(self, Bucket=None, Key=None):
        self.objs.pop(Key, None)


class Sender:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def send(self, to, subject, body_text, body_html=None):
        if self.fail:
            raise RuntimeError("MessageRejected: address not verified")
        self.sent.append((to, subject, body_text))
        return "id"


def test_sends_once_and_moves_to_sent():
    s3 = S3({KEY: (NOW - timedelta(minutes=5), NOTICE)})
    snd = Sender()
    assert bl.send_external_member_notices(s3, now=NOW, sender=snd) == (1, 0, 0)
    to, subject, text = snd.sent[0]
    assert to == "ext@home.nz" and subject == "You've been added to Tower A on FieldSight"
    assert "Site Co" in text and "Tower A" in text and "pm" in text and "same" in text
    assert KEY not in s3.objs and "notices/sent/external_member/m-1.json" in s3.objs
    # next tick: nothing left to send
    assert bl.send_external_member_notices(s3, now=NOW, sender=snd) == (0, 0, 0)
    assert len(snd.sent) == 1


def test_failure_leaves_notice_for_retry():
    s3 = S3({KEY: (NOW - timedelta(minutes=5), NOTICE)})
    assert bl.send_external_member_notices(s3, now=NOW, sender=Sender(fail=True)) == (0, 1, 0)
    assert KEY in s3.objs and not any(k.startswith("notices/sent/") for k in s3.objs)
    snd = Sender()
    assert bl.send_external_member_notices(s3, now=NOW, sender=snd) == (1, 0, 0)


def test_expires_after_24h_with_error_log(caplog):
    s3 = S3({KEY: (NOW - timedelta(hours=25), NOTICE)})
    snd = Sender()
    with caplog.at_level("ERROR"):
        assert bl.send_external_member_notices(s3, now=NOW, sender=snd) == (0, 0, 1)
    assert KEY not in s3.objs and not snd.sent
    assert any("expired" in r.message and r.levelname == "ERROR" for r in caplog.records)


def test_stub_sender_never_marks_sent(monkeypatch):
    import email_sender
    monkeypatch.setattr(email_sender, "get_sender", lambda: email_sender.StubEmailSender())
    s3 = S3({KEY: (NOW - timedelta(minutes=5), NOTICE)})
    assert bl.send_external_member_notices(s3, now=NOW) == (0, 1, 0)
    assert KEY in s3.objs


def test_handler_redrive_task_sends_notices(monkeypatch):
    called = []
    monkeypatch.setattr(bl, "_s3", lambda: object())
    monkeypatch.setattr(bl, "redrive", lambda c, time_left_ms=None: ([], 0))
    monkeypatch.setattr(bl, "emit_pending_metric", lambda n: None)
    monkeypatch.setattr(bl, "send_external_member_notices", lambda c: called.append(1))
    assert bl.lambda_handler({"task": "redrive"}, None)["redriven"] == []
    assert called == [1]


# ---- template wiring
TEMPLATE = (Path(__file__).resolve().parents[2] / "src" / "template.yaml").read_text(encoding="utf-8")


def _block(name):
    m = re.search(rf"^  {name}:\n(.*?)(?=^  [A-Za-z]\w*:\n)", TEMPLATE, re.S | re.M)
    return m.group(1)


def test_backlog_function_can_send_and_move():
    b = _block("ExtractionBacklogFunction")
    assert "EMAIL_SENDER: !Ref EmailSender" in b and "SENDER_EMAIL: !Ref SenderEmail" in b
    assert "ses:SendEmail" in b
    assert "notices/external_member/*" in b and "notices/sent/*" in b
    assert "s3:DeleteObject" in b


def test_org_api_may_put_notices():
    b = _block("OrgApiFunction")
    assert re.search(r"s3:PutObject\n\s+Resource: !Sub arn:aws:s3:::\$\{IngestBucketName\}/notices/external_member/\*", b)
