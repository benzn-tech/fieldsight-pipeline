"""Pick cards (migration 0099, cross-session status reconcile r0).

A pick card is one spoken status statement ("that one's done") that could refer to 2-4 items.
Nothing here writes action_items.status: a person picks the item (or none), and the pick goes
through the normal audited path. `candidates` is a jsonb array of stable_id strings."""
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from status_candidates import canon_uuid
from tenancy_status import not_migrated

_COLS = ("id, company_id, site_id, suggested_status, quote, segment_key, offset_sec, "
         "candidates, run, state, chosen_stable_id, resolved_by, resolved_at, created_at")


def insert_pick_card(conn, company_id, site_id, suggested_status, quote, segment_key,
                     offset_sec, candidates, run):
    """Idempotent on (segment_key, offset_sec, suggested_status). Returns the new row, or None
    when that anchor already has a card of this status in ANY state -- so a resolved card is
    never re-raised. `candidates` must hold 2-4 stable_ids (a CHECK enforces it); a bare list
    does not bind as jsonb, hence Jsonb()."""
    return conn.cursor(row_factory=dict_row).execute(
        "INSERT INTO status_pick_cards (company_id, site_id, suggested_status, quote, "
        "segment_key, offset_sec, candidates, run) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (segment_key, offset_sec, suggested_status) DO NOTHING "
        f"RETURNING {_COLS}",
        (company_id, site_id, suggested_status, quote, segment_key, offset_sec,
         Jsonb([str(c) for c in candidates]), run),
    ).fetchone()


def get_pick_card(conn, card_id):
    """The card, or None for a malformed id (no query), an unknown id, or a missing table
    (migration 0099 not applied yet). Savepoint-guarded: only `not_migrated` is swallowed;
    any other error is raised after the savepoint rolled back."""
    cid = canon_uuid(card_id)
    if cid is None:
        return None
    try:
        with conn.transaction():
            return conn.cursor(row_factory=dict_row).execute(
                f"SELECT {_COLS} FROM status_pick_cards WHERE id = %s", (cid,),
            ).fetchone()
    except psycopg.Error as exc:
        if not_migrated(exc):
            return None
        raise


def pending_for_sites(conn, site_ids):
    """Pending cards on any of `site_ids`, newest first. [] with no query for empty input,
    and [] when the table is missing. Savepoint-guarded like get_pick_card."""
    ids = [str(s) for s in (site_ids or []) if s]
    if not ids:
        return []
    try:
        with conn.transaction():
            return conn.cursor(row_factory=dict_row).execute(
                f"SELECT {_COLS} FROM status_pick_cards "
                "WHERE state = 'pending' AND site_id = ANY(%s::uuid[]) "
                "ORDER BY created_at DESC, id DESC",
                (ids,),
            ).fetchall()
    except psycopg.Error as exc:
        if not_migrated(exc):
            return []
        raise


def resolve_pick_card(conn, card_id, state, chosen_stable_id, user_id):
    """Compare-and-set: decide a card only while it is still pending. Returns the updated
    row, or None when it was already decided (or the id is unknown/malformed) -- the caller
    answers 409/404 and, inside its transaction, rolls back anything it wrote first.
    `state` is 'chosen' (with `chosen_stable_id`) or 'none' (with None); a CHECK enforces
    the state vocabulary. Not savepoint-guarded: the caller already read the card."""
    cid = canon_uuid(card_id)
    if cid is None:
        return None
    return conn.cursor(row_factory=dict_row).execute(
        "UPDATE status_pick_cards SET state = %s, chosen_stable_id = %s, "
        "resolved_by = %s, resolved_at = now() "
        "WHERE id = %s AND state = 'pending' "
        f"RETURNING {_COLS}",
        (state, chosen_stable_id, user_id, cid),
    ).fetchone()
