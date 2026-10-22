"""Photo pins: the photos the time-based day rebind must not move (M2 Task 6).

Two kinds of pin, both scoped to one (folder, day) -- the scope the rebind covers:

  * voice_note -- a phone voice note claimed the photo at /open
    (`voice_note_photo`). The photo sits under the note's own topic. The rows are
    DERIVED, never stored by topic id: every rebind deletes the day's
    `source='voice_note'` rows and rebuilds them from `voice_note_photo`, so a pin
    follows the live topic when item-writer supersedes the note's placeholder and
    writes a new one under the same source key.
  * human -- somebody moved the photo by hand on the website
    (`topic_photos.source='human'`). Written by the web line, never here; this
    module only keeps the matcher away from it.

The photo's folder and day come from the photo's OWN `recordings.s3_key`
(`users/{folder}/pictures/{date}/...`), joined on `recordings(user_id,
client_uuid)` = `voice_note_photo(user_id, photo_client_uuid)` -- never from the
session's folder -- and the note's topic must live under that same folder, so a
photo can only pin under a topic in its owner's folder.

SQL only, no S3. Every statement goes through conn.cursor(row_factory=dict_row).
"""
from psycopg.rows import dict_row

from deleted_predicates import visible_topics_predicate
from repositories.topics import _escape_like


def _lock(conn, user_folder, date):
    # The rebind's own day lock; re-entrant within one transaction, so taking it
    # here as well as in rebind_day_photos costs nothing and covers Task 7's
    # direct call from the claim step.
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                 (f"photobind:{user_folder}:{date}",))


def _picture_prefix(user_folder, date):
    return f"users/{_escape_like(user_folder)}/pictures/{_escape_like(date)}/%"


def apply_voice_note_pins(conn, user_folder, date) -> int:
    """Rebuild one (folder, day)'s `source='voice_note'` rows from `voice_note_photo`.

    A link pins only when its photo is uploaded (`uploaded_at IS NOT NULL`), lies
    under this day's picture prefix, and its note has a visible topic under this
    folder (newest `created_at` when several). Any `source='binding'` row for a
    pinned key is removed, so the photo is under one topic. Returns rows inserted.
    """
    _lock(conn, user_folder, date)
    prefix = _picture_prefix(user_folder, date)
    cur = conn.cursor(row_factory=dict_row)
    cur.execute(
        "DELETE FROM topic_photos WHERE source = 'voice_note' "
        "AND s3_key LIKE %s ESCAPE '\\'", (prefix,))
    inserted = cur.execute(
        "INSERT INTO topic_photos (topic_id, s3_key, source) "
        "SELECT topic_id, s3_key, 'voice_note' FROM ("
        "  SELECT DISTINCT ON (r.s3_key) r.s3_key, t.id AS topic_id "
        "  FROM voice_note_photo v "
        "  JOIN recordings r ON r.user_id = v.user_id "
        "   AND r.client_uuid = v.photo_client_uuid AND r.uploaded_at IS NOT NULL "
        "  JOIN topics t ON t.source_s3_key LIKE "
        "       %s || v.session_id || '.json' ESCAPE '\\' "
        f"  AND {visible_topics_predicate('t')} "
        "  WHERE r.s3_key LIKE %s ESCAPE '\\' "
        "  ORDER BY r.s3_key, t.created_at DESC, t.id DESC"
        ") pins RETURNING s3_key",
        (f"extractions/{_escape_like(user_folder)}/%/sid", prefix),
    ).fetchall()
    keys = [r["s3_key"] for r in inserted]
    if keys:
        cur.execute(
            "DELETE FROM topic_photos WHERE source = 'binding' AND s3_key = ANY(%s)",
            (keys,))
    return len(keys)


def pinned_photo_keys(conn, user_folder, date) -> set:
    """Keys under this day's picture prefix the matcher must not bind: every key a
    voice note has claimed (bound under its topic or not -- a claimed photo whose
    note has no topic yet stays unbound rather than drifting to a meeting), plus
    every key with a `source='human'` row on a visible topic."""
    prefix = _picture_prefix(user_folder, date)
    rows = conn.cursor(row_factory=dict_row).execute(
        "SELECT r.s3_key FROM voice_note_photo v "
        "JOIN recordings r ON r.user_id = v.user_id AND r.client_uuid = v.photo_client_uuid "
        "WHERE r.s3_key LIKE %s ESCAPE '\\' "
        "UNION "
        "SELECT tp.s3_key FROM topic_photos tp JOIN topics t ON t.id = tp.topic_id "
        "WHERE tp.source = 'human' AND tp.s3_key LIKE %s ESCAPE '\\' "
        f"AND {visible_topics_predicate('t')}",
        (prefix, prefix),
    ).fetchall()
    return {r["s3_key"] for r in rows}
