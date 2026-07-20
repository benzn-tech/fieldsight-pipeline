-- src/migrations/0019_content_edits.sql
-- Editable content correction (spec §5.2): generalized before/after edit
-- history across the item-store tables (topics / action_items / findings /
-- safety_observations). NOT a mirror of 0017 (which only added last-writer
-- columns to action_items) -- this is a first-class history table.
--
-- (table_name, row_id) is a SOFT polymorphic reference with NO foreign key:
-- the structured row it points at can be superseded/deleted by nightly ingest
-- re-extraction, and the audit trail must outlive that. company_id gives the
-- tenant scope the /history endpoint filters on. before_text/after_text are
-- the whole-field values (D1 free-text editing), nullable because a field can
-- go from/to empty.
CREATE TABLE content_edits (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id    uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  table_name    text NOT NULL,
  row_id        uuid NOT NULL,
  field         text NOT NULL,
  before_text   text,
  after_text    text,
  actor_user_id uuid REFERENCES users(id),
  actor_role    text,
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_content_edits_row ON content_edits (table_name, row_id, created_at);
CREATE INDEX idx_content_edits_company ON content_edits (company_id, created_at);
