"""Pure ACL logic. MUST NOT import psycopg (unit-tested locally without a DB)."""

_ALL_ROLES = {"admin", "gm"}


def resolve_scope(global_role: str) -> str:
    return "ALL" if global_role in _ALL_ROLES else "MEMBERSHIPS"
