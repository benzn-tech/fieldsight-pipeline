-- 0016: site voice — realtime push-to-talk voice messages scoped to a site.
-- Two additive tables, no existing readers (safe on the shared cluster):
--   ws_connections  — one row per live API Gateway WebSocket connection.
--   voice_messages  — metadata-only delivery pointer. NO transcript / content
--                     column: Site voice is off-the-record (data-isolation
--                     invariant — never enters transcribe/report/RAG).
-- spec: docs/superpowers/specs/2026-07-18-site-voice-design.md
CREATE TABLE ws_connections (
  connection_id text PRIMARY KEY,
  user_id       uuid NOT NULL,
  company_id    uuid NOT NULL,
  connected_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_ws_connections_user ON ws_connections (user_id);
CREATE INDEX idx_ws_connections_connected ON ws_connections (connected_at);

CREATE TABLE voice_messages (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id     uuid NOT NULL,
  site_id        uuid NOT NULL,
  sender_user_id uuid NOT NULL,
  s3_key         text NOT NULL,
  duration_s     numeric,
  created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_voice_messages_site_created
  ON voice_messages (company_id, site_id, created_at DESC);
