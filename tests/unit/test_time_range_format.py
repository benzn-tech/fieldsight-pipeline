"""Unit: one rule for "which meeting was this?", used by both places that show it.

The confirmation email and the session picker render the same fact and used to do it with
two separate bits of code. On 2026-08-12 the email learned that a span crossing a day must
say so — a 22-hour span had been rendering as `16:48–14:29`, indistinguishable from an
ordinary afternoon — and the picker did not learn it. Two implementations of one rule is
how they drift, so there is now one.

Why it matters at all: the two ends can come from different clocks. `opened_at` is set from
the chunk filename (the device's wall clock) and `closed_at` from the device's /close, and
before 2026-08-10 the backend stored the device's local time into a UTC column — eight prod
sessions still carry that. A 12-hour error renders as a perfectly plausible meeting unless
something says otherwise.
"""
import datetime as dt

import pytest

ss = pytest.importorskip("session_scope")


def test_an_ordinary_meeting_is_just_the_two_times():
    text, note = ss.format_time_range(dt.datetime(2026, 7, 31, 11, 3),
                                      dt.datetime(2026, 7, 31, 11, 6))
    assert text == "11:03–11:06" and note is None


def test_a_span_into_the_next_day_says_so():
    text, note = ss.format_time_range(dt.datetime(2026, 8, 11, 16, 48),
                                      dt.datetime(2026, 8, 12, 14, 29))
    assert text == "16:48–14:29 (+1d)"
    assert note and "1 day" in note


def test_a_genuine_meeting_over_midnight_keeps_both_ends():
    text, _ = ss.format_time_range(dt.datetime(2026, 8, 11, 23, 40),
                                   dt.datetime(2026, 8, 12, 0, 15))
    assert text == "23:40–00:15 (+1d)"


def test_an_end_before_its_start_is_not_rendered_as_a_range():
    """Only a broken clock produces this. The start alone is what is true."""
    text, note = ss.format_time_range(dt.datetime(2026, 8, 11, 16, 48),
                                      dt.datetime(2026, 8, 10, 16, 48))
    assert text == "16:48"
    assert note and "before" in note


def test_a_missing_end_gives_the_start_alone():
    text, note = ss.format_time_range(dt.datetime(2026, 8, 11, 16, 48), None)
    assert text == "16:48" and note is None


def test_a_missing_start_gives_the_end_alone():
    text, _ = ss.format_time_range(None, dt.datetime(2026, 8, 11, 16, 48))
    assert text == "16:48"


def test_nothing_known_is_nothing_claimed():
    text, note = ss.format_time_range(None, None)
    assert text is None and note is None
