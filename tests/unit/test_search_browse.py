"""search_browse.is_content_free: a question that is only a time phrase plus
generic "what happened" words has nothing to match a topic against."""
import pytest

import search_browse as sb


@pytest.mark.parametrize("q", [
    "What did we do yesterday?",
    "what happened last week",
    "anything this morning",
    "Everything that was discussed on Monday",
    "what happened on 2026-08-12",
    "昨天我们做了什么",
    "上周发生了什么",
    "这个月我们讨论了什么",
    "今天的工作",
])
def test_content_free(q):
    assert sb.is_content_free(q) is True


@pytest.mark.parametrize("q", [
    "What did we do for Level two inspection last week?",
    "door damage yesterday",
    "PS4 yesterday",
    "昨天钢筋验收",
    "上周我们讨论了什么钢筋",
    "door damage",
])
def test_has_content(q):
    assert sb.is_content_free(q) is False


def test_start_seconds():
    assert sb.start_seconds("09:30 - 10:15") == 9 * 3600 + 30 * 60
    assert sb.start_seconds("14:05–15:00") == 14 * 3600 + 5 * 60
    assert sb.start_seconds("08:00:30") == 8 * 3600 + 30
    assert sb.start_seconds("") is None
    assert sb.start_seconds(None) is None
    assert sb.start_seconds("morning") is None


# ---- spoken dates: non-content ONLY where time_range anchored on them -------
from datetime import date  # noqa: E402

FRI = date(2026, 10, 16)


@pytest.mark.parametrize("q", [
    "what did we do on 3 September",
    "what happened on the 3rd of Sep",
    "what did we do Sept 3rd 2026",
    "我们9月3日做了什么",
    "2026年9月3日发生了什么",
    "what happened last Tuesday",
    "anything on Tue",
    "周二我们做了什么",
    "星期天的工作",
])
def test_an_anchored_spoken_date_is_not_content(q):
    assert sb.is_content_free(q, FRI) is True


@pytest.mark.parametrize("q", [
    "level 2 inspection",
    "what happened at 1500 on 3 September",
    "PS4 on 3 September",
    "drainage last Tuesday",
    "9月3日钢筋",
    "the dec 2 slab",                       # no anchor: dec stays content
    "what happened last week in September",  # anchored on "last week", not on September
    "anything this week, sept",
    "9月3号楼",                              # 号楼 is a building, not a date
    "上周二发生了什么",                       # anchored on 上周; 二 is not part of it
    "on sun deck",
])
def test_unanchored_date_words_stay_content(q):
    assert sb.is_content_free(q, FRI) is False


def test_until_friday_is_never_a_date_browse():
    """The browse decision is anchor AND content-free; "until Friday" has no
    anchor, so it can never become a browse."""
    import query_slots
    assert query_slots.has_time_anchor("until Friday", FRI) is False


def test_a_spoken_date_needs_today_to_be_non_content():
    # Without an anchor day "on 3 September" resolves to nothing, so the
    # leftover "3"/"september" count as content.
    assert sb.is_content_free("what did we do on 3 September", None) is False
