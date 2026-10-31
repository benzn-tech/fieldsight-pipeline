"""SQL for company status changes and their Cognito work rows (company offboarding,
spec docs/superpowers/specs/2026-11-01-company-offboarding-design.md §4, §5.3, §5.4, §8).

Used by org-api's POST /companies/{id}/status and GET /companies/{id}/status-events. The
caller runs every function here inside one `conn.transaction()` and maps a missing
table/column (migration 0092 not applied yet) to 503; nothing here swallows errors.

company_status_cognito rows are the work list of the async executor (Task 8):
  * active -> suspended/exited: one 'disable' and one 'signout' row, state 'pending', per
    user whose HOME company (users.company_id) is the company -- archived users included
    (archiving never touched Cognito).
  * inactive -> inactive: no rows (the first deactivation already listed the users).
  * inactive -> active: still-pending disable/signout rows of the events since the company
    was last active become 'failed' / SUPERSEDED_BY_REACTIVATION, and one 'enable' row is
    created per user that has a 'done' disable row in those events (the union, §5.3) --
    a user we never actually disabled is never enabled.
  * active -> inactive also fails still-pending 'enable' rows of earlier events
    (SUPERSEDED_BY_DEACTIVATION), so a slow enable can never land after the new disable.
"""
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

SUPERSEDED_BY_REACTIVATION = "superseded by reactivation"
SUPERSEDED_BY_DEACTIVATION = "superseded by deactivation"

_STATUS_COLS = "status, status_changed_at, status_reason, billing_stopped_at, exited_at"


def lock_company(conn, company_id) -> dict | None:
    """The company's status fields, row-locked (FOR UPDATE) until the transaction ends.
    Touches both audit tables first, so an unmigrated database fails here -- before any
    write -- with UndefinedTable / UndefinedColumn."""
    conn.execute("SELECT 1 FROM company_status_events WHERE false")
    conn.execute("SELECT 1 FROM company_status_cognito WHERE false")
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT id, {_STATUS_COLS} FROM companies WHERE id = %s FOR UPDATE",
        (company_id,)).fetchone()


def read_status(conn, company_id) -> dict | None:
    """The company's status fields (no lock)."""
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT id, {_STATUS_COLS} FROM companies WHERE id = %s", (company_id,)).fetchone()


def apply_status(conn, company_id, to_status, reason, stop_billing) -> dict:
    """Set the status. exited: exited_at and billing_stopped_at are set unless already set
    (exited_at is the FIRST exit). suspended: billing_stopped_at likewise, only when
    stop_billing. active: billing_stopped_at cleared (billing resumes), exited_at kept."""
    return conn.cursor(row_factory=dict_row).execute(
        "UPDATE companies SET status = %(s)s, status_changed_at = now(), "
        "  status_reason = %(r)s, "
        "  exited_at = CASE WHEN %(s)s::text = 'exited' "
        "    THEN COALESCE(exited_at, now()) ELSE exited_at END, "
        "  billing_stopped_at = CASE "
        "    WHEN %(s)s::text = 'exited' OR (%(s)s::text = 'suspended' AND %(sb)s::boolean) "
        "      THEN COALESCE(billing_stopped_at, now()) "
        "    WHEN %(s)s::text = 'active' THEN NULL "
        "    ELSE billing_stopped_at END "
        f"WHERE id = %(cid)s RETURNING {_STATUS_COLS}",
        {"s": to_status, "r": reason, "sb": bool(stop_billing), "cid": company_id},
    ).fetchone()


def insert_event(conn, company_id, from_status, to_status, reason, actor_user_id,
                 actor_sub, detail):
    """Append one audit row; returns its id. `at` is clock_timestamp(), not now(), so
    events written in one transaction still order (the reactivation window depends on
    the order)."""
    return conn.execute(
        "INSERT INTO company_status_events "
        "(company_id, from_status, to_status, reason, actor_user_id, actor_sub, at, detail) "
        "VALUES (%s, %s, %s, %s, %s, %s, clock_timestamp(), %s) RETURNING id",
        (company_id, from_status, to_status, reason, actor_user_id, actor_sub,
         Jsonb(detail or {})),
    ).fetchone()[0]


def create_deactivation_rows(conn, event_id, company_id) -> dict:
    """'disable' + 'signout' pending rows for every home user of the company (archived
    included). Returns {"disable": n, "signout": n}."""
    rows = conn.execute(
        "INSERT INTO company_status_cognito (event_id, user_sub, action) "
        "SELECT %s::uuid, u.cognito_sub, a.action "
        "FROM users u CROSS JOIN (VALUES ('disable'), ('signout')) AS a(action) "
        "WHERE u.company_id = %s AND btrim(u.cognito_sub) <> '' "
        "ON CONFLICT (event_id, user_sub, action) DO NOTHING "
        "RETURNING action",
        (event_id, company_id)).fetchall()
    out = {"disable": 0, "signout": 0}
    for (action,) in rows:
        out[action] += 1
    return out


def supersede_pending_enables(conn, event_id, company_id) -> int:
    """Fail every still-pending 'enable' row of this company's earlier events."""
    return conn.execute(
        "UPDATE company_status_cognito c SET state = 'failed', last_error = %s, "
        "  updated_at = now() "
        "FROM company_status_events e "
        "WHERE e.id = c.event_id AND e.company_id = %s AND e.id <> %s "
        "  AND c.action = 'enable' AND c.state = 'pending'",
        (SUPERSEDED_BY_DEACTIVATION, company_id, event_id)).rowcount


# The events since the company was last active: after the most recent EARLIER event whose
# to_status is 'active' (all events when there is none), never the new event itself.
_SINCE_LAST_ACTIVE = (
    "SELECT e.id FROM company_status_events e "
    "WHERE e.company_id = %(cid)s AND e.id <> %(eid)s "
    "  AND e.at > COALESCE((SELECT max(x.at) FROM company_status_events x "
    "                       WHERE x.company_id = %(cid)s AND x.id <> %(eid)s "
    "                         AND x.to_status = 'active'), '-infinity'::timestamptz)")


def create_reactivation_rows(conn, event_id, company_id) -> dict:
    """Fail the still-pending disable/signout rows of the events since last active, then
    one pending 'enable' row per user with a 'done' disable in those events.
    Returns {"superseded": n, "enable": n}."""
    params = {"cid": company_id, "eid": event_id, "err": SUPERSEDED_BY_REACTIVATION}
    superseded = conn.execute(
        "UPDATE company_status_cognito SET state = 'failed', last_error = %(err)s, "
        "  updated_at = now() "
        f"WHERE event_id IN ({_SINCE_LAST_ACTIVE}) "
        "  AND action IN ('disable', 'signout') AND state = 'pending'",
        params).rowcount
    enabled = conn.execute(
        "INSERT INTO company_status_cognito (event_id, user_sub, action) "
        "SELECT DISTINCT %(eid)s::uuid, c.user_sub, 'enable' FROM company_status_cognito c "
        f"WHERE c.event_id IN ({_SINCE_LAST_ACTIVE}) "
        "  AND c.action = 'disable' AND c.state = 'done' "
        "ON CONFLICT (event_id, user_sub, action) DO NOTHING "
        "RETURNING user_sub",
        params).fetchall()
    return {"superseded": superseded, "enable": len(enabled)}


def home_user_count(conn, company_id) -> int:
    """Users whose home company this is (archived included) -- the people the status
    governs and the Cognito work covers."""
    return conn.execute(
        "SELECT count(*) FROM users WHERE company_id = %s AND btrim(cognito_sub) <> ''",
        (company_id,)).fetchone()[0]


def home_device_count(conn, company_id) -> int:
    """Devices (device ledger) whose last heartbeat account is a home user of the company."""
    return conn.execute(
        "SELECT count(*) FROM devices d JOIN users u ON u.cognito_sub = d.last_account_sub "
        "WHERE u.company_id = %s",
        (company_id,)).fetchone()[0]


def count_sockets(conn, company_id) -> int:
    """ws_connections rows of the company (company_id there is the connecting user's
    home company, lambda_ws_connect)."""
    return conn.execute(
        "SELECT count(*) FROM ws_connections WHERE company_id = %s",
        (company_id,)).fetchone()[0]


def close_sockets(conn, company_id) -> int:
    """Delete the company's ws_connections rows so no broadcast reaches them (§5.4)."""
    return conn.execute(
        "DELETE FROM ws_connections WHERE company_id = %s", (company_id,)).rowcount


def list_events(conn, company_id) -> list[dict]:
    """The company's audit trail, newest first, each with its Cognito rows (by user_sub,
    action) under "cognito"."""
    cur = conn.cursor(row_factory=dict_row)
    events = cur.execute(
        "SELECT id, from_status, to_status, reason, actor_user_id, actor_sub, at, detail "
        "FROM company_status_events WHERE company_id = %s ORDER BY at DESC, id DESC",
        (company_id,)).fetchall()
    if not events:
        return []
    rows = cur.execute(
        "SELECT event_id, user_sub, action, state, attempts, last_error, updated_at "
        "FROM company_status_cognito WHERE event_id = ANY(%s) "
        "ORDER BY user_sub, action",
        ([e["id"] for e in events],)).fetchall()
    by_event = {}
    for r in rows:
        by_event.setdefault(r.pop("event_id"), []).append(r)
    for e in events:
        e["cognito"] = by_event.get(e["id"], [])
    return events
