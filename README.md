# FieldSight Pipeline

Backend for FieldSight: devices and the web app upload site recordings and photos,
the pipeline transcribes and extracts them, writes the results to Aurora, and serves
reports, search and Ask through the org API. The web app lives in the separate
`fieldsight-ui` repo.

## How data moves

```
Device / web app ──POST /api/org/recordings/upload-url──► presigned PUT
                                                            │
                                     S3 users/<folder>/audio|video|pictures/...
                                                            │ S3 event
                                                            ▼
  vad ──► audio_segments/ ──► transcribe ──► transcripts/
                                                │ S3 event (live pass) / extraction_requests/ (final pass)
                                                ▼
                                       extract-session ──► extractions/ ──► item-writer ──► Aurora
                                                                                  │
          report-generator (nightly + report_requests/) ──► reports/ ──► embed-report ──► ingest ──► Aurora
                                                                                  │
                     org-api (API Gateway, Cognito) ◄── web app, devices, Ask, search
```

- S3 ObjectCreated triggers on the data bucket are not in the SAM template (SAM cannot
  attach events to an external bucket). `scripts/wire-s3-events.sh` merges them in after
  every deploy; the prefix → function map is at the top of that script.
- org-api and the other in-VPC functions reach Aurora (`fieldsight-db-test` cluster,
  databases `fieldsight` for prod and `fieldsight_test` for TEST).
- Ownership of every lake object is indexed in `lake_objects` (`src/lake_owner.py`).
  Isolation is enforced by org-api, not by the S3 key layout.
- The ASR and LLM vendors are stack parameters (`AsrProvider`, `LlmProvider` and the
  model parameters); TEST and prod set them from repo variables in the deploy workflows.

## Environments and deploy

| | TEST | PROD |
|---|---|---|
| Branch | `develop` | `main` |
| Stack | `fieldsight-test` | `fieldsight-prod` |
| Workflow | `.github/workflows/deploy.yml` | `.github/workflows/deploy-prod.yml` (gated by the `production` environment) |
| Data bucket | `fieldsight-data-test-509194952652` | `fieldsight-data-509194952652` |
| Org API gateway | `wdsgobb7b0` | `ys94qy2tk0` |

Each deploy runs `sam build` / `sam deploy`, applies DB migrations through the stage's
`*-migrate` function (`src/migrations/`) and wires the S3 events. Region:
ap-southeast-2.

`deploy-prod-code.yml` is a manual-only leftover for the March hand-made lambdas, which
were deleted on 2026-10-31; it is no longer used.

## Tests

```bash
pytest tests/unit                 # unit suite (CI: .github/workflows/test.yml)
pytest tests/integration          # needs Postgres with pgvector (CI starts one)
```

`ci.yml` lints `src/template.yaml` and `infra/db-template.yaml` with cfn-lint (pinned 1.53.3;
see the comment there before bumping it).

## Repository layout

```
src/                 Lambda handlers (lambda_*.py), shared modules, template.yaml (canonical SAM template)
src/migrations/      Aurora schema migrations, applied in order by the migrate function
scripts/             deploy helpers (wire-s3-events.sh, deploy-ui.sh, smoke tests) and one-off ops scripts
config/              prompt templates and vocabularies uploaded to S3 config/
tests/unit, tests/integration
docs/superpowers/    specs and plans, one pair per piece of work
CLAUDE.md            working rules and the bug log
ROADMAP.md           what is still open
```

## Retired

- RealPTT ingest (orchestrator, downloader, Fargate downloader) was removed from the
  stack in #1198 (prod 2026-10-30). Devices upload directly.
- `config/user_mapping.json` is no longer read by any function (M5 #1184, #1198).
  Users, folders and sites come from Aurora and the published directory
  (`config/directory.json`, `src/directory.py`).
- The March hand-made lambdas and API gateway `khfj3p1fkb` were deleted on 2026-10-31.
