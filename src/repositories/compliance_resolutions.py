"""Durable safety/quality resolved-state (migration 0025). Retires the
unauthenticated DynamoDB check-off overlay (POST /api/actions/toggle) for
compliance rows. Spec 2026-07-26.

Keyed on the re-extraction-stable natural identity
(company_id, site_id, report_date, domain, user_folder, content_hash) where
content_hash = sha256(normalize(displayed text)) -- see src/content_hash.py.

Thin repo: executes SQL, NEVER commits -- the caller owns the transaction
(see db.connection.get_connection). Style mirrors action_items /
content_edits (module-level SQL, conn.cursor(row_factory=dict_row))."""
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row

# Display name of the resolver for the "Resolved by <name> · <time>" caption.
# resolved_by holds a users.id (migration 0025: `resolved_by uuid REFERENCES
# users(id)`), so the join is on users.id -- NOT cognito_sub (that is the
# action_items.updated_by join; different column, different table history).
# NULLIF(TRIM(CONCAT_WS(' ', ...))) is the exact NULL/blank-safe form already
# used by content_edits.list_content_edits and action_items: CONCAT_WS skips
# NULL parts, so an account with no last_name yields "Ada" (never "Ada "), and
# an account with neither name collapses to NULL instead of "" -- the
# trailing-space trap that renders a blank resolver chip in the UI.
_RESOLVER_NAME = "NULLIF(TRIM(CONCAT_WS(' ', u.first_name, u.last_name)), '')"

# The 6-column natural key, in constraint order. The upsert's ON CONFLICT
# target and the rekey WHERE both reference exactly this tuple.
_KEY_COLS = ("company_id", "site_id", "report_date", "domain",
             "user_folder", "content_hash")


def upsert_resolution(conn, company_id, site_id, report_date, domain,
                      user_folder, content_hash, content_sample, resolved,
                      resolved_by):
    """Insert or flip one resolution on the 6-col natural key (§3). A reopen is
    an UPDATE resolved=false, NOT a delete, so the resolver line + audit survive
    and the union read never falls back to a stale overlay `true`. resolved_by
    is a users.id (nullable). content_hash/content_sample are computed
    server-side from normalize(text) -- the client never sends the hash.

    Returns the row plus the resolver's null-safe display name in ONE
    round-trip (resolved_by_name), so the handler can repoint the optimistic
    UI without a second query."""
    return conn.cursor(row_factory=dict_row).execute(
        f"WITH up AS ("
        f"  INSERT INTO compliance_resolutions "
        f"    (company_id, site_id, report_date, domain, user_folder, "
        f"     content_hash, content_sample, resolved, resolved_by) "
        f"  VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        f"  ON CONFLICT (company_id, site_id, report_date, domain, user_folder, content_hash) "
        f"  DO UPDATE SET resolved=EXCLUDED.resolved, "
        f"                resolved_by=EXCLUDED.resolved_by, "
        f"                resolved_at=now(), updated_at=now() "
        f"  RETURNING id, resolved, resolved_by, resolved_at, "
        f"            content_hash, content_sample "
        f") "
        f"SELECT up.id, up.resolved, up.resolved_by, up.resolved_at, "
        f"       up.content_hash, up.content_sample, "
        f"       {_RESOLVER_NAME} AS resolved_by_name "
        f"FROM up LEFT JOIN users u ON u.id = up.resolved_by",
        (company_id, site_id, report_date, domain, user_folder,
         content_hash, content_sample, resolved, resolved_by),
    ).fetchone()


def list_resolutions(conn, company_id, site_ids, date_from, date_to, domain=None):
    """Range read for the union aggregator (§4). Returns rows shaped for the
    lookup the aggregator rebuilds -- keyed
    site_id|report_date|domain|user_folder|content_hash -- plus content_sample
    (the parity fallback) and the null-safe resolved_by display name.

    Scoping (both applied, neither optional):
      * site_ids -- the caller's reach (_allowed_site_ids). An EMPTY reach
        returns [] WITHOUT a round-trip: no rows, never "unscoped" (the
        empty-list-means-no-filter trap -- [] must mean "nothing", not "all").
      * company_id -- the tenant. Pass None ONLY for a cross-company caller
        (platform_admin, is_cross_company) whose reach already spans tenants;
        every other caller passes their own company id.

    domain is optional: omitted => both safety and quality (the global Insights
    read); passed => that page only."""
    if not site_ids:
        return []
    where = ["cr.site_id = ANY(%s::uuid[])",
             "cr.report_date >= %s", "cr.report_date <= %s"]
    params = [[str(s) for s in site_ids], str(date_from), str(date_to)]
    if company_id is not None:
        where.append("cr.company_id = %s")
        params.append(company_id)
    if domain is not None:
        where.append("cr.domain = %s")
        params.append(domain)
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT cr.site_id, cr.report_date, cr.domain, cr.user_folder, "
        f"       cr.content_hash, cr.content_sample, cr.resolved, cr.resolved_at, "
        f"       {_RESOLVER_NAME} AS resolved_by "
        f"FROM compliance_resolutions cr "
        f"LEFT JOIN users u ON u.id = cr.resolved_by "
        f"WHERE " + " AND ".join(where) + " "
        f"ORDER BY cr.report_date, cr.site_id",
        tuple(params),
    ).fetchall()


def rekey_resolution(conn, old_key_parts, new_content_hash, new_content_sample):
    """Move a resolved mark from its old content_hash to new_content_hash after
    a content edit changed the row's displayed text (§3.1, item 1b).
    old_key_parts is a dict carrying the first five natural-key components plus
    the OLD content_hash: {company_id, site_id, report_date, domain,
    user_folder, content_hash}.

    UPDATE-only, three invariants (all required by the spec):
      * Only-when-a-mark-exists: matches the old key; affects 0 rows and is a
        silent no-op (returns None) when the row was never resolved. No
        SELECT-then-decide -- the vast majority of edits touch unresolved rows
        and must cost nothing.
      * No-op on no text change: old==new hash returns immediately.
      * Best-effort / connection-safe: the UPDATE runs inside a SAVEPOINT
        (`with conn.transaction()`), so a UNIQUE violation -- the edit made the
        text collide with an already-resolved sibling on the same
        site/day/domain/folder -- rolls back ONLY this move and is swallowed as
        a benign no-op (the target mark already exists; same class as the
        ordinary collision). The caller's content edit is never touched, and
        the connection is never left in a poisoned state.

    Returns the moved row {id, content_hash} or None (no mark / no-op / merge)."""
    if old_key_parts.get("content_hash") == new_content_hash:
        return None
    try:
        with conn.transaction():          # SAVEPOINT: isolates a merge collision
            return conn.cursor(row_factory=dict_row).execute(
                "UPDATE compliance_resolutions "
                "SET content_hash=%s, content_sample=%s, updated_at=now() "
                "WHERE company_id=%s AND site_id=%s AND report_date=%s "
                "  AND domain=%s AND user_folder=%s AND content_hash=%s "
                "RETURNING id, content_hash",
                (new_content_hash, new_content_sample,
                 old_key_parts["company_id"], old_key_parts["site_id"],
                 old_key_parts["report_date"], old_key_parts["domain"],
                 old_key_parts["user_folder"], old_key_parts["content_hash"]),
            ).fetchone()
    except UniqueViolation:
        return None
