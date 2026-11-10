"""Pick cards (migration 0099, cross-session status reconcile r0).

A pick card is one spoken status statement ("that one's done") that could refer to 2-4 items.
Nothing here writes action_items.status: a person picks the item (or none), and the pick goes
through the normal audited path. `candidates` is a jsonb array of stable_id strings."""
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

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
