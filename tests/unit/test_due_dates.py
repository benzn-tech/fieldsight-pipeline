"""Unit: what a model wrote about "when", turned into something sortable.

The daily prompt asks for `"deadline": "When (e.g. 'Tomorrow 08:00', 'EOD',
'15:00'), or null"`. It does not ask for a date, and it does not get one. Sorting
those strings ordered them by first character -- measured on the real ranker
before this module existed:

    15/09/2026      first, because "1" < "2"
    2026-10-01      October above September
    EOD             below October
    Tomorrow 08:00  below "Friday"

An action mentioned three times with no deadline sorted last. The report owner
asked for an ordered action list and would have received one ordered by the
alphabet, silently, because nothing fails.
"""
from datetime import date

import due_dates as dd

# A Wednesday, so weekday arithmetic has somewhere to go in both directions.
WED = date(2026, 9, 9)


def test_an_iso_date_is_the_days_between():
    assert dd.days_until("2026-09-12", WED) == 3
    assert dd.days_until("2026-09-09", WED) == 0


def test_an_unpadded_month_is_still_a_date():
    """Models write 2026-9-5 as readily as 2026-09-05."""
    assert dd.days_until("2026-9-12", WED) == 3


def test_day_first_because_this_product_is_not_american():
    """15/09/2026 is September, and reading it as the 9th of March would move a
    deadline by six months without anything failing."""
    assert dd.days_until("15/09/2026", WED) == 6


def test_a_date_already_past_is_negative_not_clamped():
    """An overdue action is more urgent than a comfortable one. Clamping would
    file it below."""
    assert dd.days_until("2026-09-05", WED) == -4


def test_tomorrow_beats_a_date_in_october():
    """The failure that started this: 'Tomorrow 08:00' sorted below 2026-10-01
    because 'T' > '2'."""
    assert dd.days_until("Tomorrow 08:00", WED) < dd.days_until("2026-10-01", WED)


def test_end_of_day_is_today_not_unknown():
    """EOD is the most urgent thing a site report says. Unparsed it sorted below
    a date three weeks out."""
    for text in ("EOD", "eod", "End of day", "COB", "ASAP", "Immediate", "urgent"):
        assert dd.days_until(text, WED) == 0, text


def test_a_bare_time_is_today():
    for text in ("15:00", "08.30", "9:00 am"):
        assert dd.days_until(text, WED) == 0, text


def test_a_named_weekday_is_the_next_one():
    assert dd.days_until("Friday", WED) == 2
    assert dd.days_until("Mon", WED) == 5


def test_a_weekday_named_on_that_weekday_means_next_week():
    """"Do it Wednesday", said on a Wednesday, is not already overdue."""
    assert dd.days_until("Wednesday", WED) == 7


def test_longer_phrases_win_over_the_words_inside_them():
    """"next week" must not be read as "week", and "end of week" must not be
    read as "end of day"."""
    assert dd.days_until("next week", WED) == 7
    assert dd.days_until("end of week", WED) == 4
    assert dd.days_until("end of day", WED) == 0


def test_text_that_names_no_day_is_None_rather_than_a_guess():
    """None is the honest answer for these, and it matters: an unreadable
    deadline must not outrank a real one just for containing characters."""
    for text in ("", None, "?", "when the crane arrives", "before handover",
                 "once the RFI is answered", "soon"):
        assert dd.days_until(text, WED) is None, text


def test_a_date_that_does_not_exist_is_not_a_date():
    """Models write 2026-02-31."""
    assert dd.days_until("2026-02-31", WED) is None


def test_no_anchor_means_no_answer():
    """Relative deadlines are meaningless without the day they were said on, and
    inventing 'today' would silently date every action to whenever the report
    happened to be regenerated."""
    assert dd.days_until("Tomorrow", None) is None
    assert dd.days_until("Tomorrow", "not a date") is None


def test_the_anchor_may_be_a_string_or_a_datetime():
    from datetime import datetime
    assert dd.days_until("2026-09-12", "2026-09-09") == 3
    assert dd.days_until("2026-09-12", datetime(2026, 9, 9, 17, 30)) == 3
