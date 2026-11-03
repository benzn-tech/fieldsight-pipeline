"""A spoken check becomes its filled checklist report, on its own.

Owner, 2026-10-06: a site manager doing a concrete pre-pour check has the
checklist in his head -- the same one as the company's Library template. He
says "starting the pre-pour check" and goes through it: this is done, that is
not, why, by when. Back at the office the filled checklist should already be
there, not rebuilt by picking twenty topics in a dialog.

So after a FINAL extraction, every spoken check that matched a checklist
template (inspection_match) is sent to the report worker as a report on
exactly that check's stretches, with that template. It is the same request
the report dialog makes when someone presses Generate -- built by the same
code (lambda_org_api.day_report_generate), as the person who recorded it, so
what they may see and what goes in cannot differ from a report they asked for.

Not generated, by the owner's choice (2026-10-06): a check no template matched
("no wrong form filled in") -- it is offered as a notice instead; and anything
on a live (provisional) pass.

Once per check: the same recording, checklist and start make one report however
often the recording is re-extracted (checklist_reports' unique key).
"""
import json
import logging

from psycopg.rows import dict_row

import inspection_match
import pipeline_trace
import tenancy_status
import units
import walk_body
from repositories import inspection_windows, report_templates, topics as topics_repo, users

logger = logging.getLogger(__name__)


def _title(template_name, check_name, start_at):
    """The checklist's own name: the check and its time are in the report's
    details and header already, and on the title they made a two-line heading."""
    return template_name


def already_made(conn, session, template_id, start_at, end_at=None, segments=None,
                 walk_key=None):
    """True when this check's report exists FOR THE SAME WINDOW.

    The same window, not just the same start: a final extraction often runs
    before the recording's last transcripts land and is re-run over the fuller
    set (generation 1, 2...). On TEST (2026-10-06, Ben_Lin_test2 17:12) the
    first final saw 17:12:52-17:14:13 of a five-minute pre-pour check; keyed on
    the start alone, the re-run's full window was "already made" and the
    report stayed the first 80 seconds. A different end or stretches makes it
    again, replacing the row (_record).

    A multi-unit walk (`walk_key`) is one row whatever recording wrote it, so it
    is found by its key alone and compared on the walk's FULL segments: a second
    recording of the walk adds segments, and that makes the report again."""
    if walk_key:
        row = conn.execute("SELECT segments FROM checklist_reports WHERE walk_key = %s",
                           (walk_key,)).fetchone()
        if row is None:
            return False
        old = row[0] if not isinstance(row[0], str) else json.loads(row[0])
        return (old or None) == (segments or None)
    row = conn.execute(
        "SELECT end_at, segments FROM checklist_reports "
        "WHERE session = %s AND template_id = %s AND start_at = %s",
        (session, str(template_id), start_at)).fetchone()
    if row is None:
        return False
    old_segments = row[1] if not isinstance(row[1], str) else json.loads(row[1])
    return row[0] == end_at and (old_segments or None) == (segments or None)


def _record(conn, company_id, folder, date, session, w, tpl_name, request_id, result_key):
    if w.get("walk_key"):
        return _record_walk(conn, company_id, folder, date, session, w, tpl_name, request_id,
                            result_key)
    conn.execute(
        "INSERT INTO checklist_reports (company_id, user_folder, report_date, session, "
        "template_id, template_name, check_name, start_at, end_at, segments, request_id, "
        "result_key) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s) "
        "ON CONFLICT (session, template_id, start_at) DO UPDATE SET "
        "end_at = EXCLUDED.end_at, segments = EXCLUDED.segments, "
        "request_id = EXCLUDED.request_id, result_key = EXCLUDED.result_key, "
        "check_name = EXCLUDED.check_name, walk_key = NULL, created_at = now()",
        (str(company_id), folder, date, session, str(w["template_id"]), tpl_name, w["name"],
         w["start_at"], w.get("end_at"), json.dumps(w.get("segments") or None),
         request_id, result_key))


def _record_walk(conn, company_id, folder, date, session, w, tpl_name, request_id, result_key):
    """One row per walk (uq_checklist_reports_walk), recorded under the recording
    that queued it and the walk's first start.

    The table's older key, checklist_reports_once (session, template_id,
    start_at), still applies to a walk row, and ON CONFLICT can name only one
    of the two. It fires when a window was an ordinary check's report before it
    became part of a walk: the same recording re-extracted, or a later recording
    a walk claimed afterwards (R13). That report IS this walk's part, so it is
    removed first. In a savepoint: a failure here costs this report's row, never
    the extraction that called it."""
    with conn.transaction():
        for part_session, part_start in w.get("parts") or []:
            conn.execute(
                "DELETE FROM checklist_reports WHERE walk_key IS NULL AND session = %s "
                "AND template_id = %s AND start_at = %s",
                (part_session, str(w["template_id"]), part_start))
        conn.execute(
            "DELETE FROM checklist_reports WHERE walk_key IS NULL AND session = %s "
            "AND template_id = %s AND start_at = %s",
            (session, str(w["template_id"]), w["start_at"]))
        conn.execute(
            "INSERT INTO checklist_reports (company_id, user_folder, report_date, session, "
            "template_id, template_name, check_name, start_at, end_at, segments, request_id, "
            "result_key, walk_key) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, "
            "%s, %s) "
            "ON CONFLICT (walk_key) WHERE walk_key IS NOT NULL DO UPDATE SET "
            "session = EXCLUDED.session, start_at = EXCLUDED.start_at, "
            "end_at = EXCLUDED.end_at, segments = EXCLUDED.segments, "
            "request_id = EXCLUDED.request_id, result_key = EXCLUDED.result_key, "
            "template_name = EXCLUDED.template_name, check_name = EXCLUDED.check_name, "
            "created_at = now()",
            (str(company_id), folder, date, session, str(w["template_id"]), tpl_name, w["name"],
             w["start_at"], w.get("end_at"), json.dumps(w.get("segments") or None),
             request_id, result_key, w["walk_key"]))


def _clock(hms):
    parts = [int(x) for x in str(hms).split(":")]
    return parts[0] * 3600 + parts[1] * 60 + (parts[2] if len(parts) > 2 else 0)


def check_topics(check, others, session_topics):
    """The topic ids of this check's report: the recording's topics that touch
    one of its stretches, minus those that are another check's.

    CHOSEN HERE, by the step that knows every check in the recording, because
    topic times are minute stamps the model writes and they are not where the
    words were: on TEST (2026-10-06) the pour-booking talk at 17:12:52-17:13:30
    was stamped "17:12 - 17:12" and the stair-core steel check at 17:15:27 was
    stamped "17:14". Judged by its minutes the report dropped the first and
    took the second. A topic touching a stretch (to the minute) is in; one whose
    title names another check's kind ("Stair Core Steel Check" -- steel) and not
    this one's is out."""
    stretches = check.get("segments") or [{"from": check["start_at"], "to": check.get("end_at")}]
    mine = set(inspection_match.words(check.get("kind")) or inspection_match.words(check.get("name")))
    theirs = set()
    for o in others:
        theirs |= set(inspection_match.words(o.get("kind")) or inspection_match.words(o.get("name")))
    theirs -= mine
    out = []
    for t in session_topics:
        tr = t.get("time_range") or ""
        clocks = [c.strip() for c in tr.replace("—", "-").replace("–", "-").split("-") if c.strip()]
        try:
            start = _clock(clocks[0]) // 60 * 60
            end = _clock(clocks[-1]) // 60 * 60 + 59
        except (ValueError, IndexError):
            continue
        touches = any(start < _clock(s["to"] or "23:59:59") and end >= _clock(s["from"])
                      for s in stretches)
        if not touches:
            continue
        named = set(inspection_match.words(t.get("title")))
        if named & theirs and not named & mine:
            continue
        out.append(str(t["id"]))
    return out


def request_body(w, template, topic_ids=None):
    """The report dialog's request, for one check."""
    body = {"templateId": str(template["id"]), "templateVersion": template["current_version"],
            "title": _title(template["name"], w["name"], w["start_at"]),
            "deliver": "download", "from": w["start_at"], "to": w.get("end_at") or "23:59"}
    if len(w.get("segments") or []) > 1:
        body["segments"] = w["segments"]
    if topic_ids:
        body["topicRowIds"] = topic_ids
    return body


_UNREAD = object()


def _recorder(conn, company_id, folder):
    """(caller, blocked): the recorder as the report's caller, and the
    offboarding gate's reason when they may not make one (None when they may)."""
    # Global: `company_id` is the SITE's company; the recorder's row stays in their home company.
    user = (users.get_by_folder_name(conn, company_id, folder)
            or users.get_by_folder_name_global(conn, folder) or {})
    caller = users.get_user_by_sub(conn, user["cognito_sub"]) if user.get("cognito_sub") else None
    # Company offboarding gate (spec 2026-11-01 §5.1): the recorder is the caller of the
    # report. Blocked -> each check is skipped, never raised; the extraction that called
    # this is untouched (R6). Flag off: logs and generates as today.
    blocked = (tenancy_status.caller_blocked(conn, caller, where="checklist-auto-report")
               if caller is not None else None)
    return caller, blocked


def _gate(conn, company_id, folder, caller, blocked, state, name):
    """Why this check may not be queued, or None. `state` caches the site
    company's suppression, read once (Company offboarding, Task 9, spec §5.5:
    no report for a check recorded on an inactive company's SITE, whoever
    recorded it). The read happens inside the caller's per-check try: a failed
    read costs that check ("error"), never the extraction. Flag off: logged
    COMPANY_GATE_WOULD_SUPPRESS, generated."""
    if caller is None:
        pipeline_trace.event("checklist_report", "no_login", detail={"check": name})
        return "no login for %s" % folder
    if blocked:
        pipeline_trace.event("checklist_report", blocked, detail={"check": name})
        return blocked
    if state.get("suppressed", _UNREAD) is _UNREAD:
        state["suppressed"] = tenancy_status.suppress_reason(
            conn, company_id=company_id, where="checklist-auto-report")
    if state["suppressed"]:
        pipeline_trace.event("checklist_report", state["suppressed"], detail={"check": name})
        return state["suppressed"]
    return None


def _send(conn, generate, caller, company_id, folder, date, session, w, tpl, body):
    """Queue one report and record it. Returns the request id, or the reason
    the report dialog's code refused it."""
    walk = {"walk": w["walk_key"]} if w.get("walk_key") else {}
    res = generate(conn, caller, date, {"queryStringParameters": {"user": folder},
                                        "body": json.dumps(body)})
    payload = json.loads(res.get("body") or "{}")
    if res.get("statusCode") not in (200, 201, 202) or not payload.get("requestId"):
        pipeline_trace.event("checklist_report", "refused", detail={
            "check": w["name"], "template": tpl["name"], "error": payload.get("error"), **walk})
        return "refused: %s" % payload.get("error")
    rid = payload["requestId"]
    result_key = (payload.get("resultKey")
                  or "session_report_results/%s/%s/day/%s.json" % (folder, date, rid))
    _record(conn, company_id, folder, date, session, w, tpl["name"], rid, result_key)
    pipeline_trace.event("checklist_report", "queued", detail={
        "check": w["name"], "template": tpl["name"], "request": rid,
        "from": body["from"], "to": body["to"], "stretches": len(w.get("segments") or []),
        **walk})
    return rid


def _walk_units(value):
    """The opening's units as the request carries them: a list of names and an
    int count or None -- `{"names": [], "count": None}` when the walk was opened
    by entering units, not by naming them (its row stores NULL)."""
    value = value if isinstance(value, dict) else {}
    names = [n for n in value.get("names") or [] if isinstance(n, str)]
    count = value.get("count")
    return {"names": names,
            "count": count if isinstance(count, int) and not isinstance(count, bool) else None}


def walk_window(company_id, folder, date, key, day):
    """The walk `key` as ONE window, from every window of it on the day
    (`day` = inspection_windows.for_day, any recording), or None.

    segments: every finished stretch of every window, in time order -- a
    recording still running (a stretch with no end) is left out until its own
    final extraction makes the report again; entered: every mark; units: the
    opening's (the window whose start gives the key, else the earliest).
    `parts` are the (session, start_at) of the windows it was made from."""
    rows = [r for r in day if r.get("walk_key") == key and r.get("template_id")]
    segs, entered, parts = [], [], []
    for r in rows:
        done = [s for s in r.get("segments") or [] if s.get("from") and s.get("to")]
        if not done:
            continue
        segs += done
        entered += [m for m in r.get("entered") or [] if isinstance(m, dict)]
        parts.append((r.get("session"), r["start_at"]))
    if not segs:
        return None
    segs.sort(key=lambda s: str(s["from"]))
    opening = next((r for r in rows if units.walk_key(
        company_id, folder, date, r["template_id"], r["start_at"]) == key), rows[0])
    return {"name": opening["name"], "kind": opening.get("kind"),
            "template_id": opening["template_id"], "session": opening.get("session"),
            "start_at": segs[0]["from"], "end_at": segs[-1]["to"], "segments": segs,
            "entered": entered, "units": _walk_units(opening.get("units")),
            "walk_key": key, "parts": parts}


def _queue_walk(conn, generate, caller, company_id, folder, date, session, key, day,
                day_topics, force=False):
    """(walk name, request id or reason) for one walk -- the per-walk body that
    auto_generate and requeue_walk both send. `session` None records it under
    the opening's recording."""
    w = walk_window(company_id, folder, date, key, day)
    if w is None:
        return None, "no such walk"
    session = session or w["session"]
    if not force and already_made(conn, session, w["template_id"], w["start_at"],
                                  w.get("end_at"), w["segments"], walk_key=key):
        return w["name"], "already made"
    tpl = report_templates.get_any(conn, w["template_id"])
    if not tpl or not tpl.get("current_version"):
        return w["name"], "template gone"
    sessions = {s for s, _ in w["parts"]}
    topics = [t for t in day_topics
              if any(s and s in (t.get("source_s3_key") or "") for s in sessions)]
    others = [o for o in day if o.get("walk_key") != key and o.get("session") in sessions]
    # A mark whose time is not HH:MM:SS (a model's "9:01:00") would open a
    # stretch at a time no clock sorts right: it is dropped, and said so.
    marks = [m for m in w["entered"] if walk_body.is_hms(units._hhmmss(m))]
    if len(marks) < len(w["entered"]):
        logger.warning("walk %s: dropped %d mark(s) with no HH:MM:SS time: %s", key,
                       len(w["entered"]) - len(marks),
                       [m for m in w["entered"] if m not in marks])
    w["entered"] = marks
    body = request_body(w, tpl, check_topics(w, others, topics) or None)
    body["walk"] = {"key": key, "units": w["units"], "entered": w["entered"],
                    "stretches": units.unit_stretches(w)}
    # Never send what org-api would refuse (walk_body's rules, R23/R24).
    _, err = walk_body.check(body["walk"])
    if err is None and len(w["segments"]) > walk_body.MAX_SEGMENTS:
        err = "walk has more than %d stretches" % walk_body.MAX_SEGMENTS
    if err:
        logger.warning("walk not sent: %s (%s)", err, key)
        pipeline_trace.event("checklist_report", "walk_not_sent",
                             detail={"check": w["name"], "walk": key, "error": err})
        return w["name"], "walk not sent: %s" % err
    return w["name"], _send(conn, generate, caller, company_id, folder, date, session, w,
                            tpl, body)


def auto_generate(conn, company_id, folder, date, session, windows, generate=None):
    """Send each matched check of a final extraction to the report worker,
    once. `windows` are the rows just stored (start_at/end_at to the second,
    template_id). Returns [(check name, request id or reason)]. Never raises:
    a report that could not be queued must not cost the extraction.

    A multi-unit walk (a window with a walk_key, spec 2026-11-03) is ONE report
    across all its recordings of the day: each walk among these windows is
    queued once, over every window of it (inspection_windows.for_day). A
    window with no walk_key takes the path it always did."""
    if generate is None:
        import lambda_org_api
        generate = lambda_org_api.day_report_generate
    out = []
    matched = [w for w in windows if w.get("template_id")]
    if not matched:
        return out
    plain = [w for w in matched if not w.get("walk_key")]
    walks = {}                                       # walk_key -> this recording's first window
    for w in matched:
        if w.get("walk_key"):
            walks.setdefault(w["walk_key"], w)
    day_topics = topics_repo.list_day_topics_for_binding(conn, folder, date)
    session_topics = [t for t in day_topics if session in (t.get("source_s3_key") or "")]
    caller, blocked = _recorder(conn, company_id, folder)
    state = {}
    for w in plain:
        try:
            reason = _gate(conn, company_id, folder, caller, blocked, state, w["name"])
            if reason:
                out.append((w["name"], reason))
                continue
            if already_made(conn, session, w["template_id"], w["start_at"], w.get("end_at"),
                            w.get("segments")):
                out.append((w["name"], "already made"))
                continue
            tpl = report_templates.get_any(conn, w["template_id"])
            if not tpl or not tpl.get("current_version"):
                out.append((w["name"], "template gone"))
                continue
            others = [o for o in windows if o is not w]
            body = request_body(w, tpl, check_topics(w, others, session_topics) or None)
            out.append((w["name"], _send(conn, generate, caller, company_id, folder, date,
                                         session, w, tpl, body)))
        except Exception:
            logger.exception("checklist report for %r not queued", w.get("name"))
            out.append((w.get("name"), "error"))
    day = None
    for key, w in walks.items():
        try:
            reason = _gate(conn, company_id, folder, caller, blocked, state, w["name"])
            if reason:
                out.append((w["name"], reason))
                continue
            if day is None:
                day = inspection_windows.for_day(conn, company_id, folder, date)
            name, got = _queue_walk(conn, generate, caller, company_id, folder, date, session,
                                    key, day, day_topics)
            out.append((name or w["name"], got))
        except Exception:
            logger.exception("walk report for %r not queued", w.get("name"))
            out.append((w.get("name"), "error"))
    return out


def requeue_walk(conn, company_id, folder, date, walk_key, generate=None):
    """Queue walk `walk_key`'s report again, made or not -- after an on-site
    answer is written (Tasks 11 and 12), the report must take it. Returns the
    request id, or the reason it was not queued. Never raises."""
    if generate is None:
        import lambda_org_api
        generate = lambda_org_api.day_report_generate
    try:
        caller, blocked = _recorder(conn, company_id, folder)
        reason = _gate(conn, company_id, folder, caller, blocked, {}, walk_key)
        if reason:
            return reason
        day = inspection_windows.for_day(conn, company_id, folder, date)
        day_topics = topics_repo.list_day_topics_for_binding(conn, folder, date)
        return _queue_walk(conn, generate, caller, company_id, folder, date, None, walk_key,
                           day, day_topics, force=True)[1]
    except Exception:
        logger.exception("walk report %s not re-queued", walk_key)
        return "error"


def for_day(conn, company_id, folder, date):
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT id, session, template_id, template_name, check_name, start_at, end_at, "
        "segments, request_id, result_key, created_at FROM checklist_reports "
        "WHERE (%s::uuid IS NULL OR company_id = %s::uuid) AND user_folder = %s "
        "AND report_date = %s ORDER BY start_at",
        (str(company_id) if company_id else None, str(company_id) if company_id else None,
         folder, date)).fetchall()


def recent_for_folder(conn, company_id, folder, since_date):
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT id, report_date, session, template_id, template_name, check_name, start_at, "
        "end_at, request_id, result_key, created_at FROM checklist_reports "
        "WHERE company_id = %s AND user_folder = %s AND report_date >= %s "
        "ORDER BY created_at DESC LIMIT 50",
        (str(company_id), folder, since_date)).fetchall()
