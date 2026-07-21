-- src/migrations/0022_redactions.sql
-- Life-conversation separation (2026-07-21 spec §4, from 2026-07-17 §3.4): a
-- redaction is a TOMBSTONE, never a hard delete. Original content is retained;
-- reverted_at IS NULL = active, reverting sets reverted_at (audit survives).
-- Company-tier reads exclude topics with an active redaction; the site/self
-- tier still reaches them (relocated to the "removed / personal" area).
-- target_type is 'topic' now; 'segment'/'finding' reserved for the future
-- segment-level upgrade. NO FK on target_id: the topic can be superseded by
-- nightly re-extraction and the tombstone must outlive that (mirrors 0019).
CREATE TABLE redactions (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id    uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  target_type   text NOT NULL DEFAULT 'topic'
                  CHECK (target_type IN ('topic', 'segment', 'finding')),
  target_id     uuid NOT NULL,
  reason        text NOT NULL,
  actor_user_id uuid REFERENCES users(id),
  actor_role    text,
  scope         text NOT NULL DEFAULT 'analysis'
                  CHECK (scope IN ('analysis', 'all')),
  created_at    timestamptz NOT NULL DEFAULT now(),
  reverted_at   timestamptz
);

CREATE INDEX idx_redactions_target ON redactions (target_type, target_id, reverted_at);
CREATE INDEX idx_redactions_company ON redactions (company_id, created_at);
