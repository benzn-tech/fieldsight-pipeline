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
                "template_id, match_score, segments, units, entered, walk_key) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, "
                "%s::jsonb, %s::jsonb, %s)",
                (str(company_id), user_folder, date, session, w["name"], w.get("kind") or "",
                 w["start_at"], w.get("end_at"), w.get("end_source") or "recording_stop",
                 w.get("start_quote") or "", w.get("end_quote"),
                 str(w["template_id"]) if w.get("template_id") else None,
                 w.get("match_score"),
                 json.dumps(w["segments"]) if w.get("segments") else None,
                 json.dumps(w["units"]) if _has_units(w.get("units")) else None,
                 json.dumps(w["entered"]) if w.get("entered") else None,
                 w.get("walk_key") or None))
    return len(windows or [])


_LATER_OPENING_LESS = (
    "company_id = %s AND user_folder = %s AND report_date = %s "
    "AND template_id = %s AND start_at > %s AND session <> %s "
    "AND (units IS NULL OR ("
    "  (CASE WHEN jsonb_typeof(units->'names') = 'array' "
    "        THEN jsonb_array_length(units->'names') ELSE 0 END) < 2 "
    "  AND (CASE WHEN jsonb_typeof(units->'count') = 'number' "
    "        THEN (units->>'count')::numeric ELSE 0 END) < 2))")


def backfill_walk(conn, company_id, user_folder, date, template_id, after_start_at,
                  walk_key, exclude_session, self_key=None):
    """A walk extracted AFTER a later recording of it (R13): give its key to the
    day's later windows on this checklist from other recordings that have no
    opening of their own (fewer than two names, no count of two or more) and
    either no walk yet, or -- with `self_key` (start_at -> the key a window
    starting then would give itself) -- a walk they keyed themselves by
    entering two or more units (R47). Such a self-keyed walk is claimed whole:
    every later window carrying its key, and its on-site answers with it
    (R52). Returns how many window rows it claimed."""
    where = (str(company_id), user_folder, date, str(template_id), after_start_at,
             exclude_session)
    cur = conn.execute(
        "UPDATE inspection_windows SET walk_key = %s WHERE " + _LATER_OPENING_LESS
        + " AND walk_key IS NULL", (walk_key,) + where)
    n = cur.rowcount
    if self_key is None:
        return n
    keyed = conn.execute(
        "SELECT start_at, walk_key FROM inspection_windows WHERE " + _LATER_OPENING_LESS
        + " AND walk_key IS NOT NULL AND walk_key <> %s", where + (walk_key,)).fetchall()
    selves = sorted({k for start, k in keyed if k == self_key(start)})
    if selves:
        cur = conn.execute(
            "UPDATE inspection_windows SET walk_key = %s "
            "WHERE company_id = %s AND user_folder = %s AND report_date = %s "
            "AND template_id = %s AND start_at > %s AND session <> %s "
            "AND walk_key = ANY(%s)", (walk_key,) + where + (selves,))
        n += cur.rowcount
        # R52: the on-site answers given under the self-made key follow its
        # windows, or the merged report drops them. In the same savepoint, in
        # this company's folder and day. walk_final_checks are NOT re-keyed:
        # GET /open serves the latest one by its own key.
        conn.execute(
            "UPDATE walk_answers SET walk_key = %s "
            "WHERE company_id = %s AND user_folder = %s AND report_date = %s "
            "AND walk_key = ANY(%s)",
            (walk_key, str(company_id), user_folder, date, selves))
    return n


def _has_units(units):
    """An opening that named no units and no count is stored as NULL, so a
    single-unit check's row looks exactly as it did before 0096."""
    return bool(units) and bool(units.get("names") or units.get("count") is not None)


def _parse_json(r, *keys):
    for k in keys:
        if isinstance(r.get(k), str):
            r[k] = json.loads(r[k])


def for_day(conn, company_id, user_folder, date):
    """The day's checks, earliest first, with the matched template's name.

    company_id=None means no company restriction (a cross-company platform
    admin), the convention the rest of this package uses."""
    rows = conn.cursor(row_factory=dict_row).execute(
        "SELECT w.id, w.session, w.name, w.kind, w.start_at, w.end_at, w.end_source, "
        "       w.start_quote, w.end_quote, w.template_id, w.match_score, w.segments, "
        "       w.units, w.entered, w.walk_key, t.name AS template_name "
        "FROM inspection_windows w LEFT JOIN report_templates t ON t.id = w.template_id "
        "WHERE (%s::uuid IS NULL OR w.company_id = %s::uuid) "
        "AND w.user_folder = %s AND w.report_date = %s "
        "ORDER BY w.start_at, w.name",
        (str(company_id) if company_id else None, str(company_id) if company_id else None,
         user_folder, date),
    ).fetchall()
    for r in rows:
        _parse_json(r, "segments", "units", "entered")
        if not r.get("segments"):
            r["segments"] = [{"from": r["start_at"], "to": r["end_at"]}]   # one stretch
    return rows


def for_session(conn, company_ids, user_folder, session):
    """One recording's checks so far (check done, spec 2026-10-20). Never by
    session alone: the same filter as the report dialog (company AND folder),
    plus the session. `company_ids` empty reads nothing -- here an empty list is
    "no company", not "no filter"."""
    ids = [str(c) for c in company_ids or [] if c]
    if not ids:
        return []
    rows = conn.cursor(row_factory=dict_row).execute(
        "SELECT name, kind, start_at, end_at, end_source, template_id, report_date, "
        "       units, entered, walk_key "
        "FROM inspection_windows "
        "WHERE company_id = ANY(%s::uuid[]) AND user_folder = %s AND session = %s "
        "ORDER BY start_at, name",
        (ids, user_folder, session),
    ).fetchall()
    for r in rows:
        _parse_json(r, "units", "entered")
    return rows


def walk_companies(conn, user_folder, date, walk_key):
    """The companies of walk `walk_key`'s windows in this folder's day (R22) --
    the site's company, under which its on-site answers are written. Empty
    when the walk is not this folder's on this day."""
    rows = conn.execute(
        "SELECT DISTINCT company_id FROM inspection_windows "
        "WHERE walk_key = %s AND user_folder = %s AND report_date = %s",
        (walk_key, user_folder, date),
    ).fetchall()
    return sorted(str(r[0]) for r in rows)


def walk_rows(conn, user_folder, walk_key):
    """Every window of walk `walk_key` in this folder, earliest first, with its
    company -- what a confirmation answer is checked against (Task 11). Never
    by walk_key alone: the folder is the caller's own, so another person's walk
    reads as none. The key already names the day (units.walk_key)."""
    rows = conn.cursor(row_factory=dict_row).execute(
        "SELECT company_id, report_date, session, template_id, start_at, units, entered "
        "FROM inspection_windows WHERE walk_key = %s AND user_folder = %s "
        "ORDER BY start_at, session",
        (walk_key, user_folder),
    ).fetchall()
    for r in rows:
        _parse_json(r, "units", "entered")
    return rows


def open_walks(conn, company_id, user_folder, date, template_id):
    """The day's multi-unit walks on this checklist so far, oldest first: every
    window (from any recording of the day) that carries a walk_key. The caller
    leaves out its own recording; this does not filter by session."""
    rows = conn.cursor(row_factory=dict_row).execute(
        "SELECT walk_key, start_at, units, session FROM inspection_windows "
        "WHERE company_id = %s AND user_folder = %s AND report_date = %s "
        "AND template_id = %s AND walk_key IS NOT NULL "
        "ORDER BY start_at, session",
        (str(company_id), user_folder, date, str(template_id)),
    ).fetchall()
    for r in rows:
        _parse_json(r, "units")
    return rows
