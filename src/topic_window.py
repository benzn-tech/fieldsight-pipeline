"""A topic's `time_range` ("HH:MM – HH:MM") as a window of naive local datetimes.

Shared by `item_support` (window candidates) and `item_locator` (range check). A leaf
module: it imports neither of them, so both can import it at top level.
"""
import re
from datetime import datetime, timedelta

_TIME_RANGE = re.compile(r"(\d{1,2}):(\d{2})\s*[–—-]\s*(\d{1,2}):(\d{2})")
# `time_range` is minute-granular, so its end minute is inclusive.
_END_SLACK = timedelta(minutes=1)


def _topic_window(time_range, session_date, turns):
    """(start, end) of the topic's `time_range` as the half-open [start, end + 1 min), or
    None when it does not parse.

    Naive local datetimes on `session_date`, as `_parse_at` builds them, compared
    with the turns' naive local abs_start. A range whose end is before its start
    crosses midnight; the whole range takes the day nearest the session's middle
    turn, the same rule `_parse_at` applies to a quote anchor.
    """
    m = _TIME_RANGE.search(str(time_range or ""))
    if not m:
        return None
    h1, m1, h2, m2 = (int(g) for g in m.groups())
    try:
        start = datetime.strptime(f"{session_date} {h1:02d}:{m1:02d}", "%Y-%m-%d %H:%M")
        end = datetime.strptime(f"{session_date} {h2:02d}:{m2:02d}", "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    if end < start:
        end += timedelta(days=1)
    anchored = [t.get("abs_start") for t in turns if isinstance(t.get("abs_start"), datetime)]
    if anchored:
        mid = anchored[len(anchored) // 2]
        shift = min((timedelta(0), timedelta(days=1), timedelta(days=-1)),
                    key=lambda s: abs((start + s - mid).total_seconds()))
        start, end = start + shift, end + shift
    return start, end + _END_SLACK
