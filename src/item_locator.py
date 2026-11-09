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
                     file or two): they cannot be told apart, so nothing links;
  outside_topic   -- the turn lies outside the topic's `time_range` widened by one
                     minute each side (a stamp copied from another topic's span).

If the topic's `time_range` does not parse, the range check is skipped and the linked
support records `range_checked: False`.

`strip` pops `said_at` from every child after resolution; it is a model-facing field,
never stored on the item.

The support object carries ids and offsets only -- never item or turn text. Pure: no
AWS, no DB. Must not import `lambda_extract_session` (it imports this module).
"""
import re
from datetime import datetime, timedelta

from item_continuity import KINDS
from turn_name_overlay import _stem

RULE_VERSION = "locator_v1"
LOCATOR_FIELD = "said_at"
STATUSES = ("linked", "no_locator", "unparseable", "not_shown", "no_line",
            "ambiguous_stamp", "outside_topic")

_STAMP = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})")
# Each side of the topic's range, on top of `item_support`'s inclusive end minute.
_RANGE_WIDEN = timedelta(minutes=1)


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


def _refuse(status, **extra):
    return {"status": status, "method": RULE_VERSION, "rule_version": RULE_VERSION, **extra}


def _in_topic(turn, topic, turns, session_date):
    """True / False for the widened range check, None when it cannot be checked."""
    # Imported here: item_support will import this module to run the locator path.
    from item_support import _topic_window
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
        return _refuse("ambiguous_stamp")
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
