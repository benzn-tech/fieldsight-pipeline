-- The voice vector re-bind already computes, kept instead of discarded.
--
-- `_rebind` builds one centroid per (call, label), clusters on them, and throws them away.
-- Nothing else in the system holds a vector for a PASSAGE -- `speaker_voiceprint_samples`
-- holds vectors for enrolled PEOPLE -- so any question of the form "which past passages
-- sound like this person" has, until now, meant re-reading the audio and re-embedding it:
-- measured at roughly 4 seconds per cluster including the S3 fetch, which makes a 72-hour
-- look-back a multi-minute job that grows with how busy the customer is.
--
-- Keeping the centroid turns that question into a vector comparison. The value is already
-- in memory at the moment the row beside it is written; not storing it was the only reason
-- it had to be recomputed.
--
-- NULLABLE, and that is load-bearing: every row written before this migration has no
-- centroid and never will, because the audio it summarised may since have expired. A reader
-- must treat NULL as "not cached", never as "no voice here" -- the two are the same absence
-- and only one of them is answerable.
--
-- No index. The candidate query is scoped to one company and a time window (default 72h),
-- which the existing (company_id, session_base) index already bounds to a handful of rows;
-- an ANN index over a set this size costs more to maintain than the scan it replaces, and
-- `speaker_voiceprint_samples` is unindexed for the same reason.
ALTER TABLE speaker_label_groups
    ADD COLUMN IF NOT EXISTS centroid vector(192);

COMMENT ON COLUMN speaker_label_groups.centroid IS
  'Unit-normalised mean of this (call, label) pair''s turn embeddings, as computed for '
  'clustering. NULL means the row predates the column or the re-bind stored none -- "not '
  'cached", never "silent". Cosine against it is comparable with speaker_voiceprint_samples.';
