"""The checks spoken on a day, and the checklist template each one matched.

See migration 0082 and inspection_match. Written by item-writer from each
extraction (one recording's rows replaced at a time, like the location
markers); read by org-api for the report dialog.
"""
import json

from psycopg.rows import dict_row


def checklist_templates(conn, company_id, user_id):
    """The company's org templates and this person's own, with their current
    body -- the candidates a spoken check is matched against."""
    rows = conn.cursor(row_factory=dict_row).execute(
        "SELECT t.id, t.name, t.scope, v.body FROM report_templates t "
        "JOIN report_template_versions v "
        "  ON v.template_id = t.id AND v.version = t.current_version "
        "WHERE t.company_id = %s AND t.archived_at IS NULL "
        "AND (t.scope = 'org' OR t.owner_user_id = %s)",
        (str(company_id), str(user_id) if user_id else None),
    ).fetchall()
    for r in rows:
        if isinstance(r.get("body"), str):
            r["body"] = json.loads(r["body"])
    return rows


def replace_for_session(conn, company_id, user_folder, date, session, windows):
    """This recording's checks, replacing whatever it had. In a savepoint: a
    failure here must not abort the extraction's own write."""
    with conn.transaction():
        conn.execute("DELETE FROM inspection_windows WHERE company_id = %s AND session = %s",
                     (str(company_id), session))
        for w in windows or []:
            conn.execute(
                "INSERT INTO inspection_windows (company_id, user_folder, report_date, session, "
                "name, kind, start_at, end_at, end_source, start_quote, end_quote, "
                "template_id, match_score, segments) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)",
                (str(company_id), user_folder, date, session, w["name"], w.get("kind") or "",
                 w["start_at"], w.get("end_at"), w.get("end_source") or "recording_stop",
                 w.get("start_quote") or "", w.get("end_quote"),
                 str(w["template_id"]) if w.get("template_id") else None,
                 w.get("match_score"),
                 json.dumps(w["segments"]) if w.get("segments") else None))
    return len(windows or [])


def for_day(conn, company_id, user_folder, date):
    """The day's checks, earliest first, with the matched template's name.

    company_id=None means no company restriction (a cross-company platform
    admin), the convention the rest of this package uses."""
    rows = conn.cursor(row_factory=dict_row).execute(
        "SELECT w.id, w.session, w.name, w.kind, w.start_at, w.end_at, w.end_source, "
        "       w.start_quote, w.end_quote, w.template_id, w.match_score, w.segments, "
        "       t.name AS template_name "
        "FROM inspection_windows w LEFT JOIN report_templates t ON t.id = w.template_id "
        "WHERE (%s::uuid IS NULL OR w.company_id = %s::uuid) "
        "AND w.user_folder = %s AND w.report_date = %s "
        "ORDER BY w.start_at, w.name",
        (str(company_id) if company_id else None, str(company_id) if company_id else None,
         user_folder, date),
    ).fetchall()
    for r in rows:
        if isinstance(r.get("segments"), str):
            r["segments"] = json.loads(r["segments"])
        if not r.get("segments"):
            r["segments"] = [{"from": r["start_at"], "to": r["end_at"]}]   # one stretch
    return rows
