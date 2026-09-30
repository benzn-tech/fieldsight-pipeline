-- Report section modules: every published version of a module's text.
--
-- A template section built from a module stores {module: {key, hash}} and the
-- text itself; on save, org-api looks the pair up here and replaces the
-- section's purpose with the stored text (report_modules.pin_modules), so what
-- reaches the prompt is always a text we published.
--
-- company_id NULL = our standard version (synced from report_modules.STANDARD,
-- which is the source of truth for the current standard text). A company_id =
-- that company's own version, written by us when we tailor its reports; it is
-- found only for that company.
--
-- APPEND-ONLY. An old version stays because a template may still pin it; the
-- current one is the code's (standard) or the latest created_at (company).
CREATE TABLE IF NOT EXISTS report_modules (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id  uuid REFERENCES companies(id),
  key         text NOT NULL,
  hash        text NOT NULL,
  title       text NOT NULL,
  kind        text NOT NULL,
  columns     jsonb,
  purpose     text NOT NULL,
  created_by  uuid REFERENCES users(id),
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS report_modules_version_uq
  ON report_modules (coalesce(company_id, '00000000-0000-0000-0000-000000000000'::uuid), key, hash);

CREATE INDEX IF NOT EXISTS idx_report_modules_company_key
  ON report_modules (company_id, key, created_at DESC);
