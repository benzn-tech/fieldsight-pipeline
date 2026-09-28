-- One stored sample per (profile, audio, window).
--
-- The same window of the same recording was being stored twice. Measured on TEST
-- 2026-09-27: six (s3_key, window) pairs on Ben Lin's profile, each held twice with a
-- byte-identical vector. Three ways in, all through `add_sample`, none of them wrong on
-- its own:
--
--   * the same passage renamed twice                  -> correction + correction
--   * the same cluster propagated twice                -> propagation + propagation
--   * passage A harvests B, later B is renamed itself  -> propagation + correction
--
-- It is not harmless. Profiles are matched on the MEAN of their samples, so a window held
-- twice counts twice, and one noisy window renamed twice pulls the profile toward itself.
-- And a withdrawal audit asking "how many recordings is this profile made of" gets the
-- wrong answer.
--
-- KEPT when collapsing: a human assertion over a propagation (the distinction `source`
-- exists to keep -- deleting a bad batch must not delete somebody's assertion), then a
-- quarantined copy over a live one (the conservative reading; on TEST every pair already
-- agreed), then the oldest.
--
-- Rows without an address (NULL s3_key or window) are left alone and not constrained:
-- nothing says two of them are the same window, and the index predicate below must match
-- `add_sample`'s ON CONFLICT clause word for word or Postgres will not infer it.
DELETE FROM speaker_voiceprint_samples s
USING (
    SELECT id,
           row_number() OVER (
               PARTITION BY voiceprint_id, s3_key, window_start_s, window_end_s
               ORDER BY (source = 'correction') DESC,
                        (quarantined_at IS NOT NULL) DESC,
                        created_at, id) AS rn
      FROM speaker_voiceprint_samples
     WHERE s3_key IS NOT NULL AND window_start_s IS NOT NULL AND window_end_s IS NOT NULL
) d
WHERE s.id = d.id AND d.rn > 1;

CREATE UNIQUE INDEX IF NOT EXISTS speaker_voiceprint_samples_one_per_window
    ON speaker_voiceprint_samples (voiceprint_id, s3_key, window_start_s, window_end_s)
    WHERE s3_key IS NOT NULL AND window_start_s IS NOT NULL AND window_end_s IS NOT NULL;
