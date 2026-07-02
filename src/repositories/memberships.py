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
        # Company-scoped "all": admin/gm see every site of THEIR company only.
        # A user with no company sees nothing (deny-by-default).
        rows = conn.execute(
            "SELECT s.id FROM sites s "
            "JOIN users u ON u.company_id = s.company_id "
            "WHERE u.id = %s",
            (user_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT site_id FROM memberships WHERE user_id=%s", (user_id,)
        ).fetchall()
    return [r[0] for r in rows]
