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


def test_body_contains_summary_site_and_open_todos():
    _subj, text, html = fin.build_confirmation_email(
        date="2026-07-25", site_name="UC PK",
        summary="Poured the slab; discussed rebar.",
        open_todos=[{"text": "Fix rebar spacing", "responsible": "Neil"},
                    {"text": "Order more steel", "responsible": None}])
    assert "Poured the slab" in text
    assert "Fix rebar spacing" in text and "Neil" in text
    assert "Order more steel" in text
    assert "UC PK" in text
    assert "Fix rebar spacing" in html and "Poured the slab" in html   # html carries the same


def test_no_action_items_section_when_none_open():
    _s, text, _h = fin.build_confirmation_email(date="2026-07-25", summary="All done.", open_todos=[])
    assert "action item" not in text.lower()


def test_blank_summary_renders_a_placeholder_not_an_empty_body():
    _s, text, _h = fin.build_confirmation_email(date="2026-07-25", summary="")
    assert text.strip()                       # never an empty email
    assert "summary" in text.lower()


def test_todos_without_text_are_dropped_with_their_owner():
    _s, text, _h = fin.build_confirmation_email(
        summary="x", open_todos=[{"text": "  ", "responsible": "Zed"}, {"text": "real one"}])
    assert "real one" in text
    assert "Zed" not in text                  # the blank-text todo (and its owner) is dropped


def test_html_escapes_content():
    _s, _t, html = fin.build_confirmation_email(summary="rebar <b>bent</b> & rusty")
    assert "&lt;b&gt;" in html and "&amp;" in html   # not raw markup injected into the email


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
    assert "Poured the slab" in text and "fix rebar" in text and "Poured the slab" in html
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
    assert "COMPLETE summary" in text and "STALE" not in text
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
    assert "Rolling summary." in text and "keep me" in text
