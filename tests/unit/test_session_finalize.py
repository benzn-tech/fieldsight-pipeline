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
