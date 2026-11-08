-- Track C slice 1 (spec 2026-11-08 §5.1, §5.4): one edge table for item relationships, and a
-- derived claim_type. Only supported_by is written by this slice; resolves (status line),
-- continues (continuity line) and supersedes are reserved for their owners' writers.
CREATE TABLE IF NOT EXISTS event_links (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  site_id             uuid NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  link_type           text NOT NULL
                        CHECK (link_type IN ('supported_by','resolves','continues','supersedes')),
  src_kind            text NOT NULL
                        CHECK (src_kind IN ('action_item','finding','decision','question')),
  src_stable_id       uuid NOT NULL,
  src_action_item_id  uuid REFERENCES action_items(id)    ON DELETE CASCADE,
  src_finding_id      uuid REFERENCES findings(id)        ON DELETE CASCADE,
  src_decision_id     uuid REFERENCES topic_decisions(id) ON DELETE CASCADE,
  src_question_id     uuid REFERENCES topic_questions(id) ON DELETE CASCADE,
  source_s3_key       text,            -- the extraction key of the pass that wrote it
  dst_kind            text NOT NULL
                        CHECK (dst_kind IN ('turn','action_item','finding','decision','question')),
  dst_stable_id       uuid,
  dst_turn            jsonb,           -- {source_stem, start_sec, end_sec, speaker_label, quote_status}
  method              text NOT NULL,
  score               real,
  rule_version        text NOT NULL,
  decision_record_id  uuid,
  created_at          timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT event_links_dst_shape CHECK (
    (dst_kind = 'turn') = (dst_turn IS NOT NULL AND dst_stable_id IS NULL)),
  CONSTRAINT event_links_dst_item CHECK (
    dst_kind = 'turn' OR dst_stable_id IS NOT NULL),
  CONSTRAINT event_links_supported_by_row CHECK (
    link_type <> 'supported_by' OR (
      (src_kind = 'action_item' AND src_action_item_id IS NOT NULL
         AND src_finding_id IS NULL AND src_decision_id IS NULL AND src_question_id IS NULL) OR
      (src_kind = 'finding' AND src_finding_id IS NOT NULL
         AND src_action_item_id IS NULL AND src_decision_id IS NULL AND src_question_id IS NULL) OR
      (src_kind = 'decision' AND src_decision_id IS NOT NULL
         AND src_action_item_id IS NULL AND src_finding_id IS NULL AND src_question_id IS NULL) OR
      (src_kind = 'question' AND src_question_id IS NOT NULL
         AND src_action_item_id IS NULL AND src_finding_id IS NULL AND src_decision_id IS NULL)))
);
CREATE INDEX IF NOT EXISTS idx_event_links_src ON event_links (src_stable_id, link_type);
CREATE INDEX IF NOT EXISTS idx_event_links_dst ON event_links (dst_stable_id) WHERE dst_stable_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_event_links_site_source ON event_links (site_id, source_s3_key);
CREATE INDEX IF NOT EXISTS idx_event_links_ai ON event_links (src_action_item_id) WHERE src_action_item_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_event_links_fi ON event_links (src_finding_id)     WHERE src_finding_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_event_links_de ON event_links (src_decision_id)    WHERE src_decision_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_event_links_qu ON event_links (src_question_id)    WHERE src_question_id IS NOT NULL;

-- Derived, never self-reported (assessment §2.4). NULL = unknown.
ALTER TABLE action_items    ADD COLUMN IF NOT EXISTS claim_type text;
ALTER TABLE action_items    ADD COLUMN IF NOT EXISTS claim_basis jsonb;
ALTER TABLE findings        ADD COLUMN IF NOT EXISTS claim_type text;
ALTER TABLE findings        ADD COLUMN IF NOT EXISTS claim_basis jsonb;
ALTER TABLE topic_decisions ADD COLUMN IF NOT EXISTS claim_type text;
ALTER TABLE topic_decisions ADD COLUMN IF NOT EXISTS claim_basis jsonb;
ALTER TABLE topic_questions ADD COLUMN IF NOT EXISTS claim_type text;
ALTER TABLE topic_questions ADD COLUMN IF NOT EXISTS claim_basis jsonb;
ALTER TABLE action_items    ADD CONSTRAINT action_items_claim_type_check
  CHECK (claim_type IN ('observed','stated','inferred'));
ALTER TABLE findings        ADD CONSTRAINT findings_claim_type_check
  CHECK (claim_type IN ('observed','stated','inferred'));
ALTER TABLE topic_decisions ADD CONSTRAINT topic_decisions_claim_type_check
  CHECK (claim_type IN ('observed','stated','inferred'));
ALTER TABLE topic_questions ADD CONSTRAINT topic_questions_claim_type_check
  CHECK (claim_type IN ('observed','stated','inferred'));
