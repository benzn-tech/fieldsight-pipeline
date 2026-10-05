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

import pipeline_trace
from repositories import report_templates, users

logger = logging.getLogger(__name__)


def _title(template_name, check_name, start_at):
    return "%s -- %s (%s)" % (template_name, check_name, (start_at or "")[:5])


def already_made(conn, session, template_id, start_at):
    return conn.execute(
        "SELECT 1 FROM checklist_reports WHERE session = %s AND template_id = %s AND start_at = %s",
        (session, str(template_id), start_at)).fetchone() is not None


def _record(conn, company_id, folder, date, session, w, tpl_name, request_id, result_key):
    conn.execute(
        "INSERT INTO checklist_reports (company_id, user_folder, report_date, session, "
        "template_id, template_name, check_name, start_at, end_at, segments, request_id, "
        "result_key) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s) "
        "ON CONFLICT (session, template_id, start_at) DO NOTHING",
        (str(company_id), folder, date, session, str(w["template_id"]), tpl_name, w["name"],
         w["start_at"], w.get("end_at"), json.dumps(w.get("segments") or None),
         request_id, result_key))


def request_body(w, template):
    """The report dialog's request, for one check."""
    body = {"templateId": str(template["id"]), "templateVersion": template["current_version"],
            "title": _title(template["name"], w["name"], w["start_at"]),
            "deliver": "download", "from": w["start_at"], "to": w.get("end_at") or "23:59"}
    if len(w.get("segments") or []) > 1:
        body["segments"] = w["segments"]
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
    user = users.get_by_folder_name(conn, company_id, folder) or {}
    caller = users.get_user_by_sub(conn, user["cognito_sub"]) if user.get("cognito_sub") else None
    for w in matched:
        try:
            if caller is None:
                out.append((w["name"], "no login for %s" % folder))
                pipeline_trace.event("checklist_report", "no_login", detail={"check": w["name"]})
                continue
            if already_made(conn, session, w["template_id"], w["start_at"]):
                out.append((w["name"], "already made"))
                continue
            tpl = report_templates.get_any(conn, w["template_id"])
            if not tpl or not tpl.get("current_version"):
                out.append((w["name"], "template gone"))
                continue
            body = request_body(w, tpl)
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
