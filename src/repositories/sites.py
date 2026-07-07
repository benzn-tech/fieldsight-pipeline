from psycopg.rows import dict_row

_COLS = "id, company_id, name, location, client, industry, icon_s3_key, created_at, archived_at, slug"


def create_site(conn, company_id, name, location=None, client=None,
                industry=None, icon_s3_key=None, slug=None) -> dict:
    return conn.cursor(row_factory=dict_row).execute(
        f"INSERT INTO sites (company_id, name, location, client, industry, icon_s3_key, slug) "
        f"VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING {_COLS}",
        (company_id, name, location, client, industry, icon_s3_key, slug),
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


def get_company_site_by_slug(conn, company_id, slug) -> dict | None:
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM sites WHERE company_id=%s AND slug=%s",
        (company_id, slug),
    ).fetchone()


def archive_site(conn, site_id, company_id) -> dict | None:
    """Soft-delete a site (company-guarded) and cascade-archive its
    memberships. Returns None if not found / wrong company / already archived."""
    cur = conn.cursor(row_factory=dict_row)
    row = cur.execute(
        f"UPDATE sites SET archived_at=now() "
        f"WHERE id=%s AND company_id=%s AND archived_at IS NULL RETURNING {_COLS}",
        (site_id, company_id),
    ).fetchone()
    if row is None:
        return None
    cur.execute(
        "UPDATE memberships SET archived_at=now() "
        "WHERE site_id=%s AND archived_at IS NULL", (site_id,))
    return row


def unarchive_site(conn, site_id, company_id) -> dict | None:
    """Restore a site row only (memberships are NOT auto-restored — re-add
    people explicitly, which revives via ensure_membership)."""
    return conn.cursor(row_factory=dict_row).execute(
        f"UPDATE sites SET archived_at=NULL "
        f"WHERE id=%s AND company_id=%s AND archived_at IS NOT NULL RETURNING {_COLS}",
        (site_id, company_id),
    ).fetchone()


def set_site_icon(conn, site_id, icon_s3_key) -> dict:
    return conn.cursor(row_factory=dict_row).execute(
        f"UPDATE sites SET icon_s3_key=%s WHERE id=%s RETURNING {_COLS}",
        (icon_s3_key, site_id),
    ).fetchone()


def update_site(conn, site_id, company_id, name=None, location=None,
                client=None, industry=None) -> dict | None:
    """None = leave unchanged (same semantics as users.update_profile).
    Company-guarded; archived sites are not editable."""
    return conn.cursor(row_factory=dict_row).execute(
        f"UPDATE sites SET "
        f"  name=COALESCE(%(name)s, name), "
        f"  location=COALESCE(%(loc)s, location), "
        f"  client=COALESCE(%(client)s, client), "
        f"  industry=COALESCE(%(ind)s, industry) "
        f"WHERE id=%(sid)s AND company_id=%(cid)s AND archived_at IS NULL "
        f"RETURNING {_COLS}",
        {"sid": site_id, "cid": company_id, "name": name, "loc": location,
         "client": client, "ind": industry},
    ).fetchone()
