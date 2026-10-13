"""Self-introductions waiting for a human to say "yes, that's a name" or "no".

Sibling of `speaker_name_proposals.py`, deliberately not a rename of it: that table
asks "is this <existing person>?" against an already-enrolled `voiceprint_id`; this one
asks "who is this NEW voice?" about a passage nothing has ever matched, and there is no
profile to point at yet (see `0071_speaker_intro_suggestions.sql`'s header). Every
function here follows the same rules `speaker_name_proposals.py` states for the same
reasons: `ON CONFLICT DO NOTHING` is the whole design of `store` (re-running the writer
over the same extraction must not re-ask a question already asked), and closing the
dialog is not a decision -- only `decide` may move a row out of `pending`.
"""
import re

from psycopg.rows import dict_row

import turn_name_overlay


def _require_company(company_id):
    # Same guard as every other repository in this package. A query that reaches
    # Postgres without a company is not a bug that shows up as an error; it is a query
    # that returns another tenant's rows.
    if not company_id:
        raise ValueError("company_id is required — this table is read per company")


def store(conn, company_id, session_base, user_folder, session_date, intros) -> dict:
    """Insert one pending row per new introduction. Returns counts, never the rows --
    the caller (the writer) logs the counts and does nothing else with the result.

    Two reasons an introduction is skipped rather than stored, each counted separately
    because they answer different questions about a run that inserted zero:

    - **`skipped_no_sid`**: `source_filename` carries no `sid<32hex>`
      (`turn_name_overlay.session_base`). A legacy whole-file or VAD-segment recording has
      no session id to confirm against later -- `speaker_corrections` refuses one
      (`lambda_org_api.py`'s `session id must carry its sid` check) -- so a row that could
      never be confirmed is not offered in the first place.
    - **`skipped_named`**: the FILE this introduction came from already has a live
      (`superseded_at IS NULL`) name in `speaker_turn_names`. Judged per file, not per
      cluster, because that table has no `speaker_label` to narrow by -- the same
      over-suppression `label_group_candidates.candidates_for_person` accepts for the
      same reason (spec correction 4). Asking "is this Sam?" about a passage the
      transcript already calls Sam is a question with no information in it.

    `ON CONFLICT DO NOTHING` on the unique key is what makes a re-run of the same
    extraction (a re-extraction, the finalize sweep's re-run chain, org-api's
    regenerate) insert nothing the second time -- never a `WHERE NOT EXISTS` in Python,
    for the reason `speaker_name_proposals.propose`'s docstring gives: the rule then
    holds for every writer this table ever gets.
    """
    _require_company(company_id)
    result = {"inserted": 0, "skipped_named": 0, "skipped_no_sid": 0}
    if not intros:
        return result

    cur = conn.cursor(row_factory=dict_row)
    already_named_cache = {}
    for intro in intros:
        src = intro.get("source_filename")
        label = intro.get("speaker_label")
        if not src or not label:
            # Half an address is not an address -- the unique constraint would reject it
            # anyway; dropping it here keeps one malformed intro from aborting the rest.
            continue

        if turn_name_overlay.session_base(src) is None:
            result["skipped_no_sid"] += 1
            continue

        if src not in already_named_cache:
            row = cur.execute(
                "SELECT EXISTS ( "
                "  SELECT 1 FROM speaker_turn_names n "
                "  WHERE n.company_id = %s AND n.session_base = %s "
                "    AND n.superseded_at IS NULL "
                "    AND split_part(n.turn_ref, '@', 1) "
                "        = regexp_replace(%s, '[.]json$', '') "
                ") AS already_named",
                (company_id, session_base, src)).fetchone()
            already_named_cache[src] = bool(row["already_named"])

        if already_named_cache[src]:
            result["skipped_named"] += 1
            continue

        inserted = cur.execute(
            "INSERT INTO speaker_intro_suggestions "
            "(company_id, session_base, source_filename, speaker_label, user_folder, "
            " session_date, start_sec, end_sec, heard_name, company_name, quote) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (company_id, session_base, source_filename, speaker_label) "
            "DO NOTHING RETURNING id",
            (company_id, session_base, src, label, user_folder, session_date,
             intro.get("start_sec"), intro.get("end_sec"), intro.get("heard_name"),
             intro.get("company_name"), intro.get("quote"))).fetchone()
        if inserted:
            result["inserted"] += 1

    return result


def pending(conn, company_id, limit=20) -> list:
    """Unanswered introductions, newest first. NO transcript read -- every field the
    dialog needs (`heard_name`, `quote`, the audio key's ingredients) is already on the
    row, which is the entire reason this table carries the words `speaker_name_proposals`
    does not."""
    _require_company(company_id)
    cur = conn.cursor(row_factory=dict_row)
    return [dict(r) for r in cur.execute(
        "SELECT id::text, heard_name, company_name, quote, "
        "       session_date::text AS session_date, user_folder, session_base, "
        "       source_filename, speaker_label, start_sec, end_sec, created_at "
        "FROM speaker_intro_suggestions "
        "WHERE company_id = %s AND state = 'pending' "
        "ORDER BY created_at DESC "
        "LIMIT %s",
        (company_id, int(limit))).fetchall()]


def pending_count(conn, company_id) -> int:
    """The bell's number -- see `speaker_name_proposals.pending_count` for why this
    count, not the badge's shape, is the one worth alarming on."""
    _require_company(company_id)
    row = conn.cursor(row_factory=dict_row).execute(
        "SELECT count(*) AS n FROM speaker_intro_suggestions "
        "WHERE company_id = %s AND state = 'pending'", (company_id,)).fetchone()
    return int((row or {}).get("n") or 0)


def decide(conn, company_id, suggestion_id, state, decided_by=None) -> dict | None:
    """Record an answer. Returns the row, or None if it was not this company's or was
    already decided.

    Only `confirmed` and `rejected` reach here, same rule as `speaker_name_proposals.decide`
    for the same reason: closing the dialog is not a decision and must leave the row
    `pending`. `WHERE state = 'pending'` makes this idempotent against a double-click and
    stops a later answer overwriting an earlier one.
    """
    _require_company(company_id)
    if state not in ("confirmed", "rejected"):
        raise ValueError(
            f"state must be 'confirmed' or 'rejected', not {state!r} — dismissal is the "
            f"absence of a decision and is recorded by leaving the row alone")
    return conn.cursor(row_factory=dict_row).execute(
        "UPDATE speaker_intro_suggestions "
        "SET state = %s, decided_at = now(), decided_by = %s "
        "WHERE company_id = %s AND id = %s AND state = 'pending' "
        "RETURNING id::text, session_base, source_filename, speaker_label, "
        "          user_folder, session_date::text AS session_date, "
        "          heard_name, company_name, start_sec, end_sec, quote, state",
        (state, str(decided_by) if decided_by else None,
         company_id, str(suggestion_id))).fetchone()
