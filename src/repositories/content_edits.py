"""Audit history for editable content correction (migration 0019, spec §5.2).
append on every successful edit; list backs GET /content/{table}/{id}/history.
Company-guarded reads (the endpoint already resolved the row's company)."""
from psycopg.rows import dict_row

_COLS = ("id, company_id, table_name, row_id, field, before_text, after_text, "
         "actor_user_id, actor_role, created_at")


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
        f"SELECT {_COLS} FROM content_edits "
        f"WHERE company_id=%s AND table_name=%s AND row_id=%s "
        f"ORDER BY created_at DESC",
        (company_id, table_name, row_id),
    ).fetchall()
