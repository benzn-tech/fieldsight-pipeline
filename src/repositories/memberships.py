from psycopg.rows import dict_row
from repositories.acl import resolve_scope  # re-export

__all__ = ["resolve_scope", "add_membership", "ensure_membership",
           "accessible_site_ids", "list_company_memberships"]


def add_membership(conn, user_id, site_id, role) -> dict:
    return conn.cursor(row_factory=dict_row).execute(
        "INSERT INTO memberships (user_id, site_id, role) VALUES (%s, %s, %s) "
        "RETURNING id, user_id, site_id, role, created_at",
        (user_id, site_id, role),
    ).fetchone()


def ensure_membership(conn, user_id, site_id, role) -> dict:
    """Idempotent add: re-adding the same (user, site) updates the role
    instead of violating the UNIQUE constraint (org seed re-runs, POST
    /members retries)."""
    return conn.cursor(row_factory=dict_row).execute(
        "INSERT INTO memberships (user_id, site_id, role) VALUES (%s, %s, %s) "
        "ON CONFLICT (user_id, site_id) DO UPDATE SET role=EXCLUDED.role "
        "RETURNING id, user_id, site_id, role, created_at",
        (user_id, site_id, role),
    ).fetchone()


def list_company_memberships(conn, company_id) -> list[dict]:
    """All memberships of a company's users (GET /org/members join source)."""
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT m.id, m.user_id, m.site_id, m.role, u.cognito_sub "
        "FROM memberships m JOIN users u ON u.id = m.user_id "
        "WHERE u.company_id = %s ORDER BY m.created_at",
        (company_id,),
    ).fetchall()


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
