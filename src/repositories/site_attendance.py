"""Who is on site, per (site, NZ day) -- narrowing input for the voice matcher.

Design: docs/superpowers/specs/2026-09-30-on-site-roster-design.md ("Review outcome"
supersedes the earlier sections). Plan: docs/superpowers/plans/2026-09-30-on-site-roster.md,
Task 2. Schema: src/migrations/0072_site_attendance.sql.

**Resolution happens twice, and the two times answer different questions.** `upsert` resolves
`user_id`/`voiceprint_id` once, at write time, as a convenience so a listing can show whether a
name is known to the directory. `on_roster_profile_ids` re-resolves at LOOKUP time, independent
of whatever `upsert` stored, because a profile enrolled AFTER the roster was written (the
roadmap's "sign in at the gate = enrol your voice") must still be found on a day whose roster
predates the enrolment (plan correction 4). The three-arm join is the load-bearing part of this
module for exactly that reason.
"""
import logging

from psycopg.rows import dict_row

logger = logging.getLogger(__name__)


def _require_company(company_id):
    if not company_id:
        raise ValueError("company_id is required on every attendance query — an absent one "
                         "would narrow one company's matching against another's roster")
    return company_id


def _resolve_voiceprint(conn, company_id, user_id, display_name):
    """A company-scoped `speaker_voiceprints` lookup: by `user_id` first, else by a UNIQUE
    `lower(display_name)` among non-withdrawn profiles. Ambiguous or absent -> None, never a
    guess -- the same rule `users.resolve_display_name` applies to the identity side."""
    cur = conn.cursor(row_factory=dict_row)
    if user_id:
        row = cur.execute(
            "SELECT id FROM speaker_voiceprints "
            "WHERE company_id = %s AND user_id = %s AND status <> 'withdrawn'",
            (company_id, user_id)).fetchone()
        if row:
            return row["id"]
    rows = cur.execute(
        "SELECT id FROM speaker_voiceprints "
        "WHERE company_id = %s AND status <> 'withdrawn' AND lower(display_name) = lower(%s)",
        (company_id, display_name)).fetchall()
    return rows[0]["id"] if len(rows) == 1 else None


def upsert(conn, company_id, site_id, attend_date, rows, source, created_by=None) -> dict:
    """Write today's attendance for one (site, day) from one source. Idempotent per row.

    `rows` is `[{"displayName", "employerName"?}, ...]`. A `manual` row's `source_ref` is
    `lower(trim(display_name))` (plan Task 2) so re-typing the same name twice in one day is
    the same row, not a duplicate -- a connector row instead carries the source's own id as
    `source_ref` and is passed already keyed (`{"displayName", "employerName"?, "sourceRef",
    "userId"?, "voiceprintId"?}`), Phase 2/3's `attendance_upsert` writer op.

    The UPDATE side never overwrites a resolved hint with a weaker one: `user_id` and
    `voiceprint_id` are `COALESCE(existing, incoming)`, because a later report with a blank
    hint must not un-resolve an identity a previous report (or a later enrolment via
    `on_roster_profile_ids`, though that path never writes back here) already established.
    `employer_name` takes the newest non-null value -- people change subcontractors, and the
    most recent report is the one to trust, matching `voiceprints.upsert_profile`'s own rule
    for the same column.
    """
    _require_company(company_id)
    inserted = updated = 0
    for row in rows or []:
        display_name = (row.get("displayName") or "").strip()
        if not display_name:
            continue
        employer_name = (row.get("employerName") or "").strip() or None
        source_ref = row.get("sourceRef") or display_name.strip().lower()
        user_id = row.get("userId")
        voiceprint_id = row.get("voiceprintId")
        if user_id is None and voiceprint_id is None:
            resolved, _ = _resolved_user(conn, company_id, display_name)
            user_id = resolved
            voiceprint_id = _resolve_voiceprint(conn, company_id, user_id, display_name)
        cur = conn.cursor(row_factory=dict_row)
        result = cur.execute(
            "INSERT INTO site_attendance "
            "(company_id, site_id, attend_date, display_name, user_id, voiceprint_id, "
            " employer_name, source, source_ref, created_by) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (company_id, source, source_ref, attend_date) DO UPDATE SET "
            "  last_seen_at = now(), "
            "  display_name = EXCLUDED.display_name, "
            "  employer_name = COALESCE(EXCLUDED.employer_name, site_attendance.employer_name), "
            "  user_id = COALESCE(site_attendance.user_id, EXCLUDED.user_id), "
            "  voiceprint_id = COALESCE(site_attendance.voiceprint_id, EXCLUDED.voiceprint_id) "
            "RETURNING (xmax = 0) AS inserted",
            (company_id, site_id, attend_date, display_name, user_id, voiceprint_id,
             employer_name, source, source_ref, created_by)).fetchone()
        if result and result.get("inserted"):
            inserted += 1
        else:
            updated += 1
    return {"inserted": inserted, "updated": updated}


def _resolved_user(conn, company_id, display_name):
    """`users.resolve_display_name`, imported locally: this module is loaded by the same
    in-VPC writer as `repositories.voiceprints`, and a module-level import of a sibling
    repository here is no different in kind from `voiceprints._agreement`'s own local
    import of `vector_math` -- kept local so a change to one repository's import graph
    cannot surprise the other's."""
    from repositories.users import resolve_display_name
    row, _reason = resolve_display_name(conn, company_id, display_name)
    return (row["id"] if row else None), _reason


def for_day(conn, company_id, site_id, attend_date) -> list[dict]:
    """The roster: every attendance row for one (site, day), across every source."""
    _require_company(company_id)
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT id, display_name, employer_name, user_id, voiceprint_id, source, "
        "       source_ref, first_seen_at, last_seen_at "
        "FROM site_attendance "
        "WHERE company_id = %s AND site_id = %s AND attend_date = %s "
        "ORDER BY display_name",
        (company_id, site_id, attend_date)).fetchall()


def remove(conn, company_id, site_id, attend_date, row_id) -> int:
    """Delete one MANUAL row. A connector row is the source's own to remove or overwrite
    (plan correction 7) -- this never touches one, on purpose: deleting a connector's row
    here would make the next sync silently re-create it with no record that it was ever
    hidden, which is a worse UX than refusing the delete outright."""
    _require_company(company_id)
    rows = conn.cursor(row_factory=dict_row).execute(
        "DELETE FROM site_attendance "
        "WHERE company_id = %s AND site_id = %s AND attend_date = %s AND id = %s "
        "  AND source = 'manual' "
        "RETURNING id",
        (company_id, site_id, attend_date, row_id)).fetchall()
    return len(rows)



# #969's statement, verbatim -- this is the `derived=False` rollback, and it must never
# change shape after this line, or the rollback stops being byte-for-byte (2026-09-30 plan,
# Task 1). Positional params: (company_id, site_id, attend_date).
_EXPLICIT_SQL = (
    "SELECT DISTINCT p.id FROM speaker_voiceprints p "
    "JOIN site_attendance a ON a.company_id = p.company_id "
    "  AND (p.id = a.voiceprint_id "
    "       OR (p.user_id IS NOT NULL AND p.user_id = a.user_id) "
    "       OR lower(p.display_name) = lower(a.display_name)) "
    "WHERE p.company_id = %s AND p.status <> 'withdrawn' "
    "  AND a.site_id = %s AND a.attend_date = %s"
)

# The derived roster (design: docs/superpowers/specs/2026-09-30-derived-roster-design.md,
# "Review outcome" section). A UNION of #969's explicit rows with two more facts the system
# already holds: people who recorded at this site on this NZ day (arm 2, Task 2), and people
# a human named at this site in the last N NZ days (arm 3, Task 3). Named params throughout,
# because the arms share %(co)s/%(site)s/%(day)s/%(lookback)s and a positional list that long
# is unreadable and easy to miscount.
#
# For now (Task 1) this is the explicit arm alone, rewritten with named params -- Task 2 and
# Task 3 append their `UNION` legs beneath it. Even with only one arm, `derived=True` (the
# default) is NOT the same statement object as `_EXPLICIT_SQL`: it is deliberately its own
# text, so a future edit to one does not silently change the other's rollback guarantee.
_DERIVED_SQL = (
    "SELECT DISTINCT p.id FROM speaker_voiceprints p "
    "JOIN site_attendance a ON a.company_id = p.company_id "
    "  AND (p.id = a.voiceprint_id "
    "       OR (p.user_id IS NOT NULL AND p.user_id = a.user_id) "
    "       OR lower(p.display_name) = lower(a.display_name)) "
    "WHERE p.company_id = %(co)s AND p.status <> 'withdrawn' "
    "  AND a.site_id = %(site)s AND a.attend_date = %(day)s "

    # Arm 2 (Task 2): people who recorded at this site on this NZ day. `recordings` has no
    # date column -- the day is the S3 key's date segment (correction 1: every other reader
    # in repositories/recordings.py dates this way, and the key is the device's own wall
    # clock, the same clock the matcher's `date` is on), never `started_at` (UTC, and a
    # different clock). `kind IN ('audio','video')` excludes photo-only presence (correction
    # 8). The recorder is matched to a profile by directory account first, then by full name
    # via `concat_ws` -- not `||`, which is NULL-poisoned by a missing last name -- because
    # most profiles carry `user_id IS NULL` (correction 5).
    "UNION "
    "SELECT p.id FROM recordings r "
    "JOIN users u ON u.id = r.user_id "
    "JOIN speaker_voiceprints p ON p.company_id = r.company_id "
    " AND (p.user_id = r.user_id "
    "      OR (p.user_id IS NULL "
    "          AND lower(p.display_name) = lower(concat_ws(' ', u.first_name, u.last_name)))) "
    "WHERE r.company_id = %(co)s AND r.site_id = %(site)s "
    "  AND r.kind IN ('audio', 'video') "
    "  AND split_part(r.s3_key, '/', 4) = %(day)s "
    "  AND p.status <> 'withdrawn'"
)


def on_roster_profile_ids(conn, company_id, site_id, attend_date,
                          derived=True, lookback_days=14) -> set:
    """Every voiceprint profile the roster puts on site that day, resolved NOW rather than
    read off whatever `upsert` stored (module docstring).

    `derived=False` runs `_EXPLICIT_SQL` -- #969's own statement, unchanged -- and is the
    rollback the design's switch promises: byte-for-byte, not merely equivalent, because a
    company that turns the derived roster off must get back exactly what it had before this
    feature existed, forever, even as the derived arms grow.

    `derived=True` (the default) runs `_DERIVED_SQL`, the union of:

      * the explicit arm (`site_attendance` rows, unchanged) --
        `p.id = a.voiceprint_id` / `p.user_id = a.user_id` / name match;
      * arm 2 (Task 2): people who recorded at this site on this NZ day;
      * arm 3 (Task 3): people a human named at this site in the last `lookback_days` NZ
        days, anchored on `attend_date` -- not on `now()` -- so re-running an old session is
        reproducible (correction 3).

    `status <> 'withdrawn'` on the profile side, same rule as `profiles_for_matching`: a
    withdrawn profile that still narrows a roster in its favour is not a withdrawal.

    Raises on a missing company_id (`_require_company`), on both paths. Returns `set()` when
    the roster is empty -- and the CALLER treats an empty set as "no roster", not "nobody"
    (the empty-list-means-no-filter trap, deliberately inverted here: an empty roster narrows
    nothing, per the spec's "narrows, never blocks").
    """
    _require_company(company_id)
    cur = conn.cursor(row_factory=dict_row)
    if not derived:
        rows = cur.execute(_EXPLICIT_SQL, (company_id, site_id, attend_date)).fetchall()
    else:
        rows = cur.execute(_DERIVED_SQL, {
            "co": company_id, "site": site_id, "day": attend_date,
            "lookback": lookback_days,
        }).fetchall()
    return {str(r["id"]) for r in rows}
