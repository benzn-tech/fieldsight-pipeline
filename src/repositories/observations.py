import psycopg
from psycopg.rows import dict_row

_COLS = ("id, company_id, kind, site_slug, report_date, author_sub, author_name, "
         "observation, risk_level, recommended_action, status, archived_at, "
         "created_at, updated_at")


def create_observation(conn, company_id, kind, site_slug, author_sub, author_name,
                       observation, risk_level=None, recommended_action=None,
                       report_date=None) -> dict:
    """Insert a manual (safety/quality) observation and return the new row.

    report_date has no SQL default — unlike status/created_at/updated_at it
    is effectively a REQUIRED argument, kept as a trailing kwarg only for
    call-site readability. The endpoint layer always computes it (NZ "today")
    and passes it explicitly; the repository stays dumb and never invents a
    date. Passing None inserts NULL and fails the NOT NULL constraint.
    """
    return conn.cursor(row_factory=dict_row).execute(
        f"INSERT INTO observations (company_id, kind, site_slug, report_date, "
        f"author_sub, author_name, observation, risk_level, recommended_action) "
        f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING {_COLS}",
        (company_id, kind, site_slug, report_date, author_sub, author_name,
         observation, risk_level, recommended_action),
    ).fetchone()


def list_observations(conn, company_id, kind=None, date_from=None, date_to=None,
                      site_slug=None, allowed_site_slugs=None,
                      include_archived=False) -> list[dict]:
    """Company-scoped list with optional filters, newest report_date first.

    allowed_site_slugs (visibility spec §3.1, Phase 3 graded roles):
    optionally narrows to observations whose site_slug is in the caller's
    resolved in-scope set; None (default) = company-wide, today's behavior
    unchanged. An empty set is a real "no in-scope sites" filter (not
    treated as None) -- it still applies and yields zero rows."""
    conditions = ["company_id = %s"]
    params = [company_id]
    if kind is not None:
        conditions.append("kind = %s")
        params.append(kind)
    if site_slug is not None:
        conditions.append("site_slug = %s")
        params.append(site_slug)
    if allowed_site_slugs is not None:
        conditions.append("site_slug = ANY(%s)")
        params.append(list(allowed_site_slugs))
    if date_from is not None:
        conditions.append("report_date >= %s")
        params.append(date_from)
    if date_to is not None:
        conditions.append("report_date <= %s")
        params.append(date_to)
    if not include_archived:
        conditions.append("archived_at IS NULL")
    where = " AND ".join(conditions)
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM observations WHERE {where} "
        f"ORDER BY report_date DESC, created_at DESC",
        params,
    ).fetchall()


def get_observation(conn, company_id, obs_id) -> dict | None:
    """Company-guarded single-row fetch. Returns None if not found, wrong
    company, or obs_id is not a valid UUID — a malformed id is treated the
    same as a missing one (404 semantics), so callers don't need to
    pre-validate the id format themselves."""
    try:
        return conn.cursor(row_factory=dict_row).execute(
            f"SELECT {_COLS} FROM observations WHERE id=%s AND company_id=%s",
            (obs_id, company_id),
        ).fetchone()
    except psycopg.Error:
        conn.rollback()
        return None


def set_status(conn, company_id, obs_id, status) -> dict | None:
    """Company-guarded status transition. Returns None if not found, wrong
    company, or obs_id is not a valid UUID (see get_observation)."""
    try:
        return conn.cursor(row_factory=dict_row).execute(
            f"UPDATE observations SET status=%s, updated_at=now() "
            f"WHERE id=%s AND company_id=%s RETURNING {_COLS}",
            (status, obs_id, company_id),
        ).fetchone()
    except psycopg.Error:
        conn.rollback()
        return None


def set_archived(conn, company_id, obs_id, archived) -> dict | None:
    """Company-guarded soft-delete toggle (archived=True) / restore
    (archived=False), mirroring sites.archive_site / unarchive_site: the
    WHERE guard requires the row to currently be in the OPPOSITE state, so
    re-archiving an already-archived row (or unarchiving an active one) is a
    no-op that returns None rather than silently succeeding. Also returns
    None if not found, wrong company, or obs_id is not a valid UUID."""
    guard = "archived_at IS NULL" if archived else "archived_at IS NOT NULL"
    set_clause = "archived_at=now()" if archived else "archived_at=NULL"
    try:
        return conn.cursor(row_factory=dict_row).execute(
            f"UPDATE observations SET {set_clause} "
            f"WHERE id=%s AND company_id=%s AND {guard} RETURNING {_COLS}",
            (obs_id, company_id),
        ).fetchone()
    except psycopg.Error:
        conn.rollback()
        return None
