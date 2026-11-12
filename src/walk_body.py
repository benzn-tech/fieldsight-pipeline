"""What a multi-unit walk's report request may carry (spec 2026-11-03).

One definition, used twice: org-api refuses a request that breaks it (400),
and checklist_reports checks the body it builds BEFORE sending, so an
automatic report is never one the door would refuse. Pure: no database.

walk = {"key": "walk:<40 hex>",
        "units": {"names": [str], "count": int|None},
        "entered": [mark] (at most MAX_MARKS),
        "stretches": [{"unit": str|None, "from": "HH:MM:SS", "to": "HH:MM:SS"}]}
with at most MAX_UNITS distinct units across the names and the stretches.
"""
import re

MAX_UNITS = 40          # R23
MAX_MARKS = 200         # R24
MAX_SEGMENTS = 200      # R24: a walk's report window; an ordinary check keeps 20

KEY_RE = re.compile(r"^walk:[0-9a-f]{40}$")
HMS_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d:[0-5]\d$")


def is_hms(value):
    return isinstance(value, str) and bool(HMS_RE.match(value))


def check(walk):
    """(clean walk, None) when `walk` is well formed, else (None, reason)."""
    if not isinstance(walk, dict):
        return None, "walk must be an object"
    key = walk.get("key")
    if not isinstance(key, str) or not KEY_RE.match(key):
        return None, "walk key is malformed"
    opening = walk.get("units")
    if not isinstance(opening, dict) or not isinstance(opening.get("names"), list) \
            or not all(isinstance(n, str) for n in opening["names"]):
        return None, "walk units must name a list of units"
    count = opening.get("count")
    if count is not None and (isinstance(count, bool) or not isinstance(count, int)):
        return None, "walk unit count must be a whole number or null"
    entered = walk.get("entered")
    if not isinstance(entered, list) or not all(isinstance(m, dict) for m in entered):
        return None, "walk entered must be a list of marks"
    if len(entered) > MAX_MARKS:
        return None, "walk has more than %d entered marks" % MAX_MARKS
    stretches = walk.get("stretches")
    if not isinstance(stretches, list):
        return None, "walk stretches must be a list"
    for s in stretches:
        if not isinstance(s, dict) or not (is_hms(s.get("from")) and is_hms(s.get("to"))):
            return None, "walk stretches must run HH:MM:SS to HH:MM:SS"
        if s.get("unit") is not None and not isinstance(s.get("unit"), str):
            return None, "walk stretch unit must be a name or null"
    named = set(opening["names"]) | {s["unit"] for s in stretches if s.get("unit")}
    if len(named) > MAX_UNITS:
        return None, "walk has more than %d units" % MAX_UNITS
    return {"key": key, "units": {"names": list(opening["names"]), "count": count},
            "entered": entered,
            "stretches": [{"unit": s.get("unit"), "from": s["from"], "to": s["to"]}
                          for s in stretches]}, None
