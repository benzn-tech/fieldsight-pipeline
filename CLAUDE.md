# CLAUDE.md — FieldSight Development Guide

## Project Overview

FieldSight (formerly SiteSync) is an AI-powered construction site documentation platform. Bodycam video, PTT audio, and photos are captured from field workers, processed through an AWS pipeline (VAD → Transcribe → AI report generation), and delivered as structured daily/weekly/monthly reports via a React frontend.

- **Frontend:** Single-file React (index.html) served via CloudFront
- **Backend:** Lambda functions + API Gateway + Cognito + DynamoDB
- **Region:** ap-southeast-2 (Sydney)
- **Bucket:** fieldsight-data-509194952652
- **Account:** 509194952652

**Product roadmap and feature tracking: see ROADMAP.md**

---

## Architecture

```
RealPTT Device → Lambda Orchestrator → S3 (raw media)
  → Lambda Transcribe → AWS Transcribe → S3 (transcript JSON)
  → Lambda Report Generator → Claude API → S3 (report JSON + Word)
  → Lambda Meeting Minutes → Claude API → S3 (minutes JSON + Word)
```

```
S3 Upload (video/audio)
  → VAD Lambda (fieldsight-vad)
    → Detect codec (skip H264 preview if already H264)
    → Extract audio → 16kHz WAV (numpy, NOT Python list)
    → Load Silero model from S3 (NOT Lambda Layer)
    → VAD: threshold 0.4 → retry 0.25 → fallback full audio
    → Upload segments to audio_segments/
  → Transcribe Lambda (fieldsight-transcribe, MUST be v1.3+)
    → Start Transcribe job with speaker diarization
    → Output to transcripts/{user}/{date}/ (WITH date subfolder)
  → Report Generator (fieldsight-report-generator)
    → Claude API → structured JSON report
    → Upload to reports/{date}/{user}/

Frontend (CloudFront → S3)
  → Cognito auth (user pool ap-southeast-2_ps7XIQGHB)
  → API Gateway → fieldsight-api Lambda
    → Role hierarchy: admin/gm > pm > site_manager > worker
    → Time regex: ALWAYS \d{4}-\d{2}-\d{2}_(\d{2})-(\d{2})-(\d{2})
    → Transcript search: date subfolder first, flat folder fallback
    → Video: web_video/ first (H264), users/video/ fallback
```

---

## Key Files

| File | Purpose |
|------|---------|
| `transcript_utils.py` | **Shared module** — unified time extraction, transcript normalization. MUST be bundled in every Lambda zip. |
| `lambda_report_generator.py` | Site inspection daily/weekly/monthly reports (v3.5) |
| `lambda_meeting_minutes.py` | Generic meeting minutes (v1.1) |
| `lambda_transcribe.py` | Triggers AWS Transcribe jobs on new audio |
| `prompt_templates.json` | Hot-swappable prompt templates (S3: config/) |
| `prompt_templates_meeting.json` | Meeting-specific prompt templates (S3: config/) |
| `user_mapping.json` | Device → person name + role + site mapping (S3: config/) |

---

## S3 Path Conventions

```
users/{display_name}/video/{date}/{device}_{date}_{time}.mp4                            ← Original recordings
users/{display_name}/audio/{date}/{device}_{date}_{time}.wav                            ← Original audio
audio_segments/{display_name}/{date}/{device}_{date}_{time}_off{start}_to{end}_src{fmt}.wav  ← VAD output
transcripts/{display_name}/{date}/{device}_{date}_{time}_off{start}_to{end}_src{fmt}.json    ← Transcribe output
web_video/{display_name}/{date}/{device}_{date}_{time}.mp4                              ← H264 preview (H265 only)
reports/{date}/{display_name}/daily_report.json                                         ← Generated reports
meeting_minutes/{date}/{title}.json                                                     ← Meeting minutes
config/user_mapping.json                                                                ← User/device mapping
config/prompt_templates.json                                                            ← Report generation prompts
models/silero_vad.onnx                                                                  ← VAD model (ALWAYS use this, not Layer)
```

---

## Key Environment Variables (VAD Lambda)

```
S3_BUCKET=fieldsight-data-509194952652
OUTPUT_PREFIX=audio_segments/
VAD_THRESHOLD=0.4
MERGE_GAP=2.0
MIN_SPEECH_DURATION=1.0
SAMPLE_RATE=16000
SKIP_EXISTING=true
GENERATE_PREVIEW=true
WEB_VIDEO_PREFIX=web_video/
SILERO_MODEL_S3_KEY=models/silero_vad.onnx
```

---

## CRITICAL BUGS — DO NOT REPEAT

These are real bugs encountered during development. Each caused production issues or wasted significant debugging time.

---

### VAD & Audio Processing

#### BUG-01: Filename Time Regex — ALWAYS skip the date part
**Bug**: `(\d{2})-(\d{2})-(\d{2})` on `Benl1_2026-02-09_09-56-40.mp4` matches `26-02-09` (date) NOT `09-56-40` (time).  
**Impact**: Video/audio/transcript files silently filtered out. Watch Video button never appeared. Transcripts showed empty.  
**Fix**: Always use `\d{4}-\d{2}-\d{2}_(\d{2})-(\d{2})-(\d{2})` — match the full `YYYY-MM-DD_` prefix first, then capture time.  
**Files affected**: `lambda_sitesync_api.py` (extract_time_seconds_from_filename, get_video_segments, get_audio_segments)
```python
# WRONG — matches date not time
re.search(r'(\d{2})-(\d{2})-(\d{2})', filename)

# CORRECT — anchored after date
re.search(r'\d{4}-\d{2}-\d{2}_(\d{2})-(\d{2})-(\d{2})', filename)
```

#### BUG-02: Silero VAD Model Version Mismatch
**Bug**: Lambda Layer contains silero_vad model v6.2.1 with different tensor format. Results in max VAD output = 0.058 (should be 0.5+), detecting 0 speech segments.  
**Impact**: All audio silently dropped — no transcripts, no reports.  
**Fix**: Always load model from S3 (`models/silero_vad.onnx`), NOT from Lambda Layer (`/opt/silero/`).  
**Required env var**: `SILERO_MODEL_S3_KEY=models/silero_vad.onnx`
```python
# WRONG — uses Layer model (wrong version)
session = ort.InferenceSession('/opt/silero/silero_vad.onnx')

# CORRECT — download from S3 first, fallback to Layer
s3_client.download_file(BUCKET, 'models/silero_vad.onnx', '/tmp/silero_vad.onnx')
session = ort.InferenceSession('/tmp/silero_vad.onnx')
```

#### BUG-03: VAD sr Parameter Shape
**Bug**: `np.array([sample_rate])` creates shape `(1,)` array. Silero expects scalar shape `()`.  
**Impact**: VAD produces near-zero probabilities on all audio.
```python
# WRONG — shape (1,)
sr = np.array([sample_rate], dtype=np.int64)

# CORRECT — shape ()
sr = np.array(sample_rate, dtype=np.int64)
```

#### BUG-04: Python List OOM for Large Audio
**Bug**: `read_wav_pcm()` reads all samples into a Python list. Each Python float = 28 bytes. A 2-hour WAV = 118M samples × 28 = 3.3 GB → Lambda 3008 MB OOM.  
**Impact**: Lambda crashes with `Runtime.OutOfMemory` on any audio > ~90 minutes.
```python
# WRONG — 28 bytes per sample
samples = struct.unpack(f'<{n}h', raw)
return [s / 32768.0 for s in samples], sr

# CORRECT — 4 bytes per sample
samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
return samples, sr
```

#### BUG-05: `if not numpy_array` — Ambiguous Truthiness
**Bug**: `if not segment_samples:` throws `ValueError: The truth value of an array with more than one element is ambiguous`.  
**Impact**: VAD crashes after successfully detecting segments.
```python
# WRONG
if not segment_samples:

# CORRECT
if len(segment_samples) == 0:
```

#### BUG-06: write_wav_segment Per-Sample Loop
**Bug**: Writing 118M samples with individual `struct.pack('<h', ...)` calls takes forever and uses excessive memory.
```python
# WRONG — O(n) Python loop
for s in samples:
    f.write(struct.pack('<h', int(s * 32767)))

# CORRECT — single numpy op
f.write((np.clip(arr, -1, 1) * 32767).astype(np.int16).tobytes())
```

#### BUG-07: VAD 0-Segments Silent Drop
**Bug**: When VAD detects 0 speech segments, audio is silently discarded. No Transcribe job created.  
**Impact**: Recordings with background noise (but valid speech) produce no output.  
**Fix**: Two-tier retry (threshold 0.4 → 0.25), then fallback to sending entire audio to Transcribe.

#### BUG-08: H264 Video Unnecessarily Re-encoded
**Bug**: VAD Lambda generates H264 720p preview for ALL videos, including ones already in H264 720p.  
**Impact**: Wastes 2.5 min Lambda time + 32 MB S3 storage per file.
```python
if codec_info.get('browser_playable') and codec_info.get('video_codec') == 'h264':
    preview_key = key  # Use original
else:
    # Generate H264 preview only for H265/other codecs
```

---

### Transcribe & Pipeline

#### BUG-09: Transcript Time Extraction is TWO Layers, Not One
**Bug**: Reports showed `12:18 – 12:18` for every topic in a 2-hour meeting.  
**Root cause**: Code only extracted the base timestamp from the filename and ignored both (a) VAD offsets in the filename and (b) per-word timestamps inside the Transcribe JSON.  
**Rule**: Absolute time for any word = `base_time_from_filename + vad_offset_from_filename + word.start_time_from_json`
```
Filename: Benl1_2026-03-20_12-18-34_off1465.8_to1729.8_srcwav.json
  base_time = 12:18:34
  vad_offset = 1465.8s
  segment_base = 12:18:34 + 1465.8s = 12:42:59
  word[0].start_time = 0.079s → absolute = 12:42:59

Full audio (no offset): Benl1_2026-03-20_12-18-34.json
  base_time = 12:18:34, no offset
  word[500].start_time = 3600s → absolute = 13:18:34
```
**Always use `transcript_utils.normalize_transcript()`** — never write inline timestamp parsing.

#### BUG-10: Transcribe JSON Per-Word Data — Don't Throw It Away
**Bug**: `parse_transcript()` originally only returned `full_text` (one flat string) and discarded the `items[]` array with per-word `start_time`, `end_time`, `speaker_label`.  
**Rule**: When building prompts, each speaker turn must carry its own absolute timestamp range. Use `format_turns_for_prompt(normalized, use_absolute_time=True)` for meetings, `use_absolute_time=False` for site reports.

#### BUG-11: Transcribe Output Filename Encodes Critical Metadata
```
Standard:    Benl1_2026-03-20_12-18-34.json
VAD segment: Benl1_2026-03-20_12-18-34_off1465.8_to1729.8_srcwav.json
```
Fields:
- `Benl1` — device account
- `2026-03-20_12-18-34` — recording session start (base time)
- `off1465.8` — segment starts at base + 1465.8 seconds
- `to1729.8` — segment ends at base + 1729.8 seconds
- `srcwav` — source format (wav/mp4/etc.)

**Never assume filenames are simple.** Always use `transcript_utils.extract_*` functions.

#### BUG-12: Transcribe Lambda Flat Folder — Missing Date Subfolder
**Bug**: `lambda_transcribe.py` v1.1 writes to `transcripts/{user}/{file}.json` (flat). v1.3 writes to `transcripts/{user}/{date}/{file}.json`.  
**Impact**: API can't find transcripts. Frontend shows "No transcript found".  
**Fix**: Correct output path: `transcripts/{user}/{date}/{file}.json`. API also needs flat folder fallback search.
```python
# v1.3 CORRECT
file_date = extract_date_from_key(key)
output_key = f"{OUTPUT_PREFIX}{display_name}/{file_date}/{base_name}.json"
```

#### BUG-13: S3 Event Triggers Can Fire on Your Own Output
**Rule**: Lambda (Transcribe) triggers on `users/*/audio/*`. If you write output to a path matching this prefix, it triggers an infinite loop. Always verify S3 event filter prefixes don't overlap with output paths.

#### BUG-14: AWS CLI List Parameters are Space-Separated, Not Comma-Separated
**Bug**: `--language-options "en-NZ,en-AU,en-GB"` → ValidationError.  
**Rule**:
```bash
# WRONG
--language-options "en-NZ,en-AU,en-GB,en-US,zh-CN"

# RIGHT
--language-options en-NZ en-AU en-GB en-US zh-CN
```
This applies to all AWS CLI list-type parameters.

---

### Report Generation

#### BUG-15: Prompt Text Truncation MUST Match Expected Input Size
**Bug**: `transcripts_text[:20000]` truncated a 2-hour meeting (105K chars) to only 19% — report covered 12:18–12:41 and missed the remaining 80 minutes.  
**Rule**:
- Meeting minutes: `[:120000]` (120K chars ≈ 30K tokens, fits in 200K context)
- Site daily report: `[:60000]` (site walks are shorter but can still be long)
- Weekly/monthly summaries: `[:15000]` is fine (these summarise already-processed reports, not raw transcripts)

#### BUG-16: max_tokens Must Scale with Input Length
**Bug**: `max_tokens=6000` was hardcoded. A 2-hour meeting with 15 topics needs 10K+ output tokens.  
**Rule**: Calculate dynamically:
```python
# Meeting minutes
prompt_tokens_est = len(prompt) // 4
max_tokens = min(max(8000, prompt_tokens_est // 2), 16000)

# Site report
max_tokens = min(4096 + n_transcripts * 350, 16000)
```

#### BUG-17: User-Provided Attendee Names Must Override Device Mapping
**Bug**: `user_mapping.json` mapped `Benl1 → Jarley Trainor`. When user passed `attendees: ["Ben", "Sam"]`, the report still showed "Jarley Trainor" because speaker labels were resolved through user_mapping before the prompt was built.  
**Rule**: When `attendees` is explicitly provided in the event payload:
- Transcript lines use device ID only (e.g., `Benl1`), NOT the mapped name
- Prompt includes explicit instruction: "Use ONLY names from the Attendees list"
- Speaker diarization labels (spk_0, spk_1) are left for Claude to map to attendees based on conversation context

#### BUG-18: Meeting/Site Report Mutual Exclusion via Manifest
**Bug**: Same transcripts processed by both meeting minutes AND site report generator — content duplicated.  
**Rule**: Meeting minutes Lambda writes `.meeting_manifest.json` listing consumed transcript S3 keys. Report generator reads this manifest and skips those keys. Always run meeting minutes FIRST.
```
reports/2026-03-20/Jarley_Trainor/
  ├── daily_report.json          ← site walk only
  ├── meeting_minutes.json       ← meeting only
  └── .meeting_manifest.json     ← exclusion marker
```

---

### Frontend & JavaScript

#### BUG-19: JavaScript Date Timezone Bug (NZ)
**Bug**: `new Date("2026-03-09T12:00:00")` creates local time in NZ (UTC+13). `.toISOString()` converts to UTC, shifting the date back one day. Calendar navigation completely broken for NZ users.
```javascript
// WRONG — timezone conversion shifts date
const d = new Date(selectedDate + "T12:00:00");
d.setDate(d.getDate() + 1);
return d.toISOString().slice(0, 10); // Wrong date in NZ!

// CORRECT — UTC arithmetic
const [y,m,dy] = selectedDate.split('-').map(Number);
const n = new Date(Date.UTC(y, m-1, dy+1));
return n.toISOString().slice(0, 10);
```

#### BUG-20: CloudFront 404 → HTML → JSON Parse Error
**Bug**: API returns 404, CloudFront SPA config converts 404 to `index.html` (200). Frontend does `res.json()` on HTML → `Unexpected token '<'`.  
**Impact**: Calendar navigation to dates without reports crashes the app.
```javascript
// CORRECT
const ct = res.headers.get("content-type") || "";
if (!ct.includes("application/json")) {
  return { _notFound: true }; // Graceful handling
}
```

#### BUG-21: React useRef Doesn't Trigger Re-render
**Bug**: Audio play/pause button checks `audioRef.current.paused` — ref changes don't trigger re-render, button symbol stays stuck.
```javascript
// WRONG — ref doesn't trigger re-render
{audioRef.current?.paused ? "▶" : "⏸"}

// CORRECT — state-driven
const [isPlaying, setIsPlaying] = useState(false);
audio.onplay = () => setIsPlaying(true);
audio.onpause = () => setIsPlaying(false);
{isPlaying ? <PauseIcon/> : <PlayIcon/>}
```

---

### Infrastructure & Deployment

#### BUG-22: Lambda Deployment Version Mismatch (RECURRING)
**Bug**: Code in project files ≠ code deployed to Lambda. Multiple bugs traced to deploying old/wrong versions.  
**Impact**: Fixes appear to not work. Time wasted debugging non-issues.  
**Prevention**: Always verify deployed version after update:
```bash
# Check what's actually running
aws lambda get-function --function-name FUNCTION_NAME \
  --query "Code.Location" --output text --region ap-southeast-2 | \
  xargs curl -sL -o /tmp/check.zip && \
  unzip -p /tmp/check.zip lambda_*.py | head -10
```

#### BUG-23: f-strings with Backslashes Fail on Python < 3.12
**Bug**: `f"...{re.findall(r'v3\.\d', content)}"` → SyntaxError on CloudShell (Python 3.9/3.11).  
**Rule**: Lambda runtime may be 3.12, but scripts run on CloudShell which may be older.
```python
# WRONG (fails on <3.12)
print(f"Found: {re.findall(r'v3\.\d', content)}")

# RIGHT
versions = re.findall(r'v3\.\d', content)
print(f"Found: {versions}")
```

#### BUG-24: python-docx Lambda Layer Must Match Runtime Architecture
**Rule**: The `lxml` binary in the Layer must be compiled for `cpython-3xx-x86_64-linux-gnu`. If the Layer is missing or incompatible, Word generation silently disables (JSON reports still generate). Check the startup log for `"Word generation: enabled"` vs `"DISABLED"`.

#### BUG-25: Site Manager Permission Leak
**Bug**: `get_accessible_users()` returned ALL users on same site, including other site_managers.  
**Impact**: Site manager could see other site managers' recordings and reports.
```python
# WRONG
result = [u for u in all_users if any(s in accessible_sites for s in u['sites'])]

# CORRECT
result = [u for u in all_users
          if (u['name'] == own_name) or
             (u['role'] == 'worker' and any(s in accessible_sites for s in u['sites']))]
```

#### BUG-26: MERGE_GAP Environment Variable Wrong Value
**Bug**: `MERGE_GAP=10` set in Lambda env (should be 2.0). Merges segments up to 10 seconds apart.  
**Impact**: Over-merged audio segments, losing silence boundaries between topics.  
**Fix**: Verify all env vars after deployment, not just the code.

---

## Lambda Deployment

**CRITICAL: Always bundle transcript_utils.py in every Lambda zip.**

```bash
# Report generator
zip -j rg.zip lambda_report_generator.py transcript_utils.py
aws lambda update-function-code --function-name fieldsight-report-generator --zip-file fileb://rg.zip

# Meeting minutes
zip -j mm.zip lambda_meeting_minutes.py transcript_utils.py
aws lambda update-function-code --function-name fieldsight-meeting-minutes --zip-file fileb://mm.zip
```

Always `aws lambda wait function-updated --function-name <name>` before invoking.

---

## Deployment Checklist

Before deploying ANY Lambda:
1. [ ] Verify the file you're uploading is the correct version (check header/version string)
2. [ ] After deploy, verify deployed code matches local: `head -5` check
3. [ ] Check all environment variables are correct (especially MERGE_GAP, SILERO_MODEL_S3_KEY)
4. [ ] Test with a single file trigger before batch processing
5. [ ] Check CloudWatch logs within 2 minutes for errors

```bash
# Single file re-trigger pattern
aws s3 cp s3://BUCKET/KEY s3://BUCKET/KEY --metadata-directive REPLACE --region ap-southeast-2
```

---

## Code Style & Conventions

- **Model default:** `claude-sonnet-4-6` (update if newer model available)
- **Timezone:** All internal times are UTC. Display times are NZDT (UTC+13). Use `get_nzdt_now()`.
- **S3 paths:** `reports/{date}/{user}/daily_report.json`, `meeting_minutes/{date}/{title}.json`
- **DynamoDB:** Controlled by `ENABLE_DYNAMODB` env var. Currently OFF in production.
- **Prompt templates:** Hot-swappable from S3 (`config/prompt_templates.json`). Lambda falls back to inline defaults if S3 template missing.
- **Debug records:** Every Claude API call saves prompt + response to `*_debug.json` alongside the report. Use these for prompt tuning.
- **Version strings:** Update in docstring header, `_report_metadata.version`, and logger startup message.

---

## Testing

```bash
# Test meeting minutes
aws lambda invoke --function-name fieldsight-meeting-minutes \
  --payload '{"date":"2026-03-20","meeting_title":"Test","attendees":["Ben","Sam"],"user":"Jarley_Trainor"}' \
  --cli-binary-format raw-in-base64-out /tmp/test.json

# Test report generator (force regenerate, skip backfill)
aws lambda invoke --function-name fieldsight-report-generator \
  --payload '{"report_type":"daily","date":"2026-03-20","force":true,"skip_backfill":true}' \
  --cli-binary-format raw-in-base64-out /tmp/test.json

# Generate report for specific user/date
aws lambda invoke --function-name fieldsight-report-generator \
  --payload '{"report_type":"daily","date":"2026-03-20","force":true,"users_filter":["Jack Gibson"]}' \
  /dev/stdout --region ap-southeast-2

# Check output
cat /tmp/test.json | python3 -m json.tool
aws s3 ls s3://fieldsight-data-509194952652/reports/2026-03-20/
```

---

## Common Debugging Commands

```bash
# Re-trigger single file VAD
aws s3 cp s3://BUCKET/KEY s3://BUCKET/KEY --metadata-directive REPLACE --region ap-southeast-2

# Tail Lambda logs
aws logs tail /aws/lambda/FUNCTION_NAME --since 5m --follow --region ap-southeast-2

# Check deployed code version
aws lambda get-function-configuration --function-name NAME \
  --query "[Environment.Variables, LastModified]" --output json --region ap-southeast-2

# Download and inspect currently deployed code
aws lambda get-function --function-name <name> --query 'Code.Location' --output text | xargs curl -sL -o /tmp/current.zip
unzip -l /tmp/current.zip  # check contents

# Create Cognito user
aws cognito-idp admin-create-user --user-pool-id ap-southeast-2_ps7XIQGHB \
  --username "email@domain.com" --user-attributes Name=email,Value="email@domain.com" \
  Name=email_verified,Value=true Name=name,Value="Full Name" \
  --temporary-password "FieldSight2026!" --region ap-southeast-2
```

---

## Windows Git Bash + AWS CLI 部署注意事项

本项目在 Windows 11 + Git Bash 环境下操作 AWS CLI，有以下已验证的陷阱：

### BUG-27: `fileb://` 路径必须用 Windows 格式
```bash
# WRONG — Git Bash /tmp 映射不被 AWS CLI 识别
aws lambda create-function --zip-file fileb:///tmp/code.zip

# CORRECT — 用 cygpath 转换
aws lambda create-function --zip-file "fileb://$(cygpath -w /tmp/code.zip)"
```

### BUG-28: MSYS 路径转换破坏 API 参数
```bash
# WRONG — /name 被转成 C:/Program Files/Git/name
aws apigateway update-rest-api --patch-operations op=replace,path=/name,value=x

# CORRECT — 禁用路径转换
export MSYS_NO_PATHCONV=1
aws apigateway update-rest-api --patch-operations op=replace,path=/name,value=x
```

### BUG-29: python3 是 Windows Store 占位符 (exit 49)
本机无 Python 安装。`python3` 返回 exit 49。所有 JSON 处理使用 Node.js：
```bash
# 用 node 替代 python 做 JSON 处理
echo "$JSON" | node -e "let d='';process.stdin.on('data',c=>d+=c);process.stdin.on('end',()=>console.log(JSON.parse(d).key))"
```

### BUG-30: Node.js 的 /tmp 路径与 Git Bash 不同
Node.js `fs.readFileSync('/tmp/x')` 解析为 `C:\tmp\x`（不是 Git Bash 的 temp）。
**Fix**: 用 stdin/stdout 管道传数据，不用文件路径。

### BUG-31: eval 吃掉 Windows 路径反斜杠
```bash
# WRONG — eval 二次解析消灭反斜杠
CMD="aws lambda create-function --zip-file fileb://$(cygpath -w /tmp/x.zip)"
eval "$CMD"

# CORRECT — 直接执行，不用 eval
aws lambda create-function --zip-file "fileb://$(cygpath -w /tmp/x.zip)"
```

### BUG-32: EventBridge Scheduler 自定义 Group
Scheduler 可能不在 default group。`get-schedule` 返回空不代表不存在。
```bash
# 先查 group
aws scheduler list-schedules --query 'Schedules[].{Name:Name,Group:GroupName}'
# 再指定 group
aws scheduler get-schedule --name X --group-name sitesync
```

### BUG-33: SAM S3 Event 不支持外部 Bucket
SAM `Events.S3.Bucket` 必须 `!Ref` 同 template 内的 `AWS::S3::Bucket`。外部 bucket 需手动配置：
```bash
aws s3api put-bucket-notification-configuration --bucket BUCKET --notification-configuration '{...}'
```

### BUG-34: SAM deploy 无法创建已存在资源
已在 stack 外存在的 S3/DynamoDB 会导致 deploy 失败。改为 Parameter 引用：
```yaml
# WRONG — 资源已存在会冲突
StorageBucket:
  Type: AWS::S3::Bucket
  Properties:
    BucketName: fieldsight-data-xxx

# CORRECT — 参数引用外部资源
Parameters:
  DataBucketName:
    Type: String
    Default: fieldsight-data-509194952652
```