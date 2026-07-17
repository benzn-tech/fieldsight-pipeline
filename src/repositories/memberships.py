from psycopg.rows import dict_row
from repositories.acl import resolve_scope  # re-export

__all__ = ["resolve_scope", "add_membership", "accessible_site_ids", "ensure_membership", "list_company_memberships",
          "members_for_site"]


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
            "WHERE u.id = %s AND s.archived_at IS NULL",
            (user_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT site_id FROM memberships WHERE user_id=%s AND archived_at IS NULL", (user_id,)
        ).fetchall()
    return [r[0] for r in rows]


def ensure_membership(conn, user_id, site_id, role) -> dict:
    """Idempotent add: re-running updates the role instead of raising on
    the (user_id, site_id) UNIQUE constraint. Used by seed + member create.
    Re-adding an archived membership revives it (archived_at reset). NOTE: a seed re-run therefore revives archived memberships — folded into the documented seed re-run quirk."""
    return conn.cursor(row_factory=dict_row).execute(
        "INSERT INTO memberships (user_id, site_id, role) VALUES (%s, %s, %s) "
        "ON CONFLICT (user_id, site_id) DO UPDATE SET role=EXCLUDED.role, archived_at=NULL "
        "RETURNING id, user_id, site_id, role, created_at",
        (user_id, site_id, role),
    ).fetchone()


def list_company_memberships(conn, company_id) -> list[dict]:
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT m.user_id, u.cognito_sub, m.site_id, m.role "
        "FROM memberships m "
        "JOIN users u ON u.id = m.user_id "
        "JOIN sites s ON s.id = m.site_id "
        "WHERE s.company_id = %s AND u.company_id = s.company_id AND m.archived_at IS NULL "
        "ORDER BY u.created_at, m.created_at",
        (company_id,),
    ).fetchall()


def members_for_site(conn, company_id, site_id) -> list[dict]:
    """Members of ONE site (memberships-backed), for org-api GET
    /api/org/sites/{id}/members -- the Aurora replacement for legacy
    /site-users, which read config/user_mapping.json and so returned []
    for Aurora-only sites (visibility spec §1.1 'USERS ON SITE empty').
    Company-pinned on BOTH sides of the join (multi-tenant invariant) and
    excludes archived members/memberships. Returns each user's display
    columns plus the per-site membership role (site_role)."""
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT u.id, u.cognito_sub, u.first_name, u.last_name, u.folder_name, "
        "u.avatar_s3_key, u.global_role, m.role AS site_role "
        "FROM memberships m "
        "JOIN users u ON u.id = m.user_id "
        "JOIN sites s ON s.id = m.site_id "
        "WHERE m.site_id = %s::uuid AND s.company_id = %s AND u.company_id = %s "
        "AND m.archived_at IS NULL AND u.archived_at IS NULL "
        "ORDER BY u.first_name, u.last_name",
        (site_id, company_id, company_id),
    ).fetchall()
