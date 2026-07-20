-- src/migrations/0020_name_aliases.sql
-- Alias store = the glossary (spec §5.4, D5). A confirmed correction becomes a
-- scoped wrong->right alias. site_id NULL = company-wide. Affects FUTURE
-- normalize() at re-embed + RAG synthesis (and, later, B's Transcribe custom
-- vocabulary). NOT retroactively applied to historic content by default.
CREATE TABLE name_aliases (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id  uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  site_id     uuid REFERENCES sites(id) ON DELETE CASCADE,   -- NULL = company-wide
  wrong_term  text NOT NULL,
  right_term  text NOT NULL,
  kind        text NOT NULL DEFAULT 'other'
                CHECK (kind IN ('person', 'product', 'company', 'other')),
  source      text NOT NULL DEFAULT 'correction'
                CHECK (source IN ('correction', 'manual')),
  status      text NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'retired')),
  created_by  uuid REFERENCES users(id),
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_name_aliases_scope ON name_aliases (company_id, site_id, status);
