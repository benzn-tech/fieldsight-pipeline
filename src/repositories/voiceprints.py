"""Speaker voiceprints: enrol a sample, offer profiles for matching, withdraw.

Design: docs/superpowers/specs/2026-08-09-speaker-identity-v2.md §6, §8
Schema: src/migrations/0038_speaker_voiceprints.sql

Nothing calls this yet (Phase 2 ships inert). It exists now so the queries that must never
be wrong are written and tested before anything depends on them being right.

Two of those queries have failure modes that are invisible in production:

* `profiles_for_matching` — a profile without consent, or a withdrawn one, would simply keep
  naming people, correctly as far as anything downstream can tell. So the filters are in the
  SQL rather than the caller, and asserted by tests on the SQL text.
* the company scope — this codebase has twice let `[]`/`None` mean both "no filter" and
  "nothing", and here that would match one company's voice against another's profiles. A
  missing company id raises.
"""
from psycopg.rows import dict_row

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


def add_sample(conn, company_id, voiceprint_id, embedding, source, s3_key, window,
               created_by=None, correction_ref=None) -> dict | None:
    """Record one enrolment contribution.

    One row per event rather than an averaged vector per person: §6's withdrawal needs each
    contribution individually removable, and an average cannot be un-poisoned.

    `correction_ref` and `created_by` are what make a bad enrolment traceable to everything
    it justified. They are optional in the signature and should not be: they are only
    optional because a future enrolment path may have no correction behind it.
    """
    _require_company(company_id)
    start_s, end_s = (window or (None, None))
    return conn.cursor(row_factory=dict_row).execute(
        "INSERT INTO speaker_voiceprint_samples "
        "(company_id, voiceprint_id, embedding, source, s3_key, window_start_s, "
        " window_end_s, created_by, correction_ref) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (company_id, voiceprint_id, _vector_literal(embedding), source, s3_key,
         start_s, end_s, created_by, correction_ref),
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
    return _decode(conn.cursor(row_factory=dict_row).execute(
        cols
        + "  AND (p.user_id IS NULL OR EXISTS ("
          "        SELECT 1 FROM memberships m "
          "         WHERE m.user_id = p.user_id AND m.site_id = %s)) "
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
    cur = conn.cursor(row_factory=dict_row)
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
