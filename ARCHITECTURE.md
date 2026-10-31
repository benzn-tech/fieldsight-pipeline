# System Architecture Overview

> Rewritten 2026-11-01 after the RealPTT ingest path was removed (#1198). The stack
> definition is `src/template.yaml`; the S3 trigger map is `scripts/wire-s3-events.sh`.
> When this file and those two disagree, they win.

## Capture to Aurora

Devices and the web app ask org-api for an upload URL
(`POST /api/org/recordings/upload-url`, then `POST /api/org/recordings/{id}/complete`)
and PUT the file straight to the data bucket under `users/<folder>/...`. Everything
after that is driven by S3 ObjectCreated events on fixed prefixes. Each prefix is
disjoint from every other one, so no function triggers itself.

| Prefix written | Triggers | Writes |
|---|---|---|
| `users/**.wav`, `users/**.mp4` (raw uploads) | `vad` | `audio_segments/`; an H.264 preview under `web_video/` for H.265 sources |
| `audio_segments/**.wav` | `transcribe` (vendor = `AsrProvider`) | `transcripts/` |
| `transcripts/**.json` (live pass) and `extraction_requests/**.json` (final pass, from the finalize sweep) | `extract-session` | `extractions/` |
| `extractions/**.json` | `item-writer` | topics, action items, findings in Aurora; `match_requests/`, `keyframe_requests/`, `index_requests/` |
| `match_requests/**.json` | `programme-matcher` | programme suggestions in Aurora |
| `keyframe_requests/**.json` | `keyframe` | stills under `users/*/pictures/` |
| `reports/**daily_report.json`, `reindex_requests/**.json`, `index_requests/**.json` | `embed-report` | `embeddings/`, `reindex_requests/**.vectors.json`, `index_vectors/` |
| `embeddings/**vectors.json`, `reindex_requests/**.vectors.json`, `index_vectors/**.json`, `project_doc_vectors/**.json` | `ingest` | search chunks and vectors in Aurora |
| `report_requests/**.json` (org-api: regenerate my report) | `report-generator` | `reports/` |
| `session_report_requests/**.json` | `session-report` | `session_reports/`, `session_report_results/` |
| `session_finalize_requests/**.json` | `session-finalize` | the post-recording confirmation email (SES) |
| `voiceprint_requests/**.json` | `speaker-embed` | invokes `voiceprint-writer` directly |
| `template_digitise_requests/**.json` | `template-digitise` | `template_digitise_results/` |
| `project_doc_requests/**.json` | `project-doc-index` | `project_doc_vectors/` |

Lambdas inside the VPC reach Aurora but have no NAT, so they cannot call SES or invoke a
non-VPC function directly (CLAUDE.md BUG-36). They hand work out by writing a request
object under one of the prefixes above.

## Scheduled work

All crons are UTC. Schedules are enabled per stage by `EnableSchedules`.

| Function | Schedule | Purpose |
|---|---|---|
| `report-generator` | `cron(0 16 * * ? *)` daily, `cron(30 16 * * ? *)` weather, `cron(0 5 ? * FRI *)` weekly, `cron(0 17 L * ? *)` monthly | per-user reports and per-company summaries |
| `finalize-sweep` | `rate(1 minute)` | closes sessions whose grace window elapsed; enqueues the final extraction pass and the confirmation email |
| `recording-segments` | `rate(5 minutes)` + transcript events | keeps `day_recording_segments` current |
| `extraction-backlog` | `rate(1 hour)` and `rate(5 minutes)` | finds transcribed words that were never extracted, and re-drives them |
| `nonwork-expiry` | `rate(1 hour)` | removes non-work (personal) topics after their rescue window |
| `lake-ops` | `rate(1 hour)` manifest drain, `rate(10 minutes)` export tick | lake manifest drain; advances one pending company export per tick |
| `org-api` | `cron(30 15 * * ? *)`, `cron(35 15 * * ? *)` | republishes site coordinates and the directory to S3 |
| `floor-recompute`, `voice-reaper` | `rate(6 hours)` | voiceprint floors; stale voice sessions |
| `device-report` | `cron(0 16 * * ? *)` | daily device-ledger report (Notion/Teams) |

`transcribe-callback`, `rolling-summary` and `session-activity` are driven by EventBridge
rules (Transcribe job state changes, transcript chunks and VAD sidecars landing).

## Serving

- **org-api** (`lambda_org_api.py`) is the web app's and the devices' API, mounted at
  `/api/org/{proxy+}` on the stage's API gateway (prod `ys94qy2tk0`, test `wdsgobb7b0`),
  with Cognito authorisers. It enforces tenancy: sites belong to companies, members can
  be external, and every lake read goes through the `lake_objects` ownership index.
- **api** (`lambda_fieldsight_api.py`) serves `/api/{proxy+}` on the same gateway: Ask and
  search. Ask runs in `ask-agent` / `ask-voice-worker`; retrieval goes through
  `rag-search` and the tools in `ask_tools.py`.
- The voice WebSocket API uses `ws-connect`, `ws-disconnect`, `voice-ws-authorizer`,
  `send-voice`, `voice-resolve` and `voice-fanout`.
- Reports are read from `reports/<date>/<folder>/daily_report.json` and, for company
  summaries, `reports/<date>/companies/<company_id>/summary_report.json`. The lake-wide
  `summary_report.json` is off (`SUMMARY_LAKE_WIDE=off`, #1183).

## Test/Prod Environment Isolation (live 2026-07-21, PR #114)

The `fieldsight-test-*` and `fieldsight-prod-*` SAM stacks share **one Aurora
cluster** (`fieldsight-db-test-dbcluster-hywiixu8ihi9`) and **one Cognito pool**
(`ap-southeast-2_q88pd6XXr`), but each stack now uses its **own database** inside
that cluster:

| | Database | S3 lake bucket | Cognito |
|---|---|---|---|
| **prod** (`fieldsight-prod-*`) | `fieldsight` | `fieldsight-data-509194952652` | shared `q88pd6XXr` |
| **test** (`fieldsight-test-*`) | `fieldsight_test` | `fieldsight-data-test-509194952652` | shared `q88pd6XXr` |

**Result:** test and prod are **physically isolated at the data + schema layer** —
a test data change or destructive migration cannot touch prod's customer data.
They still share the cluster (so cluster-level events are not isolated) and the
Cognito pool (authorization is resolved from each database's `users` row by
`cognito_sub`, so a shared sub is inert in an environment without a row there).

**Mechanism.** All 12 in-VPC DB functions render `PGDATABASE` as
`!If [HasPgDatabaseOverride, !Ref PgDatabase, !ImportValue "${DbStackName}-DbName"]`
(default falls back to the imported `fieldsight`, so the template is
behavior-neutral without an override). The override is passed **only** in
`.github/workflows/deploy.yml`'s `--parameter-overrides`
(`PgDatabase=fieldsight_test`) — NOT in `samconfig.toml`, because the CI's CLI
`--parameter-overrides` **replaces** (does not merge) the samconfig list.
`deploy-prod.yml` passes no override, so prod keeps `fieldsight`.

**Migrations** are per-database (`schema_migrations` lives in each DB). Test may
experiment with destructive migrations freely, **but any migration file merged to
`main` still auto-runs on prod** (`deploy-prod.yml` invokes `fieldsight-prod-migrate`
after each prod deploy) — so throwaway/experimental migrations must never reach
`main`.

**Bootstrap / rollback.** `fieldsight_test` was created + `pg_dump`/`pg_restore`
-populated from `fieldsight` via a throwaway EC2 in the DB VPC (over SSM), then
destroyed. To roll back: remove the `PgDatabase=fieldsight_test` line from
`deploy.yml` and redeploy (the `!If` falls back to `fieldsight`), then
`DROP DATABASE fieldsight_test`. Full procedure:
`scripts/db-isolation-bootstrap.md`; design:
`docs/superpowers/specs/2026-07-21-test-prod-db-isolation-design.md`.

## Retired components

| Component | Removed |
|---|---|
| RealPTT orchestrator, downloader, Fargate downloader stack | #1198, prod 2026-10-30 |
| `config/user_mapping.json` as an input | M5 #1184 and #1198; nothing reads it |
| March hand-made lambdas (`fieldsight-api`, `-orchestrator`, `-vad`, `-transcribe`, ...) and gateway `khfj3p1fkb` | deleted by the owner 2026-10-31; code archived in `s3://fieldsight-data-509194952652/_archive/legacy-lambdas/2026-10-29/` |
