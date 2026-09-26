"""Candidate passages waiting for a human to say yes or no.

The queue exists because of one number. `recompute_company_floor` needs 20 rows of
`source='correction'` before a company has a rejection floor, and one rename gesture
produces exactly ONE such row -- everything it propagates to is `correction_propagation`,
which the calibration deliberately excludes. Twenty separate human decisions, not twenty
named passages. At one per rename that is months; five judgements at a time makes it a
sitting.

So every function here is in service of that: a proposal is a question whose ANSWER is the
product. The names it happens to fix along the way are a side effect.
"""
from psycopg.rows import dict_row


def _require_company(company_id):
    # Same guard as every other repository in this package. A query that reaches Postgres
    # without a company is not a bug that shows up as an error; it is a query that returns
    # another tenant's rows.
    if not company_id:
        raise ValueError("company_id is required — this table is read per company")


def propose(conn, company_id, voiceprint_id, candidates) -> int:
    """Offer these passages as possibly this person. Returns how many are newly asked.

    **`ON CONFLICT DO NOTHING` is the whole design of this function**, and it is carrying
    the rule that is easiest to get wrong: re-running candidate retrieval must not re-ask a
    question somebody already answered. A candidate the user REJECTED will be recomputed
    every time -- rejection does not change what the audio sounds like -- and without the
    conflict clause each recomputation would quietly reset it to `pending` and put it back
    in front of them. Seeing something you already rejected come back reads as the system
    not having listened, which is worse than never asking.

    It is a constraint rather than a `WHERE NOT EXISTS` on purpose: the rule then holds for
    every writer this table ever gets, including ones written by somebody who has not read
    this docstring.

    Returns the count of rows actually inserted, NOT of candidates offered. "Five candidates
    considered, none new" and "five new questions" are different facts and the caller
    decides whether to open a popup on the strength of one of them.
    """
    _require_company(company_id)
    if not voiceprint_id:
        raise ValueError("voiceprint_id is required — a proposal with nobody to propose is "
                         "a row no interface can render and no human can answer")
    cur = conn.cursor(row_factory=dict_row)
    inserted = 0
    for c in candidates or []:
        if not c.get("session_base") or not c.get("source_filename") \
                or not c.get("speaker_label"):
            # Half a cluster address is not a cluster. The unique constraint would reject it
            # anyway; dropping it here keeps one malformed candidate from aborting the rest
            # inside a background lambda.
            continue
        row = cur.execute(
            "INSERT INTO speaker_name_proposals "
            "(company_id, voiceprint_id, session_base, source_filename, speaker_label, "
            " user_folder, session_date, score) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (company_id, voiceprint_id, session_base, source_filename, "
            "             speaker_label) DO NOTHING "
            "RETURNING id",
            (company_id, str(voiceprint_id), c["session_base"], c["source_filename"],
             c["speaker_label"], c["user_folder"], c["session_date"],
             c.get("score"))).fetchone()
        if row:
            inserted += 1
    return inserted


def pending_for_person(conn, company_id, voiceprint_id, limit=5) -> list:
    """This person's unanswered questions, best first.

    Ranked by score, cut at `limit`. **Neither is an admission decision** -- ranking cannot
    be wrong and `limit` is a screen-size choice. There is deliberately no threshold here
    and none belongs here: a number deciding whether a candidate "is" a match would be the
    absolute cut this system has refused to invent, arriving through a side door.

    Keyed on the PERSON, not on the gesture that produced the proposals. That is what lets
    the popup re-open later with everything still outstanding for them, rather than with
    whatever one earlier rename happened to turn up.

    `NULLS LAST` because a proposal with no score sorts ahead of every real one under
    `DESC` in Postgres, which would put the least-evidenced candidate at the top of a list
    whose whole purpose is to put the best one there.
    """
    _require_company(company_id)
    if not voiceprint_id:
        return []
    return cur_rows(conn.cursor(row_factory=dict_row).execute(
        "SELECT id::text, session_base, source_filename, speaker_label, "
        "       user_folder, session_date, score, created_at "
        "FROM speaker_name_proposals "
        "WHERE company_id = %s AND voiceprint_id = %s AND state = 'pending' "
        "ORDER BY score DESC NULLS LAST, created_at DESC "
        "LIMIT %s",
        (company_id, str(voiceprint_id), int(limit))))


def pending_count(conn, company_id) -> int:
    """How many questions this company has not answered — the bell's number.

    Worth more than a badge. Pending that only grows means the corrections that calibrate a
    company's floor are not being made, and a company that never calibrates a floor never
    gets a confirmed name out of the matcher at all (see `decide_name`). So this count
    rising without bound is not a UI annoyance; it is the mechanism failing, reported.
    """
    _require_company(company_id)
    row = conn.cursor(row_factory=dict_row).execute(
        "SELECT count(*) AS n FROM speaker_name_proposals "
        "WHERE company_id = %s AND state = 'pending'", (company_id,)).fetchone()
    return int((row or {}).get("n") or 0)


def pending_by_person(conn, company_id) -> list:
    """Unanswered questions grouped by WHOSE voice they are about — what the bell reads.

    Grouped, not listed, and that is the whole point of this function existing beside
    `pending_for_person`. A bell that said "14 clips to check" describes a chore. "Three
    voices to confirm" describes a decision somebody can picture finishing, and the unit a
    person actually thinks in is the person, not the passage.

    It joins `speaker_voiceprints` for the name because a uuid is not something a bell can
    say, and it filters on that join rather than trusting the proposal row: a profile
    withdrawn after its proposals were queued must stop being asked about, and the row
    itself has no way to know that happened.

    `newest` rides along so the bell can order by what turned up most recently rather than
    by name — the questions a person has not seen yet are the ones worth putting first.
    """
    _require_company(company_id)
    return cur_rows(conn.cursor(row_factory=dict_row).execute(
        "SELECT p.voiceprint_id::text AS voiceprint_id, v.display_name, "
        "       count(*) AS pending, max(p.created_at) AS newest "
        "FROM speaker_name_proposals p "
        "JOIN speaker_voiceprints v ON v.id = p.voiceprint_id "
        "WHERE p.company_id = %s AND p.state = 'pending' "
        "  AND v.company_id = %s AND v.status <> 'withdrawn' "
        "GROUP BY p.voiceprint_id, v.display_name "
        "ORDER BY max(p.created_at) DESC",
        (company_id, company_id)))


def decide(conn, company_id, proposal_id, state, decided_by=None) -> dict | None:
    """Record an answer. Returns the row, or None if it was not this company's to answer.

    Only `confirmed` and `rejected` reach here. **Closing the popup is not a decision and
    must never call this** -- the row stays `pending` and the bell shows it later. A
    dismissal that consumed the candidate would silently burn human decisions the company's
    floor is calibrated from, five at a time, on a mis-click.

    The `state = 'pending'` predicate makes this idempotent against a double-click and stops
    a later answer overwriting an earlier one.
    """
    _require_company(company_id)
    if state not in ("confirmed", "rejected"):
        raise ValueError(
            f"state must be 'confirmed' or 'rejected', not {state!r} — dismissal is the "
            f"absence of a decision and is recorded by leaving the row alone")
    return conn.cursor(row_factory=dict_row).execute(
        "UPDATE speaker_name_proposals "
        "SET state = %s, decided_at = now(), decided_by = %s "
        "WHERE company_id = %s AND id = %s AND state = 'pending' "
        "RETURNING id::text, voiceprint_id::text, session_base, source_filename, "
        "          speaker_label, user_folder, session_date, state",
        (state, str(decided_by) if decided_by else None,
         company_id, str(proposal_id))).fetchone()


def cur_rows(cur) -> list:
    return [dict(r) for r in cur.fetchall()]
