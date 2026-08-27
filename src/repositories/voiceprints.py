"""Speaker voiceprints: enrol a sample, offer profiles for matching, withdraw.

Design: docs/superpowers/specs/2026-08-09-speaker-identity-v2.md §6, §8
Schema: src/migrations/0038_speaker_voiceprints.sql

Live since 2026-08-14: the voiceprint writer calls `record_turn_name` and `add_sample`, the
transcript endpoint calls `live_turn_names`, and `DELETE /api/org/voiceprints/{id}` calls
`withdraw`.

`profiles_for_matching` is called in production by `lambda_voiceprint_writer` — `_profiles`
serves the matcher's synchronous fetch, and `_agreement` uses it to refuse a sample that sits
closer to somebody else. **This paragraph has now claimed the opposite three times**, once
while the callers existed, and the correction is worth more than the fact: a docstring that
says "nothing calls this" ages into a licence to change the function freely, and both callers
depend on its consent and withdrawn filters. It left the correction endpoint when the voice
vectors it returns turned out to be the biometric-residence defect's fifth home; it did not
stop being called.

(This paragraph has now been wrong twice in two days, in opposite directions.)

Two of those queries have failure modes that are invisible in production:

* `profiles_for_matching` — a profile without consent, or a withdrawn one, would simply keep
  naming people, correctly as far as anything downstream can tell. So the filters are in the
  SQL rather than the caller, and asserted by tests on the SQL text.
* the company scope — this codebase has twice let `[]`/`None` mean both "no filter" and
  "nothing", and here that would match one company's voice against another's profiles. A
  missing company id raises.
"""
import logging

from psycopg.rows import dict_row

logger = logging.getLogger(__name__)

EMBEDDING_DIMS = 192


def _vector_literal(embedding):
    """pgvector's text input form. Built here so the fake in tests only ever sees a string
    and the suite never needs the extension installed."""
    values = list(embedding or [])
    if len(values) != EMBEDDING_DIMS:
        raise ValueError(
            f"embedding must have {EMBEDDING_DIMS} dimensions, got {len(values)} — the "
            f"column is vector({EMBEDDING_DIMS}) and Postgres would only reject this at "
            f"insert time, long after the window and the consent were decided")
    return "[" + ",".join(str(float(v)) for v in values) + "]"


def _parse_vector(value):
    """The inverse of `_vector_literal`, for a runtime without pgvector.

    `register_vector` is what normally turns a `vector` column back into numbers, and it
    needs pgvector, which needs numpy, and the psycopg layer that ships it is cp311-only —
    while the function that reads these embeddings runs on cp312 (that is where onnxruntime
    lives). One function cannot have both layers.

    Without registration psycopg hands the column back as the literal text `'[0.1,0.2,…]'`.
    Passing that to `cosine` does not raise "you forgot pgvector"; numpy will happily make a
    zero-dimensional object array out of it, and the failure surfaces far from the cause, as
    a similarity score rather than an error. So the conversion happens here, at the single
    read, and is a no-op when pgvector already did it.
    """
    if value is None or not isinstance(value, str):
        return value
    return [float(x) for x in value.strip().strip("[]").split(",") if x.strip()]


def _decode(rows):
    """Every row leaving this module carries numbers, not a vector literal."""
    for r in rows:
        if "embedding" in r:
            r["embedding"] = _parse_vector(r["embedding"])
    return rows


def _require_company(company_id):
    if not company_id:
        raise ValueError("company_id is required on every voiceprint query — an absent one "
                         "would match a voice against every company's profiles at once")
    return company_id


def upsert_profile(conn, company_id, display_name=None, user_id=None,
                   consent_given=False, consented_by=None,
                   linked_by=None, linked_on=None,
                   consent_basis=None, asserted_by=None) -> dict | None:
    """The profile a name attaches to. Existing one if there is one, otherwise a new row.

    **Consent is a precondition, not a checkbox** (§6, §10). A voiceprint is biometric
    information under the NZ Privacy Act and the consent has to come from the person whose
    voice it is — not the wearer, not the employer, not whoever is doing the labelling. So a
    profile carrying a NAME cannot be created without it and this raises.

    A profile with NO name needs none: an unnamed recurring voice links a pattern to nobody.
    That is also what `speaker_voiceprints.user_id` being nullable is for.

    `consent_at` is stamped by the database, not taken from the caller. A client-supplied
    timestamp is a claim about when somebody agreed; this is a record of when the system was
    told — which is the only one of the two this code can honestly make.

    Reuse matters more than it looks: two profiles for one voice make that person his own
    runner-up, which is exactly the failure `aggregate_scores` was written for. A `withdrawn`
    profile is never reused — that would resurrect a withdrawal by the back door.
    """
    _require_company(company_id)
    # `attestation` is a basis this function did not previously have, and it exists because
    # the one it did have excluded the population the feature is for. `consented_by` is the
    # SUBJECT's id, so it can only be filled for someone with an account — and the people
    # most often named on a site are subcontractors without one. The result was a rule that
    # was never satisfied and a library that stayed empty.
    #
    # So attestation records a CLAIM, and records who made it in `asserted_by` rather than
    # borrowing `consented_by` for it. The distinction is the whole point: one column says
    # "this person agreed", the other says "this person says they agreed", and collapsing
    # them makes every existing row ambiguous and every later audit unanswerable.
    #
    # What it does NOT do is make the claim true. `consent_basis` travels with the profile
    # so every reader can see which kind it is, and a later decision to hold attested
    # profiles to a stricter standard can find them in one query.
    attested = consent_basis == "attestation"
    if attested and not asserted_by:
        raise ValueError(
            "an attested voiceprint needs asserted_by: who is making the claim. A claim "
            "with nobody attached to it is not a record of anything")
    if display_name and not (consented_by or attested):
        # The docstring claimed this was required and the code did not check, so the layer
        # that actually stores the row would accept what the endpoint refuses. A rule
        # enforced in exactly one caller is a rule until somebody adds a second caller.
        raise ValueError(
            "a named voiceprint needs consented_by: whose voice this is, recorded — a "
            "timestamp alone cannot tell the subject agreeing from somebody agreeing on "
            "their behalf (§6)")
    if display_name and not (consent_given or attested):
        raise ValueError(
            "a named voiceprint cannot be created without consent from the person whose "
            "voice it is (§6) — an unnamed profile may be created instead")
    cur = conn.cursor(row_factory=dict_row)
    if display_name:
        # Matched on the PERSON, not on the string. Two real people in one company can share
        # a display name, and merging them stores one person's voice under the other's
        # consent with no way to tell afterwards which samples belong to whom.
        #
        # The two failure directions are not symmetric, which is what decides this: a
        # duplicate profile degrades into a REFUSAL — the person becomes his own runner-up
        # and the margin declines to confirm — while a merge is a wrong confident answer
        # about somebody's biometric data. `consented_by` is required whenever a name is
        # given, so the anchor exists exactly when it is needed.
        # THREE keys, in order of how stable the identity behind them is.
        #
        # `user_id` when the name resolved to somebody in the directory: an identity, so two
        # people who share a display name stay two profiles and one person named twice stays
        # one. This is the only key that is right in both directions.
        #
        # Otherwise the name resolved to nobody — a subcontractor, a visitor — and there is
        # no identity to key on. Then it is (name, whoever vouched): duplicates when two
        # people each name the same worker, never a merge of two different workers who share
        # a name. That direction is chosen deliberately, by the asymmetry recorded above: a
        # duplicate degrades into a REFUSAL, because the person becomes his own runner-up and
        # the margin declines to confirm, while a merge is a wrong confident answer about
        # somebody's biometric data.
        if user_id:
            found = cur.execute(
                "SELECT id FROM speaker_voiceprints "
                "WHERE company_id = %s AND user_id = %s AND status <> 'withdrawn' "
                "ORDER BY created_at LIMIT 1",
                (company_id, user_id)).fetchone()
        else:
            anchor = consented_by or asserted_by
            found = cur.execute(
                "SELECT id FROM speaker_voiceprints "
                "WHERE company_id = %s AND display_name = %s AND user_id IS NULL "
                "  AND coalesce(consented_by, asserted_by) = %s "
                "  AND status <> 'withdrawn' "
                "ORDER BY created_at LIMIT 1",
                (company_id, display_name, anchor)).fetchone()
        if found:
            # Link an EXISTING profile too, or the whole thing only works for profiles
            # created after this shipped — and every profile in the database predates it,
            # so site narrowing would stay a no-op for the entire existing population.
            # That is the identical silent-inert failure this work exists to remove.
            #
            # `AND user_id IS NULL` so a link is never overwritten: a person is not
            # re-identified by somebody typing the same name again.
            if user_id:
                cur.execute(
                    "UPDATE speaker_voiceprints "
                    "SET user_id = %s, linked_by = %s, linked_at = now(), linked_on = %s "
                    "WHERE id = %s AND user_id IS NULL",
                    (user_id, linked_by, linked_on, found["id"]))
            return found
    return cur.execute(
        "INSERT INTO speaker_voiceprints "
        "(company_id, user_id, display_name, status, consent_at, consented_by, "
        " linked_by, linked_at, linked_on, consent_basis, asserted_by) "
        # Both CASE parameters are cast. A parameter whose only use is `IS NULL` gives
        # Postgres nothing to infer a type from, so the statement fails at PREPARE time for
        # every value — `IndeterminateDatatype: could not determine data type of parameter
        # $7`, observed on TEST, which made naming any speaker without an existing profile a
        # deterministic 500. The suite could not see it: FakeConn never prepares SQL.
        "VALUES (%s, %s, %s, 'tentative', "
        "        CASE WHEN %s::boolean THEN now() ELSE NULL END, %s, "
        "        %s, CASE WHEN %s::uuid IS NULL THEN NULL ELSE now() END, %s, %s, %s) "
        "RETURNING id",
        (company_id, user_id, display_name, bool(consent_given or attested), consented_by,
         linked_by, user_id, linked_on, consent_basis, asserted_by),
    ).fetchone()


class EnrolmentBelongsToSomebodyElse(Exception):
    """A sample was about to join a profile it resembles LESS than it resembles another.

    Carries the two scores so the caller can show a person the choice, rather than only
    telling them it declined.
    """

    def __init__(self, own, best_other, nearest_other_id):
        self.own = own
        self.best_other = best_other
        self.nearest_other_id = nearest_other_id
        super().__init__(
            f"this window is closer to another profile ({best_other:.3f}) than to the one "
            f"it would join ({own:.3f}); it may be a different person with the same name")


def _agreement(conn, company_id, voiceprint_id, embedding):
    """(own, best_other, nearest_other_id) for a sample about to be stored.

    `own` is against the samples this profile ALREADY has, so a first sample gets None —
    there is nothing to agree with, and inventing a number would corrupt the dataset the
    threshold is eventually meant to come from.

    Deliberately NOT a threshold. `upsert_profile` matches an existing profile by NAME, so
    two people called "Leo" confirmed by the same person land on one profile, and a profile
    cannot be un-poisoned — only the contributing sample deleted. The obvious guard is a
    similarity floor and it is not available: Phase 0 measured the same person varying by
    more than 0.2 across sessions, so any floor drawn today rejects legitimate second
    samples and was drawn from nothing.

    What IS available is the ORDER of these two numbers. Both are measured on the same
    audio, so whatever makes a voice score low today lowers both of them together, and the
    comparison survives the drift that defeats a floor.
    """
    import voiceprint_utils as vp

    rows = profiles_for_matching(conn, company_id)
    own_vecs = [r["embedding"] for r in rows if str(r["id"]) == str(voiceprint_id)]
    others = [r for r in rows if str(r["id"]) != str(voiceprint_id)]

    own = None
    if own_vecs:
        own = max(vp.cosine(v, embedding) for v in own_vecs)

    best_other, nearest_other_id = None, None
    if others:
        by_profile = {}
        for r in others:
            sc = vp.cosine(r["embedding"], embedding)
            pid = str(r["id"])
            if sc > by_profile.get(pid, -2.0):
                by_profile[pid] = sc
        nearest_other_id, best_other = max(by_profile.items(), key=lambda kv: kv[1])
    return own, best_other, nearest_other_id


def add_sample(conn, company_id, voiceprint_id, embedding, source, s3_key, window,
               created_by=None, correction_ref=None,
               admitted_max_spread=None) -> dict | None:
    """Record one enrolment contribution.

    One row per event rather than an averaged vector per person: §6's withdrawal needs each
    contribution individually removable, and an average cannot be un-poisoned.

    `admitted_max_spread` is the homogeneity limit this window got past, and it is stored
    only when it was NOT the compiled-in default. NULL therefore means "the ordinary guard",
    and the non-NULL rows are exactly the ones worth re-examining if the loosened limit turns
    out to have been too loose — which is the whole reason the limit is settable.

    `correction_ref` and `created_by` are what make a bad enrolment traceable to everything
    it justified. They are optional in the signature and should not be: they are only
    optional because a future enrolment path may have no correction behind it.

    The guard lives HERE and not in a caller because there are two enrolment paths — the
    one folded into a propagation and the standalone one — and a rule that only one of them
    runs is a rule the other quietly does without. See `_agreement` for why it compares an
    order rather than testing a threshold.
    """
    _require_company(company_id)
    own, best_other, nearest_other_id = _agreement(conn, company_id, voiceprint_id,
                                                   embedding)
    if own is not None and best_other is not None and best_other > own:
        raise EnrolmentBelongsToSomebodyElse(own, best_other, nearest_other_id)
    start_s, end_s = (window or (None, None))
    return conn.cursor(row_factory=dict_row).execute(
        "INSERT INTO speaker_voiceprint_samples "
        "(company_id, voiceprint_id, embedding, source, s3_key, window_start_s, "
        " window_end_s, created_by, correction_ref, agreement_own, "
        " agreement_best_other, nearest_other_id, admitted_max_spread) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (company_id, voiceprint_id, _vector_literal(embedding), source, s3_key,
         start_s, end_s, created_by, correction_ref, own, best_other, nearest_other_id,
         admitted_max_spread),
    ).fetchone()


def profiles_for_matching(conn, company_id, site_id=None) -> list[dict]:
    """Every profile this company may currently match against, optionally narrowed to a site.

    The two filters are the whole point, and both fail silently if they are missing:

    * `consent_at IS NOT NULL` — the subject of a voiceprint is the person recorded, not the
      person who labelled them (§6). A profile without consent must be inert, not merely
      undisplayed.
    * `status <> 'withdrawn'` — a withdrawal that still matches is not a withdrawal.

    `status` comes back with the vector so the caller can cap a tentative profile at
    tentative output rather than promoting it to a confirmed name.

    **Why scope exists.** Every turn is scored against every row returned here, and
    `decide_name` takes the runner-up as the maximum over the rest — so the size of this
    result, not the number of people in the room, is what the margin has to survive. A
    company that accumulates profiles across many sites makes every turn harder to confirm.
    `site_id=None` keeps the company-wide behaviour and pays for no join.

    **Unnamed profiles stay in scope, and this is the decision worth arguing with.**
    `speaker_voiceprints.user_id` is nullable by design (0038): a recurring unnamed voice may
    hold a profile before anyone names it, which is what makes "the same person again"
    visible. Such a profile reaches no `memberships` row, so a plain site join drops every
    one of them and silently removes the feature the nullable column exists for — the
    empty-filter-means-no-filter shape this codebase has been bitten by.

    Keeping them is also the safe direction rather than merely the convenient one: a profile
    with no user cannot produce a NAME. The worst it can do is become the runner-up and push
    a real match down to `tentative`, which is a refusal, not a wrong answer — and a wrong
    confident name is the failure this whole layer is shaped to avoid.
    """
    _require_company(company_id)
    cols = ("SELECT p.id, p.display_name, p.status, p.user_id, s.id AS sample_id, "
            "       s.embedding "
            "FROM speaker_voiceprints p "
            "JOIN speaker_voiceprint_samples s ON s.voiceprint_id = p.id "
            "WHERE p.company_id = %s "
            "  AND p.consent_at IS NOT NULL "
            "  AND p.status <> 'withdrawn' ")
    if site_id is None:
        return _decode(conn.cursor(row_factory=dict_row).execute(
            cols + "ORDER BY p.created_at", (company_id,)).fetchall())
    # Three arms, and the middle one is the whole reason this can be turned on.
    #
    # `user_id IS NULL` — an unnamed recurring voice, in scope everywhere by design.
    #
    # NO MEMBERSHIP AT ALL — `upsert_field_only_user` writes only `users`, so a directory
    # entry for somebody with no login has no membership row. Without this arm, attaching an
    # identity would make that person LESS matchable than before: matchable while unlinked,
    # invisible the moment they are linked. The fix would have introduced the regression.
    #
    # A MEMBERSHIP AT THIS SITE — the narrowing itself.
    #
    # `archived_at IS NULL` on both, because every other membership query in this repository
    # has it (repositories/memberships.py:29, 52, 71, …) and without it somebody removed from
    # a site keeps being matched there: a guard satisfied and ineffective.
    return _decode(conn.cursor(row_factory=dict_row).execute(
        cols
        + "  AND (p.user_id IS NULL "
          "       OR NOT EXISTS (SELECT 1 FROM memberships m2 "
          "                       WHERE m2.user_id = p.user_id "
          "                         AND m2.archived_at IS NULL) "
          "       OR EXISTS (SELECT 1 FROM memberships m "
          "                   WHERE m.user_id = p.user_id AND m.site_id = %s "
          "                     AND m.archived_at IS NULL)) "
          "ORDER BY p.created_at",
        (company_id, site_id),
    ).fetchall())


def withdraw(conn, company_id, voiceprint_id) -> list:
    """Honour a withdrawal: the vectors go, the audit stays.

    Returns the ids of the deleted samples so the caller can un-name the turns they
    justified (Phase 6). The profile row survives as `withdrawn` — a record that it existed
    and was removed is what an audit of a withdrawal consists of.
    """
    _require_company(company_id)
    cur = conn.cursor(row_factory=dict_row)
    rows = cur.execute(
        "SELECT id FROM speaker_voiceprint_samples "
        "WHERE company_id = %s AND voiceprint_id = %s",
        (company_id, voiceprint_id),
    ).fetchall()
    # The names this profile justified stop being shown. §6 requires a withdrawal to reach
    # "everything it justified", and removing the vectors while the transcript still reads
    # the person's name is a withdrawal in the database and not in the product — TEST showed
    # exactly that: 200 returned, seven rows still naming them.
    #
    # BOTH routes, because each alone misses rows the other reaches.
    #
    # `correction_ref` via the samples finds everything a successful enrolment justified —
    # keying on `voiceprint_id` alone would match nothing, since every propagated row carries
    # a NULL profile id by design (that is what stops the machine confirming its own
    # profiles).
    #
    # But the profile is created BEFORE the embedder runs, and the embedder can refuse the
    # enrolment — a window under ten seconds, or one holding two voices. Then names exist and
    # no sample does, and the subquery finds nothing: 200, zero removed, every name still on
    # the transcript. That is the first thing a tester hits with a short window.
    #
    # Superseded, not deleted: the audit of a withdrawal is partly the record of what it
    # removed, and a deleted row cannot say a name was ever shown.
    cur.execute(
        "UPDATE speaker_turn_names SET superseded_at = now() "
        "WHERE company_id = %s AND superseded_at IS NULL "
        "  AND (voiceprint_id = %s"
        "       OR correction_ref IN ("
        "            SELECT correction_ref FROM speaker_voiceprint_samples "
        "             WHERE company_id = %s AND voiceprint_id = %s "
        "               AND correction_ref IS NOT NULL))",
        (company_id, voiceprint_id, company_id, voiceprint_id))
    cur.execute(
        "DELETE FROM speaker_voiceprint_samples "
        "WHERE company_id = %s AND voiceprint_id = %s",
        (company_id, voiceprint_id))
    cur.execute(
        "UPDATE speaker_voiceprints SET status = 'withdrawn' "
        "WHERE company_id = %s AND id = %s",
        (company_id, voiceprint_id))
    return [r["id"] for r in rows]


def confirmations_count(conn, company_id, voiceprint_id) -> int:
    """How many INDEPENDENT confirmations this profile has (§6).

    Distinct sessions, not distinct corrections: three corrections inside one meeting are
    one person clicking three times, and counting them separately would let a single
    mistaken labelling promote a profile on its own.
    """
    _require_company(company_id)
    row = conn.cursor(row_factory=dict_row).execute(
        "SELECT count(DISTINCT n.session_base) AS n FROM speaker_turn_names n "
        "WHERE n.company_id = %s AND n.voiceprint_id = %s AND n.state = 'confirmed' "
        # Human corrections only. Propagation writes `confirmed` rows too, so without this
        # the system satisfies its own promotion criterion with its own output -- profiles
        # confirming themselves overnight, across sessions, with nothing in the schema
        # showing that the loop exists. The second cut is that propagation rows carry
        # voiceprint_id NULL, which this equality can never match.
        "  AND n.source = 'correction'",
        (company_id, voiceprint_id),
    ).fetchone()
    return int((row or {}).get("n") or 0)


def record_turn_name(conn, company_id, session_base, turn_ref, state, source,
                     correction_ref=None, cluster_ref=None, cluster_threshold=None,
                     voiceprint_id=None, score=None, margin=None,
                     label_disagreement=None, display_name=None) -> dict | None:
    """One name for one turn, replacing whatever was live for it.

    Supersede-then-insert, in the CALLER'S transaction. S3 events are unordered and more
    than one run can be in flight, so without the supersede first the partial unique index
    turns a race into a write failure rather than a replacement — and a caller that then
    retries would find the same collision.

    `voiceprint_id` stays None for propagation. A correction that creates no profile has no
    id to point at, and inventing a named profile to satisfy a foreign key is the consent
    violation Phase 4 exists to refuse.
    """
    _require_company(company_id)
    # The rank table lives in `turn_name_overlay` because the READ path needs it and that
    # module must stay importable by the embedder, which runs on cp312 with no psycopg. The
    # dependency therefore points this way, and the import is local for the same reason
    # `_agreement`'s is.
    from turn_name_overlay import _SOURCE_RANK

    cur = conn.cursor(row_factory=dict_row)
    incoming = _SOURCE_RANK.get(source, -1)
    # FOR UPDATE, so two writers racing on one turn serialise here rather than at the partial
    # unique index — where the loser sees a write failure instead of a decision.
    live = cur.execute(
        "SELECT id, source, state FROM speaker_turn_names "
        "WHERE company_id = %s AND session_base = %s AND turn_ref = %s "
        "  AND superseded_at IS NULL FOR UPDATE",
        (company_id, session_base, turn_ref)).fetchone()
    if live is not None and _SOURCE_RANK.get(live.get("source"), -1) > incoming:
        # Refused, not lost. The supersede below has no source predicate of its own — it
        # never had one — so without this a `label_inheritance` or `voiceprint_match` row
        # would bury a human's `correction` simply by arriving later, and `_SOURCE_RANK`
        # could not help: it ranks at READ time, among rows that both survived.
        #
        # Equal rank still replaces. A newer match superseding an older match is the wanted
        # behaviour; only a write that would move a turn DOWN the scale is declined.
        logger.info("turn name declined: %s would not beat live %s on %s",
                    source, live.get("source"), turn_ref)
        return None
    cur.execute(
        "UPDATE speaker_turn_names SET superseded_at = now() "
        "WHERE company_id = %s AND session_base = %s AND turn_ref = %s "
        "  AND superseded_at IS NULL",
        (company_id, session_base, turn_ref))
    return cur.execute(
        "INSERT INTO speaker_turn_names "
        "(company_id, voiceprint_id, session_base, turn_ref, state, score, margin, "
        " source, correction_ref, cluster_ref, cluster_threshold, label_disagreement, "
        " display_name) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (company_id, voiceprint_id, session_base, turn_ref, state, score, margin,
         source, correction_ref, cluster_ref, cluster_threshold, label_disagreement,
         display_name),
    ).fetchone()


def live_turn_names(conn, company_id, session_base) -> list[dict]:
    """The overlay for one session: one row per turn, superseded rows excluded.

    Precedence is applied AGAIN by the reader. This query returns what the unique index
    guarantees — one live row per `turn_ref` string — but the read-time join matches turns by
    overlap with tolerance, because re-extraction shifts `start_sec` and a strict join would
    make names silently vanish. Two rows whose strings differ slightly can therefore both
    match one physical turn while satisfying the index.
    """
    _require_company(company_id)
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT id, voiceprint_id, turn_ref, state, score, margin, source, correction_ref, "
        "       cluster_ref, cluster_threshold, label_disagreement, display_name, "
        "       created_at "
        "FROM speaker_turn_names "
        "WHERE company_id = %s AND session_base = %s AND superseded_at IS NULL "
        "ORDER BY created_at",
        (company_id, session_base),
    ).fetchall()


def unname(conn, company_id, session_base, display_name, rejected_by=None) -> int:
    """Take a name off one session's transcript. Returns how many turns stopped showing it.

    Separate from `withdraw` on purpose, because they answer different questions and only one
    of them has a handle in every case:

    * `withdraw` removes a stored voiceprint and everything it justified. It needs a profile,
      and a correction made without consent creates none — so on TEST a session held seven
      named turns and withdrawal could reach exactly one.
    * this removes a NAME from a TRANSCRIPT. It is the ordinary request ("that is not me"),
      it needs no profile, and it had no API at all until 2026-08-14.

    It deliberately does not touch the voiceprint. Somebody who wants their name off one
    meeting has not asked for their profile to be destroyed, and doing both would answer a
    question they did not ask.

    Scoped to ONE session: a person may be named correctly in twenty meetings and wrongly in
    one. Superseded rather than deleted, like every other removal here — the audit of a
    removal is partly the record that something was once shown.
    """
    _require_company(company_id)
    if not display_name:
        raise ValueError("display_name is required — an absent one would clear every name "
                         "in the session, which is a different request")
    cur = conn.cursor(row_factory=dict_row)
    rows = cur.execute(
        "UPDATE speaker_turn_names SET superseded_at = now() "
        "WHERE company_id = %s AND session_base = %s AND display_name = %s "
        "  AND superseded_at IS NULL "
        "RETURNING id",
        (company_id, session_base, display_name),
    ).fetchall()
    # A tombstone, because superseding alone records that a name WAS shown, not that it was
    # REJECTED — and only the second of those should stop a later inference. Without it,
    # label inheritance re-derives the same name from the same transcriber label on the next
    # run and the user has to delete it again after every run, with nothing logged.
    cur.execute(
        "INSERT INTO speaker_name_rejections "
        "(company_id, session_base, display_name, rejected_by) "
        "VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (company_id, session_base, display_name) "
        "DO UPDATE SET rejected_at = now(), rejected_by = EXCLUDED.rejected_by",
        (company_id, session_base, display_name, rejected_by))
    return len(rows)


def rejected_names(conn, company_id, session_base) -> set:
    """Names a person explicitly took off this session.

    Read before any inference writes a name. `live_turn_names` cannot answer this — a
    superseded row means "this was shown once", which is also true of a name that was merely
    replaced by a better one.
    """
    _require_company(company_id)
    rows = conn.cursor(row_factory=dict_row).execute(
        "SELECT display_name FROM speaker_name_rejections "
        "WHERE company_id = %s AND session_base = %s",
        (company_id, session_base)).fetchall()
    return {r["display_name"] for r in rows if r.get("display_name")}


def has_human_sample(conn, company_id, voiceprint_id) -> bool:
    """Whether any sample on this profile came from a window a person vouched for.

    A profile can now be BUILT entirely from harvest — cluster members the machine selected
    after one human named one turn. That is worth having: a single corrected window is often
    under 10 s and makes a weak profile. But a profile assembled from inference must not be
    able to earn confidence from inference, or the loop agrees with itself and no later
    evidence can say where it started going wrong.

    So: coverage may come from the machine, confidence only from people. This is the query
    that keeps those apart, and it is the reason harvested samples carry their own `source`
    rather than being indistinguishable from the anchor.
    """
    _require_company(company_id)
    row = conn.cursor(row_factory=dict_row).execute(
        "SELECT 1 FROM speaker_voiceprint_samples "
        "WHERE company_id = %s AND voiceprint_id = %s AND source = 'correction' LIMIT 1",
        (company_id, voiceprint_id)).fetchone()
    return bool(row)


def record_attempt(conn, company_id, voiceprint_id, outcome, detail=None) -> None:
    """What happened the last time a window was offered to this profile.

    A refusal currently reaches CloudWatch and stops there: the writer returns it, nothing
    reads the return value, and the person who made the correction was told `enrolment:
    "requested"`. From outside, a profile with zero samples looks the same whether its
    enrolment was declined on its merits or the embedder died halfway — both happened on
    TEST tonight, and both looked identical.

    Written for every attempt, not only failures. "Nothing was ever offered" and "something
    was offered and refused" are different answers to "why is this profile empty", and only
    one of them is a bug.
    """
    _require_company(company_id)
    conn.cursor(row_factory=dict_row).execute(
        "UPDATE speaker_voiceprints "
        "SET last_attempt_at = now(), last_attempt_outcome = %s, last_attempt_detail = %s "
        "WHERE company_id = %s AND id = %s",
        (outcome, detail, company_id, voiceprint_id))


def list_profiles(conn, company_id) -> list[dict]:
    """Every profile this company holds, with enough to explain each one's state.

    There was no read endpoint at all: a profile could be created, refused, and left empty
    with no way to look at it short of the database. `samples` is the number that matters —
    a named profile with zero of them names nobody — and `human_samples` separates what a
    person vouched for from what the clustering suggested, which is the distinction the whole
    harvest design rests on.

    Vectors are deliberately absent. They are biometric data and nothing in a listing needs
    them; the one place they may travel is the synchronous fetch the matcher makes.
    """
    _require_company(company_id)
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT p.id, p.display_name, p.status, p.user_id, p.consent_at, p.consented_by, "
        "       p.linked_on, p.linked_at, "
        "       p.last_attempt_at, p.last_attempt_outcome, p.last_attempt_detail, "
        "       count(s.id) AS samples, "
        "       count(s.id) FILTER (WHERE s.source = 'correction') AS human_samples "
        "FROM speaker_voiceprints p "
        "LEFT JOIN speaker_voiceprint_samples s ON s.voiceprint_id = p.id "
        "WHERE p.company_id = %s "
        "GROUP BY p.id ORDER BY p.created_at DESC",
        (company_id,)).fetchall()
