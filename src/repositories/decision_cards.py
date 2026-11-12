"""Decision cards: the question put to people (migration 0100). P0 is shadow: status stays
'open' unless superseded, and a platform admin labels each card; labelling never changes
status. One OPEN card per card_key (partial unique index), so a superseded card frees its key.

Every function is scoped by company_id and site_id except list_shadow, get_shadow,
evidence_sources, overdue_item_states, shadow_summary_rows (platform-admin reads across
every company), set_label (platform-admin write by card id), and the inbox reads inbox_rows
(by allow-listed companies + recipient) and get_for_answer (by card id; the caller checks
visibility). P1 (migration 0103): the inbox's answer() moves open -> answered."""
import re

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from deleted_predicates import visible_topics_predicate
from repositories.action_items import item_shown_predicate

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


def open_for_subject_prefix(conn, company_id, site_id, rule_id, prefix, report_id=None):
    """Open cards of this tenant and rule whose subject_ref starts with `prefix` (literal: LIKE
    wildcards in the prefix are escaped), oldest first.

    `report_id` (checklist.gap): only cards whose originating event (decision_cards.event_id,
    same tenant) carries payload report_id = that id -- so one checklist report never closes
    another report's cards. A card with no event never matches when report_id is given."""
    if report_id is None:
        return conn.cursor(row_factory=dict_row).execute(
            f"SELECT {_COLS} FROM decision_cards "
            "WHERE company_id = %s AND site_id = %s AND rule_id = %s AND status = 'open' "
            "AND subject_ref LIKE %s ORDER BY created_at, card_id",
            (company_id, site_id, rule_id, _like_prefix(prefix))).fetchall()
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_C_COLS} FROM decision_cards c "
        "JOIN site_events e ON e.event_id = c.event_id "
        "  AND e.company_id = c.company_id AND e.site_id = c.site_id "
        "WHERE c.company_id = %s AND c.site_id = %s AND c.rule_id = %s "
        "AND c.status = 'open' AND c.subject_ref LIKE %s "
        "AND e.payload->>'report_id' = %s ORDER BY c.created_at, c.card_id",
        (company_id, site_id, rule_id, _like_prefix(prefix), str(report_id))).fetchall()


def supersede(conn, company_id, site_id, card_key):
    """open -> superseded for this tenant's open card with that key. Returns rows touched."""
    return conn.execute(
        "UPDATE decision_cards SET status = 'superseded' "
        "WHERE company_id = %s AND site_id = %s AND card_key = %s AND status = 'open'",
        (company_id, site_id, card_key)).rowcount


def bump(conn, card_id, company_id, site_id, consequence):
    """version + 1 with a new consequence. None for another tenant's card. Intentionally NO
    status filter: the hub bumps the fault card by id whatever its status."""
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


def get_shadow(conn, card_id):
    """Platform-admin read: one card in list_shadow's shape (with company_name and
    site_name), or None."""
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_C_COLS}, co.name AS company_name, s.name AS site_name "
        "FROM decision_cards c JOIN companies co ON co.id = c.company_id "
        "JOIN sites s ON s.id = c.site_id WHERE c.card_id = %s",
        (card_id,)).fetchone()


def evidence_sources(conn, card_ids):
    """Platform-admin read, ONE query for a page of cards: where each card's evidence came
    from -- card -> originating event (same tenant) -> the checklist report its payload
    names. Rows of (card_id, user_folder, report_date, session, walk_key, walk_parts); a
    card with no event, or whose event names no existing report, has no row. The report id
    is compared as text, so a malformed payload value matches nothing instead of aborting
    the transaction.

    A multi-unit walk report (walk_key set, migration 0096) draws on several recordings:
    walk_parts lists every inspection window of that walk in the report's folder (the key
    already names the day; scoped by folder as inspection_windows.walk_rows is), as
    [{user_folder, report_date, session}], [] when none resolves. NULL walk_key -> []."""
    if not card_ids:
        return []
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT c.card_id, cr.user_folder, cr.report_date, cr.session, cr.walk_key, "
        "  CASE WHEN cr.walk_key IS NULL THEN '[]'::jsonb ELSE COALESCE(("
        "    SELECT jsonb_agg(jsonb_build_object('user_folder', w.user_folder, "
        "      'report_date', w.report_date::text, 'session', w.session) "
        "      ORDER BY w.start_at, w.session) "
        "    FROM inspection_windows w "
        "    WHERE w.walk_key = cr.walk_key AND w.user_folder = cr.user_folder"
        "  ), '[]'::jsonb) END AS walk_parts "
        "FROM decision_cards c "
        "JOIN site_events e ON e.event_id = c.event_id "
        "  AND e.company_id = c.company_id AND e.site_id = c.site_id "
        "JOIN checklist_reports cr ON cr.id::text = e.payload->>'report_id' "
        "WHERE c.card_id = ANY(%s::uuid[]) ORDER BY c.created_at, c.card_id",
        ([str(i) for i in card_ids],)).fetchall()


_UUID_RE = "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"


def overdue_item_states(conn, card_ids):
    """Platform-admin read, ONE query for a page of todo.overdue cards: rows of (card_id,
    deadline, visible). `deadline` is the originating event's payload deadline (text, None
    without an event); `visible` is whether the action item the card names (subject_ref =
    its stable_id) may still be shown: its NEWEST row passing visible_topics_predicate (not
    deleted by topic or recording, not superseded -- the row get_live_by_stable_id picks)
    ON THE CARD'S OWN SITE, whose site belongs to the card's company, also passes
    action_items.item_shown_predicate (not non_work, no active redaction of any scope) --
    the same rule overdue_open applies before emitting. An item with that stable_id under
    another site or company never makes the card look live (an item that moved site reads
    as removed: fail closed).
    A subject_ref that is not a uuid matches nothing (guarded before the cast, so it never
    aborts the transaction) and reads as not visible. Every card id given gets a row."""
    if not card_ids:
        return []
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT c.card_id, e.payload->>'deadline' AS deadline, "
        "  COALESCE(li.shown, false) AS visible "
        "FROM decision_cards c "
        "LEFT JOIN site_events e ON e.event_id = c.event_id "
        "  AND e.company_id = c.company_id AND e.site_id = c.site_id "
        "LEFT JOIN LATERAL ("
        f"  SELECT ({item_shown_predicate('t')}) AS shown "
        "  FROM action_items a JOIN topics t ON t.id = a.topic_id "
        "  JOIN sites s ON s.id = a.site_id "
        "  WHERE a.site_id = c.site_id AND s.company_id = c.company_id "
        "  AND a.stable_id = (CASE WHEN c.subject_ref ~ %s "
        "                       THEN c.subject_ref::uuid END) "
        f"  AND {visible_topics_predicate('t')} "
        "  ORDER BY a.created_at DESC LIMIT 1"
        ") li ON true "
        "WHERE c.card_id = ANY(%s::uuid[]) ORDER BY c.created_at, c.card_id",
        (_UUID_RE, [str(i) for i in card_ids])).fetchall()


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


# --- decision-card inbox (spec 2026-11-12-decision-card-inbox-design rev 2 §4, §5) ---------
# Scoped by the caller's allow-listed companies and recipient membership (inbox_rows), by
# card id for the POST's visibility check (get_for_answer), and by tenant for the write
# (answer). The event payload is joined on the SAME tenant, so another tenant's event never
# lends a card its text.

_ANSWER_COLS = "c.answered_by, c.answered_option, c.answered_at, c.answer_reason"
_INBOX_SELECT = (
    f"SELECT {_C_COLS}, {_ANSWER_COLS}, s.name AS site_name, e.payload AS payload "
    "FROM decision_cards c JOIN sites s ON s.id = c.site_id "
    "LEFT JOIN site_events e ON e.event_id = c.event_id "
    "  AND e.company_id = c.company_id AND e.site_id = c.site_id ")


_UUID_PAT = re.compile(_UUID_RE)


def _uuids(values):
    return [str(v) for v in values if v is not None and _UUID_PAT.match(str(v))]


def inbox_rows(conn, company_ids, user_id):
    """Open cards of these companies whose recipients contain `user_id`, with site_name and
    the originating event's payload (None without one) and the answer columns. Includes
    todo.overdue_backlog and fault rows: the caller splits/excludes. A company id that is not
    a uuid is dropped (never cast), so a malformed allow-list entry matches nothing."""
    ids = _uuids(company_ids)
    if not ids or not _uuids([user_id]):
        return []
    return conn.cursor(row_factory=dict_row).execute(
        _INBOX_SELECT
        + "WHERE c.company_id = ANY(%s::uuid[]) AND c.status = 'open' "
        "AND c.recipients @> %s::jsonb ORDER BY c.created_at, c.card_id",
        (ids, Jsonb([{"user_id": str(user_id)}]))).fetchall()


def get_for_answer(conn, card_id):
    """One card in inbox_rows' shape (any status), or None; None for a card id that is not a
    uuid. Visibility is decided by the caller."""
    if not _uuids([card_id]):
        return None
    return conn.cursor(row_factory=dict_row).execute(
        _INBOX_SELECT + "WHERE c.card_id = %s::uuid", (str(card_id),)).fetchone()


def answer(conn, card_id, company_id, site_id, user_id, option_id, reason):
    """First answer wins: open -> answered with who/what/when/why, for this tenant's card.
    The row (with the answer columns), or None when no open card matched (already answered,
    superseded, or another tenant's)."""
    return conn.cursor(row_factory=dict_row).execute(
        "UPDATE decision_cards SET status = 'answered', answered_by = %s, answered_option = %s, "
        "answered_at = now(), answer_reason = %s "
        "WHERE card_id = %s AND company_id = %s AND site_id = %s AND status = 'open' "
        f"RETURNING {_COLS}, answered_by, answered_option, answered_at, answer_reason",
        (user_id, option_id, reason, card_id, company_id, site_id)).fetchone()
