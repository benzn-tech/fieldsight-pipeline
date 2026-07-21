-- src/migrations/0021_topic_work_class.sql
-- Life-conversation separation (2026-07-21 spec §4): per-topic work/non_work
-- classification produced at extraction time. work_class NULL = legacy /
-- unclassified (enforcement treats NULL and 'work' alike). is_mixed marks a
-- topic holding both work and personal talk -- the quantitative trigger to
-- build segment-level separation later (reserved; no segment table now).
ALTER TABLE topics ADD COLUMN work_class text
  CHECK (work_class IN ('work', 'non_work'));
ALTER TABLE topics ADD COLUMN work_confidence real;
ALTER TABLE topics ADD COLUMN is_mixed boolean NOT NULL DEFAULT false;
