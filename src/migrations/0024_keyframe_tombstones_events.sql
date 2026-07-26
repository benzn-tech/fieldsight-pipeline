-- src/migrations/0024_keyframe_tombstones_events.sql
-- Video-keyframe plan Q7 (2026-07-24 spec). Two concerns:
--
-- keyframe_tombstones: a reviewer deleted this auto-keyframe; the generator
-- (lambda_keyframe) must never recreate it. Keyed on the full s3_key -- the
-- one identifier that survives item-writer's delete-then-reinsert topic churn
-- (topic ids are NOT stable across re-extractions, which is also why topic_id
-- below is informational and deliberately NOT a foreign key: an FK would
-- cascade the tombstone away on the re-extraction it exists to survive).
--
-- keyframe_events: privacy-preserving generation/deletion telemetry, one row
-- per frame actually produced and per frame a human deleted. Mirrors
-- 0023_classification_feedback's posture: ONLY structural signal (category,
-- duration, frame position) -- never the image, never transcript text, never a
-- per-user identity beyond company/site. Ratio = deleted/generated sliced by
-- (topic_category, duration_min, frame_index) is the fine-tune-hunting query.
CREATE TABLE keyframe_tombstones (
  s3_key        text PRIMARY KEY,
  company_id    uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  topic_id      uuid,                    -- topic at delete time; NO FK (churns)
  deleted_by    uuid REFERENCES users(id),
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE keyframe_events (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id          uuid REFERENCES companies(id) ON DELETE CASCADE,
  site_id             uuid,              -- no FK: append-only log outlives sites
  topic_category      text,              -- topics.category (coarse enum-ish)
  work_class          text,              -- topics.work_class ('work'/'non_work'/NULL)
  duration_min        int,               -- topic window length
  n_frames_generated  int,               -- frames this topic requested (capped 10)
  frame_index         int,               -- 0-based position of THIS frame
  event               text NOT NULL CHECK (event IN ('generated', 'deleted')),
  created_at          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_keyframe_events_slice
  ON keyframe_events (event, topic_category, duration_min, frame_index);
CREATE INDEX idx_keyframe_events_company
  ON keyframe_events (company_id, created_at);
