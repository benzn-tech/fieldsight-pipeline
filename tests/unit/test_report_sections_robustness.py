"""Unit: a model's output cannot cost a night's reports.

`report_sections.build` runs INSIDE the per-user loop in
`lambda_report_generator`, BEFORE the S3 write, and that loop has no per-user
try/except. So an exception here does not lose one section — it loses the
report, the Word document and the item rows for that user AND for every user
sorted after them, and fails the whole nightly invocation.

Every shape below was found by driving the real module. Before these, a model
returning `"key_decisions": [["Use Teams", "for now"]]` for the third of six
users was enough.

And a bare string in `safety_observations` was iterated character by character,
producing a Safety section reading ['L', 'o', 'o', 's', 'e'].
"""
import pytest

import report_sections as rs   # plain import: if this module stops shipping,
                               # these tests must FAIL, not skip


def _report(**kw):
    base = {"report_date": "2026-09-09", "executive_summary": "A day.",
            "topics": [], "recording_session": {"recordings": 1}}
    base.update(kw)
    return base


def _titles(sections):
    return [s["title"] for s in sections]


def _by_title(sections, title):
    for s in sections:
        if s["title"] == title:
            return s
    raise AssertionError("no section " + title + " in " + str(_titles(sections)))


# ---------------------------------------------------------------------------
# Shapes that used to raise
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("topics", [
    [None],
    ["a topic, apparently"],
    [42],
    [{"key_decisions": [["Use Teams", "for now"]]}],
    [{"key_decisions": [7]}],
    [{"open_questions": [["nested"]]}],
    [{"questions": "not a list"}],
    [{"action_items": "not a list"}],
    [{"action_items": [None, "a string", 3]}],
    [{"related_photos": "one.jpg"}],
])
def test_a_strange_topic_shape_does_not_lose_the_night(topics):
    sections = rs.build(_report(topics=topics))
    assert isinstance(sections, list)
    assert _by_title(sections, "Summary")


@pytest.mark.parametrize("report", [None, "not a report", 7, []])
def test_a_report_that_is_not_a_report_still_returns_sections(report):
    assert isinstance(rs.build(report), list)


def test_a_bare_string_of_safety_is_one_observation_not_nine_letters():
    sec = _by_title(rs.build(_report(safety_observations="Loose cable")), "Safety")
    assert [r["observation"] for r in sec["rows"]] == ["Loose cable"]


# ---------------------------------------------------------------------------
# The ranking, on the shapes the producer actually writes
# ---------------------------------------------------------------------------

def _rows(items, report_date="2026-09-09"):
    r = _report(report_date=report_date, topics=[{"action_items": items}])
    return _by_title(rs.build(r), "Actions")["rows"]


def test_tomorrow_outranks_a_date_in_october():
    """The failure this whole thing turns on: as strings, "T" > "2"."""
    rows = _rows([
        {"action": "October thing", "deadline": "2026-10-01"},
        {"action": "Tomorrow thing", "deadline": "Tomorrow 08:00"},
    ])
    assert [x["action"] for x in rows] == ["Tomorrow thing", "October thing"]


def test_end_of_day_outranks_everything_dated_later():
    rows = _rows([
        {"action": "Next week", "deadline": "2026-09-16"},
        {"action": "Right now", "deadline": "EOD"},
    ])
    assert rows[0]["action"] == "Right now"


def test_a_deadline_nobody_can_read_ranks_as_undated():
    """"when the crane arrives" is not a date. Ranked as its own text it would
    sort by "w" and sit above or below real dates by alphabet."""
    rows = _rows([
        {"action": "Vague", "deadline": "when the crane arrives", "priority": "low"},
        {"action": "Real", "deadline": "2026-12-25", "priority": "low"},
    ])
    assert rows[0]["action"] == "Real"


def test_an_unreadable_deadline_is_still_shown():
    """Ranked as undated, displayed as written -- the model's words are what the
    site manager needs to see, even when we cannot sort on them."""
    rows = _rows([{"action": "Vague", "deadline": "when the crane arrives"}])
    assert rows[0]["due"] == "when the crane arrives"


def test_an_overdue_action_comes_first():
    rows = _rows([
        {"action": "Upcoming", "deadline": "2026-09-20"},
        {"action": "Overdue", "deadline": "2026-09-01"},
    ], report_date="2026-09-09")
    assert rows[0]["action"] == "Overdue"


def test_with_no_report_date_nothing_pretends_to_be_dated():
    """Relative deadlines are meaningless without the day they were said on."""
    r = {"topics": [{"action_items": [
        {"action": "A", "deadline": "Tomorrow"},
        {"action": "B", "deadline": "EOD", "priority": "high"},
    ]}]}
    rows = _by_title(rs.build(r), "Actions")["rows"]
    assert rows[0]["action"] == "B", "falls back to priority, not to alphabet"


# ---------------------------------------------------------------------------
# Merging
# ---------------------------------------------------------------------------

def test_two_owners_for_one_action_are_both_named():
    """Keeping only the first silently reassigns somebody else's job."""
    r = _report(topics=[
        {"action_items": [{"action": "Order the brackets", "responsible": "Ben"}]},
        {"action_items": [{"action": "Order the brackets", "responsible": "James"}]},
    ])
    row = _by_title(rs.build(r), "Actions")["rows"][0]
    assert "Ben" in row["owner"] and "James" in row["owner"]


def test_an_action_whose_text_is_a_placeholder_is_not_an_action():
    rows = _rows([{"action": "N/A"}, {"action": "TBD"}, {"action": "Real work"}])
    assert [x["action"] for x in rows] == ["Real work"]


# ---------------------------------------------------------------------------
# Sections that appear and disappear honestly
# ---------------------------------------------------------------------------

def test_a_meeting_gets_no_on_site_panel_of_zeros():
    """A meeting's compat report has no recording_session. Rendering 0 recordings
    and 0 photos states something false about a real day."""
    r = {"executive_summary": "A meeting.", "topics": []}
    assert "On Site" not in _titles(rs.build(r))


def test_the_dates_block_reads_the_keys_the_model_actually_writes():
    """THE SHAPE THE PRODUCER WRITES, not one this test invented.

    The daily prompt asks for `date_mentioned / context / who_mentioned /
    urgency / type`. This section read `item|description|event|detail` and
    `date|deadline` -- none of them -- so every real report produced an empty
    Key Dates and `build` dropped the section entirely. Measured on a TEST
    report from the deployed code: 17 entries in the field, zero readable, no
    section.

    The old test fed `{"item": ..., "date": ...}` and passed. A test that
    supplies its own input can confirm any reading of it; the only thing that
    settles which keys are real is the prompt that asks for them.
    """
    r = _report(critical_dates_and_deadlines=[
        {"date_mentioned": "28th", "context": "Backfill Zone 1",
         "who_mentioned": "Ben", "urgency": "high", "type": "deadline"},
    ])
    sec = _by_title(rs.build(r), "Key Dates")
    assert sec["kind"] == "table"
    assert sec["rows"][0] == {"when": "28th", "what": "Backfill Zone 1", "who": "Ben"}


def test_the_older_key_names_still_read():
    """The meeting path and pre-existing reports are free to use them, and a
    reader is not helped by this being strict."""
    r = _report(critical_dates_and_deadlines=[
        {"item": "Crane off-hire", "date": "2026-09-20"}, "Slab pour Friday",
    ])
    rows = _by_title(rs.build(r), "Key Dates")["rows"]
    assert {"when": "2026-09-20", "what": "Crane off-hire", "who": ""} in rows
    assert {"when": "", "what": "Slab pour Friday", "who": ""} in rows


def test_key_dates_are_in_date_order():
    """A list of dates that is not in date order is a list, not a schedule.
    `date_mentioned` is free text, so the ones that cannot be read keep their
    stated order at the end rather than sorting by first character."""
    r = _report(report_date="2026-09-09", critical_dates_and_deadlines=[
        {"date_mentioned": "next week", "context": "Later thing"},
        {"date_mentioned": "when the crane arrives", "context": "Unreadable"},
        {"date_mentioned": "tomorrow", "context": "Sooner thing"},
    ])
    rows = _by_title(rs.build(r), "Key Dates")["rows"]
    assert [x["what"] for x in rows] == ["Sooner thing", "Later thing", "Unreadable"]
