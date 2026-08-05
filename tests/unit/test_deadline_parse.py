"""Resolving the extractor's free-text deadline into a real date.

The extractor has always captured deadlines -- `deadline_text` on prod holds
"This Friday, 2026-07-25", "Friday afternoon (2026-07-24)", "Tomorrow",
"Immediately" -- and `_map_action_items` threw all of it away unless the
string already WAS an ISO date. Result on prod: 18 open items with a stated
deadline, and 0 with a date. Everything that wants a due date (overdue,
due-this-week, slip counting) had nothing to read.

The frontend has resolved this text since fix/timeline-buttons-and-deadline
(`today-adapter.js:resolveDeadline`). This is that parser, server-side, so
the date is stored once rather than re-derived by every reader.

The rule it must not break: NEVER GUESS. An unparseable string keeps its
text and leaves the date NULL. A wrong due date is worse than no due date --
it fabricates urgency, or hides it.
"""
import pytest

from deadline_parse import resolve_deadline


WED = "2026-07-22"        # a Wednesday, used as the report date throughout


def r(text, on=WED):
    return resolve_deadline(text, on)


# ---- nothing to parse -----------------------------------------------------

@pytest.mark.parametrize("text", [None, "", "   ", "null", "NULL", "None"])
def test_absent_or_placeholder_yields_no_date(text):
    """`null` as a literal STRING is real prod data -- the extractor emitted
    it and it was stored verbatim. It is an absence, not a deadline."""
    assert r(text) is None


@pytest.mark.parametrize("text", [
    "ASAP", "Immediately", "Ongoing from next week", "when the crane is free",
    "before handover", "TBC",
])
def test_vague_urgency_is_not_a_date(text):
    """"Immediately" is a priority, not a due date. Inventing today's date
    for it would make every one of them overdue tomorrow."""
    assert r(text) is None


# ---- an explicit date in the text wins ------------------------------------

def test_iso_date_anywhere_in_the_string():
    assert r("This Friday, 2026-07-25") == "2026-07-25"
    assert r("2026-08-01") == "2026-08-01"


def test_an_explicit_date_beats_the_relative_phrase_around_it():
    """The extractor often writes BOTH -- "Week after next Tuesday
    (2026-07-28 approx.)". The parenthesised date is ground truth;
    re-deriving the weekday would land somewhere else."""
    assert r("Week after next Tuesday (2026-07-28 approx.)") == "2026-07-28"
    assert r("Friday afternoon (2026-07-24)") == "2026-07-24"


def test_day_month_and_month_day_spellings():
    assert r("12 Feb") == "2026-02-12"
    assert r("Feb 12") == "2026-02-12"
    assert r("12th February 2027") == "2027-02-12"


def test_a_bare_date_with_no_year_takes_the_report_year():
    assert r("3 Aug", on="2027-01-05") == "2027-08-03"


# ---- relative phrasing ----------------------------------------------------

def test_today_and_tomorrow():
    assert r("today") == WED
    assert r("Tomorrow") == "2026-07-23"
    assert r("by tomorrow morning") == "2026-07-23"


def test_a_weekday_means_the_NEXT_one_never_the_same_day():
    """Said on Wednesday, "Wednesday" means next week -- the commitment is
    always forward of the conversation."""
    assert r("Friday") == "2026-07-24"
    assert r("Wednesday") == "2026-07-29"
    assert r("mon") == "2026-07-27"


def test_next_week():
    assert r("next week") == "2026-07-29"


def test_within_n_days_or_weeks():
    assert r("within 3 days") == "2026-07-25"
    assert r("in 2 weeks") == "2026-08-05"
    assert r("within 1 day") == "2026-07-23"


# ---- guards ---------------------------------------------------------------

def test_a_missing_report_date_disables_relative_resolution():
    """Everything relative is anchored on the report date. With no anchor the
    honest answer is no date -- not the server's own today, which would drift
    a recording's deadlines every time it is reprocessed."""
    assert resolve_deadline("tomorrow", None) is None
    assert resolve_deadline("tomorrow", "") is None


def test_an_absolute_date_still_resolves_without_an_anchor():
    assert resolve_deadline("2026-07-25", None) == "2026-07-25"


def test_an_impossible_date_is_refused_not_clamped():
    assert r("2026-13-40") is None
    assert r("32 Feb") is None


def test_an_hour_scale_deadline_lands_on_the_report_day():
    """"Within one hour" is real prod text. The column's granularity is a
    DAY, so the honest answer is the report's own date -- rounding, not a
    guess. Refusing it would drop a deadline that is unambiguously today."""
    assert r("Within one hour") == WED
    assert r("in 2 hours") == WED
    assert r("within a few minutes") == WED


def test_the_earlier_date_wins_when_two_are_offered():
    """"Week of 2026-07-23 or 2026-07-27" states a range of intent. A
    deadline should surface at the earlier one: being early is recoverable,
    being late is the thing this exists to prevent."""
    assert r("Week of 2026-07-23 or 2026-07-27") == "2026-07-23"
