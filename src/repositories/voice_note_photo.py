"""voice_note_photo.py — photos pinned to a phone voice note (migration 0089).

A voice note is its own meeting_session (session_type='voice_note'); its /open
carries the clientUuids of the photos it claims. One row per (user, photo): a
photo belongs to at most one note of its own user.

The LATEST note wins, judged by the notes' `opened_at` — never by which /open
reached us last. /open is best-effort and retried, so a retried /open of an
older note routinely arrives after a newer note's; ordering by arrival would let
it steal the photo back. A current row whose note has no `opened_at` loses to
any claimant (it carries no evidence of being later); a claimant with no
`opened_at` never displaces a current row (NULL >= x is not true).

`photo_client_uuid` is stored exactly as sent: it must equal the photo's
`recordings.client_uuid`, which is itself stored as sent (no case folding).

All statements go through conn.cursor(row_factory=dict_row), matching every
other repository here.
"""
from psycopg.rows import dict_row


def link(conn, user_id, session_id, photo_client_uuids) -> int:
    """Claim `photo_client_uuids` for the note `session_id`. Returns the number of
    rows inserted or moved (a re-claim by the note that already holds a photo
    counts as moved — it refreshes `linked_at`). The note's meeting_session row
    must already exist (call after ensure_open)."""
    # De-duplicated: one INSERT ... ON CONFLICT DO UPDATE may not touch the same
    # row twice, and a repeated uuid in one body is the phone's slip, not a 500.
    uuids = list(dict.fromkeys(photo_client_uuids or ()))
    if not uuids:
        return 0
    cur = conn.cursor(row_factory=dict_row)
    cur.execute(
        "INSERT INTO voice_note_photo (user_id, photo_client_uuid, session_id) "
        "SELECT %s, p, %s FROM unnest(%s::text[]) AS p "
        "ON CONFLICT (user_id, photo_client_uuid) DO UPDATE SET "
        "session_id = EXCLUDED.session_id, linked_at = now() "
        "WHERE (SELECT opened_at FROM meeting_session cur "
        "        WHERE cur.session_id = voice_note_photo.session_id) IS NULL "
        "   OR (SELECT opened_at FROM meeting_session new "
        "        WHERE new.session_id = EXCLUDED.session_id) "
        "   >= (SELECT opened_at FROM meeting_session cur "
        "        WHERE cur.session_id = voice_note_photo.session_id)",
        (user_id, session_id, uuids),
    )
    return cur.rowcount
