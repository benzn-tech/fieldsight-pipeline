CREATE TABLE topics (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  site_id       uuid NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  user_id       uuid REFERENCES users(id),
  source_s3_key text,
  report_date   date NOT NULL,
  occurred_at   timestamptz,
  category      text,
  title         text NOT NULL,
  summary       text,
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE action_items (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  topic_id    uuid NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
  site_id     uuid NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  text        text NOT NULL,
  responsible text,
  deadline    date,
  priority    text,
  status      text NOT NULL DEFAULT 'open',
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE safety_observations (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  topic_id    uuid NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
  site_id     uuid NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  observation text NOT NULL,
  risk_level  text,
  location    text,
  status      text NOT NULL DEFAULT 'open',
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE topic_photos (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  topic_id     uuid NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
  s3_key       text NOT NULL,
  caption_text text,
  created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_topics_site_date ON topics (site_id, report_date);
CREATE INDEX idx_action_items_site_status ON action_items (site_id, status);
CREATE INDEX idx_safety_site_status ON safety_observations (site_id, status);
CREATE INDEX idx_topic_photos_topic ON topic_photos (topic_id);
