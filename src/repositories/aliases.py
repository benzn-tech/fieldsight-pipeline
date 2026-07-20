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
