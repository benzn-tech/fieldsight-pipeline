"""Which recent voice clusters sound like a given enrolled person.

A `speaker_label_groups` row (0059/0065) is one diarisation label within one transcript
call -- a voice cluster, not a turn. `centroid` (0065) is the unit-normalised mean of that
cluster's turn embeddings, kept from the re-bind that already computes it rather than
re-reading and re-embedding the audio. This module answers the question that column exists
to make cheap: "of the clusters from the last N hours, which ones sound like this person",
so `speaker_name_proposals.propose` has something to offer.

**NULL centroid means "not cached", never "no voice here"** (0065's own comment). A row
written before the migration, or one the re-bind declined to store a vector for, looks
identical to an absent cluster if this silently drops it -- which is exactly the "empty
list means no filter" shape this codebase has paid for once already (see
`speaker_name_proposals.py`'s own callout, and `repositories/voiceprints.py`'s "this
codebase has twice let [] / None mean both 'no filter' and 'nothing'"). So the uncached
count travels back to the caller instead of disappearing into a shorter list.

**There is deliberately no admission threshold in this file**, for the same reason
`speaker_name_proposals` has none: candidates are ranked and cut at `limit`, a screen-size
choice, not a similarity floor. A constant here deciding whether a candidate "is" a match
would be the exact cut `0066_speaker_name_proposals.sql` refuses to invent, arriving
through a different file.
"""
from psycopg.rows import dict_row


def _require_company(company_id):
    # Same guard as every other repository in this package (voiceprints.py,
    # speaker_name_proposals.py). A query that reaches Postgres without a company scope is
    # not a bug that surfaces as an error -- it is a query that quietly matches one
    # company's voice against another's clusters.
    if not company_id:
        raise ValueError("company_id is required — an absent one would match a voice "
                         "against every company's clusters at once")


def candidates_for_person(conn, company_id, voiceprint_id, since_hours=72,
                          site_id=None, limit=5) -> dict:
    """Voice clusters from the last `since_hours` that most resemble `voiceprint_id`.

    Returns `{"candidates": [...], "uncached": <int>}`, best-scoring candidate first. Each
    candidate is `{"session_base", "source_filename", "speaker_label", "score"}` -- the same
    three-part cluster address `speaker_name_proposals` keys on, plus the cosine similarity
    it was ranked by.

    **Why a dict and not a bare list.** `centroid IS NOT NULL` is applied in SQL, silently
    at the row level -- a cluster with no cached centroid cannot be scored and must not be
    counted as "not similar", which is a different fact than "not yet computed". `uncached`
    is how many rows in this company's window were skipped for exactly that reason, so a
    caller (or the person reading a UI built on this) can tell "no candidates because there
    is nothing here" from "no candidates because the re-bind hasn't run over this window
    yet" -- the same "≠" this codebase keeps re-discovering when two functions collapse into
    one number (see CLAUDE.md's `voiceprints.py`-adjacent notes on that class of defect).

    **Scoring is MEAN, not max, over the person's live samples** -- the pooling this repo
    switched to deliberately (`voiceprints.py`'s `MEAN_POOLING_CUTOVER`; a single loud
    outlier sample must not let one lucky comparison speak for a whole profile). Only
    samples with `quarantined_at IS NULL` count: a quarantined sample already failed the
    "sounds like this person's own core" test (`requarantine_profile`) and letting it anchor
    a NEW candidate search would let a strayed vector go on vouching for strangers after
    it stopped being trusted to vouch for its own profile.

    **The person must be matchable, not merely exist.** Mirrors `profiles_for_matching`
    exactly: `p.company_id = company_id` (never another tenant's profile), `p.status <>
    'withdrawn'` (a withdrawal that still anchors new candidate hunts is not a withdrawal),
    and `p.consent_at IS NOT NULL` (a voiceprint is biometric information under the NZ
    Privacy Act -- consent is a precondition on using the vectors, not just on creating the
    profile, per `upsert_profile`'s docstring). A profile failing any of those has no live,
    consented samples in the join below, so it silently produces zero candidates rather than
    a wrong answer -- the same "refusal over a wrong confident answer" asymmetry
    `EnrolmentBelongsToSomebodyElse` is built around.

    **pgvector's `<=>` is cosine DISTANCE, not similarity** -- `similarity = 1 - distance`,
    applied explicitly below and never left as the raw operator, because ordering by `<=>`
    ASC and then presenting the raw number as "how alike" reads backwards to anyone who
    has to debug it later.

    **`since_hours` is passed as a bound parameter multiplied onto an INTERVAL literal**,
    never interpolated into the query text -- string-building `f"'{since_hours} hours'"`
    would be the same class of injection risk this codebase avoids everywhere else SQL is
    built (adjacent string literals, %s placeholders throughout this package).

    **Already-asked clusters are excluded, in every state.** `speaker_name_proposals`'s own
    header is explicit that pending, confirmed AND rejected all mean "already asked" --
    especially rejected: "a human said 'this is not that person' -- information, never
    proposed again" -- so a `NOT EXISTS` against that table with no `state` filter is the
    correct exclusion, not a bug that happens to look like one.

    **`site_id` is accepted and still NOT applied, but the reason has changed and shrunk.**
    0068 gave this table `user_folder` and `session_date`, which is exactly what
    `recordings.site_for_day` keys on -- so the join the earlier version of this docstring
    called impossible is now possible. It is deliberately not made here, because
    `site_for_day` answers "which site did this FOLDER work at on this DATE", one lookup per
    (folder, date) pair, and doing that inside this query means either a correlated subquery
    per candidate row or a second round trip. At the measured volume -- 0 to 19 clusters in
    a 72 hour window, worst case ~54 -- neither is worth building before anyone has asked
    the question with a site in hand.

    So this is now a deferred join rather than a missing column, and the next person has
    everything they need. `site_id=None` and any other value still behave identically.
    Narrowing to nothing is never acceptable; returning the whole window is the safe side.

    """
    _require_company(company_id)
    if not voiceprint_id:
        # Mirrors `pending_for_person`: nobody to propose to, so there is nothing to rank
        # against. `uncached` is still meaningful without a person, but a person-less call
        # is not a real caller shape -- return the same empty shape rather than half-answer.
        return {"candidates": [], "uncached": 0}

    cur = conn.cursor(row_factory=dict_row)

    # `site_id` is intentionally unused in the query below -- see the docstring's "accepted
    # but NOT applied" section. Named here (rather than silently ignored) so a reader
    # scanning the function body, not just the docstring, sees that the parameter was
    # considered and not forgotten.
    del site_id

    uncached_row = cur.execute(
        "SELECT count(*) AS n FROM speaker_label_groups "
        "WHERE company_id = %s AND centroid IS NULL "
        "  AND created_at >= now() - (%s * interval '1 hour') ",
        (company_id, since_hours)).fetchone()
    uncached = int((uncached_row or {}).get("n") or 0)

    rows = cur.execute(
        "WITH person AS ( "
        "  SELECT s.embedding "
        "  FROM speaker_voiceprint_samples s "
        "  JOIN speaker_voiceprints p ON p.id = s.voiceprint_id "
        "  WHERE s.company_id = %s AND s.voiceprint_id = %s "
        "    AND s.quarantined_at IS NULL "
        "    AND p.company_id = %s "
        "    AND p.status <> 'withdrawn' "
        "    AND p.consent_at IS NOT NULL "
        ") "
        "SELECT g.session_base, g.source_filename, g.speaker_label, "
        "       g.user_folder, g.session_date, "
        # 1 - distance, NOT the raw `<=>` operator -- pgvector's `<=>` is cosine distance,
        # and presenting it unconverted would rank correctly (ORDER BY still needs the
        # transform to point the right way) but read backwards to every caller downstream.
        "       AVG(1 - (g.centroid <=> person.embedding)) AS score "
        "FROM speaker_label_groups g "
        "CROSS JOIN person "
        "WHERE g.company_id = %s "
        "  AND g.centroid IS NOT NULL "
        "  AND g.created_at >= now() - (%s * interval '1 hour') "
        "  AND NOT EXISTS ( "
        "        SELECT 1 FROM speaker_name_proposals np "
        "        WHERE np.company_id = %s AND np.voiceprint_id = %s "
        "          AND np.session_base = g.session_base "
        "          AND np.source_filename = g.source_filename "
        "          AND np.speaker_label = g.speaker_label "
        # No `state` filter here on purpose: pending, confirmed AND rejected all mean
        # "already asked" (0066's own header). A rejected candidate must not resurface --
        # that is a human's "not this person", not an open question.
        "      ) "
        # Every non-aggregated column in the SELECT must appear here or Postgres rejects
        # the statement -- and a connection double never parses SQL, so this is
        # invisible to the unit suite and shows up as a 500 in production. That has
        # happened here twice.
        "GROUP BY g.session_base, g.source_filename, g.speaker_label, "
        "         g.user_folder, g.session_date "
        "ORDER BY score DESC NULLS LAST "
        "LIMIT %s",
        (company_id, str(voiceprint_id), company_id,
         company_id, since_hours,
         company_id, str(voiceprint_id),
         int(limit))).fetchall()

    candidates = [
        {"session_base": r["session_base"], "source_filename": r["source_filename"],
         "speaker_label": r["speaker_label"], "score": float(r["score"]),
         # Required NOT NULL by `speaker_name_proposals`, because answering a proposal goes
         # through the correction path and that addresses a session as (folder, date,
         # session_base). Before 0068 this table did not carry them and a candidate could
         # not be turned into a proposal at all.
         "user_folder": r["user_folder"], "session_date": r["session_date"]}
        for r in rows
    ]
    return {"candidates": candidates, "uncached": uncached}
