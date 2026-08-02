"""Repository for programme_delay_flags (migration 0027).

Spec: fieldsight-ui/docs/superpowers/specs/2026-08-02-programme-foundation-design.md §10

Scenario D. A site manager knows a date has slipped before the plan does, but
cannot move a contract date — the next import would overwrite it, so accepting
that edit would be a lie. They raise a flag instead: reason, expected new end,
and the task it is about. It surfaces to the PM, who reschedules in P6/MSP and
re-imports.

That makes this table the only carrier of the signal between "site knows" and
"plan reflects". A flag that is malformed, invisible, or closable by the
person who raised it is worse than no flag, because the site manager believes
they have reported the problem.

Style mirrors src/repositories/programme_suggestions.py.
"""
from psycopg.rows import dict_row

_COLS = ("id, task_id, raised_by, reason, expected_end, state, "
         "created_at, resolved_at")

_STATES = ("open", "acknowledged", "resolved")


def raise_flag(conn, *, task_id, raised_by, reason, expected_end) -> dict:
    """A reason is mandatory: a flag the PM cannot act on is noise, and noise
    trains people to ignore the channel. expected_end is optional — a site
    manager often knows a task will slip before knowing by how much, and
    requiring a date would push them to invent one."""
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("a delay flag needs a reason")
    return conn.cursor(row_factory=dict_row).execute(
        f"INSERT INTO programme_delay_flags "
        f"(task_id, raised_by, reason, expected_end) "
        f"VALUES (%s,%s,%s,%s) RETURNING {_COLS}",
        (task_id, raised_by, reason, expected_end),
    ).fetchone()


def get(conn, flag_id) -> dict | None:
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM programme_delay_flags WHERE id = %s",
        (flag_id,),
    ).fetchone()


def list_for_site(conn, site_id, state="open") -> list[dict]:
    """Flags hang off tasks, tasks off programmes, programmes off sites. The
    ACL is applied on site_id, so the join has to reach it — filtering in the
    handler instead would mean fetching another site's flags first.

    `state=None` returns every state (for a history view)."""
    params = [site_id]
    state_clause = ""
    if state is not None:
        state_clause = " AND f.state = %s"
        params.append(state)
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT f.id, f.task_id, f.raised_by, f.reason, f.expected_end, "
        f"       f.state, f.created_at, f.resolved_at, "
        f"       t.name AS task_name, t.source_task_id, "
        f"       t.start_date, t.end_date "
        f"  FROM programme_delay_flags f "
        f"  JOIN programme_tasks t ON t.id = f.task_id "
        f"  JOIN programmes p ON p.id = t.programme_id "
        f" WHERE p.site_id = %s{state_clause} "
        f" ORDER BY f.created_at DESC",
        tuple(params),
    ).fetchall()


def set_state(conn, flag_id, state) -> dict | None:
    """resolved_at is stamped only on 'resolved'. Acknowledged means the PM
    has seen it, not that the slip is fixed — collapsing the two would lose
    the distinction between 'someone is on it' and 'it is dealt with'."""
    if state not in _STATES:
        raise ValueError(f"state must be one of {_STATES}")
    resolved = ", resolved_at = now()" if state == "resolved" else ""
    return conn.cursor(row_factory=dict_row).execute(
        f"UPDATE programme_delay_flags SET state = %s{resolved} "
        f"WHERE id = %s RETURNING {_COLS}",
        (state, flag_id),
    ).fetchone()
