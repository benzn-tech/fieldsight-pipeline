-- 0006: manual observations (batch B — report-side write backend, spec 2026-07-06)
CREATE TABLE observations (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id    uuid NOT NULL REFERENCES companies(id),
  kind          text NOT NULL CHECK (kind IN ('safety','quality')),
  site_slug     text NOT NULL,
  report_date   date NOT NULL,
  author_sub    text NOT NULL,
  author_name   text NOT NULL,
  observation   text NOT NULL,
  risk_level    text CHECK (risk_level IN ('low','medium','high')),
  recommended_action text,
  status        text NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed')),
  archived_at   timestamptz,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_obs_company_kind_date ON observations (company_id, kind, report_date);
CREATE INDEX idx_obs_site ON observations (company_id, site_slug, report_date);
