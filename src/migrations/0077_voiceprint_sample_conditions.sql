-- 0077 — the recording conditions of every voiceprint sample. Spec 2026-09-30 (step 1 of 3,
-- "condition-aware profiles"). This step records facts only; it changes NO matching behaviour.
--
-- Measured on the 42 owner-labelled clips (2026-09-30): signal-to-noise ratio vs match score
-- has Spearman rho +0.76 for Ben Lin's genuine clips (+0.63 after removing clip-length effects);
-- background noise alone is rho -0.70. Without these numbers on the sample row, neither "does a
-- profile need a sample per condition" (step 2) nor "condition-matched comparison" (step 3) can
-- be measured on live data.
--
-- Device is deliberately NOT a column here: it is derivable from `s3_key`
-- (`users/{folder}/...`) and a copy would drift from the source of truth.
--
-- NULL for every row before this migration, and for any row an older producer keeps writing
-- (the embedder is the only thing that can compute these three numbers -- see add_sample's
-- COALESCE semantics on a repeat window, which must not let a producer without them blank out
-- values a producer with them already stored).
ALTER TABLE speaker_voiceprint_samples
    ADD COLUMN IF NOT EXISTS level_dbfs double precision,
    ADD COLUMN IF NOT EXISTS noise_dbfs double precision,
    ADD COLUMN IF NOT EXISTS snr_db     double precision;
