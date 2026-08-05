-- Multi-device merge (spec 2026-08-04): when one member answers "the meeting has
-- ended", every other device has to be told to stop. This records that answer.
--
-- Nullable and purely additive: existing rows read as "not ended", which is what
-- every solo session is and always will be. Nothing reads this column until the
-- devices are wired to act on it, so this is inert on arrival.
ALTER TABLE meeting_session ADD COLUMN IF NOT EXISTS group_ended_at timestamptz;
