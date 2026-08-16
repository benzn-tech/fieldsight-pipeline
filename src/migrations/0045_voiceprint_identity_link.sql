-- 0045 — how a voiceprint came to be attached to a person.
--
-- `speaker_voiceprints.user_id` has been nullable since 0038 and nothing has ever written
-- it, which is why the site-scoped branch of `profiles_for_matching` has been a no-op since
-- the day it was written: every profile takes its `p.user_id IS NULL` escape.
--
-- Filling it is an identity claim about a real person, and `users.kind = 'field_only'` means
-- it reaches people with no account, who cannot see or object to it. The claim itself is not
-- new — the directory entry and the name on every report already make it — but the biometric
-- link is, and the least this can do is record how it was made.
--
-- Same discipline as `consented_by` in 0042: when somebody asks why the system believes a
-- voice belongs to a named person, there should be an answer. Recording it costs nothing
-- today and cannot be reconstructed later.
--
--   linked_by  the user whose correction caused the link
--   linked_at  when
--   linked_on  which rule matched: folder_name | full_name | first_name
ALTER TABLE speaker_voiceprints
    ADD COLUMN IF NOT EXISTS linked_by uuid,
    ADD COLUMN IF NOT EXISTS linked_at timestamptz,
    ADD COLUMN IF NOT EXISTS linked_on text;
