-- 0064 — a sample that does not sound like the person it was filed under stops matching.
--
-- ## The failure this exists for, measured 2026-09-23
--
-- A profile was enrolled from a real recording of the right person that happened to be full
-- of wind noise. The stored vector turned out to carry no speaker information at all: it sat
-- at −0.020 from that person's own clean samples, 0.037 from a second person's and 0.038
-- from a third's. Equidistant from everybody is the signature of a vector that describes the
-- CONDITION rather than the voice.
--
-- `aggregate_scores` averages a person's samples, so that one vector pulled every score for
-- that person down: measured on the same session, mean 0.257 with it removed and 0.192 with
-- it in — and nothing anywhere said so. The profile looked healthy: 12 samples, more than
-- anybody else's, and the highest sample count in the company.
--
-- ## Why the existing guards could not catch it
--
--   * the homogeneity guard (0.35) asks "does this window hold ONE voice". Wind noise is
--     uniform, so every frame agrees with every other and the window passed at 0.335.
--     Consistent rubbish is still consistent.
--   * `add_sample`'s agreement guard refuses a vector that resembles ANOTHER profile more
--     than its own. At that moment no other profile in the company held a single vector, so
--     there was nothing to be closer to. The company's first enrolments are unprotected by
--     construction.
--
-- Both guards judge a sample in isolation, at the moment it arrives. This one judges it
-- against the profile it is joining, and can be re-run as the profile grows — which is the
-- only way to catch a sample that was the ONLY evidence when it landed and is an outlier now.
--
-- ## Quarantine, not deletion
--
-- `withdraw` deletes vectors because somebody asked for their biometric data to be destroyed.
-- This is a different act: the data is still lawfully held, it is simply not evidence about
-- this person. Deleting it would throw away the one artifact that shows why a profile behaved
-- the way it did, and would make the decision unreviewable — while an inert row can be
-- reinstated when the clustering improves or a person says it was right after all.
--
--   quarantined_at      when it stopped counting; NULL = live, and live is the default so
--                       every existing row keeps behaving exactly as it does today
--   quarantine_reason   why, in the words of the rule that fired
--   quarantine_score    its mean similarity to the samples that stayed, so the decision can
--                       be argued with rather than only obeyed (the lesson `frame_spread`
--                       records: a refusal that says only "no" cannot be calibrated)
ALTER TABLE speaker_voiceprint_samples
    ADD COLUMN IF NOT EXISTS quarantined_at    timestamptz,
    ADD COLUMN IF NOT EXISTS quarantine_reason text,
    ADD COLUMN IF NOT EXISTS quarantine_score  double precision;

-- The read every matching query makes: live samples for a profile. Partial, because a
-- quarantined row answers nothing and the live ones are what every fetch wants.
CREATE INDEX IF NOT EXISTS speaker_voiceprint_samples_live
    ON speaker_voiceprint_samples (voiceprint_id)
    WHERE quarantined_at IS NULL;

COMMENT ON COLUMN speaker_voiceprint_samples.quarantined_at IS
  'Set when this sample was judged not to be evidence about its profile''s person. NULL is '
  'live. Quarantine rather than deletion: the data is still lawfully held and the row is the '
  'only record of why the profile behaved as it did. Reversible by clearing this column.';
COMMENT ON COLUMN speaker_voiceprint_samples.quarantine_score IS
  'Mean cosine to the samples that stayed. Stored so the decision can be argued with: a '
  'threshold nobody can see the distance to is a threshold nobody can calibrate.';
