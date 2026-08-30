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


def evidence_for_session(conn, company_id, session_base) -> list:
    """One row per GROUP: what it was built on. For a reader to show, not for code to gate.

    `for_session` answers "which group is this segment in". This answers "how much is that
    group standing on", which is the question somebody asks when a grouping looks wrong — and
    until it reaches the API the only person who can ask it is whoever has SQL access.

    Deliberately not used to refuse anything. Measured on the first multi-speaker session,
    **both cross-call merges contained a member built on a single turn**, and the centroid
    that stood alone was the one with the most evidence. A minimum-evidence rule would have
    refused both merges and left the session as unusable as before: a label with one turn in a
    call is precisely the case where the per-call namespace tells you nothing.
    """
    _require_company(company_id)
    if not session_base:
        return []
    rows = conn.cursor(row_factory=dict_row).execute(
        "SELECT group_label, count(*) AS labels, sum(turns) AS turns, "
        "       sum(seconds) AS seconds, max(spread) AS worst_spread "
        "FROM speaker_label_groups WHERE company_id = %s AND session_base = %s "
        "GROUP BY group_label ORDER BY group_label",
        (company_id, session_base),
    ).fetchall()
    return [{"group": r["group_label"],
             "labels": int(r["labels"] or 0),
             # NULL stays None rather than becoming 0. Rows written before 0052 genuinely do
             # not know, and a zero would claim "no evidence" about groups that had some.
             "turns": int(r["turns"]) if r["turns"] is not None else None,
             "seconds": round(float(r["seconds"]), 1) if r["seconds"] is not None else None,
             "worstSpread": (round(float(r["worst_spread"]), 3)
                             if r["worst_spread"] is not None else None)}
            for r in rows]
