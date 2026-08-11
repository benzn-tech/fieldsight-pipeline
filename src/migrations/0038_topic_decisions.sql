-- Decisions extracted per topic. lambda_extract_session has produced a
-- `decisions` array since the extraction schema was written; nothing has ever
-- stored it, so 9% of topics carried a decision that never reached the
-- database (measured: 101 of 1,127 topics across 90 real extractions).
-- See docs/superpowers/specs/2026-08-11-decisions-are-discarded-design.md
--
-- Shape follows findings (0010): a child of topics, cascading, so item-writer's
-- delete-by-source_s3_key idempotency covers it for free -- decisions have no
-- dedup of their own.
--
-- site_id is deliberately NOT carried (findings has one): decisions reach a
-- site through topics, which already cascades from sites. A future
-- site-scoped query should add the column deliberately rather than assume it
-- is already there.
CREATE TABLE IF NOT EXISTS topic_decisions (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  topic_id    uuid NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
  decision    text NOT NULL,
  rationale   text,
  decided_by  text,
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_topic_decisions_topic ON topic_decisions (topic_id);
