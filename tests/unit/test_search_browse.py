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
    "what did we do on 3 September",
    "what happened on the 3rd of Sep",
    "what did we do Sept 3rd 2026",
    "我们9月3日做了什么",
    "2026年9月3日发生了什么",
    "what happened last Tuesday",
    "anything on Tue",
    "上周二发生了什么",
    "周二我们做了什么",
    "星期天的工作",
    "what happened last week in September",
    "anything this week, sept",
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
    "level 2 inspection",
    "what happened at 1500 on 3 September",
    "PS4 on 3 September",
    "drainage last Tuesday",
    "上周二钢筋验收",
    "9月3日钢筋",
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
