"""Which transcript line raised this final item? The model names it; this checks the name.

With the locator prompt on, each final child item carries `said_at`: the printed
`[HH:MM:SS]` stamp of the one transcript line that raised it, or null. `resolve` turns
that stamp into the slice 1 turn locator, or refuses with a reason:

  no_locator      -- `said_at` null or absent (the model named no single line);
  unparseable     -- not a strict HH:MM:SS clock time;
  not_shown       -- no SHOWN turn has that stamp, but an elided one does: the model
                     never saw that line, so the stamp is a guess that happens to exist;
  no_line         -- no turn has that stamp at all;
  ambiguous_stamp -- more than one shown turn starts in that second (two speakers, one
                     file or two) and the tie-break below does not single one out;
  outside_topic   -- the turn lies outside the topic's `time_range` widened by one
                     minute each side (a stamp copied from another topic's span).

Same-second tie-break (locator_v2): when several shown turns start in the stamped second,
the one turn lasting at least `MIN_TIE_DURATION_SEC` (end_sec - start_sec) links, if exactly
one does. A missing or non-numeric start/end counts as duration 0. The usual case is a
diarization fragment of a fraction of a second next to the real line; two real lines, or two
fragments, still refuse. The winner still faces the topic range check.

If the topic's `time_range` does not parse, the range check is skipped and the linked
support records `range_checked: False`.

`strip` pops `said_at` from every child after resolution; it is a model-facing field,
never stored on the item.

The support object carries ids and offsets only -- never item or turn text. Pure: no
AWS, no DB. Must not import `lambda_extract_session` (it imports this module).
"""
import math
import re
from datetime import datetime, timedelta

from item_continuity import KINDS
from topic_window import _topic_window
from turn_name_overlay import _stem

RULE_VERSION = "locator_v2"
LOCATOR_FIELD = "said_at"
STATUSES = ("linked", "no_locator", "unparseable", "not_shown", "no_line",
            "ambiguous_stamp", "outside_topic")

_STAMP = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})", re.ASCII)   # ASCII digits only
# Each side of the topic's range, on top of `topic_window`'s inclusive end minute.
_RANGE_WIDEN = timedelta(minutes=1)
# The same-second tie-break's floor (re-test design 2026-11-12, C2), inclusive.
MIN_TIE_DURATION_SEC = 0.5
# Float noise: 0.57 - 0.07 is 0.49999999999999994, still half a second.
_TIE_EPS = 1e-9


def _parse_stamp(value):
    """Strict HH:MM:SS (hour may be one digit) normalised to %H:%M:%S, or None."""
    if not isinstance(value, str):
        return None
    m = _STAMP.fullmatch(value)
    if not m:
        return None
    h, mi, s = (int(g) for g in m.groups())
    if h >= 24 or mi >= 60 or s >= 60:
        return None
    return f"{h:02d}:{mi:02d}:{s:02d}"


def _seconds(value):
    """A finite int/float, else None (bool, None, strings and NaN are not times)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _duration(turn):
    """end_sec - start_sec, or 0 when either is missing or not a number."""
    start, end = _seconds(turn.get("start_sec")), _seconds(turn.get("end_sec"))
    return 0.0 if start is None or end is None else end - start


def _refuse(status, **extra):
    return {"status": status, "method": RULE_VERSION, "rule_version": RULE_VERSION, **extra}


def _in_topic(turn, topic, turns, session_date):
    """True / False for the widened range check, None when it cannot be checked."""
    window = _topic_window(topic.get("time_range") if isinstance(topic, dict) else None,
                           session_date, turns)
    at = turn.get("abs_start")
    if window is None or not isinstance(at, datetime):
        return None
    start, end = window
    return start - _RANGE_WIDEN <= at < end + _RANGE_WIDEN


def resolve(child, topic, turns, shown, session_date):
    """The support object for one final child (see the module docstring). Never raises on
    a malformed child; `turns` are the extractor's turns, `shown` the indices rendered."""
    raw = child.get(LOCATOR_FIELD) if isinstance(child, dict) else None
    if raw is None:
        return _refuse("no_locator")
    stamp = _parse_stamp(raw)
    if stamp is None:
        return _refuse("unparseable")
    turns = list(turns or [])
    matches = [i for i, t in enumerate(turns)
               if isinstance(t, dict) and t.get("abs_start_str") == stamp]
    shown_matches = [i for i in matches if i in shown]
    if not shown_matches:
        return _refuse("not_shown" if matches else "no_line")
    if len(shown_matches) > 1:
        long_ = [i for i in shown_matches if _duration(turns[i])
                 >= MIN_TIE_DURATION_SEC - _TIE_EPS]
        if len(long_) != 1:
            return _refuse("ambiguous_stamp")
        shown_matches = long_
    turn = turns[shown_matches[0]]
    inside = _in_topic(turn, topic, turns, session_date)
    if inside is False:
        return _refuse("outside_topic", range_checked=True)
    return {"status": "linked",
            "source_stem": _stem(turn.get("source_filename")),
            "start_sec": turn.get("start_sec"),
            "end_sec": turn.get("end_sec"),
            "speaker_label": turn.get("speaker"),
            "quote_status": None,
            "method": RULE_VERSION,
            "rule_version": RULE_VERSION,
            "score": None,
            "range_checked": inside is True}


def strip(topics):
    """Pop top-level `said_at` in place from every dict child of the four kinds; return
    how many were popped. Lists are never rebuilt or reordered (refs number children by
    position), nested fields such as `status_suggestion` are never touched, and malformed
    shapes are skipped."""
    n = 0
    for topic in topics if isinstance(topics, list) else []:
        if not isinstance(topic, dict):
            continue
        for list_name in KINDS:
            children = topic.get(list_name)
            if not isinstance(children, list):
                continue
            for child in children:
                if isinstance(child, dict) and LOCATOR_FIELD in child:
                    del child[LOCATOR_FIELD]
                    n += 1
    return n
