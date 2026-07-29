"""Delivery-C generate worker (Tier-2 T3, session-report-review-export §6).

The worker is non-VPC (has python-docx + can reach SES). org-api enqueues a
request artifact to session_report_requests/ (BUG-36: in-VPC org-api can't
render docx / reach SES), an S3 trigger fires this worker, and it renders the
Word doc into session_reports/, optionally emails it, and writes the result the
frontend polls for.

Increment 2a pins the PIPELINE (output prefix, result JSON, email branch, error
path); the content -> minutes_data mapping (2b) is tested separately.
"""
import json
from io import BytesIO

import pytest

import lambda_session_report as worker   # errors loudly if the module is missing (RED)

S3_BUCKET = "fieldsight-data-test"

DOC_KEY = "session_reports/Ada_L/2026-07-25/Benl1_2026-07-25_13-00-11/req123.docx"


def _artifact(**over):
    a = {
        "requestId": "req123",
        "sessionId": "Benl1_2026-07-25_13-00-11",
        "date": "2026-07-25",
        "folder": "Ada_L",
        "companyId": "c-1",
        "requestedBy": "u-1",
        "title": "Morning site inspection",
        "attendees": ["Ben", "Neil"],
        "fields": {"weather": "Fine"},
        "deliver": "download",
        "recipients": [],
        "content": {"date": "2026-07-25", "title": "Morning site inspection",
                    "participants": ["Ben", "Neil"], "topics": [], "topic_row_ids": []},
        "resultKey": "session_report_results/Ada_L/2026-07-25/Benl1_2026-07-25_13-00-11/req123.json",
    }
    a.update(over)
    return a


@pytest.fixture
def fake_s3(monkeypatch):
    """Serves the request artifact on get_object, records put_object calls."""
    store, puts = {}, {}

    class _S3:
        def get_object(self, Bucket, Key):
            return {"Body": BytesIO(store[Key].encode())}

        def put_object(self, Bucket, Key, Body, ContentType=None):
            puts[Key] = {"Body": Body, "ContentType": ContentType}
            return {}

    monkeypatch.setattr(worker, "S3_BUCKET", S3_BUCKET)
    monkeypatch.setattr(worker, "s3", lambda: _S3())
    return store, puts


@pytest.fixture
def fake_docx(monkeypatch):
    monkeypatch.setattr(worker, "generate_word_document",
                        lambda minutes, title: BytesIO(b"PK\x03\x04 fake docx bytes"))


@pytest.fixture
def fake_email(monkeypatch):
    sent = []

    class _Sender:
        def send(self, to, subject, body_text, body_html=None):
            sent.append({"to": to, "subject": subject})
            return "msg-1"

    monkeypatch.setattr(worker, "get_sender", lambda: _Sender())
    return sent


def _run(artifact, fake_s3):
    store, puts = fake_s3
    req_key = f"session_report_requests/Ada_L/2026-07-25/{artifact['sessionId']}/{artifact['requestId']}.json"
    store[req_key] = json.dumps(artifact)
    event = {"Records": [{"s3": {"bucket": {"name": S3_BUCKET}, "object": {"key": req_key}}}]}
    worker.lambda_handler(event, None)
    return puts


def test_worker_writes_docx_under_session_reports_prefix(fake_s3, fake_docx, fake_email):
    puts = _run(_artifact(), fake_s3)
    assert [k for k in puts if k.endswith(".docx")] == [DOC_KEY]
    assert puts[DOC_KEY]["Body"] == b"PK\x03\x04 fake docx bytes"


def test_worker_writes_done_result_with_dockey(fake_s3, fake_docx, fake_email):
    a = _artifact()
    puts = _run(a, fake_s3)
    res = json.loads(puts[a["resultKey"]]["Body"])
    assert res["status"] == "done"
    assert res["docKey"] == DOC_KEY
    assert res["requestId"] == "req123"


def test_worker_download_delivery_does_not_email(fake_s3, fake_docx, fake_email):
    _run(_artifact(deliver="download"), fake_s3)
    assert fake_email == []


def test_worker_email_delivery_sends_to_each_recipient(fake_s3, fake_docx, fake_email):
    a = _artifact(deliver="email", recipients=["a@x.nz", "b@x.nz"])
    puts = _run(a, fake_s3)
    assert [s["to"] for s in fake_email] == ["a@x.nz", "b@x.nz"]
    assert json.loads(puts[a["resultKey"]]["Body"])["emailed"] is True


def test_worker_writes_error_result_when_docx_unavailable(fake_s3, fake_email, monkeypatch):
    """python-docx missing (DOCX_AVAILABLE False -> generate returns None):
    the worker records an error result and writes NO doc, rather than crashing."""
    monkeypatch.setattr(worker, "generate_word_document", lambda minutes, title: None)
    a = _artifact()
    puts = _run(a, fake_s3)
    res = json.loads(puts[a["resultKey"]]["Body"])
    assert res["status"] == "error"
    assert not any(k.endswith(".docx") for k in puts)


def test_worker_url_encoded_event_key_is_decoded(fake_s3, fake_docx, fake_email):
    """S3 notifications URL-encode the object key (space -> +). The worker must
    unquote it before get_object, or the read 404s."""
    store, puts = fake_s3
    a = _artifact()
    real_key = "session_report_requests/Ada L/2026-07-25/s/req123.json"   # note the space
    store[real_key] = json.dumps(a)
    event = {"Records": [{"s3": {"bucket": {"name": S3_BUCKET},
                                 "object": {"key": "session_report_requests/Ada+L/2026-07-25/s/req123.json"}}}]}
    worker.lambda_handler(event, None)
    assert any(k.endswith(".docx") for k in puts)     # it found + processed the artifact


# ----------------------------------------------------------
# 2b — _content_to_minutes: map reviewed content + user fields -> minutes_data
#   (the shape generate_word_document consumes). Pure function; T4 field folding.
# ----------------------------------------------------------

def _content_topic(**over):
    t = {
        "topic_title": "Slab pour", "category": "progress", "time_range": "13:00 – 13:40",
        "participants": ["Ben", "Neil"], "summary": "Poured the slab.", "key_decisions": [],
        "action_items": [{"id": "a-1", "action": "Fix rebar", "responsible": "Neil",
                          "deadline": "2026-07-30", "priority": "high", "status": "open"}],
    }
    t.update(over)
    return t


def test_minutes_maps_topics_and_action_owner_from_responsible():
    art = _artifact(content={"date": "2026-07-25", "title": "T", "participants": [],
                             "topics": [_content_topic()], "topic_row_ids": []})
    minutes, _ = worker._content_to_minutes(art)
    assert len(minutes["topics"]) == 1
    mt = minutes["topics"][0]
    assert mt["topic_title"] == "Slab pour"
    assert mt["summary"] == "Poured the slab."
    ai = mt["action_items"][0]
    assert ai["owner"] == "Neil"          # our `responsible` -> the doc's `owner`
    assert ai["action"] == "Fix rebar"
    assert ai["deadline"] == "2026-07-30"
    assert ai["priority"] == "high"


def test_minutes_uses_user_confirmed_title_and_attendees():
    art = _artifact(title="Morning inspection", attendees=["Ben", "Neil", "James"])
    minutes, title = worker._content_to_minutes(art)
    assert title == "Morning inspection"
    assert minutes["attendees"] == ["Ben", "Neil", "James"]


def test_minutes_folds_user_fields_into_executive_summary():
    art = _artifact(fields={"weather": "Fine, 18C", "sign_off": "A. Lovelace"})
    minutes, _ = worker._content_to_minutes(art)
    joined = " | ".join(minutes["executive_summary"])
    assert "Weather: Fine, 18C" in joined
    assert "Sign Off: A. Lovelace" in joined       # any declared field renders, no per-field code


def test_minutes_meeting_date_from_content():
    art = _artifact(content={"date": "2026-07-25", "title": "T", "participants": [],
                             "topics": [], "topic_row_ids": []})
    minutes, _ = worker._content_to_minutes(art)
    assert minutes["meeting_date"] == "2026-07-25"


def test_minutes_skips_empty_user_fields():
    art = _artifact(fields={"weather": "", "notes": None, "sign_off": "Sam"})
    minutes, _ = worker._content_to_minutes(art)
    joined = " ".join(minutes.get("executive_summary", []))
    assert "Sign Off: Sam" in joined
    assert "Weather" not in joined and "Notes" not in joined   # empty/None dropped
