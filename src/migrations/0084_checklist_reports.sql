-- Checklist reports made automatically from a spoken check (owner, 2026-10-06):
-- "starting the concrete pre-pour check" fills the company's Concrete Pre-pour
-- Checklist on its own, so the report is waiting when he is back at the
-- office -- no picking topics, no dialog. One row per check that matched a
-- checklist template and was sent to the report worker; the worker's result
-- (status, the Word file) is at result_key.
--
-- The unique key is what keeps a re-extraction from making the same report
-- twice: the same recording, the same checklist, the same start.
CREATE TABLE IF NOT EXISTS checklist_reports (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id     uuid NOT NULL REFERENCES companies(id),
  user_folder    text NOT NULL,
  report_date    date NOT NULL,
  session        text NOT NULL,
  template_id    uuid REFERENCES report_templates(id) ON DELETE SET NULL,
  template_name  text NOT NULL,
  check_name     text NOT NULL,
  start_at       text NOT NULL,
  end_at         text,
  segments       jsonb,
  request_id     text NOT NULL,
  result_key     text NOT NULL,
  created_at     timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT checklist_reports_once UNIQUE (session, template_id, start_at)
);

CREATE INDEX IF NOT EXISTS idx_checklist_reports_day
  ON checklist_reports (company_id, user_folder, report_date);
