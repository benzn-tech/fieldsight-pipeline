CREATE TABLE report_chunks (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  site_id       uuid NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  user_id       uuid REFERENCES users(id),
  source_s3_key text,
  topic_id      uuid REFERENCES topics(id) ON DELETE SET NULL,
  report_date   date NOT NULL,
  chunk_type    text NOT NULL,
  chunk_text    text NOT NULL,
  embedding     vector(1024) NOT NULL,
  metadata      jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_report_chunks_embedding
  ON report_chunks USING hnsw (embedding vector_cosine_ops);

CREATE INDEX idx_report_chunks_site_date ON report_chunks (site_id, report_date);
