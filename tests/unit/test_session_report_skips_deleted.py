"""A session deleted before its report is rendered must not be emailed either.

The last surface in the deletion enumeration still carrying the defect that was
fixed in `lambda_session_finalize` one lambda over. `GET /report/status` stops
the POLL from serving a removed session, but this worker is S3-triggered: if the
recording is deleted between org-api's enqueue and the worker running -- or on an
S3 event redelivery hours later -- the DOCX is written and the email goes out.

Owner decision (2026-09-16): a mirror-read failure on THIS path now fails
closed rather than proceeding as if nothing was deleted -- a permission fault
here must not risk mailing a removed recording. `lambda_session_finalize`
keeps its lenient posture; this decision was scoped to the report worker.
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


def test_an_unreadable_mirror_fails_closed(monkeypatch):
    """STRICT (owner decision 2026-09-16). The old lenient posture let an
    unreadable mirror still render and mail -- risking a removed recording
    reaching a document and an inbox. It must now abort: no doc, no email,
    and a `status: error` result the requester's poll can see and retry."""
    sent, written, put = _run(monkeypatch, deleted=set(), raises=True)
    assert sent == [], "an unreadable mirror still sent the email"
    assert put == [], "an unreadable mirror still wrote a DOCX"
    assert written and written[0]["status"] == "error"


def test_a_generate_request_also_fails_closed_on_an_unreadable_mirror(monkeypatch):
    """Pins the rebase trap: on the OLD develop the deletion check ran BEFORE the
    single `try:` block, so `_generate_document` was the only other path in
    `process_request`. On the NEW develop (PR #859), `if artifact.get("generate")`
    is a SECOND path with its own try/except, sitting between the deletion check
    and the old assemble-and-render try. A naive conflict resolution that moves
    the deletion check back inside the OLD try leaves this second path with no
    deletion check at all -- a deleted recording could be generated into a
    document and mailed, exactly the gap this branch exists to close.

    A `generate` artifact whose mirror read raises must therefore still end as
    `status: "error"`, with `_generate_document` and `_put_document` never
    reached, and no document written or emailed."""
    generated, put_doc = [], []
    monkeypatch.setattr(rep, "_generate_document",
                        lambda *a, **k: generated.append(1) or (None, None))
    monkeypatch.setattr(rep, "_put_document",
                        lambda *a, **k: put_doc.append(1) or "session_reports/x.docx")

    def fake_deleted(s3, bucket, folder, date, strict=False):
        raise RuntimeError("mirror unreadable")
    monkeypatch.setattr(deletion_mirror, "deleted_sessions", fake_deleted)

    sent, written, put = [], [], []
    monkeypatch.setattr(rep, "_send_email", lambda a: sent.append(a))
    monkeypatch.setattr(rep, "_write_result", lambda k, p: written.append(p))
    monkeypatch.setattr(rep, "s3", lambda: type("S", (), {
        "put_object": staticmethod(lambda **kw: put.append(kw["Key"]))})())

    art = dict(ARTIFACT, generate={"templateId": "personal-meeting", "templateVersion": 1})
    rep.process_request(art)

    assert generated == [], "the generate path ran despite an unreadable mirror"
    assert put_doc == [], "a generated document was put to S3"
    assert sent == [], "an unreadable mirror still sent the email"
    assert put == [], "an unreadable mirror still wrote a DOCX"
    assert written and written[0]["status"] == "error"
