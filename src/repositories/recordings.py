from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from repositories import sites
from repositories.topics import _escape_like

_COLS = ("id, company_id, user_id, site_id, kind, s3_key, client_uuid, started_at, "
         "ended_at, duration_s, resolution, codec, size_bytes, gps_track, uploaded_at, created_at")


def insert_pending(conn, company_id, user_id, site_id, kind, s3_key, client_uuid,
                   started_at, ended_at=None, duration_s=None, resolution=None,
                   codec=None, size_bytes=None) -> dict:
    return conn.cursor(row_factory=dict_row).execute(
        f"INSERT INTO recordings (company_id, user_id, site_id, kind, s3_key, client_uuid, "
        f"started_at, ended_at, duration_s, resolution, codec, size_bytes) "
        f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING {_COLS}",
        (company_id, user_id, site_id, kind, s3_key, client_uuid,
         started_at, ended_at, duration_s, resolution, codec, size_bytes),
    ).fetchone()


def get_by_client_uuid(conn, user_id, client_uuid) -> dict | None:
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM recordings WHERE user_id=%s AND client_uuid=%s",
        (user_id, client_uuid),
    ).fetchone()


def get_by_id(conn, rec_id) -> dict | None:
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM recordings WHERE id=%s", (rec_id,)
    ).fetchone()


def mark_uploaded(conn, rec_id, company_id, size_bytes=None, gps_track=None) -> dict | None:
    return conn.cursor(row_factory=dict_row).execute(
        f"UPDATE recordings SET uploaded_at=now(), "
        f"size_bytes=COALESCE(%s, size_bytes), "
        f"gps_track=COALESCE(%s, gps_track) "
        f"WHERE id=%s AND company_id=%s RETURNING {_COLS}",
        (size_bytes, Jsonb(gps_track) if gps_track is not None else None, rec_id, company_id),
    ).fetchone()


def duration_for_media(conn, company_id, user_folder, date, session_base) -> float | None:
    """Recorded DURATION in seconds for the media file an extraction session
    came from, or None when there is no matching recordings row. Same
    session_base LIKE match + company scoping as site_for_media below (kept as
    its own query so the two callers stay independent).

    Deliberately returns a duration, NOT an absolute end instant:
    recordings.started_at/ended_at are timestamptz (UTC), while a session's
    start is the NZ device wall clock encoded in session_base. Mixing the two
    would label a 13:05 meeting as ending at 01:22 (BUG-37's family). The
    caller adds this duration to the session_base start, so everything stays
    on ONE clock. Prefers the explicitly reported duration_s; falls back to
    the ended_at - started_at delta (a difference of two timestamptz values is
    timezone-safe). Non-positive/degenerate values are treated as absent."""
    row = conn.cursor(row_factory=dict_row).execute(
        "SELECT duration_s, started_at, ended_at FROM recordings "
        "WHERE company_id = %s AND s3_key LIKE %s ESCAPE '\\' "
        "ORDER BY created_at DESC LIMIT 1",
        (company_id, f"users/{_escape_like(user_folder)}/%/{date}/{_escape_like(session_base)}.%"),
    ).fetchone()
    if row is None:
        return None
    if row.get("duration_s") is not None and float(row["duration_s"]) > 0:
        return float(row["duration_s"])
    started, ended = row.get("started_at"), row.get("ended_at")
    if started is not None and ended is not None:
        delta = (ended - started).total_seconds()
        if delta > 0:
            return delta
    return None


def site_for_media(conn, company_id, user_folder, date, session_base) -> dict | None:
    """The app-tagged site (recordings.site_id) for the recording whose media
    file this extraction session came from, or None. Matches recordings.s3_key
    by session_base within users/{folder}/.../{date}/ (LIKE, wildcard-escaped),
    scoped to company_id, and only returns a site that is itself in-company
    (multi-tenant invariant — never attribute across tenants). Newest matching
    recording wins. Returns a sites.get_site()-shaped row so it drops in where
    resolve_site's return is used (lambda_item_writer)."""
    pattern = f"users/{_escape_like(user_folder)}/%/{date}/{_escape_like(session_base)}.%"
    row = conn.cursor(row_factory=dict_row).execute(
        "SELECT r.site_id FROM recordings r JOIN sites s ON s.id = r.site_id "
        "WHERE r.company_id = %s AND s.company_id = %s AND r.site_id IS NOT NULL "
        "AND r.s3_key LIKE %s ESCAPE '\\' "
        "ORDER BY r.created_at DESC LIMIT 1",
        (company_id, company_id, pattern),
    ).fetchone()
    if row is None:
        return None
    return sites.get_site(conn, row["site_id"])
