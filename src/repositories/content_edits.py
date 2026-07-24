"""Audit history for editable content correction (migration 0019, spec §5.2).
append on every successful edit; list backs GET /content/{table}/{id}/history.
Company-guarded reads (the endpoint already resolved the row's company)."""
from psycopg.rows import dict_row

_COLS = ("id, company_id, table_name, row_id, field, before_text, after_text, "
         "actor_user_id, actor_role, created_at")
_COLS_QUALIFIED = ", ".join("ce." + c for c in _COLS.split(", "))


def append_content_edit(conn, company_id, table_name, row_id, field,
                        before_text, after_text, actor_user_id, actor_role):
    return conn.cursor(row_factory=dict_row).execute(
        f"INSERT INTO content_edits (company_id, table_name, row_id, field, "
        f"before_text, after_text, actor_user_id, actor_role) "
        f"VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING {_COLS}",
        (company_id, table_name, row_id, field, before_text, after_text,
         actor_user_id, actor_role),
    ).fetchone()


def list_content_edits(conn, company_id, table_name, row_id):
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS_QUALIFIED}, "
        f"       NULLIF(TRIM(CONCAT_WS(' ', u.first_name, u.last_name)), '') AS actor_name "
        f"FROM content_edits ce "
        f"LEFT JOIN users u ON u.id = ce.actor_user_id "
        f"WHERE ce.company_id=%s AND ce.table_name=%s AND ce.row_id=%s "
        f"ORDER BY ce.created_at DESC",
        (company_id, table_name, row_id),
    ).fetchall()


# The site-local wall clock every FieldSight date axis is expressed in (the UI's
# FS.api.todayNZDT()). Named zone, not a hardcoded UTC+13: the +13 shortcut
# scattered through the older lambdas silently mis-buckets closures by an hour
# for the ~7 months a year NZ is on NZST (+12), which for a midnight-bounded
# weekly window means a late-Sunday-evening closure landing in the wrong week.
CLOSURE_TZ = "Pacific/Auckland"

# What counts as "closed": exactly the transition patch_action_item records for
# a check-off (_AUDITED_ACTION_FIELDS 'status', before 'open'/'in_progress'/
# 'blocked' -> after 'done'). Priority/deadline/responsible edits are NOT
# closures, and a no-op re-tick never reaches content_edits at all (the handler
# only appends when the value actually changes) -- the before/after predicate
# below is belt-and-braces for rows written by any future writer.
_CLOSED_STATUS = "done"


def count_action_closures_by_day(conn, site_ids, start_date, end_date,
                                 company_id=None, tz=CLOSURE_TZ):
    """`{'YYYY-MM-DD': n}` — action items CLOSED per local calendar day in
    [start_date, end_date] inclusive, from the content_edits audit trail.

    This is the only store that knows WHEN a task was closed. action_items
    carries `updated_at`, but that is last-write-wins over every field (a
    priority tweak on Friday moves it), and the legacy DynamoDB overlay the
    Today KPI used to read is keyed by the REPORT date of the task, not the
    close date. content_edits rows are append-only, one per changed field,
    stamped with a real `created_at` and actor — so a day bucket here is a
    genuine count of closures that happened on that day.

    Scoping (both applied, neither optional):
      * `site_ids` — the caller's reach (lambda_org_api._allowed_site_ids).
        content_edits has no site_id of its own, so the site comes from the
        action item the row points at. An EMPTY reach returns {} without a
        round-trip: no rows, never "unscoped" (the empty-list-means-no-filter
        trap — see the audit of report ACLs).
      * `company_id` — the tenant. Pass None ONLY for a cross-company caller
        (platform_admin, acl.is_cross_company), where the reach itself already
        spans tenants; every other caller must pass their own company id.

    (table_name, row_id) is a soft reference with no FK (migration 0019), so
    the INNER JOIN also drops closures whose action item has since been
    superseded by re-extraction. That is deliberate and fail-closed: without a
    live row there is no site to authorise the closure against, and a KPI must
    not count what it cannot scope.

    No redaction filter: an action item on a redacted/personal topic is not
    rendered anywhere a user could tick it, so it cannot enter this trail.
    """
    if not site_ids:
        return {}
    where = ["ce.table_name = 'action_items'", "ce.field = 'status'",
             "ce.after_text = %s", "ce.before_text IS DISTINCT FROM %s",
             "a.site_id = ANY(%s::uuid[])",
             "ce.created_at >= (%s::date::timestamp AT TIME ZONE %s)",
             "ce.created_at < ((%s::date + 1)::timestamp AT TIME ZONE %s)"]
    params = [_CLOSED_STATUS, _CLOSED_STATUS, [str(s) for s in site_ids],
              str(start_date), tz, str(end_date), tz]
    if company_id is not None:
        where.append("ce.company_id = %s")
        params.append(company_id)
    rows = conn.cursor(row_factory=dict_row).execute(
        "SELECT (ce.created_at AT TIME ZONE %s)::date AS close_date, "
        "       count(*) AS closed "
        "FROM content_edits ce "
        "JOIN action_items a ON a.id = ce.row_id "
        "WHERE " + " AND ".join(where) + " "
        "GROUP BY 1",
        tuple([tz] + params),
    ).fetchall()
    out = {}
    for r in rows:
        d = r["close_date"]
        out[d.isoformat() if hasattr(d, "isoformat") else str(d)] = int(r["closed"])
    return out
