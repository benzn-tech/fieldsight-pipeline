# FieldSight — Status + Roadmap (session handoff, 2026-07-15)

Self-contained handoff for a fresh session. Two repos, one AWS account (509194952652 / ap-southeast-2 — the USER's own SAM pipeline, NOT the company CDK prod 164088480050; never conflate).

- **fieldsight-pipeline** (`C:/Users/camil/Dropbox/fieldsight-pipeline`) — Python lambdas + SAM.
- **fieldsight-ui** (`C:/Users/camil/Dropbox/fieldsight-ui`) — no-build browser-React (React.createElement, tokens.css + JS mirror fs-globals.js, `?v=N` cache-busters, register composites in components-preview.html, `node --check`, NO test runner). Both under Dropbox — the repo dir is occasionally transiently unavailable to `git -C` (Dropbox online-only); retry.

**AWS access:** `--profile fieldsight-deployer` = permanent IAM user keys (never expire). The `[default]` profile is an ephemeral "aws login" mechanism that expires — do NOT rely on it. To make deployer the shell default: `setx AWS_PROFILE fieldsight-deployer`.

---

## PART 1 — WHAT SHIPPED THIS SESSION (all live)

### A. Prod isolation (pipeline) — COMPLETE + LIVE
Independent customer-facing environment, isolated from the dev/test environment.
- **`fieldsight-prod` SAM stack** deployed from `main` (20 lambdas + API gateway + own Cognito). Deploy: `.github/workflows/deploy-prod.yml` on push to `main`, gated by the `production` GitHub environment (required reviewer = benzn-tech). PROD API endpoint: **`https://ys94qy2tk0.execute-api.ap-southeast-2.amazonaws.com/prod/api`**.
- **Shared** Aurora (`fieldsight-db-test`) + RAG + S3 lake (`fieldsight-data-509194952652`) + Cognito pool `ap-southeast-2_q88pd6XXr` (fieldsight-users). Tenancy is ROW-LEVEL by company (`users.company_id`), not by pool/stack. `MultiTenantResolution=true` on prod ingest/item-writer (routes lake objects to owning company by globally-unique `folder_name`, 0012); `false` on test (pinned to internal FieldSight company).
- **Lake S3 events FLIPPED** from `fieldsight-test-*` to `fieldsight-prod-*` (all 8: vad/transcribe/extract/ingest/embed/write/match). Backup: `lake-notif-preflip.json` (job tmp). Rollback = re-PUT it + `PROD_WIRE_LAKE=false`. Repo vars set: `PROD_WIRE_LAKE=true`, `PROD_RETIRE_IDS=vad-on-users,transcribe-on-segments`, `PROD_VAD_LAYER_ARN`, `PROD_DOCX_LAYER_ARN`, `PROD_ENABLE_SCHEDULES=false`.
- **test stack severed from the lake** (`IngestBucketName=test bucket`, PR #57) → develop iteration can't touch the lake/prod.
- **Customer UI LIVE**: `https://main.d2fssznicvuckr.amplifyapp.com/` (Amplify `main` branch → prod gateway ys94qy2tk0; USEMOCKS/WRITEMOCKS=false, ORGWRITES=true). Dev site `https://dev.d2fssznicvuckr.amplifyapp.com/` unchanged (legacy khfj3p1fkb + test wdsgobb7b0).
- Traps hit + fixed: Cognito domain replacement (StageConfig.Domain map: test=fieldsight keeps existing, prod=fieldsight-prod); no `production` gate + main diverged 217/3 (created gate + `-s ours` aligned main); StorageBucketPolicy CFN can't adopt the lake's existing policy (`ManageDataBucketPolicy=false` → prod skips it).
- **Vestigial**: `fieldsight-prod-users` Cognito pool (`ap-southeast-2_jtbFNn5Zi`) — SAM auto-created, UNUSED (UI hardcodes q88pd6XXr). Cleanup deferred (template change to not create it).
- Detailed plan: `docs/superpowers/plans/2026-07-14-prod-isolation.md`.

### B. UI Phase 1 (fieldsight-ui) — MERGED to dev + live on dev site
PR #65 merged to `dev` (Amplify dev built, live at dev.d2fssznicvuckr.amplifyapp.com; hard-refresh Ctrl+Shift+R). 7 tasks, frontend-only:
- T3 timeline: selected topic = accent left-border + neutral tint (readable), yellow only on hover; reduced-motion flash legible.
- T5: safety/quality "+ Raise Observation"/"+ Log Item" buttons — root cause was `ModalOverlay` called WITHOUT `open:true` → invisible modal; fixed (also fixed identical bug in template-upload-modal.js × 4 → /library upload).
- T2 Today/Timeline buttons equal width.
- T7+G2 resolved sink-to-bottom + gray/strikethrough (safety/quality/timeline middle+right; Today/Leftover intentionally DROP resolved).
- T6 safety resolve shows "Resolved by X · time" (from toggleAction API response only — never local user; persisted into state.rows; cleared on reopen).
- T1 Leftover batch-select (removed square checkbox; round selector doubles as multi-select in "Batch Select" mode; Shift range / Ctrl toggle).
- T4 extracted `useMultiSelect` (scripts/composites/multi-select-list.js) reused by Leftover + safety/quality batch Mark-Resolved.
- Spec: `fieldsight-ui/docs/superpowers/specs/2026-07-14-ui-phase1-batch-audit-sorting-design.md`; plan: `.../plans/2026-07-14-ui-phase1-batch-audit-sorting.md`.
- Binding UI guardrails: resolve/check operator ONLY from the API response (`checked_by`/`checked_at`), never a local AuthMock/session read; action-item sort must preserve ORIGINAL index for `actionIndex`/`lookupAction` keys (never shift by sorted render position).

---

## PART 2 — HOW TO OPERATE (deploy flows, key IDs)

| What | Flow |
|---|---|
| pipeline develop → | `deploy.yml` → SAM `fieldsight-test` (test bucket, internal-pinned) |
| pipeline main → | `deploy-prod.yml` → SAM `fieldsight-prod` (lake owner, multi-tenant), `production` gate |
| ui dev → | Amplify `dev` branch → dev.d2...amplifyapp.com |
| ui main → | Amplify `main` branch → main.d2...amplifyapp.com (customer) |
| PROMOTE code to customers | pipeline develop→main PR (+ approve production gate); ui dev→main PR (Amplify main auto-builds) |

Key IDs: prod API `ys94qy2tk0`; dev/customer Amplify app `d2fssznicvuckr`; Cognito pool `ap-southeast-2_q88pd6XXr` (client `4ratjdjonqm17tln6bs2761ci3`); DB cluster `fieldsight-db-test-dbcluster-hywiixu8ihi9`; lake `fieldsight-data-509194952652`; test bucket `fieldsight-data-test-509194952652`; legacy prod api gateway `khfj3p1fkb`; test org api `wdsgobb7b0`.

Aurora Data API: `aws rds-data execute-statement --resource-arn <cluster ARN> --secret-arn <fieldsight-db-test SecretArn export> --database fieldsight --sql "..."` (deployer profile has access). Internal company = `FieldSight` (dc2eafa9-...). Company sites include Southbase projects (SB1108 Ellesmere College, SB1131 Northbrook Wanaka). NO separate customer tenant provisioned (user chose to test prod with existing accounts; a "Southbase Construction" company was created then deleted). IRON RULE: customers get NO rows in DynamoDB `fieldsight-users` / `config/user_mapping.json` (legacy single-company surface grants admin/gm a cross-tenant bypass) — their identity lives ONLY in Aurora `users` + Cognito.

---

## PART 3 — IMPLEMENTATION PLAN (planned + unplanned)

### IMMEDIATE (in flight) — Create-modal enhancement (fieldsight-ui)
User request (2026-07-15) after verifying Phase 1: make the Safety "Raise Observation" (`safety-create-modal.js`) and Quality "Log Item" (`quality-create-modal.js`) modals **consistent** (they differ too much) with the same basic fields, and switch the Chinese-looking date-calendar + photo-upload to English. **CFT clarification still OPEN**: user named the areas "CFT 和 Quality" — interpret CFT = the Safety-side create (Raise Observation) unless the user says otherwise (confirm at start).

**Explored 2026-07-15 — key findings (they change the design):**
- **The "Chinese" is NOT app strings.** A repo-wide CJK grep found only 3 hits, all developer comments (never rendered). The Chinese the user sees comes from NATIVE HTML controls rendered in the browser's Chinese locale:
  - Quality's "Deadline" is a native `<input type="date">` (quality-create-modal.js:329-339) → the calendar chrome is the OS/browser date picker (Chinese). **FIX = replace it with the app's own `DatePicker` composite** (`scripts/composites/date-picker.js`, all hardcoded English — DAY_NAMES/MONTH_NAMES arrays; NOT used by either modal today).
  - Safety's Photos is a native `<input type="file" multiple>` (safety-create-modal.js:374-390) → the "Choose File / No file chosen" text is browser Chinese. **FIX = hide the native input, drive it via a custom English "Choose photos" button** (the modal already shows an English "N photo(s) selected" count).
- **Current field asymmetry** (neither has Title or Assignee): Safety = Observation(req textarea), Risk level(req select low/med/high), Recommended action(textarea), Location(text), Photos(file, non-functional). Quality = Observation(req textarea), Category(req select quality/compliance/workmanship), Follow-up required(checkbox), Deadline(date), Location(text). Neither uses the shared form primitives.
- **Target unified field set** (user): Title, Description, Assignee, Date, Priority, Photos — applied consistently to BOTH, reusing `window.FieldSight.Input`/`.Textarea`/`.Select` (scripts/components/input.js — a full label/required/hint/error shell that BOTH modals currently hand-roll; safety imports `Input` but never uses it, quality doesn't import it). Map: Title→Input, Description→Textarea, Assignee→Select/Input, Date→DatePicker, Priority→Select (unify Safety risk_level + a new Quality priority), Photos→custom button.
- **Backend gaps** (`scripts/api/org.js createObservation` is a thin pass-through; `observations` table = 0006): payload carries only `kind, site_slug, observation, risk_level, recommended_action` (safety) / `kind, site_slug, observation` (quality). NO `date`/`assignee`/`priority`/`photo_keys` field exists; safety's photo path calls `FS.api.media.presignedPut` which **does not exist** (dead code — real photo upload needs a backend presign endpoint). So: adding date/assignee/priority as UI fields is fine (send them; backend currently ignores extras) — but real persistence + photo upload is a BACKEND follow-up (Phase 2). Decide with the user whether v1 is UI-consistency-only or includes backend field wiring.
- Approach: brainstorm the shared modal design (one shared layout/field-set component both modals compose, reusing the primitives), spec, then subagent-driven. Do NOT re-explore — this section IS the map.

### PLANNED (roadmap, roughly in order)
- **UI Phase 2 — audit backend** (deferred from Phase 1): a status-change HISTORY table + `GET .../history` read endpoint (T6 "full history incl Reopen"); manual-observation operator stamping (`org.updateObservation` → closed_by/at — needs a column); Aurora `action_items`/`safety_observations` resolve endpoints (NONE exist today) — DEPENDS on #27 (live items are cascade-deleted nightly at 05:00 NZDT until authority-flip). The only working audit today is the DynamoDB action-checkbox (`toggle_action`, writes an immutable `AUDIT#{date}` log that is NEVER read back — a read endpoint is the cheap first win).
- **#27 authority-flip** (`docs/superpowers/plans/2026-07-14-authority-flip.md`, parked): Today/Timeline read Aurora (org) instead of the legacy `fieldsight-api`. Fixes the "dev timeline uses old legacy code / prod uses new prod-api" inconsistency, makes live items persistent (no nightly wipe), and unblocks Phase 2 audit. NOTE its Task 0 expectations invert post-prod-isolation (lake chain now → fieldsight-prod-*).
- **#28 correction loop** (Phase 1/2) — unified-extraction follow-up.
- **#29 dashboard refactor** (rolling Today / rich labels).
- **Prod-isolation leftovers** (all optional, non-blocking): Task 10 schedules cutover (legacy sitesync crons → prod schedules; legacy crons still feed the lake today = dogfooding, works); Task 12 customer onboarding runbook + a real customer tenant (Southbase demo deferred by user); Task 13 one-time programme copy (test bucket → lake); Task 14 retire deploy-prod-code.yml after soak; cleanup the vestigial fieldsight-prod-users pool (template change).

### UNPLANNED / follow-ups (from Phase 1 final review — non-blocking)
- Browser-verify Phase 1 per the manual checklist (in the spec/plan) — the implementer subagents had no browser.
- Fable-flagged Minors left unfixed: T5 try/catch comment overstates coverage (only catches descriptor construction, not child-render throws — would need an error boundary); `useMultiSelect.setBatchMode` runs setState side effects inside an updater (works, not pure). Cosmetic.
- The template-upload-modal fix, timeline right-detail sink, safety multi-select gate, components-preview versioning — DONE in the Phase 1 follow-up commit.

---

## Pointers
- prod-isolation plan: `docs/superpowers/plans/2026-07-14-prod-isolation.md`
- authority-flip (parked): `docs/superpowers/plans/2026-07-14-authority-flip.md`
- UI Phase 1 spec+plan: `fieldsight-ui/docs/superpowers/{specs,plans}/2026-07-14-ui-phase1-batch-audit-sorting*.md`
- Auto-memory (loaded every session): `~/.claude/projects/C--Users-camil-Dropbox/memory/fieldsight-current-progress.md` (+ MEMORY.md index).
