# Monitoring & Debugging Guide

## Quick Access Links

| What | Where |
|------|-------|
| Lambda functions | AWS Console → Lambda → Functions |
| Logs | AWS Console → CloudWatch → Log groups |
| Alarms | AWS Console → CloudWatch → Alarms |
| S3 files | AWS Console → S3 → nottag-bs-manual-1 |
| EventBridge | AWS Console → EventBridge → Rules |
| ECS / Fargate | AWS Console → ECS → Clusters → fieldsight-downloader-cluster |
| DynamoDB | AWS Console → DynamoDB → Tables (fieldsight-*) |

**Region:** All resources are in **ap-southeast-2 (Sydney)**. Always verify the region in the top-right corner of AWS Console before checking any service.

---

## 1. Did the Pipeline Run? (EventBridge)

**Check EventBridge rule invocations:**

```
AWS Console → Amazon EventBridge → Rules
```

| Rule | Cron (UTC) | NZDT | Target |
|------|-----------|------|--------|
| Orchestrator daily | `cron(0 7 * * ? *)` | 20:00 daily | fieldsight-orchestrator |
| Orchestrator weekly catchup | `cron(0 9 ? * SAT *)` | Sat 22:00 | fieldsight-orchestrator (7-day lookback) |
| Fargate Trigger | `cron(30 7 * * ? *)` | 20:30 daily | fieldsight-fargate-trigger |
| Daily Report | `cron(0 16 * * ? *)` | 05:00 daily | fieldsight-report-generator |
| Weekly Report | `cron(0 5 ? * FRI *)` | Fri 18:00 | fieldsight-report-generator |
| Monthly Report | `cron(0 17 L * ? *)` | Last day of month | fieldsight-report-generator |

- **Monitoring tab** on each rule shows invocation history
- If a rule is disabled, click **Enable**

**Common issue:** EventBridge Cron is in **UTC**, not NZDT. NZDT = UTC + 13 hours.

---

## 2. Lambda Execution History

### Quick view — all Lambdas at once:
```
AWS Console → Lambda → Functions
```
Each function shows:
- **Last invocation** timestamp
- **Success rate** (green/red)
- **Duration** and **Memory used**

### All Lambda functions:

| Function | Timeout | Memory | Purpose |
|----------|---------|--------|---------|
| fieldsight-orchestrator | 900s (15 min) | 256 MB | Query file lists, trigger downloads |
| fieldsight-downloader | 900s (15 min) | 512 MB | Download single file → S3 |
| fieldsight-transcribe | 60s | 128 MB | Start AWS Transcribe job |
| fieldsight-fargate-trigger | 30s | 128 MB | Check pending, launch Fargate |
| fieldsight-report-generator | 900s (15 min) | 512 MB | Generate daily/weekly/monthly reports |
| fieldsight-vad | 300s (5 min) | 3008 MB | VAD speech detection, audio segmentation |
| fieldsight-api | 60s | 256 MB | Frontend REST API |
| fieldsight-transcribe-callback | 30s | 128 MB | Process Transcribe job completions |
| fieldsight-meeting-minutes | 300s (5 min) | 512 MB | Generate meeting minutes |

### Detailed per-function:
```
Lambda → fieldsight-orchestrator → Monitor tab
```

Key metrics:
| Metric | What it tells you |
|--------|-------------------|
| **Invocations** | How many times it ran |
| **Errors** | How many runs failed |
| **Duration** | How long each run took |
| **Throttles** | If Lambda concurrency was hit |

---

## 3. Viewing Logs (Most Important)

### Find logs:
```
AWS Console → CloudWatch → Log groups →
  /aws/lambda/fieldsight-orchestrator        ← Query + trigger downloads
  /aws/lambda/fieldsight-downloader          ← Individual file downloads
  /aws/lambda/fieldsight-transcribe          ← Transcription jobs
  /aws/lambda/fieldsight-report-generator    ← Report creation
  /aws/lambda/fieldsight-fargate-trigger     ← Fargate launch decisions
  /ecs/fieldsight-fargate-downloader         ← Fargate container logs (large file downloads)
  /aws/lambda/fieldsight-vad                 ← VAD speech detection
  /aws/lambda/fieldsight-api                 ← Frontend API requests
  /aws/lambda/fieldsight-transcribe-callback ← Transcribe completions
  /aws/lambda/fieldsight-meeting-minutes     ← Meeting minutes generation
```

### Read a specific run:
1. Click the log group (e.g., `/aws/lambda/fieldsight-orchestrator`)
2. Click the most recent **Log stream** (sorted newest first)
3. Look for the structured output:

**Orchestrator (healthy run):**
```
FieldSight Pipeline v3 - Starting
Query range: 2026-02-18 to 2026-02-19
Loaded user mapping: 6 entries
Querying videos via org_videolist...
  HTML size: 84346 bytes
  Found 20 videos
Querying audio via org_audiolist (per group/day)...
  2 days x 2 groups = 4 queries
  [1/4] NorthIsland/2026-02-18: 5 recordings
  Total audio: 5
Querying upload files...
  Upload files: page 1/3, total 20
  Upload files total: 52
Sync Complete!
  Total found:     77
  Already exists:  45
  Downloads fired: 32
  By type: {'video': 10, 'audio': 5, 'upload': 17}
  By user:
    Jarley Trainor: 15 files
    David Barillaro: 12 files
    Andrew McMillan: 5 files
```

**Report Generator (healthy run):**
```
Report Generator v3.1 - Starting
Word generation: enabled                     ← python-docx layer OK
DynamoDB: DISABLED (set ENABLE_DYNAMODB=true to enable)
=== Generating DAILY report for 2026-02-19 ===
Loaded user mapping: 6 entries (format: v2)  ← v2 mapping loaded correctly
Found 3 users: ['David_Barillaro', 'Jarley_Trainor', 'Andrew_McMillan']
Processing user: Jarley_Trainor for 2026-02-19
  Found 8 transcripts, 3 photos, 2150 words
  Saved debug: reports/2026-02-19/Jarley_Trainor/daily_report_debug.json
Saved: reports/2026-02-19/Jarley_Trainor/daily_report.json
Saved: reports/2026-02-19/Jarley_Trainor/daily_report.docx
Checking past 7 days for stale reports...
  Backfill 2026-02-17: up to date (15 transcripts = 15 in report)
  Backfill 2026-02-18: report has 10, now 12 transcripts → STALE, regenerate
All past reports are up to date.
```

**Fargate Trigger (healthy run):**
```
No pending downloads. Skipping Fargate.
```
or
```
Found pending downloads. Launching Fargate task...
Fargate task started: arn:aws:ecs:ap-southeast-2:xxx:task/fieldsight-downloader-cluster/xxx
```

**Fargate Container (healthy run):**
```
Fargate Downloader - Starting
S3 Bucket: nottag-bs-manual-1
Found 5 pending downloads
[1/5] Downloading: users/Jarley_Trainor/video/2026-02-19/Benl1_2026-02-19_14-20-00.mp4
  File size: 245.3 MB
  50.0 MB/245.3 MB (20%) 2.10 MB/s ETA:1m33s
  100.0 MB/245.3 MB (41%) 2.15 MB/s ETA:1m08s
  ...
  Done: 245.3 MB in 1m52s (2.18 MB/s)
Fargate Downloader - Complete
  Success: 5
  Failed:  0
  Total downloaded: 812.5 MB
  Total time: 6m23s
```

### Search for errors:
```
CloudWatch → Log groups → /aws/lambda/fieldsight-orchestrator
→ Search log group → Enter filter: "ERROR" or "failed" or "Exception"
```

### Downloader errors (check per-file failures):
```
CloudWatch → Log groups → /aws/lambda/fieldsight-downloader
→ Search: "Download failed" or "HTTP 4" or "HTTP 5" or "Timeout"
```

### Fargate errors:
```
CloudWatch → Log groups → /ecs/fieldsight-fargate-downloader
→ Search: "Failed" or "ERROR" or "Exception"
```

---

## 4. Report Generator Health Checks

The report generator log shows several critical status lines at startup. Here's what to look for:

| Log line | Healthy | Problem |
|----------|---------|---------|
| `Word generation: enabled` | ✅ python-docx layer working | ❌ `DISABLED` = layer missing or incompatible |
| `DynamoDB: enabled` | ✅ tables connected | ℹ️ `DISABLED` = optional, set ENABLE_DYNAMODB=true |
| `Loaded user mapping: 6 entries (format: v2)` | ✅ v2 mapping loaded | ❌ `format: v1` with v2 JSON = code outdated |
| `parse_success: true` | ✅ Claude returned valid JSON | ❌ `false` = prompt issue, check debug file |

### Checking debug records:

Every Claude API call saves a debug file you can inspect:
```
S3 → nottag-bs-manual-1 → reports/{date}/{user}/daily_report_debug.json
```

Contains:
- `prompt` — the full prompt sent to Claude
- `raw_response` — Claude's exact response text
- `parsed_json` — the extracted JSON (or null if parse failed)
- `parse_success` — whether JSON extraction succeeded
- `input_stats` — transcript count, word count, photo count

### Checking backfill activity:

In the report generator log, look for:
```
Checking past 7 days for stale reports...
  Backfill 2026-02-17: up to date (15 transcripts = 15 in report)
  Backfill 2026-02-18: report has 10, now 12 transcripts → STALE, regenerate
```

If a date shows `STALE`, the report is automatically regenerated with the latest transcripts.

---

## 5. Email Alerts (Automatic)

If you provided `AlertEmail` during deployment, you'll receive emails when:

| Alert | Trigger |
|-------|---------|
| `fieldsight-orchestrator-errors` | Orchestrator fails (any error) |
| `fieldsight-downloader-errors` | >5 download failures in a day |
| `fieldsight-report-errors` | Report generator fails |

**First time:** Check your email for the SNS subscription confirmation and click **Confirm subscription**.

Manage alerts:
```
AWS Console → CloudWatch → Alarms
```

---

## 6. Common Failure Scenarios

### Orchestrator: "Login failed"
- **Cause:** Password changed, account locked, or REAL PTT server down
- **Fix:** Update password in Lambda console → Configuration → Environment variables
- **Log pattern:** `Login failed: xxx`

### Orchestrator: Timeout (>15 min)
- **Cause:** Too many date × group combinations for audio (START_DAYS_AGO too large)
- **Fix:** Reduce `START_DAYS_AGO` environment variable
- **Log pattern:** `Task timed out after 900.00 seconds`

### Downloader: "HTTP 403" or "HTTP 404"
- **Cause:** Session expired between orchestrator and downloader
- **Fix:** Downloader uses direct URLs that don't need auth for upload files and videos. Audio URLs (record.realptt.com) also don't need auth.
- **Log pattern:** `HTTP 403` or `HTTP 404`

### Downloader: "Empty file" or "Timeout"
- **Cause:** REAL PTT server intermittent issues (known behavior)
- **Fix:** Auto-retry (3 attempts built in). Large files that timeout will be picked up by Fargate.
- **Log pattern:** `Empty file` or `Timeout`

### Fargate: Task fails to start
- **Cause:** Subnet has no internet access, or security group blocks outbound
- **Fix:** Verify subnets are public (have IGW route), security group allows all outbound
- **Log pattern:** Check ECS → Clusters → fieldsight-downloader-cluster → Tasks → Stopped → Stopped reason

### Fargate: "pip install" fails
- **Cause:** Container can't reach pypi.org (network issue)
- **Fix:** Check security group egress rules, NAT gateway if using private subnets
- **Log pattern:** `pip: error` in `/ecs/fieldsight-fargate-downloader`

### Transcribe: "Job already exists"
- **Cause:** Same file processed twice (normal when re-running or with lookback > 1 day)
- **Fix:** No action needed, it's a safety check
- **Log pattern:** `Job xxx already exists, status: COMPLETED`

### Report: "Word generation: DISABLED"
- **Cause:** python-docx Lambda Layer missing, not bound, or binary incompatible
- **Fix:** Upload correct layer zip (Python 3.11, x86_64 linux). Verify lxml `.so` files contain `cpython-311-x86_64-linux-gnu` in filename. Bind layer to fieldsight-report-generator.
- **Log pattern:** `Word generation: DISABLED (no python-docx layer)`

### Report: "lxml etree import error"
- **Cause:** Layer zip was built on wrong platform (Windows/Mac/Python 3.12)
- **Fix:** Rebuild layer with `pip download lxml --python-version 3.11 --platform manylinux_2_28_x86_64 --only-binary=:all:`
- **Log pattern:** `cannot import name 'etree' from 'lxml'`

### Report: DynamoDB "ResourceNotFoundException"
- **Cause:** Tables created in wrong region, or not yet created
- **Fix:** Create tables in **ap-southeast-2** (same region as Lambda). Or set `ENABLE_DYNAMODB=false` to skip.
- **Log pattern:** `Requested resource not found: Table: fieldsight-items not found`

### Report: "Claude API error" or timeout
- **Cause:** API key invalid, rate limited, or response too slow
- **Fix:** Check ANTHROPIC_API_KEY env var. Timeout is 180s. Inspect debug file for details.
- **Log pattern:** `Claude API error:` or `Claude API call failed:`

### Report: "Failed to parse Claude JSON"
- **Cause:** Claude returned non-JSON or malformed response
- **Fix:** Check debug file (`daily_report_debug.json`) → `raw_response` field. Report still saved with raw text as fallback.
- **Log pattern:** `Failed to extract JSON from Claude response`

### Report: User mapping shows dict instead of name
- **Cause:** Code expects v1 mapping (strings) but S3 has v2 (nested objects)
- **Fix:** Update Lambda code to v3.1 which normalizes both formats. Check log for `format: v2`.
- **Log pattern:** Folder names containing `{` or `name` instead of actual user names

---

## 7. Manual Testing

### Test orchestrator (without waiting for schedule):
```
Lambda → fieldsight-orchestrator → Test tab → Test event:
{}
```
Or for specific lookback:
```json
{"override_days_ago": 5}
```

### Test report generator (safe — uses a date with no data):
```json
{"report_type": "daily", "date": "2000-01-01", "skip_backfill": true}
```
Check the log for:
- `Word generation: enabled` or `DISABLED`
- `DynamoDB: enabled` or `DISABLED`
- `Loaded user mapping: N entries (format: v2)`
- `Found 0 transcripts` (expected for a date with no data)

### Test report generator (real date):
```json
{"report_type": "daily", "date": "2026-02-19", "skip_backfill": true}
```

### Test report generator (with backfill):
```json
{"report_type": "daily"}
```
This generates yesterday's report AND checks past 7 days for stale reports.

### Test Fargate trigger:
```
Lambda → fieldsight-fargate-trigger → Test tab → Test event:
{}
```
If no pending downloads exist, it will log "No pending downloads. Skipping Fargate."

### Check what's in S3:
```bash
# List all users
aws s3 ls s3://nottag-bs-manual-1/users/

# List a user's files for a date
aws s3 ls s3://nottag-bs-manual-1/users/Jarley_Trainor/audio/2026-02-19/

# List today's reports
aws s3 ls s3://nottag-bs-manual-1/reports/2026-02-19/ --recursive

# List pending downloads (should be empty if Fargate ran)
aws s3 ls s3://nottag-bs-manual-1/pending_downloads/

# Check a debug record
aws s3 cp s3://nottag-bs-manual-1/reports/2026-02-19/Jarley_Trainor/daily_report_debug.json - | python -m json.tool
```

### Manually run Fargate task (from Console):
```
ECS → Clusters → fieldsight-downloader-cluster → Tasks → Run new task
  Launch type: FARGATE
  Task definition: fieldsight-fargate-downloader (latest revision)
  Subnets: pick your public subnet
  Security group: fieldsight Fargate security group
  Auto-assign public IP: ENABLED
```

---

## 8. CloudWatch Insights (Advanced Queries)

For power-user log analysis:
```
AWS Console → CloudWatch → Logs Insights
```

**Find all errors in last 24h (all Lambda functions):**
```
fields @timestamp, @message
| filter @message like /ERROR|Exception|failed|Timeout/
| sort @timestamp desc
| limit 50
```

**Count downloads per user today:**
```
fields @timestamp, @message
| filter @message like /Download complete/
| parse @message "User: *" as user
| stats count(*) as downloads by user
```

**Find slow downloads (>60s):**
```
fields @timestamp, @message, @duration
| filter @duration > 60000
| sort @duration desc
```

**Report generator — find parse failures:**
```
fields @timestamp, @message
| filter @message like /Failed to extract JSON|parse_success.*false/
| sort @timestamp desc
| limit 20
```

**Report generator — backfill activity:**
```
fields @timestamp, @message
| filter @message like /STALE|Backfill|backfill/
| sort @timestamp desc
| limit 20
```

**Fargate — download speeds and totals:**
```
fields @timestamp, @message
| filter @message like /Done:|Total downloaded/
| sort @timestamp desc
| limit 20
```

---

## 9. DynamoDB Monitoring (Optional)

If `ENABLE_DYNAMODB=true`, you can inspect data in:

```
AWS Console → DynamoDB → Tables → Explore items
```

| Table | What to check |
|-------|--------------|
| fieldsight-items | Individual topics — filter by PK = `SITE#sb1108-ellesmere#DATE#2026-02-19` |
| fieldsight-reports | Report versions — see generation history |
| fieldsight-audit | Full audit trail — who generated/regenerated, when, why |

**Common checks:**
- Are items being written after report generation?
- Does the audit log show `backfill` entries for stale dates?
- How many topics per date? Filter by PK pattern.

---

## 10. Cost Monitoring

```
AWS Console → Billing → Cost Explorer
→ Filter by Service: Lambda, S3, Transcribe, ECS, DynamoDB
→ Group by: Service
→ Region: ap-southeast-2
```

Expected monthly costs (~$25):

| Service | Est. Cost | Notes |
|---------|-----------|-------|
| Lambda | ~$0 | Free tier covers this usage |
| S3 (10 GB) | ~$0.25 | Storage + requests |
| Transcribe (1000 min) | ~$24 | Main cost driver |
| Anthropic API | ~$0.30 | Sonnet 4.5 for report summaries |
| Fargate (SPOT) | ~$0.30 | ~$0.01 per task, ~30 tasks/month |
| DynamoDB | ~$0 | On-demand, minimal writes |
| **Total** | **~$25/month** | |

**Transcribe** is the dominant cost. To reduce: skip transcription for short recordings, or reduce lookback window.