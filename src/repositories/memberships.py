from psycopg.rows import dict_row
from repositories.acl import resolve_scope  # re-export
import logging

logger = logging.getLogger(__name__)

__all__ = ["resolve_scope", "add_membership", "accessible_site_ids", "ensure_membership", "list_company_memberships",
          "members_for_site", "caller_site_roles", "has_live_external", "external_site_roles", "external_site_companies", "record_audit", "get_membership", "add_external_membership", "worker_user_ids_for_sites",
          "user_ids_for_sites", "archive_membership", "site_usable_by_user"]


# DEPLOY TOLERANCE. The deploy workflows apply migrations AFTER `sam deploy`, so for about a
# minute the new code runs against a schema without memberships.external (0085) and without
# membership_audit (0086). Reads that touch the column fall back to the pre-0085 SQL (no
# external members exist yet, so the answer is identical) and log a WARNING; they never 500.
_LEGACY_SUBS = (
    ("m.external, ", "false AS external, "),
    (" OR m.external)", ")"),
    (" AND NOT m.external", ""),
    ("role, external, created_at", "role, false AS external, created_at"),
)


def _legacy_sql(sql):
    out = sql
    for a, b in _LEGACY_SUBS:
        out = out.replace(a, b)
    return out


def _is_missing_schema(exc):
    import psycopg.errors as pe
    return isinstance(exc, (pe.UndefinedColumn, pe.UndefinedTable))


def _exec(conn, sql, params=()):
    """conn.cursor(dict_row).execute(sql, params), falling back to the pre-0085 SQL when the
    external column does not exist yet (see DEPLOY TOLERANCE)."""
    def go(q):
        return conn.cursor(row_factory=dict_row).execute(q, params)
    tx = getattr(conn, "transaction", None)
    try:
        if tx is None:
            return go(sql)
        with tx():
            return go(sql)
    except Exception as exc:  # noqa: BLE001
        legacy = _legacy_sql(sql)
        if not _is_missing_schema(exc) or legacy == sql:
            raise
        logger.warning("memberships.external missing (migration 0085 not applied yet); "
                       "using the pre-external query")
        return go(legacy)


def _external_or(conn, sql, params, default):
    """Rows of an external-only read, or `default` while 0085 is not applied."""
    tx = getattr(conn, "transaction", None)
    try:
        if tx is None:
            return conn.execute(sql, params).fetchall()
        with tx():
            return conn.execute(sql, params).fetchall()
    except Exception as exc:  # noqa: BLE001
        if not _is_missing_schema(exc):
            raise
        logger.warning("memberships.external missing (migration 0085 not applied yet); "
                       "treating the caller as having no external memberships")
        return default


def add_membership(conn, user_id, site_id, role) -> dict:
    return conn.cursor(row_factory=dict_row).execute(
        "INSERT INTO memberships (user_id, site_id, role) VALUES (%s, %s, %s) "
        "RETURNING id, user_id, site_id, role, created_at",
        (user_id, site_id, role),
    ).fetchone()


def get_membership(conn, user_id, site_id) -> dict | None:
    """The (user, site) membership INCLUDING an archived one, so the caller can
    tell 'already a member' (409) from 'was one, revive it'."""
    return _exec(conn, 
        "SELECT id, user_id, site_id, role, external, created_at, archived_at "
        "FROM memberships WHERE user_id=%s AND site_id=%s",
        (user_id, site_id),
    ).fetchone()


def add_external_membership(conn, user_id, site_id, role) -> dict:
    """Put a person from ANOTHER company on one site (project-owned tenancy P1).
    Upsert on (user_id, site_id): an archived row is revived with the new role
    and marked external. The caller has already refused a live membership, so
    this never silently flips a live employee row to external."""
    return conn.cursor(row_factory=dict_row).execute(
        "INSERT INTO memberships (user_id, site_id, role, external) VALUES (%s, %s, %s, true) "
        "ON CONFLICT (user_id, site_id) DO UPDATE SET role=EXCLUDED.role, external=true, archived_at=NULL "
        "RETURNING id, user_id, site_id, role, external, created_at",
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


def archive_membership(conn, user_id, site_id) -> dict | None:
    """Take ONE person off ONE project, without touching the person or the
    project. Soft: `archived_at` is set, so the row (and the history that
    references it) survives and ensure_membership revives it on re-staffing.

    Returns None when there was no live membership to remove -- the caller
    turns that into a 404 rather than reporting a removal that never happened.
    Guarded on archived_at IS NULL so a second call is a no-op that reports
    itself as one, instead of silently re-stamping the timestamp.

    Until this existed, `memberships.archived_at` was only ever written as
    collateral by sites.archive_site / users.archive_user, so the only way to
    unstaff somebody was to archive the whole site or the whole person."""
    return conn.cursor(row_factory=dict_row).execute(
        "UPDATE memberships SET archived_at=now() "
        "WHERE user_id=%s AND site_id=%s AND archived_at IS NULL "
        "RETURNING id, user_id, site_id, role, created_at, archived_at",
        (user_id, site_id),
    ).fetchone()


def list_company_memberships(conn, company_id) -> list[dict]:
    return _exec(conn, 
        "SELECT m.user_id, u.cognito_sub, m.site_id, m.role, m.external, "
        "hc.name AS home_company_name "
        "FROM memberships m "
        "JOIN users u ON u.id = m.user_id "
        "JOIN sites s ON s.id = m.site_id "
        "LEFT JOIN companies hc ON hc.id = u.company_id "
        "WHERE s.company_id = %s AND (u.company_id = s.company_id OR m.external) AND m.archived_at IS NULL "
        "ORDER BY u.created_at, m.created_at",
        (company_id,),
    ).fetchall()


def count_by_site(conn, site_ids) -> dict:
    """Active-member count per site, for the Sites-page card KPIs. Returns
    {site_id(str): count}; sites with no members are absent (caller defaults
    to 0). Counts everyone holding a live membership, external members from
    other companies included (an UNflagged cross-company row is bad data and
    still dropped) (project-owned tenancy: the site's roster, not the
    site company's payroll)."""
    if not site_ids:
        return {}
    rows = _exec(conn, 
        "SELECT m.site_id, COUNT(*) AS n "
        "FROM memberships m "
        "JOIN users u ON u.id = m.user_id "
        "JOIN sites s ON s.id = m.site_id "
        "WHERE m.site_id = ANY(%s) AND (u.company_id = s.company_id OR m.external) "
        "AND m.archived_at IS NULL AND u.archived_at IS NULL "
        "GROUP BY m.site_id",
        (list(site_ids),),
    ).fetchall()
    return {str(r["site_id"]): r["n"] for r in rows}


def list_all_memberships(conn) -> list[dict]:
    """Cross-company membership list -- platform_admin only. Mirrors
    list_company_memberships without the company pin; the in-company invariant
    (u.company_id = s.company_id) is still enforced so mis-tenanted rows drop."""
    return _exec(conn, 
        "SELECT m.user_id, u.cognito_sub, m.site_id, m.role, m.external, "
        "hc.name AS home_company_name "
        "FROM memberships m "
        "JOIN users u ON u.id = m.user_id "
        "JOIN sites s ON s.id = m.site_id "
        "LEFT JOIN companies hc ON hc.id = u.company_id "
        "WHERE (u.company_id = s.company_id OR m.external) AND m.archived_at IS NULL "
        "ORDER BY u.created_at, m.created_at",
    ).fetchall()


def members_for_site(conn, company_id, site_id) -> list[dict]:
    """Members of ONE site (memberships-backed), for org-api GET
    /api/org/sites/{id}/members -- the Aurora replacement for legacy
    /site-users, which read config/user_mapping.json and so returned []
    for Aurora-only sites (visibility spec §1.1 'USERS ON SITE empty').
    Pinned to the SITE's company; the members themselves may belong to
    another company (external=true, project-owned tenancy P1) and carry
    home_company_name. Excludes archived members/memberships. Returns each
    user's display columns plus the per-site membership role (site_role)."""
    return _exec(conn, 
        "SELECT u.id, u.cognito_sub, u.first_name, u.last_name, u.folder_name, "
        "u.avatar_s3_key, u.global_role, m.role AS site_role, m.external, "
        "hc.name AS home_company_name "
        "FROM memberships m "
        "JOIN users u ON u.id = m.user_id "
        "JOIN sites s ON s.id = m.site_id "
        "LEFT JOIN companies hc ON hc.id = u.company_id "
        "WHERE m.site_id = %s::uuid AND s.company_id = %s AND (u.company_id = s.company_id OR m.external) "
        "AND m.archived_at IS NULL AND u.archived_at IS NULL "
        "ORDER BY u.first_name, u.last_name",
        (site_id, company_id),
    ).fetchall()


def caller_site_roles(conn, user_id) -> dict:
    """Map {site_id_str: membership.role} for one user across their non-archived
    memberships -- the per-site within-project authority D1 says ACL must now
    read. Drives visible_scope's pm/site_manager grading (a person can be pm on
    Project A and worker on Project B)."""
    rows = conn.execute(
        "SELECT site_id, role FROM memberships WHERE user_id=%s AND archived_at IS NULL",
        (user_id,),
    ).fetchall()
    return {str(r[0]): r[1] for r in rows}


def user_ids_for_sites(conn, site_ids) -> set:
    """Distinct user_ids holding ANY non-archived membership on any of
    site_ids -- the SITE tier's author set (pm / regional_manager see every
    author on an in-scope site, visibility spec 3.1). Mirrors
    worker_user_ids_for_sites exactly, minus the role filter; kept as a
    separate function rather than a role= kwarg so neither call site can
    accidentally widen the other. Empty in -> empty out, no round-trip."""
    if not site_ids:
        return set()
    rows = conn.execute(
        "SELECT DISTINCT user_id FROM memberships "
        "WHERE site_id = ANY(%s::uuid[]) AND archived_at IS NULL",
        (list(site_ids),),
    ).fetchall()
    return {str(r[0]) for r in rows}


def worker_user_ids_for_sites(conn, site_ids) -> set:
    """Distinct user_ids holding a WORKER membership on any of site_ids
    (non-archived) -- the 'workers on the caller's sites' half of a
    site_manager's SELF+WORKERS author set (visibility spec §3.1/D3, the graded
    restatement of BUG-25's site_manager leak fix: a site_manager sees own +
    workers, never other site_managers/pms). Empty in -> empty out, no
    round-trip. ::uuid[] accepts the str ids visible_scope holds."""
    if not site_ids:
        return set()
    rows = conn.execute(
        "SELECT DISTINCT user_id FROM memberships "
        "WHERE site_id = ANY(%s::uuid[]) AND role='worker' AND archived_at IS NULL",
        (list(site_ids),),
    ).fetchall()
    return {str(r[0]) for r in rows}


def site_usable_by_user(conn, user_id, site_id) -> dict | None:
    """The site row (id, company_id) when `user_id` may record onto it, else None.
    Project-owned tenancy P2: a site is usable when the user holds a LIVE
    membership on it (any company -- an external member's home company differs),
    or when it belongs to the user's own company (keeps admin/gm, who carry no
    memberships, working as before). Never a site of a company the user has no
    relation to. One query so every capture/ingest rung applies the same rule."""
    if not user_id or not site_id:
        return None
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT s.id, s.company_id FROM sites s JOIN users u ON u.id = %s "
        "WHERE s.id = %s AND (s.company_id = u.company_id OR EXISTS ("
        "  SELECT 1 FROM memberships m WHERE m.user_id = u.id AND m.site_id = s.id "
        "  AND m.archived_at IS NULL))",
        (user_id, site_id),
    ).fetchone()


def external_site_roles(conn, user_id) -> dict:
    """{site_id_str: membership.role} for the user's LIVE external memberships
    (projects of another company they were added to)."""
    rows = _external_or(
        conn, "SELECT site_id, role FROM memberships WHERE user_id=%s AND external "
        "AND archived_at IS NULL", (user_id,), [])
    return {str(r[0]): r[1] for r in rows}


def has_live_external(conn, user_id) -> bool:
    """Whether the user holds a live membership on another company's project.
    Such a caller's reach includes sites whose rows carry a company other than
    their own, so a bare `company_id = <home company>` pin on a site-scoped read
    would hide exactly those rows (project-owned tenancy P3)."""
    rows = _external_or(
        conn, "SELECT EXISTS (SELECT 1 FROM memberships WHERE user_id=%s AND external "
        "AND archived_at IS NULL)", (user_id,), [])
    return bool(rows and rows[0][0])


def external_site_companies(conn, user_id, within_site_ids=None) -> dict:
    """{site_id_str: company_id_str} for the user's LIVE external memberships
    (optionally only those in `within_site_ids`). The sites whose rows carry a company
    other than the user's own, so a home-company pin has to be waived for exactly
    these site ids -- and only these (final review F4)."""
    rows = _external_or(
        conn, "SELECT m.site_id, s.company_id FROM memberships m JOIN sites s ON s.id = m.site_id "
        "WHERE m.user_id=%s AND m.external AND m.archived_at IS NULL", (user_id,), [])
    out = {str(r[0]): str(r[1]) for r in rows}
    if within_site_ids is not None:
        keep = {str(x) for x in within_site_ids}
        out = {k: v for k, v in out.items() if k in keep}
    return out


def record_audit(conn, action, actor, target_user_id, site, role, membership_id=None) -> None:
    """One membership_audit row (migration 0086): who (`actor` is the caller dict), did what
    (`action`: external_add / external_revive / external_role / external_archive), to whom,
    on which site (`site` is the site row), at which role. Runs on the request's connection,
    so it commits or rolls back with the membership change itself."""
    _audit_insert(conn,
        "INSERT INTO membership_audit (action, actor_user_id, actor_role, target_user_id, "
        "site_id, site_company_id, role, membership_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (action, actor.get("id"), actor.get("global_role"), target_user_id,
         site["id"], site.get("company_id"), role, membership_id))


def _audit_insert(conn, sql, params):
    tx = getattr(conn, "transaction", None)
    try:
        if tx is None:
            conn.execute(sql, params)
        else:
            with tx():
                conn.execute(sql, params)
    except Exception as exc:  # noqa: BLE001
        if not _is_missing_schema(exc):
            raise
        logger.warning("membership_audit missing (migration 0086 not applied yet); "
                       "audit row NOT written for %s", params[0])
