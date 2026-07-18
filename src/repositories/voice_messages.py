"""voice_messages writes/reads (Site voice delivery pointer). Metadata only —
NO transcript/content column (off-the-record invariant). The caller owns the
transaction (see db.connection.get_connection) — these NEVER commit."""
from psycopg.rows import dict_row

_COLS = "id, company_id, site_id, sender_user_id, s3_key, duration_s, created_at"


def insert_message(conn, company_id, site_id, sender_user_id, s3_key,
                   duration_s=None) -> dict:
    return conn.cursor(row_factory=dict_row).execute(
        f"INSERT INTO voice_messages (company_id, site_id, sender_user_id, s3_key, duration_s) "
        f"VALUES (%s, %s, %s, %s, %s) RETURNING {_COLS}",
        (company_id, site_id, sender_user_id, s3_key, duration_s),
    ).fetchone()


def list_since(conn, company_id, site_id, since) -> list[dict]:
    """Recent messages for reconnect backfill: everything on this site created
    strictly after `since` (ISO string or datetime). Company- and site-pinned;
    chronological (oldest first) for ordered replay."""
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM voice_messages "
        f"WHERE company_id=%s AND site_id=%s::uuid AND created_at > %s "
        f"ORDER BY created_at",
        (company_id, site_id, since),
    ).fetchall()


def prune_older_than(conn, cutoff) -> int:
    """Scheduled retention prune: drop rows older than cutoff (30-day parity
    with the voice/ S3 lifecycle). cutoff is a tz-aware datetime."""
    return conn.cursor().execute(
        "DELETE FROM voice_messages WHERE created_at < %s", (cutoff,)
    ).rowcount
