-- 0031: multi-device session merge (spec docs/superpowers/specs/2026-08-04-multi-device-session-merge-design.md).
--
-- The group's identity IS the lead device's session_id. Nothing has to be
-- allocated, so a group can form with no connectivity at all -- which is the
-- entire reason joining is a QR scan rather than a server-issued token. A
-- design that made group formation require a network round-trip would throw
-- that away.
--
-- NULL = a solo recording, which is every row that exists today. Additive and
-- idempotent: no backfill, no behaviour change for single-device sessions.
ALTER TABLE meeting_session ADD COLUMN IF NOT EXISTS group_id text
  REFERENCES meeting_session(session_id);

-- Partial index: only grouped sessions are ever looked up this way, and the
-- overwhelming majority of rows are solo.
CREATE INDEX IF NOT EXISTS idx_meeting_session_group
  ON meeting_session (group_id) WHERE group_id IS NOT NULL;
