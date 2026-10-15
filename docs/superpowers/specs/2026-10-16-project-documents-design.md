# Project documents: specs and docs a project can be asked about

Status: design (2026-10-16), Fable-reviewed (approve with changes; all
findings resolved in "Review resolutions" at the end, which is binding and
overrides earlier text where they differ). Sub-project 1 of the "project assets / template
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
-- NO HNSW index (exact scan, see R12); GIN on the lexical expression (R11);
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
- **PDF** (pypdf in its own pure-Python layer, see R13): text per page;
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

## Review resolutions (binding; Fable review 2026-10-16)

**R1 -- Document rows must never become R refs.** `ask_tools._item`
classifies by shape (a row with `chunk_text` becomes `kind: "chunk"`,
provenance records) and `_envelope` defaults the prefix to `R`. So: `_item`
branches on the tool name `search_project_documents` BEFORE the shape check
and returns `kind: "document"` with `title, heading, page_from, page_to,
document_id, series_id, revision`; `_envelope` passes `prefix="D"`;
`_CARD_KINDS` and `_item_line` gain a `document` branch rendering
`title · heading · p.N`. Documents do NOT go through `_rerank_chunks`
(`_post_search`): the reranker is tuned on recordings.

**R2 -- D refs are parsed, sanitised and returned separately.** Five sites in
`ask_orchestrator.py` change together: `_FORGED_LEAD` becomes `[RWD]\d+`;
`_REF_GROUP`/`_REF_TOKEN` accept `D`; `to_client.repl` gets a D branch that
strips the ref from the text and collects cited D items, in citation order,
into a `documents` block `[{n, title, heading, page, document_id}]` plus
`refs_d`; `_cited_card_refs` stays R-only (documents are never cards). A test
feeds an answer citing R, W and D and asserts each lands only in its own
block, and that a document whose text begins `D1 |` or contains a code fence
is neutralised.

**R3 -- Prod reach.** The `D` family exists only on the orchestrator-v2 path
(`ASK_AGENTIC`), TEST-only today; prod Ask falls back to `_rag_answer`, which
never sees documents (rule 4). On prod, documents are reachable through the
project page and the search palette until ASK_AGENTIC is promoted. Stated in
the release notes.

**R4 -- Basis and guard.** The final-answer basis enum, `_derived_basis` and
`guard_basis` gain `documents` (only D cited); R+D is `mixed`. The
time-anchored guard applies to R only: a D-only answer is never forced to
fallback for lacking a date. `_SYSTEM` gains rule 2b: "D refs are the
project's documents -- what should be done. Never state that something
happened on site because a document requires it. When records and documents
disagree, say both, each with its ref."

**R5 -- Write role.** Upload / revision / rename / delete are allowed when: on
a home-company site, `global_role` in `_MANAGER_ROLES`; on an external site,
the external membership's role (computed by `repositories.scope`) in
`_MANAGER_ROLES`; or `is_cross_company(global_role)` (platform_admin),
written explicitly. Reads use `_allowed_site_ids` unchanged.

**R6 -- Ownership follows the project.** At insert
`company_id = sites.company_id` (not the caller's). Presigned keys and the
request key use the SITE's company:
`project_doc_sources/{site_company}/{id}.{ext}`; the request route checks the
key against the site's company; `lake_objects` rows carry `site_id` and the
site's company, so `lake_objects.reassign_site` moves them with the project.

**R7 -- Search palette path.** The palette goes `scripts/api/search.js` ->
`POST /api/search` -> `lambda_fieldsight_api.py` -> ask-agent `mode=search`
-> `_rag_search_list` -> rag-search, with the embedding computed in
ask-agent. So `_rag_search_list` gains an optional `include_documents` that
makes a second rag-search call in `documents` mode and returns
`{results, documents}`; the proxy passes `documents` through untouched; the
palette renders it as its own group.

**R8 -- Revisions and lifecycle.**
- `UNIQUE (series_id, revision)`; the revision is computed inside the insert
  transaction (lock the series rows `FOR UPDATE`).
- Deleting the current revision clears `superseded_at` on its immediate
  predecessor in the same transaction, so list and search agree. Deleting a
  non-current revision only removes it from history.
- A new revision on a series whose every revision is deleted -> 404.
- The same filename twice in one project is allowed: two series.
- `sites.archived_at` set -> the project's documents are read-only (writes
  answer 409 "This project is archived").

**R9 -- Never stuck pending.** The worker's top level catches everything and
writes an error artifact under `project_doc_vectors/` (`status: error,
reason`) that Ingest applies; `MaximumRetryAttempts: 0`; embedding runs
against a deadline from the Lambda context (minus a 60 s write reserve) and
writes `error` "Too large to index in one go" when it cannot finish. The UI
shows `pending` older than 20 minutes as "Taking too long -- Retry".
Documents over 1,000 chunks are refused up front as unreadable ("too long to
index").

**R10 -- IAM (simulate every action on TEST before the flag goes on).**
org-api: Put+Get on `project_doc_sources/*`, Put on `project_doc_requests/*`,
ListBucket prefix-conditioned on both. Worker: Get on sources and requests,
Put on `project_doc_vectors/*` and `lake_manifest/*`. Ingest: Get on
`project_doc_vectors/*` plus prefix-conditioned ListBucket (403-vs-404).

**R11 -- Lexical index.** Expression
`to_tsvector('simple', coalesce(heading,'') || ' ' || chunk_text)` -- the
`simple` config only, without the `regexp_replace` of 0061 (which strips
non-Latin text; documents may be in Chinese). The repository query uses the
identical expression; a test pins expression == index.

**R12 -- No HNSW.** The repo forces exact scans for ACL-filtered search
because filtered HNSW top-k drops rows. Project-document search is filtered
by site, so it uses exact cosine distance; indexes are btree `(site_id)`,
`(document_id, ordinal)` and the GIN above. `project_doc_chunks` carries no
`company_id` (tenant via the document join).

**R13 -- pypdf.** A separate pure-Python layer `fieldsight-pypdf` built by
`scripts/build-pypdf-layer.sh` (pip install pypdf into `python/`, zip,
`publish-layer-version`), published once per account by hand; parameter
`PdfLayerArn` (TEST var `TEST_PDF_LAYER_ARN`, prod var `PROD_PDF_LAYER_ARN`),
attached only to `ProjectDocIndexFunction`. The python-docx layer and every
other function are untouched. With the parameter empty, PDFs get `error`
"PDF reading is not set up on this deployment".

**R14 -- The contamination import test** names the forbidden importers:
`lambda_extract_session`, `lambda_item_writer`, `lambda_session_report`,
`lambda_report_generator`, `checklist*`, `report_template`, `session_index`,
`chunking` must not import `repositories.project_docs` or any `project_doc_*`
module (`lambda_ingest` may, for its apply route).
