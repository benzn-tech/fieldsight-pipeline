# Authority Flip (Unified Extraction, Task 4) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:subagent-driven-development (or superpowers:executing-plans). Each task below is one PR: a fresh implementer writes the TDD code from this task + the design spec, a task review gates it, the user merges. Steps use `- [ ]`.
>
> **Companion design spec (read first, it is the north star):**
> `docs/superpowers/specs/2026-07-13-unified-extraction-labeling-design.md` — §6 (authority migration, THE section this plan implements), §2 (current state), §5 (data model), §8 Task 4, plus the §6 breadcrumb block (2026-07-14).
> Sibling plan whose shipped state this builds on: `docs/superpowers/plans/2026-07-13-programme-impact-link.md` (findings/0010, artifact v2, D5 same-day-only, D8 double-write — all live on TEST).

## ⚠ RETARGET 2026-07-15 — post-prod-isolation (READ FIRST; overrides everything below on conflict)

This plan was written 2026-07-14, before prod isolation shipped. Prod isolation (`docs/superpowers/plans/2026-07-14-prod-isolation.md`) inverted its targets; the following overrides apply. The plan's §0 file:line evidence remains valid at the CODE level — only WHICH STACK runs that code changed. **Prerequisite:** execute `docs/superpowers/plans/2026-07-15-schedules-cutover.md` (timeliness Phase A) before Tasks 8-9, so intraday recordings exist for the soaks to observe.

**Re-verified live 2026-07-15 (replaces Task 0 Steps 1 & 3):** the lake's 8 S3 notifications now route to `fieldsight-prod-*` (vad/transcribe/embed-report/ingest/extract-session/item-writer/programme-matcher) — the nightly wipe therefore lives in **`fieldsight-prod-ingest`**. Amplify app `d2fssznicvuckr` (on 509194952652, deployer profile works): branch `main` (customer) has `FS_BASEURL` **and** `FS_ORG_BASEURL` = `https://ys94qy2tk0.execute-api.ap-southeast-2.amazonaws.com/prod/api`; branch `dev` keeps legacy `khfj3p1fkb` + test-org `wdsgobb7b0`. `deploy-prod.yml` invokes `fieldsight-prod-migrate` on every deploy (idempotent, shared `schema_migrations`).

**Global overrides:**

1. **Active runtime = `fieldsight-prod-*`.** Every "fieldsight-test-<fn>" runtime target below (ingest defer, item-writer, org-api shim serving the flipped UI, live smokes) is ACTIVATED as `fieldsight-prod-<fn>`. The test stack remains the develop-CI integration stage: it shares Aurora (live-items/shim override path testable there) but is severed from the lake (PR #57, `IngestBucketName`=test bucket) — its S3-verbatim branch cannot serve lake history; unit tests + prod smokes cover that branch.
2. **Deploy flow per backend task:** PR → `develop` (CI deploys test stack; unit tests green; test-stack checks where possible) → promote develop→main PR → `production` gate approval → prod stack deploys + migrations auto-apply → live smokes against `fieldsight-prod-*`. Tasks 1-4 and 7 may promote as one batch or per-task; nothing activates user-visibly until Tasks 8/9 flip flags.
3. **Migration 0011:** Aurora is SHARED — landing it via the develop/test deploy applies it for prod lambdas too (additive columns, safe). No second apply needed; deploy-prod re-runs migrate idempotently.
4. **MATCH gate:** already enforced in CI by `deploy-prod.yml`; manual pre-merge checks stay as written.
5. **Task 4 (shim):** `LAKE_BUCKET: !Ref IngestBucketName` resolves to the LAKE on prod (correct) and the TEST BUCKET on test (S3-verbatim branch inert there — accepted; see override 8 for the dev-site consequence). Multi-tenant: D10's ACL-before-any-S3-read is now load-bearing for real customer tenants — `folder_name` is globally unique (MultiTenantResolution), and the ACL must gate BOTH the Aurora override rows and the verbatim document read, exactly as D10 states.
6. **Task 5 (differ):** `PROD_BASE` = the customer site's CURRENT report path = `https://ys94qy2tk0.execute-api.ap-southeast-2.amazonaws.com/prod/api` (prod-stack api lambda, same lake documents); `ORG_BASE` = the same gateway (org routes under `/api/org`). A khfj3p1fkb comparison run is optional extra evidence, not the gate.
7. **Task 6 (UI):** code changes unchanged; they ship to ui `dev` AND must be promoted dev→main (Amplify main auto-builds) with `FS_TIMELINE_SOURCE` unset on both branches → default `'report'`, zero behavior change until Task 8.
8. **Task 8 (enable the UI flag) targets Amplify branch `main` (the customer site)** — update-branch with the FULL existing var map + `FS_TIMELINE_SOURCE=aurora`, E2E on `https://main.d2fssznicvuckr.amplifyapp.com/`. The dev site is NOT flipped in v1 (its org gateway is the lake-severed test stack). Follow-up ledger addition: dev-site flip needs test org-api read-only lake access (`reports/*` GetObject via a template param) or re-pointing dev's `FS_ORG_BASEURL` at the prod gateway.
9. **Task 9 (`AuthorityFlip=true`)** is a one-line parameter-overrides addition in **`.github/workflows/deploy-prod.yml`** (not deploy.yml), via PR → main → gate. The template param from Task 7 must default `'false'` so the un-overridden test stack (deploy.yml) stays legacy. Emergency rollback: console env edit `AUTHORITY_FLIP=false` on **`fieldsight-prod-ingest`** + `FS_TIMELINE_SOURCE=report` on Amplify `main`; re-supersede via `aws lambda invoke --function-name fieldsight-prod-ingest --payload '{"date":"<D>","user":"<U>"}'` per pair.
10. **Wipe timing post-Phase-A:** the daily report generates at 16:00 UTC (04:00 NZST) → prod ingest wipes ~04:05 NZST. "Before 05:00 NZDT" verification windows remain valid; after Task 9 they dissolve.
11. **Task 0 residual (still required, read-only):** (a) `ys94qy2tk0` `/api/{proxy+}` integration → `fieldsight-prod-api` with `S3_BUCKET`=lake; (b) prod org-api `/live-items` smoke with a prod-pool idToken (customer-site auth path); (c) record a pilot `extractions/...` source_s3_key (Phase A Task 2 usually produces one).
12. **Global Constraints amendment:** "never sam deploy against prod / fieldsight-test stack ONLY" becomes: prod stack deploys ONLY via `deploy-prod.yml` + `production` gate (never local `sam deploy`); the hand-built legacy lambdas/gateway (`khfj3p1fkb`, unprefixed `fieldsight-*`) remain untouched as before.

---

**Goal:** Make the real-time extraction (Aurora topics + findings) authoritative for the item store — stop the nightly overwrite — and migrate the main UI read path (Today/Timeline and every other `getTimeline` consumer) onto the authoritative topics, without losing the daily_report.json document, without blanking history, and with a two-layer kill switch.

**Architecture (one paragraph):** The UI's single timeline fetch seam (`fieldsight-ui/scripts/api/timeline.js`) is re-routed, behind a deploy-time flag, from the prod hand-built `fieldsight-api` (`/api/timeline`, S3 daily_report.json) to a NEW compatibility endpoint `GET /api/org/timeline` on the in-VPC TEST `org-api` — which serves the S3 `daily_report.json` VERBATIM for every day that has no extraction topics (byte-identical history, diffable), and renders Aurora extraction topics INTO the daily_report.json shape only for days that have them. On the write side, `lambda_ingest` gains a flag-gated day-level defer: when extraction topics exist for `(user, date)`, the nightly ingest no longer deletes them and no longer writes report topics (chunks/RAG unaffected); `item-writer`'s I-4 gate is left untouched — its inversion is emergent. The report DOCUMENT (prod report-generator, Word/JSON) keeps being produced unchanged.

**Tech Stack:** Python lambdas (SAM, `fieldsight-test` stack), Aurora PG (psycopg, in-VPC), S3 prod lake, pure-frontend React (no build step) in `fieldsight-ui`.

---

## §0 Investigation record (file:line evidence — verified 2026-07-14)

### The nightly overwrite (spec §2/§6)

1. **The wipe:** `src/lambda_ingest.py:297` — `topics.delete_topics_for_source_prefix(conn, f"extractions/{user_folder}/{date}/")` runs inside every report ingest ("Nightly report supersedes that day's session-sourced items — Phase 4b"), cascading children incl. `findings` + programme-impact links (`src/repositories/topics.py:78-103`, LIKE-escape at :98). Then :301-323 re-inserts topics from `report["topics"]` (no findings — reports have none), and :353-354 emits a `match_requests/` artifact from the report topics.
2. **The trigger chain is CROSS-STACK:** the 05:00 NZDT cron (`cron(0 16 * * ? *)`, `src/template.yaml:498-505`) is DISABLED on TEST (`deploy.yml:75` `EnableSchedules=false`). The real chain: **prod** hand-managed scheduler → **prod** `fieldsight-report-generator` → writes `reports/{date}/{user}/daily_report.json` to the **prod lake** (`fieldsight-data-509194952652`) → the prod lake's HAND-MANAGED S3 notifications (see `scripts/wire-s3-events.sh:84-91` comment: "The REAL lake trigger lives on the prod bucket … managed MANUALLY there") → **TEST** `fieldsight-test-embed-report` → `embeddings/*vectors.json` → **TEST** `fieldsight-test-ingest` → TEST Aurora. So stopping the overwrite is a change to `fieldsight-test-ingest` only (in git, SAM-deployed, low blast radius); prod report-generator and its cron are untouched, and `daily_report.json` keeps being produced — exactly spec §6's "decouple the report DOCUMENT from the item store".
3. **What else ingest does that must NOT stop:** `chunks.delete_chunks_for_source` + chunk inserts (:293, :325-345) feed RAG (`report_chunks`, `lambda_rag_search.py:85-92`); chunk `topic_id` is nullable passthrough metadata (transcript-window chunks already carry `topic_id=None` when `topic_seq_to_id` misses — :331, :342) and rag-search citations key on chunk metadata/topic_title, not topic_id (`ui .../ask-chat.js:69` uses `c.topic_title`). Chunks keep flowing post-flip with `topic_id=None`.

### item-writer's I-4 gate

4. `src/lambda_item_writer.py:106-119` — skip the extraction write if `reports/{date}/{user}/daily_report.json` topics already exist ("late session extraction superseded"). **Post-flip this inverts EMERGENTLY with zero code change:** once ingest defers report-topic writes on extraction days (Task 7), the I-4 probe (`SELECT 1 FROM topics WHERE source_s3_key=%s` for the report key) finds nothing on normal days → extraction always writes; on a zero-extraction outage day ingest falls back to writing report topics → a later-landing extraction still skips → no duplicate day is representable in either order. I-4 stays as the outage-day guard.

### The read path (the risky change) — dual-lambda reality

5. **UI fetch seam:** `fieldsight-ui/scripts/api/timeline.js:31-36` — the ONLY live fetch: `window.FS.api.request('/timeline', {params:{date,user}})`. `request` resolves `FS.api.baseUrl` (`scripts/api/_fetch.js:137-140`), which the Amplify dev site sets to the **prod** gateway: `FS_BASEURL=https://khfj3p1fkb.execute-api.ap-southeast-2.amazonaws.com/prod/api` (`docs/MIGRATION-HANDOFF-2026-07-04.md:19-21`; Amplify app `d2fssznicvuckr`, branch `dev`). `khfj3p1fkb` is the HAND-BUILT prod gateway → **prod `fieldsight-api`** lambda: `src/lambda_fieldsight_api.py:219-291` `get_timeline` reads `S3_BUCKET=fieldsight-data-509194952652` (:50) key `reports/{date}/{user}/daily_report.json` (:256), admin-no-user path serves `summary_report.json` or the `available_users` envelope (:240-291). **NOT Aurora, non-VPC, no PG env/layer.** Its CODE is in git and code-deployable (`scripts/deploy-lambda-code.sh:27-37` maps `[api]=lambda_fieldsight_api`; `deploy-prod-code.yml`, main branch, approval-gated), but its INFRA (gateway routes, env, VPC-lessness) is hand-managed — putting Aurora access into it means hand-editing prod lambda networking + a coupled 9-lambda main deploy. Rejected (see §1).
6. **The second lambda/gateway:** the SAM TEST stack's own gateway (docs: `wdsgobb7b0`) serves `/api/org/{proxy+}` → `fieldsight-test-org-api` (`src/template.yaml:763-769`), in-VPC with PG. The UI ALREADY dual-routes: `FS.api.orgBaseUrl` (`FS_ORG_BASEURL` Amplify var) + `orgRequest` (`_fetch.js:235-243`, prefixes `/org`), used live today by org pages, `/live-items` (`scripts/api/org.js:254-261`), programme suggestions, `/ask` and `/api/search` (`scripts/api/ask.js:24`, `search.js:39`). Auth: the org authorizer accepts prod-pool (`ap-southeast-2_q88pd6XXr`) idTokens (phase-3 plan, verified live by the running org UI); CORS to `*.amplifyapp.com` is wired (`scripts/wire-bucket-cors.sh`, gateway CORS per phase-3).
7. **Dual-bucket reality is SOFTER than feared:** the TEST stack's whole extraction/ingest chain reads the **prod lake** — `IngestBucketName` default `fieldsight-data-509194952652` (`src/template.yaml:65-73`), used as `S3_BUCKET` by extract-session (:834), ingest (:893), embed-report (:952), item-writer (:1013), matcher (:1119). So TEST Aurora is fed FROM the same lake prod `fieldsight-api` serves — one data source, two read paths. The org-api shim reading `reports/*` from the SAME lake closes the loop with an IAM grant, not a data migration.
8. **`getTimeline` consumers (ALL inherit the flip through the one seam):** `pages/today.js:406,460,598` (today extras + admin fan-out + rolling open-items loader), `pages/timeline.js:383,767,803` (aggregated day view, date bootstrap, day view), `pages/evidence.js:243`, `pages/safety.js:560`, `pages/quality.js:562`, `pages/programme.js:1467`, `api/tasks-aggregator.js:151,167`, `api/compliance-aggregator.js:264,288`, `api/user-activity-aggregator.js:151`. Fields consumed (fixture contract `scripts/mock/daily-report.fixture.js:10-16`, adapter `scripts/api/today-adapter.js`, `composites/topic-card.js:4-12`): report-level `report_date`/`site`/`user_name`/`executive_summary`/`safety_observations`; per-topic `topic_id` (int, keys the DynamoDB check-off audit `<folder>|<topic_id>_<action_index>`), `time_range` (EN-DASH, drives ordering + transcript/audio deep-links), `topic_title`, `category`, `participants`, `summary`, `key_decisions`, `action_items[{action,responsible,deadline,priority}]`, `safety_flags[{observation,risk_level,recommended_action}]`, `related_photos` (filenames).
9. **The producer shape:** `src/lambda_report_generator.py:141-196` (DAILY_REPORT_SCHEMA — the exact shape the shim must render); photos live at `users/{user}/pictures/{date}/` and are time-correlated (:386-402, :1070-1078).

### Aurora item store vs daily_report.json shape (the gaps)

10. `/live-items` (`src/lambda_org_api.py:706-716`) already exposes `topics.list_topics_for_date` (`src/repositories/topics.py:112-181`): per topic `id (uuid)`, `site_name`, `user_name`, `category`, `title`, `summary`, `occurred_at` (ALWAYS NULL today — neither writer sets it), `action_items[{text,responsible,deadline(date|None),priority,status}]`, `safety_observations[{observation,risk_level,location,status}]`, `findings[...]`, `is_live`. **Missing vs the report shape:** `time_range` and `participants` (present in the extraction JSON — `src/lambda_extract_session.py:146-147` — but DROPPED at the Aurora boundary: `upsert_topic` has no such params, `topics.py:9-43`), `related_photos` (extraction JSON has none; `topic_photos` table exists but nothing writes it), free-text `deadline` (nulled to SQL-date-or-NULL by `lambda_ingest._map_action_items:183-201`), `key_decisions` (extraction `decisions` exist in JSON but have no table — deliberately deferred in 0010, sibling plan D1), report-level `executive_summary`/`safety_observations`. Tasks 1-3 close the user-visible gaps at WRITE time; the shim sources report-level prose from the (still-produced) report document.
11. **Compliance live-merge duplication hazard:** `api/compliance-aggregator.js:415-520` merges `org.getLiveItems` rows (source `'live'`) IN ADDITION to `getTimeline` rows. Today past days have no live topics (wiped nightly). Post-flip, persistent extraction topics would appear TWICE in Safety/Quality (once via the shim'd `getTimeline`, once via the live merge) — the live merge must be gated off when the flag routes timeline to Aurora (Task 6), and the flip must be enabled UI-first (see §2 D9 ordering).
12. **Rollup / D8 bridge:** `repositories/rollup.py` counts `safety_observations`; the extraction path double-writes safety findings into `safety_observations` (PR #46 bridge) + `findings` (0010). Post-flip, report topics stop being written on extraction days, so safety counts come from the bridge rows — no double count, no zero-out. Bridge retirement (repoint rollup at `findings`) stays a SEPARATE post-flip cleanup (spec §6 breadcrumb item 2) — explicitly out of scope here.
13. **Human edits:** none can exist yet (the correction loop is spec §8 Task 5, unbuilt; org `observations` are a different table/lineage, untouched). See D8 for the minimum this phase carries.
14. **Latest migration is `0010_findings.sql`** → this plan's migration is **0011**. Users rows carry `folder_name` (`repositories/users.py:3`, `get_by_folder_name:38-42`) — the shim's user resolution needs no new identity machinery.

## §1 The CRUX — compat shim vs new endpoint, resolved WITH the dual-lambda reality

**Decision: spec §6 option (b) — a compatibility shim that renders Aurora topics into the daily_report.json shape — hosted on `org-api` (TEST gateway), reached by a frontend routing flag. Not option (a), and not a backend change to prod `fieldsight-api`.**

- **Why the shape must stay:** the DailyReport shape is not a Today/Timeline contract — it is a UI-WIDE contract consumed by 9+ surfaces through one seam (§0.8). Option (a) (new shape, UI switches) means re-adapting every consumer — weeks of UI churn and per-surface regression risk. Option (b) changes ONE function (`timeline.js:fetchTimeline`) and every consumer inherits it.
- **Why the shim lives on org-api and not fieldsight-api:** the authoritative store is TEST Aurora, reachable only in-VPC (BUG-36). `fieldsight-api` is the hand-built, non-VPC prod lambda — giving it Aurora access means hand-managed VPC/env/layer surgery on prod infra outside IaC, deployed via the all-9-lambdas main workflow. `org-api` is in-VPC, in git, SAM-deployed, already carries the topics repo, the ACL (`/live-items`), and the UI's second base URL with working cross-pool auth. **The read-path migration is therefore a FRONTEND ROUTING change (baseUrl seam), not a backend change to the prod lambda.**
- **Why "verbatim S3 unless extraction topics exist":** rendering Aurora *report-sourced* topics back into report shape would lossily re-derive what the S3 document already states perfectly (and would blank identity-bridge-miss days that were never ingested). Serving the S3 document VERBATIM for non-extraction days makes the shim **byte-identical to prod `/api/timeline` for all history** — mechanically diffable (Task 5) — and confines behavior change to exactly the flip-era extraction days.
- **Dual-lambda residual risk, stated honestly:** after the flip, the dev site's PRIMARY read path depends on the TEST stack (org gateway availability, Aurora, the PGPASSWORD rotation trap, every develop-merge redeploy). Mitigations: two-layer kill switch (UI flag per-session via query param / site-wide via Amplify env; client-side transport-error fallback to the prod path), the MATCH gate before every merge, rotation already disabled (2026-07-10). This coupling is the EXISTING posture for ask/search/org/programme — the flip extends it to timeline, it does not create it. **Not a hard blocker** (see Risks #1 and the §30 note).
- **Is #30 prod-isolation a prerequisite?** No. The flip's data coherence holds because TEST Aurora is fed from the prod lake (§0.7). Prod isolation later will have to move ONE more UI surface (`timelineSource` routing) together with orgBaseUrl — the flag design keeps that a config change. Do not block the flip on it; do record the added surface in the #30 notes (Task 10).

## §2 Design decisions

- **D1 — Shim contract:** `GET /api/org/timeline?date=YYYY-MM-DD&user=<Folder_Name>` returns EXACTLY one of: (i) the S3 `daily_report.json` VERBATIM (no extraction topics for that user+date), (ii) an Aurora-rendered DailyReport-shaped object (extraction topics exist — the override), (iii) the prod 404 body `{message, date}` (status 404), (iv) admin-no-user: `summary_report.json` verbatim, else single-report verbatim, else `{date, available_users:[...]}` — mirroring `lambda_fieldsight_api.py:240-291`, with Aurora extraction users UNIONED into `available_users`.
- **D2 — Override detection is by SOURCE PREFIX, not user_id:** extraction topics for the target are `source_s3_key LIKE 'extractions/{folder}/{date}/%'` (escape-aware) — immune to identity-bridge drift (extraction rows can carry `user_id=NULL`). New repo fns share the existing LIKE-escape logic.
- **D3 — Aurora-rendered topic mapping (the shim's core):** `topic_id` = positional index over rows ordered by `time_range NULLS LAST, created_at, id` (content-stable ordering; positional ids match the report path's 0-based ints so audit keys keep their shape); `time_range`/`participants` from the new 0011 columns; `action_items.action`←`text`, `deadline` = `deadline_text` (new column) else ISO date else null; `safety_flags` derived from `findings` where `domain='safety'` (severity→risk_level via the PR #46 map, carries `recommended_action`), falling back to `safety_observations` child rows for pre-#46 legacy extractions; `related_photos` = `topic_photos` filenames (Task 3); `key_decisions: []` in v1 (decisions/questions tables stay deferred — no consumer renders a gap as an error; follow-up noted in Task 10); plus an ADDITIVE `findings` passthrough (the UI ignores unknown keys today; the correction-loop UI will want them).
- **D4 — Report-level prose comes from the DOCUMENT:** on override days the shim merges `executive_summary`, `safety_observations`, `quality_and_compliance`, `critical_dates_and_deadlines` from the S3 report document when it exists (it lands at 05:00 next day; same-day these are empty — Today's morning brief for the live day was ALWAYS empty, no regression). This is spec §6's decoupling made literal: items from the item store, prose from the report artifact.
- **D5 — Ingest defer is DAY-LEVEL and flag-gated (`AUTHORITY_FLIP` env, default false):** when ON and extraction topics exist for `(user_folder, date)`: still `delete_chunks_for_source` + `delete_topics_for_source(report_key)` (clears stale pre-flip report rows on re-ingest), **skip** `delete_topics_for_source_prefix("extractions/…")`, **skip** report-topic inserts, write all chunks with `topic_id=None`, emit NO match_request (the extraction path already emitted). When OFF, or when NO extraction topics exist (extraction outage day): legacy behavior verbatim — report topics as fallback. `item-writer` I-4: unchanged (emergent inversion, §0.4) — only its comment is updated.
- **D6 — Rollback is layered and rehearsed (Task 10 documents the drill):** UI layer: `?timelinesrc=report` (instant, per-session) / Amplify `FS_TIMELINE_SOURCE=report` (site-wide, one build); transport-error auto-fallback to the prod path is built into timeline.js. Backend layer: `AUTHORITY_FLIP=false` redeploy, then re-supersede affected days with manual ingest invokes (`{"date": D, "user": U}` per pair — legacy path deletes the persistent extraction topics and restores report topics) → store returns to pre-flip state exactly. Nothing in the flip is a one-way door.
- **D7 — Enablement ORDER is UI-first, write-flip second.** UI flag ON while the overwrite still runs is safe (past days: shim serves S3 verbatim = current behavior; today: extraction topics show live, wiped at 05:00 into the report version — a strict preview). Write-flip ON while the UI still reads prod `/api/timeline` is NOT safe to leave long (persistent `is_live` rows double-display in Safety/Quality via the live merge, §0.11). Sequence: Task 8 (UI on, soak) → Task 9 (flip on, soak).
- **D8 — Human-edit minimum for THIS phase:** migration 0011 adds `topics.source text NOT NULL DEFAULT 'ai'` as a PASSIVE column (no reader, no writer besides the default). Rationale: no human edits can exist before the correction loop (Task 5 of spec §8), so enforcement now is speculative; but the delete paths (`delete_topics_for_source*`) are exactly where Task 5 must add `AND source != 'human'` guards + `ai_original` stashing — landing the column now means Task 5 is a WHERE-clause + columns-on-children change, not another topics migration mid-flight. The flip itself adds NO new overwrite paths (it only removes one), so the spec's "human edits never overwritten" invariant is not weakened by shipping protection later.
- **D9 — `/api/dates` does NOT move in v1.** The report document keeps being generated nightly, so the S3-driven calendar dots and the rolling-loader span are unchanged. Same-day items still surface (Today's `loadFor` hits `getTimeline(today)` directly, `today.js:406`); the only v1 gap is that TODAY's action items don't join the rolling open-items list until the next morning (when the report doc dots the date and the shim override serves the persistent extraction topics). An Aurora-backed `/dates` is a cheap follow-up (`SELECT DISTINCT report_date` …), noted in Task 10 — not in the critical path.
- **D10 — Shim ACL v1:** mirrors `/live-items` (`_allowed_site_ids` / `resolve_scope`): scope `ALL` (admin/gm) may request any folder + gets the no-user disambiguation path; everyone else is FORCED to their own `caller["folder_name"]` (403 with a clear message if unset). This is TIGHTER than prod's site_manager-sees-workers rule — acceptable for the pilot (the dev site's live users are admin); the relaxation (site-scoped folder access via memberships) is a noted follow-up, and the S3-verbatim branch must apply the SAME ACL before returning a document (no S3 read for a folder the caller may not see).

## What moves / what stays (read-path inventory)

| Read | Today | After flip |
|---|---|---|
| Topics for a (date,user) — Today, Timeline, Evidence, Safety/Quality day view, aggregators | prod `/api/timeline` (S3 report) | `org /api/org/timeline` (verbatim S3, or Aurora override) — **frontend routing flag** |
| Calendar dots `/api/dates` | prod api (S3 listing) | unchanged (D9) |
| Action check-off `/api/actions*` (DynamoDB) | prod api | unchanged |
| Transcripts/audio/video/photo presign | prod api | unchanged (extraction topics carry `time_range`, so deep-links keep working) |
| Meeting minutes fetch | prod api presigner | unchanged (meeting-minutes retirement is spec §8 item 7, later) |
| `/live-items` merge in Safety/Quality | org api | gated OFF when flag routes timeline to Aurora (dupe prevention, §0.11) |
| `/ask`, `/api/search`, org pages, programme | org/test gateway | unchanged |

## Global Constraints (every task inherits these)

- **Account 509194952652 / ap-southeast-2 / `fieldsight-test` stack ONLY.** Never `sam deploy` against prod; never touch the hand-built prod lambdas/gateway (`khfj3p1fkb`), prod report-generator, or its scheduler. The one prod-adjacent artifact (Amplify env vars, app `d2fssznicvuckr`) is a console/CLI config change, reversible.
- **Migration number is 0011** (0010_findings latest). Apply via `aws lambda invoke --function-name fieldsight-test-migrate`; verify with Aurora Data API.
- **Merge to `develop` auto-triggers the SAM test deploy.** Before merging ANY backend PR, confirm **`DbSecret.password == a live in-VPC lambda's PGPASSWORD` (MATCH)** (e.g. `fieldsight-test-rag-search`) — the rotation trap (memory: rotation disabled 2026-07-10, still verify).
- **UI repo (`C:/Users/camil/Dropbox/fieldsight-ui`)**: no build step, no npm; `node --check` every touched JS; bump `?v=N` cache busters on preview HTMLs for changed files; branch off **`dev`** (the Amplify-deployed branch — NOT the sprint10/11 branches; confirm with the user if sprint branches have merged since); token/mock/BEM conventions per that repo's CLAUDE.md.
- **Fail-safe reads:** the shim must never 500 a day into blankness — S3 miss → prod-identical 404 body; Aurora empty → S3 verbatim; UI transport error on the org path → prod path fallback.
- **Postgres traps:** `%s::text IS NULL` casts for null-or-equals params; `Jsonb(...)` for jsonb; LIKE patterns must escape `_`/`%` (reuse `topics.py:98`'s escaping — never re-derive); mock-green ⇒ still do a Data-API/live verification.
- **Windows/AWS quirks:** `MSYS_NO_PATHCONV=1` for `/aws/...` ARNs; `cygpath -w` for `fileb://`; `node` (not python3) for shell JSON (BUG-29); single-line Edit anchors (CRLF repos); never `git add -A`.
- **User merges PRs.** Each task = one branch → PR. Do not touch `programme_progress_suggestions`, the matcher, the suggestion flow, `rollup.py`, or the PR #46 safety bridge (D8 double-write stays until the separate retirement task).
- **Same-day verification windows:** any live check of extraction topics/findings on a NOT-yet-flipped stack must run before 05:00 NZDT (the wipe). After Task 9 this constraint dissolves — that is the point.

## File Structure

| File | Create/Modify | Responsibility |
|---|---|---|
| `src/migrations/0011_authority_flip.sql` | Create | topics: `time_range`, `participants`, `source` (passive); action_items: `deadline_text` |
| `src/repositories/topics.py` | Modify | pass-through new cols; `_escape_like` factored; `has_topics_for_source_prefix`; `list_topics_for_source_prefix` (children incl. photos) |
| `src/lambda_ingest.py` | Modify | `_map_action_items` carries `deadline_text`; report path passes time_range/participants; Task 7: `AUTHORITY_FLIP` defer |
| `src/lambda_item_writer.py` | Modify | passes time_range/participants; Task 3 photo attach; I-4 comment update (no behavior change) |
| `src/lambda_org_api.py` | Modify | `GET /timeline` shim (dispatch + handler + S3 doc reads) |
| `src/template.yaml` | Modify | OrgApiFunction: `LAKE_BUCKET` env + reports/* IAM; Ingest/OrgApi: `AuthorityFlip` param → `AUTHORITY_FLIP` env (default 'false') |
| `scripts/compare-timeline.mjs` | Create | parallel-run diff harness (prod vs shim), node |
| `tests/unit/test_topics_repo.py`, `test_lambda_ingest.py`, `test_lambda_item_writer.py`, `test_lambda_org_api.py` | Modify | TDD per task |
| `fieldsight-ui/scripts/api/index.js` | Modify | `timelineSource` from `FS_ENV` |
| `fieldsight-ui/scripts/api/timeline.js` | Modify | flag-gated org routing + fallback + cache key |
| `fieldsight-ui/scripts/api/compliance-aggregator.js` | Modify | live-merge gate under the flag |
| `fieldsight-ui/amplify.yml`, `env.example.js`, `app-shell-preview.html` | Modify | env plumbing, `?timelinesrc=` override, cache busters |
| `CLAUDE.md` (pipeline), spec §6, memory | Modify | Task 10 docs/breadcrumbs |

---

### Task 0 — Live-infrastructure verification gate (read-only; STOP on any mismatch)

The planning session's AWS session expired mid-verification; these documented facts MUST be re-verified live before any code lands. All read-only. Run `aws login` first.

- [ ] **Step 1 — prod lake notifications point at the TEST extraction/ingest chain.**
```bash
export MSYS_NO_PATHCONV=1
aws s3api get-bucket-notification-configuration --bucket fieldsight-data-509194952652 --region ap-southeast-2 --output json
```
Expected: `LambdaFunctionConfigurations` entries routing `transcripts/*.json` → `fieldsight-test-extract-session`, `extractions/*.json` → `fieldsight-test-item-writer`, `reports/*daily_report.json` → `fieldsight-test-embed-report`, `embeddings/*vectors.json` → `fieldsight-test-ingest`, `match_requests/*.json` → `fieldsight-test-programme-matcher`. **If any of these route to `fieldsight-*` (prod) functions instead, STOP — the overwrite lives elsewhere and Task 7's target is wrong.**
- [ ] **Step 2 — the dev site's `/api/*` really is prod `fieldsight-api` + prod lake.**
```bash
aws apigateway get-resources --rest-api-id khfj3p1fkb --region ap-southeast-2 --query "items[?path=='/api/{proxy+}'].id" --output text
# then, with <RESOURCE_ID>:
aws apigateway get-integration --rest-api-id khfj3p1fkb --resource-id <RESOURCE_ID> --http-method ANY --region ap-southeast-2 --query uri --output text
aws lambda get-function-configuration --function-name fieldsight-api --region ap-southeast-2 --query "{S3:Environment.Variables.S3_BUCKET,VPC:VpcConfig.VpcId}"
```
Expected: integration URI contains `function:fieldsight-api`; `S3=fieldsight-data-509194952652`; `VPC=null` (non-VPC — confirming the "backend option" is infra surgery).
- [ ] **Step 3 — Amplify env vars (company-account credentials if the app is not on 509194952652).**
```bash
aws amplify get-branch --app-id d2fssznicvuckr --branch-name dev --region ap-southeast-2 --query "branch.environmentVariables"
```
Expected: `FS_BASEURL=https://khfj3p1fkb...../prod/api`, `FS_ORG_BASEURL=https://<test-api-id>...../prod/api` (docs say `wdsgobb7b0` — record the actual id), `FS_USEMOCKS=false`. **If `FS_ORG_BASEURL` is empty, STOP — the org channel the flip rides on isn't live.**
- [ ] **Step 4 — org-api serves `/live-items` with findings to a prod-pool token (also proves auth+CORS+Aurora content).** Reuse the established idToken curl flow (Ben Lin admin): `GET <FS_ORG_BASEURL>/org/live-items?date=<a day with extractions before 05:00>` → topics with `findings[]`. Record one `source_s3_key` starting `extractions/` as the Task 5 pilot target.
- [ ] **Step 5 — record findings in the PR description of Task 1** (gateway ids, notification map, Amplify vars) so later tasks don't re-derive them.

**Done:** every documented assumption confirmed live, or the plan is halted and re-scoped.

---

### Task 1 — Migration 0011 + repository pass-through

**Files:** Create `src/migrations/0011_authority_flip.sql`; modify `src/repositories/topics.py`; extend `tests/unit/test_topics_repo.py`.

**DDL (0010 header-comment style):**
```sql
-- 0011: authority flip (unified-extraction Task 4 — spec §6).
-- topics.time_range/participants: display fields the extraction JSON already
-- carries (lambda_extract_session EXTRACTION_SCHEMA) but the Aurora boundary
-- dropped; needed so the org-api timeline shim can render the
-- daily_report.json shape from extraction-sourced topics.
-- topics.source: PASSIVE provenance column (default 'ai') — no reader/writer
-- yet; the correction loop (spec §8 Task 5) adds source='human' + delete
-- guards. Landed now so Task 5 needs no topics migration.
-- action_items.deadline_text: raw free-text deadline ("Tomorrow 08:00") the
-- date-typed column cannot hold (lambda_ingest._map_action_items nulls it).
ALTER TABLE topics ADD COLUMN time_range   text;
ALTER TABLE topics ADD COLUMN participants jsonb;
ALTER TABLE topics ADD COLUMN source       text NOT NULL DEFAULT 'ai';
ALTER TABLE action_items ADD COLUMN deadline_text text;
```

**Repo changes (`src/repositories/topics.py`) — later tasks depend on these EXACTLY:**
- `_TOPIC_COLS` and `_TOPIC_COLS_JOINED` gain `time_range, participants, source`.
- `upsert_topic(..., time_range=None, participants=None)` — two new kwargs, inserted; `participants` bound via `Jsonb(participants) if participants is not None else None`. `action_items` child INSERT gains `deadline_text` from `a.get("deadline_text")`.
- `_escape_like(prefix) -> str` — factored from `delete_topics_for_source_prefix:98` (single definition, three users).
- `has_topics_for_source_prefix(conn, source_prefix) -> bool` — `SELECT 1 FROM topics WHERE source_s3_key LIKE %s ESCAPE '\\' LIMIT 1` over `_escape_like(prefix) + '%'`.
- `list_topics_for_source_prefix(conn, source_prefix) -> list[dict]` — same JOIN + batched children as `list_topics_for_date` (action_items now incl. `deadline_text`; safety_observations; findings via `findings.list_for_topics`) **plus a fourth batched child `photos`** (`SELECT id, topic_id, s3_key, caption_text FROM topic_photos WHERE topic_id = ANY(%s) ORDER BY created_at`), `WHERE source_s3_key LIKE … ESCAPE '\\'`, `ORDER BY time_range NULLS LAST, created_at, id` (D3 stable ordering).
- `list_topics_for_date` also attaches nothing new beyond the added SELECT columns (live-items consumers get `time_range`/`participants`/`source` for free — additive).

- [ ] **Step 1 — failing tests** (FakeConn/monkeypatch pattern already in `test_topics_repo.py`): `test_upsert_passes_time_range_participants_jsonb`, `test_upsert_action_items_carry_deadline_text`, `test_has_topics_for_source_prefix_escapes_like`, `test_list_for_source_prefix_orders_by_time_range_and_batches_four_children`, `test_list_topics_for_date_selects_new_columns`. Run `pytest tests/unit/test_topics_repo.py -v` → FAIL.
- [ ] **Step 2 — DDL + repo as specified.** `pytest tests/unit -q` → green.
- [ ] **Step 3 — PR → MATCH check → user merges → CI deploys → migrate.** `aws lambda invoke --function-name fieldsight-test-migrate ...`; Data-API `SELECT column_name FROM information_schema.columns WHERE table_name='topics' AND column_name IN ('time_range','participants','source')` → 3 rows; same for `action_items.deadline_text`.
- [ ] **Step 4 — commit** `feat(db): 0011 authority-flip columns (topics.time_range/participants/source, action_items.deadline_text) + repo pass-through`.

**Done:** columns live on TEST; repo unit-tested; NO behavior change anywhere (writers don't pass the new kwargs yet).

---

### Task 2 — Write-path enrichment (item-writer + ingest pass the display fields)

**Files:** Modify `src/lambda_item_writer.py` (:147-158 upsert call), `src/lambda_ingest.py` (`_map_action_items:183-201`, upsert call :301-309); extend `tests/unit/test_lambda_item_writer.py`, `tests/unit/test_lambda_ingest.py`.

**Changes:**
1. `lambda_ingest._map_action_items`: each mapped dict gains `"deadline_text": a.get("deadline")` (the RAW string, before the ISO-only filter — the date column logic is untouched).
2. Both writers' `topics.upsert_topic(...)` calls add `time_range=t.get("time_range"), participants=t.get("participants")` — extraction topics (item-writer) AND report topics (ingest; report topics carry both per DAILY_REPORT_SCHEMA) get them. Missing keys → None → NULL (legacy JSONs safe).
3. NOTHING else moves: safety bridge untouched (D8), artifact untouched, I-4 untouched.
4. **No history backfill** — the shim serves S3 verbatim for report-sourced days (D1), so NULL `time_range` on historical Aurora rows is invisible to the UI. Do not add a backfill step.

- [ ] **Step 1 — failing tests:** `test_item_writer_passes_time_range_and_participants`, `test_ingest_passes_time_range_and_participants`, `test_map_action_items_carries_raw_deadline_text_alongside_date_filter`, `test_legacy_extraction_without_time_range_writes_null`. FAIL.
- [ ] **Step 2 — implement.** `pytest tests/unit -q` green.
- [ ] **Step 3 — PR → MATCH check → merge → deploy → live smoke (before 05:00 NZDT):** re-trigger one real extraction (`aws s3 cp s3://fieldsight-data-509194952652/extractions/<key> s3://.../<key> --metadata-directive REPLACE --region ap-southeast-2`) → Data-API: `SELECT time_range, participants FROM topics WHERE source_s3_key='<key>'` non-null; one `action_items.deadline_text` populated where the extraction had a free-text deadline.
- [ ] **Step 4 — commit** `feat(writers): persist time_range/participants/deadline_text (authority-flip display parity)`.

**Done:** extraction-sourced Aurora topics carry everything the shim needs except photos.

---

### Task 3 — Photo attach for extraction topics (topic_photos, write-time)

Without this, flip-era days lose photos in Timeline/Evidence (`related_photos` drives both) — the report path attaches photos by time correlation (`lambda_report_generator.py:386-402`, photos at `users/{user}/pictures/{date}/` :1070). Mirror it at item-writer time.

**Files:** Modify `src/lambda_item_writer.py`; extend `tests/unit/test_lambda_item_writer.py`.

**Design:**
- New pure helper in `lambda_item_writer.py`: `_photos_for_topics(photo_objects, topics) -> dict[int, list[dict]]` — `photo_objects` = `[{key, filename, hhmm}]` from listing `users/{user_folder}/pictures/{date}/` (derive `hhmm` from the filename's BUG-01-safe time regex `\d{4}-\d{2}-\d{2}_(\d{2})-(\d{2})-(\d{2})` via `transcript_utils`); a photo attaches to topic index `i` when its HH:MM falls inside that topic's `time_range` (parse the en-dash "HH:MM – HH:MM"; topics without a parseable range get none); a photo matches AT MOST one topic (first containing range wins); cap 5/topic (report parity :402).
- Adapter: `write_extraction_items` lists the pictures prefix ONCE per invocation (paginator, outside the per-topic loop; empty/missing prefix → no-op) and passes `photos=[{"s3_key": p["key"], "caption_text": None} for p in matched]` into the EXISTING `upsert_topic(photos=...)` support (`topics.py:38-42`).
- IAM: ItemWriterFunction policy adds `s3:ListBucket` prefix `users/*` on `${IngestBucketName}` (GetObject not needed — we only list). Template change is part of this task.

- [ ] **Step 1 — failing tests (pure first):** `test_photo_matches_inside_time_range_only`, `test_photo_attaches_to_first_matching_topic_only`, `test_cap_five_photos_per_topic`, `test_unparseable_time_range_gets_no_photos`; adapter: `test_item_writer_upserts_topic_photos`, `test_missing_pictures_prefix_is_noop`. FAIL.
- [ ] **Step 2 — implement (helper pure, adapter thin).** Green; full suite green.
- [ ] **Step 3 — PR → MATCH check → merge → deploy → live smoke:** re-trigger an extraction for a (user,date) that HAS pictures → Data-API `SELECT count(*) FROM topic_photos WHERE topic_id IN (SELECT id FROM topics WHERE source_s3_key='<key>')` > 0. If no real day with pictures exists, upload one test photo to `users/<folder>/pictures/<date>/` named with an in-range timestamp first.
- [ ] **Step 4 — commit** `feat(item-writer): time-correlated photo attach to extraction topics (topic_photos)`.

**Done:** flip-era topics carry photos; the shim can render `related_photos` filenames.

---

### Task 4 — org-api compatibility shim: `GET /api/org/timeline`

**Files:** Modify `src/lambda_org_api.py` (dispatch ~:186 + new handlers), `src/template.yaml` (OrgApiFunction env + IAM); extend `tests/unit/test_lambda_org_api.py`.

**Interfaces:**
- Consumes: `topics.has_topics_for_source_prefix`, `topics.list_topics_for_source_prefix` (Task 1), `users.get_by_folder_name`, `sites.list_company_sites`/`memberships.accessible_site_ids` (existing ACL), S3 lake reads.
- Produces: the D1 contract — the exact daily_report.json shape (§0.9) or verbatim documents. Task 5's differ and Task 6's UI depend on it.

**Template:** OrgApiFunction `Environment.Variables` gains `LAKE_BUCKET: !Ref IngestBucketName`; policy gains `s3:GetObject` on `${IngestBucketName}/reports/*` and `s3:ListBucket` on `${IngestBucketName}` with prefix `reports/*` (mirror the ListBucket-for-404-not-403 reasoning documented at the MatcherFunction grant, template.yaml:1152-1159).

**Handler sketch (mirror the module's existing style; `_SEV_TO_RISK` copied from `lambda_extract_session.py:253`):**
```python
# dispatch():
if route == "/timeline" and method == "GET":
    return get_timeline_compat(conn, caller, event)

def get_timeline_compat(conn, caller, event):
    p = event.get("queryStringParameters") or {}
    date, user = p.get("date"), (p.get("user") or "").strip()
    if not date or not REPORT_DATE_RE.match(date):
        return error("date required (YYYY-MM-DD)", 400)
    is_all = resolve_scope(caller["global_role"]) == "ALL"
    if not is_all:
        own = caller.get("folder_name")
        if not own:
            return error("no folder mapping for your account", 403)
        user = own                                  # D10: forced self
    if not user:                                    # admin/gm, no user
        return admin_disambiguation(conn, caller, date)   # D1(iv)
    prefix = f"extractions/{user}/{date}/"
    if topics.has_topics_for_source_prefix(conn, prefix):
        rows = topics.list_topics_for_source_prefix(conn, prefix)
        allowed = _allowed_site_ids(conn, caller)   # existing helper
        rows = [r for r in rows if str(r["site_id"]) in allowed]
        if rows:
            doc = _get_lake_json(f"reports/{date}/{user}/daily_report.json")  # None on miss
            return ok(render_report_shape(rows, doc, date, user))
    doc = _get_lake_json(f"reports/{date}/{user}/daily_report.json")
    if doc is not None:
        return ok(doc)                              # VERBATIM (byte-identical history)
    return ok({"message": f"No report for {user} on {date}", "date": date}, 404)
```
`render_report_shape(rows, doc, date, folder)` (pure function, unit-test target):
```python
def render_report_shape(rows, doc, date, folder):
    doc = doc or {}
    topics_out = []
    for i, t in enumerate(rows):                    # repo already D3-ordered
        flags = [{"observation": f["observation"],
                  "risk_level": _SEV_TO_RISK.get(f["severity"], "medium"),
                  "recommended_action": f["recommended_action"]}
                 for f in t["findings"] if f["domain"] == "safety"]
        if not flags:                               # pre-#46 legacy extractions
            flags = [{"observation": s["observation"], "risk_level": s["risk_level"],
                      "recommended_action": None} for s in t["safety_observations"]]
        topics_out.append({
            "topic_id": i,
            "time_range": t["time_range"],
            "topic_title": t["title"],
            "category": t["category"],
            "participants": t["participants"] or [],
            "summary": t["summary"],
            "key_decisions": [],                    # D3: v1, decisions table deferred
            "action_items": [{"action": a["text"], "responsible": a["responsible"],
                              "deadline": a["deadline_text"] or (str(a["deadline"]) if a["deadline"] else None),
                              "priority": a["priority"]} for a in t["action_items"]],
            "safety_flags": flags,
            "related_photos": [ph["s3_key"].rsplit("/", 1)[-1] for ph in t["photos"]],
            "findings": t["findings"],              # additive passthrough (D3)
        })
    return {
        "report_date": date,
        "site": rows[0]["site_name"],
        "user_name": rows[0]["user_name"] or folder.replace("_", " "),
        "executive_summary": doc.get("executive_summary"),
        "safety_observations": doc.get("safety_observations", []),
        "quality_and_compliance": doc.get("quality_and_compliance", []),
        "critical_dates_and_deadlines": doc.get("critical_dates_and_deadlines", []),
        "_report_metadata": {"source": "live_extraction", "version": "flip-v1"},
        "topics": topics_out,
    }
```
`admin_disambiguation`: try `reports/{date}/summary_report.json` verbatim; else list `reports/{date}/` folders (S3, `_debug` filtered — mirror `lambda_fieldsight_api.py:264-291`) UNIONED with `SELECT DISTINCT u.folder_name FROM topics t JOIN users u ON u.id=t.user_id WHERE t.report_date=%s AND t.source_s3_key LIKE 'extractions/%%'` (batched via a small repo helper or inline cursor — keep it read-only); one candidate → recurse into the single-user path; many → `{"date": date, "available_users": [...]}`. **Before implementing, Read `fieldsight-ui/scripts/pages/timeline.js:176-190` to confirm `_report_metadata` is only read defensively (investigation says yes — it detects the meeting picker); adjust the metadata object only if that read requires a specific key.**

- [ ] **Step 1 — failing tests** (extend the existing org-api test scaffolding — FakeConn + event builder): `test_timeline_shim_serves_s3_verbatim_when_no_extraction_topics` (assert the EXACT dict passthrough), `test_timeline_shim_renders_override_when_extraction_topics_exist`, `test_render_shape_topic_ids_positional_and_ordered`, `test_render_shape_safety_flags_from_findings_with_legacy_fallback`, `test_render_shape_deadline_prefers_deadline_text`, `test_render_shape_merges_doc_prose_fields`, `test_404_body_matches_prod_shape`, `test_non_all_scope_forced_to_own_folder`, `test_admin_no_user_unions_extraction_folders`, `test_site_acl_filters_override_rows`. FAIL.
- [ ] **Step 2 — implement handler + pure renderer + template change.** Green; full suite green.
- [ ] **Step 3 — PR → MATCH check → merge → deploy → live curl smoke (Ben Lin admin idToken):** (a) a historical report day → response identical to prod `/api/timeline` same params; (b) a no-data day → 404 body; (c) TODAY with a fresh extraction (before 05:00) → override shape with `time_range`, `safety_flags`, `related_photos`, `findings`; (d) admin no-user → `available_users`.
- [ ] **Step 4 — commit** `feat(org-api): /timeline compatibility shim — S3 verbatim + Aurora extraction override (authority flip read path)`.

**Done:** the authoritative read path exists, prod-shape-compatible, ACL'd, live-verified.

---

### Task 5 — Parallel-run diff harness (shim vs prod), recorded evidence

**Files:** Create `scripts/compare-timeline.mjs`.

Node (BUG-29: no python on the workstation). Inputs via env: `PROD_BASE` (khfj3p1fkb `/prod/api`), `ORG_BASE` (test gateway `/prod/api`), `IDTOKEN`; args: `--date YYYY-MM-DD --user Folder_Name` repeatable, or `--dates-from <file>`.

```js
// for each (date,user): GET `${PROD_BASE}/timeline?...` and `${ORG_BASE}/org/timeline?...`
// (Authorization: IDTOKEN), stable-stringify both (sorted keys, recursive), diff.
// Exit 0 only if every non-extraction day is BYTE-IDENTICAL; extraction-override
// days are reported (not failed) with a field-presence summary:
// topics count, per-topic {time_range, n_action_items, n_safety_flags, n_photos}.
```
Implementation notes: use global `fetch` (node ≥18); stable stringify = recursive key-sort then `JSON.stringify`; treat prod 404 + shim 404 with equal bodies as identical; print a one-line verdict per pair.

- [ ] **Step 1 — write the script** (this task is tooling; its "test" is the recorded run).
- [ ] **Step 2 — run over ≥10 historical (date,user) pairs** spanning: normal report days, a meeting-converted day, an identity-bridge-miss day (report exists in S3, never ingested), a no-data day. Expected: ALL identical.
- [ ] **Step 3 — run against TODAY (before 05:00)** with a fresh extraction → capture the override-shape summary; eyeball topics/time ranges/flags against the extraction JSON.
- [ ] **Step 4 — paste both outputs into the PR description; commit** `chore(tools): compare-timeline parallel-run differ (authority flip gate)`.

**Done:** mechanical proof the shim cannot regress history; a recorded preview of the flip-day shape.

---

### Task 6 — UI: `timelineSource` flag, routing, fallback, live-merge gate (deploy flag OFF)

**Repo:** `C:/Users/camil/Dropbox/fieldsight-ui`, branch off `dev`. No behavior change until the Amplify env var flips (Task 8).

**Files:** Modify `scripts/api/index.js` (:83-86 seam), `scripts/api/timeline.js` (:31-36 fetch + :70-76 cache key), `scripts/api/compliance-aggregator.js` (live-merge legs ~:668 and ~:813), `amplify.yml` (env.js printf), `env.example.js`, `app-shell-preview.html` (bootstrap ~:272 + cache busters).

1. **index.js** — after the `orgBaseUrl` line:
```js
    /* authority flip (pipeline plan 2026-07-14): 'report' = prod /api/timeline
       (S3 daily_report.json), 'aurora' = org /api/org/timeline (item store).
       'aurora' only takes effect when orgBaseUrl is non-empty (kill switch). */
    timelineSource: env.timelineSource || 'report',
```
2. **timeline.js** — replace the live branch of `fetchTimeline`:
```js
  function timelineSource() {
    var api = window.FS.api;
    return (api.timelineSource === 'aurora' && api.orgBaseUrl) ? 'aurora' : 'report';
  }

  async function fetchTimeline(opts) {
    if (!window.FS.api.useMocks) {
      var params = { date: opts.date, user: opts.user };
      if (timelineSource() === 'aurora') {
        try {
          var r = await window.FS.api.orgRequest('/timeline', { params: params });
          /* _accessDenied → ACL divergence (shim v1 is stricter than prod for
             site_manager/pm, plan D10): fall through to the report path rather
             than blanking the page. _notFound is authoritative (the shim
             already fell back to S3 server-side) — return it. */
          if (r && !r._accessDenied) return r;
        } catch (e) { /* org gateway/transport failure → report fallback */ }
      }
      return window.FS.api.request('/timeline', { params: params });
    }
    ...unchanged mock branch...
```
   and make the cache key source-aware (`getTimeline`): `var key = 'tl:' + timelineSource() + ':' + opts.date + ':' + (opts.user || '');`
3. **compliance-aggregator.js** — at both live-merge legs, short-circuit when the flag is on (the shim already serves the same extraction topics through `getTimeline` — merging live-items again double-displays every safety/quality finding, investigation §0.11): guard the `org.getLiveItems` fetch with `if ((window.FS.api.timelineSource === 'aurora') && window.FS.api.orgBaseUrl) return <empty result of that leg>;` — exact anchor lines confirmed at implementation (search for the two `feat 4b — live-items merge` comments).
4. **amplify.yml** — extend the printf pair (both lines): add `timelineSource: "%s"` fed by `"${FS_TIMELINE_SOURCE:-report}"`. **env.example.js** mirrors it.
5. **app-shell-preview.html** bootstrap (next to the existing `?baseUrl` escape): `var ts = params.get('timelinesrc'); if (ts && window.FS && window.FS.api) window.FS.api.timelineSource = ts;` Bump `?v=N` on `index.js`, `timeline.js`, `compliance-aggregator.js`.

- [ ] **Step 1 — pre-checks (grep):** `grep -n "request('/timeline'" scripts/api/timeline.js` (exactly 1), `grep -n "live-items merge" scripts/api/compliance-aggregator.js` (exactly 2), `grep -n "orgBaseUrl: env" scripts/api/index.js` (exactly 1).
- [ ] **Step 2 — implement patches 1-5.**
- [ ] **Step 3 — verify:** `node --check` on the three JS files; open `app-shell-preview.html?dev=1&mocks=0&baseUrl=<prod>&orgbaseurl=<org>&timelinesrc=aurora` in a browser (or state deferred-to-user per repo convention): Timeline for a historical day renders identically; `?timelinesrc=report` identical; with the org gateway URL deliberately broken, `aurora` mode still renders via fallback.
- [ ] **Step 4 — PR (fieldsight-ui, user merges to `dev`) → Amplify auto-build with FS_TIMELINE_SOURCE unset → default 'report' → zero behavior change. Commit** `feat(api): timelineSource flag — org-shim routing for getTimeline + live-merge gate (default report)`.

**Done:** flip is one env var away; kill switches proven.

---

### Task 7 — Backend: `AUTHORITY_FLIP` defer in lambda_ingest (deploy flag OFF)

**Files:** Modify `src/lambda_ingest.py`, `src/template.yaml` (new `AuthorityFlip` parameter, default `'false'`, → `AUTHORITY_FLIP` env on IngestFunction), `src/lambda_item_writer.py` (I-4 comment only); extend `tests/unit/test_lambda_ingest.py`.

**Changes (`ingest_report`, around :286-323):**
```python
AUTHORITY_FLIP = os.environ.get("AUTHORITY_FLIP", "false").lower() == "true"  # module top

        extraction_prefix = f"extractions/{user_folder}/{date}/"
        defer_to_extraction = AUTHORITY_FLIP and topics.has_topics_for_source_prefix(
            conn, extraction_prefix)

        chunks.delete_chunks_for_source(conn, report_key)
        topics.delete_topics_for_source(conn, report_key)   # always: clears stale pre-flip report rows
        if defer_to_extraction:
            # Authority flip (spec §6): the day's extraction topics ARE the item
            # store; the report is a document artifact only. No extraction wipe,
            # no report topics, no match_request. Chunks still written below
            # (RAG) with topic_id=None.
            logger.info("%s: authority flip — deferring to extraction topics under %s",
                        report_key, extraction_prefix)
        else:
            topics.delete_topics_for_source_prefix(conn, extraction_prefix)

        topic_seq_to_id = {}
        collected_topics = []
        if not defer_to_extraction:
            for t in report.get("topics", []):
                ...existing loop verbatim...
```
Chunk loops and the `if collected_topics: match_request.emit(...)` tail are UNTOUCHED (empty `collected_topics` on defer → no emit; `topic_seq_to_id.get(...)` → `None` chunk links, tolerated per §0.3). `item-writer`: update the I-4 comment block (:106-110) to state the post-flip semantics ("report topics only exist for zero-extraction fallback days; this guard keeps that rare day duplicate-free") — no code change.

**Template:** `AuthorityFlip: {Type: String, Default: 'false', AllowedValues: ['true','false']}`; IngestFunction env `AUTHORITY_FLIP: !Ref AuthorityFlip`. deploy.yml is NOT changed in this task (the parameter defaults false; Task 9 sets it).

- [ ] **Step 1 — failing tests** (extend the existing ingest test scaffolding): `test_flip_off_behavior_unchanged` (delete_prefix called, topics written, emit called), `test_flip_on_with_extractions_defers` (no prefix delete, no report topics, no emit, chunks written with topic_id None, stale report-key topics still deleted), `test_flip_on_without_extractions_falls_back_to_legacy`, `test_flip_env_parsing_defaults_false`. FAIL.
- [ ] **Step 2 — implement.** Green; full suite green.
- [ ] **Step 3 — PR → MATCH check → merge → deploy (flag still 'false') → post-deploy sanity:** re-trigger one historical report ingest (`aws lambda invoke --function-name fieldsight-test-ingest --payload '{"date":"<D>","user":"<U>"}' ...`) → identical row counts to before (Data-API compare) — proves OFF is a true no-op.
- [ ] **Step 4 — commit** `feat(ingest): AUTHORITY_FLIP — defer nightly report topics when extraction topics exist (spec §6, default off)`.

**Done:** the overwrite stop is deployed, inert, and reversible by parameter.

---

### Task 8 — Enable the UI flag on Amplify dev (soak ≥ 2 days)

No code. Ordering per D7: UI first.

- [ ] **Step 1 —** `aws amplify update-branch --app-id d2fssznicvuckr --branch-name dev --environment-variables ...existing vars..,FS_TIMELINE_SOURCE=aurora` (fetch + re-supply the FULL existing var map — update-branch replaces it), trigger a build, then `curl -s https://dev.d2fssznicvuckr.amplifyapp.com/env.js` → `timelineSource: "aurora"`.
- [ ] **Step 2 — browser E2E (Ben Lin admin), same-day (before 05:00):** Today shows the live day's urgent/activity from extraction topics; Timeline for today renders topics with time ranges + transcripts deep-link works; Safety/Quality show each live finding ONCE (live-merge gate proven); Evidence photos present on photo-bearing days.
- [ ] **Step 3 — historical spot-checks:** 3 report days + 1 no-data day + admin no-user disambiguation — identical to pre-flip screenshots/behavior.
- [ ] **Step 4 — rollback drill (do it for real, once):** append `?timelinesrc=report` → old path; then set it back. Record in the PR/issue thread.
- [ ] **Step 5 — soak:** ≥2 calendar days with the nightly overwrite STILL RUNNING (safe preview state): each morning confirm yesterday renders as the report version (shim verbatim), today as live extraction. Watch `aws logs tail /aws/lambda/fieldsight-test-org-api --since 1h` for shim errors.

**Done:** the read path is migrated in production-dev, with the write world unchanged underneath it.

---

### Task 9 — Enable AUTHORITY_FLIP (the actual flip) + nightly verification (soak ≥ 3 nights)

- [ ] **Step 1 — pre-flight:** MATCH check; Aurora snapshot (`aws rds create-db-cluster-snapshot ... fieldsight-pre-authority-flip-<date>`); git tag `backup-pre-authority-flip-<date>` on develop.
- [ ] **Step 2 — set the parameter:** one-line deploy.yml change (`"AuthorityFlip=true"` in the parameter-overrides block) via PR → merge → CI deploy. (Parameter-in-CI, not console, so the setting survives future deploys — the same reason deploy.yml pins every other env param.)
- [ ] **Step 3 — first-night verification (the acceptance test of this whole plan), morning after:**
  - Data-API: yesterday's extraction topics STILL EXIST (`SELECT count(*) FROM topics WHERE source_s3_key LIKE 'extractions/<user>/<yesterday>/%'` > 0) **with findings + impact links intact** (`SELECT count(*) FROM findings f JOIN topics t ON t.id=f.topic_id WHERE t.report_date='<yesterday>' AND f.programme_task_id IS NOT NULL` — the spec §6 breadcrumb's "findings become persistent automatically" made true).
  - No report topics for that (user,date): `SELECT count(*) FROM topics WHERE source_s3_key='reports/<yesterday>/<user>/daily_report.json'` = 0.
  - The report DOCUMENT exists in S3 (prod generator untouched): `aws s3 ls s3://fieldsight-data-509194952652/reports/<yesterday>/<user>/daily_report.json`.
  - UI: yesterday now renders the EXTRACTION version (override) with exec-summary prose from the document (D4); Safety/Quality single-listed; rollup counts sane (bridge rows, §0.12).
  - RAG: `/ask` over yesterday still answers with citations (chunks with `topic_id=None` tolerated, §0.3).
- [ ] **Step 4 — late-extraction path check (within the soak):** find/force a Fargate catch-up day (or re-trigger an old extraction for a PRE-flip day) → I-4 skips it (report topics exist for that day) — CloudWatch `fieldsight-test-item-writer` shows the I-4 log line; no duplicate day appears.
- [ ] **Step 5 — soak ≥3 nights**, then declare the flip landed in the tracking issue.
- [ ] **Step 6 — commit** (`chore(ci): enable AuthorityFlip on TEST`) is Step 2's PR; this step is verification-only.

**Rollback (if any check fails):** flip `AuthorityFlip=false` (PR or emergency console env edit on `fieldsight-test-ingest`) → re-supersede affected days: `aws lambda invoke --function-name fieldsight-test-ingest --payload '{"date":"<D>","user":"<U>"}'` per pair (legacy path re-wipes extractions + rewrites report topics) → optionally `FS_TIMELINE_SOURCE=report`. Store restored to pre-flip semantics; snapshot exists for catastrophe.

---

### Task 10 — Docs, breadcrumbs, and the follow-up ledger

**Files:** Modify `CLAUDE.md` (pipeline, the "Aurora item store" section), `docs/superpowers/specs/2026-07-13-unified-extraction-labeling-design.md` (§6 status note), auto-memory.

- [ ] **Step 1 — CLAUDE.md:** rewrite the "同日有效 (same-day-only)" bullet to "authority flip live (2026-07-XX): nightly ingest defers to extraction topics (AUTHORITY_FLIP); findings/impacts persistent; Today/Timeline read org-api /timeline shim (FS_TIMELINE_SOURCE)"; document both kill switches and the re-supersede rollback command.
- [ ] **Step 2 — spec §6:** add a dated "SHIPPED as plan 2026-07-14-authority-flip; option (b) chosen; report document retained" note.
- [ ] **Step 3 — follow-up ledger (record, do NOT implement here):**
  1. Retire the D8 safety double-write — REPOINT `repositories/rollup.py` at `findings` FIRST (spec §6 breadcrumb 2).
  2. Correction loop (spec §8 Task 5): `source='human'` enforcement — add `AND source != 'ai'`-style guards to `delete_topics_for_source*` + `ai_original`/audit columns on children; the 0011 `source` column is already waiting.
  3. Aurora-backed `/dates` (D9) so TODAY dots the calendar and joins the rolling list same-day.
  4. Shim ACL relaxation for site_manager/pm folder access (D10).
  5. `decisions`/`questions` tables + `key_decisions` rendering (D3 gap); report-path findings (spec §6 breadcrumb 3).
  6. #30 prod-isolation note: the flip adds `timelineSource`+`/api/org/timeline` to the list of surfaces bound to the TEST gateway that isolation must re-home.
  7. Extraction re-runs renumber positional topic_ids → a checked action's audit key can orphan (same class as report regeneration today). Durable item identity belongs to the correction-loop phase.
- [ ] **Step 4 — memory update** (fieldsight-current-progress / rag-live cross-refs: same-day-only caveat is DEAD after Task 9).
- [ ] **Step 5 — commit** `docs: authority flip shipped — item-store authority, read-path shim, rollback drill, follow-up ledger`.

---

## Risks (ranked)

1. **Read-path migration onto the dual-lambda seam (the headline risk).** Today/Timeline — the product's face — move from a hand-built-but-boring prod lambda to the TEST stack (org gateway, in-VPC Aurora, rotation trap, every develop merge redeploys it). Mitigations: verbatim-S3 default (history is byte-identical, Task 5 proves it mechanically), two kill switches (query param / env var), client transport fallback, UI-first enablement (Task 8 is fully reversible while the write world is untouched), MATCH gate on every merge, rotation disabled. Residual: a TEST-stack outage degrades dev-site timeline to the fallback path (report-only) — acceptable; prod CloudFront frontend is untouched (its `orgBaseUrl` is empty → flag inert by construction).
2. **Shape-parity gaps blanking or degrading render** (time_range ordering/deep-links, photos in Evidence, exec-summary prose, free-text deadlines, `key_decisions`). Mitigations: Tasks 1-3 close the write-time gaps BEFORE any read moves; D4 sources prose from the still-produced document; Task 5's field-presence report + Task 8's E2E checklist gate enablement; `key_decisions: []` is a rendered-as-empty, not a crash.
3. **Duplicate/blank days from gate logic** (ingest defer × I-4 orderings). Mitigations: defer is day-level and flag-gated; I-4 untouched (emergent inversion — both arrival orders unit-tested in Task 7); stale-report-row delete kept on the defer branch; zero-extraction days fall back to the report path wholesale; Safety/Quality double-display prevented by the Task 6 live-merge gate + D7 ordering.
4. **RAG/chunk regression from `topic_id=None` report chunks post-flip.** Investigation says nullable/tolerated (§0.3); Task 9 Step 3 verifies `/ask` live the first morning. Rollback restores the link if wrong.
5. **Rotation trap** — three tasks deploy in-VPC lambdas; the MATCH check before every merge is non-negotiable.
6. **Intraday churn on the live day** — sessions land incrementally; topics/ids reshuffle as extraction re-runs (positional ids, re-extraction delete+reinsert). Same-day check-offs can orphan (follow-up ledger 7). Accepted for v1: the live day was previously EMPTY, so any live content is strictly additive.
7. **Meeting-converted days** — post-flip a meeting day renders its extraction topics instead of `convert_to_daily_report_format`'s projection; the meeting_minutes.json view in Timeline is unchanged. This is spec-intended (D2/§8-7 retire the projection later) but is a visible content change — include one meeting day in Task 5/8 checks.

## Self-review (author, 2026-07-14)

- **Spec §6 coverage:** stop the delete → Task 7 (D5); item-writer gate inversion → emergent, verified Task 7/9 (§0.4); decouple report document → prod generator untouched + D4 prose merge; migrate Today/Timeline → Tasks 4/6/8 via option (b) with (a) noted as end-state; human-edit protection → D8 minimum + ledger; breadcrumb items 1-3 → Task 9 Step 3 (persistence), ledger 1 (bridge), ledger 5 (report-path findings).
- **Prompt-mandated resolutions:** shim-vs-endpoint decided WITH the dual-lambda reality (§1); exact reads moved tabulated; frontend-routing (not prod-backend) chosen and justified; staged rollout flag-gated at two layers with a rehearsed rollback (Task 8 Step 4, Task 9 rollback block); dual-lambda/dual-bucket judged NOT a hard blocker with the honest residual in Risk 1; #30 recorded, not prerequisite.
- **Type/name consistency:** `has_topics_for_source_prefix`/`list_topics_for_source_prefix`/`_escape_like` (Task 1) consumed by Tasks 4/7 under identical names; `deadline_text`/`time_range`/`participants` spelled identically in DDL, writers, renderer; `timelineSource` string values `'report'|'aurora'` identical across index.js/timeline.js/compliance-aggregator/amplify.yml; `AUTHORITY_FLIP` env ↔ `AuthorityFlip` param mapped once in Task 7.
- **Placeholder scan:** every code-bearing step carries the code or the exact anchor + verbatim-mirror instruction (two UI anchors deferred to grep pre-checks because the CRLF repo mandates fresh single-line anchors at edit time — the pre-check makes them deterministic).
- **Honest limits:** Task 0 exists because live AWS verification expired mid-planning; if its Step 1 or 3 fails, the plan halts by design. Same-day items reach the rolling Today list only next-morning in v1 (D9) — a scoped, recorded gap, not a silent one.
