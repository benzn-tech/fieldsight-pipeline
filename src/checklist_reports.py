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
from repositories import report_templates, topics as topics_repo, users

logger = logging.getLogger(__name__)


def _title(template_name, check_name, start_at):
    return "%s -- %s (%s)" % (template_name, check_name, (start_at or "")[:5])


def already_made(conn, session, template_id, start_at, end_at=None, segments=None):
    """True when this check's report exists FOR THE SAME WINDOW.

    The same window, not just the same start: a final extraction often runs
    before the recording's last transcripts land and is re-run over the fuller
    set (generation 1, 2...). On TEST (2026-10-06, Ben_Lin_test2 17:12) the
    first final saw 17:12:52-17:14:13 of a five-minute pre-pour check; keyed on
    the start alone, the re-run's full window was "already made" and the
    report stayed the first 80 seconds. A different end or stretches makes it
    again, replacing the row (_record)."""
    row = conn.execute(
        "SELECT end_at, segments FROM checklist_reports "
        "WHERE session = %s AND template_id = %s AND start_at = %s",
        (session, str(template_id), start_at)).fetchone()
    if row is None:
        return False
    old_segments = row[1] if not isinstance(row[1], str) else json.loads(row[1])
    return row[0] == end_at and (old_segments or None) == (segments or None)


def _record(conn, company_id, folder, date, session, w, tpl_name, request_id, result_key):
    conn.execute(
        "INSERT INTO checklist_reports (company_id, user_folder, report_date, session, "
        "template_id, template_name, check_name, start_at, end_at, segments, request_id, "
        "result_key) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s) "
        "ON CONFLICT (session, template_id, start_at) DO UPDATE SET "
        "end_at = EXCLUDED.end_at, segments = EXCLUDED.segments, "
        "request_id = EXCLUDED.request_id, result_key = EXCLUDED.result_key, "
        "check_name = EXCLUDED.check_name, created_at = now()",
        (str(company_id), folder, date, session, str(w["template_id"]), tpl_name, w["name"],
         w["start_at"], w.get("end_at"), json.dumps(w.get("segments") or None),
         request_id, result_key))


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


def auto_generate(conn, company_id, folder, date, session, windows, generate=None):
    """Send each matched check of a final extraction to the report worker,
    once. `windows` are the rows just stored (start_at/end_at to the second,
    template_id). Returns [(check name, request id or reason)]. Never raises:
    a report that could not be queued must not cost the extraction."""
    if generate is None:
        import lambda_org_api
        generate = lambda_org_api.day_report_generate
    out = []
    matched = [w for w in windows if w.get("template_id")]
    if not matched:
        return out
    session_topics = [t for t in topics_repo.list_day_topics_for_binding(conn, folder, date)
                      if session in (t.get("source_s3_key") or "")]
    user = users.get_by_folder_name(conn, company_id, folder) or {}
    caller = users.get_user_by_sub(conn, user["cognito_sub"]) if user.get("cognito_sub") else None
    for w in matched:
        try:
            if caller is None:
                out.append((w["name"], "no login for %s" % folder))
                pipeline_trace.event("checklist_report", "no_login", detail={"check": w["name"]})
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
            res = generate(conn, caller, date, {"queryStringParameters": {"user": folder},
                                                "body": json.dumps(body)})
            payload = json.loads(res.get("body") or "{}")
            if res.get("statusCode") not in (200, 201, 202) or not payload.get("requestId"):
                out.append((w["name"], "refused: %s" % payload.get("error")))
                pipeline_trace.event("checklist_report", "refused", detail={
                    "check": w["name"], "template": tpl["name"], "error": payload.get("error")})
                continue
            rid = payload["requestId"]
            result_key = (payload.get("resultKey")
                          or "session_report_results/%s/%s/day/%s.json" % (folder, date, rid))
            _record(conn, company_id, folder, date, session, w, tpl["name"], rid, result_key)
            out.append((w["name"], rid))
            pipeline_trace.event("checklist_report", "queued", detail={
                "check": w["name"], "template": tpl["name"], "request": rid,
                "from": body["from"], "to": body["to"], "stretches": len(w.get("segments") or [])})
        except Exception:
            logger.exception("checklist report for %r not queued", w.get("name"))
            out.append((w.get("name"), "error"))
    return out


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
