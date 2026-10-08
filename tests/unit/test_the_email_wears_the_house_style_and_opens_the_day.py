"""The confirmation email looks like the reports it announces, and opens the day.

Owner, 2026-10-08: "深蓝表头记着用白字" -- the table header is navy with WHITE
text, as on every DOCX report -- and an "Open in FieldSight" link to that day's
Timeline, built so a phone app can take it over later.
"""
import os
import re

import pytest

fin = pytest.importorskip("lambda_session_finalize")

ROWS = [{"text": "Release agent on the deck", "responsible": "formwork crew", "due": "tomorrow"},
        {"text": "Spare vibrator", "responsible": None, "due": None}]


def _email(**kw):
    return fin.build_confirmation_email(date="2026-10-08", time_range="11:36–11:42",
                                        site_name="Level 2", open_todos=ROWS, **kw)


def test_THE_the_table_header_is_navy_with_white_text():
    _, _, html = _email()
    heads = re.findall(r'<th style="([^"]*)"[^>]*>(AGENDA ITEM|ASSIGNED|DUE DATE)</th>', html)
    assert len(heads) == 3
    for style, _label in heads:
        assert "background:#1F3A5F" in style and "color:#FFFFFF" in style


def test_the_date_reads_in_words():
    _, text, html = _email()
    assert "Thursday 8 October 2026 · 11:36–11:42" in html
    assert "Date: 2026-10-08 11:36–11:42" in text, "the plain-text flavour is unchanged"


def test_the_link_is_in_both_flavours_when_there_is_one():
    url = "https://dev.example.amplifyapp.com/#/timeline?date=2026-10-08"
    _, text, html = _email(open_url=url)
    assert text.rstrip().endswith("Open in FieldSight: " + url)
    assert 'href="https://dev.example.amplifyapp.com/#/timeline?date=2026-10-08"' in html
    assert ">Open in FieldSight</a>" in html


def test_no_link_no_button():
    _, text, html = _email()
    assert "Open in FieldSight" not in text and "Open in FieldSight</a>" not in html


def test_the_link_is_per_stage_and_absent_when_unset(monkeypatch):
    monkeypatch.setenv("APP_URL", "https://main.example.amplifyapp.com/")
    assert fin.open_link("2026-10-08") == "https://main.example.amplifyapp.com/#/timeline?date=2026-10-08"
    assert fin.open_link("2026-10-08&user=x") is None, "only a date goes into the URL"
    assert fin.open_link(None) is None
    monkeypatch.delenv("APP_URL")
    assert fin.open_link("2026-10-08") is None, "unset is no link, never another stage's"


def test_the_template_gives_each_stage_its_own_app():
    tpl = open(os.path.join(os.path.dirname(fin.__file__), "template.yaml"), encoding="utf-8").read()
    fn = tpl[tpl.index("  SessionFinalizeFunction:"):]
    fn = fn[:6000]
    assert "APP_URL: !If [IsProd, 'https://main.d2fssznicvuckr.amplifyapp.com', " \
           "'https://dev.d2fssznicvuckr.amplifyapp.com']" in fn


def test_an_iso_due_date_reads_as_a_day_in_the_card_only():
    _, text, html = fin.build_confirmation_email(
        date="2026-10-08", open_todos=[{"text": "Blow out the deck", "responsible": "Mike",
                                        "due": "2026-10-14"},
                                       {"text": "Book the pump", "due": "Friday"}])
    assert ">Wed 14 Oct</td>" in html and ">Friday</td>" in html
    assert "| Blow out the deck | Mike | 2026-10-14 |" in text, "the text table is the frontend's contract"


def test_a_check_inside_one_minute_shows_the_minute_once():
    lines = fin._check_lines([{"check": "steel", "from": "11:39", "to": "11:39"}])
    assert "(11:39)" in lines[0] and "11:39–11:39" not in lines[0]


def test_the_email_does_not_ask_for_a_reply():
    """Owner, 2026-10-09: a reply reaches nobody who can act on it."""
    _, text, html = _email()
    assert "reply" not in text.lower() and "reply" not in html.lower()
    assert "open FieldSight to correct anything before you leave site" in text
