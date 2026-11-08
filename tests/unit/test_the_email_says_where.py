"""The confirmation email says WHERE each row was said (Track L Task 7 Step 1,
backend half), behind EMAIL_WHERE_COLUMN -- off in TEST and prod until the
frontend's Preview & copy renders the same column (the header text is literal
and identical on both surfaces; owner, 2026-10-08).

A row's place is `location_locate.locate(markers, row["at"])` -- the day view's
locator, with its 30-minute carry. The column appears only when at least one row
has a place; an unplaced row shows an EMPTY cell ("—" means unassigned, "N/A"
means not a task). With the flag off, or nothing placed, the email is
byte-identical to the one without markers.
"""
import re

import pytest

fin = pytest.importorskip("lambda_session_finalize")

DATE = "2026-11-09"
MARKERS = [{"at": "09:00", "location": "Level  2\n east"},
           {"at": "09:20", "location": "Te Kaha | room"}]
ROWS = [
    {"text": "Strip the forms", "responsible": "Neil", "due": None, "at": "09:05"},
    {"text": "Order ties", "responsible": None, "due": None, "at": "09:25"},
    {"text": "Before anyone said where", "responsible": "Sam", "due": None, "at": "08:40"},
    {"text": "Rebar cover — short on the east side.", "responsible": None, "due": None,
     "at": "09:22", "kind": "topic"},
]


def _email(rows=ROWS, **kw):
    return fin.build_confirmation_email(date=DATE, time_range="09:00–09:40",
                                        site_name="Ellesmere", open_todos=rows, **kw)


def _heads(html):
    return re.findall(r'<th style="([^"]*)"[^>]*>([A-Z ]+)</th>', html)


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv("EMAIL_WHERE_COLUMN", "true")


# ---- off: nothing changes -------------------------------------------------

def test_THE_off_flag_leaves_the_email_byte_identical(monkeypatch):
    monkeypatch.delenv("EMAIL_WHERE_COLUMN", raising=False)
    assert _email(location_markers=MARKERS) == _email()
    monkeypatch.setenv("EMAIL_WHERE_COLUMN", "false")
    assert _email(location_markers=MARKERS) == _email()


def test_on_with_nothing_placed_is_byte_identical(on):
    far = [{"at": "14:00", "location": "Gate"}]       # after every row
    assert _email(location_markers=far) == _email()
    assert _email(location_markers=[]) == _email()
    assert _email(location_markers=None) == _email()


# ---- on: the column -------------------------------------------------------

def test_THE_on_flag_adds_a_where_column_in_both_flavours(on):
    _, text, html = _email(location_markers=MARKERS)
    lines = text.splitlines()
    head = lines.index("| AGENDA ITEM | WHERE | ASSIGNED | DUE DATE |")
    assert lines[head + 1] == "| --- | --- | --- | --- |"
    assert lines[head + 2:head + 6] == [
        "| Strip the forms | Level 2 east | Neil | — |",
        "| Order ties | Te Kaha \\| room | — | — |",
        "| Before anyone said where |  | Sam | — |",
        "| Rebar cover — short on the east side. | Te Kaha \\| room | N/A | N/A |",
    ]
    assert [label for _s, label in _heads(html)] == ["AGENDA ITEM", "WHERE", "ASSIGNED", "DUE DATE"]
    cells = re.findall(r"<tr><td[^>]*>[^<]*</td><td[^>]*>([^<]*)</td>", html)
    assert cells == ["Level 2 east", "Te Kaha | room", "", "Te Kaha | room"]


def test_the_where_header_wears_the_house_style(on):
    _, _, html = _email(location_markers=MARKERS)
    heads = _heads(html)
    assert len(heads) == 4
    for style, _label in heads:
        assert "background:#1F3A5F" in style and "color:#FFFFFF" in style
    assert "width:50%" in heads[0][0] and "width:60%" not in html


def test_the_label_is_escaped_in_html(on):
    _, _, html = _email(rows=[{"text": "x", "at": "09:05"}],
                        location_markers=[{"at": "09:00", "location": "<b>Gate</b>"}])
    assert "&lt;b&gt;Gate&lt;/b&gt;" in html and "<b>Gate</b>" not in html


def test_a_row_without_a_time_has_an_empty_cell(on):
    rows = [{"text": "Placed", "at": "09:05"}, {"text": "No time"}]
    _, text, _ = _email(rows=rows, location_markers=MARKERS)
    assert "| No time |  | — | — |" in text


def test_a_place_older_than_the_carry_is_not_claimed(on):
    """`> 30` minutes: 31 minutes after the marker the row is unplaced, 30 is
    still placed."""
    rows = [{"text": "Thirty", "at": "09:30"}, {"text": "Thirty-one", "at": "09:31"}]
    _, text, _ = _email(rows=rows, location_markers=[{"at": "09:00", "location": "Gate"}])
    assert "| Thirty | Gate | — | — |" in text
    assert "| Thirty-one |  | — | — |" in text


def test_hh_mm_ss_times_are_placed_too(on):
    _, text, _ = _email(rows=[{"text": "Brief task", "at": "09:05:30"}],
                        location_markers=MARKERS)
    assert "| Brief task | Level 2 east | — | — |" in text


# ---- the worker hands the markers through ---------------------------------

def test_the_worker_passes_the_requests_markers(on, monkeypatch):
    monkeypatch.setattr(fin, "_session_was_deleted", lambda a: False)
    sent = []
    art = {"kind": "final", "recipient": "ben@example.nz", "date": DATE, "sessionId": "abc",
           "openTodos": [{"text": "Strip the forms", "responsible": "Neil", "at": "09:05"}],
           "locationMarkers": MARKERS}
    out = fin.process_finalize_request(
        art, send=lambda *a: sent.append(a) or "m-1", write_result=lambda *a: None,
        already_sent=lambda rid: False)
    assert out["status"] == "sent"
    _to, _subject, text, _html = sent[0]
    assert "| Strip the forms | Level 2 east | Neil | — |" in text
