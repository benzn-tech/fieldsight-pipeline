"""The final check's open questions for a multi-unit walk (migration 0096, R41).

Written by the check-done hop each time the final check of a walk leaves
questions open; read by GET /open, which serves the newest of these and the
latest finished walk report. Never read by walk_key alone.
"""
import json

from psycopg.rows import dict_row


def add(conn, row):
    """One final check's questions. Returns its id."""
    new_id = conn.execute(
        "INSERT INTO walk_final_checks (company_id, user_folder, report_date, walk_key, questions) "
        "VALUES (%s, %s, %s, %s, %s::jsonb) RETURNING id",
        (str(row["company_id"]), row["user_folder"], row["report_date"], row["walk_key"],
         json.dumps(row["questions"])),
    ).fetchone()[0]
    return str(new_id)


def latest(conn, company_ids, walk_key):
    """The walk's newest final check within `company_ids`, or None. An empty
    list reads nothing ("no company", not "no filter")."""
    ids = [str(c) for c in company_ids or [] if c]
    if not ids:
        return None
    row = conn.cursor(row_factory=dict_row).execute(
        "SELECT id, company_id, user_folder, report_date, walk_key, questions, created_at "
        "FROM walk_final_checks WHERE company_id = ANY(%s::uuid[]) AND walk_key = %s "
        "ORDER BY created_at DESC, id DESC LIMIT 1",
        (ids, walk_key),
    ).fetchone()
    if row:
        row["id"] = str(row["id"])
    return row
