-- 0008: programme progress suggestions (daily items -> programme feedback,
-- human-confirmed; spec docs/superpowers/specs/2026-07-12-programme-item-feedback-design.md)
CREATE TABLE programme_progress_suggestions (
  id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  site_id              uuid NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  task_id              text NOT NULL,
  topic_id             uuid REFERENCES topics(id) ON DELETE SET NULL,
  topic_title          text NOT NULL,
  topic_summary        text,
  topic_user_id        uuid REFERENCES users(id),
  report_date          date NOT NULL,
  source_s3_key        text NOT NULL,
  task_name            text NOT NULL,
  task_status_before   text,
  task_progress_before smallint,
  suggested_status     text CHECK (suggested_status IN ('in_progress','completed','blocked','delayed')),
  suggested_progress   smallint CHECK (suggested_progress BETWEEN 0 AND 100),
  confidence           real NOT NULL,
  match_evidence       jsonb NOT NULL DEFAULT '{}'::jsonb,
  dedupe_key           text NOT NULL,
  state                text NOT NULL DEFAULT 'pending'
                       CHECK (state IN ('pending','confirmed','rejected','stale')),
  decided_by           uuid REFERENCES users(id),
  decided_at           timestamptz,
  applied_status       text,
  applied_progress     smallint,
  created_at           timestamptz NOT NULL DEFAULT now(),
  updated_at           timestamptz NOT NULL DEFAULT now(),
  CHECK (suggested_status IS NOT NULL OR suggested_progress IS NOT NULL)
);
CREATE UNIQUE INDEX uq_pps_dedupe ON programme_progress_suggestions (dedupe_key);
CREATE INDEX idx_pps_site_state ON programme_progress_suggestions (site_id, state, report_date);
CREATE INDEX idx_pps_topic ON programme_progress_suggestions (topic_id);
