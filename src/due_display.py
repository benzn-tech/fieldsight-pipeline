"""due_display.py -- one way to say when something is due.

Pure. No boto3, no psycopg, no network, no model.

A deadline is spoken relative to the day it was said: "tomorrow morning",
"before Wednesday", "this arvo". Read a day later, every one of those is wrong
or useless, so every surface that shows a deadline -- the stop-recording email,
the checklist and daily reports, the Timeline and Today pages -- shows the day
itself instead: "Tue 13 Oct 2026, morning" (owner, 2026-10-12).

The DAY comes from `deadline_parse.resolve_deadline`, the one parser the
`action_items.deadline` column already uses. This module adds only what a day
cannot say:

* the PART of the day that was spoken (morning, lunchtime, afternoon, end of
  day, evening) or a clock time that was spoken ("7am");
* the word that turns a day into a limit ("before", "by", "after"), kept as said:
  "before Wednesday" on a site can mean before Wednesday's 2pm inspection, so it
  is not rewritten into "by Tuesday";
* `by`, the latest local wall-clock moment the commitment still counts -- what
  ordering, overdue and any later reminder compare against.

NO CLOCK TIME IS INVENTED FOR DISPLAY. "Morning" stays "morning"; writing 11:00
would be a time nobody said (owner agreed, 2026-10-12). The part's END is used
only inside `by`: morning 12:00, lunchtime 13:00, afternoon and end of day
17:00, evening 23:59. A deadline is met until its window closes.

What cannot be resolved keeps its words: "before the pour" is shown as said.
"""
import re
from datetime import date

import deadline_parse

__all__ = ["describe", "display", "format_day"]

#: (part, label, window end). Order matters: the first match wins, so "after
#: lunch" is the afternoon and "before lunch" the morning before "lunch" alone.
_PARTS = (
    ("end_of_day", "end of day", "17:00",
     r"\b(?:end of (?:the )?day|eod|cob|close of business|knock[- ]?off)\b"),
    ("evening", "evening", "23:59", r"\b(?:tonight|this evening|evening)\b"),
    ("afternoon", "afternoon", "17:00",
     r"\b(?:afternoon|arvo|avo|after lunch|pm)\b"),
    ("morning", "morning", "12:00",
     r"\b(?:morning|first thing|before lunch|am)\b"),
    ("lunchtime", "lunchtime", "13:00", r"\b(?:lunch(?:time)?|midday|noon)\b"),
)

_CLOCK_12 = re.compile(r"\b(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm)\b", re.IGNORECASE)
_CLOCK_24 = re.compile(r"\b([01]?\d|2[0-3])[:.]([0-5]\d)\b")

#: The word in front of a day that makes it a limit. Kept as spoken.
_LEAD = re.compile(r"^\s*(no later than|not before|prior to|before|by|until|after|from)\b",
                   re.IGNORECASE)
#: Phrases that are a limit without saying "by".
_IMPLIED_BY = re.compile(r"\b(?:end of (?:the |next )?(?:week|month)|eow|eom|this week|"
                         r"this month|next month)\b", re.IGNORECASE)


def format_day(iso):
    """'2026-10-14' -> 'Wed 14 Oct 2026', or None."""
    try:
        d = date.fromisoformat(str(iso)[:10])
    except (TypeError, ValueError):
        return None
    return "%s %d %s %d" % (d.strftime("%a"), d.day, d.strftime("%b"), d.year)


def _clock(text):
    """(hh, mm) of a spoken clock time, or None. 24-hour only with a colon."""
    m = _CLOCK_12.search(text)
    if m:
        h, mm = int(m.group(1)), int(m.group(2) or 0)
        if not 1 <= h <= 12 or mm > 59:
            return None
        h = h % 12 + (12 if m.group(3).lower() == "pm" else 0)
        return h, mm
    m = _CLOCK_24.search(text)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None


def _say_clock(h, mm):
    """(7, 0) -> '7am', (14, 30) -> '2:30pm'."""
    suffix = "am" if h < 12 else "pm"
    h12 = h % 12 or 12
    return "%d%s%s" % (h12, ":%02d" % mm if mm else "", suffix)


def describe(text, report_date):
    """Everything a surface needs about one spoken deadline.

    Returns {text, date, part, time, by, display}, or None for no deadline.
    `date` is ISO or None; `part` one of the _PARTS names or None; `time`
    'HH:MM' when a clock time was spoken; `by` 'YYYY-MM-DDTHH:MM' local wall
    clock or None; `display` what to show -- the original words when no day
    could be resolved.

    `report_date` anchors every relative phrase: the day it was SAID, never
    today, so a recording reprocessed next month still says the same thing."""
    words = " ".join(str(text or "").split())
    if not words or words.lower() in deadline_parse._PLACEHOLDERS:
        return None
    lower = words.lower()
    day = deadline_parse.resolve_deadline(words, report_date)

    clock = _clock(lower)
    part = label = end = None
    if clock is None:
        for name, lab, close, rx in _PARTS:
            if re.search(rx, lower):
                part, label, end = name, lab, close
                break

    lead = _LEAD.match(lower)
    lead = lead.group(1) if lead else ("by" if _IMPLIED_BY.search(lower) else None)

    out = {"text": words, "date": day, "part": part,
           "time": "%02d:%02d" % clock if clock else None, "by": None, "display": words}
    if not day:
        return out
    if lead == "before":
        # Met only while the named day has not started.
        out["by"] = day + "T00:00"
    elif lead in ("after", "from", "not before"):
        out["by"] = None             # a start, not a limit
    else:
        out["by"] = "%sT%s" % (day, out["time"] or end or "23:59")

    shown = format_day(day)
    if clock:
        shown += ", " + _say_clock(*clock)
    elif label:
        shown += ", " + label
    out["display"] = (lead + " " + shown) if lead else shown
    return out


def display(text, report_date):
    """Just the words to show: the resolved day, or the text as said, or None."""
    d = describe(text, report_date)
    return d["display"] if d else None
