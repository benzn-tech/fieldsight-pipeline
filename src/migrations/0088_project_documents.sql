-- Project documents (spec docs/superpowers/specs/2026-10-16-project-documents-design.md).
-- A project's specs and documents, kept apart from recordings: own tables, never
-- report_chunks (contamination rule 1). Additive only.
--
-- company_id is the owner at upload (sites.company_id then) and is used for S3 keys only;
-- the tenant is sites.company_id (project-owned tenancy). requested_at is reset by Retry
-- so "pending for more than 20 minutes" is measured from the last request, not the upload.
-- Search visibility: deleted_at IS NULL AND superseded_at IS NULL AND status = 'indexed'.
CREATE TABLE IF NOT EXISTS project_documents (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id    uuid NOT NULL,
  site_id       uuid NOT NULL REFERENCES sites(id),
  series_id     uuid NOT NULL,
  revision      int  NOT NULL DEFAULT 1,
  title         text NOT NULL,
  filename      text NOT NULL,
  source_key    text NOT NULL,
  content_type  text NOT NULL,
  status        text NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','indexed','unreadable','error')),
  reason        text,
  pages         int,
  chars         int,
  chunks        int,
  uploaded_by   uuid NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  requested_at  timestamptz NOT NULL DEFAULT now(),
  indexed_at    timestamptz,
  superseded_at timestamptz,
  deleted_at    timestamptz,
  CONSTRAINT project_documents_series_revision UNIQUE (series_id, revision)
);
CREATE INDEX IF NOT EXISTS project_documents_site
  ON project_documents (site_id) WHERE deleted_at IS NULL;

-- No approximate (graph) vector index (R12): project-document search is always filtered
-- by site, and a filtered approximate top-k drops rows; the search sorts exactly. No
-- company_id: the tenant comes through the document join.
CREATE TABLE IF NOT EXISTS project_doc_chunks (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  document_id uuid NOT NULL REFERENCES project_documents(id) ON DELETE CASCADE,
  site_id     uuid NOT NULL,
  ordinal     int  NOT NULL,
  heading     text,
  page_from   int,
  page_to     int,
  chunk_text  text NOT NULL,
  embedding   vector(1024) NOT NULL
);
CREATE INDEX IF NOT EXISTS project_doc_chunks_document
  ON project_doc_chunks (document_id, ordinal);
CREATE INDEX IF NOT EXISTS project_doc_chunks_site
  ON project_doc_chunks (site_id);
-- R11: 'simple' only, no Latin-only character stripping (0061's cleanup erases Chinese).
-- The repository's query repeats this expression character for character (tests/unit/test_project_docs_sql.py).
CREATE INDEX IF NOT EXISTS project_doc_chunks_tsv
  ON project_doc_chunks USING gin ((to_tsvector('simple', coalesce(heading, '') || ' ' || chunk_text)));
