"""Answers given on site at a multi-unit walk's final check (migration 0096).

Written when he answers on the device or the phone; read by the report worker,
which merges them with what the recording said (an item not checked on site
takes the recording's answer; a disagreement keeps both and flags the item).
"""
from psycopg.rows import dict_row

_COLUMNS = ("company_id", "user_folder", "report_date", "walk_key", "unit", "item_text",
            "answer", "comment", "move_to_unit", "source", "quote", "voice_note_id",
            "answered_at", "answered_by", "asked_kind")


def add(conn, row):
    """One on-site answer (or move). Returns its id."""
    vals = {k: row.get(k) for k in _COLUMNS}
    vals["comment"] = vals["comment"] or ""
    vals["quote"] = vals["quote"] or ""
    # The question it answers (R36): a read-back No's answer is the inspector's choice.
    vals["asked_kind"] = vals["asked_kind"] or "not_checked"
    for k in ("company_id", "voice_note_id", "answered_by"):
        vals[k] = str(vals[k]) if vals[k] else None
    new_id = conn.execute(
        "INSERT INTO walk_answers (%s) VALUES (%s) RETURNING id"
        % (", ".join(_COLUMNS), ", ".join(["%s"] * len(_COLUMNS))),
        tuple(vals[k] for k in _COLUMNS),
    ).fetchone()[0]
    return str(new_id)


def for_walk(conn, company_ids, walk_key):
    """A walk's on-site answers, oldest first. Never by walk_key alone: only
    within `company_ids`, and an empty list reads nothing ("no company", not
    "no filter")."""
    ids = [str(c) for c in company_ids or [] if c]
    if not ids:
        return []
    rows = conn.cursor(row_factory=dict_row).execute(
        "SELECT id, %s, created_at FROM walk_answers "
        "WHERE company_id = ANY(%%s::uuid[]) AND walk_key = %%s "
        "ORDER BY created_at, answered_at, id" % ", ".join(_COLUMNS),
        (ids, walk_key),
    ).fetchall()
    for r in rows:
        r["id"] = str(r["id"])
    return rows
