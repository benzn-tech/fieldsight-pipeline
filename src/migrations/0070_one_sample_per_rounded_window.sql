-- One stored sample per (profile, audio, window ROUNDED TO THE SECOND).
--
-- 0069 made the window key exact, and that missed the case it was meant for. The same
-- passage renamed twice gives windows a few hundredths of a second apart -- measured on
-- TEST 2026-09-27, 7.22-17.22 s and 7.26-17.26 s of one recording on Sam Yu's profile:
-- the enrolment window is the tightest contiguous 10 s inside the turn, and the turn's
-- edges move slightly between two transcriptions of it. Two 10-second windows less than a
-- second apart share over 90% of their audio; as evidence they are one sample.
--
-- Rounding is not a tolerance: 7.49 and 7.51 still land on different keys. It is chosen
-- anyway because it keeps the rule inside a unique index, where a concurrent pair of
-- enrolments cannot both slip past it, and a near-miss costs one extra sample, not a
-- wrong one.
--
-- Same collapse order as 0069: a human assertion over a propagation, then a quarantined
-- copy, then the oldest. The predicate must still match `add_sample`'s ON CONFLICT clause
-- word for word or Postgres will not infer the index.
DELETE FROM speaker_voiceprint_samples s
USING (
    SELECT id,
           row_number() OVER (
               PARTITION BY voiceprint_id, s3_key, round(window_start_s), round(window_end_s)
               ORDER BY (source = 'correction') DESC,
                        (quarantined_at IS NOT NULL) DESC,
                        created_at, id) AS rn
      FROM speaker_voiceprint_samples
     WHERE s3_key IS NOT NULL AND window_start_s IS NOT NULL AND window_end_s IS NOT NULL
) d
WHERE s.id = d.id AND d.rn > 1;

DROP INDEX IF EXISTS speaker_voiceprint_samples_one_per_window;

CREATE UNIQUE INDEX IF NOT EXISTS speaker_voiceprint_samples_one_per_second
    ON speaker_voiceprint_samples
       (voiceprint_id, s3_key, round(window_start_s), round(window_end_s))
    WHERE s3_key IS NOT NULL AND window_start_s IS NOT NULL AND window_end_s IS NOT NULL;
