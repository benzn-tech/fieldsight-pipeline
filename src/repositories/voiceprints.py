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


def profiles_for_matching(conn, company_id) -> list[dict]:
    """Every profile this company may currently match against.

    The two filters are the whole point, and both fail silently if they are missing:

    * `consent_at IS NOT NULL` — the subject of a voiceprint is the person recorded, not the
      person who labelled them (§6). A profile without consent must be inert, not merely
      undisplayed.
    * `status <> 'withdrawn'` — a withdrawal that still matches is not a withdrawal.

    `status` comes back with the vector so the caller can cap a tentative profile at
    tentative output rather than promoting it to a confirmed name.
    """
    _require_company(company_id)
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT p.id, p.display_name, p.status, p.user_id, s.id AS sample_id, "
        "       s.embedding "
        "FROM speaker_voiceprints p "
        "JOIN speaker_voiceprint_samples s ON s.voiceprint_id = p.id "
        "WHERE p.company_id = %s "
        "  AND p.consent_at IS NOT NULL "
        "  AND p.status <> 'withdrawn' "
        "ORDER BY p.created_at",
        (company_id,),
    ).fetchall()


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
        "WHERE n.company_id = %s AND n.voiceprint_id = %s AND n.state = 'confirmed'",
        (company_id, voiceprint_id),
    ).fetchone()
    return int((row or {}).get("n") or 0)
