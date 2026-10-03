# src/repositories/aliases.py
"""Repository for the name_aliases glossary store (migration 0020, spec §5.4).
Style mirrors repositories/observations.py. list_active feeds text_normalize.
normalize(); create_alias is written by the D2 glossary-confirm endpoint."""
from psycopg.rows import dict_row

_COLS = ("id, company_id, site_id, wrong_term, right_term, kind, source, "
         "status, created_by, created_at")


def list_active(conn, company_id, site_ids=None):
    """Active aliases for the company. Site-scoped rows first (site_id NULLS
    LAST puts company-wide last), so a caller feeding these to normalize()
    applies the more specific site alias before the company-wide one (spec §7
    scope precedence). site_ids optionally narrows the site-scoped rows to the
    caller's reach; company-wide rows (site_id IS NULL) are always included."""
    if site_ids is not None:
        rows = conn.cursor(row_factory=dict_row).execute(
            f"SELECT {_COLS} FROM name_aliases "
            f"WHERE company_id=%s AND status='active' "
            f"AND (site_id IS NULL OR site_id = ANY(%s::uuid[])) "
            f"ORDER BY site_id NULLS LAST, created_at",
            (company_id, list(site_ids)),
        ).fetchall()
    else:
        rows = conn.cursor(row_factory=dict_row).execute(
            f"SELECT {_COLS} FROM name_aliases "
            f"WHERE company_id=%s AND status='active' "
            f"ORDER BY site_id NULLS LAST, created_at",
            (company_id,),
        ).fetchall()
    return rows


def create_alias(conn, company_id, site_id, wrong_term, right_term, kind,
                 created_by, source="correction"):
    """Insert one alias (D5) and return it. site_id None = company-wide."""
    return conn.cursor(row_factory=dict_row).execute(
        f"INSERT INTO name_aliases (company_id, site_id, wrong_term, "
        f"right_term, kind, source, created_by) "
        f"VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING {_COLS}",
        (company_id, site_id, wrong_term, right_term, kind, source, created_by),
    ).fetchone()


def learn_alias(conn, company_id, site_id, wrong_term, right_term, created_by):
    """Record a correction someone made as a glossary entry (source 'learned').

    One active entry per (company, site, wrong term), compared without case:
    the same correction again is a no-op, and a different correction of the
    same word replaces the old one -- the latest person to fix it is right.
    Returns the active row, or None when nothing changed.
    """
    cur = conn.cursor(row_factory=dict_row)
    # Putting a correction back (TEKAHA -> Tikaha) is an UNDO, not a new fact:
    # learning it would leave two entries rewriting each other. The entry it
    # reverses is retired and nothing is learned.
    reversed_ = cur.execute(
        "UPDATE name_aliases SET status='retired' WHERE company_id=%s "
        "AND site_id IS NOT DISTINCT FROM %s AND lower(wrong_term)=lower(%s) "
        "AND lower(right_term)=lower(%s) AND status='active'",
        (company_id, site_id, right_term, wrong_term)).rowcount
    if reversed_:
        return None
    existing = cur.execute(
        f"SELECT {_COLS} FROM name_aliases WHERE company_id=%s "
        f"AND site_id IS NOT DISTINCT FROM %s AND lower(wrong_term)=lower(%s) "
        f"AND status='active'",
        (company_id, site_id, wrong_term)).fetchall()
    if any(r["right_term"] == right_term for r in existing):
        return None
    for r in existing:
        cur.execute("UPDATE name_aliases SET status='retired' WHERE id=%s", (r["id"],))
    return create_alias(conn, company_id, site_id, wrong_term, right_term, "other",
                        created_by, source="learned")


def list_for_company(conn, company_id):
    """Every active glossary entry of a company, newest first, with the site's
    name and who made it -- the admin list (view and undo)."""
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT a.id, a.site_id, s.name AS site_name, a.wrong_term, a.right_term, "
        "a.kind, a.source, a.created_at, "
        "trim(coalesce(u.first_name,'') || ' ' || coalesce(u.last_name,'')) AS created_by_name "
        "FROM name_aliases a LEFT JOIN sites s ON s.id = a.site_id "
        "LEFT JOIN users u ON u.id = a.created_by "
        "WHERE a.company_id=%s AND a.status='active' ORDER BY a.created_at DESC",
        (company_id,)).fetchall()


def retire(conn, company_id, alias_id):
    """Undo one entry. Company-pinned: an id from another company retires
    nothing. Returns True when a row changed."""
    return conn.execute(
        "UPDATE name_aliases SET status='retired' "
        "WHERE id=%s AND company_id=%s AND status='active'",
        (alias_id, company_id)).rowcount == 1
