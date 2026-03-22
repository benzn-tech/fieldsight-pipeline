# FieldSight Pipeline

Automated pipeline that downloads construction site radio recordings, body camera footage, and photos from RealPTT Cloud Platform, transcribes them, and generates AI-powered daily reports.

## Architecture

```
EventBridge (20:00 NZDT)
    │
    ▼
Lambda 1: Orchestrator ──→ realptt.com (web scraping)
    │                       ├── org_videolist (HTML → Ud() parse)
    │                       ├── org_audiolist (HTML → per group/day)
    │                       └── /ptt/uploadFile (JSON, limit=20)
    │
    │ Async invoke per file
    ▼
Lambda 2: Downloader ──→ S3 (users/{name}/audio|video|pictures/)
                              │
                              │ S3 Event trigger
                              ▼
                         Lambda 3: Transcribe ──→ AWS Transcribe
                              │                        │
                              │                        ▼
                         Lambda: VAD ──→ audio_segments/{user}/{date}/
                                                       │
                                                       ▼
                                               S3 (transcripts/{name}/)
EventBridge (05:00 NZDT)                              │
    │                                                  │
    ▼                                                  │
Lambda 5: Report Generator ←───────────────────────────┘
    │
    ├── Anthropic Claude API (Sonnet 4.6) for AI summaries
    │
    ▼
S3 (reports/{date}/summary.json + by_user/{name}.json + .docx)

Frontend:
  CloudFront → S3 (fieldsight-web)
  Cognito (user auth) → API Gateway → Lambda: API
```

## Quick Start

### Prerequisites
- AWS account with CLI access
- [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html) installed
- RealPTT company account credentials

### Deploy (3 commands)

```bash
# 1. Build
sam build

# 2. First-time deploy (interactive, creates samconfig.toml)
sam deploy --guided
#   Stack name: fieldsight-pipeline
#   Region: ap-southeast-2  (Sydney, closest to NZ)
#   Fill in: RealPTTAccount, RealPTTPassword, BucketNameSuffix
#   Accept defaults for the rest

# 3. Future deploys (one command)
sam deploy
```

### Upload User Mapping

```bash
aws s3 cp config/user_mapping.json s3://fieldsight-data-YOURSUFFIX/config/user_mapping.json
```

### Test

```bash
aws lambda invoke --function-name fieldsight-orchestrator output.json
cat output.json
```

## Repository Structure

```
.
├── .github/workflows/
│   └── deploy.yml              # GitHub Actions: push-to-deploy
├── src/
│   ├── lambda_orchestrator.py       # Query files from RealPTT (web scraping)
│   ├── lambda_downloader.py         # Download single file → S3
│   ├── lambda_transcribe.py         # Trigger AWS Transcribe
│   ├── lambda_transcribe_callback.py # Process Transcribe completions
│   ├── lambda_report_generator.py   # AI-powered daily/weekly/monthly reports
│   ├── lambda_meeting_minutes.py    # AI-powered meeting minutes
│   ├── lambda_vad.py                # Voice Activity Detection
│   ├── lambda_fieldsight_api.py     # Frontend REST API
│   ├── transcript_utils.py          # Shared transcript normalization
│   └── fargate_downloader.py        # Fargate large file downloader
├── config/
│   ├── user_mapping.json            # Device → user name mapping
│   ├── prompt_templates.json        # Report generation prompts
│   └── prompt_templates_meeting.json # Meeting minutes prompts
├── frontend/
│   └── index.html                   # React frontend
├── scripts/
│   ├── scan-aws-naming.sh           # AWS resource naming audit
│   └── batch_convert_h264.py        # H265→H264 video conversion
├── docs/
│   ├── ARCHITECTURE.md              # System architecture diagram
│   └── MONITORING.md                # Monitoring & debugging guide
├── template.yaml                    # SAM template (all AWS resources)
├── CLAUDE.md                        # Claude Code project rules
├── ROADMAP.md                       # Product roadmap
└── README.md                        # This file
```

## Configuration

### SAM Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `RealPTTAccount` | - | RealPTT platform account |
| `RealPTTPassword` | - | RealPTT platform password |
| `BucketNameSuffix` | mycompany | S3 bucket suffix (final: fieldsight-data-xxx) |
| `StartDaysAgo` | 1 | How many days back to query |
| `DownloadAudio` | true | Download PTT radio recordings |
| `DownloadVideo` | true | Download PTT video calls |
| `DownloadFiles` | true | Download body cam & photos |
| `EnableTranscribe` | true | Auto-transcribe audio |
| `TimeDifferenceMs` | 46800000 | NZ timezone offset (13h) |
| `LanguageOptions` | en-NZ,en-AU,en-GB,en-US,zh-CN | Transcribe languages |
| `MaxSpeakers` | 5 | Speaker diarization max |
| `AlertEmail` | (empty) | Email for failure alerts |
| `ClaudeApiKey` | (empty) | Anthropic API key for report generation |

## AWS Resources

| # | Service | Name | Purpose |
|---|---------|------|---------|
| 1 | Lambda | fieldsight-orchestrator | Query RealPTT, trigger downloads |
| 2 | Lambda | fieldsight-downloader | Download files → S3 |
| 3 | Lambda | fieldsight-transcribe | Start Transcribe jobs |
| 4 | Lambda | fieldsight-fargate-trigger | Launch Fargate for large files |
| 5 | Lambda | fieldsight-report-generator | Daily/weekly/monthly reports |
| 6 | Lambda | fieldsight-vad | Voice activity detection |
| 7 | Lambda | fieldsight-api | Frontend REST API |
| 8 | Lambda | fieldsight-transcribe-callback | Transcribe job completions |
| 9 | Lambda | fieldsight-meeting-minutes | Meeting minutes generation |
| 10 | ECS Fargate | fieldsight-fargate-downloader | Large file downloads |
| 11 | S3 | fieldsight-data-{suffix} | All media, transcripts, reports |
| 12 | DynamoDB | fieldsight-items/reports/audit/users/transcripts | App data |
| 13 | API Gateway | fieldsight-api | HTTPS endpoint |
| 14 | CloudFront | fieldsight-web | Frontend CDN |
| 15 | Cognito | fieldsight-users | User authentication |

## Key Design Decisions

- **Domain**: Uses `realptt.com` (web interface), NOT `api.realptt.com` (JSON API returns empty for video/audio)
- **Video/Upload download**: Direct URLs from realptt.com, no session auth needed
- **Audio download**: `record.realptt.com/voice/?SpkId=...` — trailing slash required, no auth needed
- **Transcribe**: Auto language detection (en-NZ, en-AU, en-GB, en-US, zh-CN) with speaker diarization
- **Reports**: Claude Sonnet 4.6 generates structured JSON → optional Word document via python-docx Lambda Layer
- **Region**: ap-southeast-2 (Sydney) — closest to NZ, all resources colocated
