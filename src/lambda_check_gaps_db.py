"""
Lambda: check-gaps-db -- the in-VPC hop of "check done" (spec 2026-10-20).

Invoked synchronously by CheckGapsFunction (non-VPC), the same split as
AskAgent -> RagSearch: the VPC has no egress, so the function that calls the STT
vendor and the model cannot reach Aurora, and this one, which can, never leaves
the VPC. No S3. One write, and only that one: a walk_answers row (below).

Event:  {"caller_sub": "<cognito sub>", "pressed_at": <epoch seconds>}
Result: {"user": {id, company_id, folder_name} | None,
         "owner_company_id": str | None,
         "session": {session_id, company_id, site_id, kind, status, opened_at,
                     last_segment_at} | None,
         "templates": [{id, name, scope, body}],
         "live_windows": [{name, kind, start_at, end_at, end_source, template_id,
                           report_date, units, entered, walk_key}],
         "walk": {walk_key: {key, units, entered, sessions, start_at, spans, answers}}
                 -- only when a live window carries a walk_key (spec 2026-11-03)}
        or {"error": "..."}. Never raises: the caller is waiting with a person
        on the other end.

Event:  {"action": "write_walk_answer", "caller_sub", "walk_key", "unit", "item_text",
         "answer": "Yes" | "No" | "N/A" | None, "comment", "move_to_unit" | None,
         "quote", "answered_at": "HH:MM:SS"}   (multi-unit walks, Task 11, R4/R31)
Result: {"status": "saved", "id", "company_id", "folder", "date", "walk_key"}
        or {"error": "..."} with nothing written.
        The hop's only write: one walk_answers row, committed. It checks first
        that the walk is the caller's (his folder), that `unit` and
        `move_to_unit` are units of the walk, and that `item_text` is an item of
        the walk's checklist. The row is under the walk's company (R22), by the
        caller, source 'device'. The walk report is re-queued by CheckGaps
        (item-writer, async), never from here.

The only person this reads about is `caller_sub`, which the Ask worker took from
the API authorizer. No session, folder or company is accepted from the event.
"""
import json
import logging
import re

import checklist
from db.connection import close_cached_connection, get_cached_connection, get_connection
import nz_time
from repositories import (inspection_windows, meeting_session, recordings, report_templates,
                          sites, users, walk_answers)
import tenancy_status
import units as units_mod

logger = logging.getLogger()
logger.setLevel(logging.INFO)

SESSION_WINDOW_S = 300

WALK_KEY_RE = re.compile(r"^walk:[0-9a-f]{40}$")
HMS_RE = re.compile(r"^\d{2}:\d{2}:\d{2}$")
ANSWERS = ("Yes", "No", "N/A")
#: walk_confirm.MOVE_NOT_IN_WALK: the device's handler answers by the next rule.
MOVE_NOT_IN_WALK = "move_to_unit not in walk"
MAX_QUOTE = 4000
MAX_COMMENT = 2000


def lambda_handler(event, context):
    """Release the pooled connection on every exit (lambda_rag_search's reason:
    a warm container holding one keeps Aurora from pausing). No `action` is the
    read it always was."""
    event = event or {}
    try:
        action = event.get("action")
        if action == "write_walk_answer":
            return _write_walk_answer(event)
        if action is not None:
            return {"error": "unknown action"}
        return _facts(event)
    except Exception as e:                            # noqa: BLE001 -- see "Never raises"
        if event.get("action") is not None:
            # Type and action only: a psycopg DETAIL ("Failing row contains ...")
            # carries the row's values, the spoken quote among them.
            logger.error("check-gaps-db: failed action=%s error=%s",
                         event.get("action"), type(e).__name__)
        else:
            logger.exception("check-gaps-db: failed")
        return {"error": "db"}
    finally:
        close_cached_connection()


def _jsonable(value):
    # uuids, timestamps and dates as strings: Lambda serialises the return value
    # with the plain json encoder.
    return json.loads(json.dumps(value, default=lambda v: v.isoformat() if hasattr(v, "isoformat") else str(v)))


def _site(conn, user, session):
    """The site item-writer files this recording under, by its rungs in the same
    order (lambda_item_writer, the ladder before `owner_company_id`): the app's
    tagged recording, the session's own site, then the day's majority site. The
    windows it wrote are under that site's company (project-owned tenancy), so a
    different answer here reads nobody's windows.

    The group-lead rung is left out: a merged artifact's base is `grp{gid}`, and
    this reads one device session's live state, never a merged one. resolve_site
    (the legacy membership guess) is left out too: it is a company-pinned guess,
    and the session's or his own company is the same guess without a query."""
    folder = user.get("folder_name")
    opened = session.get("opened_at")
    local = nz_time.to_nz(opened) if opened is not None else None
    date = local.date().isoformat() if local is not None else None
    base = "sid%s" % session["session_id"]
    if folder and date:
        site = recordings.site_for_media(conn, user["id"], folder, date, base)
        if site:
            return site
    if session.get("site_id"):
        site = sites.get_site(conn, session["site_id"])
        if site:
            return site
    if folder and date:
        return recordings.site_for_day(conn, user["id"], folder, date)
    return None


def _walks(conn, companies, folder, live_windows):
    """{walk_key: the walk's facts} for each walk among the live windows, or {}.

    check_done chooses the window, so every walk present is returned and it
    picks the chosen window's. A walk runs across the day's recordings, so its
    windows are read for the whole day, in the same company scope as the live
    windows (for_day has one company per call). Its on-site answers are read in
    the companies its own rows are under -- the site's (R22) -- never the
    caller's home company just for being in scope.

    key/units/entered/sessions/answers, plus start_at (the opening's start,
    R27) and spans: [{session, segments: [{from, to}]}], one per window of the
    walk (its stretches; a window still open has to None), so the gap pass
    reads only the walk's own speech, never another check done between
    units (R28)."""
    out = {}
    days = {}
    for w in live_windows or []:
        key = w.get("walk_key")
        if not key or key in out:
            continue
        date = str(w.get("report_date") or "")[:10]
        rows = []                                  # (company, row), every recording's
        for c in companies:
            if (c, date) not in days:
                days[(c, date)] = inspection_windows.for_day(conn, c, folder, date)
            rows += [(c, r) for r in days[(c, date)] if r.get("walk_key") == key]
        if not rows:
            continue
        rows.sort(key=lambda cr: str(cr[1].get("start_at") or ""))
        opening = next((r for c, r in rows if r.get("template_id") and units_mod.walk_key(
            c, folder, date, r["template_id"], r.get("start_at")) == key), rows[0][1])
        owners = list(dict.fromkeys(c for c, _ in rows))
        spans = [{"session": r.get("session"),
                  "segments": [{"from": s.get("from"), "to": s.get("to")}
                               for s in r.get("segments") or [] if isinstance(s, dict)]
                  or [{"from": r.get("start_at"), "to": r.get("end_at")}]}
                 for _, r in rows if r.get("session")]
        out[key] = {
            "key": key,
            "units": opening.get("units"),
            "entered": [m for _, r in rows for m in r.get("entered") or [] if isinstance(m, dict)],
            "sessions": list(dict.fromkeys(r["session"] for _, r in rows if r.get("session"))),
            "start_at": opening.get("start_at"),
            "spans": spans,
            "answers": walk_answers.for_walk(conn, owners, key),
        }
    return out


def _write_walk_answer(event):
    """Its own transaction, not the cached autocommit connection (that one is
    for reads): the checks and the insert commit together, or not at all."""
    with get_connection() as conn:
        out = write_walk_answer(conn, event)
    logger.info("check-gaps-db: write_walk_answer status=%s",
                out.get("status") or "error:%s" % out.get("error"))
    return _jsonable(out)


def _walk_units(opening, rows):
    """The walk's units: the opening's names, then every unit entered in any of
    its windows, normalized (units.normalize_unit). "Kitchen" is not a unit."""
    out = []
    opened = opening.get("units") if isinstance(opening.get("units"), dict) else {}
    marks = [m.get("unit") for r in rows for m in r.get("entered") or [] if isinstance(m, dict)]
    for raw in list(opened.get("names") or []) + marks:
        u = units_mod.normalize_unit(raw) if isinstance(raw, str) else None
        if u and u not in out:
            out.append(u)
    return out


def _checklist_item(body, item_text):
    """The template's own wording of `item_text` (every checklist section of the
    template, sub-sections included), or None."""
    want = " ".join(str(item_text).split()).casefold()
    for items in checklist.checklist_sections(body).values():
        for i in items:
            if " ".join(str(i).split()).casefold() == want:
                return str(i)
    return None


def write_walk_answer(conn, event):
    """Check, then write one walk_answers row. Returns the saved facts or
    {"error": ...}; nothing is written unless every check passes."""
    sub = event.get("caller_sub")
    if not sub:
        return {"error": "caller_sub required"}
    key = event.get("walk_key")
    if not isinstance(key, str) or not WALK_KEY_RE.match(key):
        return {"error": "walk_key invalid"}
    answer, move = event.get("answer"), event.get("move_to_unit")
    if answer is not None and answer not in ANSWERS:
        return {"error": "answer invalid"}
    if answer is None and not move:
        return {"error": "answer or move_to_unit required"}
    if answer is not None and move:
        return {"error": "answer with a move_to_unit"}
    at = event.get("answered_at")
    if not isinstance(at, str) or not HMS_RE.match(at):
        return {"error": "answered_at invalid"}
    for k in ("unit", "item_text", "comment", "quote", "move_to_unit"):
        if event.get(k) is not None and not isinstance(event.get(k), str):
            return {"error": "%s invalid" % k}

    user = users.get_user_by_sub(conn, sub)
    if not user:
        return {"error": "unknown caller"}
    blocked = tenancy_status.caller_blocked(conn, user, where="check-gaps-db")
    if blocked:
        return {"error": blocked}
    folder = user.get("folder_name")
    rows = inspection_windows.walk_rows(conn, folder, key) if folder else []
    if not rows:
        return {"error": "walk not found"}

    # The opening is the window whose start gives the key; its company is the
    # walk's (R22). A walk with no such window and one company is that company's.
    opening, company = None, None
    for r in rows:
        if r.get("template_id") and units_mod.walk_key(
                str(r["company_id"]), folder, str(r.get("report_date"))[:10],
                str(r["template_id"]), r.get("start_at")) == key:
            opening, company = r, str(r["company_id"])
            break
    if opening is None:
        companies = {str(r["company_id"]) for r in rows}
        if len(companies) != 1:
            return {"error": "walk company unclear"}
        opening = next((r for r in rows if r.get("template_id")), rows[0])
        company = companies.pop()
    date = str(opening.get("report_date"))[:10]

    walk_units = _walk_units(opening, rows)
    unit = units_mod.normalize_unit(event.get("unit") or "")
    if not unit or unit not in walk_units:
        return {"error": "unit not in walk"}
    target = None
    if move:
        target = units_mod.normalize_unit(move)
        if not target or target not in walk_units:
            return {"error": MOVE_NOT_IN_WALK}

    version = (report_templates.get_version(conn, opening["template_id"], None)
               if opening.get("template_id") else None)
    item = _checklist_item((version or {}).get("body") or {}, event.get("item_text") or "")
    if item is None:
        return {"error": "item not in checklist"}

    new_id = walk_answers.add(conn, {
        "company_id": company, "user_folder": folder, "report_date": date, "walk_key": key,
        "unit": unit, "item_text": item, "answer": answer,
        "comment": (event.get("comment") or "")[:MAX_COMMENT], "move_to_unit": target,
        "source": "device", "quote": (event.get("quote") or "")[:MAX_QUOTE],
        "voice_note_id": None, "answered_at": at, "answered_by": str(user["id"])})
    return {"status": "saved", "id": new_id, "company_id": company, "folder": folder,
            "date": date, "walk_key": key}


def _facts(event):
    sub = event.get("caller_sub")
    if not sub:
        return {"error": "caller_sub required"}
    at = int(event.get("pressed_at") or 0)
    out = {"user": None, "owner_company_id": None, "session": None, "templates": [],
           "live_windows": []}
    conn = get_cached_connection()
    user = users.get_user_by_sub(conn, sub)
    if not user:
        return out
    # Company offboarding gate (spec 2026-11-01 section 5.1): this hop's error shape;
    # check_done answers any `error` with its spoken error. Flag off: logs and allows.
    blocked = tenancy_status.caller_blocked(conn, user, where="check-gaps-db")
    if blocked:
        return {"error": blocked}
    out["user"] = {"id": user["id"], "company_id": user.get("company_id"),
                   "folder_name": user.get("folder_name")}
    session = meeting_session.latest_for_user_near(conn, user["id"], at, SESSION_WINDOW_S)
    if session:
        site = _site(conn, user, session)
        owner = (site or {}).get("company_id") or session.get("company_id") or user.get("company_id")
        out["owner_company_id"] = owner
        out["session"] = {k: session.get(k) for k in (
            "session_id", "company_id", "site_id", "kind", "status", "opened_at", "last_segment_at")}
        # No company anywhere: nothing to match against, which CheckGaps answers
        # as no_template. Querying with None would send "None" as a uuid and fail
        # the hop as a database error instead.
        out["templates"] = (inspection_windows.checklist_templates(conn, owner, user["id"])
                            if owner else [])
        companies = list(dict.fromkeys(str(c) for c in (owner, session.get("company_id"),
                                                        user.get("company_id")) if c))
        out["live_windows"] = inspection_windows.for_session(
            conn, companies, user.get("folder_name"), "sid%s" % session["session_id"])
        try:
            walks = _walks(conn, companies, user.get("folder_name"), out["live_windows"])
        except Exception:                             # noqa: BLE001 -- R29
            # The check still runs, as a single-unit one: a walk that cannot be
            # read must not cost him the answer.
            logger.exception("check-gaps-db: walk read failed; answering without the walk")
            walks = {}
        if walks:
            out["walk"] = walks
    logger.info("check-gaps-db: session=%s templates=%d windows=%d walks=%d",
                bool(out["session"]), len(out["templates"]), len(out["live_windows"]),
                len(out.get("walk") or {}))
    return _jsonable(out)
