"""Pure ACL logic. MUST NOT import psycopg (unit-tested locally without a DB)."""

_ALL_ROLES = {"admin", "gm"}

# Global role hierarchy (mirrors prod API: admin/gm > pm > site_manager > worker).
ROLE_RANK = {"admin": 5, "gm": 4, "pm": 3, "site_manager": 2, "worker": 1}
VALID_GLOBAL_ROLES = frozenset(ROLE_RANK)
# Site-level membership roles: admin/gm are company-scoped, never per-site.
VALID_MEMBERSHIP_ROLES = frozenset({"pm", "site_manager", "worker"})


def resolve_scope(global_role: str) -> str:
    return "ALL" if global_role in _ALL_ROLES else "MEMBERSHIPS"


def can_manage_org(global_role: str) -> bool:
    """Company-level org writes (create sites, add members, set roles)."""
    return ROLE_RANK.get(global_role, 0) >= ROLE_RANK["gm"]


def can_assign_role(caller_role: str, target_role: str) -> bool:
    """Anti-privilege-escalation: a caller may only assign roles at or below
    their own rank, and must hold org-management rank themselves. Roles
    outside the whitelist are always rejected (deny-by-default)."""
    if target_role not in VALID_GLOBAL_ROLES:
        return False
    if not can_manage_org(caller_role):
        return False
    return ROLE_RANK[target_role] <= ROLE_RANK.get(caller_role, 0)


def can_modify_user(caller_role: str, target_current_role: str) -> bool:
    """A caller may only change the role of someone at or below their own
    rank (a gm must not touch an admin)."""
    if not can_manage_org(caller_role):
        return False
    return ROLE_RANK.get(target_current_role, 0) <= ROLE_RANK.get(caller_role, 0)
