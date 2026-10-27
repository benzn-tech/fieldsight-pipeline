"""Audit history for editable content correction (migration 0019, spec §5.2).
append on every successful edit; list backs GET /content/{table}/{id}/history.
Company-guarded reads (the endpoint already resolved the row's company)."""
from psycopg.rows import dict_row

from deleted_predicates import DELETED_SOURCE_PREDICATE, DELETED_TOPIC_PREDICATE

_COLS = ("id, company_id, table_name, row_id, field, before_text, after_text, "
         "actor_user_id, actor_role, created_at")
_COLS_QUALIFIED = ", ".join("ce." + c for c in _COLS.split(", "))


def append_content_edit(conn, company_id, table_name, row_id, field,
                        before_text, after_text, actor_user_id, actor_role,
                        created_at=None):
    """`created_at=None` keeps the column default (now()) -- every live writer.
    A backfill passes the moment the thing really happened, because
    count_action_closures_by_day buckets by this column."""
    cols = ("company_id, table_name, row_id, field, before_text, after_text, "
            "actor_user_id, actor_role")
    params = [company_id, table_name, row_id, field, before_text, after_text,
              actor_user_id, actor_role]
    marks = "%s,%s,%s,%s,%s,%s,%s,%s"
    if created_at is not None:
        cols += ", created_at"
        marks += ",%s"
        params.append(created_at)
    return conn.cursor(row_factory=dict_row).execute(
        f"INSERT INTO content_edits ({cols}) VALUES ({marks}) RETURNING {_COLS}",
        tuple(params),
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
#
# `closed` (no longer needs doing: cancelled, superseded, duplicate) is NOT a
# completion and never enters this count. It is counted on its own by passing
# status=NO_LONGER_NEEDED_STATUS, so a week where half the list was cancelled
# does not read as a productive one.
_CLOSED_STATUS = "done"
NO_LONGER_NEEDED_STATUS = "closed"
FINISHED_STATUSES = (_CLOSED_STATUS, NO_LONGER_NEEDED_STATUS)


def count_action_closures_by_day(conn, site_ids, start_date, end_date,
                                 company_id=None, tz=CLOSURE_TZ,
                                 status=_CLOSED_STATUS):
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
    if status not in FINISHED_STATUSES:
        raise ValueError(f"status must be one of {FINISHED_STATUSES}")
    params = [status, status, [str(s) for s in site_ids],
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


def list_action_finishes(conn, site_ids, start_date, end_date, company_id=None,
                         author_ids=None, tz=CLOSURE_TZ, limit=50):
    """The action items that ENDED [start_date, end_date] finished -- `done` or
    `closed` -- with when and by whom, newest first. Backs the report's
    "Closed today" / "Closed this week" sections.

    Read from the audit trail, not from action_items.status, because a status
    alone says nothing about WHEN: "closed this week" needs the transition's own
    timestamp and actor.

    One row per item identity (stable_id), its LAST status change inside the
    window. Ticked on Monday and reopened on Tuesday is not closed this week, and
    a carried row (re-extraction gives the item a new id, same stable_id) does not
    list the item twice. A change before the window cannot decide anything here:
    with no change inside the window the item did not finish inside it.

    Scoping is the same pair count_action_closures_by_day takes (reach + tenant;
    an empty reach returns [] without a round-trip). Unlike that count, this
    returns TEXT, so it also applies every rule a topic read does before showing
    words: not deleted by topic or by recording, no active redaction of any
    scope, not a non-work conversation. The supersession arm is deliberately
    NOT applied -- a tick recorded on the row a later pass superseded is still
    a real closure, and stable_id is what collapses it with its successor.

    `author_ids` narrows to items raised in those people's recordings (a
    person's day report lists what happened to THEIR list, and must not show
    the words of recordings its reader was never cleared to open). None = no
    author filter; an empty list returns [] (never "everyone").
    """
    if not site_ids or (author_ids is not None and not author_ids):
        return []
    company_sql, company_params = "", []
    if company_id is not None:
        company_sql = "AND ce.company_id = %s "
        company_params = [company_id]
    author_sql, author_params = "", []
    if author_ids is not None:
        author_sql = "AND t.user_id = ANY(%s::uuid[]) "
        author_params = [[str(a) for a in author_ids]]
    rows = conn.cursor(row_factory=dict_row).execute(
        "WITH last AS ("
        "  SELECT DISTINCT ON (a.stable_id) a.stable_id, a.text, a.topic_id, "
        "         ce.after_text AS status, ce.created_at, ce.actor_user_id "
        "  FROM content_edits ce JOIN action_items a ON a.id = ce.row_id "
        "  WHERE ce.table_name = 'action_items' AND ce.field = 'status' "
        "    AND ce.after_text IS DISTINCT FROM ce.before_text "
        "    AND a.site_id = ANY(%s::uuid[]) "
        "    AND ce.created_at >= (%s::date::timestamp AT TIME ZONE %s) "
        "    AND ce.created_at < ((%s::date + 1)::timestamp AT TIME ZONE %s) "
        "    " + company_sql +
        "  ORDER BY a.stable_id, ce.created_at DESC) "
        "SELECT l.stable_id, l.text, l.status, l.created_at, "
        "       (l.created_at AT TIME ZONE %s)::date AS closed_on, "
        "       NULLIF(TRIM(CONCAT_WS(' ', u.first_name, u.last_name)), '') AS actor_name "
        "FROM last l JOIN topics t ON t.id = l.topic_id "
        "LEFT JOIN users u ON u.id = l.actor_user_id "
        "WHERE l.status = ANY(%s) " + author_sql +
        f"  AND {DELETED_TOPIC_PREDICATE.format(alias='t')} "
        f"  AND {DELETED_SOURCE_PREDICATE.format(alias='t')} "
        "  AND NOT EXISTS (SELECT 1 FROM redactions ra WHERE ra.target_type = 'topic' "
        "                  AND ra.target_id = t.id AND ra.reverted_at IS NULL) "
        "  AND t.work_class IS DISTINCT FROM 'non_work' "
        "ORDER BY l.created_at DESC LIMIT %s",
        tuple([[str(s) for s in site_ids], str(start_date), tz, str(end_date), tz]
              + company_params + [tz, list(FINISHED_STATUSES)] + author_params
              + [int(limit)]),
    ).fetchall()
    out = []
    for r in rows:
        d = r["closed_on"]
        out.append({
            "stable_id": str(r["stable_id"]),
            "text": r["text"],
            "status": r["status"],
            "closed_on": d.isoformat() if hasattr(d, "isoformat") else str(d),
            "closed_by": r["actor_name"],
        })
    return out
