# System Architecture Overview

## Complete Automated Pipeline

```
                        ┌──────────────────────────────────────────────────────────┐
                        │              FieldSight Automated Pipeline v4.0                │
                        └──────────────────────────────────────────────────────────┘

  20:00 NZDT              20:30 NZDT                       05:00 NZDT     Fri 18:00 / Month-end
      │                       │                                │               │
      ▼                       ▼                                ▼               ▼
┌────────────┐        ┌──────────────┐                  ┌────────────┐  ┌────────────┐
│ EventBridge│        │ EventBridge  │                  │ EventBridge│  │ EventBridge│
│ (Daily)    │        │ (+30 min)    │                  │ (Daily)    │  │ (Wk/Mo)    │
└─────┬──────┘        └──────┬───────┘                  └─────┬──────┘  └─────┬──────┘
      │                      │                                │               │
      ▼                      ▼                                ▼               ▼
┌────────────┐        ┌──────────────┐                  ┌──────────────────────────┐
│  Lambda 1  │        │  Lambda 4    │                  │      Lambda 5            │
│Orchestrator│        │Fargate Trigger│                 │  Report Generator v3.1   │
│   v3       │        │              │                  │  (daily/weekly/monthly)   │
└─────┬──────┘        └──────┬───────┘                  └─────────────┬────────────┘
      │                      │                                        │
      │ Login to             │ Check pending_downloads/               │
      │ realptt.com          │                                        │
      │                      ▼                                        │
      │ Query ALL     ┌──────────────┐                                │
      │ users' files  │  ECS Fargate │ ◄── No time limit             │
      │               │  Downloader  │     Parallel downloads         │
      ▼               │  (3 threads) │     Streaming multipart        │
┌────────────┐        └──────┬───────┘                                │
│  Lambda 2  │               │                                        │
│ Downloader │ ◄─ Async      │                                        │
│(small files│   invoke      │                                        │
│ <15 min)   │               │                                        │
└─────┬──────┘               │                                        │
      │                      │                                        │
      │  Download & Upload   │                                        │
      ▼                      ▼                                        │
┌─────────────────────────────────────────────────────────────────┐   │
│                        S3 Bucket                                │   │
│  nottag-bs-manual-1                                             │   │
│                                                                 │   │
│  users/{name}/{type}/{date}/{file}     ← raw media files        │   │
│       │                                                         │   │
│       │ S3 Event (ObjectCreated on users/*)                     │   │
│       ▼                                                         │   │
│  ┌──────────┐     ┌────────────┐                                │   │
│  │ Lambda 3 │────►│   AWS      │                                │   │
│  │Transcribe│     │ Transcribe │                                │   │
│  │ Trigger  │     │  (async)   │                                │   │
│  └──────────┘     └─────┬──────┘                                │   │
│                         │                                       │   │
│                         ▼                                       │   │
│  transcripts/{name}/{date}/{basename}.json                      │   │
│                                                                 │   │
│  pending_downloads/{uuid}.json  ← Lambda timeout fallback       │   │
│  scripts/fargate_downloader.py  ← Fargate reads this at boot    │   │
│                                                                 │   │
└─────────────────────────────────────────────────────────────────┘   │
                                                                      │
      ┌───────────────────────────────────────────────────────────────┘
      │
      ▼
┌──────────────────┐     ┌─────────────────────────────────────────────┐
│ Anthropic Claude │     │  reports/{date}/                            │
│ API (Sonnet 4.5) │────►│    ├── summary_report.json + .docx         │
│ Structured JSON  │     │    ├── Jarley_Trainor/                     │
└──────────────────┘     │    │     ├── daily_report.json + .docx     │
                         │    │     └── daily_report_debug.json        │
      ┌──────────────┐   │    └── David_Barillaro/                    │
      │  DynamoDB    │   │          └── ...                            │
      │  (optional)  │   └─────────────────────────────────────────────┘
      │  items/      │
      │  reports/    │
      │  audit/      │
      └──────────────┘
```

---

## S3 Folder Structure

```
nottag-bs-manual-1/
│
├── config/
│   └── user_mapping.json              # Device → User mapping (v2.1 with roles/sites)
│
├── users/                             # Raw media files (date-layered)
│   ├── Jarley_Trainor/
│   │   ├── audio/
│   │   │   └── 2026-02-19/
│   │   │       └── Benl1_2026-02-19_10-30-00.wav
│   │   ├── video/
│   │   │   └── 2026-02-19/
│   │   │       └── Benl1_2026-02-19_11-00-00.mp4
│   │   └── pictures/
│   │       └── 2026-02-19/
│   │           └── Benl1_2026-02-19_10-28-00.jpg
│   │
│   └── David_Barillaro/
│       └── ...
│
├── transcripts/                       # Transcription results (date-layered)
│   ├── Jarley_Trainor/
│   │   └── 2026-02-19/
│   │       └── Benl1_2026-02-19_10-30-00.json
│   └── David_Barillaro/
│       └── ...
│
├── reports/                           # AI-generated reports (per-user folders)
│   └── 2026-02-19/
│       ├── summary_report.json        # Combined report (all users)
│       ├── summary_report.docx
│       ├── summary_report_debug.json  # Prompt + raw response (for tuning)
│       ├── Jarley_Trainor/
│       │   ├── daily_report.json      # Individual structured report
│       │   ├── daily_report.docx
│       │   └── daily_report_debug.json
│       └── David_Barillaro/
│           └── ...
│
├── pending_downloads/                 # Fargate queue (auto-cleaned after 7 days)
│   └── {uuid}.json                    # Download job: {s3_key, download_url, file_info}
│
└── scripts/
    └── fargate_downloader.py          # Fargate container downloads this at boot
```

---

## User Mapping Configuration (v2.1)

Upload `config/user_mapping.json` to S3. Supports multi-site, roles, and device reassignment tracking:

```json
{
    "_version": "2.1",
    "sites": {
        "sb1108-ellesmere": {
            "name": "SB1108 Ellesmere College",
            "location": "Christchurch",
            "client": "Ministry of Education"
        }
    },
    "mapping": {
        "Benl1": {
            "name": "Jarley Trainor",
            "role": "site_manager",
            "primary_site": "sb1108-ellesmere",
            "sites": ["sb1108-ellesmere"]
        },
        "Benl6": {
            "name": "Andrew McMillan",
            "role": "pm",
            "primary_site": "sb1108-ellesmere",
            "sites": ["sb1108-ellesmere"]
        }
    },
    "reassignment_log": []
}
```

Both v1 (string values) and v2 (nested objects) are supported. Code normalizes to `device → display name` at load time.

If a device is not in the mapping, its account name is used as-is.

---

## Component Summary

| # | Component | Name | Trigger | Function |
|---|-----------|------|---------|----------|
| 1 | Lambda | fieldsight-orchestrator | EventBridge 20:00 NZDT daily + Sat 22:00 catchup | Login, query file lists, trigger downloads |
| 2 | Lambda | fieldsight-downloader | Async invocation from Lambda 1 | Download single file → S3 (< 15 min) |
| 3 | Lambda | fieldsight-transcribe | S3 Event (ObjectCreated on users/*) | Start AWS Transcribe job |
| 4 | Lambda | fieldsight-fargate-trigger | EventBridge 20:30 NZDT | Check pending_downloads/, launch Fargate if needed |
| 5 | Lambda | fieldsight-report-generator | EventBridge daily/weekly/monthly | Collect transcripts, call Claude, generate reports |
| 6 | ECS Fargate | fieldsight-fargate-downloader | Lambda 4 → ecs:RunTask | Download large files with no time limit |
| 7 | S3 | nottag-bs-manual-1 | — | Store all files |
| 8 | Transcribe | — | Lambda 3 | Speech to text (auto language detection) |
| 9 | Anthropic API | Claude Sonnet 4.6 | Lambda 5 | Structured JSON report generation |
| 10 | DynamoDB | fieldsight-items / reports / audit | Lambda 5 | Report items, metadata, audit log (optional) |
| 11 | Lambda | fieldsight-vad | S3 Event (video/audio upload) | VAD speech detection, audio segmentation |
| 12 | Lambda | fieldsight-api | API Gateway | Frontend REST API (sites, users, reports, recordings) |
| 13 | Lambda | fieldsight-transcribe-callback | EventBridge (Transcribe state change) | Process completed transcription jobs |
| 14 | Lambda | fieldsight-meeting-minutes | Manual / API invoke | Generate meeting minutes from transcripts |
| 15 | API Gateway | fieldsight-api (REST) | Frontend | HTTPS endpoint for React frontend |
| 16 | CloudFront | fieldsight-web | — | CDN for static frontend assets |
| 17 | Cognito | fieldsight-users (pool) | Frontend | User authentication and authorization |
| 18 | Lambda Layer | fieldsight-vad-layer | Lambda 11 | Silero VAD model + dependencies |

---

## Download Strategy: Lambda + Fargate

Small/medium files are downloaded by **Lambda 2** (up to 15 min timeout). When downloads would exceed Lambda's time limit (e.g. 150–300 MB body cam videos), the orchestrator writes a **pending download job** to `pending_downloads/{uuid}.json`.

At **20:30 NZDT** (30 min after orchestrator), **Lambda 4 (Fargate Trigger)** checks if any pending jobs exist. If so, it launches an **ECS Fargate task** that:

- Has **no time limit** (runs until complete)
- Uses **parallel downloads** (3 threads via ThreadPoolExecutor)
- Streams directly to S3 via **multipart upload** (6 MB chunks)
- Costs ~$0.01–0.03 per run (FARGATE_SPOT pricing)

```
Orchestrator (20:00)                    Fargate Trigger (20:30)
     │                                        │
     ├─── Small file → Lambda Downloader      │
     ├─── Small file → Lambda Downloader      │
     ├─── Large file → pending_downloads/     │──── Any pending? ──→ ECS Fargate
     └─── Large file → pending_downloads/     │                     (parallel DL)
```

---

## Report Generation Strategy

### Modes

| Mode | Schedule (NZDT) | EventBridge Cron (UTC) | Input |
|------|----------------|----------------------|-------|
| Daily | 05:00 daily | `cron(0 16 * * ? *)` | `{"report_type": "daily"}` |
| Weekly | Friday 18:00 | `cron(0 5 ? * FRI *)` | `{"report_type": "weekly"}` |
| Monthly | Last day of month | `cron(0 17 L * ? *)` | `{"report_type": "monthly"}` |

### Structured Output (not free-text)

Claude returns **structured JSON** with topic-based analysis:

```json
{
  "executive_summary": "2-3 sentence overview",
  "topics": [
    {
      "topic_id": 0,
      "time_range": "08:15 – 09:00",
      "topic_title": "Morning Safety Briefing",
      "category": "safety",
      "summary": "...",
      "key_decisions": ["..."],
      "action_items": [
        {"action": "...", "responsible": "...", "deadline": "...", "priority": "high"}
      ],
      "safety_flags": [
        {"observation": "...", "risk_level": "high", "recommended_action": "..."}
      ]
    }
  ],
  "safety_observations": [...]
}
```

### Auto-Backfill

On each daily run, the report generator checks the past 7 days for **stale reports** (where new transcripts have arrived since the report was generated). Stale dates are automatically regenerated.

### Debug Records

Every Claude call saves a debug file alongside the report (`daily_report_debug.json`) containing the full prompt, raw response, and parsed JSON — for prompt tuning and troubleshooting.

---

## Lookback & Catchup Strategy

| Schedule | Days Back | Purpose |
|----------|-----------|---------|
| Daily 20:00 NZDT | 3 days | Catches files uploaded 1–2 days late |
| Saturday 22:00 NZDT | 7 days | Deep weekly scan for anything missed |
| Report backfill | 7 days | Auto-regenerates stale reports |

The orchestrator supports `override_days_ago` in the event payload for the weekly catchup rule.

---

## Schedule Timeline (NZDT)

```
NZDT Timeline
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
00:00   04:00   08:00   12:00   16:00   20:00   22:00  24:00
  │       │       │       │       │       │       │      │
  │    ┌──┴──┐    │       │       │    ┌──┴──┐ ┌──┴──┐   │
  │    │Daily│    │       │       │    │Orch │ │Sat  │   │
  │    │Rpt  │    │       │       │    │20:00│ │Catch│   │
  │    │05:00│    │       │       │    ├─────┤ │up   │   │
  │    └─────┘    │       │       │    │FG   │ │22:00│   │
  │               │       │       │    │20:30│ └─────┘   │
  │            ┌──┴──┐    │       │    └─────┘           │
  │            │Fri  │    │       │                      │
  │            │Wkly │    │       │                      │
  │            │18:00│    │       │                      │
  │            └─────┘    │       │                      │
━━┻━━━━━━┻━━━━━━━┻━━━━━━━┻━━━━━━━┻━━━━━━┻━━━━━━┻━━━━━━━┻━━

Legend:
  Orch   = Orchestrator (download files, 3-day lookback)
  FG     = Fargate Trigger (check pending large downloads)
  Sat    = Saturday weekly catchup (7-day lookback)
  Daily  = Report Generator (yesterday + backfill check)
  Fri    = Weekly report (Mon–Sun summary)
```

---

## DynamoDB Tables (Optional)

Controlled by `ENABLE_DYNAMODB` environment variable (default: `false`).

All tables use on-demand billing and `PK` (String) + `SK` (String) key schema.

| Table | PK Pattern | SK Pattern | Purpose |
|-------|-----------|-----------|---------|
| fieldsight-items | `SITE#{id}#DATE#{date}` | `ITEM#{time}#{topic_id}` | Individual topics, recordings, safety flags |
| fieldsight-reports | `SITE#{id}#DATE#{date}` | `REPORT#{type}#{timestamp}` | Report metadata, version history |
| fieldsight-audit | `SITE#{id}#DATE#{date}` | `AUDIT#{timestamp}` | All actions (generate, hide, regenerate) |

Region: **ap-southeast-2** (must match Lambda region).

---

## Lambda Layers

| Layer | Purpose | Contents |
|-------|---------|----------|
| python-docx-layer | Word document generation | python-docx + lxml (Python 3.11, x86_64 linux) |

The `lxml` binary must be compiled for `cpython-311-x86_64-linux-gnu`. If the layer is missing or incompatible, Word generation is automatically disabled (reports are still generated as JSON).

---

## Cost Estimation (Monthly)

| Service | Unit Price | Est. Usage | Monthly Cost |
|---------|-----------|------------|--------------|
| S3 Storage | $0.025/GB | 10 GB | $0.25 |
| S3 Requests | $0.005/1K | 10K | $0.05 |
| Lambda | $0.20/1M requests | 5K | ~$0 (free tier) |
| Transcribe | $0.024/minute | 1000 min | $24.00 |
| Anthropic API (Sonnet) | ~$3/1M input, $15/1M output | ~50K in, ~10K out | ~$0.30 |
| Fargate (SPOT) | ~$0.01/run | 30 runs | ~$0.30 |
| DynamoDB (on-demand) | $1.25/1M writes | <1K | ~$0 |
| **Total** | | | **~$25/month** |

---

## Output Formats

### Transcript JSON (from AWS Transcribe)

```json
{
  "results": {
    "transcripts": [{"transcript": "Full text here..."}],
    "items": [
      {
        "start_time": "0.5",
        "end_time": "1.0",
        "type": "pronunciation",
        "alternatives": [{"content": "Hello", "confidence": "0.98"}]
      }
    ]
  }
}
```

### Daily Report JSON (v3.1 structured)

```json
{
  "report_date": "2026-02-19",
  "report_type": "daily",
  "user_name": "Jarley Trainor",
  "device": "Benl1",
  "site": "SB1108 Ellesmere College",
  "executive_summary": "AI-generated 2-3 sentence overview...",
  "topics": [
    {
      "topic_id": 0,
      "time_range": "08:15 – 09:00",
      "topic_title": "Morning Safety Briefing",
      "category": "safety",
      "summary": "2-4 sentence topic summary...",
      "key_decisions": ["Decision 1"],
      "action_items": [
        {
          "action": "What needs to be done",
          "responsible": "Person name",
          "deadline": "Tomorrow 08:00",
          "priority": "high"
        }
      ],
      "safety_flags": [
        {
          "observation": "What was observed",
          "risk_level": "high",
          "recommended_action": "What should be done"
        }
      ]
    }
  ],
  "safety_observations": [
    {
      "observation": "Site-wide safety observation",
      "risk_level": "medium",
      "location": "Block C"
    }
  ],
  "_report_metadata": {
    "version": "v3.1",
    "generated_at": "2026-02-20T16:00:00Z",
    "generated_by": "system",
    "recordings_processed": 12,
    "total_words": 3450,
    "model": "claude-sonnet-4-6",
    "parse_success": true
  }
}
```

### Debug Record JSON

```json
{
  "_description": "Debug record for prompt tuning",
  "timestamp": "2026-02-20T16:00:00Z",
  "model": "claude-sonnet-4-6",
  "prompt": "Full prompt sent to Claude...",
  "prompt_length": 8500,
  "raw_response": "Raw Claude response text...",
  "parsed_json": { "...structured output..." },
  "parse_success": true,
  "input_stats": {
    "transcripts_count": 12,
    "total_words": 3450,
    "photos_count": 5
  }
}
```

### Daily Report Word Document

- Title with user name and date
- Executive Summary
- ⚠ Safety Observations (color-coded by risk level)
- Detailed Timeline (topics grouped chronologically)
  - Category tags (SAFETY / PROGRESS / QUALITY)
  - Key Decisions
  - Action Items with priority, responsible person, deadline
  - Safety Flags (HIGH risk in red)
  - Related photo filenames
- Metadata footer (model, version, recording count)