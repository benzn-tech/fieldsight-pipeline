# CLAUDE.md — FieldSight Development Guide

## Project Overview

FieldSight (formerly SiteSync) is an AI construction-site documentation platform. Field
workers' body-cam video / PTT audio / photos are captured **by the GrandTime mobile app**
and uploaded to an AWS pipeline (VAD → transcription → Claude extraction → templated
reports), producing structured daily/weekly/monthly reports plus a live dashboard. Beyond
reporting it now also does content correction, RAG Q&A, hybrid search, and life/work
privacy separation.

- **System of Record:** Aurora PostgreSQL + pgvector (topics / findings / action_items —
  editable + queryable). S3 holds media / transcripts / extractions / reports / the report lake.
- **Frontend (live):** the `fieldsight-ui` repo — vanilla-JS dashboard on **Amplify**
  (client site = `main`). The old single-file React `index.html` is retired.
- **Read/write API:** `lambda_org_api` (`/api/org/{proxy+}`) over Aurora + Cognito.
- **Region:** ap-southeast-2 · **Account (own SAM):** 509194952652 · **Data bucket:** `fieldsight-data-509194952652`
  (test bucket `fieldsight-data-test-509194952652`). A separate company CDK prod `164088480050` exists — **don't conflate**.

> **Living sources of truth** — this file, `ARCHITECTURE.md`, and `ROADMAP.md` all drift; the
> latter two still describe the retired RealPTT / DynamoDB era and are stale. Current state lives in:
> - `docs/PROJECT-SUMMARY-*.md` (latest = **2026-07-29**) — state, priorities, repos, bottleneck.
> - `docs/superpowers/specs/` + `plans/` + `SESSION-HANDOFF-*.md` — per-feature design + session handoffs.
> - `~/.claude/…/memory/MEMORY.md` — cross-session traps / topology (empty-list ACL, DB isolation, identity bridge …).
> - `/fieldsight-backend` skill — fast pickup (repo layout, deploy, test harness, live traps).

---

## Docs & repo hygiene

**Canonical doc spine — keep these current; everything else is derived or historical:**

| Doc | Role | Cadence |
|---|---|---|
| `CLAUDE.md` (this file) | How the system works + traps (BUG-01..38). Reference. | When architecture or a trap changes. |
| `PLAN.md` | **Living** ledger — shipped / in-flight / priorities / open questions / bottleneck. | **In place, every session — the single source for "what's next."** |
| `~/.claude/…/memory/MEMORY.md` + files | Cross-session facts / topology / traps that outlive one task. | As facts are learned. |
| `docs/superpowers/specs/` + `plans/` + `SESSION-HANDOFF-*.md` | Per-feature design + per-session handoff. | Per feature / session. |

**Anti-drift rules (why this section exists):**
- **Don't mint new dated status files.** `docs/PROJECT-SUMMARY-2026-07-29.md` is a one-time onboarding
  narrative; the *living* state lives in `PLAN.md`. Update PLAN.md in place instead of dropping a new
  dated snapshot each session.
- **`ARCHITECTURE.md` (v4.0 RealPTT/Fargate) and `ROADMAP.md` (2026-03) are STALE** — both now carry a
  top banner pointing here. Don't trust them; don't onboard a session from them.
- **The real frontend is the separate `fieldsight-ui` repo.** Any UI copy *inside* this repo is stale.

**Foreign / duplicate folders that don't belong here (pending cleanup — see PLAN.md §6):**

| Path | What it is | Git | Cleanup |
|---|---|---|---|
| `ui/` | Stale full copy of the `fieldsight-ui` repo (139 files; its docs have diverged from the real repo) | tracked | `git rm -r` on a housekeeping branch (recoverable from history) |
| `frontend/` | Dead `fieldsight_v5.jsx` + `index.html` artifact | tracked (2) | `git rm -r` |
| `claw-code/` | A **different** project (Claude-Code-like; Containerfile / PHILOSOPHY.md / install.sh) | **untracked** | confirm with user before deleting — git can't restore it |
| `../fieldsight-pipeline-main/` | A loose second copy of the pipeline (no `.git`) | non-repo | confirm with user before deleting — unrecoverable via git |

---

## Architecture

**System of Record = Aurora PostgreSQL + pgvector.** The single-file React artifact and the
legacy RealPTT-scraper flow (orchestrator → realptt.com login → Fargate downloader → DynamoDB)
are **retired**; media now arrives via the GrandTime app.

### Capture → report pipeline
```
app upload → org-api create_recording_upload_url → S3 + `recordings` row stamped with site_id
  → lambda_vad             Silero VAD (models/silero_vad.onnx); segments → audio_segments/…_off{X}_to{Y}_src{fmt}
  → lambda_transcribe      AWS Transcribe | ElevenLabs (ASR_PROVIDER); → transcripts/{folder}/{date}/ (date subfolder)
  → lambda_extract_session Claude → extractions/{folder}/{date}/{session_base}.json (topics/findings/action_items)
  → lambda_item_writer     in-VPC; writes topics/findings/action_items to Aurora = SoR (identity bridge, see Traps)
  → reports on demand      lambda_report_generator / lambda_meeting_minutes → structured JSON + python-docx Word
```

### Serve / query
```
fieldsight-ui (Amplify) → API Gateway
  → lambda_org_api    /api/org/{proxy+} — dashboard / timeline / reports / sessions; role hierarchy
                      (admin/gm > pm > site_manager > worker) + site-scoped ACL + graded visibility
  → lambda_ingest     nightly report→item ingest, authority-flip aware (defers to extraction topics)
  → lambda_ask_agent  RAG Q&A — DashScope embed (non-VPC) ↔ Aurora pgvector retrieve (in-VPC)
Auth: Cognito pool ap-southeast-2_q88pd6XXr (fieldsight-users). **Two identity systems** — see Traps.
```

> The legacy single-site gateway `lambda_fieldsight_api` still exists (memory
> *two-legacy-api-lambdas*); the client site is served by org-api, not it. Filename-time regex,
> VAD, and transcript-offset rules are unchanged — see BUG-01 / 09 / 11 below.

---

## Key Files

> All Lambda handlers + shared modules live under **`src/`**. Migrations under
> `src/migrations/` (0001–0023 in this checked-out tree; newest voice/session migrations land
> on the current feature branch — see the note below the table).

| File (`src/…`) | Purpose |
|------|---------|
| `transcript_utils.py` | **Shared module** — unified time extraction + transcript normalization. Bundle in every Lambda zip. |
| `lambda_org_api.py` | Read/write API over Aurora (`/api/org/{proxy+}`) — dashboard/timeline/reports/sessions; role + site ACL. |
| `lambda_item_writer.py` | **In-VPC** writer: extractions → Aurora topics/findings/action_items (System of Record). Identity bridge. |
| `lambda_extract_session.py` | Claude extraction of a transcript session → `extractions/`. |
| `lambda_ingest.py` | Nightly report→item ingest; authority-flip aware. |
| `lambda_ask_agent.py` / `lambda_rag_search.py` | RAG Q&A (DashScope embed + pgvector) / hybrid semantic+lexical search. |
| `lambda_embed_report.py` / `reindex.py` / `chunking.py` | Embedding + reindex + chunking for RAG. |
| `lambda_vad.py` | Silero VAD (loads `models/silero_vad.onnx`, NOT a Layer — BUG-02). |
| `lambda_transcribe.py` | Triggers AWS Transcribe (or ElevenLabs via `ASR_PROVIDER`) on new audio. |
| `lambda_report_generator.py` / `lambda_meeting_minutes.py` | Site daily/weekly/monthly reports / meeting minutes → JSON + Word. |
| `lambda_programme_matcher.py` / `lambda_suggestion_writer.py` | Finding→programme-task impact matching + suggestions. |
| `claude_utils.py` / `dashscope_utils.py` | Claude API wrapper / Qwen (DashScope) STT·TTS·embed. |
| `lambda_voice_*.py`, `lambda_ws_*.py` | Site-voice realtime (WebSocket connect/disconnect/send + fanout/resolve/reaper/audit). |
| **Legacy** `lambda_fieldsight_api.py`, `lambda_orchestrator.py`, `lambda_downloader.py`, `fargate_downloader.py` | Old single-site gateway + RealPTT scraper (retired data path; don't extend). |
| `prompt_templates.json` / `prompt_templates_meeting.json` / `user_mapping.json` | Hot-swappable prompts + device→person mapping (S3: `config/`). |

> **On the current feature branch** (`feat/voice-chunk-session`, off `develop`) but not yet in
> this checked-out tree: chunk/session identity, `meeting_session` migration, session-lifecycle
> endpoints, `email_sender.py`, provider abstraction (`llm_utils.py`), Delivery-C T1/T2. See
> `docs/PROJECT-SUMMARY-2026-07-29.md` §1 + the voice specs for what's live vs inert.

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

## Deploy

**Primary path = SAM via GitHub Actions.** Push `develop` → `deploy.yml` → **TEST** stack
(`fieldsight-test-*`, DB `fieldsight_test`, bucket `fieldsight-data-test-509194952652`, auto-migrate).
Push `main` → `deploy-prod.yml` → **PROD** (DB `fieldsight`) behind a **`production` GitHub-environment
approval gate** — a human approves via `gh api … pending_deployments … -f state=approved` (often
permission-gated; the user runs it with `!`). Test↔prod org-api share the Aurora cluster, different DBs
(BUG-38). Adding a CFN resource? Also grant IAM on the deploy role `github-actions-fieldsight-deploy`
(missing → CREATE_FAILED rolls back the whole stack — memory *org-api-new-route-iam-trap*). Runbook:
`DEPLOYMENT-RUNBOOK.md`.

**Test harness (Windows, Python 3.14 + uv):**
```bash
export UV_LINK_MODE=copy   # Dropbox cloud-sync breaks uv hardlinks
export AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_DEFAULT_REGION=ap-southeast-2
uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit -q
```
Repo unit tests use a FakeConn/FakeCursor double (no real Postgres); org-api handler tests go through
`org.lambda_handler(make_event(...))` with repo funcs monkeypatched on `org.<module>`.

### Legacy single-lambda hotfix (standalone functions only)

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
