"""One table. Things owed first, things merely discussed underneath.

The confirmation email rendered ONLY action items, so a session where nobody
promised anything arrived as a header and one line saying nothing was captured --
while the recording plainly had content. That is the ordinary case, not an edge
one: 11 of 16 measured prod sessions produced no action item at all, and the
recording the owner made on 2026-09-17 was one of them.

The owner's shape, chosen over the two-table alternative: keep ONE table, put the
topics that produced no task into it as rows, sink them below the real tasks, and
show their owner and due date as N/A. The columns are AGENDA ITEM / ASSIGNED /
DUE DATE (handoff-sync plan §2.4), because a row is now either something someone
owes or something the meeting covered, and there is no separate "Items" heading
above the table any more -- the header row carries it.

N/A IS NOT THE EM DASH. An unassigned action shows "—" and means "nobody has
picked this up yet"; a topic row shows "N/A" and means "there is no task here".
Collapsing them would invite someone to adopt a row that was never a task.
"""
import pytest

fin = pytest.importorskip("lambda_session_finalize", reason="requires boto3 (installed in CI)")
iw = pytest.importorskip("lambda_item_writer", reason="requires psycopg (installed in CI)")
fc = pytest.importorskip("lambda_finalize_claim", reason="requires psycopg (installed in CI)")

ACTION = {"text": "Re-inspect the cylinder", "responsible": "Neil", "due": "2026-09-18"}
UNOWNED = {"text": "Chase the supplier", "responsible": None, "due": None}
TOPIC = {"text": "Voiceprint test — recorded outdoors in site noise.",
         "responsible": None, "due": None, "kind": "topic"}


def _email(rows):
    return fin.build_confirmation_email(date="2026-09-17", time_range="13:05–13:07",
                                        site_name="Test Project", open_todos=rows)


# ---- the table ------------------------------------------------------------

def test_THE_test_a_session_with_no_tasks_still_says_what_it_was_about():
    """The 2026-09-17 recording: one topic, zero action items."""
    _subject, text, html = _email([TOPIC])
    assert "Voiceprint test" in text and "Voiceprint test" in html
    assert "Nothing was captured" not in text
    assert "<table" in html, "a topic-only session gets the table, not a bare note"


def test_tasks_come_first_and_topics_sink():
    _subject, text, _html = _email([TOPIC, ACTION])
    assert text.index("Re-inspect the cylinder") < text.index("Voiceprint test")


def test_a_topic_is_N_A_and_an_unowned_task_is_a_dash():
    _subject, text, html = _email([UNOWNED, TOPIC])
    assert "N/A" in text and "N/A" in html
    row = html[html.index("Voiceprint test"):]
    assert row.count("N/A") >= 2, "both the owner and the due cell of a topic row"
    unowned = html[html.index("Chase the supplier"):html.index("Voiceprint test")]
    assert "N/A" not in unowned, "an unassigned task is not N/A -- someone should take it"
    assert "—" in unowned


def test_the_table_header_is_agenda_item_assigned_due_date():
    _subject, text, html = _email([ACTION])
    assert "AGENDA ITEM | ASSIGNED | DUE DATE" in text
    assert "<h3>Items</h3>" not in html, "the header row carries the title now"
    assert ">AGENDA ITEM</th>" in html and ">ASSIGNED</th>" in html and ">DUE DATE</th>" in html
    assert "Action items" not in text and "Action items" not in html


def test_the_text_table_escapes_pipes_and_collapses_newlines():
    """Plan §1.7: the plain-text flavour is a pipe table; a cell's own `|` or
    embedded newline would otherwise be read as a column boundary / a new row.
    Leading/trailing pipes and the escaping shape are pinned byte-for-byte
    against the frontend's `cell()`/`pipe()` in
    fieldsight-ui/scripts/composites/email-preview-modal.js (fix round on
    3237e56, finding 2) -- that file is the other half of this contract."""
    _subject, text, _html = _email(
        [{"text": "Cut 2400 | 1200 sheet\nsecond line", "responsible": "Sam\nSmith",
         "due": "Mon\nTue"}])
    row = next(ln for ln in text.splitlines() if "Cut 2400" in ln)
    assert "\n" not in row
    assert row == r"| Cut 2400 \| 1200 sheet second line | Sam Smith | Mon Tue |"


def test_the_text_table_header_and_separator_match_the_frontend_exactly():
    """Pins the two fixed lines byte-for-byte against
    fieldsight-ui/scripts/composites/email-preview-modal.js's `COLUMNS` +
    `pipe(COLUMNS)` / `'| --- | --- | --- |'` (renderEmailText). If either
    side's literal changes without the other, this goes red rather than the
    two surfaces silently drifting -- exactly what the owner asked the two
    repos to stay in sync on."""
    _subject, text, _html = _email([ACTION])
    lines = text.splitlines()
    assert "| AGENDA ITEM | ASSIGNED | DUE DATE |" in lines
    assert "| --- | --- | --- |" in lines
    header_idx = lines.index("| AGENDA ITEM | ASSIGNED | DUE DATE |")
    assert lines[header_idx + 1] == "| --- | --- | --- |"


def test_an_empty_recording_still_says_so():
    _subject, text, html = _email([])
    assert "Nothing was captured" in text and "Nothing was captured" in html
    assert "<table" not in html


def test_a_row_with_no_kind_is_a_task():
    """Everything already in flight -- the rolling summariser's to-dos, a brief's,
    a group's -- carries no `kind`, and must keep rendering as a task."""
    _subject, text, _html = _email([{"text": "Fix rebar", "responsible": "Sam"}])
    assert "Fix rebar | Sam | —" in text and "N/A" not in text


# ---- who builds the rows --------------------------------------------------

def test_a_topic_that_produced_a_task_is_not_listed_twice():
    art = {"topics": [
        {"topic_title": "Cylinder testing", "summary": "It was tested.",
         "action_items": [{"action": "Re-inspect", "responsible": "Neil"}]},
        {"topic_title": "Voiceprint test", "summary": "Recorded outdoors.",
         "action_items": []}]}
    rows = iw._topic_rows(art)
    assert [r["text"] for r in rows] == ["Voiceprint test — Recorded outdoors."]


def test_a_topic_row_carries_the_first_sentence_only():
    art = {"topics": [{"topic_title": "Voiceprint test",
                       "summary": "Recorded outdoors in site noise. "
                                  "The purpose was to check recall against the pipeline.",
                       "action_items": []}]}
    text = iw._topic_rows(art)[0]["text"]
    assert text == "Voiceprint test — Recorded outdoors in site noise."


def test_a_long_topic_row_is_trimmed_not_dropped():
    art = {"topics": [{"topic_title": "T", "summary": "x" * 400, "action_items": []}]}
    text = iw._topic_rows(art)[0]["text"]
    assert len(text) <= iw.TOPIC_ROW_MAX_CHARS and text.endswith("…")


def test_a_topic_with_no_words_at_all_is_not_a_row():
    art = {"topics": [{"topic_title": "  ", "summary": "", "action_items": []}]}
    assert iw._topic_rows(art) == []


def test_the_backstop_email_says_what_the_session_was_about_too():
    """Otherwise the two paths send visibly different emails for the same kind of
    recording, and a reader learns to trust neither."""
    row = fc._summary_row({"summary": "Speaker tested the voiceprint outdoors. "
                                      "Then discussed recall."})
    assert row == [{"text": "Speaker tested the voiceprint outdoors.",
                    "responsible": None, "due": None, "kind": "topic"}]


def test_no_rolling_summary_means_no_row():
    assert fc._summary_row({}) == [] and fc._summary_row({"summary": "   "}) == []
