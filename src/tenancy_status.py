"""Company status reads and the caller gate for company offboarding.

Spec: docs/superpowers/specs/2026-11-01-company-offboarding-design.md §4, §5.1, §10.

companies.status ('active' | 'suspended' | 'exited') arrives in migration 0092, and the
deploy runs `sam deploy` BEFORE the migrate step. So every read here:
  * runs in its own query, never through users._COLS / get_user_by_sub;
  * runs inside `conn.transaction()` -- a SAVEPOINT when the caller already has a
    transaction open (the normal case: a request runs in one transaction), so a failing
    read rolls back alone and never aborts the request's transaction. If no transaction
    is open yet, psycopg3 instead BEGINs and COMMITs around the read, which is harmless
    for a SELECT;
  * treats a missing column / table (SQLSTATE 42703 / 42P01) as "not migrated yet":
    company_status -> 'active', inactive_company_ids -> empty set. Any other database
    error propagates (the savepoint has already un-poisoned the transaction).

Enforcement is behind COMPANY_STATUS_ENFORCE (enforce_enabled). With it off the gate is
shadow-only: it logs COMPANY_GATE_WOULD_BLOCK at WARNING (Lambda's root logger drops
INFO) and allows the call.
"""
import logging
import os

logger = logging.getLogger(__name__)

ACTIVE = "active"
SUSPENDED = "suspended"
EXITED = "exited"

REASON_COMPANY_INACTIVE = "company_inactive"
REASON_ACCOUNT_ARCHIVED = "account_archived"

_ENFORCE_ON = ("on", "true", "1")
# undefined_column, undefined_table: the window between `sam deploy` and migration 0092.
_NOT_MIGRATED = ("42703", "42P01")


def enforce_enabled() -> bool:
    """True only when COMPANY_STATUS_ENFORCE is exactly "on", "true" or "1"
    (case-insensitive, no trimming). Unset or anything else is OFF (shadow).
    Read at call time, never cached."""
    return (os.environ.get("COMPANY_STATUS_ENFORCE") or "").lower() in _ENFORCE_ON


STATUSES = (ACTIVE, SUSPENDED, EXITED)


def _not_migrated(exc) -> bool:
    return getattr(exc, "sqlstate", None) in _NOT_MIGRATED


def not_migrated(exc) -> bool:
    """True when `exc` is a database error meaning migration 0092 is not applied yet
    (undefined column / table). For endpoints that answer 503 `not migrated`."""
    return _not_migrated(exc)


def company_statuses(conn) -> dict:
    """{str(company id): status} for every company -- or {} when companies.status is not
    migrated yet (callers then treat every company as 'active'). Savepoint-guarded (module
    doc). A separate query on purpose: companies.list_companies is shared with other
    routes and must not grow a 0092 column."""
    try:
        with conn.transaction():
            rows = conn.execute("SELECT id, status FROM companies").fetchall()
    except Exception as exc:  # noqa: BLE001 - narrowed below
        if _not_migrated(exc):
            return {}
        raise
    return {str(r[0]): r[1] for r in rows}


def company_status(conn, company_id) -> str:
    """The company's status: 'active', 'suspended' or 'exited'.

    Returns 'active' when company_id is None, when no such company row exists (nothing to
    block on -- the caller guards that decide "no company" already do so themselves), and
    when the status column/table is not migrated yet. Savepoint-guarded (module doc)."""
    if company_id is None:
        return ACTIVE
    try:
        with conn.transaction():
            row = conn.execute(
                "SELECT status FROM companies WHERE id = %s", (company_id,)).fetchone()
    except Exception as exc:  # noqa: BLE001 - narrowed below
        if _not_migrated(exc):
            return ACTIVE
        raise
    if row is None or row[0] is None:
        return ACTIVE
    return row[0]


def inactive_company_ids(conn) -> set:
    """Ids of every company whose status is not 'active' (same type psycopg returns for
    users.company_id / sites.company_id). Empty set when none -- or when the column is
    not migrated yet. Callers must treat an empty set as "inject no filter" (R4).
    Savepoint-guarded (module doc)."""
    try:
        with conn.transaction():
            rows = conn.execute(
                "SELECT id FROM companies WHERE status <> 'active'").fetchall()
    except Exception as exc:  # noqa: BLE001 - narrowed below
        if _not_migrated(exc):
            return set()
        raise
    return {r[0] for r in rows}


INACTIVE_SITES_MEMO = "_inactive_site_ids"


def inactive_site_ids(conn, caller=None) -> set | None:
    """The live-company choke point (spec §5.2, ruling R4): ids (as str) of every site
    whose company is not 'active', i.e. the sites a non-platform-admin caller must not
    reach -- or None, meaning "filter nothing".

    None when the flag is off (no DB read at all, so traffic is byte-identical to before),
    when the caller is platform_admin (R5), when no company is inactive (the second query is
    then never run), and when companies.status is not migrated yet. Never an empty set.

    Callers apply it by post-filtering in Python (drop_inactive), so every existing SQL
    statement stays byte-identical. Memoized on the caller dict (one read per request,
    like scope.visible_scope's `_visible_scope`); anything that echoes the caller dict
    must strip INACTIVE_SITES_MEMO. Savepoint-guarded (module doc)."""
    if not enforce_enabled():
        return None
    if caller is not None:
        if caller.get("global_role") == "platform_admin":
            return None
        if INACTIVE_SITES_MEMO in caller:
            return caller[INACTIVE_SITES_MEMO]
    out = None
    company_ids = inactive_company_ids(conn)
    if company_ids:
        try:
            with conn.transaction():
                rows = conn.execute(
                    "SELECT id FROM sites WHERE company_id = ANY(%s)",
                    (list(company_ids),)).fetchall()
        except Exception as exc:  # noqa: BLE001 - narrowed below
            if not _not_migrated(exc):
                raise
            rows = []
        out = {str(r[0]) for r in rows} or None
    if caller is not None:
        caller[INACTIVE_SITES_MEMO] = out
    return out


def drop_inactive(items, inactive):
    """`items` without the entries whose site id is in `inactive` (inactive_site_ids).
    Works on a set / list / tuple of site ids (str or UUID) or a dict keyed by site id;
    returns the same kind. `inactive` None -> `items` unchanged (the same object)."""
    if not inactive:
        return items
    if isinstance(items, dict):
        return {k: v for k, v in items.items() if str(k) not in inactive}
    kept = [x for x in items if str(x) not in inactive]
    if isinstance(items, (set, frozenset)):
        return set(kept)
    if isinstance(items, tuple):
        return tuple(kept)
    return kept


def blocked_reason(conn, caller) -> str | None:
    """What WOULD block this caller, regardless of the flag: 'account_archived',
    'company_inactive' or None. platform_admin -> None (exempt, no DB read). Archived is
    decided from the caller row without a DB read. No logging."""
    if not caller:
        return None
    if caller.get("global_role") == "platform_admin":
        return None
    if caller.get("archived_at") is not None:
        return REASON_ACCOUNT_ARCHIVED
    if company_status(conn, caller.get("company_id")) != ACTIVE:
        return REASON_COMPANY_INACTIVE
    return None


def write_refusal(conn, caller, company_id, where: str = "") -> str | None:
    """Writes that add people or access to an inactive company (spec §5.6, Task 5).

    `company_id` is the TARGET of the write -- the company the member / membership / site
    belongs to, or the company owning the site a session lies on -- never the caller's own
    company as such (that is the caller gate's question, caller_blocked).

    platform_admin -> None, no DB read (R5: comeback and admin fix-ups write into an
    inactive company). Target active, unknown or not migrated -> None.
    Target inactive: flag on  -> 'company_inactive' (the endpoint answers 409);
                     flag off -> None, after one WARNING line
                     `COMPANY_GATE_WOULD_REFUSE where=... company=... status=... user=...`.

    Callers must check BEFORE their first write: an error() return is a clean exit and
    the request transaction commits."""
    if caller and caller.get("global_role") == "platform_admin":
        return None
    if company_id is None:
        return None
    status = company_status(conn, company_id)
    if status == ACTIVE:
        return None
    if enforce_enabled():
        return REASON_COMPANY_INACTIVE
    logger.warning(
        "COMPANY_GATE_WOULD_REFUSE where=%s company=%s status=%s user=%s sub=%s",
        where, company_id, status, (caller or {}).get("id"),
        (caller or {}).get("cognito_sub"))
    return None


def caller_blocked(conn, caller, *, where: str = "") -> str | None:
    """The caller gate (spec §5.1). Call at CALLER resolution, never at lookups of
    other users.

    Flag on  -> the reason ('account_archived' | 'company_inactive') or None.
    Flag off -> always None; when a reason exists, logs one WARNING line
                `COMPANY_GATE_WOULD_BLOCK reason=... user=... sub=... company=... where=...`.

    platform_admin is exempt (R5): None, no DB read, no log.

    Note on 'account_archived': org-api already refuses archived callers today with no
    flag involved. Callers that already enforce archived (org-api dispatch) KEEP their own
    archived check unchanged, so that block stays byte-identical whatever this flag says;
    they use this gate for the company check (and may see 'account_archived' here first
    only after their own check has already refused). Callers that never checked archived
    (rag-search, ws-connect, voice-resolve, ...) get it from here, behind the flag."""
    reason = blocked_reason(conn, caller)
    if reason is None:
        return None
    if enforce_enabled():
        return reason
    logger.warning(
        "COMPANY_GATE_WOULD_BLOCK reason=%s user=%s sub=%s company=%s where=%s",
        reason, caller.get("id"), caller.get("cognito_sub"), caller.get("company_id"),
        where)
    return None


def suppress_decision(company_id, status, *, where: str = "", site_id=None) -> str | None:
    """Customer email / generated reports for one item (spec §5.5, Task 9), once the
    deciding company and its status are known. `status` None or 'active' -> None, silent.
    Inactive: flag on -> 'company_inactive' (the caller does not send / generate);
    flag off -> None after one WARNING line
    `COMPANY_GATE_WOULD_SUPPRESS where=... company=... site=... status=...`.

    Pure (no DB): the non-VPC senders decide from the published directory
    (directory.suppress_reason) through this same function."""
    if status is None or status == ACTIVE:
        return None
    if enforce_enabled():
        return REASON_COMPANY_INACTIVE
    logger.warning("COMPANY_GATE_WOULD_SUPPRESS where=%s company=%s site=%s status=%s",
                   where, company_id, site_id, status)
    return None


def site_company_status(conn, site_id):
    """(company id, status) of the company that owns `site_id`, or None when there is no
    such site. Not migrated -> (None, 'active'). Savepoint-guarded (module doc)."""
    try:
        with conn.transaction():
            row = conn.execute(
                "SELECT c.id, c.status FROM sites s JOIN companies c ON c.id = s.company_id "
                "WHERE s.id = %s", (str(site_id),)).fetchone()
    except Exception as exc:  # noqa: BLE001 - narrowed below
        if _not_migrated(exc):
            return None, ACTIVE
        raise
    return (row[0], row[1]) if row is not None else None


def suppress_reason(conn, *, site_id=None, company_id=None, where: str = "") -> str | None:
    """Should a customer email / generated report about one item be suppressed?

    The deciding company is the SITE's company of the item (`site_id`); for a site-less
    item -- or a site id with no row -- it is `company_id` (the recipient's or the folder
    owner's HOME company). Returns 'company_inactive' or None; see suppress_decision for
    the flag and the shadow log. Not migrated (no companies.status) -> None. Both reads
    savepoint-guarded (module doc), so the caller's transaction stays usable."""
    if site_id is not None:
        found = site_company_status(conn, site_id)
        if found is not None:
            return suppress_decision(found[0], found[1], where=where, site_id=site_id)
    if company_id is None:
        return None
    return suppress_decision(company_id, company_status(conn, company_id), where=where,
                             site_id=site_id)
