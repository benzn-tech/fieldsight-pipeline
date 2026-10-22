"""Retrieval tools for the Ask orchestrator (spec 2026-10-15-ask-retrieval-tools).

The tool layer only: definitions the model sees, argument validation, date
resolution, and (Task 6) `execute`. WHEN a tool is called is the orchestrator's
business (companion spec, Ask orchestration v2).

Identity, company, ACL, timezone and "today" are never arguments: `execute`
injects them from the authenticated caller. A schema that let the model name a
company would let the model widen its own visibility.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date, timedelta

import ask_tools_log
import lexical_terms
import query_slots

logger = logging.getLogger()

RELATIVE_DATES = ("today", "yesterday", "day_before_yesterday", "this_week",
                  "last_week", "this_month", "last_month")
METRICS = ("duration", "count_sessions", "count_photos",
           "count_findings_safety", "count_findings_quality")
MAX_DAYS = {"day_overview": 7}
DEFAULT_MAX_DAYS = 92
MAX_K = 12

_SPOKEN_DATES = "Resolve spoken dates (e.g. '3 September', 'last Tuesday') to ISO before calling."
_DATE_DESC = ("ISO date YYYY-MM-DD in the user's local time, or one of: "
              + ", ".join(RELATIVE_DATES) + ". " + _SPOKEN_DATES)
# Without date_to a range is ONE day; the model must be told, or "since Oct 1"
# silently becomes "on Oct 1".
_DATE_TO = {"type": "string",
            "description": _DATE_DESC + " Defaults to date_from. Omitted means a single day. "
                           "Requires date_from."}
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
        "When nothing is recorded, meta.latest_before gives the latest earlier day. "
        + _SPOKEN_DATES,
        {"date_from": {"type": "string", "description": _DATE_DESC},
         "date_to": _DATE_TO,
         "site": _SITE, "person": _PERSON},
        ["date_from"]),
    _fn("search_records",
        "Search what was said or recorded about a subject, optionally within dates, "
        "a site or a person.",
        {"query": {"type": "string"},
         "date_from": {"type": "string", "description": _DATE_DESC},
         "date_to": _DATE_TO,
         "site": _SITE, "person": _PERSON,
         "k": {"type": "integer", "minimum": 1, "maximum": MAX_K}},
        ["query"]),
    _fn("latest_activity",
        # With `query`, the day is the latest one whose records name the most
        # subject words (spec 2026-10-20 §8, coverage first; at least two of them
        # when there are two or more). Extra generic words in a model-written query
        # can still decide it, so ask for one to three words. Without it: the
        # latest day with any records.
        "Find the most recent day with records and what was recorded that day. "
        "With query: the most recent day whose records name the most words of "
        "`query` (stemmed or exact; at least two when it has two or more). Give "
        "the subject in one to three words, e.g. \"concrete pour\", \"drainage\".",
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
         "date_to": _DATE_TO},
        ["metric", "date_from"]),
]

# Project documents (spec 2026-10-16). Always KNOWN (validation, dispatch); OFFERED to the
# model only when PROJECT_DOCUMENTS is on (offered_tools). Its rows are D refs (R1).
DOC_TOOL = "search_project_documents"
TOOLS.append(_fn(
    DOC_TOOL,
    "Search this project's own documents (specifications, contracts, method statements) "
    "for what they REQUIRE. Use for requirements, specs, tolerances, clauses, or 'what does "
    "the spec say'. A document says what should be done, never what happened on site.",
    {"query": {"type": "string"}, "site": _SITE,
     "k": {"type": "integer", "minimum": 1, "maximum": MAX_K}},
    ["query"]))


def offered_tools():
    """The tools the orchestrator offers the model: TOOLS, minus the documents tool while
    PROJECT_DOCUMENTS is off."""
    import project_doc_flag
    return [t for t in TOOLS if t["name"] != DOC_TOOL or project_doc_flag.on()]


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
        clamped = {}
        # Order: resolve, cap at today, then clamp. Nothing is recorded after
        # today, so a future end only shrinks the real window; capping first
        # means the clamp keeps real days.
        if "on_or_before" in raw:
            ob = _one_date(raw["on_or_before"], today, "on_or_before", end=True)
            if today is not None and ob > today:
                ob = today
                clamped["capped_to_today"] = True
            out["on_or_before"] = ob.isoformat()
        if "date_from" in raw:
            lo = _one_date(raw["date_from"], today, "date_from", end=False)
            # None means absent (a single day); "" is a malformed date.
            hi = (_one_date(raw["date_to"], today, "date_to", end=True)
                  if raw.get("date_to") is not None else
                  _one_date(raw["date_from"], today, "date_from", end=True))
            if hi < lo:
                raise _Bad("date_to")
            if today is not None and hi > today:
                if lo > today:
                    raise _Bad("date_from")   # wholly in the future: nothing to read
                hi = today
                clamped["capped_to_today"] = True
            max_days = MAX_DAYS.get(name, DEFAULT_MAX_DAYS)
            if (hi - lo).days + 1 > max_days:
                # Keep the most recent days: questions are about recent activity.
                clamped["requested_from"] = lo.isoformat()
                clamped["max_days"] = max_days
                lo = hi - timedelta(days=max_days - 1)
            out["date_from"], out["date_to"] = lo.isoformat(), hi.isoformat()
        elif "date_to" in raw:
            raise _Bad("date_from")
        if clamped:
            out["clamped"] = clamped
        return out, None
    except _Bad as bad:
        return None, {"error": "bad_args", "field": bad.field}


# --- execution (spec §3.2-§3.4) ---------------------------------------------

# Spec §3.2: <= ~3,100 tokens per tool result, measured as
# json.dumps(items, ensure_ascii=False). SEAM: the orchestrator must render tool
# results the same way -- with the default ensure_ascii=True a CJK character costs
# six characters (\uXXXX), and a Chinese day would fit 2 topics where English fits 15.
# 12,500 (was 10,000) so DEFAULT_K = 8 full WINDOW_CHARS items still fit: an item
# costs ~539 characters of JSON besides its text (ids, source key), plus a newline
# escape per line. PROVISIONAL: the item-window replay (plan Task 10) sets it from
# the measured per-item overhead p95 (2026-10-22-ask-tools-item-window review SHOULD 1).
MAX_RESULT_CHARS = 12_500
# Item text (spec 2026-10-22-ask-tools-item-window §5). A topic row, person or metric
# keeps the first MAX_TEXT characters. A recording chunk (transcript window, topic,
# session digest) or a document chunk is packed to ~2,600 characters, so it shows a
# WINDOW_CHARS window centred on the query's terms instead (`_window`): lines for
# recordings, blank-line paragraphs for documents; a topic/digest chunk keeps line 1.
MAX_TEXT = 600
WINDOW_CHARS = 1000
_KEEP_FIRST = ("topic", "session_digest")
# Spec §3.4: one rag-search batch is at most 4 calls and never plans past 20 s
# (RagSearchFunction Timeout is 23 s).
MAX_BATCH = 4
MAX_DEADLINE_S = 20
DEFAULT_K = 8
# Calls that need a query embedding ...
_SEARCH_SHAPED = ("search_records", "latest_activity", DOC_TOOL)
# ... and the subset that goes through the RECORDINGS post-search/reranker. Documents do
# not (R1): the reranker and the lexical-admit rule are tuned on recordings.
_RERANKED = ("search_records", "latest_activity")
# Keys the orchestrator maps onto a citation card (lambda_ask_agent._build_citations).
# Always present on topic/chunk items, even when None, so a card never needs a
# re-fetch; every other None-valued key is dropped to save budget.
_CITATION_KEYS = ("source", "date", "site", "site_slug", "title", "chunk_type",
                  "text", "time_start")
# Log shape: the SAME helpers rag_tools_dispatch logs with (ask_tools_log).
sub_hash = ask_tools_log.sub_hash
arg_shape = ask_tools_log.arg_shape


def _candidate_k():
    """The rerank candidate pool `_rag_answer` fetches: RERANK_CANDIDATE_K (default
    32, as lambda_ask_agent.RERANK_CANDIDATES) when ENABLE_RERANK is on, else None
    (it fetches plain k then). A malformed value is None, never an error."""
    if os.environ.get("ENABLE_RERANK", "false").lower() != "true":
        return None
    try:
        v = int(os.environ.get("RERANK_CANDIDATE_K", "32"))
    except ValueError:
        return None
    return v if v > 0 else None


class RefAssigner:
    """One per Ask turn, shared by the records tools (R#) and the orchestrator's
    web tool (W#). The same (kind, ident) always gets the same ref; each prefix
    counts on its own."""

    def __init__(self):
        self._refs, self._next = {}, {}

    def assign(self, kind, ident, prefix="R"):
        key = (str(kind), str(ident))
        if key not in self._refs:
            n = self._next.get(prefix, 1)
            self._refs[key] = f"{prefix}{n}"
            self._next[prefix] = n + 1
        return self._refs[key]

    def fresh(self, prefix="R"):
        """A new ref that nothing dedupes onto (an item with no id)."""
        n = self._next.get(prefix, 1)
        self._next[prefix] = n + 1
        return f"{prefix}{n}"


@dataclass
class ToolContext:
    caller_sub: str
    today: object                   # date | None -- the caller's LOCAL today
    deadline: float                 # absolute time.monotonic() timestamp
    lambda_client: object
    function_name: str
    refs: RefAssigner = field(default_factory=RefAssigner)
    clock: object = time.monotonic


def _embed(texts):
    import dashscope_utils
    return dashscope_utils.embed(texts)


def _post_search(query, rows, k):
    """The admit-one-lexical-hit + rerank chain the Ask path runs today
    (`_rerank_chunks` admits the lexical hit itself when rerank is off).
    Imported lazily so this module stays importable without the ask-agent."""
    import lambda_ask_agent as laa
    return laa._rerank_chunks(query, rows, k)


_TIME_START_WARNED = False


def _time_start(row):
    global _TIME_START_WARNED
    try:
        import lambda_ask_agent as laa
        return laa._citation_time_start(row)
    except Exception as exc:                            # noqa: BLE001
        # Once per process: a bundle without the ask-agent would otherwise blank
        # every citation anchor with nothing in the log.
        if not _TIME_START_WARNED:
            _TIME_START_WARNED = True
            logger.warning("ask tools time_start unavailable: %s", type(exc).__name__)
        return None


def _coerce(value):
    """A string. A non-string value is coerced, so one malformed row can never fail
    the turn."""
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _text(value):
    """Item text: a string capped at MAX_TEXT."""
    return _coerce(value)[:MAX_TEXT]


def _window_terms(query):
    """The words an item window centres on: `query_terms` minus ASKING_WORDS,
    deduplicated in order. CJK bigrams are kept (unlike `subject_terms`): a Chinese
    query over Chinese speech must centre too. Lowercase only, no ASCII step."""
    if not isinstance(query, str):
        return []
    out = []
    for t in lexical_terms.query_terms(query):
        if t not in lexical_terms.ASKING_WORDS and t not in out:
            out.append(t)
    return out


# A unit longer than the room is cut by characters; each cut end moves to the nearest
# space within this many characters, else it is a hard cut (CJK has no spaces).
SNAP_CHARS = 40
_ELLIPSIS = "…"
# `session_index._window_line`: `[HH:MM:SS] speaker: text`. Kept on a cut turn.
_TURN_PREFIX = re.compile(r"\[\d\d:\d\d:\d\d\] [^:\n]{1,60}: ")
_TURN_TIME = re.compile(r"\[(\d\d:\d\d:\d\d)\]")


def _unit_score(unit, terms):
    """(distinct terms found, total occurrences) in one unit, by lowercase substring."""
    low = unit.lower()
    found = frozenset(t for t in terms if t in low)
    return found, sum(low.count(t) for t in found)


def _cut(unit, terms, budget, keep_prefix):
    """`unit` cut to <= `budget` characters around its earliest term hit (the start
    when none hits). A transcript turn keeps its `[HH:MM:SS] spk: ` prefix. Each cut
    end moves inward to a space within SNAP_CHARS, never past the hit, and is marked
    with an inline `…`."""
    budget = max(budget, 0)
    if len(unit) <= budget:
        return unit
    prefix = ""
    m = _TURN_PREFIX.match(unit) if keep_prefix else None
    if m and len(m.group(0)) <= budget // 2:
        prefix = m.group(0)
    body = unit[len(prefix):]
    avail = budget - len(prefix) - 2 * len(_ELLIPSIS)
    if avail <= 0:
        return unit[:budget]
    pos, hl = 0, 0
    for t in terms:
        # On the original text, not an offset into body.lower(): lower() can change
        # a string's length (review NIT 1).
        hit = re.search(re.escape(t), body, re.IGNORECASE)
        if hit and (not hl or hit.start() < pos):
            pos, hl = hit.start(), len(hit.group(0))
    start = max(0, min(pos + hl // 2 - avail // 2, len(body) - avail))
    end = min(len(body), start + avail)
    if start > 0 and body[start - 1] != " ":
        sp = body.find(" ", start, start + SNAP_CHARS)
        if sp != -1 and sp + 1 <= pos:
            start = sp + 1
    if end < len(body) and body[end] != " ":
        sp = body.rfind(" ", max(start, end - SNAP_CHARS), end)
        if sp != -1 and sp >= pos + hl:
            end = sp
    piece = body[start:end]
    if start > 0:
        piece = _ELLIPSIS + piece.lstrip(" ")
    if end < len(body):
        piece = piece.rstrip(" ") + _ELLIPSIS
    return prefix + piece


def _window(text, terms, cap, sep="\n", keep_first=False):
    """The part of `text` an item shows: (shown, first_unit_index, mode).

    Spec 2026-10-22-ask-tools-item-window §4-§5, review BLOCKER 1. `mode` is
    "whole" (text <= cap, returned unchanged), "window" (centred on the terms) or
    "head" (no term hits: today's head rule, on whole units). `first_unit_index` is
    the index in text.split(sep) of the first unit shown after skipped ones (0 when
    nothing above was skipped): `_item` reads a turn's time from it.

        units = text.split(sep)            -- never splitlines(): `\\r` stays on its unit
        keep_first: units[0] always shown (topic/digest header), the rest is the body
        score(unit) = (terms found, occurrences), lowercase substring
        candidates: every run body[i..j] whose joined length plus markers fits, and
                    every single unit too long to fit even alone
        best = max by (distinct terms, occurrences, fewest units, earliest start)
        an oversize best: cut by characters around its first hit (_cut)
        a run best: extended by the unit before, then after, alternately, while it
                    fits; a side that does not fit stops, the other is still tried
        markers: a `…` unit before the run when units above were skipped, after it
                 when units below were; they count against `cap`

    "Fewest units" before "earliest" picks the tightest run around the hits, so the
    extension centres it; otherwise a lone hit lands at the bottom edge of the run.
    """
    if len(text) <= cap:
        return text, 0, "whole"
    units = text.split(sep)
    mark = len(_ELLIPSIS) + len(sep)
    head = []
    if keep_first and len(units) > 1 and len(units[0]) + len(sep) <= cap // 2:
        head = units[:1]
    room = cap - (len(head[0]) + len(sep) if head else 0)
    body = units[len(head):]
    n = len(body)
    acc = [0]
    for u in body:
        acc.append(acc[-1] + len(u))

    def cost(i, j):
        return (acc[j + 1] - acc[i] + (j - i) * len(sep)
                + (mark if i > 0 else 0) + (mark if j < n - 1 else 0))

    def shown(parts, i, j):
        return sep.join(head + ([_ELLIPSIS] if i > 0 else []) + parts
                        + ([_ELLIPSIS] if j < n - 1 else []))

    def first(i):
        return i + len(head) if i > 0 else 0

    keep_prefix = sep == "\n"
    scores = [_unit_score(u, terms) for u in body]
    if not any(found for found, _ in scores):
        j = -1
        while j + 1 < n and cost(0, j + 1) <= room:
            j += 1
        if j < 0:                                       # the first unit alone is too long
            budget = room - cost(0, 0) + len(body[0])
            return shown([_cut(body[0], (), budget, keep_prefix)], 0, 0), 0, "head"
        return shown(body[:j + 1], 0, j), 0, "head"

    best = None
    for i in range(n):
        if cost(i, i) > room:
            key = (len(scores[i][0]), scores[i][1], 0, -i)
            if best is None or key > best[0]:
                best = (key, i, None)
            continue
        found, hits = set(), 0
        for j in range(i, n):
            if cost(i, j) > room:
                break
            found |= scores[j][0]
            hits += scores[j][1]
            key = (len(found), hits, -(j - i), -i)
            if best is None or key > best[0]:
                best = (key, i, j)
    _, i, j = best
    if j is None:                                       # one oversize unit
        budget = room - cost(i, i) + len(body[i])
        return shown([_cut(body[i], terms, budget, keep_prefix)], i, i), first(i), "window"
    prev_open, next_open, turn = True, True, 0
    while prev_open or next_open:
        if turn == 0 and prev_open:
            if i > 0 and cost(i - 1, j) <= room:
                i -= 1
            else:
                prev_open = False
        elif turn == 1 and next_open:
            if j < n - 1 and cost(i, j + 1) <= room:
                j += 1
            else:
                next_open = False
        turn ^= 1
    return shown(body[i:j + 1], i, j), first(i), "window"


def _metric_text(metric, row):
    """`duration=5400 seconds`, `count_photos=0 (no_rows_for_that_day)`."""
    value = row.get("value")
    out = f"{metric}={'unknown' if value is None else value}"
    if value is not None and row.get("unit"):
        out += f" {row['unit']}"
    notes = row.get("notes") if isinstance(row.get("notes"), dict) else {}
    if notes.get("zero_kind"):
        out += f" ({notes['zero_kind']})"
    return _text(out)


def _metadata(row):
    md = row.get("metadata")
    if isinstance(md, str):
        try:
            md = json.loads(md)
        except ValueError:
            md = None
    return md if isinstance(md, dict) else {}


def _item(name, args, row, call_id):
    """(kind, dedupe ident, item-without-ref) for one dispatcher row. A chunk or
    document item also carries `_window` (its `_window` mode), which `_envelope`
    counts and removes."""
    if name == DOC_TOOL:
        # Before the shape check (R1): a document row carries chunk_text and would
        # otherwise become a recording chunk with provenance "records".
        text, _, mode = _window(_coerce(row.get("chunk_text")),
                                _window_terms(args.get("query")), WINDOW_CHARS, sep="\n\n")
        return "document", row.get("id"), {
            "kind": "document", "provenance": "documents",
            "title": row.get("title"), "heading": row.get("heading"),
            "page_from": row.get("page_from"), "page_to": row.get("page_to"),
            "document_id": row.get("document_id"), "series_id": row.get("series_id"),
            "revision": row.get("revision"), "site": row.get("site_name"),
            "site_id": row.get("site_id"), "content_type": row.get("content_type"),
            "text": text, "_window": mode}
    if name == "find_people":
        return "person", row.get("person_ref"), {
            "kind": "person", "provenance": "records", "who": row.get("name"),
            "person_ref": row.get("person_ref"), "text": _text(row.get("name"))}
    if name == "count_metric":
        # Per call: two metrics in one turn are two pieces of evidence.
        ident = f"{args.get('metric')}:{args.get('date_from')}:{args.get('date_to')}:{call_id}"
        return "metric", ident, {
            "kind": "metric", "provenance": "records", "metric": args.get("metric"),
            "text": _metric_text(args.get("metric"), row)}
    if "chunk_text" in row:
        md = _metadata(row)
        span = md.get("window_span")
        full = _coerce(row.get("chunk_text"))
        chunk_type = row.get("chunk_type")
        text, first, mode = _window(full, _window_terms(args.get("query")), WINDOW_CHARS,
                                    keep_first=chunk_type in _KEEP_FIRST)
        time_start = _time_start(dict(row, metadata=md))
        if chunk_type == "transcript_window" and first > 0:
            # The card jumps to what the model read (spec §7). Only a turn line carries
            # `[HH:MM:SS]`; anything else keeps the span start (review BLOCKER 3).
            m = _TURN_TIME.match(full.split("\n")[first])
            if m:
                time_start = m.group(1)
        return "chunk", row.get("id"), {
            "kind": "chunk", "provenance": "records",
            "date": str(row.get("report_date") or ""),
            "time": span if isinstance(span, str) else None,
            "site": row.get("site_name"), "site_slug": row.get("site_slug"),
            "who": row.get("user_name"),
            "title": row.get("topic_title") or row.get("title"),
            "text": text, "_window": mode,
            "topic_id": row.get("topic_id"), "chunk_id": row.get("id"),
            "chunk_type": chunk_type, "time_start": time_start,
            "source": row.get("source_s3_key")}
    return "topic", row.get("id"), {
        "kind": "topic", "provenance": "records",
        "date": str(row.get("report_date") or ""),
        "time": row.get("time_range"),
        "site": row.get("site_name"), "site_slug": row.get("site_slug"),
        "who": row.get("user_name"),
        "title": row.get("title") or row.get("topic_title"),
        "text": _text(row.get("summary")),
        "topic_id": row.get("id"),
        # Spec §3.1.1: the day's item counts (day_overview_rows' count subqueries).
        "n_tasks": row.get("n_tasks"), "n_findings": row.get("n_findings"),
        "chunk_type": row.get("chunk_type"), "time_start": None,
        "source": row.get("source_s3_key")}


def _size(obj):
    """Budget unit: characters of the JSON the model reads (see MAX_RESULT_CHARS)."""
    return len(json.dumps(obj, default=str, ensure_ascii=False))


def _envelope(name, call_id, args, rows, meta, ctx):
    items, used, seen = [], 2, set()                    # 2: the list's brackets
    shown = {"window": 0, "head": 0}                    # `_window` modes, for the log
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            kind, ident, item = _item(name, args, row, call_id)
        except Exception as exc:                        # noqa: BLE001 -- one bad row
            logger.warning("ask tool %s row skipped: %s", name, type(exc).__name__)
            continue
        key = None if ident in (None, "") else (kind, str(ident))
        if key is not None and key in seen:
            continue                                    # same row twice in one result
        mode = item.pop("_window", None)
        keep = _CITATION_KEYS if kind in ("topic", "chunk") else ()
        item = {k: v for k, v in item.items() if v is not None or k in keep}
        # Measured before a ref is assigned: a dropped item must not consume one.
        size = _size(dict(item, ref="R0000")) + (2 if items else 0)
        if used + size > MAX_RESULT_CHARS:
            meta["truncated"] = True
            break
        # No id: its own ref, never deduped onto another id-less row.
        prefix = "D" if name == DOC_TOOL else "R"
        ref = ctx.refs.fresh(prefix) if key is None else ctx.refs.assign(key[0], key[1], prefix)
        if key is not None:
            seen.add(key)
        items.append({"ref": ref, **item})
        used += size
        if mode in shown:
            shown[mode] += 1
    meta.setdefault("truncated", False)
    if any(shown.values()):
        # Counts only. `ask_orchestrator._header` reads named meta keys, so these
        # never reach the judge.
        meta["windowed"], meta["head"] = shown["window"], shown["head"]
    if not items and not meta.get("empty_reason") and not meta.get("error"):
        # §3.3: items [] always comes with a reason. A result that gives none
        # (malformed, or every row unusable) is a failure, not "nothing there".
        meta["error"] = "upstream"
    return {"tool": name, "id": call_id, "items": items, "meta": meta}


def _bad(call, call_id, meta):
    name = call.get("name") if isinstance(call, dict) else None
    return {"tool": name if isinstance(name, str) else None, "id": call_id,
            "items": [], "meta": meta}


def _invoke(ctx, wire):
    """{call id: result} from ONE rag-search invoke. Raises on any failure."""
    left = max(0, min(ctx.deadline - ctx.clock(), MAX_DEADLINE_S))
    resp = ctx.lambda_client.invoke(
        FunctionName=ctx.function_name, InvocationType="RequestResponse",
        Payload=json.dumps({"mode": "tools", "sub": ctx.caller_sub,
                            "deadline_s": left, "calls": wire}))
    if resp.get("FunctionError"):
        raise RuntimeError("rag-search FunctionError")
    body = json.loads(resp["Payload"].read())
    # .get: one result without an id must not fail the batch; its call has no
    # result and becomes `upstream` on its own (results.get(cid) is None).
    return {r.get("id"): r for r in body["results"]
            if isinstance(r, dict) and r.get("id") is not None}


def execute(calls, ctx):
    """One envelope per call, in order (spec §3.2). Never raises.

    Validates and normalizes args, injects identity and the caller's today,
    embeds search-shaped queries, runs every valid call in ONE rag-search
    invoke, reranks search results, assigns turn-wide refs, enforces the
    output budget. Logs names, counts and timings only."""
    try:
        return _execute(calls, ctx)
    except Exception as exc:                            # noqa: BLE001 -- last resort
        logger.warning("ask tools execute failed: %s", type(exc).__name__)
        calls = calls if isinstance(calls, list) else []
        return [_bad(c, str(i), {"error": "upstream"}) for i, c in enumerate(calls)]


def _execute(calls, ctx):
    calls = calls if isinstance(calls, list) else []
    envs = [None] * len(calls)
    pending = {}                                        # call id -> (name, args, clamped)
    for i, call in enumerate(calls):
        cid = str(i)
        if i >= MAX_BATCH:
            envs[i] = _bad(call, cid, {"error": "bad_args", "field": "calls"})
            continue
        args, err = validate_call(call, ctx.today)
        if err:
            envs[i] = _bad(call, cid, dict(err))
            continue
        clamped = args.pop("clamped", None)
        if (call["name"] == "latest_activity" and "on_or_before" not in args
                and ctx.today is not None):
            args["on_or_before"] = ctx.today.isoformat()
        pending[cid] = (call["name"], args, clamped)

    failed = {}                                         # call id -> meta
    if pending and ctx.deadline - ctx.clock() <= 0:
        # No time left: nothing can start (spec §3.4). Not even the embed is paid for.
        failed.update({cid: {"error": "timeout"} for cid in pending})
    need = [cid for cid, (name, args, _) in pending.items()
            if cid not in failed and name in _SEARCH_SHAPED and args.get("query")]
    vectors = {}
    candidate_k = _candidate_k()
    if need:
        t0 = ctx.clock()
        try:
            vecs = _embed([pending[cid][1]["query"] for cid in need])
            if len(vecs) != len(need):
                raise ValueError("embedding count mismatch")
            vectors = dict(zip(need, vecs))
        except Exception as exc:                        # noqa: BLE001
            logger.warning("ask tools embed failed: %s n=%d", type(exc).__name__, len(need))
            failed.update({cid: {"error": "upstream"} for cid in need})
        logger.info("ask tools embed n=%d ms=%d", len(need), int((ctx.clock() - t0) * 1000))

    wire = []
    for cid, (name, args, _) in pending.items():
        if cid in failed:
            continue
        w = {"id": cid, "name": name, "args": args}
        if cid in vectors:
            w["query_embedding"] = vectors[cid]
            if candidate_k and pending[cid][0] in _RERANKED:
                # The same candidate pool _rag_answer reranks over (rag-search
                # fetches max(k*2, candidate_k), capped).
                w["candidate_k"] = candidate_k
        wire.append(w)

    results = {}
    if wire:
        t0 = ctx.clock()
        try:
            results = _invoke(ctx, wire)
        except Exception as exc:                        # noqa: BLE001
            logger.warning("ask tools batch failed: %s calls=%d", type(exc).__name__, len(wire))
            failed.update({w["id"]: {"error": "upstream"} for w in wire})
        logger.info("ask tools batch calls=%d ms=%d", len(wire),
                    int((ctx.clock() - t0) * 1000))

    for cid, (name, args, clamped) in pending.items():
        r = None if cid in failed else results.get(cid)
        if r is None:
            meta, rows = dict(failed.get(cid) or {"error": "upstream"}), []
        else:
            meta = dict(r["meta"]) if isinstance(r.get("meta"), dict) else {}
            rows = r["rows"] if isinstance(r.get("rows"), list) else []
        if name in _RERANKED and args.get("query") and rows:
            k = args.get("k") or DEFAULT_K
            try:
                rows = _post_search(args["query"], rows, k)
            except Exception as exc:                    # noqa: BLE001
                logger.warning("ask tools post-search failed: %s", type(exc).__name__)
                rows = rows[:k]
        elif name == DOC_TOOL:
            rows = rows[:args.get("k") or DEFAULT_K]
        if clamped:
            meta["clamped"] = clamped
        if args.get("date_from"):
            meta.setdefault("range", {"from": args["date_from"],
                                      "to": args.get("date_to") or args["date_from"]})
        env = _envelope(name, cid, args, rows, meta, ctx)
        fields, days = arg_shape(args)
        logger.info("ask tool %s fields=%s days=%s items=%d rows=%d empty=%s err=%s "
                    "truncated=%s windowed=%d head=%d caller=%s",
                    name, fields, days, len(env["items"]), len(rows),
                    meta.get("empty_reason"), meta.get("error"), meta.get("truncated"),
                    meta.get("windowed", 0), meta.get("head", 0), sub_hash(ctx.caller_sub))
        envs[int(cid)] = env
    return envs
