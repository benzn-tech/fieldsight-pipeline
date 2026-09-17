"""The session-report worker renders a day-scoped request safely.

A day bundles several sessions, possibly including a multi-device meeting whose
tombstone lives in the LEAD's mirror. Every session is checked against the mirror
of every folder involved; the document key uses a `day/` segment; the result
carries the scope so the status route can re-check before presigning.
Spec 2026-09-15 §9 step 3, finding F7.
"""
import io

import pytest

rep = pytest.importorskip("lambda_session_report", reason="requires boto3 (installed in CI)")
import deletion_mirror  # noqa: E402

A = "a" * 32
B = "b" * 32


def _day(**over):
    a = {"scope": "day", "requestId": "r-1", "date": "2026-09-10", "folder": "James_Lamb",
         "sessionIds": ["sid" + A, "grp" + B], "mirrorFolders": ["James_Lamb", "Lead_F"],
         "deliver": "email", "recipients": ["x@example.nz"],
         "content": {"date": "2026-09-10", "topics": []},
         "resultKey": "session_report_results/James_Lamb/2026-09-10/day/r-1.json"}
    a.update(over)
    return a


def _run(monkeypatch, artifact, deleted_by_folder=None):
    sent, written, put, asked = [], [], [], []

    def fake_deleted(s3, bucket, folder, date, strict=False):
        asked.append(folder)
        return set((deleted_by_folder or {}).get(folder, set()))

    monkeypatch.setattr(deletion_mirror, "deleted_sessions", fake_deleted)
    monkeypatch.setattr(rep, "_send_email", lambda a: sent.append(a))
    monkeypatch.setattr(rep, "_write_result", lambda k, p: written.append((k, p)))
    monkeypatch.setattr(rep, "_content_to_minutes", lambda a: ({}, "A day"))
    monkeypatch.setattr(rep, "generate_word_document", lambda m, t: io.BytesIO(b"x"))
    monkeypatch.setattr(rep, "s3", lambda: type("S", (), {
        "put_object": staticmethod(lambda **kw: put.append(kw["Key"]))})())
    rep.process_request(artifact)
    return sent, written, put, asked


def test_a_day_document_is_keyed_under_day():
    assert rep._doc_key(_day()) == "session_reports/James_Lamb/2026-09-10/day/r-1.docx"


def test_a_day_with_no_deletions_renders_and_records_its_scope(monkeypatch):
    sent, written, put, _ = _run(monkeypatch, _day())
    assert put == ["session_reports/James_Lamb/2026-09-10/day/r-1.docx"]
    assert len(sent) == 1
    key, payload = written[-1]
    assert key == "session_report_results/James_Lamb/2026-09-10/day/r-1.json"
    assert payload["status"] == "done"
    assert payload["scope"] == "day"
    assert payload["sessionIds"] == ["sid" + A, "grp" + B]
    assert payload["mirrorFolders"] == ["James_Lamb", "Lead_F"]


def test_one_deleted_session_stops_the_whole_day(monkeypatch):
    sent, written, put, _ = _run(monkeypatch, _day(), {"James_Lamb": {"sid" + A}})
    assert sent == [] and put == []
    assert written[-1][1]["status"] == "skipped"


def test_a_merged_meeting_deleted_in_the_leads_mirror_stops_the_day(monkeypatch):
    sent, written, put, asked = _run(monkeypatch, _day(), {"Lead_F": {"grp" + B}})
    assert "Lead_F" in asked, "the lead's mirror was never read"
    assert sent == [] and put == []
    assert written[-1][1]["status"] == "skipped"


def test_the_bare_spelling_in_a_mirror_still_matches(monkeypatch):
    sent, _, put, _ = _run(monkeypatch, _day(), {"James_Lamb": {A}})
    assert sent == [] and put == []


def test_a_day_request_without_session_ids_is_an_error_not_a_render(monkeypatch):
    sent, written, put, asked = _run(monkeypatch, _day(sessionIds=[]))
    assert sent == [] and put == [] and asked == []
    assert written[-1][1]["status"] == "error"


def test_a_session_request_result_is_unchanged(monkeypatch):
    sid = "c" * 32
    artifact = {"resultKey": "session_report_results/r-2.json", "requestId": "r-2",
                "folder": "Ben_UCPK2", "date": "2026-08-27", "sessionId": sid,
                "deliver": "download", "content": {"date": "2026-08-27", "topics": []}}
    _, written, _, _ = _run(monkeypatch, artifact)
    payload = written[-1][1]
    assert payload["status"] == "done"
    assert "scope" not in payload and "sessionIds" not in payload
