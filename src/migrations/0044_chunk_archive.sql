-- A deleted recording's search index is MOVED, not filtered.
--
-- Spec: docs/superpowers/specs/2026-08-14-user-deletes-a-recording.md
--
-- WHY THIS EXISTS: the read-time filter shipped with the delete feature and it does not
-- reach the rows that matter.
--
--   * `lambda_ingest` stamps EVERY chunk with `source_s3_key = reports/{date}/{folder}/
--     daily_report.json`, so the tombstone's source arm -- which holds an
--     `extractions/...` prefix -- can never match. That arm is dead for chunks.
--   * The topic arm needs `report_chunks.topic_id`, and `chunk_transcripts` buckets turns
--     by `time_range +- 120s`. Turns that match no topic land in the "unassigned" bucket
--     with `topic_id = NULL`, where `NOT EXISTS (... r.target_id = c.topic_id)` is
--     trivially true. That bucket holds VERBATIM speech and it was fully searchable.
--
-- So a customer deleted a recording, saw it disappear, and the sentence stayed findable.
-- Filtering was the wrong mechanism: the rows have no reliable link to the thing being
-- deleted. Moving them does not need one -- `metadata.source_files` names the transcript
-- files every window came from, assigned or not, and those filenames carry the session id.
--
-- ARCHIVE RATHER THAN DELETE, because the feature's promise is that a delete is reversible.
-- Rebuilding a chunk on restore would mean re-embedding (the vectors arrive in a sidecar
-- keyed by sha256 of the text, which may be long gone) and re-running an ingest. Keeping
-- the row is exact, cheap, and cannot fail halfway.
--
-- `LIKE report_chunks` copies the column definitions and NOT NULLs. It deliberately copies
-- NO foreign keys (LIKE never does) and no indexes: an archived row must not be reachable
-- by CASCADE or SET NULL from a topic that gets superseded while it sits here, and it must
-- not be in the vector index -- being out of the index IS the point.

CREATE TABLE IF NOT EXISTS report_chunks_archive (
  LIKE report_chunks
);

-- Which delete took it, so one undelete restores exactly what one delete removed. Same
-- rule, and same column name, as `redactions.batch_id`.
ALTER TABLE report_chunks_archive ADD COLUMN IF NOT EXISTS batch_id uuid;
ALTER TABLE report_chunks_archive ADD COLUMN IF NOT EXISTS archived_at timestamptz NOT NULL DEFAULT now();

-- The restore path's only query.
CREATE INDEX IF NOT EXISTS idx_report_chunks_archive_batch
  ON report_chunks_archive (batch_id);

-- Recall, the same shape as the redactions one:
--   SELECT batch_id, count(*) FROM report_chunks_archive GROUP BY batch_id;
