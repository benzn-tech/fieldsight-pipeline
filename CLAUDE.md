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
  → Cognito auth (user pool ap-southeast-2_q88pd6XXr) (fieldsight-users; old pool ps7XIQGHB deleted)
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

### Run the SQL against a real database before trusting it

The unit suite drives connection doubles (`FakeConn`, `FakeProgrammeStore`).
They prove the handler's logic and **nothing about the SQL underneath it** —
they do not enforce foreign keys, NULL ordering, or cast semantics. Every
entry below is a real defect that the full suite passed straight through.

- **`ON DELETE CASCADE` defeats a scoped `DELETE`.** `programme_tasks.parent_id`
  cascades, so `DELETE ... WHERE origin='imported'` also removes the *local*
  children hanging off those rows. A fix written to **preserve** local rows
  would have deleted them; it needs a `SET parent_id = NULL` detach first.
  **1598 unit tests passed both before and after that fix.**
- **`ORDER BY (col = %s) DESC` needs `NULLS LAST`.** Postgres sorts NULLs
  first under `DESC`, so a row whose comparison is NULL beats an exact match.
  Verified by running the query with and without the clause.
- **`= NULL` is never true**, which is the wanted behaviour for an
  unattributed row — but pin it, because the alternative reading ("belongs to
  everyone") would leak it to every user on the site.
- **A `return error(...)` does not roll back.** Only raising does. Use the
  `conn.transaction()` + custom-exception pattern (see the action-item PATCH
  and `confirm_suggestion`) when a partial write must unwind.

The test cluster is VPC-private, so `TEST_DATABASE_URL` is usually
unavailable locally. Drive assertions through the **RDS Data API** instead,
inside one transaction that is rolled back:

```bash
CL=arn:aws:rds:ap-southeast-2:509194952652:cluster:fieldsight-db-test-dbcluster-hywiixu8ihi9
SEC=arn:aws:secretsmanager:ap-southeast-2:509194952652:secret:'rds!cluster-...'
TX=$(aws rds-data begin-transaction --resource-arn "$CL" --secret-arn "$SEC" \
      --database fieldsight_test --query transactionId --output text)
# ... execute-statement with --transaction-id "$TX" ...
aws rds-data rollback-transaction --resource-arn "$CL" --secret-arn "$SEC" \
  --transaction-id "$TX"
```

Commit the same cases as `tests/integration/*.py` so CI covers them where
`TEST_DATABASE_URL` does exist; they skip cleanly without it. Examples:
`test_programme_task_doc_id.py`, `test_programme_suggestions_author_scope.py`,
`test_programme_list_local.py`.

**Set `MSYS_NO_PATHCONV=1`** for any AWS CLI call carrying a `/`-prefixed
argument (`/aws/lambda/...`, `/api/org/...`) — Git Bash rewrites it into a
Windows path and the call fails in a way that reads like a routing bug.

### Two habits worth more than more tests

- **An assumption written in a docstring and not enforced will be violated.**
  `put_programme` said *"the client confirms before calling it"*; nothing
  checked, and the ordinary Save button silently converted every local row to
  imported. Enforce it in the repository or the handler, not in a comment.
- **Re-check your own claims.** The most expensive defect of that session was
  the phrase *"now redundant but harmless"* in a commit message about a
  guard. It was not harmless — it refused every save, which made the fix
  behind it unreachable.

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

# Create Cognito user (pool: fieldsight-users; old pool ps7XIQGHB deleted)
aws cognito-idp admin-create-user --user-pool-id ap-southeast-2_q88pd6XXr \
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
### BUG-35: 中文 Windows 下 AWS CLI 用 GBK 读文件
`aws cloudformation deploy --template-file ...` 在中文 locale 下用 GBK 解码模板,遇 UTF-8 字符即
`'gbk' codec can't decode byte ...`。**Fix**: 命令前 `export AWS_CLI_FILE_ENCODING=UTF-8 PYTHONUTF8=1`。

### BUG-36: VPC Lambda 无出口时调用 AWS API 会静默挂死
无 NAT/无 VPC endpoint 的 VPC Lambda,任何 AWS API 调用(如 Secrets Manager)都会黑洞直到超时,
**日志零输出**(看起来像函数本身挂了)。**Fix**: 凭据在部署时经 CloudFormation 动态引用注入
(`PGPASSWORD: !Sub '{{resolve:secretsmanager:${DbSecretArn}:SecretString:password}}'`,ARN 走
Parameter 而非 ImportValue);或为所需服务加 VPC interface endpoint。运行时零外呼是首选。

**2026-07-31 复发实例(QR redeem 504)**:v2 把 QR create/redeem 放进 **in-VPC 的 `lambda_org_api`**(v1 的
触发器是非-VPC),而 VPC `vpc-0791974a474386d1c` **只有 S3 网关端点、没有 DynamoDB 端点、且无 NAT** →
每次 DynamoDB 调用黑洞 → 29s 挂死 → API GW 504。单测 mock 掉 DynamoDB 所以测不出,只有真部署 + curl 才暴露。
**已修(out-of-band,非 IaC)**:`aws ec2 create-vpc-endpoint --vpc-endpoint-type Gateway --service-name
com.amazonaws.ap-southeast-2.dynamodb --vpc-id vpc-0791974a474386d1c --route-table-ids rtb-0f167c1fa3469bafd`
→ `vpce-01233d5b756ffefcb`。org-api 的 3 个子网都用**主路由表** `rtb-0f167c1fa3469bafd`(S3 端点也在这张表),
test/prod **共用这个 VPC**,故一个端点同时修好两边。**⚠️ 这个端点不在任何 CFN 模板里**——直接往
DbStack 加同名资源会 `RouteAlreadyExists` 冲突,要纳管须走 `cloudformation import`(或先删端点再由 CFN 建,
会有短暂中断)。**通则**:任何 in-VPC lambda 新调一个 AWS 服务前,先确认该服务有 VPC 端点。

### BUG-37: Lambda 里 `datetime.now()` 是 UTC —— 拿去和 NZ 日期比会偏一天
Lambda 运行时时区是 UTC,`datetime.now()`(naive)= UTC。RealPTT 录音按 **NZ 客户端时间**记日期。
orchestrator 用 `end_date = datetime.now()` 算查询窗口(`lambda_orchestrator.py`),NZ 上午
(UTC 还是前一天)时窗口末端锚在 UTC 昨天,**漏掉今天 NZ 的录音**,要拖到 UTC 翻天(≈NZ 中午)才拉。
2026-07-16 实证:NZ 10:49(UTC 07-15 22:49)查询窗口是 `[07-14,07-15]`,今早录音全在窗外。
**Fix**(`compute_query_range`,已上线 PR #70):`datetime.now(timezone.utc) + timedelta(ms=time_difference_ms)`
先转 NZ 再取日期。**通则:后端任何"今天"的日期计算都要显式转 NZ,别用裸 `datetime.now()`**
(前端同类是 BUG-19)。同类可疑点:自研 app 的日期文件夹也偏了一天(见
`docs/superpowers/specs/2026-07-16-grandtime-app-prod-integration.md` G3)。

### BUG-38: test 与 prod org-api 共享 Aurora,只差 S3 桶 + GRADED_ROLES
`deploy.yml`(fieldsight-test)与 `deploy-prod.yml`(fieldsight-prod)给 `OrgApiFunction` 传的
`DbStackName=fieldsight-db-test`、`DbSecretArn`、`OrgUserPoolId` **两边完全相同** —— test/prod org-api
读写的是**同一个 Aurora + 同一个 Cognito 池**,只有 S3 桶(`fieldsight-data-test` vs prod 湖 `fieldsight-data`)
和 `GRADED_ROLES` 不同。两个含义:
1. **无数据隔离**:test org-api 的写会落到 prod 库行。dev 因 `FS_WRITEMOCKS=true` 写全 mock 才安全;
   想在 dev 测真实写必须另起一套 Aurora(大工程),不能靠"指向 test org-api"隔离。
2. **`GRADED_ROLES` 由 repo 变量驱动**:prod 有 `PROD_GRADED_ROLES=true`,**test 原本没有 `TEST_GRADED_ROLES`
   → `${{ vars.TEST_GRADED_ROLES || 'false' }}` = false**(2026-07-21 已补 `TEST_GRADED_ROLES=true`)。
   **graded-off 不对称坑**:platform_admin 的 `list_members`(直接走 `is_cross_company`)正常返回全公司成员,
   但 `list_org_sites`(走 `_allowed_site_ids`,跨公司仅 graded 开时生效)返回 `[]` → 站点列表/选择器空。
   member 通、site 空 = 一定是 GRADED_ROLES,不是代码/数据(develop==main 时更要先怀疑环境变量)。

dev 可把 Amplify `FS_ORG_BASEURL` 指向 test 网关(`wdsgobb7b0…/prod/api`,prod 是 `ys94qy2tk0`)在 dev 上验
新端点、免先上 prod —— test 网关的 CognitoAuthorizer 已信任 prod 池 token。`update-branch --environment-variables`
是**整包替换**(须带全部 6 个 FS_* 变量),改完 `start-job RELEASE` 重建。

**DB 隔离已上线(2026-07-21,PR #114)**:test 栈通过 `PgDatabase` 参数指向同一集群上**另一个独立数据库**
`fieldsight_test`,prod 仍留在 `fieldsight`——这个拆分是**故意的**,不要再当成 bug 去"修"。机制:template
的 12 处 `PGDATABASE` 走 `!If [HasPgDatabaseOverride, !Ref PgDatabase, !ImportValue …]`;
`PgDatabase=fieldsight_test` 只加在 **`deploy.yml` 的 `--parameter-overrides`**(不是 samconfig——CLI 覆盖会
整体取代 samconfig),`deploy-prod.yml` 不加故 prod 留默认。**含义**:test 与 prod **数据/schema 物理隔离**
(test 随便改/删/破坏性 migration 都碰不到 prod 客户数据);Cognito 仍共享。**但合进 `main` 的 migration 仍会
自动跑 prod**(deploy-prod.yml 夜跑),所以试验性 migration 别进 main。回滚:去掉 deploy.yml 那行重部署→
`!If` 回落 import→test 复用 `fieldsight`;再 `DROP DATABASE fieldsight_test`。详见
`docs/superpowers/specs/2026-07-21-test-prod-db-isolation-design.md` + runbook `scripts/db-isolation-bootstrap.md`。

### BUG-39: authority-flip 让新 chunk 的 topic_id 全 NULL → 中面板 Search 恒空(2026-07-17 起回归)
**现象**:prod 客户站 Search 对任何词返回 0(即便向量命中);Ask agent 400/403。**非**鉴权/嵌入/索引问题。
**Search 根因**:`report_chunks.topic_id` **从 2026-07-17 起新建的全为 NULL**(按 created_at 分组实证:07-16 前有、07-17/18/22/23/30 全 0)。`lambda_ask_agent._aggregate_topics` 明确"**只保留有 topic_id 的 chunk,丢弃无 topic 的转写窗口**"(注释 user pref 2026-07-10)→ 无主 chunk 被全丢 → count:0。
**触发**:**AUTHORITY_FLIP**(#64/#67,`PROD_AUTHORITY_FLIP`)+ **G5b #71**。authority-flip 让"有 extraction topics 的日子夜间 ingest 不建报告 topic";但 RAG chunk 从**报告** topics 切(`chunking.py` 读 `report.topics`),`report_chunks.topic_id`(UUID) 靠 **ingest 把报告 topic 匹配到 Aurora topic** 得来——报告 topic 被 defer → 匹配不到 → topic_id 落 NULL。即**真实数据的 topic 搬到 extraction 侧,RAG 索引链仍绑报告侧 topic**。
**检索本身没坏**(排除误判):库有 254×1024 维向量;自相似 dist 0.0;`text-embedding-v4` 嵌 "recording" 距 UC PK chunk 仅 0.357<0.55;**直调 `fieldsight-prod-rag-search` 用正确 sub → 返 8 条**(site_count:1)。
**Ask 根因(另一回事)**:`/api/search`、`/api/ask` 经 `/api/{proxy+}` → 遗留报表 Lambda `fieldsight-prod-api`(dashboard 走 `/api/org/{proxy+}` → org-api 故正常)。report API `get_caller_identity` 用遗留 DynamoDB `fieldsight-users`(仅4行)→ org 账号 Ben_UCPK 不在 → 带 user 时 **403 "Access denied to this user"**、全局 Ask 不带 user 时 **400 "Missing user"**。改用 org 新账号(UC PK 队列)才暴露;老 platform_admin 账号不会 403。参见 BUG-25 类身份坑 + memory `fieldsight-legacy-gateway-identity-403`。
**次要**:site-scoped 搜索前端发 `site`=站点 UUID,`lambda_rag_search.py:79` 当 **slug** 查 → 11 站里 7 个 slug=NULL(含 UC PK)→ 站点范围空短路 0。
**修法**:①让 authority-flip 天的 chunk 关联 extraction/Aurora topic(或 ingest 用 extraction topic 回填 `report_chunks.topic_id`,并重建存量 07-17 起 NULL-topic chunk);②`/search`+`/ask` 改用 org-api 的 sub→Aurora 身份或迁到 org-api;③前端发 slug 或后端接受 UUID + 回填 slug。**排查**:prod/test 共用集群 `fieldsight-db-test-dbcluster-hywiixu8ihi9` Data API 已开,`aws rds-data execute-statement --database fieldsight` 直查;可直调 rag-search/ask-agent 复现。详见 memory `fieldsight-search-ask-regression`。
### BUG-40: DynamoDB 保留字 `consumed` 让 QR redeem 每次静默 401(2026-07-31)
**现象**:三端全上 prod 后,每次扫码都提示 "Invalid or expired QR code",web `create` 明明 201、码明明在表里
未过期未消费。**根因**:redeem 的原子单次消费
`update_item(UpdateExpression="SET consumed = :t", ConditionExpression="consumed = :f")` —— `consumed` 是
**DynamoDB 保留字**,每次都抛 `ValidationException`,而那个 `except` **静默吞掉**(不打日志)→ 一律回退成通用 401。
**误导点**:`get_item` 其实一直命中(靠临时 prod 调试日志 `QRDBG ... item=Y` 才看出来);create 的 `put_item`
和限流器的 `hits/expiresAt` 都不碰保留字所以正常,让人误判成"读失败"。**单测没抓到**:测试桩
`FakeQrTable` 不校验保留字。**修**:`ExpressionAttributeNames={"#c": "consumed"}` + 表达式用 `#c`(PR #180);
同时给那个静默 except 加 `logger.exception`,并让测试桩模拟 DynamoDB 拒绝未别名化的保留字(防回归)。
**通则**:①DynamoDB 表达式里任何普通英文词都先查保留字表,一律用 `#别名`;②**永远别写静默的
`except: return 通用错误`** —— 服务端至少 `logger.exception`,否则这类 bug 无从下手。

### BUG-41: 报告的 `site` 来自 `SITE_NAME` env 兜底 → chunk 站点错归属(2026-08-02)
**现象**:某用户在 RAG 搜索里**搜不到自己的内容**,而别站的人反倒搜得到;同时 defer 天的 topic 匹配恒 0。
**根因链**:`lambda_report_generator` 对**不在遗留 `config/user_mapping.json` 里的用户**(= 所有注册制新账号),
把 `report['site']` 填成 **env `SITE_NAME`**(prod 值 `SB1108 Ellesmere College`):`:1245` `user_primary_site.get(user_name,'')`
→ `''` → `:1247` `.get('name', site_name)` 回退 → `:1182` `os.environ['SITE_NAME']`。`lambda_ingest.resolve_site`
路径①按名字查站,**忠实采信这个错站名** → chunk 的 `site_id` 全错。而 `lambda_item_writer` 走 G5b
`recordings.site_id`(App 内选站)→ 同一 user+date 的 extraction topic 站点是**对的**。两侧不一致的后果:
①RAG 按 `site_id` 过滤 → 本人看不见自己的内容、他站的人看得见(**可见性错误**);②defer 天 topic 匹配器按
`(site_id,user_id,report_date)` 取候选 → **恒空** → `topic_id` 全 NULL(实测 6/54)。prod 实证 40 条中招。
**Fix(已上线 PR #196/#197)**:ingest 与 item_writer **对齐优先级**——新增
`recordings.site_for_day(conn,company_id,user_folder,date)`(`site_for_media` 的日级兄弟,同款租户安全:
company 双重限定 + `site_id IS NOT NULL` + `_escape_like`;一天跨站按**录音条数取多数**、同数取最新),
调用点改 `site_for_day(...) or resolve_site(...)`。**`resolve_site` 本身不动**——`item_writer` 拿它当自己第三级。
重跑后 topic_id 6→21、站点归属不再被重跑撤销。
**残留(设计正确,勿"修")**:若 `recordings.company_id` 与 ingest 解析出的公司不同(遗留数据:早期录音还挂在
`dc2eafa9` FieldSight 名下),跨租户守卫会正确拒绝 → 回落老逻辑 → 仍可能错站。**放宽 company 限定 = 租户隔离
回归,绝不做**;正解是迁移那批 recordings 的 `company_id`。
**通则**:①任何"站点/归属"判定都以 **App 端 `recordings.site_id`** 为权威,`user_mapping.json` 和 env 兜底只配
当最后一级;②env 级默认值(`SITE_NAME`)用于**多租户归属**是危险设计——它把"查不到"静默变成"归到某个具体客户"。

### BUG-42: Git Bash 的 MSYS 路径改写让 Lambda 冒烟测试全部静默 404(2026-08-03)
**现象**:用 `aws lambda invoke` 打已部署的 org-api 做冒烟测试,**每一条路由都返回 404 `{"error":"not found"}`**,
连 `/api/org/me` 也一样。看起来完全像 Lambda 的路由 bug——事件形状对、`httpMethod`/`path` 都填了、
caller 也能解析(否则会是 403 而不是 404)。
**根因**:Git Bash(MSYS)会把**看起来像 Unix 路径的命令行参数**自动改写成 Windows 路径。
传 `/api/org/me` 到达 Python 时已经变成 `C:/Program Files/Git/api/org/me`,于是
`lambda_handler` 的 `re.match(r"^/api/org(/.*)?$", path)` 不匹配 → `route` 保持原样 → `dispatch` 走到
末尾的兜底 `return error("not found", 404)`。**静默落空,没有任何线索指向参数被改写。**
**Fix**:`export MSYS_NO_PATHCONV=1`(已写进 `scripts/invoke_org_api.py` 的 docstring)。
**通则**:在 Windows 上排查"路由不匹配",**先确认进程真正收到的字符串**,再去看路由代码——
我在这上面把一整轮排查花在了错误的层。同类风险适用于任何以 `/` 开头的参数(S3 key 前缀、API 路径、cron 表达式)。

### BUG-43: 账号级 Lambda 并发只有 10 + extract-session 活锁 → 移动端上传真丢数据(2026-08-04)
**现象**:客户 2 小时离线录制,260 个分片只有 129 个进 S3,其中 69 个"文件在、`recordings.uploaded_at` 仍 NULL";
用户点 App 的 "Retry failed" **毫无反应**;而且**已上传的那 129 个在网页上一条也看不到**。

**根因一(容量):账号 Lambda `Concurrent executions` 配额 = 10** ——**账号级、单 region、全部 18 个函数共享**,
不是每函数。且 Lambda **按墙钟时长占槽**(包括干等 LLM/HTTP 响应的时间),所以 `占用并发 = 到达率 × 单次时长`。
`extract-session` 一个函数就常驻 ~5 格。**后果不对称**:同步路径(APIGW→org-api)被限流 = 立刻 5XX、**无重试、真丢数据**;
异步路径(S3 事件→vad/transcribe)被限流 = Lambda 自动重试最长 6h,**能自愈**。移动端上传走的正是同步路径:
org-api **1547 次 throttle → 88% 请求 5XX**。⚠️**被 throttle 的调用不产生任何日志**——Lambda 日志一片干净,
排查必须看 CloudWatch `Throttles` 指标 + API Gateway `5XXError`,别在代码里找。提额(`L-B99A9384`)不一定自动批,
可能转 support case。**提额后必须给 org-api 设 reserved concurrency**(配额是 10 时设不了,AWS 强制留 100 unreserved)。

**根因二(活锁):`extract-session` 的 I-2 守卫在持续进料下永远不可能满足。** 它在 LLM 调用**跑完之后**重新列目录,
集合变了就 `raise` 丢弃整份结果。但 chunk 分片 30s 一个、调用要 170s(thinking 模式)→ 回头必然变了 → 每次都丢。
**录制期间成功率为 0**。实测 381 次调用 / 360 错误(**94.5%**) / 664 次 `Duration=180000ms` 硬超时
(`llm_utils.HTTP_TIMEOUT=150s` < `Timeout=180s` → urllib3 超时 → 重试 → 被硬杀,连一次完整调用都跑不完)。
**错误率随录制时长单调上升**(7/29 0% → 7/30 46% → 7/31 61% → 8/3 94.5%)——**从上线起就在恶化,短测试录音一直掩盖着它**。

**根因三(归属):离线 chunk session 三条站点来源同时落空** → `identity bridge miss ... zero writes`。
`site_for_media` 的 LIKE 要求文件名**就是** `{session_base}.ext`(chunk 文件是 `{user}_{ts}_sid{id}_c{NNNN}.wav`);
`meeting_session.site_id` 为 NULL(设备的 `/sessions/{id}/open` 是 record-start 时 fire-and-forget,当时没网,
后来 session 由 chunk 流**推断**打开、不带 site);`resolve_site` 对 admin/gm **按设计**返回 None。
而 `recordings.site_id` 一直是对的。

**Fix**:
- 两层抽取(PR #217/#219),**写同一个 S3 key**(复用已有幂等覆盖 + item-writer `delete_topics_for_source`,零新增管道):
  `live` = transcripts/ 触发 / thinking **off** / **90s 节流**(照搬 `lambda_rolling_summary` 读输出件时间戳那套);
  `final` = `extraction_requests/` 触发(finalize-sweep 在会话关闭时写) / thinking **on** / 不节流 / 权威。
  守卫改成**覆盖比较**(`_supersedes`):已存在的是 final、或已覆盖本次全部片段 → 停手。保住竞态安全,
  但**不再丢弃已付费的 LLM 结果**。`Timeout` 180→600 + `LLM_HTTP_TIMEOUT=540`。
- `item-writer` 加第 3 级 `recordings.site_for_day`(**排在 membership 之前**,BUG-41 的规则:App 的
  `recordings.site_id` 是权威),PR #218/#219。
- App(GrandTime PR #3):`ExistingWorkPolicy.REPLACE` 用于手动 Retry、429/5xx 归为 `Busy` 不烧 8 次永久失败预算、
  PUT 结果无论如何都调一次幂等 `complete`、并发封顶 2、PUT 用无 `callTimeout` 的 client。

**通则(比这次事故本身更重要)**:
1. **「Lambda 能并行所以随便跑」是错的。** 加任何长耗时函数前,先算 `到达率 × 时长` 占配额多少。
2. **任何「做完昂贵操作后再校验前提、不成立就整份丢弃」的模式,在持续进料的系统里就是活锁。**
   要么在昂贵操作**之前**校验,要么让结果可被后续覆盖。
3. **新读自己输出的地方要补 IAM**:extract-session 原本只有 `PutObject` on `extractions/*`,节流要读旧件却没
   `GetObject` —— 而 `read_existing_extraction` 吞掉 AccessDenied 返回 None,**节流会静默失效**。
   一律 `simulate-principal-policy` 实测(同 BUG-41 一带的教训)。
4. **in-VPC 的函数不能主动 `lambda:InvokeFunction`**(BUG-36)——无 NAT 时任何外呼黑洞。
   需要**由 in-VPC 侧发起**的跨边界调用一律走 **S3 请求件**
   (`extraction_requests/` / `session_finalize_requests/` / `reindex_requests/` 同一套模式)。
   **⚠️ 反方向(VPC 外 → 调 VPC 内)是允许的,别误当成禁止。** 调用方在 VPC 外有正常网络,
   被调的那个只是靶子、自己不发起外呼。现有先例:`AskAgentFunction`(无 `VpcConfig`)invoke
   `RagSearchFunction`(VPC 内),`lambda_ask_agent.py:625`/`:703`;`device-report` invoke
   `device-ledger` 同理(2026-08-04 实测 5 秒返回,非超时)。**把这个方向也禁掉的代价**:
   会逼人去建一个本该直接 invoke 的 S3 跳,而 BUG-33 意味着每个新 S3 触发器都是模板外的手工接线。
5. **错误率要按"输入规模"分组看**,不要只看总体。这个 bug 的错误率是录制时长的函数,
   总体指标被大量短录音稀释,所以两周没人发现。

### 数据桶的 CORS 规则不在任何模板里(2026-08-04,带外配置,**别当成多余去删**)
`fieldsight-data-509194952652` 上有一条名为 **`amplify-read-for-canvas`** 的 CORS 规则:
`GET`/`HEAD`,`AllowedOrigins` = `https://main.d2fssznicvuckr.amplifyapp.com` +
`https://dev.d2fssznicvuckr.amplifyapp.com`,暴露 `ETag`/`Content-Length`/`Content-Type`。

**为什么需要**:前端的邮件预览要把照片**嵌成 data URI**(presign URL 贴进邮件会过期,收件人只看到破图),
做法是 `img.crossOrigin='anonymous'` + canvas `toDataURL()`。没有 CORS 响应头,canvas 会被"污染"
(tainted),`toDataURL` 抛 `SecurityError` —— 而且**图片本身照样显示正常**,所以症状是"预览里看得见、
复制出去就没有",很容易误判成复制逻辑的 bug。

**为什么不在 IaC 里**:桶是 stack 外的既有资源(BUG-34,靠 `DataBucketName` 参数引用),CFN 不管它的
CORS。所以这条规则**只存在于线上**,`git grep` 找不到任何痕迹。

**含义**:①换 Amplify 域名(自定义域、新 app id)必须同步加进 `AllowedOrigins`,否则复制照片静默失效;
②谁要"清理没在模板里的配置"时,这条不是遗留垃圾;③改动方式:
`aws s3api put-bucket-cors --bucket ... --cors-configuration file://...`(**整体替换**,不是追加,
先 `get-bucket-cors` 拿全量再改)。同类带外资源:BUG-36 里那个 DynamoDB VPC endpoint。

### programme 写端点尚未认识 `platform_admin`(2026-08-03,**待决策,非 bug**)
`_MANAGER_ROLES = ("admin","gm","pm")` —— programme 的所有写端点(`put_programme`、
`import_programme`、`create/delete task`、批量写、版本回滚、baseline)都用它做门。
`platform_admin` **不在其中**,所以跨公司运维账号无法导入或修改任何 programme。

这与本仓既有模式一致(见"platform_admin 跨公司"笔记:**分级读路径自动生效,每个写端点要单独教它 span-all**,
Team/sites/编辑任务/编辑项目都是这么一个个加上去的),所以 programme 只是**又一个还没教的新写面**,不是回归。

**没有直接放开,是因为这是权限放宽**:要不要让平台运维替客户导入 programme 是产品决定,不是实现细节。
需要时的改法是把 `platform_admin` 加进 `_MANAGER_ROLES`,并确认 `_resolve_site_param` 的跨公司可达性
(test 上还有 `TEST_GRADED_ROLES` 缺失 → 默认 false → platform_admin 站点恒空的坑,见 BUG-38 一带)。

### 定时器交接(2026-07-15 schedules cutover 上线)
录音下载 + 报告生成的 cron 已从遗留手管的 `sitesync` EventBridge 组切到 **fieldsight-prod SAM 栈**
的 schedule(`PROD_ENABLE_SCHEDULES=true`):orchestrator 15 分钟 sweep(工作时段 05:00–19:59 NZ)
让录音**盘中进湖**(不再一天一次 19:00)。5 个遗留 `sitesync` schedule 已 DISABLED(定义保留可回滚)。
plan `docs/superpowers/plans/2026-07-15-schedules-cutover.md`。

---

## Aurora item store — findings + programme-impact (2026-07-13/14)

背景见 `docs/superpowers/specs/2026-07-13-unified-extraction-labeling-design.md` +
`docs/superpowers/plans/2026-07-13-programme-impact-link.md`（Tasks 1-5，全部已上 TEST）。

- **`findings` 表（migration 0010）**：每个 topic 的富提取项（`observation`/`domain`/
  `severity`/`entity_name`/`entity_trade`/`recommended_action`），加上 programme-impact
  列（`programme_task_id`/`impact_severity`/`impact_note`/`impact_task_name`/
  `impact_evidence`/`impact_matched_at`）—— impact 是 finding 行上的列，不是第二张
  link 表（spec §9 的约束）。
- **`match_requests/` artifact v2**：item-writer 现在把每个 topic 的 `findings[]`
  （含 durable uuid）一并塞进它本来就在发的 artifact 里；matcher 复用已有的候选门/
  embedding 批次，多打一次 Claude 调用产出 finding→task 的 impact 判定；in-VPC
  writer 用 `findings.apply_impact` 落库。matcher 本身仍是 non-VPC（BUG-36）。
- **~~同日有效（same-day-only）~~ → 已持久化（authority flip 2026-07-16 上线）**：
  authority flip 全线上线（Tasks 0-9，plan `docs/superpowers/plans/2026-07-14-authority-flip.md`）。
  `fieldsight-prod-ingest` `AUTHORITY_FLIP=true`（由 repo 变量 `PROD_AUTHORITY_FLIP` 驱动，
  deploy-prod.yml）：当某 `(user,date)` 有 extraction topics 时，夜间 ingest **defer**
  （不再 `delete_topics_for_source_prefix("extractions/…")`、不写报告 topic），
  findings + impact **跨天持久、手动打勾不回滚**。~~"验证必须在 05:00 NZDT 前"作废~~。
  读路径：Today/Timeline 经 org-api `/timeline` shim（客户站 Amplify `FS_TIMELINE_SOURCE=aurora`）
  服务持久 override + 报告文档 prose。回滚：`PROD_AUTHORITY_FLIP=false` 重部署 + 按 (date,user)
  重跑 ingest 重新 supersede。零-extraction 天仍回落报告路径（I-4 门）。
- **D8 过渡期双写**：safety 域的 finding 现在同时落两个地方——旧的
  `safety_observations`（PR #46 `_derive_safety_flags` 桥接，`rollup.py` 仍读这张表）
  和新的 `findings`（0010）。这是刻意的过渡态，不去重；authority flip 落地后再退役
  桥接（届时 `rollup.py` 要先切到读 `findings`，否则 rollup 计数会静默归零）。
- **读路径**：`repositories/topics.py` `list_topics_for_date` 现在用第三个批量子查询
  （`ANY(%s)`，一次查全部 topic，不是 N+1，镜像 action_items/safety_observations 的写法）
  把 `findings` 挂到每个 topic 上；`/live-items`（`lambda_org_api.py`）零改动就能透传
  ——它本来就是通用序列化，没有 child allowlist。
