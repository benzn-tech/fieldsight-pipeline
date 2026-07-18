"""Live WebSocket connection registry (Site voice). The caller owns the
transaction (see db.connection.get_connection) — these NEVER commit."""
from psycopg.rows import dict_row


def upsert_connection(conn, connection_id, user_id, company_id) -> None:
    """Register (or refresh) a live connection. Idempotent on connection_id —
    API Gateway ids are unique per connection, so this only ever refreshes."""
    conn.cursor().execute(
        "INSERT INTO ws_connections (connection_id, user_id, company_id) "
        "VALUES (%s, %s, %s) "
        "ON CONFLICT (connection_id) DO UPDATE SET "
        "user_id=EXCLUDED.user_id, company_id=EXCLUDED.company_id, connected_at=now()",
        (connection_id, user_id, company_id),
    )
    return None


def delete_connection(conn, connection_id) -> None:
    conn.cursor().execute(
        "DELETE FROM ws_connections WHERE connection_id=%s", (connection_id,))
    return None


def delete_connections(conn, connection_ids) -> int:
    """Bulk-delete gone connections (fanout GoneException reap). Empty in ->
    0 out, no round-trip."""
    if not connection_ids:
        return 0
    return conn.cursor().execute(
        "DELETE FROM ws_connections WHERE connection_id = ANY(%s)",
        (list(connection_ids),),
    ).rowcount


def delete_stale(conn, older_than) -> int:
    """Scheduled sweep: drop connections whose connected_at is older than the
    cutoff — a dead connection that never fired $disconnect. older_than is a
    timezone-aware datetime."""
    return conn.cursor().execute(
        "DELETE FROM ws_connections WHERE connected_at < %s", (older_than,)
    ).rowcount


def recipients_for_site(conn, company_id, site_id, exclude_user_id) -> list[str]:
    """Connection ids of every ONLINE member of site_id EXCEPT the sender.
    Joins live connections to non-archived memberships on the site; company-
    pinned on the connection row (multi-tenant invariant). DISTINCT guards
    against duplicate join rows; each of a member's devices is its own
    connection_id, so all their devices receive the message."""
    rows = conn.cursor(row_factory=dict_row).execute(
        "SELECT DISTINCT wc.connection_id "
        "FROM ws_connections wc "
        "JOIN memberships m ON m.user_id = wc.user_id "
        "WHERE m.site_id = %s::uuid AND wc.company_id = %s "
        "AND wc.user_id <> %s AND m.archived_at IS NULL",
        (site_id, company_id, exclude_user_id),
    ).fetchall()
    return [r["connection_id"] for r in rows]
