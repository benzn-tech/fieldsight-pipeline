from psycopg.rows import dict_row

_COLS = "id, company_id, name, location, client, industry, icon_s3_key, created_at, archived_at"


def create_site(conn, company_id, name, location=None, client=None,
                industry=None, icon_s3_key=None) -> dict:
    return conn.cursor(row_factory=dict_row).execute(
        f"INSERT INTO sites (company_id, name, location, client, industry, icon_s3_key) "
        f"VALUES (%s, %s, %s, %s, %s, %s) RETURNING {_COLS}",
        (company_id, name, location, client, industry, icon_s3_key),
    ).fetchone()


def get_site(conn, site_id) -> dict | None:
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM sites WHERE id=%s", (site_id,)
    ).fetchone()


def list_company_sites(conn, company_id, include_archived=False) -> list[dict]:
    guard = "" if include_archived else "AND archived_at IS NULL "
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM sites WHERE company_id=%s {guard}ORDER BY created_at", (company_id,)
    ).fetchall()


def list_sites_by_ids(conn, site_ids) -> list[dict]:
    if not site_ids:
        return []  # ANY('{}') is valid SQL but skip the round-trip
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM sites WHERE id = ANY(%s) AND archived_at IS NULL ORDER BY created_at",
        (list(site_ids),),
    ).fetchall()


def get_company_site_by_name(conn, company_id, name) -> dict | None:
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM sites WHERE company_id=%s AND name=%s",
        (company_id, name),
    ).fetchone()
