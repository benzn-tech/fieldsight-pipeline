"""A recording deleted before its confirmation email goes out must not be emailed.

There is no coordination between the two paths at all: `lambda_session_finalize`
and `lambda_finalize_claim` contain no deletion check of any kind, and
`delete_recordings_endpoint` does not touch `meeting_session` or the enqueued
request. So a finalize processed after a delete rebuilds the summary from
`gather_session_segments`, which does not filter either, and mails it.

The window is narrow but real: the recordings list is populated at upload, well
before finalize runs, and the sweep re-drives a finalize that failed once.

SEVERITY, stated so it is not over- or under-read. The recipient is the RECORDER
-- the person who deleted it. This is not a disclosure to a third party; it is
the product telling someone their recording was removed and then emailing it
back. That is a promise broken, not a breach, and it is why the mirror is read
LENIENTLY here: an unreadable mirror must not cost a legitimate recorder their
only confirmation, and this worker records failures rather than retrying them.
"""
import pytest

fin = pytest.importorskip("lambda_session_finalize", reason="requires boto3 (installed in CI)")
import deletion_mirror  # noqa: E402


SID = "a" * 32
ARTIFACT = {"recipient": "ben@example.nz", "folder": "Ben_UCPK2", "date": "2026-08-27",
            "sessionId": SID, "siteName": "Ellesmere",
            "summary": "Doors on floors 1-3 by Tuesday.", "openTodos": ["Re-inspect"]}


def _run(monkeypatch, deleted, raises=False):
    sent, written = [], []

    def fake_deleted(s3, bucket, folder, date, strict=False):
        if raises:
            raise RuntimeError("mirror unreadable")
        return set(deleted)

    monkeypatch.setattr(deletion_mirror, "deleted_sessions", fake_deleted)
    monkeypatch.setattr(fin, "_complete_summary",
                        lambda a: {"summary": "fresh", "open_todos": []})
    return fin.process_finalize_request(
        ARTIFACT,
        send=lambda *a, **kw: sent.append(a) or {"MessageId": "m-1"},
        write_result=lambda sid, payload: written.append(payload),
    ), sent, written


def test_a_deleted_session_is_not_emailed(monkeypatch):
    result, sent, written = _run(monkeypatch, deleted={"sid" + SID})

    assert sent == [], "the confirmation email went out for a deleted recording"
    assert result["status"] == "skipped"
    assert "deleted" in result["reason"]


def test_the_outcome_is_still_recorded(monkeypatch):
    """The in-VPC sweep reconciles `finalizing` from this file. A skip that
    writes nothing leaves the session stuck in `finalizing` forever, which is a
    different bug wearing this fix's clothes."""
    _, _, written = _run(monkeypatch, deleted={"sid" + SID})

    assert written, "nothing was recorded -- the sweep will never move this session"
    assert written[0]["status"] == "skipped"


def test_the_bare_hex_spelling_is_matched(monkeypatch):
    """The mirror carries whatever `sessionBase` the delete endpoint had."""
    _, sent, _ = _run(monkeypatch, deleted={SID})
    assert sent == []


def test_another_sessions_deletion_does_not_suppress_this_email(monkeypatch):
    result, sent, _ = _run(monkeypatch, deleted={"sid" + "b" * 32})
    assert len(sent) == 1
    assert result["status"] != "skipped"


def test_no_deletions_at_all_sends_as_before(monkeypatch):
    result, sent, _ = _run(monkeypatch, deleted=set())
    assert len(sent) == 1
    assert result["status"] != "skipped"


def test_an_unreadable_mirror_still_sends(monkeypatch):
    """LENIENT, unlike the brief endpoint, and the asymmetry is the point.

    There a failed check costs one reader one refresh. Here it costs a recorder
    their only confirmation for a session that was probably never deleted, and
    this worker records failures instead of retrying them -- so failing closed
    would lose the email permanently. Logged loudly instead.
    """
    result, sent, _ = _run(monkeypatch, deleted=set(), raises=True)
    assert len(sent) == 1
    assert result["status"] != "skipped"
