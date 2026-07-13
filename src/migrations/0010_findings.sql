-- 0010: findings (unified-extraction Task 3 — rich per-topic findings +
-- programme-impact link; spec docs/superpowers/specs/2026-07-13-unified-
-- extraction-labeling-design.md §4/§5, plan docs/superpowers/plans/
-- 2026-07-13-programme-impact-link.md.
-- The impact link lives HERE as columns (1 finding -> at most 1 task), NOT
-- in a second link table — programme_progress_suggestions (0008) stays the
-- only proposal/link table (spec §9: one link table).
CREATE TABLE findings (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  topic_id           uuid NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
  site_id            uuid NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  observation        text NOT NULL,
  domain             text CHECK (domain IN ('safety','quality','progress')),
  severity           text CHECK (severity IN ('none','minor','major')),
  entity_name        text,
  entity_trade       text,
  recommended_action text,
  programme_task_id  text,
  impact_severity    text CHECK (impact_severity IN ('none','minor','major')),
  impact_note        text,
  impact_task_name   text,
  impact_evidence    jsonb,
  impact_matched_at  timestamptz,
  status             text NOT NULL DEFAULT 'open',
  created_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_findings_topic ON findings (topic_id);
CREATE INDEX idx_findings_site_domain ON findings (site_id, domain);
CREATE INDEX idx_findings_task ON findings (programme_task_id) WHERE programme_task_id IS NOT NULL;
