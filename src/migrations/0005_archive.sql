-- Phase 3 batch 1: soft-delete (archive) support. NULL = active.
ALTER TABLE sites       ADD COLUMN archived_at timestamptz;
ALTER TABLE users       ADD COLUMN archived_at timestamptz;
ALTER TABLE memberships ADD COLUMN archived_at timestamptz;
