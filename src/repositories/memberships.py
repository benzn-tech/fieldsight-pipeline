from psycopg.rows import dict_row
from repositories.acl import resolve_scope  # re-export

__all__ = ["resolve_scope", "add_membership", "accessible_site_ids"]


def add_membership(conn, user_id, site_id, role) -> dict:
    return conn.cursor(row_factory=dict_row).execute(
        "INSERT INTO memberships (user_id, site_id, role) VALUES (%s, %s, %s) "
        "RETURNING id, user_id, site_id, role, created_at",
        (user_id, site_id, role),
    ).fetchone()


def accessible_site_ids(conn, user_id, global_role) -> list:
    if resolve_scope(global_role) == "ALL":
        rows = conn.execute("SELECT id FROM sites").fetchall()
    else:
        rows = conn.execute(
            "SELECT site_id FROM memberships WHERE user_id=%s", (user_id,)
        ).fetchall()
    return [r[0] for r in rows]
