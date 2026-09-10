"""Unit: "EOD Friday" is Friday, and this value goes into a DATE column.

`resolve_deadline` gained a vocabulary of phrases that name a day without
spelling one -- EOD, COB, EOW, a bare time, end of month. Run BEFORE the
weekday and tomorrow branches, `_TODAY_WORDS` matched the "EOD" **inside**
"EOD Friday" and returned today. Measured, anchored Wednesday 2026-09-09:

    EOD Friday               2026-09-11  ->  2026-09-09
    COB Monday               2026-09-14  ->  2026-09-09
    End of day tomorrow      2026-09-10  ->  2026-09-09
    by end of day on Friday  2026-09-11  ->  2026-09-09

That is not a display detail. `lambda_ingest._map_action_items` writes this
value into `action_items.deadline`, which the Today page reads for "overdue"
and "due this week" -- so a Friday commitment appears overdue on Thursday
morning. The meeting prompt's own example of a deadline is "'By Friday',
'EOW'", so the combination is what the model was asked to produce.

The tests that shipped the regression fed single words. Every word resolved
correctly on its own; not one sentence contained two of them. So the rule
these tests exist for is about ORDER, not vocabulary: a phrase that names an
actual day always wins, and the weak words only speak when the answer would
otherwise be None.
"""
import pytest

import deadline_parse

WED = "2026-09-09"      # a Wednesday


def r(text, anchor=WED):
    return deadline_parse.resolve_deadline(text, anchor)


# ---------------------------------------------------------------------------
# The regression itself
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("EOD Friday", "2026-09-11"),
    ("eod friday", "2026-09-11"),
    ("COB Monday", "2026-09-14"),
    ("End of day tomorrow", "2026-09-10"),
    ("by end of day on Friday", "2026-09-11"),
    ("close of business Thursday", "2026-09-10"),
    ("tonight or tomorrow morning", "2026-09-10"),
    ("EOD today", "2026-09-09"),
])
def test_a_named_day_beats_the_weak_word_beside_it(text, expected):
    assert r(text) == expected, text


@pytest.mark.parametrize("text,expected", [
    ("EOW but ideally 15 September", "2026-09-15"),
    ("end of week, 2026-09-10 at the latest", "2026-09-10"),
    ("EOD 15/09/2026", "2026-09-15"),
    ("this week, within 2 days", "2026-09-11"),
])
def test_a_spelled_or_numeric_date_beats_the_weak_word_too(text, expected):
    """The weak words run after the spelled-out forms as well, not just after
    the weekday branch. "EOW but ideally 15 September" naming Friday would be
    a plausible reading; naming TODAY was not."""
    assert r(text) == expected, text


def test_the_weak_word_still_answers_when_it_is_the_only_thing_there():
    """Moving them last must not silence them: an unresolved EOD was the
    original reason for adding the vocabulary at all -- every one of these was
    a NULL deadline column, so nothing could call it overdue."""
    for text in ("EOD", "eod", "COB", "End of day", "tonight", "this afternoon"):
        assert r(text) == WED, text
    assert r("EOW") == "2026-09-11"
    assert r("end of week") == "2026-09-11"


def test_a_weekday_named_by_itself_is_unaffected():
    assert r("Friday") == "2026-09-11"
    assert r("Wednesday") == "2026-09-16"      # strictly after the conversation


# ---------------------------------------------------------------------------
# A bare number is not a time
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["2026", "3", "15", "1.5", "42"])
def test_a_bare_number_names_nothing(text):
    """`\\d{1,2}` with every following group optional matched any one- or
    two-digit string, so `deadline: "3"` resolved to the report's own date and
    was overdue the next morning. A number on its own is not a time."""
    assert r(text) is None, text


@pytest.mark.parametrize("text", ["15:00", "08.30", "9:00 am", "9am", "5pm"])
def test_a_real_time_is_still_today(text):
    assert r(text) == WED, text


# ---------------------------------------------------------------------------
# The month phrases the merge dropped
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("end of month", "2026-09-30"),
    ("EOM", "2026-09-30"),
    ("this month", "2026-09-30"),
    ("by the end of the month", "2026-09-30"),
    ("next month", "2026-10-31"),
])
def test_month_phrases_resolve_to_the_end_of_that_month(text, expected):
    """The report's own ranker knew these before the two parsers were merged
    and this module did not, so unifying them turned "end of month" from an
    order into unreadable. It resolves to the last day rather than the old
    ranker's "+25 days": that number was never a date, and where a month
    phrase is imprecise, LATE is the safe direction -- too late delays a nudge,
    too early reports something overdue that was never owed."""
    assert r(text) == expected, text


def test_end_of_month_is_month_length_aware():
    assert r("end of month", "2026-02-03") == "2026-02-28"
    assert r("end of month", "2028-02-03") == "2028-02-29"   # leap
    assert r("end of month", "2026-12-03") == "2026-12-31"
    assert r("next month", "2026-12-03") == "2027-01-31"


@pytest.mark.parametrize("text,expected", [("tmr", "2026-09-10"),
                                           ("next day", "2026-09-10")])
def test_the_shorthands_for_tomorrow(text, expected):
    assert r(text) == expected


def test_now_still_names_no_day():
    """The old ranker mapped "now" to 0. It belongs with ASAP and Immediately,
    which this repository already refuses: they state a priority, and inventing
    today's date makes them overdue tomorrow and every day after, forever."""
    assert r("now") is None


def test_ongoing_still_wins_over_all_of_it():
    """`_CONTINUOUS` is checked before any of this. "Ongoing from next week"
    has a day in it and is still not a deadline."""
    assert r("ongoing from next week") is None
    assert r("daily, EOD") is None
