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
# Spoken dates: day + month (no ISO), and weekday names.
# --------------------------------------------------------------------------

FRI = date(2026, 10, 16)           # a Friday


@pytest.mark.parametrize("question", [
    "what did we do on 3 September",
    "3rd September",
    "the 3rd of Sep",
    "September 3",
    "Sept 3rd",
    "Sep. 3 pour",
    "3 SEPTEMBER",
    "9月3日做了什么",
    "9月3号",
    "9 月 3 日",
])
def test_day_and_month_resolve_to_the_most_recent_such_date(question):
    assert qs.time_range(question, FRI) == ("2026-09-03", "2026-09-03")


def test_day_and_month_on_today_is_today():
    assert qs.time_range("16 October", FRI) == ("2026-10-16", "2026-10-16")


def test_day_and_month_after_today_is_last_year():
    assert qs.time_range("20 October", FRI) == ("2025-10-20", "2025-10-20")
    assert qs.time_range("Dec 1st", FRI) == ("2025-12-01", "2025-12-01")


def test_day_and_month_across_the_year_boundary():
    assert qs.time_range("28 December", date(2026, 1, 5)) == ("2025-12-28", "2025-12-28")


@pytest.mark.parametrize("question", ["31 Sep", "30 Feb", "September 31", "2月30日", "0 March"])
def test_an_impossible_day_and_month_is_no_anchor(question):
    assert qs.time_range(question, FRI) == (None, None)


@pytest.mark.parametrize("question", [
    "3 September 2025", "September 3, 2025", "3rd of Sept 2025", "2025年9月3日",
])
def test_day_month_and_year_is_that_exact_date(question):
    assert qs.time_range(question, FRI) == ("2025-09-03", "2025-09-03")


def test_day_month_and_year_needs_no_anchor():
    assert qs.time_range("3 September 2025", None) == ("2025-09-03", "2025-09-03")


def test_day_and_month_without_a_year_needs_an_anchor():
    assert qs.time_range("3 September", None) == (None, None)


def test_day_and_month_beats_a_relative_phrase():
    assert qs.time_range("last week, on 3 September", FRI) == ("2026-09-03", "2026-09-03")


def test_iso_still_beats_day_and_month():
    assert qs.time_range("3 September or 2026-08-12", FRI) == ("2026-08-12", "2026-08-12")


@pytest.mark.parametrize("question", [
    "Tuesday", "what happened on Tuesday?", "on Tue", "last Tuesday", "LAST TUES",
    "周二", "星期二", "礼拜二", "周二我们做了什么",
])
def test_a_bare_or_last_weekday_is_the_most_recent_one_before_today(question):
    # 2026-10-16 is a Friday; the Tuesday before it is the 13th.
    assert qs.time_range(question, FRI) == ("2026-10-13", "2026-10-13")


def test_a_bare_weekday_naming_today_means_a_week_ago():
    assert qs.time_range("Friday", FRI) == ("2026-10-09", "2026-10-09")
    assert qs.time_range("星期五", FRI) == ("2026-10-09", "2026-10-09")


def test_on_a_monday_last_monday_and_monday_are_seven_days_earlier():
    mon = date(2026, 10, 12)
    for q in ("last Monday", "Monday", "on Mon", "周一"):
        assert qs.time_range(q, mon) == ("2026-10-05", "2026-10-05"), q


@pytest.mark.parametrize("question,expected", [
    ("this Tuesday", ("2026-10-13", "2026-10-13")),
    ("this Friday", ("2026-10-16", "2026-10-16")),
    ("这周二", ("2026-10-13", "2026-10-13")),
    ("本周二的会", ("2026-10-13", "2026-10-13")),
    ("这个星期五", ("2026-10-16", "2026-10-16")),
])
def test_this_weekday_is_in_the_current_week(question, expected):
    assert qs.time_range(question, FRI) == expected


@pytest.mark.parametrize("question", ["this Saturday", "这周六", "本周日"])
def test_this_weekday_still_ahead_is_no_anchor(question):
    assert qs.time_range(question, FRI) == (None, None)


@pytest.mark.parametrize("question,expected", [
    ("上周二", ("2026-10-06", "2026-10-06")),
    ("上星期二发生了什么", ("2026-10-06", "2026-10-06")),
    ("上个星期二", ("2026-10-06", "2026-10-06")),
    ("上周日", ("2026-10-11", "2026-10-11")),
    ("上礼拜五", ("2026-10-09", "2026-10-09")),
])
def test_chinese_last_week_weekday_is_in_the_previous_calendar_week(question, expected):
    assert qs.time_range(question, FRI) == expected


@pytest.mark.parametrize("question", [
    "we sat down with the client",
    "sun exposure on the deck",
    "wedge anchors",
    "monitor the crack",
    "Mondays are busy",
    "next Tuesday's pour",
    "the coming Friday",
    "we pour every Monday",
    "下周二的计划",
    "每周三次检查",
    "每周三开会",
    "we may need 3 more",
])
def test_weekday_and_month_words_do_not_fire_elsewhere(question):
    assert qs.time_range(question, FRI) == (None, None)


def test_a_weekday_does_not_override_last_week():
    """'last week on Tuesday' keeps the existing last-week range: the weekday
    rule runs after the relative phrases, and does not narrow them."""
    assert qs.time_range("last week on Tuesday", FRI) == ("2026-10-05", "2026-10-11")
    assert qs.time_range("yesterday, Tuesday", FRI) == ("2026-10-15", "2026-10-15")


def test_has_time_anchor():
    assert "has_time_anchor" in qs.__all__
    assert qs.has_time_anchor("what happened last Tuesday", FRI) is True
    assert qs.has_time_anchor("3 September", FRI) is True
    assert qs.has_time_anchor("yesterday", FRI) is True
    assert qs.has_time_anchor("door schedule", FRI) is False
    assert qs.has_time_anchor("this Saturday", FRI) is False
    assert qs.has_time_anchor("Tuesday", None) is False


def test_may_after_a_day_number_is_a_month_only_when_no_word_follows():
    assert qs.time_range("level 3 may need propping", FRI) == (None, None)
    assert qs.time_range("what happened on 3 May?", FRI) == ("2026-05-03", "2026-05-03")
    assert qs.time_range("3 May, any issues", FRI) == ("2026-05-03", "2026-05-03")
    assert qs.time_range("May 3 pour", FRI) == ("2026-05-03", "2026-05-03")
