# Who is on site, derived from our own data (design, 2026-09-30)

**Owner decisions (2026-09-30):** do not build SignOnSite / HammerTech / 1Breadcrumb
integrations yet; the on-site roster (#969, TEST only) goes to prod only after the owner
has tested it successfully. A roster nobody fills in cannot be tested, and a site manager
will not type names in every morning — so the first real source is one that needs nobody.

**Line:** voiceprint. **Customer rule:** plain words, no scores.

## What

At match time the writer's `_profiles(site_id, date)` already returns `on_roster` per
profile from `site_attendance` (#969). Extend `on_roster_profile_ids` so the roster for
(site, NZ date) is the union of:

1. **Explicit rows** in `site_attendance` (manual today, vendors later) — unchanged.
2. **People who recorded at this site that day** — `recordings` rows with this `site_id`
   and this NZ date, dated by the S3 key's date segment (`split_part(s3_key, '/', 4)`),
   never `started_at` (`recordings` has no date column, and `started_at` is a different
   clock — see "Review outcome"), joined to the recorder's user → that user's live profile
   (`speaker_voiceprints.user_id`, or by full name when `user_id IS NULL`, the common
   case). The device wearer is nearly always on site.
3. **People recently confirmed at this site** — profiles with a live, human-sourced turn
   name (`speaker_turn_names.source = 'correction'`, not superseded) in a session at this
   site in the last `ROSTER_LOOKBACK_DAYS` (default 14) NZ days, via the `meeting_session` →
   `speaker_turn_names` ladder (a session's site is `meeting_session.site_id`, falling back
   to a recording of that session when the session was opened offline — see "Review
   outcome"). The lookback window is anchored on the roster day being queried, not on
   today (so re-running an old session is reproducible), and the name arm also matches a
   correction whose `voiceprint_id` is NULL. Regulars who were named here recently are
   very likely here again.

No migration: derived membership is computed at lookup, never written, so it can never go
stale and a withdrawn profile disappears from it immediately.

## Why these two sources

Both are facts the system already holds and a person already vouched for: a recording
uploaded from a site, and a human rename. Neither is inferred by the matcher itself — a
roster built from the matcher's own guesses would confirm its own mistakes.

## Behaviour it changes

With #969's rule, an off-roster full-pool winner is capped at `tentative`. A derived
roster is incomplete by design (a visitor who never recorded and was never named here is
absent), so the cost of a miss is a question mark on a correct name, never a wrong name.
Absent everything (new site, no recordings, no names) → the roster is empty → exactly
today's behaviour, as #969 guarantees.

**Correction 9 (plan):** a one-person derived roster changes behaviour at every site where
only the device wearer has a profile. With the wearer on the roster and nobody else,
`decide_with_roster` uses the full-pool result for the wearer (subset < 2 is exempted), but
caps **every other** full-pool winner at `tentative`. This is the one visible regression a
customer notices on day one — accepted per the owner decisions above, because no company
has a calibrated floor yet and names are tentative today regardless.

## Switch

`ROSTER_DERIVED` (default `on` in code). Wired through template + both workflows so prod
can be released with it `off` if the owner wants the explicit-only roster first. It must
be three-segment wired (repo variable → workflow → template Parameter), or the variable
does nothing.

## Tests that must go red without the change

- Recorder-at-site-today is on the roster; the same recorder at another site is not.
- A correction at this site 3 days ago puts the profile on the roster; 20 days ago does not;
  a propagation-sourced name does not; a superseded name does not; a withdrawn profile never.
- NZ date: a recording at 07:00 NZDT (previous UTC day) counts for the NZ day.
- `ROSTER_DERIVED=off` → only explicit rows, byte-for-byte #969.
- The SQL runs on `fieldsight_test` in a rolled-back transaction and returns Sam Yu for
  his 2026-08-08 site.

## How the owner tests it (TEST)

Rename a passage at a site on TEST, then record again at that site: the voice of the
person renamed is named with confidence only when they are on the derived roster, and an
unregistered visitor's voice is never confirmed as a roster member.

## Review outcome (Fable review, 2026-09-30) — supersedes the sections above where they differ

Adopted from the review (evidence in the plan): recordings are dated by the key segment
`users/{folder}/{kind}/{date}/…`, not `started_at`; a turn name's site comes through
`meeting_session.site_id`, falling back to `recordings.site_id` (BUG-41 authority) because
offline-opened sessions have NULL; the session day is `meeting_session.opened_at` in
Pacific/Auckland; `source='correction' AND superseded_at IS NULL`, resolved through the
voiceprint-id arm AND the name arm (voiceprint_id is often NULL); arm 2 also matches the
recorder's display name (most profiles have `user_id` NULL); the query leads from
`meeting_session` for performance; the switch has explicit wiring tests and the repository
takes `derived` / `lookback_days` as arguments.

Owner decisions, taken on the owner's standing instruction (long-term option):
1. An off-roster winner is capped at `tentative` even where the derived roster is small —
   accepted: no company has a calibrated floor yet, so names are tentative today anyway,
   and recently-confirmed regulars are on the roster.
2. Photos do not count as "recorded here" — audio/video only.
3. The lookback is anchored on the roster day, not on today, so re-running an old session
   is reproducible.
