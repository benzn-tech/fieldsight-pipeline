"""The programme time-window query.

Spec: fieldsight-ui/docs/superpowers/specs/2026-08-02-programme-foundation-design.md §7

This is the load boundary, not a filter. A ten-week window over a programme of
any size is typically a few hundred rows, which is why programme size stopped
being a rendering problem: we never render 30,000 tasks because we never fetch
them.

One subtlety. A task inside the window can have a parent outside it, and
dropping the parent leaves the tree rendering as orphans. So the CTE takes the
matching rows and walks up to the root, marking the added ancestors
`in_window = false` — they are context, and the client greys them rather than
presenting them as work happening now.
"""
from psycopg.rows import dict_row

_COLS = (
    "t.id, t.programme_id, t.source_task_id, t.parent_id, t.origin, t.name, "
    "t.wbs_code, t.start_date, t.end_date, t.duration_days, t.progress_pct, "
    "t.status, t.zone, t.total_float_days, t.is_critical, "
    "t.removed_in_version, t.locally_modified, t.sort_order, t.row_version, "
    "t.created_at"
)


def tasks_in_window(conn, programme_id, *, date_from, date_to, assignee=None):
    """Tasks overlapping [date_from, date_to], plus their ancestors.

    Overlap, not containment: a task that starts before the window and ends
    after it is in the window — and containment would hide precisely the long
    tasks a PM most needs to see.

    `assignee=None` means no restriction. Note the `is not None` test rather
    than a truthiness one: an empty-string assignee must still filter, or a
    caller with no folder identity would be handed the entire programme. And
    the None case must never be turned into an empty allow-list; that
    inversion has shipped here before, and in this query it would render an
    empty programme with no error.
    """
    params = [programme_id, date_to, date_from]
    assignee_clause = ""
    if assignee is not None:
        assignee_clause = (
            " AND EXISTS (SELECT 1 FROM programme_task_assignees a "
            "             WHERE a.task_id = t.id AND a.assignee = %s)")
        params.append(assignee)

    sql = f"""
        WITH RECURSIVE matched AS (
            SELECT t.id, t.parent_id
              FROM programme_tasks t
             WHERE t.programme_id = %s
               AND t.removed_in_version IS NULL
               AND t.start_date <= %s
               AND t.end_date   >= %s
               {assignee_clause}
        ),
        with_ancestors AS (
            SELECT id, parent_id, true AS in_window FROM matched
            UNION
            SELECT p.id, p.parent_id, false AS in_window
              FROM programme_tasks p
              JOIN with_ancestors w ON w.parent_id = p.id
             WHERE p.removed_in_version IS NULL
        )
        SELECT {_COLS}, bool_or(w.in_window) AS in_window
          FROM programme_tasks t
          JOIN with_ancestors w ON w.id = t.id
         GROUP BY t.id
         ORDER BY t.sort_order, t.wbs_code NULLS LAST, t.created_at
    """
    return conn.cursor(row_factory=dict_row).execute(sql, tuple(params)).fetchall()
