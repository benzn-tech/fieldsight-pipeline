from psycopg.rows import dict_row

_COLS = "id, company_id, name, location, client, industry, icon_s3_key, created_at"


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


def list_company_sites(conn, company_id) -> list[dict]:
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM sites WHERE company_id=%s ORDER BY created_at", (company_id,)
    ).fetchall()


def list_sites_by_ids(conn, site_ids) -> list[dict]:
    if not site_ids:
        return []
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM sites WHERE id = ANY(%s) ORDER BY created_at", (list(site_ids),)
    ).fetchall()


def get_site_by_name(conn, company_id, name) -> dict | None:
    """Org seed idempotency: sites have no unique-name constraint, so seed
    re-runs look up by (company, name) before inserting."""
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM sites WHERE company_id=%s AND name=%s", (company_id, name)
    ).fetchone()


def set_icon_key(conn, site_id, icon_s3_key) -> dict | None:
    return conn.cursor(row_factory=dict_row).execute(
        f"UPDATE sites SET icon_s3_key=%s WHERE id=%s RETURNING {_COLS}",
        (icon_s3_key, site_id),
    ).fetchone()
