"""Retrieval tools for the Ask orchestrator (spec 2026-10-15-ask-retrieval-tools).

The tool layer only: definitions the model sees, argument validation, date
resolution, and (Task 6) `execute`. WHEN a tool is called is the orchestrator's
business (companion spec, Ask orchestration v2).

Identity, company, ACL, timezone and "today" are never arguments: `execute`
injects them from the authenticated caller. A schema that let the model name a
company would let the model widen its own visibility.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

import query_slots

RELATIVE_DATES = ("today", "yesterday", "day_before_yesterday", "this_week",
                  "last_week", "this_month", "last_month")
METRICS = ("duration", "count_sessions", "count_photos",
           "count_findings_safety", "count_findings_quality")
MAX_DAYS = {"day_overview": 7}
DEFAULT_MAX_DAYS = 92
MAX_K = 12

_DATE_DESC = ("ISO date YYYY-MM-DD in the user's local time, or one of: "
              + ", ".join(RELATIVE_DATES) + ".")
_SITE = {"type": "string", "description": "Optional project/site name or id."}
_PERSON = {"type": "string",
           "description": "Optional person: a person_ref from find_people, or a name."}


def _fn(name, description, properties, required):
    return {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties,
                           "required": required, "additionalProperties": False}}


TOOLS = [
    _fn("day_overview",
        "List what was recorded on a day or a few days (up to 7): every topic with "
        "time, site, who, and a one-line summary. Use for 'what did we do yesterday'. "
        "When nothing is recorded, meta.latest_before gives the latest earlier day.",
        {"date_from": {"type": "string", "description": _DATE_DESC},
         "date_to": {"type": "string", "description": _DATE_DESC + " Defaults to date_from."},
         "site": _SITE, "person": _PERSON},
        ["date_from"]),
    _fn("search_records",
        "Search what was said or recorded about a subject, optionally within dates, "
        "a site or a person.",
        {"query": {"type": "string"},
         "date_from": {"type": "string", "description": _DATE_DESC},
         "date_to": {"type": "string", "description": _DATE_DESC},
         "site": _SITE, "person": _PERSON,
         "k": {"type": "integer", "minimum": 1, "maximum": MAX_K}},
        ["query"]),
    _fn("latest_activity",
        "Find the most recent day with records (optionally: the most recent day a "
        "subject came up) and what was recorded that day.",
        {"query": {"type": "string"},
         "on_or_before": {"type": "string", "description": _DATE_DESC},
         "site": _SITE, "person": _PERSON},
        []),
    _fn("find_people",
        "Resolve a name to the people this user can see. Returns every match; "
        "ask the user when more than one comes back.",
        {"name": {"type": "string"}},
        ["name"]),
    _fn("count_metric",
        "A number from the database, never estimated: recording duration, sessions, "
        "photos, safety or quality findings, over a date range.",
        {"metric": {"type": "string", "enum": list(METRICS)},
         "date_from": {"type": "string", "description": _DATE_DESC},
         "date_to": {"type": "string", "description": _DATE_DESC}},
        ["metric", "date_from"]),
]
_BY_NAME = {t["name"]: t for t in TOOLS}
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class _Bad(Exception):
    def __init__(self, field):
        super().__init__(field)
        self.field = field


def _one_date(value, today, field, end):
    if not isinstance(value, str) or not value.strip():
        raise _Bad(field)
    v = value.strip()
    if v in RELATIVE_DATES:
        if today is None:
            raise _Bad("tz")
        lo, hi = query_slots.span(today, v)
        return hi if end else lo
    if not _ISO.match(v):
        raise _Bad(field)
    try:
        return date.fromisoformat(v)
    except ValueError:
        raise _Bad(field)


def validate_call(call, today):
    """(normalized_args, None) or (None, {"error": "bad_args", "field": f})."""
    try:
        name = call.get("name") if isinstance(call, dict) else None
        if name not in _BY_NAME:
            raise _Bad("name")
        raw = call.get("args") or {}
        if not isinstance(raw, dict):
            raise _Bad("args")
        schema = _BY_NAME[name]["parameters"]
        for key in raw:
            if key not in schema["properties"]:
                raise _Bad(key)
        for key in schema["required"]:
            if raw.get(key) in (None, ""):
                raise _Bad(key)
        out = {}
        for key in ("query", "name", "site", "person"):
            if key in raw:
                if not isinstance(raw[key], str) or not raw[key].strip():
                    raise _Bad(key)
                out[key] = raw[key].strip()
        if "k" in raw:
            # bool is an int subclass: a model sending k=true must not read as k=1.
            if (not isinstance(raw["k"], int) or isinstance(raw["k"], bool)
                    or not 1 <= raw["k"] <= MAX_K):
                raise _Bad("k")
            out["k"] = raw["k"]
        if "metric" in raw:
            if raw["metric"] not in METRICS:
                raise _Bad("metric")
            out["metric"] = raw["metric"]
        if "on_or_before" in raw:
            out["on_or_before"] = _one_date(raw["on_or_before"], today, "on_or_before",
                                            end=True).isoformat()
        if "date_from" in raw:
            lo = _one_date(raw["date_from"], today, "date_from", end=False)
            hi = (_one_date(raw["date_to"], today, "date_to", end=True)
                  if raw.get("date_to") else
                  _one_date(raw["date_from"], today, "date_from", end=True))
            if hi < lo:
                raise _Bad("date_to")
            max_days = MAX_DAYS.get(name, DEFAULT_MAX_DAYS)
            if (hi - lo).days + 1 > max_days:
                out["clamped"] = {"requested_to": hi.isoformat(), "max_days": max_days}
                hi = lo + timedelta(days=max_days - 1)
            out["date_from"], out["date_to"] = lo.isoformat(), hi.isoformat()
        elif "date_to" in raw:
            raise _Bad("date_from")
        return out, None
    except _Bad as bad:
        return None, {"error": "bad_args", "field": bad.field}
