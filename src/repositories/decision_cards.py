"""Decision cards: the question put to people (migration 0099). P0 is shadow: status stays
'open' unless superseded, and a platform admin labels each card; labelling never changes
status. One OPEN card per card_key (partial unique index), so a superseded card frees its key.

Every function is scoped by company_id and site_id except list_shadow, shadow_summary_rows
(platform-admin reads across every company) and set_label (platform-admin write by card id)."""
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

_COLS = ("card_id, event_id, company_id, site_id, rule_id, subject_ref, purpose, card_key, "
         "kind, question, consequence, options, evidence, urgency, deadline_at, "
         "timeout_default, recipients, version, delivered_refs, status, shadow_label, "
         "labelled_by, labelled_at, label_note, created_at")
_C_COLS = ", ".join(f"c.{c.strip()}" for c in _COLS.split(","))


def insert_open(conn, card):
    """Insert an open card as given (nothing computed here). None when an open card with that
    card_key already exists."""
    return conn.cursor(row_factory=dict_row).execute(
        "INSERT INTO decision_cards (card_key, rule_id, purpose, subject_ref, kind, question, "
        "consequence, options, evidence, urgency, deadline_at, timeout_default, recipients, "
        "event_id, company_id, site_id) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (card_key) WHERE status = 'open' DO NOTHING "
        f"RETURNING {_COLS}",
        (card["card_key"], card["rule_id"], card["purpose"], card["subject_ref"], card["kind"],
         card["question"], card.get("consequence"), Jsonb(card.get("options") or []),
         Jsonb(card.get("evidence") or []), card.get("urgency"), card.get("deadline_at"),
         card.get("timeout_default"), Jsonb(card.get("recipients") or []),
         card.get("event_id"), card["company_id"], card["site_id"])).fetchone()


def find_open(conn, company_id, site_id, card_key):
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM decision_cards "
        "WHERE company_id = %s AND site_id = %s AND card_key = %s AND status = 'open'",
        (company_id, site_id, card_key)).fetchone()


def _like_prefix(prefix):
    """LIKE pattern matching `prefix` literally (backslash is Postgres's default escape)."""
    esc = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return esc + "%"


def open_for_subject_prefix(conn, company_id, site_id, rule_id, prefix):
    """Open cards of this tenant and rule whose subject_ref starts with `prefix` (literal: LIKE
    wildcards in the prefix are escaped), oldest first."""
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM decision_cards "
        "WHERE company_id = %s AND site_id = %s AND rule_id = %s AND status = 'open' "
        "AND subject_ref LIKE %s ORDER BY created_at, card_id",
        (company_id, site_id, rule_id, _like_prefix(prefix))).fetchall()


def supersede(conn, company_id, site_id, card_key):
    """open -> superseded for this tenant's open card with that key. Returns rows touched."""
    return conn.execute(
        "UPDATE decision_cards SET status = 'superseded' "
        "WHERE company_id = %s AND site_id = %s AND card_key = %s AND status = 'open'",
        (company_id, site_id, card_key)).rowcount


def bump(conn, card_id, company_id, site_id, consequence):
    """version + 1 with a new consequence. None for another tenant's card."""
    return conn.cursor(row_factory=dict_row).execute(
        "UPDATE decision_cards SET version = version + 1, consequence = %s "
        f"WHERE card_id = %s AND company_id = %s AND site_id = %s RETURNING {_COLS}",
        (consequence, card_id, company_id, site_id)).fetchone()


def list_shadow(conn, limit, offset, label=None):
    """Platform-admin read: cards of every company, newest first, with company_name and
    site_name. `label` filters on shadow_label."""
    where, params = "", []
    if label is not None:
        where, params = "WHERE c.shadow_label = %s ", [label]
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_C_COLS}, co.name AS company_name, s.name AS site_name "
        "FROM decision_cards c JOIN companies co ON co.id = c.company_id "
        "JOIN sites s ON s.id = c.site_id "
        f"{where}ORDER BY c.created_at DESC, c.card_id DESC LIMIT %s OFFSET %s",
        (*params, limit, offset)).fetchall()


def shadow_summary_rows(conn):
    """Platform-admin read: (shadow_label, kind, recipients, created_at) for every card, for a
    summary computed in Python."""
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT shadow_label, kind, recipients, created_at FROM decision_cards "
        "ORDER BY created_at").fetchall()


def set_label(conn, card_id, label, note, user_id):
    """Platform-admin shadow label. Never touches status. None for an unknown card."""
    return conn.cursor(row_factory=dict_row).execute(
        "UPDATE decision_cards SET shadow_label = %s, label_note = %s, labelled_by = %s, "
        f"labelled_at = now() WHERE card_id = %s RETURNING {_COLS}",
        (label, note, user_id, card_id)).fetchone()
