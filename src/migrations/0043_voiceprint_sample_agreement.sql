-- 0043 — what the sample agreed with, at the moment it was stored.
--
-- `upsert_profile` finds an existing profile by NAME. Two different people called "Leo",
-- both confirmed by the same person, land on one profile — and a profile cannot be
-- un-poisoned, only the contributing sample deleted. The poisoning is silent: every later
-- match runs against the mixture.
--
-- The obvious guard is a similarity threshold, and it is not available. A cross-session
-- absolute cosine is not a stable quantity here — Phase 0 measured the same person varying
-- by more than 0.2 across sessions, so any line drawn today would reject legitimate second
-- samples and would have been drawn from nothing.
--
-- So this records the two numbers a threshold would eventually be drawn FROM, on every
-- sample, at the moment it is stored:
--
--   agreement_own        cosine to the centroid of the samples this profile already had
--   agreement_best_other cosine to the nearest OTHER profile in the company
--   nearest_other_id     which profile that was
--
-- All nullable. The first sample of a profile has no `agreement_own` — there is nothing to
-- agree with — and a company with one profile has no `agreement_best_other`. Recording NULL
-- is the honest answer; recording 0.0 would put a fabricated value into the very dataset
-- the threshold is meant to come from.
--
-- The guard that ships alongside this uses only the ORDER of the two, never their values:
-- a sample closer to somebody else's profile than to the one it is joining is refused. That
-- comparison needs no constant, because both numbers are measured on the same audio and the
-- session-to-session drift is common to both.
ALTER TABLE speaker_voiceprint_samples
    ADD COLUMN IF NOT EXISTS agreement_own real,
    ADD COLUMN IF NOT EXISTS agreement_best_other real,
    ADD COLUMN IF NOT EXISTS nearest_other_id uuid;
