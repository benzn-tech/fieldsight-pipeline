# Digitise a checklist template from a photo, PDF or Word file

Status: design (2026-10-16), Fable-reviewed (approve with changes, applied). Sub-project 2 of the "project assets / template
digitisation / inspection gaps" batch. Decisions taken by the controller under
the owner's standing instruction (2026-10-15: pick the long-term, reasonable
option); each is marked **Decision**.

## Why

A subcontractor or client hands the site manager their inspection form -- a
Word file, a PDF, or a sheet of paper. Today the Library's upload picker checks
the extension and throws the file away (`template-upload-modal.js`: "NOTHING IS
READ FROM THE FILE"); the person retypes 47 or 115 items by hand. Once a
checklist is in the Library, a spoken check fills it on its own and now names
what was not done (checklist gaps, 2026-10-16). Getting the form in is the
missing first step.

## What the spike measured (2026-10-16)

Two real forms (BICL Site Establishment, 47 items / 11 sections; Penina Site
Inspection, 115 items / 19 sections). Word exported each to PDF; pages were
rasterised and degraded into a "phone photo" and a "harsh photo" (skew,
shadow, noise, ~95 dpi). Scored against python-docx ground truth. Scripts and
scores: `2026-10-16-template-digitise-spike/` next to this file.

| route | input | recall | exact wording | order | sections |
|---|---|---|---|---|---|
| python-docx | .docx | 1.00 | 1.00 | 1.00 | all |
| LandingAI ADE + own parser | PDF / photo | 1.00 | 0.98-1.00 | 1.00 | broke on harsh (split a section, "8." -> "B.") |
| Gemini 3.8 flash (OpenRouter), all pages one call | PDF / photo | 1.00 | 0.99-1.00 | 1.00 | all, harsh included |

Gemini cost about $0.003-0.004 per page; ADE 3 credits per page and needs a
table parser per layout. Both vendors silently "correct" a customer's typo on a
degraded photo ("singage" -> "signage"). Caveats: two forms, one company's
style, synthetic photos, one run per cell.

**Decision -- no ADE.** Gemini through the OpenRouter path we already use for
transcripts: equal or better accuracy, no per-layout parser, no new vendor
receiving customer documents, no new key. ADE stays an option behind the same
interface if a layout ever defeats the model.

**Decision -- DOCX goes through python-docx**, no model: free, exact,
deterministic. A Word layout the reader does not recognise is reported as
unreadable with a hint to upload a PDF export (section 3).

**Decision -- a person confirms before anything is saved.** Recall and order
were perfect, but wording can be silently "fixed" and a section can split
across a page break. The draft is never a template until someone presses Save.

## The flow

```
Library "Upload a form"  --presigned PUT-->  lake: template_sources/{company_id}/{id}.{ext}
        |
        +-- POST /api/org/templates/digitise {sourceKey, name}
              org-api (in-VPC): authorise, check the object, write
              template_digitise_requests/{company_id}/{id}.json
                    |
                    v  S3 event (wired by hand, scripts/wire-s3-events.sh)
              TemplateDigitiseFunction (NOT in VPC: it calls OpenRouter)
                docx -> python-docx reader; pdf/jpg/png -> Gemini
                writes template_digitise_results/{company_id}/{id}.json
        |
        +-- GET /api/org/templates/digitise/{id}  (polls the result, presigns the source for preview)
        |
Review screen: draft sections + items beside the source; edit; Save
        |
        +-- POST /api/org/templates (existing; body sections kind=checklist)
```

This is the session-report pattern (`session_report_requests/` ->
`SessionReportFunction` -> `session_report_results/`), chosen because org-api
is in the VPC with no outbound (BUG-36) and a 115-item form takes 27-40 s, past
API Gateway's 29 s.

### 1. Upload (org-api)

- `POST /api/org/templates/digitise/upload-url` `{filename, contentType, size}`
  -> presigned PUT for `template_sources/{company_id}/{uuid}.{ext}`.
  Allowed: `.pdf .docx .jpg .jpeg .png` (the modal's `.md .tiff` are dropped:
  markdown has no table to read, tiff is rare and the model route does not take
  it). Max 20 MB; content type must match the extension. A presigned PUT cannot
  enforce size, so the request step re-checks `ContentLength` on the HEAD and
  refuses anything larger. Company resolved exactly as `_template_company`
  resolves it (so a platform_admin's `?company_id=` is honoured the same way in
  upload-url, request and poll -- otherwise an operator's upload lands under
  the operator's company and the request refuses it).
- Who: anyone who may create a template. A personal draft is saved as a
  personal template; an org one needs `_TEMPLATE_WRITE_ROLES`, enforced by the
  existing `POST /templates` at Save -- digitising itself writes no template.
- Multi-page photos: the picker takes up to 10 images at once; each is its own
  object, and the request lists them in order.

### 2. Request (org-api)

Routing: every `/templates/digitise...` route is matched before the generic
`^/templates/([^/]+)$` in `dispatch` (the ordering warning there), or
`digitise` is read as a template id. With `TEMPLATE_DIGITISE` off the routes
answer 404 (not 403) -- the UI's only gate (see Rollout).

`POST /api/org/templates/digitise` `{sourceKeys: [...], name}`:
- Every key must be under `template_sources/{caller's company_id}/` and exist
  (HEAD). Otherwise 400/404. No key from another company is ever accepted.
- Writes `template_digitise_requests/{company_id}/{id}.json`
  `{id, companyId, userId, sourceKeys, name, requestedAt}` and returns
  `{id, status: "pending"}` (202).
- Registers each source object in `lake_objects` with an explicit `kind =
  'template_source'` (`lake_keys.parse` does not know the family and would
  fall back to `template_sources`), `company_id` set, `site_id` NULL; the
  request and result JSONs are registered too, as session-report does.

### 3. Worker (`lambda_template_digitise.py`, non-VPC)

Function definition copied from `SessionReportFunction`: `Runtime:
python3.12` (the docx layer is cp312, BUG-24; Globals is 3.11), `Layers: !If
[HasDocxLayer, ...]`, `Timeout: 900`, `LLM_CHAIN_BUDGET_SECONDS`,
`LLM_HTTP_TIMEOUT`, `QWEN_API_KEY` / `QWEN_BASE_URL` (the real env names),
`lake_manifest/*` PutObject, and **`LLM_FALLBACK_MODELS` set to empty** so the
Globals text-model chain cannot leak in. It logs "docx reader:
enabled/DISABLED" at import; with the layer absent a DOCX gets `status:
error`, "Word files cannot be read on this deployment -- upload a PDF", never
an ImportError.

- **DOCX** (`template_docx.read(path) -> sections`): the rule the spike used --
  a row is an item when one of its cells is a Yes/No/N/A checkbox cell (a
  `w:sdt` checkbox or the literal header columns); a section is a merged
  single-cell row or the first cell of a Yes/No/N/A header row. Item text is the
  first cell's text, whitespace-joined. If the reader finds no item, the
  result is `status: "unreadable"` with `reason: "no checklist table found in
  the Word file"` -- **Decision:** no model fallback for DOCX in v1 (there is no
  local docx-to-PDF renderer in Lambda; the person can upload a PDF export).
- **PDF / images** (`template_vision.read(pages) -> sections`): one OpenRouter
  call, model `TEMPLATE_DIGITISE_MODEL` (default `google/gemini-3.8-flash`),
  temperature 0, JSON output, the spike's prompt verbatim (copy it from
  `2026-10-16-template-digitise-spike/run_llm.py`). Images as `image_url` data
  URIs; a PDF as OpenRouter's `file` content part (verified in the plan's
  first task; if refused, PDFs are refused with "export the pages as images" --
  PyMuPDF is not in any layer). Page limit 15. Through `llm_utils`' key, base
  URL and retry; `llm_utils` gains a function for a multi-part (image) message
  with **its own model chain**: `TEMPLATE_DIGITISE_FALLBACK_MODELS`, default
  empty -- a text-only fallback would ignore the images and invent items that
  code validation cannot catch. The text `call_llm` is not changed. The shipped
  request is not the spike's: `_openrouter_fields` adds reasoning effort and
  token headroom, and temperature comes from `LLM_TEMPERATURE`. So the plan's
  first task re-runs both fixture forms through the real function and confirms
  the spike scores before anything else is built.
- The uploaded document is data to this prompt; everything decided about it is
  decided in code. A digitised template is `SOURCE_LIBRARY` like a typed one,
  so its items already go through `_fenceable` inside the customer fence when
  a report is written (`report_template._rendered_section`) -- no new guard.
- **Validation in code, never in the prompt** (the model's output is data):
  sections with no items dropped; items stripped of a leading list label
  (`1.`, `a)`, `ii)`); empty items dropped; each item cut to `MAX_ITEM_CHARS`
  (300) with a flag; more than `MAX_ITEMS` (120) in a section is split into
  "Title (1)", "Title (2)" with a flag; a section title over 120 chars is cut
  (flag); more than 40 sections is flagged (the template limit) -- the person
  merges before Save. JSON that does not parse -> one retry, then
  `status: "error"`.
- **Flags for the review screen** (per item or section, computed in code):
  `label_kept` (still starts with a list label after stripping),
  `near_duplicate_section` (two section titles at ratio >= 0.85, e.g. "Gasses" /
  "Gases"), `split_section` (two adjacent sections with the same or
  near-same title -- likely a page break), `truncated`, `split_for_limit`.
- Result: `template_digitise_results/{company_id}/{id}.json`
  `{status: done|unreadable|error, sections: [{title, items: [{text, flags}],
  flags}], counts: {sections, items}, route: "docx"|"vision", model, pages,
  error?, reason?}`.

### 4. Poll (org-api)

`GET /api/org/templates/digitise/{id}` -> the result JSON (pending when
absent), plus a presigned GET URL per source page for the preview (1 hour).
Company-scoped: the id is looked up only under the caller's company prefix.

### 5. Review screen (fieldsight-ui)

- The Library's upload modal stops discarding the file: it uploads, requests,
  and polls every 3 s ("Reading your form... about half a minute").
- Then a review screen: the source on the left (page images, or a "Word file"
  note), the draft on the right as editable sections and items (rename, edit
  text, delete, reorder, merge two sections, add an item). Each section shows
  its item count. Flagged items and sections are highlighted with the reason
  in plain words ("Two sections with nearly the same name -- one form split
  across a page?").
- A short banner: "Check the wording against your form -- the reader can
  correct a spelling without saying so."
- Save -> the existing `POST /templates` with `scope` (personal, or org for
  admin/gm), `report_type: "custom"`, name, and a body that passes
  `report_template.validate_body`:
  - each section `kind: "checklist"`, `title` (<= 120), `items` as **plain
    strings** (the draft's `{text, flags}` objects are mapped down), and a
    fixed house `purpose` sentence (non-empty is required):
    "Record the inspector's answer to each item from the recording.";
  - `catch_all` from the UI's `DEFAULT_CATCH_ALL`;
  - at most 40 sections, body <= 60,000 chars.
  Nothing is stored as a template before Save.
- **`template-store.js` `create()` today always sends the starter sections for
  the report type and discards what it is given** -- the API layer rebuilds the
  body. It must accept `sections` (or a full body) and send them; a node test
  asserts the real request body carries the checklist items.
- Web editing on the Library is not capture: the "no typing in capture" rule
  does not apply here.

## Not in this spec

- Reproducing the customer's own .docx as the report output (the stored source
  makes it possible later; parked by the owner on 2026-10-08).
- Non-checklist templates (prose sections). The prompt and the reader only
  produce checklist sections.
- Handwriting, glare, curled paper -- not measured; the review screen is the
  safety net.
- A second model read to diff wording automatically.

## Error handling

- Upload of the wrong type/size -> 400 before any S3 write.
- Worker failure -> `status: "error"` with a message the screen shows; the
  source stays so the person can retry (`POST /templates/digitise` again with
  the same keys makes a new id).
- Model returns zero items -> `unreadable`, "No checklist rows found -- is this
  a checklist form?"
- A result older than 7 days is not served (pending forever is impossible:
  the screen gives up after 3 minutes with "Taking too long -- try again").
- Lifecycle: `template_sources/`, `template_digitise_requests/` and
  `_results/` expire after 90 days, added to `scripts/wire-bucket-lifecycle.sh`
  (it replaces the whole config -- edit, then run).

## Testing

- `template_docx.read` against the two real forms (fixtures: the BICL and
  Penina .docx, or trimmed copies) -> exactly the spike's ground truth (47/11,
  115/19).
- Validation/flags: unit tests with hand-written model outputs (label kept,
  Gasses/Gases, split section, 121 items, 301-char item, bad JSON).
- Worker: the OpenRouter call stubbed at the HTTP boundary, never the whole
  function; one test drives the real handler from an S3 event to the result
  JSON.
- org-api: upload-url validation, cross-company key refused, request written
  with the right key, poll scoped to the company -- through a fake s3.
- Frontend: node tests for the upload/poll state machine and the save mapping
  (sections -> `kind: checklist` body that the server's `validate_items`
  accepts).
- TEST end to end: upload the Penina PDF and a phone photo of a page; the draft
  matches the form; Save; a spoken check is matched to it.

## Rollout and other sessions

- New function + new prefixes + one new S3 trigger (wired by hand, documented
  in `scripts/wire-s3-events.sh`, `fn_exists`-guarded) + IAM for org-api
  (PutObject requests and sources, GetObject results/sources, **ListBucket on
  `template_digitise_results/*` and `template_sources/*`** -- without it a
  missing key is AccessDenied (500), not pending/404) and the worker (Get
  sources/requests, Put results, `lake_manifest/*`). `simulate-principal-policy`
  every action before claiming it works.
- Behind `TEMPLATE_DIGITISE` (org-api env, default off). `/me` carries no
  feature list, so the UI gate is the route itself: the upload modal calls
  `upload-url`; a 404 keeps today's behaviour.
- **Prod bucket CORS has no PUT** (only `amplify-read-for-canvas`, GET/HEAD);
  TEST allows PUT. This is the first browser->S3 PUT on prod, so the prod
  hand-wiring list gains: add PUT + `Content-Type` for the prod Amplify origin
  (`get-bucket-cors` first; put replaces the whole config).
- Spike directory goes into the PR without keys (`env.json` not copied;
  `calls.json` checked).
- Backend PR -> develop -> TEST; frontend PR -> dev. Prod with an owner-approved
  release; the trigger and lifecycle rule must be wired on the prod bucket by
  hand at that time (list it in the release notes).
