-- 0009: recordings — per-recording media metadata pushed by the mobile app
-- (GrandTime), with app-tagged site attribution. This is the finest-grain,
-- highest-confidence site signal (source='app'); the coarse recording_sessions
-- override ledger (identity Phase 4b) is a separate concern and NOT built here.
-- spec: GrandTime docs/superpowers/specs/2026-07-13-sp4-upload-project-selection-design.md §4
CREATE TABLE recordings (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id   uuid NOT NULL REFERENCES companies(id),
  user_id      uuid NOT NULL REFERENCES users(id),
  site_id      uuid REFERENCES sites(id),
  kind         text NOT NULL CHECK (kind IN ('video','audio','photo')),
  s3_key       text NOT NULL,
  client_uuid  text NOT NULL,
  started_at   timestamptz NOT NULL,
  ended_at     timestamptz,
  duration_s   numeric,
  resolution   text,
  codec        text,
  size_bytes   bigint,
  gps_track    jsonb,
  uploaded_at  timestamptz,
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uq_recordings_s3_key ON recordings (s3_key);
CREATE UNIQUE INDEX uq_recordings_client_uuid ON recordings (user_id, client_uuid);
CREATE INDEX idx_recordings_site ON recordings (site_id);
CREATE INDEX idx_recordings_user_started ON recordings (user_id, started_at);
