-- 0046 — what happened the last time somebody tried to enrol into this profile.
--
-- org-api creates the profile, then queues the work; the embedder decides whether the window
-- may be stored, and the writer records the outcome in a Lambda return value that nothing
-- reads. So a refusal reaches CloudWatch and stops there.
--
-- The person who did the correction sees `enrolment: "requested"` and never learns it was
-- declined. Worse, the two states are indistinguishable from outside: a profile with zero
-- samples is what you get whether the enrolment was refused on its merits or the embedder
-- died halfway. Both happened on TEST tonight and both looked the same.
--
-- Three columns rather than a log line, because the question "why is this profile empty" is
-- asked days later by somebody who does not have the request id.
--
--   last_attempt_at       when a window was last offered to this profile
--   last_attempt_outcome  stored | refused
--   last_attempt_detail   which rule refused it, in words meant for a person
ALTER TABLE speaker_voiceprints
    ADD COLUMN IF NOT EXISTS last_attempt_at timestamptz,
    ADD COLUMN IF NOT EXISTS last_attempt_outcome text,
    ADD COLUMN IF NOT EXISTS last_attempt_detail text;
