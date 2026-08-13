-- A user deletes recordings, and everything derived from them stops being visible.
--
-- Spec: docs/superpowers/specs/2026-08-14-user-deletes-a-recording.md
-- Plan: docs/superpowers/plans/2026-08-14-user-deletes-a-recording.md (phase 1)
--
-- This reuses `redactions` rather than adding a second tombstone table: it is already
-- soft, reversible (`reverted_at`), and audited (actor + role). What it cannot do today is
-- accept the row this feature needs, and the spec's claim that "create_redaction already
-- takes scope, so this just adds a value" hid exactly that.
--
-- WHY THE TOMBSTONE IS KEYED ON THE SOURCE, NOT THE TOPIC
--
-- `lambda_ingest` deletes a day's topics by source prefix and re-inserts them with NEW
-- uuids whenever the nightly report supersedes the live extraction. A tombstone holding a
-- topic uuid therefore stops matching within a day, and the content the customer deleted
-- comes back overnight. 0022 even documents that topics get superseded -- there was simply
-- nothing for the tombstone to re-attach to. `target_key` is that anchor.
--
-- Constraint names below were read off the LIVE database, not guessed: 0022 wrote inline
-- CHECKs and Postgres auto-named them. Verified on fieldsight_test 2026-08-14 --
--   redactions_scope_check        CHECK (scope = ANY (ARRAY['analysis','all']))
--   redactions_target_type_check  CHECK (target_type = ANY (ARRAY['topic','segment','finding']))
-- and both databases hold only ('analysis','topic') rows, so neither widening can put an
-- existing row in violation.
--
-- Nothing here destroys anything. That is the whole premise of the feature: the S3 objects
-- and the rows stay, they simply stop being readable. A DROP COLUMN or DELETE in this file
-- would be the one irreversible step in a design built to be reversed.

ALTER TABLE redactions DROP CONSTRAINT IF EXISTS redactions_scope_check;
ALTER TABLE redactions ADD CONSTRAINT redactions_scope_check
  CHECK (scope IN ('analysis', 'all', 'deleted'));

-- 'recording' is the unit the customer selects ("用户删了视频 A,B,C,D"), and it is the unit
-- that survives re-extraction.
ALTER TABLE redactions DROP CONSTRAINT IF EXISTS redactions_target_type_check;
ALTER TABLE redactions ADD CONSTRAINT redactions_target_type_check
  CHECK (target_type IN ('topic', 'segment', 'finding', 'recording'));

-- One delete action = one batch. Without it, "one revert restores exactly what one delete
-- hid" cannot be implemented, and that sentence is the only check that proves the feature
-- is reversible at all.
ALTER TABLE redactions ADD COLUMN IF NOT EXISTS batch_id uuid;

-- The source anchor: an extraction/session prefix, which is what survives supersession.
-- `target_id` is `uuid NOT NULL` and cannot hold an S3 key, so this is a second column
-- rather than a widened one.
ALTER TABLE redactions ADD COLUMN IF NOT EXISTS target_key text;

-- A retried delete must not stack a second active tombstone: two rows for one target make
-- the revert count stop matching the delete count, and that count is the evidence.
-- Partial, so the pre-existing 'analysis' redactions (which legitimately repeat) are
-- untouched.
CREATE UNIQUE INDEX IF NOT EXISTS uq_redactions_active_deleted
  ON redactions (target_type, target_id)
  WHERE reverted_at IS NULL AND scope = 'deleted';

-- The read predicate's source arm looks this up on every filtered query.
CREATE INDEX IF NOT EXISTS idx_redactions_deleted_key
  ON redactions (target_key)
  WHERE reverted_at IS NULL AND scope = 'deleted';

-- Recall: everything this feature ever hid is one query --
--   SELECT * FROM redactions WHERE scope = 'deleted' AND reverted_at IS NULL;
-- and one batch is `AND batch_id = :id`. The marker is in the data on purpose: a feature
-- flag that is turned off leaves no way to find what it did.
