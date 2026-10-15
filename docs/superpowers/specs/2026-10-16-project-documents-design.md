# Project documents: specs and docs a project can be asked about

Status: design (2026-10-16). Sub-project 1 of the "project assets / template
digitisation / inspection gaps" batch (sub-projects 3B and 2 are on TEST).
Decisions taken by the controller under the owner's standing instruction
(2026-10-15: pick the long-term, reasonable option); each is marked
**Decision**.

## Why

Owner, 2026-10-15: extend the Library with a project-assets area that holds a
project's documents and specs, and eventually drawings. Start with text. Two
questions he asked, answered here:

1. **Do we need LandingAI ADE?** Not for text. **Decision:** Word, plain text
   and PDFs with a text layer are read locally (python-docx, pypdf), at no
   cost and without sending customer documents to another vendor. A scanned
   PDF (no text layer) is reported as unreadable in v1; reading it with the
   vision model already used for templates is a later step behind the same
   interface. Drawings are out of scope.
2. **How do we store and isolate it so search works and recordings and
   documents never contaminate each other?** Separate tables, separate
   retrieval tool, separate labels -- below.

## The contamination rules (the point of the design)

1. **Separate storage.** Documents live in `project_documents` and
   `project_doc_chunks`, never in `report_chunks`. Measured reason:
   `search_sql.family_predicate` files every row whose `source_s3_key` is not
   `extractions/%` under the "report" family, and `author_ids` narrowing keys
   on `user_id` -- a document row in `report_chunks` would appear in
   recorded-content search and Ask, and be mis-scoped for self-scoped roles.
   With separate tables, no existing reader can see a document by accident;
   every reader that wants documents asks for them explicitly.
2. **Extraction never reads documents.** Nothing in the extraction / report /
   checklist pipeline imports the documents repository. Pinned by a test that
   fails if any of those modules imports it. So "the spec says X" can never be
   written down as "on site they did X".
3. **Answers say where each fact came from.** In Ask, document excerpts are a
   separate reference family (`D1`, `D2`...) next to recordings (`R`) and web
   (`W`), rendered in their own labelled block in the UI -- "From project
   documents" -- with document title and page/clause, exactly as the web block
   is labelled "From the open web -- not from your recordings" today. The
   prompt states the rule: documents say what should be; recordings say what
   happened; never present one as the other.
4. **Document text is data.** Excerpts go inside the existing data fence and
   through `ask_orchestrator.sanitize` (neutralises ``` and forged ref
   prefixes). The legacy `build_rag_prompt` path does not get documents at all
   (it inserts chunk text raw).
5. **Superseded versions are invisible.** Uploading Rev.3 of a document
   supersedes Rev.2: its chunks stop being searched. Deleted documents
   likewise. Two contradictory revisions are never retrieved together.

## Data model (migration 0088, additive)

```sql
CREATE TABLE IF NOT EXISTS project_documents (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id uuid NOT NULL,
  site_id uuid NOT NULL REFERENCES sites(id),
  series_id uuid NOT NULL,          -- all revisions of one document share it
  revision int NOT NULL DEFAULT 1,
  title text NOT NULL,              -- defaults to the file name, editable
  filename text NOT NULL,
  source_key text NOT NULL,         -- project_doc_sources/{company}/{id}.{ext}
  content_type text NOT NULL,
  status text NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','indexed','unreadable','error')),
  reason text,                      -- unreadable/error, shown to people
  pages int, chars int, chunks int,
  uploaded_by uuid NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  indexed_at timestamptz,
  superseded_at timestamptz,
  deleted_at timestamptz
);
CREATE INDEX IF NOT EXISTS project_documents_site
  ON project_documents (site_id) WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS project_doc_chunks (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  document_id uuid NOT NULL REFERENCES project_documents(id) ON DELETE CASCADE,
  site_id uuid NOT NULL,
  ordinal int NOT NULL,
  heading text,                     -- "3.2 Formwork > Tolerances"
  page_from int, page_to int,
  chunk_text text NOT NULL,
  embedding vector(1024) NOT NULL
);
-- HNSW cosine on embedding; GIN on to_tsvector('simple', heading||' '||chunk_text);
-- btree (document_id, ordinal); btree (site_id).
```

Search visibility is `deleted_at IS NULL AND superseded_at IS NULL AND
status = 'indexed'` on the document. Tenant is `sites.company_id` (project-owned
tenancy, 2026-10-09); `company_id` on the document is the owner at upload and
used for S3 keys only.

## Flow

```
Project page "Documents" -> presigned PUT -> project_doc_sources/{company}/{id}.{ext}
  POST /sites/{site}/documents  (org-api, in-VPC): authorise, HEAD, insert row
     (status pending), write project_doc_requests/{company}/{id}.json
        | S3 event (scripts/wire-s3-events.sh)
  ProjectDocIndexFunction (NOT in VPC): extract text -> chunk -> embed (DashScope)
     -> project_doc_vectors/{company}/{id}.json
        | S3 event
  IngestFunction (in VPC): new route -> one transaction: insert chunks, set
     status/indexed_at/counts, set superseded_at on the previous revision
```

This is the existing index pattern (`index_requests/` -> EmbedReport ->
`index_vectors/` -> Ingest), chosen because org-api and Ingest are in the VPC
with no outbound (BUG-36) and DashScope needs outbound.

### Extraction (`project_doc_text.py`, pure functions)

- **DOCX** (python-docx layer): paragraphs in order; Word heading styles and
  numbered clause patterns (`^\d+(\.\d+)*\s`) start a new heading path; tables
  are flattened row by row (`cell | cell`).
- **PDF** (new pypdf layer, pure Python, published by
  `scripts/build-pypdf-layer.sh`, parameter `PdfLayerArn`): text per page;
  headings detected by the same clause pattern; page numbers kept. A PDF whose
  extracted text is under 200 characters per page on average is `unreadable`
  with "This looks like a scanned PDF -- scanned documents are not read yet."
- **TXT / MD**: as is; Markdown `#` headings start a heading path.
- Limits: 50 MB per file, 500 pages, 2 million characters; over -> unreadable
  with the reason.

### Chunking (`project_doc_chunks.py`)

Structure first: a chunk never spans two top-level headings; target 1,800
characters, hard cap 3,000, 150-character overlap within a section; each chunk
carries its heading path and page range. The heading path is prepended to the
text that is embedded (not to the stored `chunk_text`), so a clause found by
its heading is found by meaning too.

### Embedding

`dashscope_utils.embed` (text-embedding-v4, 1024 dims, batches of 10, its own
retries). A 500-page spec is ~1,000 chunks = 100 calls; the function's timeout
is 900 s; the vectors are written once at the end. **Decision:** no reuse of
vectors across revisions in v1 (YAGNI; cost is cents).

## API (org-api, behind `PROJECT_DOCUMENTS`, default off -> 404)

- `GET /sites/{site}/documents` -> current documents (latest revision per
  series, not deleted) with status, revision, counts, uploader, dates; plus
  `?series={id}` for a document's revision history.
- `POST /sites/{site}/documents/upload-url` `{filename, contentType, size}` ->
  presigned PUT (server-generated key, pinned content type, 15 min).
- `POST /sites/{site}/documents` `{sourceKey, title, seriesId?}` -> HEAD
  checks (prefix of the caller's company, exists, size), insert row
  (`revision = max+1` within the series when `seriesId` is given), write the
  request, `lake_objects` rows, 202 `{id, status: 'pending'}`.
- `PATCH /sites/{site}/documents/{id}` `{title}`.
- `DELETE /sites/{site}/documents/{id}` -> soft delete (`deleted_at`).
- `GET /sites/{site}/documents/{id}/file` -> presigned GET (1 h) for opening
  the original.

**Who.** Read (list, open, and documents in search/Ask): anyone who can see
the project (`_allowed_site_ids`). Upload, new revision, rename, delete:
`admin`, `gm`, `pm` on that project, and `platform_admin`. **Decision:** the
same managers who edit the programme (`_MANAGER_ROLES`); site managers and
workers read only.

## Search and Ask

- **Repository** `repositories/project_docs.py`: `search(conn, site_ids, query_vec,
  query_text, k)` -- vector + lexical arms over `project_doc_chunks` joined to
  visible documents, scoped by `site_ids` only (no author narrowing: documents
  belong to the project, not a person). Same hybrid shape as `search_sql`
  (vector distance threshold, `websearch_to_tsquery('simple')`).
- **rag-search**: a new mode `documents` on `lambda_rag_search` (same ACL code
  path, `scope.visible_scope`); the existing modes are unchanged.
- **Search palette** (fieldsight-ui): a separate "Project documents" group
  under the recording results, each hit showing document title, heading, page,
  and opening the original at that page (`#page=N` for PDFs).
- **Ask (orchestrator v2 only)**: a new tool `search_project_documents` in
  `ask_tools.TOOLS`; results enter the data fence as `D` refs with
  `title · heading · p.N`; `to_client` returns them in a separate `documents`
  block (like `web`), never mixed into recording cards. The system prompt gains
  the rule in "contamination rule 3". The orchestrator's routing decides when to
  call the tool (questions about requirements, specs, tolerances, "what does
  the spec say"); the judge never has to merge the two families.
- **Ask UI**: `renderDocumentOrigin` beside `renderWebOrigin` -- "From project
  documents -- what should be, not what happened on site", with links.

## Not in this spec

- Drawings, scanned PDFs (vision), images, spreadsheets.
- Comparing recordings against the spec ("checklist against the spec") --
  later, on top of this.
- Company-wide (non-project) documents.
- Sharing documents with external members (project-owned tenancy rules apply
  unchanged: whoever can see the project can read its documents).

## Error handling

- Extraction or embedding failure -> `status error` with a reason; the
  original stays; "Retry" re-writes the request (same id).
- Unreadable -> `status unreadable` with the reason; not searchable.
- A request whose document row is gone or deleted -> the worker writes
  nothing; Ingest ignores vectors for a deleted document.
- Ingest applies a document's chunks idempotently (delete-then-insert for that
  `document_id` in one transaction, advisory lock on the id).

## Testing

- Text extraction and chunking: unit tests with generated DOCX/PDF fixtures
  (python-docx, a small pypdf-written PDF) -- headings, clause numbers, tables,
  page ranges, the scanned-PDF rule, limits.
- Worker: real handler from an S3 event, DashScope stubbed at the HTTP
  boundary.
- Ingest route: through the fake connection AND a real-SQL integration test
  (`tests/integration`, pgserver locally) for insert, supersede and the
  visibility predicate -- the SQL must run somewhere (repo lesson).
- org-api: authorisation per role, cross-company keys refused, flag off 404,
  through a fake s3.
- Search: real-SQL test that a superseded / deleted / other-site document is
  never returned; that `report_chunks` search returns no document rows.
- Contamination: a test that the extraction/report/checklist modules do not
  import `repositories.project_docs` or `project_doc_*`.
- Ask: `D` refs never appear as recording cards; the documents block carries
  title/page; sanitised text.
- Frontend: node tests for the Documents section (list, upload, revision,
  delete, status), the search group, the Ask documents block.

## Rollout and other sessions

- Migration 0088 is additive (new tables only). It runs on TEST with the
  develop deploy; it reaches prod only via an owner-approved release to main.
- New function + layer + 2 prefixes + 2 S3 triggers (`project_doc_requests/`,
  `project_doc_vectors/`), wired by `wire-s3-events.sh` (auto on TEST; on prod
  via `PROD_WIRE_LAKE`), checked for prefix overlap against the live
  notification configs of both buckets.
- Flags: `PROJECT_DOCUMENTS` (org-api routes + rag-search mode + Ask tool),
  default false; TEST var `TEST_PROJECT_DOCUMENTS`, prod absent = off. The
  UI hides the Documents section and the Ask tool's block when the list route
  answers the flag-off 404.
- Touches shared hot files (`lambda_org_api.py`, `lambda_rag_search.py`,
  `ask_orchestrator.py`, `ask_tools.py`, `lambda_ingest.py`, `template.yaml`)
  that other sessions edit daily: branch from fresh `origin/develop`, keep each
  change to new functions plus one dispatch line, rebase right before merge.
- Prod hand steps (release notes): the pypdf layer must be published in the
  account (script) and `PROD_PDF_LAYER_ARN` set; prod bucket CORS PUT (already
  required by template digitise); lifecycle: none (documents are kept).
