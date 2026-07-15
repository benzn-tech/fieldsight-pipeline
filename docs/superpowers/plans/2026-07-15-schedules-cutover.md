# Schedules Cutover (Timeliness Phase A) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move recording downloads + report generation from the legacy hand-managed `sitesync` crons (recordings enter the lake ONCE daily at 19:00 NZ) to the `fieldsight-prod` stack's schedules, whose 15-min working-hours sweep gets recordings into the lake — and through the extraction chain into Aurora — intraday.

**Architecture:** The prod SAM stack already contains 6 schedule events (orchestrator nightly + 15-min sweep, fargate trigger, daily/weekly/monthly report), all deployed DISABLED behind one `EnableSchedules` parameter fed by the repo variable `PROD_ENABLE_SCHEDULES` (`.github/workflows/deploy-prod.yml:85`). Cutover = flip the variable, redeploy via workflow_dispatch (production-gate approved), then disable the 5 legacy `sitesync` scheduler entries the same afternoon. Concurrent-run safety during the transition window comes from the orchestrator's S3 conditional-put claim locks (`lambda_orchestrator.py:505-566`, `download_claims/` prefix on the shared lake).

**Why this is Phase A of the timeliness ask (2026-07-15):** the user wants each site inspection's TO-DO list + classification visible on the customer web page promptly. Bottleneck ① is upstream (this plan): recordings only enter the lake at 19:00. Bottleneck ② is the read path (Phase B): `docs/superpowers/plans/2026-07-14-authority-flip.md` — execute it AFTER this plan; see its RETARGET 2026-07-15 header.

**Tech Stack:** AWS EventBridge Scheduler (legacy `sitesync` group) + SAM schedule events, GitHub Actions (`deploy-prod.yml`), AWS CLI via Git Bash.

## Global Constraints

- Account **509194952652** / ap-southeast-2. `export AWS_PROFILE=fieldsight-deployer` and `export MSYS_NO_PATHCONV=1` in every shell.
- **Never touch** the legacy lambdas' code/config (`fieldsight-orchestrator`, `fieldsight-report-generator`, `fieldsight-fargate-trigger`) — this plan only flips SCHEDULER STATE. The legacy schedule definitions are kept (disabled) as the rollback path.
- Prod stack deploys ONLY via `deploy-prod.yml` (workflow_dispatch or main push) with the `production` gate — never local `sam deploy`.
- All shell JSON handling via `node` (BUG-29: no python on this workstation). `aws lambda invoke` output files: use a relative filename inside the scratchpad, not `/tmp`.
- `$SCRATCHPAD` below = your session's scratchpad directory; set it once per shell: `export SCRATCHPAD=<scratchpad path>`.
- Timezone facts (July = NZ winter, NZST = UTC+12): legacy `daily-download` cron(0 19 * * ? *) Pacific/Auckland = **07:00 UTC**, the SAME minute as prod `NightlyEvent` cron(0 7 * * ? *) — the cutover MUST complete before 19:00 NZ so only one of them fires that night. Sweep window cron(0/15 17-23,0-7 UTC) = 05:00–19:59 NZST.
- Docs-only commits go to `develop` (deploy workflows path-ignore `docs/**`); this plan has no code changes.

## Legacy ↔ prod schedule map (verified live 2026-07-15)

| Legacy `sitesync` entry (Pacific/Auckland) | Target | Prod stack event (UTC) | Prod fires (NZST) |
|---|---|---|---|
| `daily-download` cron(0 19 * * ? *) | fieldsight-orchestrator | `NightlyEvent` cron(0 7 * * ? *) | 19:00 |
| — (new capability) | — | `SweepEvent` cron(0/15 17-23,0-7 * * ? *) | every 15 min, 05:00–19:59 |
| `fargate-trigger` cron(30 20 * * ? *) | fieldsight-fargate-trigger | `FargateSchedule` cron(30 7 * * ? *) | 19:30 |
| `daily-report` cron(30 2 * * ? *) | fieldsight-report-generator `{"report_type":"daily"}` | `DailyReportSchedule` cron(0 16 * * ? *) | 04:00 |
| `weekly-report` | fieldsight-report-generator | `WeeklyReportSchedule` cron(0 5 ? * FRI *) | Fri 17:00 |
| `monthly-report` | fieldsight-report-generator | `MonthlyReportSchedule` cron(0 17 L * ? *) | last day 05:00 |

Accepted timing shifts: daily report moves 02:30→04:00 NZST (report ingest — and the extraction wipe, until Phase B Task 9 — then runs ~04:05; the "before 05:00" verification-window rule in Phase B still holds). Weekly/monthly shift similarly; no consumer depends on the exact hour.

---

### Task 1: Read-only parity verification (STOP gate)

**Files:** none (read-only AWS). Record all output in the task log / PR description.

**Interfaces:**
- Consumes: live AWS (deployer profile).
- Produces: go/no-go decision + `backup/` JSONs of all 5 legacy schedules used by Task 3 (disable) and the rollback drill.

- [ ] **Step 1: Confirm the 6 prod schedule rules exist DISABLED (wiring sanity)**

```bash
export MSYS_NO_PATHCONV=1 AWS_PROFILE=fieldsight-deployer
aws events list-rules --region ap-southeast-2 \
  --query "Rules[?starts_with(Name,'fieldsight-prod-')].{N:Name,S:ScheduleExpression,St:State}" --output table
```
Expected: 6 schedule rules (Orchestrator Nightly + Sweep, FargateTrigger, ReportGenerator Daily/Weekly/Monthly) all `DISABLED`, plus `TranscribeCallback…` `ENABLED` (event-pattern rule, not schedule — leave it). **STOP if any of the 6 is missing.**

- [ ] **Step 2: Env parity — legacy vs prod lambdas (the pairs that swap duty)**

```bash
for p in orchestrator report-generator; do
  echo "== fieldsight-$p =="
  aws lambda get-function-configuration --function-name "fieldsight-$p" --region ap-southeast-2 --query "Environment.Variables" --output json
  echo "== fieldsight-prod-$p =="
  aws lambda get-function-configuration --function-name "fieldsight-prod-$p" --region ap-southeast-2 --query "Environment.Variables" --output json
done
aws lambda list-functions --region ap-southeast-2 \
  --query "Functions[?starts_with(FunctionName,'fieldsight-prod-')].FunctionName" --output text
```
Check on the prod side: RealPTT credentials present and equal to legacy's; `S3_BUCKET=fieldsight-data-509194952652`; report-generator has a Claude API key and the same prompt/template config keys as legacy. Intended diffs are fine (Dynamo table params, MultiTenant flags). **STOP if prod orchestrator lacks RealPTT creds or either points at a non-lake bucket** — fix the deploy params first.
(If the fargate-trigger function names differ from the `fieldsight-prod-fargate-trigger` guess, find them in the list-functions output — the FargateSchedule rule's target from Step 1 is authoritative.)

- [ ] **Step 3: Back up all 5 legacy schedule definitions (rollback artifacts)**

```bash
mkdir -p "$SCRATCHPAD/sitesync-backup" && cd "$SCRATCHPAD/sitesync-backup"
for n in daily-download fargate-trigger daily-report weekly-report monthly-report; do
  aws scheduler get-schedule --name "$n" --group-name sitesync --region ap-southeast-2 --output json > "$n.json"
done
ls -la
```
(`$SCRATCHPAD` = this session's scratchpad dir.) Expected: 5 JSON files, each with `State: ENABLED`, `ScheduleExpression`, `Target`. Also copy them into the repo job-notes if a PR thread exists.

**Done when:** 6 prod rules confirmed, env parity recorded, 5 backups saved. No state changed.

---

### Task 2: Controlled manual smoke — one prod orchestrator invoke (daytime)

Proves the prod download+extraction chain end-to-end BEFORE any schedule flips, and — because today's recordings haven't been downloaded yet (legacy runs at 19:00) — puts today's items into Aurora immediately.

**Files:** none (one intentional lambda invoke + observation).

**Interfaces:**
- Consumes: Task 1's parity go.
- Produces: evidence the sweep will work; a today `(user,date)` pair with Aurora extraction topics for Phase B's pilot key.

- [ ] **Step 1: Invoke once, watch the run**

```bash
cd "$SCRATCHPAD"
aws lambda invoke --function-name fieldsight-prod-orchestrator \
  --cli-binary-format raw-in-base64-out --payload '{}' \
  orch-out.json --region ap-southeast-2
cat orch-out.json
aws logs tail /aws/lambda/fieldsight-prod-orchestrator --since 10m --region ap-southeast-2
```
Expected: clean exit; logs show either downloads claimed+dispatched (`download_claims/` markers, downloader invocations) or an explicit "no new recordings" pass — BOTH are PASS. **STOP on auth errors against the RealPTT API** (creds problem — back to Task 1 Step 2).

- [ ] **Step 2: If downloads happened, follow the chain (allow ~20–40 min)**

```bash
aws s3 ls "s3://fieldsight-data-509194952652/users/" --recursive --region ap-southeast-2 \
  | grep "$(date +%Y-%m-%d)" | tail -20
for f in vad transcribe extract-session item-writer; do
  echo "== $f =="; aws logs tail "/aws/lambda/fieldsight-prod-$f" --since 1h --region ap-southeast-2 | tail -5
done
```
Then Aurora (Data API, cluster `fieldsight-db-test-dbcluster-hywiixu8ihi9`, secret = `fieldsight-db-test-SecretArn` export, database `fieldsight`):
```bash
aws rds-data execute-statement --resource-arn "arn:aws:rds:ap-southeast-2:509194952652:cluster:fieldsight-db-test-dbcluster-hywiixu8ihi9" \
  --secret-arn "$(aws cloudformation list-exports --query "Exports[?Name=='fieldsight-db-test-SecretArn'].Value" --output text)" \
  --database fieldsight \
  --sql "SELECT source_s3_key, title FROM topics WHERE source_s3_key LIKE 'extractions/%$(date +%Y-%m-%d)%' ORDER BY created_at DESC LIMIT 10" \
  --region ap-southeast-2 --output json | node -e "let d='';process.stdin.on('data',c=>d+=c);process.stdin.on('end',()=>{(JSON.parse(d).records||[]).forEach(r=>console.log(r[0].stringValue,'|',r[1].stringValue))})"
```
Expected: today's extraction topics present (record one `source_s3_key` — this is Phase B Task 0's pilot key). If no device recorded today, note it and re-run this check on the first day with recordings.

**Done when:** one manual invoke completed cleanly with the outcome recorded (chain-verified, or no-recordings pass).

---

### Task 3: The cutover (same afternoon, complete BEFORE 19:00 NZ)

**Files:** none in-repo (GitHub repo variable + AWS scheduler state). The `production` gate approval is the user's action — coordinate before starting.

**Interfaces:**
- Consumes: Task 1 backups; Task 2 pass.
- Produces: prod schedules ENABLED, legacy `sitesync` entries DISABLED — the new steady state Phase B builds on.

- [ ] **Step 1: Flip the repo variable and dispatch the prod deploy**

```bash
cd /c/Users/camil/Dropbox/fieldsight-pipeline
gh variable set PROD_ENABLE_SCHEDULES --body "true"
gh variable list | grep PROD_ENABLE_SCHEDULES
gh workflow run deploy-prod.yml --ref main
```
Then tell the user to approve the `production` gate; watch:
```bash
gh run list --workflow=deploy-prod.yml -L1
gh run watch "$(gh run list --workflow=deploy-prod.yml -L1 --json databaseId -q '.[0].databaseId')" --exit-status
```
Expected: run succeeds (MATCH gate, SAM deploy, migrate, wire-lake no-op re-apply, smoke test all green).

- [ ] **Step 2: Verify the 6 rules are now ENABLED and the sweep actually fires**

```bash
aws events list-rules --region ap-southeast-2 \
  --query "Rules[?starts_with(Name,'fieldsight-prod-')].{N:Name,St:State}" --output table
```
Expected: the 6 schedule rules `ENABLED`. Then wait ≤15 min (inside the 05:00–19:59 NZST window):
```bash
aws logs tail /aws/lambda/fieldsight-prod-orchestrator --since 20m --region ap-southeast-2 | tail -20
```
Expected: at least one sweep invocation on a :00/:15/:30/:45 boundary. **Do not proceed to Step 3 until a sweep run is observed.**

- [ ] **Step 3: Disable the 5 legacy sitesync entries (update-schedule is PUT-semantics — rebuild from backup with State=DISABLED)**

```bash
cd "$SCRATCHPAD/sitesync-backup"
for n in daily-download fargate-trigger daily-report weekly-report monthly-report; do
  node -e "
    const s = require('./$n.json');
    const out = {
      Name: s.Name, GroupName: s.GroupName,
      ScheduleExpression: s.ScheduleExpression,
      FlexibleTimeWindow: s.FlexibleTimeWindow,
      Target: s.Target,
      State: 'DISABLED'
    };
    if (s.ScheduleExpressionTimezone) out.ScheduleExpressionTimezone = s.ScheduleExpressionTimezone;
    if (s.Description) out.Description = s.Description;
    require('fs').writeFileSync('$n.disabled.json', JSON.stringify(out));
  "
  aws scheduler update-schedule --cli-input-json "file://$(cygpath -w "$PWD/$n.disabled.json")" --region ap-southeast-2
done
aws scheduler list-schedules --group-name sitesync --region ap-southeast-2 \
  --query "Schedules[].{N:Name,St:State}" --output table
```
Expected: all 5 `DISABLED`. (Rollback at any time: same loop with `State: 'ENABLED'` — the definitions were never deleted.)

- [ ] **Step 4: Record the cutover moment** — timestamp, deploy run URL, and the rule/schedule state tables — in the tracking thread / job notes.

**Done when:** prod 6 ENABLED + sweep observed + legacy 5 DISABLED, all before 19:00 NZ.

---

### Task 4: Next-morning verification + docs/breadcrumbs

**Files:**
- Modify: `docs/superpowers/plans/2026-07-14-prod-isolation.md` (mark the Task 10 leftover done)
- Modify: `docs/superpowers/plans/2026-07-15-status-and-roadmap.md` (Part 3: schedules cutover shipped; timeliness initiative status)
- Modify: auto-memory `fieldsight-current-progress.md`

- [ ] **Step 1: Overnight run verification (morning after cutover)**

```bash
export MSYS_NO_PATHCONV=1 AWS_PROFILE=fieldsight-deployer
# yesterday in NZ terms (NZST=UTC+12 in July; the 04:00 NZST run reports on the NZ day that just ended)
Y="$(node -e "console.log(new Date(Date.now()+12*3600e3-864e5).toISOString().slice(0,10))")"
# nightly (19:00 NZST) ran on the PROD orchestrator:
aws logs tail /aws/lambda/fieldsight-prod-orchestrator --since 18h --region ap-southeast-2 | grep -i -m5 "nightly\|download"
# daily report generated at 04:00 NZST by the PROD generator:
aws s3 ls "s3://fieldsight-data-509194952652/reports/$Y/" --region ap-southeast-2
aws logs tail /aws/lambda/fieldsight-prod-report-generator --since 18h --region ap-southeast-2 | tail -5
# the LEGACY generator did NOT run (its 02:30 cron is disabled):
aws logs tail /aws/lambda/fieldsight-report-generator --since 18h --region ap-southeast-2 | tail -3
# embed→ingest chain processed the report:
aws logs tail /aws/lambda/fieldsight-prod-ingest --since 18h --region ap-southeast-2 | tail -5
```
Expected: prod nightly + report logs present; legacy generator silent since cutover; exactly ONE `daily_report.json` per user for yesterday; ingest ran. Also open the customer site (`https://main.d2fssznicvuckr.amplifyapp.com/`) — yesterday renders normally (read path untouched by this plan).

- [ ] **Step 2: Intraday latency spot-check (first day with a real recording)** — note the recording's S3 `LastModified` vs its filename timestamp (device time): gap should be ≤ ~20 min during working hours, vs hours before. Record the numbers.

- [ ] **Step 3: Update the three docs** listed above — prod-isolation Task 10 marked done with the cutover date; status-and-roadmap Part 3 gains a "Timeliness (2026-07-15): Phase A shipped, Phase B = authority-flip w/ RETARGET header" entry; memory reflects the new steady state (recordings intraday; reports 04:00 NZST by fieldsight-prod-report-generator; legacy sitesync crons disabled-not-deleted).

- [ ] **Step 4: Commit (docs only, to develop)**

```bash
cd /c/Users/camil/Dropbox/fieldsight-pipeline
git add docs/superpowers/plans/2026-07-14-prod-isolation.md docs/superpowers/plans/2026-07-15-status-and-roadmap.md
git commit -m "docs: schedules cutover shipped — prod stack owns download/report crons, 15-min intraday sweep live"
git push origin develop
```

**Done when:** one clean overnight cycle verified end-to-end + docs/memory updated.

---

## Rollback (any point after Task 3)

1. Re-enable legacy: the Task 3 Step 3 loop with `State: 'ENABLED'` rebuilt from `sitesync-backup/*.json`.
2. Disable prod: `gh variable set PROD_ENABLE_SCHEDULES --body "false"` + `gh workflow run deploy-prod.yml --ref main` (+ gate approval).
3. Order matters the same way: don't let both daily crons be enabled across a 19:00 NZ boundary (claim locks make an overlap safe for downloads, but the report would generate twice — harmless but noisy).

## Out of scope / noted risks

- **Three transcribe-callback listeners** (`fieldsight-transcribe-state-change` legacy + test + prod stack rules) are all ENABLED — pre-existing, unrelated to schedules; callbacks are keyed by job so this is noise, not corruption. Left as-is; flagged for the prod-isolation cleanup list.
- Weekly/monthly report hour shifts (see the map table) — accepted.
- The read path stays report-based until Phase B — intraday items are visible only via the Safety/Quality pages' live-items merge until the authority flip lands.
