"""Old anchors are immutable.

`query_slots_baseline_86262261.py` beside this file is a byte-for-byte copy of
src/query_slots.py at 86262261, before spoken dates and weekdays existed.

The rule: for any input where that parser returns a range, the current parser
returns the IDENTICAL range. A new rule may only fill a gap. It may never move
an anchor that already worked. A wrong range silently filters Ask to the wrong
day ("上周二层浇筑" read as last Tuesday instead of last week).

The sentences are the review's regression inputs, every sentence the
2026-10-16 review flagged, and the old suite's own sentences. Each one is
checked against several `today`s, including None, a Monday, a Sunday and a
week across a year boundary.
"""
import importlib.util
import pathlib
from datetime import date

import pytest

import query_slots as qs

_BASELINE = pathlib.Path(__file__).with_name("query_slots_baseline_86262261.py")
_spec = importlib.util.spec_from_file_location("query_slots_baseline_86262261", _BASELINE)
old = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(old)

TODAYS = [None, date(2026, 10, 16), date(2026, 10, 12), date(2026, 8, 30),
          date(2026, 1, 1), date(2026, 1, 5)]

SENTENCES = [
    # scratchpad/regress_inputs.txt (review, 2026-10-16)
    "上周日报", "本周日报", "这周日报交了吗", "上周五金店送货", "上周六角螺栓到货",
    "上周一到周五的进度", "上周一至上周五", "这周一到现在做了什么", "上周三号楼",
    "上周二层浇筑", "这周五层模板", "本周四层验收", "上周日志", "上周日常检查",
    "这周二次结构", "上周一层", "上周天气", "上星期天",
    "yesterday, march 3 crews to level 2", "yesterday we pumped the sept 3 tank",
    "today mar 3 drawings", "last week's dec 2 slab", "this month on sun deck",
    "yesterday on Sat scaffold", "最近7天3号楼", "昨天9月3号楼", "2026-08-12 on Monday",
    "周二和上周三", "上周二和这周三", "每个星期一", "周一到周五", "周日休息",
    "9月3号楼进度", "2月3号楼", "12月25号线", "1月2号机", "3月5号桩", "on sun deck",
    "Monday to Friday",
    # the rest of the review's "wrong range where an anchor existed" table
    "上周三号楼", "上周日报", "last week on Tuesday", "yesterday on Tuesday",
    "last week Tuesday", "past 2 weeks on Tuesday", "上周二和上周三",
    "last week, on 3 September", "上周二", "上星期二发生了什么", "上个星期二",
    "上周日", "这周二", "本周二的会", "这个星期五", "这周六", "本周日",
    # the old suite's own sentences
    "昨天发生了什么", "what happened yesterday?", "Yesterday's issues", "今天的问题",
    "anything today?", "前天的会说了什么", "这周有什么问题", "本周进度",
    "how did this week go", "上周的决定", "last week's decisions", "这个月的安全问题",
    "本月总结", "this month", "上个月的记录", "last month", "最近7天的问题", "过去 3 天",
    "last 14 days", "past 2 weeks", "昨天提到的 2026-08-12 那次", "2026-08-12 那次会",
    "混凝土的问题怎么解决", "who is responsible for the door schedule", "",
]


@pytest.mark.parametrize("today", TODAYS, ids=str)
@pytest.mark.parametrize("question", SENTENCES)
def test_an_old_anchor_is_never_moved(question, today):
    before = old.time_range(question, today)
    if before == (None, None):
        pytest.skip("the old parser did not anchor this; new rules may fill it")
    assert qs.time_range(question, today) == before


def test_the_baseline_really_anchors_most_of_these():
    """Guard against a fixture that silently anchors nothing, which would
    make every case above a skip."""
    hits = sum(old.time_range(q, date(2026, 10, 16)) != (None, None) for q in SENTENCES)
    assert hits >= 45


def test_the_baseline_is_the_86262261_parser():
    # The two names that exist only in the new module must be absent from it.
    assert not hasattr(old, "spoken_anchor") and not hasattr(old, "has_time_anchor")
    assert old.time_range("上周二", date(2026, 10, 16)) == ("2026-10-05", "2026-10-11")
