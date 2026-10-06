"""lambda_session_report.py — Delivery-C generate worker (Tier-2 T3).

Non-VPC (has the python-docx layer + can reach SES). Triggered by an org-api
enqueue writing `session_report_requests/{...}.json`: org-api is in-VPC and so
can neither render docx nor reach SES (CLAUDE.md BUG-36), so it hands the work
off via an S3 request artifact (the match_requests/ pattern). This worker reads
the artifact, renders the reviewed session content into a Word doc (reusing
`lambda_meeting_minutes.generate_word_document`), writes it under
`session_reports/`, optionally emails it, and writes the result the frontend
polls for at the artifact's `resultKey`.

Design: docs/superpowers/specs/2026-07-28-session-report-review-export-design.md §6.
"""
import datetime
import json
import logging
import os
import re
import time
from io import BytesIO
from urllib.parse import unquote_plus

import boto3

import checklist
import chunking
import lambda_meeting_minutes
import llm_utils
import nz_time
import photo_binding
import pipeline_trace
import report_facts
import report_photos
import report_template
import text_normalize
import transcript_window
from email_sender import get_sender
from lambda_meeting_minutes import generate_word_document

logger = logging.getLogger()
logger.setLevel(logging.INFO)

S3_BUCKET = os.environ.get("S3_BUCKET", "")
DOCX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# Photo evidence budgets. Two caps, because either one alone leaves a hole: a
# per-topic cap does not bound a forty-topic day, and a total budget alone lets
# one photo-heavy topic eat everything before the later topics are reached.
# The numbers keep the doc something a site manager actually opens on a phone,
# and keep the render inside the Lambda's memory.
#
# The per-topic cap was 4 while photographs went in at camera size (2-4 MB
# each, so the byte budget held ~4 anyway). An inspection walk is one topic
# with a photograph per spot -- 7 on the TEST day that raised it (2026-10-01)
# -- so photographs are now shrunk to what a page shows (PHOTO_MAX_EDGE) and
# the cap counts pictures a reader can use, not bytes.
#
# SIXTY A REPORT (owner, 2026-10-01): a real site day can be a long inspection
# walk, so the count is per REPORT, not per topic -- one topic may take them
# all. At page size that is ~15-20 MB of document; the byte budget is now only
# a guard against a pathological day, not the limit people meet.
#
# 60 at page size, then up to 120 shrunk automatically, then the person
# chooses (owner, 2026-10-01) -- the rules live in report_photos, shared with
# the nightly daily report.
MAX_PHOTOS_PER_REPORT = report_photos.MAX_LIMIT
MAX_PHOTOS_PER_TOPIC = MAX_PHOTOS_PER_REPORT
MAX_PHOTO_BYTES_TOTAL = 40 * 1024 * 1024
PHOTO_MAX_EDGE = report_photos.STANDARD_EDGE


def _shrink(body, edge=PHOTO_MAX_EDGE):
    """See report_photos.shrink: upright, `edge` px, JPEG; never raises."""
    return report_photos.shrink(body, edge)

_s3_client = None


def s3():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3")
    return _s3_client


def _is_day(artifact):
    return artifact.get("scope") == "day"


def _doc_key(artifact):
    """The Word doc lives under a DEDICATED session_reports/ prefix — NOT the
    nightly meeting_minutes/ path — so this on-demand Delivery-C artifact never
    collides with the report-generator's manifest machinery (BUG-18 sidestepped;
    authority-flip already de-dupes the nightly path). A day report takes the
    literal `day` segment where a session report takes its session id; the session
    routes refuse `day` as an id (spec 2026-09-15 F2)."""
    segment = "day" if _is_day(artifact) else artifact["sessionId"]
    return (f"session_reports/{artifact['folder']}/{artifact['date']}/"
            f"{segment}/{artifact['requestId']}.docx")


def _scope_ids_and_folders(artifact):
    """(session ids in scope, folders whose deletion mirror must be read).

    A day names every session it bundles, and every folder those sessions' topics
    were written under -- a merged meeting's rows live under the LEAD's folder, and
    so does its tombstone."""
    folder = artifact.get("folder")
    if _is_day(artifact):
        ids = [str(s).strip() for s in (artifact.get("sessionIds") or []) if str(s).strip()]
        folders = {f for f in (artifact.get("mirrorFolders") or []) if f}
        if folder:
            folders.add(folder)
        return ids, sorted(folders)
    sid = (artifact.get("sessionId") or "").strip()
    return ([sid] if sid else []), ([folder] if folder else [])


def _scope_result_fields(artifact):
    """What a day result must carry so the status route can re-check it."""
    if not _is_day(artifact):
        return {}
    ids, folders = _scope_ids_and_folders(artifact)
    return {"scope": "day", "sessionIds": ids, "mirrorFolders": folders}


def _humanize(key):
    return str(key).replace("_", " ").title()


def _fetch_photos(folder, date, filenames, budget, names_out=None, edge=None):
    """Download a topic's photos as open streams, newest failure tolerated.

    The renderer does no I/O and must stay that way, so the bytes are fetched
    here and handed over. `budget` is a one-element list carrying the REMAINING
    total allowance, mutated as it is spent — the caller walks the topics in
    order, so an early photo-heavy topic cannot silently starve a later one of
    its cap, only of the shared budget.

    A photo that cannot be read costs only itself. The prose is the
    deliverable; the pictures support it, and losing the report because one
    object was deleted would be the wrong trade."""
    streams = []
    for name in (filenames or [])[:MAX_PHOTOS_PER_TOPIC]:
        if budget[0] <= 0 or (len(budget) > 1 and budget[1] <= 0):
            logger.info("photo budget spent; skipping %s", name)
            break
        key = f"users/{folder}/pictures/{date}/{name}"
        try:
            body = s3().get_object(Bucket=S3_BUCKET, Key=key)["Body"].read()
        except Exception:
            logger.warning("could not read photo %s; leaving it out", key)
            continue
        body = _shrink(body, edge or PHOTO_MAX_EDGE)
        streams.append(BytesIO(body))
        if names_out is not None:
            names_out.append(name)       # which file each stream is: a skipped one shifts nothing
        budget[0] -= len(body)
        if len(budget) > 1:
            budget[1] -= 1                   # the report-wide count (MAX_PHOTOS_PER_REPORT)
    return streams


def _precise(window):
    """A window given to the second -- a spoken check's (inspection_windows),
    never one a person typed into the dialog's HH:MM picker."""
    return any(str((window or {}).get(k) or "").count(":") == 2 for k in ("from", "to"))


def _mostly_in(topic, date, win_from, win_to):
    """True when most of a topic's minutes fall inside [win_from, win_to).

    Topic times are whole minutes; a check's window is to the second. The
    Level 2 steel topic ("11:02 - 11:03") shares 14 of its 120 seconds with a
    pre-pour check that ended at 11:02:14 -- overlap alone put it, and its
    PPE action, in the pre-pour checklist."""
    parsed = chunking.parse_time_range(topic.get("time_range"))
    if not parsed:
        return False
    day = datetime.datetime.strptime(date, "%Y-%m-%d")
    start = day + datetime.timedelta(seconds=parsed[0])
    end = day + datetime.timedelta(seconds=parsed[1] + 60)       # the last minute, whole
    inside = (min(end, win_to) - max(start, win_from)).total_seconds()
    return inside > 0 and inside >= 0.5 * (end - start).total_seconds()


def _in_window(topic, date, win_from, win_to):
    """True when a topic's time_range overlaps [win_from, win_to).

    A time_range that does not parse cannot be placed, so it is in the window
    only when the window is the whole day -- where every topic of the scope is
    in it by definition. Anywhere narrower it is left out rather than guessed
    in: the coverage note would otherwise say "recorded in this window" about a
    topic that may have been recorded outside it."""
    parsed = chunking.parse_time_range(topic.get("time_range"))
    day = datetime.datetime.strptime(date, "%Y-%m-%d")
    whole_day = (win_from <= day and win_to >= day + datetime.timedelta(hours=23, minutes=59))
    if not parsed:
        return whole_day
    start = day + datetime.timedelta(seconds=parsed[0])
    end = day + datetime.timedelta(seconds=parsed[1])
    # A collapsed range (start == end, BUG-09) is a point; it is in the window
    # when the point is.
    return (start < win_to and end > win_from) or (start == end and win_from <= start < win_to)


def _offered_topics(artifact, budget, win_from, win_to, stretches=None, precise=False):
    """(offer, streams_by_ref) for the topics of this report inside the window.

    `offer` is what the prompt shows the model: a stable ref, the time range,
    the title and how many photographs it has. `streams_by_ref` is what the
    renderer places, for the topics that have any. Both come from one walk, so
    a photograph count the model is shown has bytes behind it.

    EVERY TOPIC IN THE WINDOW IS OFFERED, not only the ones with photographs.
    The ref a line ends with is how a photograph finds its line, and it is also
    the one thing in the model's answer that says which topic a line reported.
    Counting the refs that came back -- outside the model -- is what lets the
    report say which recorded topics it does not mention (see
    _coverage_note). A topic offered only when it had a photograph could not
    be counted when it had none.

    THE ORDER IS THE TOPICS' OWN, and the shared byte budget is spent walking
    it, so an early photo-heavy topic cannot silently starve a later one.

    A PHOTOGRAPH IS FETCHED ONCE even when two topics name it. Prod has
    measured 13.7% of photographs bound to more than one topic -- the binding
    unit is the session, not the day -- so without this a report would carry
    the same picture twice and charge the budget twice for it.

    A topic whose photographs could not be read is still offered, as a topic
    with none: losing the pictures must not also lose it from the count.
    """
    seen = {}
    offer, streams = [], {}
    folder = artifact.get("folder")
    content = artifact.get("content") or {}
    date = artifact.get("date") or content.get("date")
    # An interrupted check's report offers the topics of its stretches, not of
    # the other check between them (topic times are whole minutes, so one that
    # shares a minute with a stretch is still offered).
    test = _mostly_in if precise else _in_window
    in_window = [(i, t) for i, t in enumerate(content.get("topics") or [])
                 if (any(test(t, date, a, b) for a, b in stretches) if stretches
                     else test(t, date, win_from, win_to))]
    # THE WHOLE REPORT IS PLANNED BEFORE ANYTHING IS FETCHED (report_photos):
    # the person's own exclusions for the day, one copy of each photograph,
    # the size the count calls for, and -- past MAX_LIMIT -- a fair share per
    # topic rather than whatever the first topics happened to hold.
    excluded = (report_photos.read_excluded(s3(), S3_BUCKET, folder, date)
                if folder and S3_BUCKET else set())
    chosen, edge, _ = report_photos.plan(
        [("t%d" % i, t.get("related_photos") or []) for i, t in in_window], excluded)
    PLAN_STATS.update({"edge": edge, "excluded": excluded})
    for i, topic in in_window:
        ref = "t%d" % i
        names = chosen.get(ref) or []
        mine_all = [n for n in (topic.get("related_photos") or []) if n and n not in excluded]
        fresh = [n for n in names if n not in seen]
        got_names = []
        got = _fetch_photos(folder, date, fresh, budget, got_names, edge) if fresh else []
        for name, stream in zip(got_names, got):
            seen[name] = stream
        kept = [n for n in names if n in seen]
        mine = [seen[n] for n in kept]
        if mine:
            streams[ref] = mine
        offer.append({"ref": ref,
                      "title": topic.get("topic_title"),
                      "time_range": topic.get("time_range"),
                      "category": topic.get("category"),
                      "content": _topic_content(topic),
                      "photos": len(mine),
                      # Aligned with streams[ref]: which file each one is, so a
                      # photograph can be placed by WHERE it was taken.
                      "photo_names": kept,
                      # Bound to this topic, not left out by the person, and not
                      # in the report: past the report's limit, or unreadable.
                      # Counted, so a report never says less than the day had
                      # without saying so. A photograph another topic carries
                      # is not left out.
                      "photos_left_out": len([n for n in mine_all if n not in seen
                                              and not _carried_elsewhere(n, chosen, ref)])})
    return offer, streams


# The last plan's size and exclusions, for the result's provenance. Module
# state is safe here: one invocation renders one report at a time.
PLAN_STATS = {}


def _carried_elsewhere(name, chosen, ref):
    return any(name in names for r, names in chosen.items() if r != ref)


def _unanchored(prose, skip_titles=()):
    """[{section, text}] for every line of the answer that names no topic.

    Counted outside the model, like the coverage note: the report is written
    from the record (report_template._record_block), and a line that reports
    no topic is either a summary of several the model forgot to tag or
    something the record does not hold. Not judged here -- counted, so the
    number can be watched before anything is ever dropped for it. A table's
    header and rule rows, "Nothing here." and checklist sections (rebuilt and
    evidence-checked by code) are not lines of reporting.
    """
    skip = {(t or "").strip().lower() for t in skip_titles}
    out = []
    for sec in prose:
        if (sec.get("title") or "").strip().lower() in skip:
            continue
        paragraphs = sec.get("paragraphs") or []
        refs = sec.get("line_refs") or []
        header_next = True
        for i, text in enumerate(paragraphs):
            t = (text or "").strip()
            is_row = "|" in t
            if lambda_meeting_minutes._TABLE_RULE_RE.match(t):
                continue
            if is_row and header_next and i + 1 < len(paragraphs) and \
                    lambda_meeting_minutes._TABLE_RULE_RE.match((paragraphs[i + 1] or "").strip()):
                continue                              # the header row
            header_next = not is_row
            if not t or t.rstrip(".").lower() == "nothing here":
                continue
            if not (refs[i] if i < len(refs) else []):
                out.append({"section": sec.get("title"), "text": t[:160]})
    return out


def _glossed(value, pairs):
    """Every string inside `value` with the glossary applied (text_normalize:
    whole-word, case-kept). Keys and non-strings pass through; nothing else
    about the shape changes."""
    if isinstance(value, str):
        return text_normalize.normalize(value, pairs)
    if isinstance(value, list):
        return [_glossed(v, pairs) for v in value]
    if isinstance(value, dict):
        return {k: _glossed(v, pairs) for k, v in value.items()}
    return value


_ITEM_TEXT_KEYS = ("text", "decision", "question", "description", "observation",
                   "finding", "action", "summary", "title")


def _item_text(item):
    if isinstance(item, dict):
        for k in _ITEM_TEXT_KEYS:
            if isinstance(item.get(k), str) and item[k].strip():
                return item[k].strip()
        return ""
    return str(item or "").strip()


def _topic_content(topic):
    """A topic's own content, as the record the report is written from
    (report_template._record_block): what the page shows for it."""
    out = []
    if (topic.get("summary") or "").strip():
        out.append("Summary: " + " ".join(topic["summary"].split()))
    for label, key in (("Decided", "key_decisions"), ("Open question", "open_questions"),
                       ("Safety", "safety_flags"), ("Finding", "findings")):
        for item in topic.get(key) or []:
            text = _item_text(item)
            if text:
                out.append("%s: %s" % (label, text))
    for a in topic.get("action_items") or []:
        text = _item_text(a)
        if text:
            who = (a.get("responsible") or a.get("owner") or "") if isinstance(a, dict) else ""
            out.append("Action: %s%s" % (text, (" (%s)" % who) if who else ""))
    return out


def _referenced(sections):
    """Every topic ref the answer names, on a line or on a `[covers:]` line."""
    out = set()
    for section in sections:
        for refs in section.get("line_refs") or []:
            out.update(refs)
        out.update(section.get("covers") or [])
    return out


COVERAGE_TITLE = "Also recorded"
COVERAGE_INTRO = "Recorded in this window, but not referred to by any line above:"


def _coverage_note(offer, sections):
    """The section the report ends with when a recorded topic went unmentioned,
    or None when every offered topic was named somewhere.

    WHY THIS IS WRITTEN HERE AND NOT BY THE MODEL. A section description can
    tell the model what to leave out, and the owner has decided descriptions
    keep that power ("NO DATA TODAY" is a feature). Probe 3 measured a
    description beating the house rule 10 times out of 10 with the fence in
    place, so nothing written INTO the prompt can make leaving something out
    visible. This note is built after the answer, from a count, and rendered
    by our code: no description reaches it, and it cannot be reworded, moved
    or dropped by one.

    WHAT IT RESTS ON, stated so nobody quotes it as more: the refs are the
    model's own statement of which topic a line reported. It catches a topic
    the model did not write about -- the shape a description-driven omission
    takes -- and it does not catch a line tagged with a topic it did not
    actually report. The wording says "not referred to", which is what was
    counted, rather than "left out", which was not.

    Each line carries its topic's ref, so a photograph of a topic nobody wrote
    about lands under the line that names it instead of under whatever heading
    happened to be last.
    """
    named = _referenced(sections)
    missing = [t for t in offer if t["ref"] not in named]
    if not missing:
        return None
    paragraphs, line_refs = [COVERAGE_INTRO], [[]]
    for t in missing:
        when = (t.get("time_range") or "").strip() or "time not recorded"
        title = (t.get("title") or "").strip() or "Untitled topic"
        paragraphs.append("- %s  %s" % (when, title))
        line_refs.append([t["ref"]])
    return {"title": COVERAGE_TITLE, "paragraphs": paragraphs, "level": 1,
            "covers": [], "line_refs": line_refs, "coverage_note": True}


_SUMMARY_TITLE_RE = re.compile(r"\b(summary|overview)\b", re.IGNORECASE)


def _summary_titles(template):
    """Titles of the plan's overview sections: the standard Summary module, or
    any section called a summary or an overview."""
    out = []
    def walk(sections):
        for s in sections or []:
            if ((s.get("module") or {}).get("key") == "summary"
                    or _SUMMARY_TITLE_RE.search(s.get("title") or "")):
                out.append(s.get("title") or "")
            walk(s.get("children"))
    walk((template or {}).get("sections"))
    return out


def _place_photos(sections, streams_by_ref, no_photos=(), captions=None, places=None):
    """Put each topic's photographs as close as the model said they belong.

    Returns (under_a_line, under_a_section, fell_to_the_end) as photograph
    counts. Three tiers, most precise first, each a fallback for the one above:

      1. DIRECTLY UNDER THE LINE that names the topic -- a paragraph, a list
         item or a table row ending in `[t1]`. This is what the owner asked
         for: the photograph beside what was said about it, as the Timeline
         shows it.
      2. At the end of a SECTION whose `[covers: ...]` line names it. The
         prompt no longer asks for this; it stays because a model that writes
         it anyway should not lose the photograph for having done so.
      3. Under the LAST HEADING, for anything nobody claimed -- the same floor
         the prompt states for text nobody covered. A photograph that vanished
         because the model forgot a tag would be indistinguishable from one
         that was never taken.

    A photograph appears ONCE. A day's work does not divide neatly and two
    lines naming the same topic is not an error, but the picture printed twice
    would read as one. It goes to the MOST SPECIFIC line naming the topic --
    a table row, then a list item, then a paragraph; the first of equals.
    "First line" alone put every inspection photograph under the Daily
    Summary's prose and none in the section that listed the inspections
    (TEST, Ben_Lin_test2 2026-10-01): the overview cites everything first.

    A SUMMARY HOLDS NO PHOTOGRAPHS (owner, 2026-10-01): sections titled in
    `no_photos` are skipped by tiers 1 and 2. A topic only the summary named
    falls to tier 3, where each topic's photographs go under a caption naming
    it (`captions`: ref -> "title (time)") -- still bound to their topic.
    """
    if not streams_by_ref or not sections:
        return 0, 0, 0
    skip = {(t or "").strip().lower() for t in no_photos}
    sections_all = sections
    sections = [s for s in sections_all
                if (s.get("title") or "").strip().lower() not in skip] or sections_all[-1:]
    at_line = at_section = 0
    taken = set()

    def claim(ref):
        if ref in taken or ref not in streams_by_ref:
            return []
        taken.add(ref)
        return list(streams_by_ref[ref])

    cands = {}                                  # ref -> [(rank, order, section, line, text)]
    order = 0
    for s, section in enumerate(sections):
        paragraphs = section.get("paragraphs") or []
        has_table = any(lambda_meeting_minutes._TABLE_RULE_RE.match(p or "") and "|" in (p or "") for p in paragraphs)
        for i, refs in enumerate(section.get("line_refs") or []):
            text = (paragraphs[i] if i < len(paragraphs) else "") or ""
            rank = 3 if has_table and "|" in text else 2 if text.lstrip().startswith(("- ", "* ")) else 1
            for ref in refs:
                if ref in streams_by_ref:
                    cands.setdefault(ref, []).append((rank, order, s, i, text))
                order += 1

    def put(s, i, streams):
        after = sections[s].setdefault("photos_after", {})
        after[i] = after.get(i, []) + streams

    # BY PLACE FIRST, THEN BY HOW SPECIFIC (owner, 2026-10-01): an inspection
    # written as a Ground floor row and a Level 1 row puts each photograph in
    # the row naming where it was taken. The place wins over the rank: when
    # the checklist left Ground floor unanswered, its photographs went into
    # the Level 1 row -- the only row the topic had -- although a Quality line
    # said "Ground floor inspection carried out" (TEST run, same day). A
    # photograph whose place no line names goes to the most specific line.
    def best(lines):
        top_rank = max(c[0] for c in lines)
        return next(c for c in lines if c[0] == top_rank)

    for ref in sorted(cands, key=lambda r: cands[r][0][1]):
        mine = claim(ref)
        if not mine:
            continue
        where = (places or {}).get(ref) or []
        for j, stream in enumerate(mine):
            place = where[j] if j < len(where) else None
            naming = [c for c in cands[ref] if photo_binding.names_place(c[4], place)]
            hit = best(naming) if naming else best(cands[ref])
            put(hit[2], hit[3], [stream])
        at_line += len(mine)

    for section in sections:
        mine = []
        for ref in section.get("covers") or []:
            mine.extend(claim(ref))
        if mine:
            section["photo_streams"] = (section.get("photo_streams") or []) + mine
            at_section += len(mine)

    leftover = []
    last = sections_all[-1]
    for ref in streams_by_ref:
        mine = claim(ref)
        if not mine:
            continue
        leftover.extend(mine)
        caption = (captions or {}).get(ref)
        if caption:
            last["paragraphs"] = list(last.get("paragraphs") or []) + ["Photos: " + caption]
            last["line_refs"] = list(last.get("line_refs") or []) + [[]]
            last.setdefault("photos_after", {})[len(last["paragraphs"]) - 1] = mine
        else:
            last["photo_streams"] = (last.get("photo_streams") or []) + mine
    return at_line, at_section, len(leftover)


def _content_to_minutes(artifact):
    """Map the reviewed session content + the user's confirmed fields into the
    `minutes_data` shape `generate_word_document` consumes (T4).

    The fixed meeting-minutes layout has no arbitrary-placeholder slots (a real
    per-company template engine is the fast-follow, once the template store moves
    server-side — spec §2/§10.6), so the user's declared fields render generically
    as labeled Executive-Summary lines: any field the modal collects shows up with
    zero per-field code (spec §5). Our topic shape (`render_report_shape`) maps to
    the doc's shape 1:1 except action items' `responsible` -> `owner`."""
    content = artifact.get("content") or {}
    fields = artifact.get("fields") or {}

    folder, date = artifact.get("folder"), content.get("date") or artifact.get("date")
    budget = [MAX_PHOTO_BYTES_TOTAL]

    topics = []
    for t in (content.get("topics") or []):
        photos = _fetch_photos(folder, date, t.get("related_photos"), budget)
        topics.append({
            "topic_title": t.get("topic_title"),
            "category": t.get("category") or "general",
            "time_range": t.get("time_range") or "",
            "participants": t.get("participants") or [],
            "summary": t.get("summary") or "",
            "key_decisions": t.get("key_decisions") or [],
            "action_items": [{"action": a.get("action"),
                              "owner": a.get("responsible"),      # our shape -> the doc's shape
                              "deadline": a.get("deadline"),
                              "priority": a.get("priority") or "medium"}
                             for a in (t.get("action_items") or [])],
            "open_questions": [],
            # Absent, not empty, when there is nothing — so the renderer's
            # `if topic.get('photo_streams')` needs no second check.
            **({"photo_streams": photos} if photos else {}),
        })

    minutes = {
        "meeting_date": content.get("date"),
        "attendees": artifact.get("attendees") or content.get("participants") or [],
        "topics": topics,
    }
    exec_summary = [f"{_humanize(k)}: {v}" for k, v in fields.items()
                    if v not in (None, "", [], {})]
    if exec_summary:
        minutes["executive_summary"] = exec_summary

    title = artifact.get("title") or content.get("title") or "Session report"
    return minutes, title


def _write_result(result_key, payload):
    s3().put_object(Bucket=S3_BUCKET, Key=result_key,
                    Body=json.dumps(payload), ContentType="application/json")


def _send_email(artifact):
    recipients = artifact.get("recipients") or []
    title = artifact.get("title") or "Site report"
    subject = f"FieldSight report — {title}"
    body_text = (f"The report \"{title}\" for {artifact.get('date', '')} is ready in FieldSight.")
    sender = get_sender()
    for to in recipients:
        sender.send(to, subject, body_text)


def _session_was_deleted(artifact):
    """Is this session in the day's deletion mirror?

    Owner decision (2026-09-16): a mirror-read failure now FAILS CLOSED. This
    used to log and return False ("proceed as if nothing was deleted"), which
    could put a removed recording into a document and an email; that lenient
    posture is superseded for this path. On a read failure this raises, and
    `process_request`'s existing exception handling records a `status: error`
    result instead of rendering or mailing anything.

    `lambda_session_finalize._session_was_deleted` is a sibling that still has
    the old lenient posture -- this decision was scoped to the report worker,
    not to that sibling. `lambda_org_api._session_was_removed` was already
    strict, backing read endpoints where a failed check costs one reader one
    refresh; this function now matches that posture for the same reason a
    read endpoint does, even though the two are still separate functions.

    Both spellings are compared: the mirror carries whatever `sessionBase` the
    delete endpoint had, and this artifact's `sessionId` is bare hex.

    Logged before raising, because a permission fault here looks exactly like
    "nothing was deleted".

    A day request is deleted when ANY of its sessions is, in ANY of its folders'
    mirrors.
    """
    date = artifact.get("date")
    ids, folders = _scope_ids_and_folders(artifact)
    if not (date and ids and folders):
        return False
    try:
        import boto3

        import deletion_mirror
        client = boto3.client("s3")
        deleted = set()
        for folder in folders:
            deleted |= set(deletion_mirror.deleted_sessions(client, S3_BUCKET, folder, date))
    except Exception:
        logger.exception("report: deletion mirror unreadable for %s on %s -- failing closed, "
                         "not rendering or mailing", folders, date)
        raise
    for sid in ids:
        bare = sid[3:] if sid.startswith("sid") else sid
        if sid in deleted or bare in deleted or f"sid{bare}" in deleted:
            return True
    return False


# The function has Timeout: 900 and llm_utils retries up to four times at
# LLM_HTTP_TIMEOUT (300) each, so an unbounded ladder outlives the function and writes
# no result at all -- the poller then spins forever. Bound it well inside the timeout.
# Used only as a FALLBACK when the invocation carries no `context` (unit tests, or
# any caller that never got one) -- `context.get_remaining_time_in_millis()` is the
# authority whenever it is available (see `_model_budget_seconds`).
GENERATION_BUDGET_SECONDS = float(os.environ.get("GENERATION_BUDGET_SECONDS", "600"))

# Reserved out of whatever time is left for the docx render + the result write that
# must still happen AFTER the model answers -- without this reserve the model call
# could legitimately use every remaining millisecond and still get SIGKILLed one
# step from finishing, which writes no result at all.
RENDER_AND_WRITE_RESERVE_SECONDS = float(
    os.environ.get("RENDER_AND_WRITE_RESERVE_SECONDS", "30"))


def _remaining_seconds(context):
    """Lambda's own account of what is left on this invocation, in seconds.
    None when there is no context (a unit test calling process_request directly,
    or any caller that never got one) or it does not offer the method."""
    getter = getattr(context, "get_remaining_time_in_millis", None)
    if getter is None:
        return None
    try:
        return getter() / 1000.0
    except Exception:
        return None


def _model_budget_seconds(context):
    """Seconds available for whatever comes next (a read phase, or the model call),
    reserving RENDER_AND_WRITE_RESERVE_SECONDS for what must still happen after the
    model answers. Falls back to the fixed GENERATION_BUDGET_SECONDS when there is
    no context to ask -- the authority this replaces whenever a real one exists.
    Called again right before the model call so it reflects time the read phase
    actually spent, not an estimate made before it ran."""
    remaining = _remaining_seconds(context)
    if remaining is None:
        return GENERATION_BUDGET_SECONDS
    return remaining - RENDER_AND_WRITE_RESERVE_SECONDS


def _segment_gaps(date, segments):
    """[(gap_start, gap_end)] between consecutive stretches of a window, or []."""
    if not isinstance(segments, list) or len(segments) < 2:
        return []
    out = []
    for a, b in zip(segments, segments[1:]):
        start, end = _clock(date, a["to"]), _clock(date, b["from"])
        if end > start:
            out.append((start, end))
    return out


def _clock(date, hhmm):
    """A wall-clock time on the report's own date. No timezone conversion happens
    anywhere on this path (spec 2026-09-15 global constraints).

    'HH:MM' or 'HH:MM:SS': a spoken check's window is to the second
    (inspection_windows) -- "back to the level one" at 11:02:05 and the next
    check at 11:02:14 are in one minute."""
    fmt = "%Y-%m-%d %H:%M:%S" if str(hhmm).count(":") == 2 else "%Y-%m-%d %H:%M"
    return datetime.datetime.strptime("%s %s" % (date, hhmm), fmt)


def _action_items_for_prompt(content, offer=None):
    """The actions the model is given: extraction's, not its own reading of the
    transcript. Owner and date are already recorded against them.

    `offer` (the topics in this report's window, _offered_topics) limits them
    to those topics' actions. Without it every action of the day came along: on
    TEST (2026-10-06) a pre-pour checklist for 10:59-11:02 listed the Level 2
    PPE action from 11:03."""
    keep = None if offer is None else {int(t["ref"][1:]) for t in offer
                                       if str(t.get("ref", "")).startswith("t")}
    out = []
    for i, topic in enumerate(content.get("topics") or []):
        if keep is not None and i not in keep:
            continue
        for a in (topic.get("action_items") or []):
            out.append({"action": a.get("action") or a.get("text"),
                        "owner": a.get("owner") or a.get("responsible"),
                        "deadline": a.get("deadline")})
    return out


_COVERS_RE = re.compile(r"^\[covers:\s*([^\]]*)\]$", re.IGNORECASE)
_REF_RE = re.compile(r"t\d+", re.IGNORECASE)
# `... signed off by the engineer. [t1]` / `[t1, t3]` / `[T1 and T3]`, at the END
# of a line or of a table CELL. End-of-line only printed the tag in a customer's
# checklist, where a row puts it at the end of a cell: "Ground floor inspection
# carried out [t2] | |" (TEST, 2026-10-01). Still anchored, so a bracket in the
# middle of a sentence -- "[sic]", a citation, "Section [t1] of the contract" --
# is never read as a reference; and only `t` followed by digits.
_LINE_TAG_RE = re.compile(
    r"\s*\[\s*(t\d+(?:\s*(?:,|and|&)\s*t\d+)*)\s*\](?=\s*(?:\||$))", re.IGNORECASE)


def _line_refs(line):
    """(line without its tags, [refs]) -- the refs lowercased, unique, in order."""
    refs = []
    for m in _LINE_TAG_RE.finditer(line):
        for ref in _REF_RE.findall(m.group(1)):
            ref = ref.lower()
            if ref not in refs:
                refs.append(ref)
    if not refs:
        return line, []
    return _LINE_TAG_RE.sub("", line).rstrip(), refs


def _covers_refs(line):
    """The topic refs on a `[covers: ...]` line, lowercased, in order, unique.

    Deliberately forgiving about what surrounds them and strict about their
    shape: the model writes this line, and a model that writes `[covers: t1 and
    t3]` or `[covers: T1,T3]` means the same thing. What it cannot do is invent
    a topic -- every ref is checked against the ones actually offered before a
    photograph moves anywhere.
    """
    m = _COVERS_RE.match(line)
    if not m:
        return []
    body = m.group(1).strip()
    if body.lower() == report_template.COVERS_NONE:
        return []
    out = []
    for ref in _REF_RE.findall(body):
        ref = ref.lower()
        if ref not in out:
            out.append(ref)
    return out


def _prose_sections(text):
    """Split the model's markdown back into {title, paragraphs}. Anything before the
    first heading is kept under an empty title rather than dropped."""
    sections, current = [], {"title": "", "paragraphs": [], "level": 1,
                             "covers": [], "line_refs": []}
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        # A table whose first column is "#" (`# | Action | Owner`) is a header
        # row, not a heading: read as one it split the table off its section.
        if line.startswith("#") and not line.lstrip("#").strip().startswith("|"):
            if current["title"] or current["paragraphs"]:
                sections.append(current)
            # THE DEPTH IS PART OF THE HEADING and used to be thrown away with
            # the hashes. A sub-section asked for as `####` came back as `####`
            # and was rendered at the same level as its parent, which reads as
            # the nesting having been ignored -- the fault it was meant to fix,
            # wearing a different face.
            depth = len(line) - len(line.lstrip("#"))
            current = {"title": line.lstrip("#").strip(), "paragraphs": [],
                       "level": 2 if depth > 3 else 1, "covers": [],
                       "line_refs": []}
        elif _COVERS_RE.match(line.strip()):
            # OUR OWN SCAFFOLDING, and it never reaches the page. The model is
            # asked to end each section with it so the renderer knows which
            # topics that section reported -- see report_template._covers_block
            # for why the model is asked rather than the renderer guessing.
            current["covers"] = _covers_refs(line.strip())
        elif line.strip():
            # The tag comes off before the line is kept, so it can never reach
            # the page -- including when the line is a table row, where it
            # would otherwise sit in the last cell.
            text, refs = _line_refs(line.strip())
            if text:
                current["paragraphs"].append(text)
                current["line_refs"].append(refs)
    if current["title"] or current["paragraphs"]:
        sections.append(current)
    return [s for s in sections if s["title"] or s["paragraphs"]]


def _put_document(artifact, buf):
    """Puts a generated docx at the same address a topics-assembled one would use,
    and returns its key -- mirrors the inline put in `process_request` below."""
    doc_key = _doc_key(artifact)
    s3().put_object(Bucket=S3_BUCKET, Key=doc_key,
                    Body=buf.getvalue(), ContentType=DOCX_CONTENT_TYPE)
    return doc_key


def _generate_document(artifact, context=None):
    """Returns (buffer, meta). Raises on anything that must not produce a document."""
    gen = artifact["generate"]
    # THE BODY ARRIVES WITH THE REQUEST. This function runs non-VPC and cannot
    # reach Aurora, so a template a company wrote in the Library could never be
    # read from here. org-api resolves it in the VPC and inlines it, and inlines
    # the file-backed ones the same way so there is ONE path rather than two.
    #
    # The fallback to disk is for artifacts enqueued BEFORE this field existed
    # and still sitting in the bucket when this deploys. It is deliberately not
    # a fallback for a uuid: report_template.load_template rejects anything that
    # is not a slug, so a stored template with no inlined body raises
    # TemplateNotFound rather than silently writing the report to some other
    # template -- which would make the template name in the result a lie.
    template = gen.get("templateBody")
    if not template:
        template = report_template.load_template(gen["templateId"], int(gen["templateVersion"]))
    # An artifact enqueued before org-api started saying this carries an inlined
    # body and no source. It is treated as customer-written, because the two
    # mistakes are not symmetrical: calling a reviewed template customer-written
    # costs a fence around text that did not need one, and calling a customer
    # template reviewed hands unreviewed text the instruction layer. Loaded from
    # disk just above, it is ours by construction.
    source = gen.get("templateSource")
    if not source:
        source = (report_template.SOURCE_LIBRARY if gen.get("templateBody")
                  else report_template.SOURCE_BUILTIN)
    # THE GLOSSARY FIRST: everything the report is written from -- the topics,
    # their actions, the transcript -- carries the company's corrected names,
    # so the model never sees the wrong spelling to copy (owner, 2026-10-02).
    glossary = (artifact.get("reportFacts") or {}).get("aliases") or []
    if glossary:
        _trace_glossary(artifact.get("content") or {}, glossary)
        artifact["content"] = _glossed(artifact.get("content") or {}, glossary)
    content = artifact.get("content") or {}
    date = artifact.get("date") or content.get("date")
    window = artifact.get("window") or {}
    win_from = _clock(date, window.get("from") or "00:00")
    win_to = _clock(date, window.get("to") or "23:59")

    # An excluded topic wholly outside THIS window cannot appear in it and cannot
    # be proven placeable-but-irrelevant unless its time_range actually parses --
    # see transcript_window.excluded_spans for the fail-closed ruling on the
    # unparseable case (that one still raises, window or no window).
    spans = transcript_window.excluded_spans(date, artifact.get("excludedTopics") or [],
                                             win_from, win_to)
    # An interrupted check (window.segments): the gaps between its stretches
    # are left out exactly like an excluded topic's span -- the other check
    # done in between is not this one.
    gaps = _segment_gaps(date, window.get("segments"))
    spans = spans + gaps

    read_budget = _model_budget_seconds(context)
    if read_budget <= llm_utils.MIN_USEFUL_SECONDS:
        raise RuntimeError(
            "generation budget exhausted before reading the window: %.1fs left "
            "after reserving %.1fs for the render and result write"
            % (read_budget, RENDER_AND_WRITE_RESERVE_SECONDS))
    read_deadline = time.time() + read_budget

    client = s3()
    picked = transcript_window.select_keys(client, S3_BUCKET, artifact["folder"], date,
                                           win_from, win_to)
    turns = transcript_window.drop_spans(
        transcript_window.assemble(client, S3_BUCKET, picked, deadline=read_deadline), spans)
    # Only the speech INSIDE the window. select_keys picks whole audio files
    # that overlap it, and every turn of a file used to come along: on TEST
    # (2026-10-06) a pre-pour check ending 11:02:14 was filled from the Level 2
    # steel check later in the same 2-minute file ("No -- glasses and gloves").
    turns = [t for t in turns if (t.get("until") or t["at"]) > win_from and t["at"] < win_to
             or t["at"] == win_from]
    if not turns:
        raise RuntimeError("no recorded speech in this window after exclusions")
    if glossary:
        # The same text the prompt reads is the text checklist evidence is
        # checked against, so a quote of the corrected name still matches.
        turns = [dict(t, line=text_normalize.normalize(t.get("line") or "", glossary))
                 for t in turns]

    # THE PHOTOGRAPHS ARE FETCHED BEFORE THE PROMPT IS BUILT, because the
    # prompt tells the model how many each topic has, and that number has to
    # have bytes behind it.
    photo_budget = [MAX_PHOTO_BYTES_TOTAL, MAX_PHOTOS_PER_REPORT]
    topic_offer, photo_streams = _offered_topics(
        artifact, photo_budget, win_from, win_to, precise=_precise(window),
        stretches=[(_clock(date, sg["from"]), _clock(date, sg["to"]))
                   for sg in window.get("segments") or []] if gaps else None)

    # REPORT DETAILS AND WEATHER ARE OURS. Those sections leave the plan the
    # model sees and are written from the facts we hold (report_facts.py);
    # built now, before the model call, so a slow weather fetch spends the
    # read budget rather than the render reserve.
    model_template, code_placements = report_facts.split_plan(template)
    code_sections, code_meta = report_facts.build(
        code_placements, artifact, client, S3_BUCKET, nz_time.nz_today().isoformat(),
        span=report_facts.recorded_span([t.get("time_range") for t in topic_offer],
                                        [t.get("at") for t in turns]))

    prompt = report_template.render_prompt(
        model_template,
        {"folder": artifact["folder"], "date": date,
         "from": window.get("from") or "00:00", "to": window.get("to") or "23:59",
         "recordings": len(picked)},
        _action_items_for_prompt(content, topic_offer),
        "\n".join(t["line"] for t in turns),
        source=source,
        topics=topic_offer)

    # Recomputed from `context` (not reused from `read_budget`) because this is the
    # actual authority on what is left after the read phase ran, not an estimate
    # made before it did.
    model_budget = _model_budget_seconds(context)
    if model_budget <= llm_utils.MIN_USEFUL_SECONDS:
        raise RuntimeError(
            "generation budget exhausted before the model call: %.1fs left "
            "after reserving %.1fs for the render and result write"
            % (model_budget, RENDER_AND_WRITE_RESERVE_SECONDS))
    text, err = llm_utils.call_llm(prompt, max_tokens=8000, deadline=model_budget,
                                   caller="session_report")
    if err or not (text or "").strip():
        raise RuntimeError(err or "empty answer from model")

    prose = _prose_sections(text)
    # CHECKLISTS ARE REBUILT HERE, before anything counts the answer: only rows
    # whose evidence is in the transcript survive, in the customer's order and
    # wording, and every item nobody addressed stays blank (checklist.py).
    checklist_reports = checklist.apply(
        prose, template, "\n".join(t["line"] for t in turns))
    model_copies = report_facts.drop_model_copies(prose, code_placements)
    unanchored = _unanchored(prose, skip_titles=list(checklist_reports))
    if unanchored:
        logger.info("record: %d line(s) name no topic", len(unanchored))
    # Counted here, after the answer and outside it -- see _coverage_note.
    note = _coverage_note(topic_offer, prose)
    named = _referenced(prose)
    not_referenced = [t for t in topic_offer if t["ref"] not in named]
    at_line, at_section, orphaned = _place_photos(
        prose + ([note] if note else []), photo_streams,
        no_photos=_summary_titles(template),
        captions={t["ref"]: ("%s (%s)" % (t.get("title") or "Untitled", t.get("time_range"))
                             if t.get("time_range") else (t.get("title") or "Untitled"))
                  for t in topic_offer},
        # Where each photograph was taken, by the day's location markers --
        # the same stays the binding used (photo_binding.place_at).
        places={t["ref"]: [photo_binding.place_at(
                    (artifact.get("reportFacts") or {}).get("locations") or [],
                    photo_binding.photo_time(n)) for n in t.get("photo_names") or []]
                for t in topic_offer})
    placed = at_line + at_section
    if topic_offer:
        logger.info("coverage: %d topics offered, %d referenced, not referenced: %s",
                    len(topic_offer), len(topic_offer) - len(not_referenced),
                    ",".join(t["ref"] for t in not_referenced) or "none")
    if photo_streams:
        # Counted, not assumed, and BY TIER. "Did the model tag the lines we
        # asked it to" is the question this feature turns on, and only the
        # output end can answer it: a prompt that contains the request is not
        # a model that obeyed it.
        logger.info("photos: %d offered, %d under a line, %d under a section, "
                    "%d fell to the last heading",
                    sum(len(v) for v in photo_streams.values()),
                    at_line, at_section, orphaned)

    # PAST THE LIMIT, THE REPORT SAYS SO (owner, 2026-10-01): written by us,
    # at the end, so a reader knows the day had more photographs than the
    # document carries. No link: a customer document carries no app URL.
    left_out = sum(t.get("photos_left_out") or 0 for t in topic_offer)
    if left_out:
        included = sum(t.get("photos") or 0 for t in topic_offer)
        line = ("Photographs: %d taken in this window; %d included in this report."
                % (included + left_out, included))
        tail = note if note else prose[-1]
        tail["paragraphs"] = list(tail.get("paragraphs") or []) + [line]
        tail["line_refs"] = list(tail.get("line_refs") or []) + [[]]

    # Put in after the photographs were placed, so none can fall to them.
    report_facts.insert(prose, code_placements, code_sections,
                        (template.get("catch_all") or {}).get("title"))

    buf = lambda_meeting_minutes.generate_prose_document(
        artifact.get("title") or gen.get("templateName") or template.get("name") or "Report",
        # The date only (owner, 2026-10-01): the window asked for is mostly
        # "everything", 00:00 - 23:59, which says nothing; when there is a
        # Report Details section it carries what was actually recorded.
        date,
        prose,
        _action_items_for_prompt(content, topic_offer),
        closing=note)
    # WHICH TEMPLATE THIS WAS comes from the REQUEST, not from the template's
    # own text. The files in report_templates/ carry `template_id` and
    # `version` inside them; a template written in the Library does not -- its
    # body is sections, catch_all, excluded_subjects and style, and nothing
    # else. Reading identity off the body worked for exactly as long as every
    # template was a file, and raised KeyError('template_id') the first time a
    # customer generated from their own template. The error reached the screen
    # as the word 'template_id' and nothing else.
    #
    # `gen` is the right place regardless: it is what was ASKED for, and it is
    # already what the status endpoint echoes back as provenance.
    meta = {"generated": True,
            "templateId": gen.get("templateId") or template.get("template_id"),
            "templateVersion": gen.get("templateVersion") or template.get("version"),
            # For the filename, and only for it. A uuid identifies the template
            # to the system; the NAME is what the person who made it called it,
            # and the file lands in their Downloads folder. It is recorded in
            # the result rather than looked up later because the presign runs
            # in-VPC and the name it wants is a fact about this generation.
            "templateName": gen.get("templateName") or template.get("name"),
            "model": llm_utils.active_model(),
            "promptChars": len(prompt),
            # Provenance for the one thing that cannot be read back off the
            # document: a photograph under the last heading looks exactly like
            # a photograph the model placed there on purpose.
            "photosPlaced": placed,
            "photosUnderALine": at_line,
            "photosUnderASection": at_section,
            "photosUnplaced": orphaned,
            "photosLeftOut": sum(t.get("photos_left_out") or 0 for t in topic_offer),
            # The size the count called for (report_photos), and how many the
            # person chose to leave out of the day's reports.
            "photoEdge": PLAN_STATS.get("edge"),
            "photosExcludedByChoice": len(PLAN_STATS.get("excluded") or ()),
            # What the coverage note was built from. `topicsNotReferenced` is
            # exactly what the note printed; it is here because the note is in
            # a Word file and this is not.
            "topicsOffered": len(topic_offer),
            # Per checklist: items, answered, dropped (with why), and the
            # evidence each answer stood on -- the audit trail the Word file
            # does not carry.
            "checklists": checklist_reports,
            # Which sections we wrote, and where each site's weather came
            # from: the nightly record, computed here, or none.
            "codeFilled": code_meta,
            "modelCopiesDropped": model_copies,
            # Lines that report no topic of the record -- watched, not yet
            # acted on (see _unanchored).
            "linesWithoutATopic": unanchored,
            "topicsNotReferenced": [{"ref": t["ref"], "title": t.get("title"),
                                     "time_range": t.get("time_range")}
                                    for t in not_referenced]}
    return buf, meta


def process_request(artifact, context=None):
    """Render one enqueued request → Word doc (+ optional email) → result JSON.

    `context` (optional): the Lambda invocation context, threaded through to the
    generate path so it can bound itself against `get_remaining_time_in_millis()`
    instead of a fixed budget guessed at the top of the function. None in tests
    that call this directly, or in the non-generate path, which does not need it."""
    result_key = artifact["resultKey"]
    request_id = artifact.get("requestId")

    scope_fields = _scope_result_fields(artifact)
    if _is_day(artifact) and not scope_fields["sessionIds"]:
        # A day with no sessions cannot be checked against any mirror, so it is not rendered.
        _write_result(result_key, {"status": "error", "requestId": request_id,
                                   "error": "day request carries no sessionIds"})
        return

    # A session deleted before this request is rendered must not become a DOCX in
    # S3, and must not be emailed.
    #
    # `GET /report/status` stops the POLL from serving a removed session, but this
    # worker is S3-triggered: a delete landing between org-api's enqueue and this
    # run -- or an event redelivery hours later -- reaches neither guard. It is
    # the same defect fixed in `lambda_session_finalize` one lambda over, and it
    # was the last surface in the deletion enumeration still carrying it.
    #
    # Checked BEFORE the render, so nothing is produced from content that must
    # not leave. The outcome is RECORDED because the requester polls `resultKey`;
    # a silent skip leaves that poll spinning forever, which is a different bug
    # wearing this fix's clothes.
    # The mirror check itself must not escape uncaught: a read failure here used to
    # be swallowed inside _session_was_deleted (return False), which this branch
    # removed -- it now raises. That raise is caught HERE, not left to whichever
    # downstream try happens to wrap this call, so it guards BOTH the `generate`
    # path below and the legacy assemble-and-render path, not just one of them.
    try:
        deleted = _session_was_deleted(artifact)
    except Exception as exc:                      # noqa: BLE001 -- recorded, not retried
        logger.exception("report: deletion mirror check failed for %s", request_id)
        _write_result(result_key,
                      dict({"status": "error", "requestId": request_id,
                            "error": str(exc)}, **scope_fields))
        return
    if deleted:
        logger.info("report: %s was deleted -- not rendering, not sending", request_id)
        _write_result(result_key, {"status": "skipped", "requestId": request_id,
                                   "reason": "recording deleted", **scope_fields})
        return

    if artifact.get("generate"):
        try:
            buf, meta = _generate_document(artifact, context)
        except Exception as exc:                      # noqa: BLE001 -- recorded, not retried
            logger.exception("report: generation failed for %s", artifact.get("requestId"))
            pipeline_trace.event("template_report", "error",
                                 detail={"error": type(exc).__name__,
                                         "template": (artifact.get("generate") or {}).get("templateName")})
            _write_result(artifact["resultKey"],
                          dict({"status": "error", "requestId": artifact.get("requestId"),
                                "error": _failure_message(exc)},
                               **_scope_result_fields(artifact)))
            return
        doc_key = _put_document(artifact, buf)
        _trace_report(meta)
        _write_result(artifact["resultKey"],
                      dict({"status": "done", "requestId": artifact.get("requestId"),
                            "docKey": doc_key, "emailed": False}, **meta,
                           **_scope_result_fields(artifact)))
        return

    try:
        minutes, title = _content_to_minutes(artifact)
        buf = generate_word_document(minutes, title)
        if buf is None:
            # DOCX layer missing / disabled — record it, don't crash the trigger.
            _write_result(result_key, {"status": "error", "requestId": request_id,
                                       "error": "document generation unavailable", **scope_fields})
            return
        doc_key = _doc_key(artifact)
        s3().put_object(Bucket=S3_BUCKET, Key=doc_key,
                        Body=buf.getvalue(), ContentType=DOCX_CONTENT_TYPE)
        emailed = False
        if artifact.get("deliver") == "email":
            _send_email(artifact)
            emailed = True
        _write_result(result_key, {"status": "done", "requestId": request_id,
                                   "docKey": doc_key, "emailed": emailed, **scope_fields})
    except Exception as e:
        logger.exception("session report generation failed for %s", request_id)
        _write_result(result_key, {"status": "error", "requestId": request_id,
                                   "error": _failure_message(e), **scope_fields})


def _trace_glossary(content, glossary):
    """Which glossary terms this report's text actually contained -- the
    keyword hits -- and the ones it did not, on the recording's trace."""
    try:
        text = json.dumps(content, ensure_ascii=False)
        for a in glossary:
            n = text_normalize.occurrences(text, a.get("wrong_term"))
            pipeline_trace.event("glossary", "applied" if n else "no_match",
                                 detail={"wrong_term": a.get("wrong_term"),
                                         "right_term": a.get("right_term"), "replaced": n})
    except Exception:
        logger.debug("glossary trace skipped", exc_info=True)


def _trace_report(meta):
    """The template report on the recording's trace: which template, what the
    code wrote, where the photos went, and each checklist -- items answered,
    and which were dropped and why. Counts and names only."""
    pipeline_trace.event("template_report", "ok", detail={
        "template": meta.get("templateName"), "version": meta.get("templateVersion"),
        "model": meta.get("model"), "topics_offered": meta.get("topicsOffered"),
        "topics_not_referenced": len(meta.get("topicsNotReferenced") or []),
        "lines_without_a_topic": len(meta.get("linesWithoutATopic") or []),
        "code_filled": sorted((meta.get("codeFilled") or {}).keys()),
        "photos_placed": meta.get("photosPlaced"), "photos_unplaced": meta.get("photosUnplaced"),
        "photos_left_out": meta.get("photosLeftOut")})
    for title, rep in (meta.get("checklists") or {}).items():
        rep = rep or {}
        pipeline_trace.event(
            "checklist", "omitted_by_model" if rep.get("omitted_by_model")
            else ("answered" if rep.get("answered") else "no_match"),
            detail={"checklist": title, "items": rep.get("items"),
                    "answered": rep.get("answered"),
                    "dropped": [d.get("reason") for d in rep.get("dropped") or []]})


def _failure_message(exc):
    """A sentence, not a token.

    `str(KeyError("template_id"))` is `"'template_id'"`. That went straight into
    the result, and the screen showed a customer the single quoted word
    `'template_id'` with no download and no next step -- a string that reads
    like a leak rather than a message, because it is one.

    Every exception type has this problem to some degree: `str(IndexError())`
    is empty, `str(TimeoutError())` often is too. So the type is always named
    and a plain-English lead always precedes it. The detail stays attached,
    because the person reporting this is the fastest route to whoever fixes it
    and a screenshot is what they will send.
    """
    detail = str(exc).strip()
    kind = type(exc).__name__
    if not detail:
        return "The report could not be generated (%s)." % kind
    return "The report could not be generated (%s: %s)." % (kind, detail)


def lambda_handler(event, context):
    for rec in event.get("Records", []):
        s3rec = rec.get("s3") or {}
        key = (s3rec.get("object") or {}).get("key")
        bucket = (s3rec.get("bucket") or {}).get("name") or S3_BUCKET
        if not key:
            continue
        key = unquote_plus(key)          # S3 notifications URL-encode the key
        obj = s3().get_object(Bucket=bucket, Key=key)
        artifact = json.loads(obj["Body"].read().decode("utf-8"))
        pipeline_trace.begin("session-report", user_folder=artifact.get("folder"),
                             date=artifact.get("date"), session=artifact.get("sessionId"),
                             company_id=artifact.get("companyId"))
        try:
            process_request(artifact, context)
        finally:
            pipeline_trace.flush(s3, S3_BUCKET)
    return {"ok": True}
