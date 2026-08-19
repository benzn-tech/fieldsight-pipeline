"""Tier-0 auto-finalize confirmation email (voice-timeliness Point 1 / spec
2026-07-27 §Tier-0): when a recording's grace window elapses without a resume, the
recorder gets a short summary of what was captured so they can confirm/correct
before leaving site. This tests the PURE email builder; the version-CAS claim step,
the non-VPC send worker, and the grace one-shot wiring land separately. Pure at
import (no boto3 / env)."""
import lambda_session_finalize as fin


def test_subject_carries_fieldsight_and_the_date():
    subj, _text, _html = fin.build_confirmation_email(date="2026-07-25", summary="Poured the slab.")
    assert "FieldSight" in subj
    assert "2026-07-25" in subj


def test_subject_carries_the_meeting_time_range():
    # so a recorder with several recordings a day can tell WHICH meeting this is
    subj, _t, _h = fin.build_confirmation_email(
        date="2026-07-25", time_range="14:11–14:14", summary="x")
    assert "2026-07-25" in subj and "14:11–14:14" in subj


def test_body_contains_site_and_every_open_todo():
    _subj, text, html = fin.build_confirmation_email(
        date="2026-07-25", site_name="UC PK",
        summary="Poured the slab; discussed rebar.",
        open_todos=[{"text": "Fix rebar spacing", "responsible": "Neil"},
                    {"text": "Order more steel", "responsible": None}])
    assert "Fix rebar spacing" in text and "Neil" in text
    assert "Order more steel" in text                  # an unassigned one is not dropped
    assert "UC PK" in text
    assert "Fix rebar spacing" in html and "Order more steel" in html  # html carries the same


def test_no_action_items_table_when_none_open():
    _s, text, html = fin.build_confirmation_email(date="2026-07-25", summary="All done.",
                                                  open_todos=[])
    assert "Action items" not in text       # the heading -- the explanatory note reads differently
    assert "<table" not in html


def test_a_summary_is_never_echoed_into_the_body_even_when_present():
    _s, text, _h = fin.build_confirmation_email(date="2026-07-25",
                                                summary="Poured the slab.")
    assert text.strip()                       # never an empty email
    assert "Poured the slab" not in text
    assert "summary" not in text.lower()      # not even the old placeholder wording


def test_todos_without_text_are_dropped_with_their_owner():
    _s, text, _h = fin.build_confirmation_email(
        summary="x", open_todos=[{"text": "  ", "responsible": "Zed"}, {"text": "real one"}])
    assert "real one" in text
    assert "Zed" not in text                  # the blank-text todo (and its owner) is dropped


def test_html_escapes_content():
    # proven on the field that IS rendered now -- the to-do text
    _s, _t, html = fin.build_confirmation_email(
        open_todos=[{"text": "rebar <b>bent</b> & rusty", "responsible": "A & B"}])
    assert "&lt;b&gt;" in html and "&amp;" in html   # not raw markup injected into the email
    assert "<b>bent</b>" not in html


def test_action_items_render_task_assignee_and_due():
    _s, text, html = fin.build_confirmation_email(
        summary="x", open_todos=[{"text": "Order steel", "responsible": "Neil", "due": "Friday"}])
    assert "Order steel" in text and "Neil" in text and "Friday" in text
    assert "Task" in html and "Assignee" in html and "Due" in html   # structured columns
    assert "Order steel" in html and "Neil" in html and "Friday" in html


def test_action_item_missing_assignee_or_due_shows_placeholders():
    _s, text, html = fin.build_confirmation_email(
        summary="x", open_todos=[{"text": "Do it", "responsible": None, "due": None}])
    assert "Do it" in text and "Unassigned" in text   # text: assignee falls back to Unassigned
    assert "—" in html                                # html: em-dash for missing assignee/due


# ---- process_finalize_request (non-VPC send worker) ---------------------

def _art(**over):
    a = {"recipient": "bob@site.com", "date": "2026-07-25", "siteName": "UC PK",
         "summary": "Poured the slab.", "sessionId": "abc",
         "openTodos": [{"text": "fix rebar", "responsible": "Neil"}]}
    a.update(over)
    return a


def test_worker_builds_sends_and_records_a_sent_result():
    sent, results = [], []
    out = fin.process_finalize_request(
        _art(), send=lambda *a: sent.append(a) or "msg-1",
        write_result=lambda sid, payload: results.append((sid, payload)))
    assert out["status"] == "sent" and out["recipient"] == "bob@site.com"
    to, subject, text, html = sent[0]
    assert to == "bob@site.com" and "2026-07-25" in subject
    assert "fix rebar" in text and "fix rebar" in html
    # the in-VPC sweep reconciles this result -> mark_sent
    assert results == [("abc", {"status": "sent", "sessionId": "abc", "recipient": "bob@site.com"})]


def test_worker_records_an_error_result_when_the_send_fails():
    results = []

    def _boom(*a):
        raise RuntimeError("ses down")

    out = fin.process_finalize_request(
        _art(sessionId="s9"), send=_boom,
        write_result=lambda sid, payload: results.append((sid, payload)))
    assert out["status"] == "error"          # recorded, NOT re-raised (no S3 retry-storm / double-send)
    sid, payload = results[0]
    assert sid == "s9" and payload["status"] == "error" and "ses down" in payload["error"]


def test_worker_skips_a_request_with_no_recipient():
    sent, results = [], []
    out = fin.process_finalize_request({"recipient": "", "summary": "x"},
                                       send=lambda *a: sent.append(a),
                                       write_result=lambda *a: results.append(a))
    assert out["status"] == "skipped" and sent == [] and results == []


def test_worker_prefers_the_fresh_complete_summary_over_the_stale_rolling_one():
    sent = []
    art = _art(summary="STALE partial summary",
               openTodos=[{"text": "old", "responsible": None, "due": None}])
    fresh = {"summary": "COMPLETE summary",
             "open_todos": [{"text": "new task", "responsible": "Ana", "due": "Mon"}]}
    out = fin.process_finalize_request(
        art, send=lambda *a: sent.append(a) or "m",
        write_result=lambda *a: None, complete_summary=lambda artifact: fresh)
    assert out["status"] == "sent"
    _to, _subj, text, html = sent[0]
    # the fresh to-dos travel with the fresh summary -- they are the observable half
    assert "new task" in text and "Ana" in text and "Mon" in text and "old" not in text
    assert "new task" in html and "Mon" in html


def test_worker_falls_back_to_rolling_summary_when_resummary_returns_none():
    sent = []
    art = _art(summary="Rolling summary.",
               openTodos=[{"text": "keep me", "responsible": None, "due": None}])
    fin.process_finalize_request(
        art, send=lambda *a: sent.append(a) or "m",
        write_result=lambda *a: None, complete_summary=lambda artifact: None)
    _to, _subj, text, _html = sent[0]
    assert "keep me" in text          # the rolling to-dos were used, not dropped


# ----------------------------------------------------------
# Email shape, 2026-08-10: the recorder wants the ACTION TABLE, not prose.
# The narrative Summary is dropped from the body, and the meeting's time range
# moves onto the Date line so the email states WHEN inside the body (it was
# only in the subject before).
# ----------------------------------------------------------

def test_date_line_carries_the_meeting_time_range():
    _s, text, html = fin.build_confirmation_email(
        date="2026-07-25", time_range="14:11–14:14", summary="x")
    assert "Date: 2026-07-25 14:11–14:14" in text
    assert "2026-07-25 14:11–14:14" in html


def test_date_line_without_a_time_range_is_just_the_date():
    _s, text, _h = fin.build_confirmation_email(date="2026-07-25", summary="x")
    assert "Date: 2026-07-25\n" in text


def test_summary_prose_is_not_rendered_in_the_body():
    _s, text, html = fin.build_confirmation_email(
        date="2026-07-25", summary="Poured the slab; discussed rebar.",
        open_todos=[{"text": "Fix rebar spacing", "responsible": "Neil"}])
    assert "Poured the slab" not in text and "Poured the slab" not in html
    assert "Summary" not in text and "Summary" not in html
    assert "Fix rebar spacing" in text and "Fix rebar spacing" in html


def test_a_recording_with_no_action_items_says_so_rather_than_sending_a_blank():
    """With the prose gone, a todo-less session would otherwise be an email with
    a header and nothing else -- indistinguishable from a broken send."""
    _s, text, html = fin.build_confirmation_email(
        date="2026-07-25", summary="All done.", open_todos=[])
    assert "No action items" in text and "No action items" in html


# --- SESSION_BRIEF: which summariser the finalize re-summary uses ------------
# The brief returns the same {summary, open_todos} the email already reads, so
# the switch must be invisible to everything below it. These pin that, and pin
# that storing the brief can never cost the recorder their email.

def _artifact():
    return {"folder": "Ben_Test", "date": "2026-08-19", "sessionId": "abc"}


def test_the_flag_is_off_by_default_so_nothing_changes_until_it_is_set():
    assert fin.SESSION_BRIEF is False


def test_an_injected_summariser_still_wins_over_the_flag(monkeypatch):
    # The caller's injection point is what the existing tests use; adding a flag
    # must not quietly take it away.
    monkeypatch.setattr(fin, "SESSION_BRIEF", True, raising=False)
    called = {}

    def fake(turns):
        called["yes"] = True
        return {"summary": "injected", "open_todos": []}

    monkeypatch.setattr(fin, "_complete_summary",
                        lambda a, summarize=None: fake(["t"]), raising=False)
    out = fin.process_finalize_request(
        {**_artifact(), "recipient": "a@b.c"},
        send=lambda *a, **k: None, write_result=lambda *a, **k: None)
    assert out["status"] == "sent" and called


def test_a_brief_that_cannot_be_stored_still_sends_the_email(monkeypatch, caplog):
    # Best-effort by design: S3 is not on the path between the recorder and
    # their confirmation.
    def boom(*a, **k):
        raise RuntimeError("s3 down")

    monkeypatch.setattr(fin, "_store_brief", boom, raising=False)
    sent = {}
    out = fin.process_finalize_request(
        {**_artifact(), "recipient": "a@b.c", "summary": "s", "openTodos": []},
        send=lambda *a, **k: sent.setdefault("to", a[0]),
        write_result=lambda *a, **k: None,
        complete_summary=lambda artifact: None)
    assert out["status"] == "sent" and sent["to"] == "a@b.c"


def test_store_brief_swallows_its_own_failure():
    # Called directly: no bucket configured, so the write fails. It must not
    # raise into the worker.
    fin._store_brief("Ben_Test", "2026-08-19", "abc", {"headline": "x", "sections": []})
