-- 0036_session_group.sql — per-group merge state for the multi-device merge
-- (spec docs/superpowers/specs/2026-08-08-group-merge-phase-c-design.md, Phase C).
--
-- One row per group, created when the FIRST JOINER joins.
--
-- The state deliberately does NOT live as columns on the lead's meeting_session
-- row, though that was the first design. Three reasons, all found in review:
--
--   * The lead row may never exist. Its /open is fire-and-forget at
--     record-start and a site is routinely offline then, so there may be
--     nothing to stamp and nothing to CAS against — the group would be
--     permanently unclaimable.
--
--   * The scan could not be both bounded and indexed. A lead carries no
--     group_id of its own (the group id IS its session id), so a scan keyed on
--     the lead row must enumerate DISTINCT group_id over every group ever
--     created and then join to test the flag — the bounding predicate ends up
--     on the joined row, so the scan never shrinks. Worse, a group that settles
--     with no content is never claimed and accumulates forever.
--
--   * Three consumers need the merged artifact's key (the timeline union,
--     item-writer's suppression check, ingest's defer test). Re-deriving the
--     folder + NZ date + lead-never-uploaded fallback in three places means any
--     drift is a silent miss, in a different direction each time. merged_key is
--     written once, here.
--
-- Not `group_ended_at` either: PR #276 already gave that a different meaning
-- (the lead stopped, refuse new members). A group is "ended" the moment the
-- lead presses stop, while joiners may still be uploading for hours; triggering
-- the merge on it would merge half a meeting.
CREATE TABLE IF NOT EXISTS session_group (
  group_id     text PRIMARY KEY,           -- the LEAD's session_id
  company_id   uuid NOT NULL,
  merged_at    timestamptz,                -- claimed; NULL means available to the scan
  merge_count  int NOT NULL DEFAULT 0,     -- incremented at CLAIM, not at re-arm
  merge_result text,                       -- NULL | 'merged' | 'rejected' | 'empty'
  merged_key   text,                       -- the authoritative merged artifact key
  created_at   timestamptz NOT NULL DEFAULT now()
);

-- The standing scan reads ONLY unresolved groups. A resolved group leaves the
-- candidate set permanently, so the per-tick cost does not grow with history.
CREATE INDEX IF NOT EXISTS idx_session_group_pending
  ON session_group (created_at) WHERE merge_result IS NULL;

-- "Which groups was this user a member of on this date" — needed by the
-- timeline union and by ingest's defer test. The existing
-- idx_meeting_session_group is keyed on group_id and answers the opposite
-- question, so it does not serve this lookup.
CREATE INDEX IF NOT EXISTS idx_meeting_session_group_user
  ON meeting_session (user_id) WHERE group_id IS NOT NULL;
