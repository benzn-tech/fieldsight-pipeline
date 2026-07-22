"""Audit history for editable content correction (migration 0019, spec §5.2).
append on every successful edit; list backs GET /content/{table}/{id}/history.
Company-guarded reads (the endpoint already resolved the row's company)."""
from psycopg.rows import dict_row

_COLS = ("id, company_id, table_name, row_id, field, before_text, after_text, "
         "actor_user_id, actor_role, created_at")
_COLS_QUALIFIED = ", ".join("ce." + c for c in _COLS.split(", "))


def append_content_edit(conn, company_id, table_name, row_id, field,
                        before_text, after_text, actor_user_id, actor_role):
    return conn.cursor(row_factory=dict_row).execute(
        f"INSERT INTO content_edits (company_id, table_name, row_id, field, "
        f"before_text, after_text, actor_user_id, actor_role) "
        f"VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING {_COLS}",
        (company_id, table_name, row_id, field, before_text, after_text,
         actor_user_id, actor_role),
    ).fetchone()


def list_content_edits(conn, company_id, table_name, row_id):
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS_QUALIFIED}, "
        f"       NULLIF(TRIM(CONCAT_WS(' ', u.first_name, u.last_name)), '') AS actor_name "
        f"FROM content_edits ce "
        f"LEFT JOIN users u ON u.id = ce.actor_user_id "
        f"WHERE ce.company_id=%s AND ce.table_name=%s AND ce.row_id=%s "
        f"ORDER BY ce.created_at DESC",
        (company_id, table_name, row_id),
    ).fetchall()
