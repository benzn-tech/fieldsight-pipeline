"""Which per-call speaker labels are the same voice, within one session.

Batching numbers speakers per ASR call, so `spk_0` in call 3 and `spk_0` in call 9 are two
different people about half the time. These rows say which ones agree. They are letters, not
names: nothing here identifies anybody and no vector is stored.

Read at display time and never baked into the transcript — see migration 0051 for why.
"""
from psycopg.rows import dict_row


def _require_company(company_id):
    """Every read and write here is company-scoped, and an empty company is not 'all'.

    The same guard the voiceprint repository carries, for the same reason: a falsy company id
    reaching a `WHERE company_id = %s` binds NULL, matches nothing, and looks like "this
    session has no groups" — while the same slip in a delete would match nothing and silently
    leave the previous generation in place.
    """
    if not company_id:
        raise ValueError("company_id is required: an unscoped speaker-group query is either "
                         "empty or another tenant's")


def replace_for_session(conn, company_id, session_base, rows) -> int:
    """The session's groups, replacing whatever was there. Returns how many were written.

    **Delete-then-insert, in the caller's transaction.** A session can be re-finalized, and the
    letters are assigned by clustering — a second run may legitimately call the same voice `B`
    where the first called it `A`. Two generations left side by side would give one transcript
    two contradictory groupings with no way to tell which is current.

    An empty `rows` still deletes. "The re-bind ran and found nothing to group" and "the old
    groups are still there" must not be the same state; the first is honest and the second is a
    transcript displaying a mapping nobody computed.
    """
    _require_company(company_id)
    if not session_base:
        raise ValueError("session_base is required")
    cur = conn.cursor(row_factory=dict_row)
    cur.execute("DELETE FROM speaker_label_groups "
                "WHERE company_id = %s AND session_base = %s",
                (company_id, session_base))
    written = 0
    for r in rows or []:
        # A row missing either half of its key is dropped rather than stored: the primary key
        # would reject it anyway, and one bad row must not abort the whole session's mapping
        # inside a background lambda.
        if not r.get("source_filename") or not r.get("speaker_label"):
            continue
        cur.execute(
            "INSERT INTO speaker_label_groups "
            "(company_id, session_base, source_filename, speaker_label, group_label, "
            " spread, turns, seconds) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (company_id, session_base, r["source_filename"], r["speaker_label"],
             r["group_label"], r.get("spread"), r.get("turns"), r.get("seconds")))
        written += 1
    return written


def for_session(conn, company_id, session_base) -> dict:
    """`{(source_filename, speaker_label): group_label}` for one session, or `{}`.

    A dict rather than rows because the caller does one lookup per transcript segment, and a
    list would turn a page render into a linear scan per segment.
    """
    _require_company(company_id)
    if not session_base:
        return {}
    rows = conn.cursor(row_factory=dict_row).execute(
        "SELECT source_filename, speaker_label, group_label FROM speaker_label_groups "
        "WHERE company_id = %s AND session_base = %s",
        (company_id, session_base),
    ).fetchall()
    return {(r["source_filename"], r["speaker_label"]): r["group_label"] for r in rows}
