"""Pure ACL logic. MUST NOT import psycopg (unit-tested locally without a DB)."""

_ALL_ROLES = {"admin", "gm"}


def resolve_scope(global_role: str) -> str:
    return "ALL" if global_role in _ALL_ROLES else "MEMBERSHIPS"


# --- Graded roles (visibility spec §3.1, D1/D2/D3/D6). resolve_scope above is
# the LEGACY binary primitive still used by the admin/gm write gates and by the
# read paths while GRADED_ROLES is off; the two functions below are the graded
# WITHIN-project authority + cross-company predicate used once GRADED_ROLES is
# on. Kept PURE (no psycopg import) so they stay locally unit-testable, like
# resolve_scope. ---

_CROSS_COMPANY_ROLES = {"platform_admin"}                 # D6: the ONLY cross-company tier
_ALL_USER_SCOPE_ROLES = {"platform_admin", "admin", "gm"}  # no per-author filter
_SITE_USER_SCOPE_ROLES = {"regional_manager", "pm"}        # D2: cross-project within one company; pm


def is_cross_company(global_role: str) -> bool:
    """D6: platform_admin is the sole role whose visibility crosses the company
    boundary. Every other role is hard-pinned to caller.company_id."""
    return global_role in _CROSS_COMPANY_ROLES


def visible_user_scope(global_role: str, membership_roles) -> str:
    """The caller's WITHIN-project authority — the per-author filter tier a read
    path applies on top of site_ids (visibility spec §3.1 table + D1/D3).
    global_role sets cross-project REACH (regional/gm/admin/platform); the
    per-site membership.role sets within-project AUTHORITY (pm/site_manager/
    worker). membership_roles = the set of the caller's membership.role values
    across their accessible sites; it is a FLOOR over global_role (D1's
    Neil-at-UC-PK example: a global 'worker' with a pm membership -> SITE).
    Returns one of:
      ALL          -> no per-author filter (platform_admin/admin/gm)
      SITE         -> every author on an in-scope site (regional_manager or pm)
      SELF+WORKERS -> own + worker-role members on the caller's sites (D3)
      SELF         -> own only (worker)."""
    if global_role in _ALL_USER_SCOPE_ROLES:
        return "ALL"
    roles = set(membership_roles or ())
    if global_role in _SITE_USER_SCOPE_ROLES or "pm" in roles:
        return "SITE"
    if global_role == "site_manager" or "site_manager" in roles:
        return "SELF+WORKERS"
    return "SELF"
