"""report_sections.py -- the report a person reads.

Pure. No boto3, no psycopg, no network, no model.

A daily report carries two shapes for two audiences, and confusing them has
already cost this system once. `topics` is the machine's: `chunking.py` splits
RAG chunks straight out of `report["topics"]`, so anything that removes or
mutates it empties the search index without failing. `sections` is the person's.

Sections are shaped like the template Library's sections -- `{title, kind,
fields}` with `kind` from the Library's own vocabulary -- so that pointing
reports at a user-editable template later is a change of data source rather
than a rewrite. Nothing here reads a template yet; the shape is the point.

What changed, and why, from the device owner reading a real report:

    "作为报告，不需要知道几点几点干了什么，追溯的时候再去 query 就行，
     我希望呈现出来按 priority 排列的 action list 就行"

So the per-topic timeline -- time range, category, participants, one summary
per block -- stops being rendered. It is still in the file for query.

ON RANKING BY PRIORITY. Measured over 40 prod extractions carrying 59 action
items: high 44.1%, medium 50.8%, low 5.1%, and only 22% carry a deadline.
Sorting by priority alone puts 26 items in one undifferentiated pile, which is
not an order. So a date ranks first where there is one, and where there is not,
how many separate times the day returned to a thing ranks above the label a
model applies to half of everything.
"""

#: The Library's section vocabulary (scripts/api/template-store.js). A kind
#: outside this set has no renderer on the other side.
KINDS = frozenset({"narrative", "kpi", "list", "table", "photos"})

_PRIORITY = {"high": 0, "medium": 1, "low": 2}

#: Values that mean "nobody filled this in". `lambda_meeting_minutes` defaults a
#: missing deadline to the string "?", which is truthy -- ranked as a date it
#: would put every undated action from a meeting above every genuinely dated
#: one. Cleaned here rather than only at that one call site, because a
#: placeholder reaching the ranker is a wrong order, not a crash, and a wrong
#: order is exactly the thing nobody notices.
_PLACEHOLDERS = frozenset({
    "", "?", "-", "--", "—", "–", "n/a", "na", "tbd", "tba",
    "none", "unknown", "unspecified", "null",
})


def _clean(value):
    """A field's value, or "" when it is a stand-in for one."""
    text = str(value or "").strip()
    return "" if text.lower() in _PLACEHOLDERS else text


def build(report):
    """The ordered, non-empty sections for one report.

    Never mutates `report`. Sections with nothing in them are dropped rather
    than rendered as a heading over a blank -- a quiet day should read as a
    short report, not a form somebody failed to fill in.
    """
    report = report or {}
    topics = report.get("topics") or []

    sections = [
        _summary(report),
        _on_site(report),
        _actions(topics),
        _open_questions(topics),
        _decisions(topics),
        _issues(report),
        _safety(report),
        _photos(topics),
    ]
    return [s for s in sections if s]


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------

def _summary(report):
    """One narrative for the day.

    `executive_summary` arrives as a string from some paths and a list of
    sentences from others; both become one readable paragraph rather than
    bullets, because this is the part somebody reads instead of the timeline
    that used to be underneath it.
    """
    value = report.get("executive_summary")
    if isinstance(value, (list, tuple)):
        body = " ".join(str(v).strip() for v in value if str(v).strip())
    else:
        body = str(value or "").strip()
    return {"title": "Summary", "kind": "narrative", "body": body}


def _on_site(report):
    """What was captured. Numbers only.

    `per_recording` is a list of S3 filenames and was being rendered; it is
    plumbing and belongs nowhere near a customer.
    """
    rec = report.get("recording_session") or {}
    return {
        "title": "On Site",
        "kind": "kpi",
        "fields": ["recordings", "duration", "photos"],
        "values": {
            "recordings": rec.get("recordings", 0),
            "duration": rec.get("total_duration_display", ""),
            "photos": rec.get("photos", 0),
        },
    }


def _actions(topics):
    """Every action the day produced, once each, most-owed first."""
    seen = {}
    order = []
    for topic in topics:
        for item in topic.get("action_items") or []:
            if not isinstance(item, dict):
                continue
            text = str(item.get("action") or "").strip()
            if not text:
                continue
            key = _normalise(text)
            if key in seen:
                seen[key]["mentions"] += 1
                # First mention wins the wording; a later one fills a blank.
                for src, dst in (("responsible", "owner"), ("deadline", "due")):
                    if not seen[key][dst]:
                        seen[key][dst] = _clean(item.get(src))
                continue
            seen[key] = {
                "action": text,
                "owner": _clean(item.get("responsible")),
                "due": _clean(item.get("deadline")),
                "priority": _clean(item.get("priority")).lower(),
                "mentions": 1,
            }
            order.append(key)

    rows = [seen[k] for k in order]
    if not rows:
        return None
    for i, row in enumerate(rows):
        row["_seq"] = i
    rows.sort(key=_rank)
    for row in rows:
        row.pop("_seq", None)
    return {
        "title": "Actions",
        "kind": "table",
        "fields": ["action", "owner", "due", "priority"],
        "rows": rows,
    }


def _rank(row):
    """Dated first and soonest; then how often the day came back to it; then
    the label; then the order it was said in.

    An action with no priority sorts after `low` rather than in the middle:
    absence of a judgement is not a middling judgement, and putting it above a
    labelled item would let a silent field outrank a stated one.
    """
    dated = 0 if row["due"] else 1
    return (
        dated,
        row["due"] or "",
        -row["mentions"],
        _PRIORITY.get(row["priority"], len(_PRIORITY)),
        row["_seq"],
    )


def _open_questions(topics):
    """Asked in the session and never answered.

    A different kind of thing from an action -- nobody owns one, because nobody
    knows the answer -- and it was being concatenated onto the end of each topic
    summary, which buried both.
    """
    items = []
    for topic in topics:
        for q in topic.get("open_questions") or []:
            _add(items, q if isinstance(q, str) else (q or {}).get("question"))
        for q in topic.get("questions") or []:
            _add(items, q if isinstance(q, str) else (q or {}).get("question"))
    if not items:
        return None
    return {"title": "Open Questions", "kind": "list", "items": items}


def _decisions(topics):
    items = []
    for topic in topics:
        for d in topic.get("key_decisions") or []:
            _add(items, d if isinstance(d, str) else (d or {}).get("decision"))
    if not items:
        return None
    return {"title": "Decisions", "kind": "list", "items": items}


def _issues(report):
    rows = []
    for entry in report.get("quality_and_compliance") or []:
        if not isinstance(entry, dict):
            continue
        item = _clean(entry.get("item"))
        if not item:
            continue
        rows.append({
            "item": item,
            "status": _clean(entry.get("status")),
            "detail": _clean(entry.get("details")) or _clean(entry.get("detail")),
        })
    if not rows:
        return None
    return {"title": "Issues & Quality", "kind": "table",
            "fields": ["item", "status", "detail"], "rows": rows}


def _safety(report):
    items = []
    for entry in report.get("safety_observations") or []:
        if isinstance(entry, str):
            _add(items, entry)
        elif isinstance(entry, dict):
            _add(items, entry.get("observation") or entry.get("detail"))
    if not items:
        return None
    return {"title": "Safety", "kind": "list", "items": items}


def _photos(topics):
    items = []
    for topic in topics:
        for p in topic.get("related_photos") or []:
            _add(items, p if isinstance(p, str) else None)
    if not items:
        return None
    return {"title": "Photos", "kind": "photos", "items": items}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _add(bucket, value):
    """Append once, keeping the order things were first said in."""
    text = _clean(value)
    if not text:
        return
    key = _normalise(text)
    for existing in bucket:
        if _normalise(existing) == key:
            return
    bucket.append(text)


def _normalise(text):
    """For deciding two strings are the same thing.

    Case and trailing punctuation only. Anything cleverer starts merging
    "Order the brackets" with "Order the bracket covers", and a report that
    quietly drops an action is worse than one that lists it twice.
    """
    return " ".join(str(text).lower().split()).strip(" .,;:!-")
