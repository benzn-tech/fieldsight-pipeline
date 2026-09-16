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

AND A DEADLINE IS NOT A DATE. The prompt asks for `"deadline": "When (e.g.
'Tomorrow 08:00', 'EOD', '15:00'), or null"`, so it arrives as free text.
Sorting that as a string ordered it by first character -- `15/09/2026` first
because "1" < "2", October above September, `EOD` below October, `Tomorrow`
below `Friday`. `due_dates` turns it into days-from-the-report-date where it
honestly can and returns None where it cannot, so an unreadable deadline cannot
outrank a real one merely for containing characters. The text the model wrote is
still what gets displayed.
"""

import due_dates

#: The Library's section vocabulary (scripts/api/template-store.js). A kind
#: outside this set has no renderer on the other side.
KINDS = frozenset({"narrative", "kpi", "list", "table", "entries", "photos"})

#: `entries` is a thing, what state it is in, and one line about it:
#: `{"title": ..., "status": ..., "note": ...}`. It exists because a table was
#: the wrong shape for the two sections that describe observations. Four
#: columns of prose read as a spreadsheet of paragraphs -- the report's owner
#: called it 冗长 -- and the columns do not line up anyway, because `detail` is
#: a sentence and `status` is a word. An entry puts the word beside the thing
#: and the sentence under it.
#:
#: A table is still right for Actions: five short fields that a reader scans
#: DOWN, comparing one row's date against another's.

_PRIORITY = {"high": 0, "medium": 1, "low": 2}

#: An action that is finished is not an action to chase. org-api serves a
#: `status` on every action item (it is one of five editable columns, and a
#: person ticking something off on the Today page is what writes it), and this
#: section dropped the field -- so a report listed a job somebody had already
#: closed alongside the ones still owed, indistinguishable, and where it had a
#: past deadline it sorted to the TOP as the most overdue thing on site.
#:
#: Closed rows are kept and ranked last rather than removed. "We said we would
#: do this, and it is done" is the good half of a daily report, and dropping it
#: would make a report of a productive day shorter than one of an idle day.
_CLOSED = frozenset({"done", "completed", "complete", "closed", "cancelled",
                     "canceled", "resolved"})

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


def _join(*parts):
    """The non-empty parts, separated by a middot. One line, not four."""
    return " \u00b7 ".join([p for p in parts if p])


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
    report = report if isinstance(report, dict) else {}
    # A model writes what it likes. Every list below is filtered to the shape its
    # reader expects, because this runs BEFORE the S3 write inside a loop over
    # users: one bad shape meant no report for this user and every user after
    # them. A section that comes out short beats a lost nightly run.
    topics = [t for t in (report.get("topics") or []) if isinstance(t, dict)]

    sections = [
        _summary(report),
        _actions(topics, report.get("report_date")),
        _open_questions(topics),
        _decisions(topics),
        _dates(report),
        _issues(report),
        _safety(report),
        _photos(topics, report),
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


# `_on_site` is gone. It restated `recording_session` -- recordings, duration,
# photos -- which the viewer already renders as header facts, so the same three
# numbers appeared twice on one screen and disagreed about zero. The report's
# owner then asked for the counts to go entirely: "多少分钟。多少个字啊？多少，
# 这些都不要了". A day is described by what was decided and what is owed, not by
# how many files it took to record it.

def _actions(topics, report_date=None):
    """Every action the day produced, once each, most-owed first."""
    seen = {}
    order = []
    for topic in topics:
        for item in _as_list(topic.get("action_items")):
            if not isinstance(item, dict):
                continue
            # `_clean`, not `strip`: an action whose text is "N/A" or "TBD" is
            # not an action, and rendering it gives somebody a row to chase.
            text = _clean(item.get("action"))
            if not text:
                continue
            key = _normalise(text)
            if key in seen:
                seen[key]["mentions"] += _mention_count(item)
                # A later mention fills a blank -- and where two topics name
                # DIFFERENT owners for one action, both are named. Keeping only
                # the first silently reassigns somebody else's job.
                owner = _clean(item.get("responsible"))
                if owner and owner.lower() not in seen[key]["owner"].lower():
                    prior = seen[key]["owner"]
                    seen[key]["owner"] = (prior + ", " + owner) if prior else owner
                if not seen[key]["due"]:
                    seen[key]["due"] = _clean(item.get("deadline"))
                # Two topics naming one action, one closed and one open, means
                # it was raised again after being closed. Open wins: the report
                # must not mark something done while a later mention is still
                # asking for it.
                later = _clean(item.get("status")).lower() or "open"
                if later not in _CLOSED:
                    seen[key]["status"] = later
                continue
            seen[key] = {
                "action": text,
                "owner": _clean(item.get("responsible")),
                "due": _clean(item.get("deadline")),
                "priority": _clean(item.get("priority")).lower(),
                "status": _clean(item.get("status")).lower() or "open",
                # org-api's 0-day collapse already merged one commitment said in
                # three recordings into one row and counted it. That count is
                # authoritative -- it spans recordings, where the text match
                # below only spans topics inside one report -- so it wins where
                # it exists. Its own comment reaches the same conclusion this
                # ranking does, from the same 44% measurement: repetition is
                # evidence, a priority label mostly is not.
                "mentions": _mention_count(item),
            }
            order.append(key)

    rows = [seen[k] for k in order]
    if not rows:
        return None
    for i, row in enumerate(rows):
        row["_seq"] = i
        row["_days"] = due_dates.days_until(row["due"], report_date)
    rows.sort(key=_rank)
    for row in rows:
        row.pop("_seq", None)
        row.pop("_days", None)
    return {
        "title": "Actions",
        "kind": "table",
        "fields": ["action", "owner", "due", "priority", "status"],
        "rows": rows,
    }


def _mention_count(item):
    """How many times this was said, as the producer counted it."""
    try:
        n = int(item.get("mention_count") or 1)
    except (TypeError, ValueError):
        return 1
    return n if n > 0 else 1


def _rank(row):
    """Readably-dated first and soonest; then how often the day came back to it;
    then the label; then the order it was said in.

    "Readably" is the point. A deadline that cannot be turned into a day ranks as
    undated rather than as its own text: a string sorts by its first character,
    and `EOD` filed below a date in October.

    An action with no priority sorts after `low` rather than in the middle:
    absence of a judgement is not a middling judgement, and putting it above a
    labelled item would let a silent field outrank a stated one.

    Anything already closed sorts below all of it, whatever its date said. A
    finished job with last Tuesday's deadline is not the most overdue thing on
    site, and before `status` was carried at all it was ranked as exactly that.
    """
    days = row["_days"]
    return (
        1 if row.get("status", "open") in _CLOSED else 0,
        1 if days is None else 0,
        days if days is not None else 0,
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
        for q in _as_list(topic.get("open_questions")):
            _add(items, q if isinstance(q, str)
                 else (q.get("question") if isinstance(q, dict) else None))
        for q in _as_list(topic.get("questions")):
            _add(items, q if isinstance(q, str)
                 else (q.get("question") if isinstance(q, dict) else None))
    if not items:
        return None
    return {"title": "Open Questions", "kind": "list", "items": items}


def _decisions(topics):
    items = []
    for topic in topics:
        for d in _as_list(topic.get("key_decisions")):
            _add(items, d if isinstance(d, str)
                 else (d.get("decision") if isinstance(d, dict) else None))
    if not items:
        return None
    return {"title": "Decisions", "kind": "list", "items": items}


def _dates(report):
    """The block that is literally dates.

    `critical_dates_and_deadlines` is produced by the daily report and rendered
    in the Word document. For a design whose first ranking key is the date, the
    one section made of dates should not be the one that vanishes when the UI
    moves to sections.

    IT VANISHED ANYWAY, AND THE TESTS SAID IT DID NOT. This read
    `item|description|event|detail` and `date|deadline`. The daily prompt
    (`lambda_report_generator.py`) asks the model for
    `date_mentioned / context / who_mentioned / urgency / type`, and has for
    as long as the field has existed. Not one of those keys was read, so every
    real report produced an empty Key Dates and `build` dropped the section.
    Measured on a TEST report generated by the deployed code: **17 entries in
    `critical_dates_and_deadlines`, zero of them readable, no Key Dates
    section**.

    It stayed invisible because the frontend still had a fallback that
    rendered the raw key, and because the unit tests fed `{"item":…,
    "date":…}` -- a shape the producer has never written. A test that supplies
    its own input can confirm any reading of it.

    The old key names stay in the lookup: the meeting path and older reports
    are free to use them, and a reader is not helped by this being strict.
    """
    rows = []
    for entry in report.get("critical_dates_and_deadlines") or []:
        if isinstance(entry, str):
            text = _clean(entry)
            if text:
                rows.append({"when": "", "what": text, "who": ""})
            continue
        if not isinstance(entry, dict):
            continue
        what = _clean(entry.get("context") or entry.get("item")
                      or entry.get("description") or entry.get("event")
                      or entry.get("detail"))
        when = _clean(entry.get("date_mentioned") or entry.get("date")
                      or entry.get("deadline"))
        who = _clean(entry.get("who_mentioned") or entry.get("responsible"))
        if not (what or when):
            continue
        rows.append({"when": when, "what": what, "who": who})
    if not rows:
        return None
    # Ordered like the actions are, and for the same reason: a list of dates
    # that is not in date order is a list, not a schedule. `date_mentioned` is
    # free text ("28th", "Monday"), so the ones that cannot be read keep their
    # stated order at the end rather than being sorted by first character.
    anchor = report.get("report_date") or report.get("date")
    for i, row in enumerate(rows):
        row["_seq"] = i
        row["_days"] = due_dates.days_until(row["when"], anchor)
    rows.sort(key=lambda r: (1 if r["_days"] is None else 0,
                             r["_days"] if r["_days"] is not None else 0,
                             r["_seq"]))
    for row in rows:
        row.pop("_seq", None)
        row.pop("_days", None)
    return {"title": "Key Dates", "kind": "table",
            "fields": ["when", "what", "who"], "rows": rows}


def _issues(report):
    rows = []
    for entry in report.get("quality_and_compliance") or []:
        if not isinstance(entry, dict):
            continue
        item = _clean(entry.get("item"))
        if not item:
            continue
        rows.append({
            "title": item,
            "status": _clean(entry.get("status")),
            "note": _clean(entry.get("details")) or _clean(entry.get("detail")),
        })
    if not rows:
        return None
    return {"title": "Issues & Quality", "kind": "entries", "items": rows}


def _safety(report):
    rows = []
    entries = report.get("safety_observations")
    # A bare string here was iterated character by character, producing a Safety
    # section reading ['L', 'o', 'o', 's', 'e'].
    if isinstance(entries, str):
        entries = [entries]
    for entry in entries or []:
        if isinstance(entry, str):
            text = _clean(entry)
            if text:
                rows.append({"title": text, "status": "", "note": ""})
        elif isinstance(entry, dict):
            what = _clean(entry.get("observation") or entry.get("detail"))
            if not what:
                continue
            note = _join(_clean(entry.get("location")),
                         _clean(entry.get("recommended_action")
                                or entry.get("recommended")))
            rows.append({
                "title": what,
                # The risk level, in the same slot the quality status uses, so
                # the two sections read alike -- which is what was asked for.
                "status": _clean(entry.get("risk_level") or entry.get("severity")),
                # WHERE IT IS AND WHAT TO DO, on one line. Both are content,
                # not database fields: this section once listed only the
                # observation, so a report said a digger was operating inside
                # the exclusion zone and did not say to stop it.
                "note": note,
            })
    if not rows:
        return None
    return {"title": "Safety", "kind": "entries", "items": rows}


def _photos(topics, report=None):
    """The day's photographs, as keys something can actually fetch.

    `related_photos` holds BARE FILENAMES -- `lambda_ingest` and the report
    generator both store `s3_key.rsplit("/", 1)[-1]`. A filename cannot be
    presigned, so the viewer could only ever print the name of a photograph it
    was standing next to, and the Word document wrote a comma-separated list of
    them. Asked why photos are not in the report, the honest answer was that
    nothing downstream had been given enough to find one.

    The prefix is not a guess: it is the same one the generator lists to count
    the day's photos in the first place (`users/{user}/pictures/{date}/`).
    `name` is kept beside `key` because a reader recognises the filename and a
    fetch needs the path.
    """
    report = report or {}
    user = str(report.get("user_name") or "").strip()
    date = str(report.get("report_date") or report.get("date") or "").strip()
    prefix = "users/%s/pictures/%s/" % (user, date) if user and date else ""
    items, seen = [], set()
    for topic in topics:
        for p in _as_list(topic.get("related_photos")):
            if not isinstance(p, str):
                continue
            name = p.strip()
            if not name or name in seen:
                continue
            seen.add(name)
            # An entry that already carries a path is left alone: a future
            # writer storing full keys must not get the prefix twice.
            key = name if "/" in name else (prefix + name if prefix else "")
            items.append({"name": name, "key": key})
    if not items:
        return None
    return {"title": "Photos", "kind": "photos", "items": items}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _as_list(value):
    """A list, whatever arrived. A bare string is one item, not its characters."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _add(bucket, value):
    """Append once, keeping the order things were first said in."""
    if not isinstance(value, (str, int, float)) and value is not None:
        return
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
