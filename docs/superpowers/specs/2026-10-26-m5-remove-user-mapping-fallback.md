# M5: remove the frozen `user_mapping.json` fallback from `directory.py`

Sub-project 2 of the tenancy roadmap (`2026-10-05-tenancy-roadmap-design.md`), decision M5 of
`2026-10-08-retire-the-frozen-mapping-design.md`. Scheduled ~2026-10-18; done 2026-10-26.

## Gate (measured 2026-10-26, read-only)

The gate is "one release with the directory live on prod and zero
`directory: falling back to user_mapping.json` WARNINGs for a week".

- Logs Insights over **all 44** `/aws/lambda/fieldsight-prod-*` groups, last **10 days**:
  0 matches for `falling back to user_mapping` or `neither config/directory.json`.
  Same query over **30 days** on report-generator, meeting-minutes, extract-session, org-api: 0.
- Positive control (same 10 days): WARNING lines do reach these groups (extract-session and
  org-api both logged WARNINGs), and the readers ran: report-generator 22 invocations,
  extract-session 203, org-api 34,751.
- org-api logs `directory: published …` / `republish found nothing to change` every day since
  at least 2026-10-12 (39 people, 20 sites on 2026-10-25). `config/directory.json` exists in
  the prod bucket (last written 2026-10-20). `config/user_mapping.json` was last written
  2026-08-01.
- IngestBucketName and DataBucketName are the same bucket on prod. `s3:GetObject` on
  `config/directory.json` is allowed for the deployed report-generator, meeting-minutes and
  extract-session roles (`simulate-principal-policy`).
- **Weak spot:** meeting-minutes had **zero** invocations in 30 days on prod and TEST, so its
  "zero warnings" proves nothing about it by itself. Its grant was checked by simulation, and it
  calls the same `directory.load()` as the two readers that did run.

## What is removed

From `src/directory.py`:
- `LEGACY_KEY`, `LEGACY_PREFIX`, `adapt_legacy()`.
- The fallback branch in `load()`: no read of `config/user_mapping.json` ever.
- The "TRANSITION FALLBACK" paragraph of the module docstring.

From `src/lambda_report_generator.py`: the `startswith(directory.LEGACY_PREFIX)` guard. With
no legacy ids, every site id in the directory is a real Aurora UUID, and it now **overwrites**
`site_uuid` (was `setdefault`). Review finding: `site_coords.overlay_sites_info` runs first and
sets `site_uuid` from the slug-keyed coordinate file, and slugs are unique per company only, so
two tenants' `main-site` could share one UUID and read each other's programme and weather. The
directory id wins.

From `src/lambda_extract_session.py` (`_fuzzy_match_site`): the unscoped branch. It existed only
for the legacy document, the one document with no company ids; `sites.company_id` is NOT NULL
and `build()` always stamps it. The match is now always scoped to the speaker's company, and a
speaker with no company gets no match.

From `src/template.yaml`:
- The extract-session grant `${IngestBucketName}/config/user_mapping.json` (its only reader
  was this fallback).
- The stale comment above the org-api site-coords grant that calls the file "read by six
  lambdas".

## Readers and their replacement (all already on the directory)

| Reader | Call | Uses |
|---|---|---|
| report-generator | `directory.load` x3 (`get_user_site_mapping`, `external_folders`, per-company summaries) | `people[folder]` role / primary_site / sites / has_external / company_id; `sites[id]` name, slug, location |
| meeting-minutes | `load_user_mapping()` = `directory.names_by_folder(directory.load())` | speaker name from the transcript's folder. Name kept; it no longer reads the file it is named after. |
| extract-session | `load_sites()` -> `directory.load` | `sites[*].name`, scoped by `directory.person(doc, folder)["company_id"]` |
| org-api | publisher only (`_publish_directory`) | writes `config/directory.json`; reads nothing from the old file |

## When the directory has no entry

Unchanged by this PR except extract-session; listed so the reviewer can check it:
- report-generator: `person()` -> `{}` -> no role and no displayed site name. **Not** "no site
  at all": `group_reports_by_site` puts a folder with no sites under the `SITE_NAME` env
  default, and the DynamoDB SITE# write uses `user_site_slug or site_name` slugified. Both are
  pre-existing and out of scope here (see memory note on the env fallback).
- meeting-minutes: the folder is not in `names_by_folder` -> the device-derived speaker name is
  kept.
- extract-session: no company for the speaker -> **no match** (as before whenever the directory
  carried company ids, which the published one always does).

## When `config/directory.json` itself is absent or unreadable (including 403)

`load()` logs ERROR (`directory: could not read …` for a non-absent failure, then
`directory: config/directory.json could not be read -- every person and site lookup will come
back empty`) and returns `empty()`, cached for the container. The report generator then
fails closed on top of that: `external_folders` returns None, so site rollups, SITE# stamping
and per-company summaries are skipped with an ERROR rather than mixing tenants. This was already the
"both missing" behaviour. **The only behaviour change of this PR:** a prod bucket that has
lost `directory.json` but still has `user_mapping.json` now gets empty lookups and an ERROR,
not the August answer and a WARNING. The daily republish (`{"task": "republish_directory"}`,
15:35 UTC) restores the object. Note the per-container cache: a transient S3 failure on the
first read now sticks as empty lookups (before: as the August answer) until the container
recycles.

## Not in this PR: the orchestrator

`lambda_orchestrator.load_user_mapping` still reads `config/user_mapping.json` directly (its own
code, not this fallback; device ids exist nowhere else). It is untouched here and still fails
closed (M3). Its two prod EventBridge rules are DISABLED and it has had zero invocations since
2026-10-12. Removing `OrchestratorFunction` pulls in the downloader, the Fargate trigger,
eight parameters and both deploy workflows' overrides; that is the legacy-gateway phase B
follow-up. Consequences:
- `config/user_mapping.json` **stays in S3** until that follow-up lands; the owner deletes it
  then, not now.
- The orchestrator keeps its bucket-wide `S3ReadPolicy`; no grant of its is touched.

## Tests (red first)

- `directory.load` with only the legacy key present -> `empty()`, an ERROR, and
  `config/user_mapping.json` is never requested.
- `directory` has no `LEGACY_KEY` / `adapt_legacy` / `LEGACY_PREFIX`.
- An AccessDenied on `config/directory.json` -> `empty()` + ERROR, not a crash. A non-absence
  S3 error (`SlowDown`) and a malformed document (no `people`) -> `empty()` + ERROR, and the old
  file is not read.
- extract-session: a directory with null company ids gets **no** match; the two
  `declared_site` tests use a `config/directory.json` doc whose speaker and site share a company.
- report-generator: a site gets `site_uuid` = its directory id even when the slug-keyed
  coordinate file names another tenant's id for the same slug.
- template: zero `config/user_mapping.json` grants.
- `test_lambda_orchestrator.py` and the orchestrator tests run unchanged.

## Rollback

Revert the PR. No data, schema or S3 change is made, `directory.json` keeps being republished
daily, and `user_mapping.json` is still in the bucket, so a revert restores the fallback
exactly.

## Review (Fable, 2026-10-26) — folded in

- site_uuid `setdefault` was a no-op behind the coordinate overlay -> directory id now wins (fixed).
- "no site stamped" was wrong for the env default / SITE# write -> corrected above, left as is.
- extract-session's unscoped branch was dead fail-open code -> removed, always scoped.
- Missing tests for non-absence errors and malformed documents -> added.
- Grant removal confirmed safe: report-generator and meeting-minutes hold bucket-wide
  `S3ReadPolicy`; the orchestrator reads through its own.
- Not done (NIT): stale "read by six lambdas" prose in `site_coords.py`, `lambda_org_api.py`
  ~5407, `README.md`, `template.yaml` DefaultSiteName description. Left for the orchestrator
  follow-up, which removes the last reader.
