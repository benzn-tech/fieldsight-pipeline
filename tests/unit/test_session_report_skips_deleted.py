"""A session deleted before its report is rendered must not be emailed either.

The last surface in the deletion enumeration still carrying the defect that was
fixed in `lambda_session_finalize` one lambda over. `GET /report/status` stops
the POLL from serving a removed session, but this worker is S3-triggered: if the
recording is deleted between org-api's enqueue and the worker running -- or on an
S3 event redelivery hours later -- the DOCX is written and the email goes out.

Same posture as finalize and for the same reason: LENIENT. An unreadable mirror
must not cost a requester the report they asked for, and this worker records
errors rather than retrying them.
"""
import pytest

rep = pytest.importorskip("lambda_session_report", reason="requires boto3 (installed in CI)")
import deletion_mirror  # noqa: E402


SID = "c" * 32
ARTIFACT = {"resultKey": "session_report_results/r-1.json", "requestId": "r-1",
            "folder": "Ben_UCPK2", "date": "2026-08-27", "sessionId": SID,
            "deliver": "email", "recipient": "ben@example.nz",
            "content": {"date": "2026-08-27", "topics": []}}


def _run(monkeypatch, deleted, raises=False):
    sent, written, put = [], [], []

    def fake_deleted(s3, bucket, folder, date, strict=False):
        if raises:
            raise RuntimeError("mirror unreadable")
        return set(deleted)

    monkeypatch.setattr(deletion_mirror, "deleted_sessions", fake_deleted)
    monkeypatch.setattr(rep, "_send_email", lambda a: sent.append(a))
    monkeypatch.setattr(rep, "_write_result", lambda k, p: written.append(p))
    monkeypatch.setattr(rep, "_content_to_minutes", lambda a: ({}, "A report"))
    monkeypatch.setattr(rep, "generate_word_document", lambda m, t: __import__("io").BytesIO(b"x"))
    monkeypatch.setattr(rep, "s3", lambda: type("S", (), {
        "put_object": staticmethod(lambda **kw: put.append(kw["Key"]))})())

    rep.process_request(dict(ARTIFACT))
    return sent, written, put


def test_a_deleted_session_is_not_rendered_or_emailed(monkeypatch):
    sent, written, put = _run(monkeypatch, deleted={"sid" + SID})

    assert sent == [], "the report email went out for a deleted recording"
    assert put == [], "a DOCX of a deleted session was written to S3"
    assert written and written[0]["status"] == "skipped"


def test_the_result_is_still_written(monkeypatch):
    """The caller polls `resultKey`. A skip that writes nothing leaves the poll
    spinning forever, which is a different bug wearing this fix's clothes."""
    _, written, _ = _run(monkeypatch, deleted={"sid" + SID})
    assert written, "nothing recorded -- the requester's poll never resolves"
    assert "deleted" in written[0].get("reason", "")


def test_the_bare_hex_spelling_is_matched(monkeypatch):
    sent, _, _ = _run(monkeypatch, deleted={SID})
    assert sent == []


def test_another_sessions_deletion_does_not_suppress_this_report(monkeypatch):
    sent, written, put = _run(monkeypatch, deleted={"sid" + "d" * 32})
    assert len(sent) == 1
    assert put and written[0]["status"] == "done"


def test_no_deletions_renders_as_before(monkeypatch):
    sent, written, put = _run(monkeypatch, deleted=set())
    assert len(sent) == 1
    assert written[0]["status"] == "done"


def test_an_unreadable_mirror_still_renders(monkeypatch):
    """LENIENT, like finalize. A failed check must not cost a requester the
    report they asked for, and this worker records errors instead of retrying."""
    sent, written, _ = _run(monkeypatch, deleted=set(), raises=True)
    assert len(sent) == 1
    assert written[0]["status"] == "done"
