"""The gateway forwards the scope fields untouched, and never invents them.

Spec 2026-09-15 §4.1 / §5 test 12. The proxy does no validation (the Ask Agent
does); it must not drop a field, and must not turn an absent one into '' -- a
blank every reader downstream would have to special-case.
"""
import json

import pytest

fsapi = pytest.importorskip("lambda_fieldsight_api", reason="requires boto3 (installed in CI)")

CALLER = {"sub": "sub-1", "role": "admin", "display_name": "Ada", "email": "a@x.nz"}
FIELDS = ("site_id", "author_folder", "topic_row_id")


class Recorder:
    def __init__(self):
        self.sent = []

    def invoke(self, FunctionName, InvocationType, Payload, **kw):
        self.sent.append(json.loads(Payload))

        class S:
            def read(inner):
                return json.dumps({"answer": "ok", "citations": []}).encode("utf-8")
        return {"Payload": S()}


def test_scope_fields_are_forwarded_verbatim(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(fsapi, "lambda_client", rec)

    fsapi.ask_question({"question": "open actions?", "date": "2026-09-03",
                        "site_id": "5c0e8d7a-1111-4222-8333-444455556666",
                        "author_folder": "Ben_UCPK2",
                        "topic_row_id": "not-a-uuid"}, CALLER)   # no validation here

    sent = rec.sent[0]
    assert sent["site_id"] == "5c0e8d7a-1111-4222-8333-444455556666"
    assert sent["author_folder"] == "Ben_UCPK2"
    assert sent["topic_row_id"] == "not-a-uuid"
    assert sent["date"] == "2026-09-03"


@pytest.mark.parametrize("value", [None, ""])
def test_absent_or_blank_scope_fields_are_not_sent(monkeypatch, value):
    rec = Recorder()
    monkeypatch.setattr(fsapi, "lambda_client", rec)

    body = {"question": "open actions?"}
    if value is not None:
        body.update({f: value for f in FIELDS})
    fsapi.ask_question(body, CALLER)

    for f in FIELDS:
        assert f not in rec.sent[0]


def test_scoped_is_forwarded_when_true(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(fsapi, "lambda_client", rec)
    fsapi.ask_question({"question": "open actions?", "date": "2026-09-03",
                        "scoped": True}, CALLER)
    assert rec.sent[0]["scoped"] is True
    assert rec.sent[0]["date"] == "2026-09-03"


@pytest.mark.parametrize("value", [None, False, ""])
def test_scoped_is_not_sent_when_absent_or_falsy(monkeypatch, value):
    rec = Recorder()
    monkeypatch.setattr(fsapi, "lambda_client", rec)
    body = {"question": "open actions?", "date": "2026-09-03"}
    if value is not None:
        body["scoped"] = value
    fsapi.ask_question(body, CALLER)
    assert "scoped" not in rec.sent[0]
