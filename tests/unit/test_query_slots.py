"""Tests for src/query_slots.py — the time slot Ask reads out of a question.

TDD, written before the module exists.

Two things are being pinned here and they fail differently:

  * `resolve_today` must go through a real tz database, not an offset someone
    typed. New Zealand and Australia are both on daylight saving for part of the
    year and they do NOT switch on the same date, so a hardcoded +12/+10 is
    wrong for several weeks a year in each direction. The January case below is
    the discriminator: it only passes if the +13 summer offset is honoured.

  * `time_range` must return nothing rather than guess. A question with no time
    expression yields (None, None), which is what keeps the search unfiltered
    and the existing behaviour byte-identical.
"""
from datetime import date, datetime, timezone

import pytest

# NOT importorskip: this module is ours. `importorskip` would turn "the module
# does not exist yet" into a green skip, which is the exact shape this repo
# keeps re-learning -- a suite that passes over a path nothing runs.
import query_slots as qs  # noqa: E402


# --------------------------------------------------------------------------
# resolve_today
# --------------------------------------------------------------------------

def _utc(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


def test_same_instant_is_a_different_day_in_auckland_and_sydney():
    """The reason the client sends a zone and not a date.

    At this instant it is already tomorrow in Auckland and still today in
    Sydney. A single 'today' computed anywhere but in the caller's own zone is
    wrong for one of them.
    """
    instant = _utc(2026, 8, 29, 12, 30)
    assert qs.resolve_today("Pacific/Auckland", now=instant) == date(2026, 8, 30)
    assert qs.resolve_today("Australia/Sydney", now=instant) == date(2026, 8, 29)


def test_daylight_saving_is_honoured_not_a_fixed_offset():
    """January is NZDT (+13), not NZST (+12).

    With a hardcoded +12 this instant lands on the 15th; with the real zone it
    is the 16th. This test is the whole reason the zone is resolved through the
    tz database.
    """
    assert qs.resolve_today("Pacific/Auckland", now=_utc(2026, 1, 15, 11, 30)) == date(2026, 1, 16)


def test_unknown_or_missing_zone_yields_none():
    """Fail soft. An unrecognised zone must not filter the search to a wrong
    day -- it must not filter at all, which is exactly today's behaviour."""
    for bad in (None, "", "   ", "Mars/Olympus", "GMT+12", 12):
        assert qs.resolve_today(bad, now=_utc(2026, 8, 29, 12, 30)) is None


# --------------------------------------------------------------------------
# time_range
# --------------------------------------------------------------------------

TODAY = date(2026, 8, 30)          # a Sunday


@pytest.mark.parametrize("question", ["昨天发生了什么", "what happened yesterday?", "Yesterday's issues"])
def test_yesterday(question):
    assert qs.time_range(question, TODAY) == ("2026-08-29", "2026-08-29")


@pytest.mark.parametrize("question", ["今天的问题", "anything today?"])
def test_today(question):
    assert qs.time_range(question, TODAY) == ("2026-08-30", "2026-08-30")


def test_day_before_yesterday():
    assert qs.time_range("前天的会说了什么", TODAY) == ("2026-08-28", "2026-08-28")


@pytest.mark.parametrize("question", ["这周有什么问题", "本周进度", "how did this week go"])
def test_this_week_runs_monday_to_today(question):
    # 2026-08-30 is a Sunday; its Monday is 2026-08-24.
    assert qs.time_range(question, TODAY) == ("2026-08-24", "2026-08-30")


@pytest.mark.parametrize("question", ["上周的决定", "last week's decisions"])
def test_last_week_is_a_closed_monday_to_sunday(question):
    assert qs.time_range(question, TODAY) == ("2026-08-17", "2026-08-23")


@pytest.mark.parametrize("question", ["这个月的安全问题", "本月总结", "this month"])
def test_this_month_runs_from_the_first_to_today(question):
    assert qs.time_range(question, TODAY) == ("2026-08-01", "2026-08-30")


@pytest.mark.parametrize("question", ["上个月的记录", "last month"])
def test_last_month_is_a_closed_calendar_month(question):
    assert qs.time_range(question, TODAY) == ("2026-07-01", "2026-07-31")


@pytest.mark.parametrize("question,expected", [
    ("最近7天的问题", ("2026-08-24", "2026-08-30")),
    ("过去 3 天", ("2026-08-28", "2026-08-30")),
    ("last 14 days", ("2026-08-17", "2026-08-30")),
    ("past 2 weeks", ("2026-08-17", "2026-08-30")),
])
def test_relative_windows_include_today(question, expected):
    assert qs.time_range(question, expected[1] and TODAY) == expected


def test_an_explicit_iso_date_wins_over_a_relative_word():
    """'昨天我们在 2026-08-12 说过' -- the explicit date is the specific one."""
    assert qs.time_range("昨天提到的 2026-08-12 那次", TODAY) == ("2026-08-12", "2026-08-12")


@pytest.mark.parametrize("question", [
    "混凝土的问题怎么解决",
    "who is responsible for the door schedule",
    "",
    None,
])
def test_no_time_expression_yields_nothing(question):
    """The whole safety property: no slot means no filter, not a guessed one."""
    assert qs.time_range(question, TODAY) == (None, None)


def test_no_today_yields_nothing_even_with_a_time_word():
    """`today` is None when the zone was unusable. A relative word cannot be
    resolved without an anchor, and inventing one would filter to a day the
    caller never meant."""
    assert qs.time_range("昨天发生了什么", None) == (None, None)


def test_an_absolute_date_still_resolves_without_an_anchor():
    """An explicit date needs no 'today', so losing the zone must not cost it."""
    assert qs.time_range("2026-08-12 那次会", None) == ("2026-08-12", "2026-08-12")


# --------------------------------------------------------------------------
# "last <weekday>": the one spoken-date rule. Every other spoken date is the
# Ask judge model's job (ask_tools tells it to resolve them to ISO). Old
# anchors are immutable (test_query_slots_old_anchors.py).
# --------------------------------------------------------------------------

FRI = date(2026, 10, 16)           # a Friday
LAST_WEEK = ("2026-10-05", "2026-10-11")


@pytest.mark.parametrize("question", [
    "last Tuesday", "what happened last Tuesday?", "LAST TUESDAY", "last tue",
    "last Tue.", "drainage last Tuesday, any issues",
])
def test_last_weekday_is_the_most_recent_one_before_today(question):
    # 2026-10-16 is a Friday; the Tuesday before it is the 13th.
    assert qs.time_range(question, FRI) == ("2026-10-13", "2026-10-13")


def test_last_weekday_naming_today_means_a_week_ago():
    assert qs.time_range("last Friday", FRI) == ("2026-10-09", "2026-10-09")
    assert qs.time_range("last fri", FRI) == ("2026-10-09", "2026-10-09")


def test_on_a_monday_last_monday_is_seven_days_earlier():
    mon = date(2026, 10, 12)
    for q in ("last Monday", "last mon"):
        assert qs.time_range(q, mon) == ("2026-10-05", "2026-10-05"), q


def test_every_weekday_name():
    names = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    expected = ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-09",
                "2026-10-10", "2026-10-11"]
    for name, day in zip(names, expected):
        assert qs.time_range("last " + name, FRI) == (day, day), name
        assert qs.time_range("last " + name[:3], FRI) == (day, day), name[:3]


def test_last_weekday_across_the_year_boundary():
    assert qs.time_range("last Sunday", date(2026, 1, 1)) == ("2025-12-28", "2025-12-28")


@pytest.mark.parametrize("question", [
    # no "last": general spoken dates are the judge's
    "Tuesday", "on Tuesday", "this Tuesday", "we pour on Friday", "next Tuesday",
    "3 September", "Sept 3rd", "9月3日", "周二", "上礼拜五", "on the 3rd",
    # "last" but not a single past day
    "last tues", "last thurs",                 # only full names or 3 letters
    "last Sunday's slab", "last Mondays",
    "last Monday to Friday", "last Mon-Fri", "last Mon–Fri", "last Monday through Friday",
    "last Monday till Friday", "the last Friday of the month",
    "last Monday mornings", "last Friday nights",
    "last Monday Street site", "last Friday Lane", "last Sun Rd",
    "since last Monday", "until last Friday", "from last Monday", "due last Friday",
    # not a weekday at all
    "the last sundial", "last monitor check", "last wedge", "last saturation test",
])
def test_no_anchor(question):
    assert qs.time_range(question, FRI) == (None, None)


def test_last_weekday_does_not_override_an_old_phrase():
    assert qs.time_range("last week on Tuesday", FRI) == LAST_WEEK
    assert qs.time_range("yesterday, last Tuesday", FRI) == ("2026-10-15", "2026-10-15")


def test_last_weekday_needs_today():
    assert qs.time_range("last Tuesday", None) == (None, None)


def test_a_blocked_last_weekday_does_not_hide_a_later_good_one():
    assert qs.time_range("since last Monday, and last Tuesday", FRI) == \
        ("2026-10-13", "2026-10-13")


def test_has_time_anchor():
    assert "has_time_anchor" in qs.__all__
    assert qs.has_time_anchor("what happened last Tuesday", FRI) is True
    assert qs.has_time_anchor("yesterday", FRI) is True
    assert qs.has_time_anchor("door schedule", FRI) is False
    assert qs.has_time_anchor("on Tuesday", FRI) is False
    assert qs.has_time_anchor("last Tuesday", None) is False


def test_spoken_anchor_reports_only_the_last_weekday_span():
    assert qs.spoken_anchor("yesterday, last Tuesday", FRI) is None   # old rule anchored
    q = "what did we do last Tue?"
    d, (a, b) = qs.spoken_anchor(q, FRI)
    assert d == date(2026, 10, 13)
    assert q[a:b] == "last Tue"
    assert qs.spoken_anchor("door schedule", FRI) is None
