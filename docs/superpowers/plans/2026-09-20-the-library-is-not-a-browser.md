# The Library stops being a browser

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or
> superpowers:executing-plans to implement this plan task-by-task.

**Goal:** A template a company writes in `/library` is stored on the server, survives the
browser it was written in, and is the thing a generated report is actually written to.

**Why now:** the generation step shipped (`report_template.load_template`,
`_generation_request` in `lambda_org_api.py:1625`, the worker's generate branch). It can only
load the **one template bundled inside the Lambda**, `src/report_templates/personal-meeting.v3.json`.
Everything a user builds in `/library` lives in `localStorage` under `fs_templates_v1`
(`fieldsight-ui/scripts/api/template-store.js`, 373 lines, no network call). So today a
`templateId` the user picked names nothing the server can read, and clearing site data
destroys the template.

**Spec:** `docs/superpowers/specs/2026-09-15-reports-over-any-stretch-of-the-day-design.md`
§3.4 (what the browser store is), §6 (storage, hash, schema additions, migration), §6.1 (the
built-in default). That spec's own plan says this piece is **"not covered here, by design:
§6 template storage and the Library (its own plan)"**. This is that plan.

## The constraint that shapes the whole design

**The worker that generates the report cannot read the database.** `SessionReportFunction` is
**non-VPC** — it has to be, it calls a model over HTTPS — and Aurora is in the VPC
(BUG-36: an in-VPC function with no egress black-holes; the inverse is that a non-VPC function
has no route to the cluster).

So a stored template **cannot be fetched by the worker from its id**. org-api — which is
in-VPC and already owns the request artifact — must **resolve the template and write the whole
resolved schema into the artifact**, alongside the id and version it came from. Two
consequences to hold onto:

* the artifact is a **snapshot**: a report generated today is not silently re-shaped by an
  edit made to the template tomorrow, and the version id + hash in the artifact says exactly
  what it was written to;
* `report_template.load_template` keeps working unchanged for the built-in, and the worker
  gains **one** new branch: "the artifact already carries a schema — use it".

Getting this backwards (worker resolves the id) is the failure this paragraph exists to
prevent; it does not fail in tests, it fails as a timeout in prod.

## Global constraints

- Repo `fieldsight-pipeline`, base `develop`; frontend `fieldsight-ui`, base `dev`.
  Fresh worktree off the remote tip. **Never `git add -A`**, never `git stash`.
- Comments, commit messages, PR bodies in English.
- Tests: `export UV_LINK_MODE=copy AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_DEFAULT_REGION=ap-southeast-2`
  then `uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy --with tzdata pytest tests/unit -q`.
  Frontend: `node --test --test-reporter=tap tests/*.test.js` (the **glob** form).
- **A migration merged to `main` runs on prod** (BUG-38). This one must be exercised on TEST
  (`fieldsight_test`) before the promote PR is opened.
- Every new **write** endpoint must be taught `platform_admin` span-all explicitly; graded
  read paths do not carry it (see `fieldsight-org-api-new-route-iam-trap`,
  `fieldsight-platform-admin-cross-company`).
- `null` means "no restriction", `[]` means "restrict to nothing". A tenant filter that
  receives `[]` must return nothing, not everything.

---

### Task 1: The tables

**Files:** create `src/migrations/0059_report_templates.sql`;
create `src/repositories/report_templates.py`; test `tests/unit/test_report_templates_repo.py`.

Two tables, mirroring the shape the frontend already speaks (§3.4) so the UI migration is a
transport change, not a data model change:

* `report_templates(id uuid pk, company_id uuid not null references companies(id),
  scope text check (scope in ('org','personal')), owner_user_id uuid null,
  report_type text, title text not null, description text, active boolean not null default true,
  deleted_at timestamptz null, created_at, updated_at)`
* `report_template_versions(id uuid pk, template_id uuid not null references
  report_templates(id) on delete cascade, version int not null, schema jsonb not null,
  schema_hash text not null, change_note text, created_by_user_id uuid, created_at,
  unique (template_id, version))`

Versions are **append-only** — the frontend's `updateSchema` appends and `restore` appends a
copy, and nothing here may turn that into an update-in-place, because a report's artifact
points at a version id and *that* row must never change under it (§6).

`schema_hash` is sha256 over canonical JSON (sorted keys, compact separators, UTF-8).
**Not `content_hash.py`** — it casefolds and collapses whitespace, and a heading's
capitalisation is visible in a report (§6).

Repository functions, all taking `company_id` as a required argument (not a filter the caller
may omit): `list_templates`, `get_template`, `create_template`, `update_schema`,
`set_active`, `soft_delete`, `list_versions`, `restore_version`, `get_version`.

- [ ] Write the failing tests first, including one that a template from another company is
      invisible to `get_template` (not just to `list_templates` — the id is guessable).
- [ ] Run the migration and the SQL **against the real database** via the RDS Data API inside
      a transaction that is rolled back (`--database fieldsight_test`). The unit doubles do not
      enforce the foreign key or the unique constraint, and this repo has three defects on
      record that only a real run found.

### Task 2: The routes

**Files:** modify `src/lambda_org_api.py`; test `tests/unit/test_org_api_templates.py`.

Implement exactly the shape the frontend already calls, so the UI change is small:

| Method | Path | Notes |
|---|---|---|
| GET | `/api/org/templates?scope=org\|personal` | company-scoped; personal also owner-scoped |
| POST | `/api/org/templates` | creates template + version 1 |
| GET | `/api/org/templates/{id}` | |
| PATCH | `/api/org/templates/{id}/schema` | appends a version, returns it |
| POST | `/api/org/templates/{id}/activate` | |
| DELETE | `/api/org/templates/{id}` | soft delete |
| GET | `/api/org/templates/{id}/versions` | |
| POST | `/api/org/templates/{id}/versions/{vid}/restore` | appends a copy |

- [ ] Validation at the door, not in the worker: a section without a title, a `kind` outside
      `{narrative, list, table, kpi, photos}`, a schema with no catch-all section, or a
      `excluded_subjects` entry without a label is a 400.
- [ ] **Placeholder guard.** A schema whose text still contains an unfilled placeholder
      (`{{...}}`, `<...>`, `TODO`) is refused on write. A placeholder that reaches a customer
      report is the failure recorded in `vendor-swap-leaves-arguments-behind`, and refusing it
      at write time is cheaper than catching it at render time.
- [ ] Each write route teaches `platform_admin` span-all explicitly, with a test per route.

### Task 3: The artifact carries the schema

**Files:** modify `_generation_request` (`src/lambda_org_api.py:1625`); modify
`src/report_template.py`; modify `src/lambda_session_report.py`;
test `tests/unit/test_generation_request_snapshot.py`.

- [ ] `_generation_request` resolves a `templateId` that is **not** a built-in against the
      store, for the caller's company, and puts `{templateId, templateVersion, versionId,
      schemaHash, schema}` on the artifact. An unknown or other-company id stays a 400 —
      the caller is still on the line at that point, which is why validation lives here.
- [ ] `report_template.render_prompt` is unchanged; it already takes a template dict. The
      worker's only change is to prefer `generate.schema` when present over
      `load_template(id, version)`.
- [ ] A built-in id keeps working with no schema on the artifact (backwards compatible; no
      existing test may change its expectations).
- [ ] Mutation control: delete the `schema` key from the artifact and prove a test goes red.

### Task 4: `/library` stops writing to localStorage

**Files:** `fieldsight-ui/scripts/api/template-store.js`; `scripts/api/org.js`; the
`/library` page; tests.

- [ ] Every function in `template-store.js` keeps its signature and returns the same shape;
      the body becomes a call through `FS.api` (register the module **after**
      `scripts/api/index.js`, or the wholesale `window.FS.api = {…}` assignment wipes it).
- [ ] **One-time upload with a notice**: on first load, templates found in `fs_templates_v1`
      are offered for upload, uploaded on confirmation, and the local key is kept (not
      deleted) until the upload is confirmed by a read-back. Templates live in one browser
      today; a silent migration that half-fails loses a customer's work.
- [ ] Favourites stay in `localStorage` — a per-viewer convenience, not shared state.

### Task 5: The picker

**Files:** the Generate-report flow in `fieldsight-ui/scripts/pages/timeline.js`.

- [ ] The flow lists the company's active templates and sends `templateId` +
      `templateVersion` (`scripts/api/org.js` already sends `templateId`).
- [ ] With no template chosen, the request carries none and the assembled report is produced,
      exactly as today.

### Task 6: The built-in becomes a seed, not a special case

- [ ] `personal-meeting.v3.json` stays bundled as the fallback, and is offered in the picker
      as a read-only starting point a company can **copy** into its own store.
- [ ] Do not auto-insert it as a row for every company: a company that never opens `/library`
      should not acquire a template it did not write.

## Verification

1. Unit, with the mutation controls named in Tasks 1 and 3 — a control first, then the fix
   (eight harnesses in these repos have reported "nothing red" while testing nothing).
2. Real SQL on TEST through the Data API, rolled back (Task 1).
3. **Tenant isolation, measured, not reasoned**: create a template as company A, attempt every
   route as company B, and assert 404/403 per route. `[]` from a list route is not evidence —
   an empty list is also what a broken filter returns.
4. End to end on TEST: write a template in `/library`, reload in a **different browser
   profile**, generate a report over a real window with it, and read the document. The point
   of the different profile is that it is the one check `localStorage` can never pass.
5. Confirm the deploy carries the change by matching `headSha`, not by a green merge.

## Risks, stated

* **The upload migration is the only step that can lose data.** It is why Task 4 keeps the
  local key until a read-back confirms.
* **A stored schema is user-written text that becomes part of a prompt.** Task 2's validation
  is the only thing between a badly-written section and a strange report; it is not a security
  boundary, and the schema is never executed.
* **Version rows are pointed at by artifacts.** Any later "tidy up old versions" work has to
  reckon with that; deleting a version breaks the provenance of every report written to it.
