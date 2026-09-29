-- Supersession instead of deletion for extraction topics. Spec 2026-09-24 §2.1.
ALTER TABLE topics ADD COLUMN IF NOT EXISTS superseded_at timestamptz;
ALTER TABLE topics ADD COLUMN IF NOT EXISTS superseded_by_run text;   -- "{tier}:{extracted_at}" of the pass that replaced it
CREATE INDEX IF NOT EXISTS idx_topics_live_source
    ON topics (source_s3_key) WHERE superseded_at IS NULL;

-- Stable child identity. DEFAULT so every existing row is valid at once.
ALTER TABLE action_items ADD COLUMN IF NOT EXISTS stable_id uuid NOT NULL DEFAULT gen_random_uuid();
ALTER TABLE action_items ADD COLUMN IF NOT EXISTS carried_from uuid;   -- the superseded row this took its stable_id from
ALTER TABLE findings     ADD COLUMN IF NOT EXISTS stable_id uuid NOT NULL DEFAULT gen_random_uuid();
ALTER TABLE findings     ADD COLUMN IF NOT EXISTS carried_from uuid;
CREATE INDEX IF NOT EXISTS idx_action_items_stable ON action_items (stable_id);
CREATE INDEX IF NOT EXISTS idx_findings_stable     ON findings (stable_id);

-- Room reserved on 2026-09-29 for the two customer segments (brainstorm round two), so the
-- segment work is a later migration ADDING CHECK values and payload keys, not re-keying rows.
-- `audience`: what may leave the company. 'internal' is the default and the only value any
-- writer sets today; 'owner' is set by a person (site manager) before an owner-facing
-- publish, never by the extractor alone. On every item table, because publishing is per item.
-- `kind` on findings: an observation today; 'instruction_received' / 'daywork_record' /
-- 'delay_event' are the subcontractor segment's event types, whose fields are NOT fixed yet
-- (they wait on the owner's phone calls) and will live in `payload` when they are.
ALTER TABLE findings ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'observation';
-- Postgres has no `ADD CONSTRAINT IF NOT EXISTS` for CHECK (syntax error) and no inline
-- `ADD COLUMN ... CHECK IF NOT EXISTS` either -- the column and its CHECK are added in two
-- statements. Safe to run unconditionally: this file is tracked in schema_migrations and
-- runs at most once (see src/db/migrate.py apply_migrations).
ALTER TABLE findings ADD CONSTRAINT findings_kind_check
    CHECK (kind IN ('observation','instruction_received','daywork_record','delay_event'));
ALTER TABLE findings ADD COLUMN IF NOT EXISTS payload jsonb;
ALTER TABLE findings ADD COLUMN IF NOT EXISTS audience text NOT NULL DEFAULT 'internal';
ALTER TABLE findings ADD CONSTRAINT findings_audience_check
    CHECK (audience IN ('internal','owner'));
ALTER TABLE action_items ADD COLUMN IF NOT EXISTS audience text NOT NULL DEFAULT 'internal';
ALTER TABLE action_items ADD CONSTRAINT action_items_audience_check
    CHECK (audience IN ('internal','owner'));

-- Decisions and questions become rows. Mirrors 0010 (site_id denormalised, CASCADE on both FKs).
-- These tables were declined on 2026-09-07 because ids churned; supersession is what makes them viable.
CREATE TABLE IF NOT EXISTS topic_decisions (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  stable_id   uuid NOT NULL DEFAULT gen_random_uuid(),
  carried_from uuid,
  topic_id    uuid NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
  site_id     uuid NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  decision    text NOT NULL,
  rationale   text,
  decided_by  text,
  audience    text NOT NULL DEFAULT 'internal' CHECK (audience IN ('internal','owner')),
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_topic_decisions_topic ON topic_decisions (topic_id);
CREATE TABLE IF NOT EXISTS topic_questions (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  stable_id   uuid NOT NULL DEFAULT gen_random_uuid(),
  carried_from uuid,
  topic_id    uuid NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
  site_id     uuid NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  question    text NOT NULL,
  status      text NOT NULL DEFAULT 'open' CHECK (status IN ('open','answered','dropped')),
  answered_by uuid REFERENCES users(id),
  answered_at timestamptz,
  audience    text NOT NULL DEFAULT 'internal' CHECK (audience IN ('internal','owner')),
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_topic_questions_topic ON topic_questions (topic_id);

-- One row per gated AI verdict, accepted or not, and the human's answer to it. Spec §2.2.
CREATE TABLE IF NOT EXISTS decision_records (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id      uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  site_id         uuid REFERENCES sites(id) ON DELETE CASCADE,
  kind            text NOT NULL,          -- programme_match | programme_impact | thread | work_class | ...
  subject_type    text NOT NULL,          -- topic | finding | action_item | decision | question
  subject_stable_id uuid NOT NULL,        -- topics.id for a topic (topics are not re-keyed); stable_id for children
  object_ref      text,                   -- the other side: task_id, earlier topic id, ...
  provider        text NOT NULL,          -- qwen | anthropic | typesafe | lexical | rule
  model           text,
  model_version   text,
  question_set    text,                   -- name+hash of the prompt or question set
  input_key       text,                   -- S3 key of what the model saw (match_requests/... ), never the text
  input_hash      text,
  output          jsonb NOT NULL,         -- probabilities / verdict as returned
  score           real,                   -- the single number the gate compared
  threshold       real,
  auto_outcome    text NOT NULL,          -- accepted | rejected | abstained
  human_outcome   text,                   -- confirmed | rejected | edited | NULL
  human_actor     uuid REFERENCES users(id),
  human_at        timestamptz,
  created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_decision_records_subject ON decision_records (subject_type, subject_stable_id);
CREATE INDEX IF NOT EXISTS idx_decision_records_kind_time ON decision_records (company_id, kind, created_at DESC);
