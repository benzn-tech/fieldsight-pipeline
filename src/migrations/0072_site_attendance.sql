-- Who is on site, per (site, NZ day) -- the roster the voice matcher narrows against.
--
-- Design: docs/superpowers/specs/2026-09-30-on-site-roster-design.md ("Review outcome"
-- section supersedes the earlier ones). Plan: docs/superpowers/plans/2026-09-30-
-- on-site-roster.md, corrections 1-9.
--
-- `attend_date` is an NZ calendar day (nz_time.py), never a UTC date. A manual entry
-- carries the date the client displayed; a connector (Phase 2/3) converts its source
-- timestamp with `nz_time.to_nz(...).date()` before writing here. Storing a UTC date
-- would misplace every sign-in between 00:00 and about 13:00 NZ time onto yesterday
-- (BUG-19/BUG-37) -- exactly the class of bug this column exists to avoid re-introducing.
--
-- `user_id` / `voiceprint_id` are RESOLUTION HINTS, written when `upsert` can resolve
-- them at write time -- they are not the roster's identity. The writer's lookup
-- (`on_roster_profile_ids`) re-resolves at LOOKUP time by three arms (profile id, user
-- id, or lower(display_name)), because a profile enrolled AFTER the roster was written
-- (the roadmap's "sign in at the gate = enrol your voice") must still be found on a day
-- whose roster predates the enrolment. Storing only a resolved id once would make such a
-- profile invisible to every roster written before it existed.
--
-- `voiceprint_id` is `ON DELETE SET NULL`, not CASCADE: a voiceprint withdrawal must not
-- delete the fact that a person was recorded on site that day. The attendance row is a
-- fact about the day; the profile is a separate, revocable thing.
--
-- `source` is a closed set (CHECK) because every consumer branches on it: `manual` rows
-- alone are removable by `DELETE /attendance/{id}` (correction 7), and the others are
-- each one connector's own to remove or overwrite via `attendance_upsert` (Phase 2/3).
--
-- UNIQUE (company_id, source, source_ref, attend_date) is the idempotency key. It
-- INCLUDES attend_date on purpose (correction 8): the same sign-in reported on both sides
-- of NZ midnight (a night-shift sign-in, or a source that reports in UTC while we resolve
-- to NZ) yields two rows, one per NZ day the person was actually present.
CREATE TABLE IF NOT EXISTS site_attendance (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id      uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    site_id         uuid NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    attend_date     date NOT NULL,
    display_name    text NOT NULL,
    user_id         uuid REFERENCES users(id),
    voiceprint_id   uuid REFERENCES speaker_voiceprints(id) ON DELETE SET NULL,
    employer_name   text,
    source          text NOT NULL CHECK (source IN
                        ('graph_calendar', 'signonsite', 'hammertech', '1breadcrumb',
                         'manual')),
    source_ref      text NOT NULL,
    first_seen_at   timestamptz NOT NULL DEFAULT now(),
    last_seen_at    timestamptz NOT NULL DEFAULT now(),
    created_by      uuid REFERENCES users(id),
    UNIQUE (company_id, source, source_ref, attend_date)
);

-- The roster lookup's own access path: every consumer asks "who is on site at THIS site
-- on THIS day", never "every row for this source" or "every row for this company".
CREATE INDEX IF NOT EXISTS site_attendance_site_day_idx
    ON site_attendance (company_id, site_id, attend_date);
