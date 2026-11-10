"""Decision ledger: what was decided, by whom, on what authority (migration 0100).

APPEND-ONLY (a trigger rejects UPDATE, DELETE and TRUNCATE). Changing one's mind is a new row
whose supersedes_entry_id points at the row it replaces."""
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

_COLS = ("entry_id, company_id, site_id, card_id, subject_ref, decision, kind, decided_by, "
         "authority_basis, decided_at, evidence, supersedes_entry_id, source")


def decided(conn, company_id, site_id, subject_ref):
    """The latest 'decided' or 'declined' row for this subject that no other row supersedes,
    or None. 'defaulted' rows never count as a decision.

    Supersession is by pointer only (no decided_at comparison), and only a person's new row
    can supersede: a row of kind 'decided'/'declined' of the SAME company and site. A
    'defaulted' row's supersedes_entry_id is ignored (a default is never a change of mind),
    and so is a pointer from another tenant."""
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM decision_ledger l "
        "WHERE l.company_id = %s AND l.site_id = %s AND l.subject_ref = %s "
        "AND l.kind IN ('decided', 'declined') "
        "AND NOT EXISTS (SELECT 1 FROM decision_ledger n "
        "                WHERE n.supersedes_entry_id = l.entry_id "
        "                  AND n.kind IN ('decided', 'declined') "
        "                  AND n.company_id = l.company_id AND n.site_id = l.site_id) "
        "ORDER BY l.decided_at DESC, l.entry_id DESC LIMIT 1",
        (company_id, site_id, subject_ref)).fetchone()


def append(conn, row):
    """Insert one ledger row; decided_at defaults to now() when not given."""
    return conn.cursor(row_factory=dict_row).execute(
        "INSERT INTO decision_ledger (company_id, site_id, card_id, subject_ref, decision, kind, "
        "decided_by, authority_basis, decided_at, evidence, supersedes_entry_id, source) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,COALESCE(%s, now()),%s,%s,%s) "
        f"RETURNING {_COLS}",
        (row["company_id"], row["site_id"], row.get("card_id"), row["subject_ref"],
         row["decision"], row["kind"], row.get("decided_by"), row.get("authority_basis"),
         row.get("decided_at"), Jsonb(row.get("evidence") or []),
         row.get("supersedes_entry_id"), row["source"])).fetchone()
