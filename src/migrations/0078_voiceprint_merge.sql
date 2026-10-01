-- 0078 -- "Same person as..." merge of two voiceprint profiles. Spec 2026-10-01
-- (docs/superpowers/specs/2026-10-01-voices-identity-and-merge-design.md, section 2).
--
-- A merged profile is NOT deleted and NOT given a new status: it becomes `withdrawn`, so every
-- existing `status <> 'withdrawn'` reader (matching, the listing, the partial unique index on
-- external identity) excludes it without change. These three columns are what tell a merge
-- from a deletion afterwards, and who did it.
--
-- `merged_into` has no ON DELETE clause on purpose: a target must not be hard-deleted while
-- a merged row still points at it, and nothing in the product hard-deletes profiles.
ALTER TABLE speaker_voiceprints
    ADD COLUMN IF NOT EXISTS merged_into uuid REFERENCES speaker_voiceprints(id),
    ADD COLUMN IF NOT EXISTS merged_at   timestamptz,
    ADD COLUMN IF NOT EXISTS merged_by   uuid REFERENCES users(id);
