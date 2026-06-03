# [[BACKEND-CONTEXT.md](http://BACKEND-CONTEXT.md)](http://BACKEND-CONTEXT.md) — FieldSight Backend Reference for UI/UX Rebuild
> Purpose: Single source of truth for **the data the frontend consumes**. Read this before designing or coding any UI surface. Schemas here are derived directly from `src/lambda_fieldsight_[[api.py](http://api.py)](http://api.py)`, `src/lambda_report_[[generator.py](http://generator.py)](http://generator.py)`, `src/lambda_meeting_[[minutes.py](http://minutes.py)](http://minutes.py)`, `src/lambda_ask_[[agent.py](http://agent.py)](http://agent.py)`, and `config/`. If the code disagrees with this doc, the code wins — please update this doc.
---
## 1. Product in one paragraph
FieldSight is an AI construction-site documentation platform. Body-cam video, push-to-talk audio, and photos captured by field workers (RealPTT devices like `Benl1`–`Benl6`) are uploaded to S3, run through a pipeline (VAD → AWS Transcribe → Claude), and emitted as **structured JSON daily / weekly / monthly reports** plus optional **meeting minutes**. The frontend authenticates users through Cognito and reads everything via a single REST API Lambda (`fieldsight-api`). All media is served via short-lived **presigned S3 URLs** the API hands out.
- **Region:** `ap-southeast-2` (Sydney) — all resources colocated
- **S3 bucket:** `fieldsight-data-509194952652`
- **Cognito user pool:** `ap-southeast-2_ps7XIQGHB`
- **Cognito client ID:** `5npb81jbj1hgh9tsck25kan3os`
- **Display timezone:** NZDT (UTC+13). All API output is already UTC-based; **the frontend converts to NZDT for display**.
---
## 2. High-level data flow (what the UI ultimately reads)
Field device (RealPTT)
    │
    ▼
 S3: users/{display_name}/{video|audio|pictures}/{date}/{file}
    │
    ├──► VAD Lambda      → audio_segments/{user}/{date}/*.wav
    │                       + web_video/{user}/{date}/*.mp4 (H264 preview)
    │
    ├──► Transcribe      → transcripts/{user}/{date}/*.json (AWS Transcribe schema)
    │
    └──► Report Generator (Claude)
            ├── reports/{date}/{user}/daily_report.json     ← per-user
            ├── reports/{date}/summary_report.json          ← combined (admin/gm view)
            └── reports/{date}/{user}/.meeting_manifest.json ← exclusion marker
        Meeting Minutes (Claude)
            └── meeting_minutes/{date}/{title}.json
                  AND/OR
                reports/{date}/{user}/meeting_minutes.json
Frontend (CloudFront → S3 static) ──► API Gateway ──► fieldsight-api Lambda
**The UI never reads S3 directly.** It calls the API, which either returns JSON or hands back a presigned URL for binary media.
---
## 3. Authentication — Cognito JWT
Cognito hosted JS flow. The frontend gets `idToken` via `USER_PASSWORD_AUTH` (with a `NEW_PASSWORD_REQUIRED` challenge on first login) and sends it as the `Authorization` header to every API call.
```js
// Sign in
POST https://cognito-idp.ap-southeast-2.amazonaws.com/
Headers: X-Amz-Target: AWSCognitoIdentityProviderService.InitiateAuth
Body:    { AuthFlow:"USER_PASSWORD_AUTH", ClientId, AuthParameters:{USERNAME,PASSWORD} }
// API call
GET /api/timeline?date=2026-04-29
Authorization: <idToken>
### JWT claims the API trusts
| Claim | Source | Used for |
|---|---|---|
| sub | Cognito | Lookup USERS_TABLE → PK=USER#{sub}, SK=PROFILE for role/sites |
| email | Cognito | Display only |
| name | Cognito | Fallback to look up device in user_mapping.json |
The API derives a caller object on every request:
{
  sub: string,
  email: string,
  name: string,            // Cognito display name
  role: 'admin'|'gm'|'pm'|'site_manager'|'worker'|'viewer',
  display_name: string,    // e.g. "Jarley Trainor"
  device_id: string,       // e.g. "Benl1"
  sites: string[],         // sites this user works on
  managed_sites: string[], // sites this user manages (pm/site_manager)
  company_id: string,
}
### Role hierarchy & visibility rules
admin / gm > pm > site_manager > worker
| Role | Can see |
|---|---|
| admin, gm | All sites, all users, summary reports |
| pm | Users on managed_sites |
| site_manager | **Self only + workers on accessible sites** (NOT other site managers — fixed in BUG-25) |
| worker | **Self only** — user query param is forced to caller's display name |
CORS is open (Access-Control-Allow-Origin: *). 401 = invalid/missing token. 403 = role can't access target user/site. 404 may be returned with a JSON body {message, date} instead of HTTP 404 — check both.
> **CloudFront 404 trap (BUG-20)**: When the SPA receives a 404 from API, CloudFront may rewrite it to index.html (HTML, status 200). The frontend MUST inspect content-type; if it isn't application/json, treat as not-found.
---
## 4. REST API — every endpoint the UI uses
Base path is /api. Every endpoint except /api/health requires a Cognito ID token.
### 4.1 Health
GET /api/health
→ 200 { status:"ok", service:"sitesync-api", version:"2.0", bucket, timestamp }
### 4.2 Sites & users
GET /api/sites
→ {
    sites: [
      { site_id, name, location, client, user_count }
    ],
    role: "site_manager",
    display_name: "Jarley Trainor"
  }
GET /api/site-users?site=sb1108-ellesmere
→ { users: [{device_id,name,folder_name,role,sites,primary_site}], site }
   403 if caller has no access to site
GET /api/users      // all mapped users (admin-flavoured; doesn't enforce filtering)
→ { users: [{device_id, name, role, sites}] }
### 4.3 Calendar (which days have a report?)
GET /api/dates?months=3&site=sb1108-ellesmere   // site optional
→ {
    dates: {
      "2026-04-28": { hasReport:true, topics:14, safety:3 },
      "2026-04-29": { hasReport:true, topics:11, safety:2 },
      ...
    }
  }
Use this to render heat-mapped calendar dots / week pickers. topics is the **max** across accessible users on that date; safety counts topics with category=='safety' OR non-empty safety_flags[].
### 4.4 Timeline (daily report — the main "story" view)
GET /api/timeline?date=YYYY-MM-DD&user=Jarley_Trainor
> **Inversion note (in-flight):** this becomes **item-backed** (assembled from `ITEM#` rows) but the **response shape stays byte-compatible** with §5.1 — `today-adapter.js` / `timeline.js` are unaffected; falls back to the `daily_report.json` file during migration. New optimistic feed: §4.13 `GET /api/today`.
User folder names use **underscore form** (Jarley_Trainor). The API accepts spaces too and tries both.
| Caller | Behaviour when user is omitted |
|---|---|
| admin/gm | Returns summary_report.json (combined). If missing, find_any_report returns {date, available_users:[...]} so the UI can let admin pick. |
| pm/site_manager | Falls back to caller's own report, then find_any_report. |
| worker | Forced to caller's own data. |
Response is a **Daily Report** (schema in §5.1) **OR**:
{ "message": "No report for Jarley_Trainor on 2026-04-29", "date": "2026-04-29" }   // 404 body
{ "date": "2026-04-29", "available_users": ["Jarley_Trainor","David_Barillaro"] }  // 200, multi-user
### 4.5 Transcripts (raw text behind a topic)
GET /api/transcripts?date=YYYY-MM-DD&user=Jarley_Trainor&start=08:15:00&end=09:00:00
→ {
    text:                "concatenated speaker text in range",
    filtered_text:       "(same)",
    segments: [          // Per source file (10 min recording chunk)
      {
        time:                  "08:14:32",
        time_seconds:          29672,
        text:                  "full file transcript",
        filtered_text:         "words inside [start,end] only",
        filename:              "Benl1_2026-04-29_08-14-32_off0.5_to612.0_srcwav.json",
        word_count:            812,
        in_range_count:        540,
        speaker_segment_count: 18
      }
    ],
    speaker_segments: [  // Per Transcribe diarized turn, sorted by absolute time
      {
        speaker:    "spk_0",
        text:       "...",
        start:      29672.5,         // absolute seconds since midnight
        end:        29684.1,
        time_label: "08:14:32",
        duration:   11.6
      }
    ],
    speakers: ["spk_0","spk_1","spk_2"],
    count: 3,                       // segment files in range
    speaker_count: 3,
    total_speaker_segments: 142
  }
The query is **time-windowed with a 60s buffer** on both sides. start/end accept HH:MM:SS or HH:MM.
### 4.6 Audio segments (playable VAD chunks)
GET /api/audio-segments?date=...&user=...&start=08:15:00&end=09:00:00
→ {
    segments: [
      {
        url:            "<presigned, 15min>",
        filename:       "Benl1_2026-04-29_08-14-32_off30.5_to95.2_srcwav.wav",
        absolute_start: 29702.5,
        absolute_end:   29767.2,
        duration:       64.7,
        time_label:     "08:15:02"
      }
    ],
    count: 12
  }
Each segment is short (typically 5-90s) — built from VAD output. Use these for the topic-level audio scrubber/play list.
### 4.7 Video segments
GET /api/video-segments?date=...&user=...&start=...&end=...
→ {
    videos: [
      {
        url:             "<presigned>",
        key:             "web_video/Jarley_Trainor/2026-04-29/Benl1_2026-04-29_08-14-32.mp4",
        filename:        "Benl1_2026-04-29_08-14-32.mp4",
        base_name:       "Benl1_2026-04-29_08-14-32",
        video_start_sec: 29672,
        time_label:      "08:14:32",
        offset_sec:      28.0,    // jump-to point inside the file (start - file_start)
        size_mb:         48.2,
        is_preview:      true,    // true = web_video/ (H264, browser playable)
        codec:           "h264"
      }
    ],
    count: 2
  }
**Always prefer is_preview:true files** — they're guaranteed browser-playable H264. Originals (users/{name}/video/) may be H265 and won't play in Chrome. The API already de-dupes (skips an original if a preview exists).
### 4.8 Recording stats (header KPIs)
GET /api/recording-stats?date=YYYY-MM-DD&user=...
→ {
    video_count:             4,
    audio_count:             8,
    total_files:             12,
    total_size_mb:           512.4,
    estimated_duration_min:  120
  }
estimated_duration_min is a coarse files × 10min heuristic — fine for "≈2h of recordings today" cards, not precise.
### 4.9 Generic media presigner (escape hatch)
GET /api/media/presigned-url?key=<urlencoded S3 key>
→ { url:"<https presigned>", expires_in: 900 }
Allowed prefixes: users/, audio_segments/, transcripts/, reports/, web_video/. The API enforces per-user permission on the key's owner folder. **Use this for thumbnails (pictures/) and .docx downloads.**
### 4.10 Action items (interactive checkboxes)
The frontend persists checkbox state across users / devices.
POST /api/actions/toggle
Body: { date, topic_id, action_index, checked, action_text }
→ { message:"Updated", checked:true }
> **UI fusion note:** the sprint11 UI's `actions.js` currently fires `PATCH /api/actions/{id}` + `POST /api/actions`; the backend implements only this `POST /api/actions/toggle`. Reconcile during wiring (repoint the UI module or add a backend route alias).
GET /api/actions?date=YYYY-MM-DD
→ {
    date,
    actions: {
      "0_2": { checked:true, checked_by:"Jack Gibson", checked_at:"2026-04-29T01:18:42Z" },
      // key = `${topic_id}_${action_index}`
    }
  }
Both current state AND an immutable audit log are written to fieldsight-audit DynamoDB. No way to delete history — all toggles are kept.
### 4.11 Reports — history & regenerate
GET /api/reports/history?limit=20
→ { reports: [{ key, type:"daily"|"weekly"|"monthly", date, generated_at, size }] }
POST /api/reports/generate
Body: { report_type:"daily"|"weekly"|"monthly", date?:"YYYY-MM-DD", force?:bool }
→ 202 { message, status:"pending" }
force:true regenerates even if the report exists. Workers can only trigger their own report (server forces users_filter).
### 4.12 Ask Agent (Q&A grounded in transcript + report)
POST /api/ask
Body: {
  date: "2026-04-29",
  user: "Jarley_Trainor",
  question: "What was decided about the scaffold inspection?",
  scope?: "report"|"transcript"|"both",   // default "both"
  topic_id?: 2                            // narrow to one topic's time range
}
→ { answer, citations:[...], model:"claude-haiku-4-5-20251001", ... }
Stateless — no conversation memory. Each question is independent. Workers are forced to query their own data.
---
### 4.13 Today feed (optimistic, item-backed) — NEW (dashboard-first)
GET /api/today?date=YYYY-MM-DD
→ { "items":[...], "processing":[...], "generated_at":"..." }
- `items[]` = materialized `ITEM#` / `TODAY#` rows for the date (the dashboard-first source of truth).
- `processing[]` = optimistic cards derived from the transcript ledger (status transcribing/pending), so the UI shows "Recording uploaded → Transcribing → Summarizing → Ready" within seconds — replaces the §4.4 404 empty-state.
- `generated_at` = real freshness; use it, never a hardcoded time.
- **Polling**: this is the one endpoint the UI may poll (~30–60s) for the live Today list (see §8.11). Pairs with Fast Mode's *provisional* items (`provisional:true` + `confidence`).
> Status: planned — part of the report→dashboard inversion (pipeline `DASHBOARD-FIRST-INVERSION.md`, Phase 0/1). Needs the ledger `'reported'` terminal state to exist (pipeline BUG-40) or processing cards won't clear.

### 4.14 Dashboard (per-site cards) — NEW (on feature/p2)
GET /api/dashboard?site=...&date=...
→ per-site cards from `ITEM#` + `DEADLINE#` rows. Lights up once the item store is enabled AND `DEADLINE#` rows are written (pipeline BUG-39 — today the writer emits only `ITEM#`).

### 4.15 Export to email / share — NEW
POST /api/export { kind, id, recipients, format }
→ renders + **freezes an immutable snapshot** of what the caller may see (keyed by `generated_at`), emails it via SES, logs to the audit table. Backs the shared `ExportButton` embedded across Today / dashboard / report / meeting / search. Only exports data the caller is permitted to see.

---

## 5. Data structures — what the JSON looks like
### 5.1 Daily Report (reports/{date}/{user}/daily_report.json)
> **Inversion note (in-flight):** `daily_report.json` / `.docx` are being **demoted to a secondary, on-demand, frozen export** (for storage & accountability / 追责). The source of truth becomes the DynamoDB item store (`ITEM#`/`TODAY#`/`DEADLINE#`). The shape below stays the **frozen contract** the projection must reproduce exactly (esp. `executive_summary` and `_report_metadata.generated_at`), so adapters don't break.
The **primary** object the UI renders. Returned verbatim by /api/timeline. Generated by Claude from prompt_templates.json.
{
  "report_date":  "2026-04-29",
  "report_type":  "daily",
  "user_name":    "Jarley Trainor",
  "device":       "Benl1",
  "site":         "SB1108 Ellesmere College",
  "executive_summary": [           // v3.0+: array of bullet strings (NO bullet char)
    "Morning brief covered safety...",
    "Concrete pour at Block C completed..."
  ],
  "critical_dates_and_deadlines": [
    {
      "date_mentioned": "Friday 03 May",
      "context":        "Council inspection scheduled",
      "who_mentioned":  "Jack Gibson",
      "urgency":        "high",          // high | medium | low
      "type":           "inspection"     // deadline|inspection|delivery|weather|meeting|other
    }
  ],
  "quality_and_compliance": [
    {
      "item":             "Concrete slump test",
      "status":           "completed",   // pending|in_progress|completed|concern
      "details":          "...",
      "follow_up_needed": false
    }
  ],
  "safety_observations": [           // site-wide (separate from per-topic safety_flags)
    {
      "observation":         "Loose scaffold board on level 2",
      "risk_level":          "high",     // high|medium|low
      "location":            "Block C",
      "who_raised":          "Jarley Trainor",
      "recommended_action":  "..."
    }
  ],
  "topics": [
    {
      "topic_id":     0,                  // sequential, starts at 0
      "time_range":   "08:15 – 09:00",   // EN-DASH (–), HH:MM
      "topic_title":  "Morning Safety Briefing",
      "category":     "safety",           // safety|progress|quality
      "participants": ["Jarley Trainor","Jack Gibson"],
      "summary":      "2-4 sentence summary...",
      "key_decisions": [
        "Decision attributed to person (string)"
      ],
      "action_items": [
        {
          "action":      "Order replacement boards",
          "responsible": "Jack Gibson",
          "deadline":    "Tomorrow 08:00",  // free-text or null
          "priority":    "high"             // high|medium|low
        }
      ],
      "safety_flags": [
        {
          "observation":        "Trip hazard near gate 2",
          "risk_level":         "medium",
          "recommended_action": "..."
        }
      ],
      "related_photos": ["IMG_1234.jpg"]    // filenames only — fetch via /api/media/presigned-url
    }
  ],
  "_report_metadata": {
    "version":              "v3.5",
    "generated_at":         "2026-04-29T16:00:00Z",
    "generated_by":         "system",
    "recordings_processed": 12,
    "total_words":          3450,
    "model":                "claude-sonnet-4-6",
    "parse_success":        true
  }
}
**UI implications**
- executive_summary is now an **array of strings** (since prompt-templates v3.0). Render as a bullet list, never assume single string.
- time_range always uses the en-dash – (U+2013), never a hyphen.
- participants may include device IDs (Benl1) when names couldn't be resolved.
- related_photos are filenames only; the photos live at users/{folder_name}/pictures/{date}/{filename}. Build the S3 key yourself and call /api/media/presigned-url.
- All times are NZDT clock times (HH:MM), already in display tz.
- Empty arrays are normal — render gracefully.
### 5.2 Summary Report (reports/{date}/summary_report.json)
Same shape as Daily Report but covers all users on the date. Only admins/gm reach this via /api/timeline?date=... with no user param. Treat as Daily Report for rendering.
### 5.3 Weekly Report (reports/{date}/{...}/weekly_report.json etc.)
Returned by /api/reports/history; UI fetches with /api/media/presigned-url to render.
{
  "executive_summary": ["bullet 1", "bullet 2", "..."],
  "safety_trends": [
    { "trend":"...", "risk_level":"high", "frequency":"3 times this week", "recommendation":"..." }
  ],
  "progress_highlights": [
    { "item":"...", "date":"2026-04-25", "status":"completed" }   // completed|in_progress|delayed
  ],
  "outstanding_actions": [
    { "action":"...", "responsible":"...", "original_date":"2026-04-22",
      "priority":"high", "status":"open" }                         // open|overdue|completed
  ],
  "quality_summary":      "Overview of quality observations...",
  "next_week_priorities": ["Priority 1","Priority 2"]
}
Monthly reports use the same schema with next_week_priorities renamed to next_month_priorities.
### 5.4 Meeting Minutes (meeting_minutes/{date}/{title}.json or under reports/{date}/{user}/meeting_minutes.json)
Generic business-meeting flavour, NOT site-inspection.
{
  "executive_summary": ["bullet 1","bullet 2","bullet 3"],
  "topics": [
    {
      "topic_id":     0,
      "time_range":   "10:05 – 10:32",
      "topic_title":  "Q3 budget review",
      "category":     "finance",   // strategy|operations|finance|product|partnership|technical|hr|legal|general
      "status":       "decided",   // decided|deferred|in_discussion|blocked
      "participants": ["Ben","Sam"],
      "summary":      "...",
      "key_decisions": [
        { "decision":"...", "rationale":"...", "decided_by":"Ben" }
      ],
      "action_items": [
        { "action":"...", "owner":"Sam", "deadline":"By Friday", "priority":"high" }
      ],
      "open_questions": ["..."]
    }
  ],
  "follow_ups":   [{ "item":"...", "owner":"...", "deadline":"...", "priority":"high", "depends_on":"..." }],
  "next_steps":   ["Top action 1","Top action 2"],
  "parking_lot":  ["Items deferred to next meeting"]
}
> Different field names from Daily Report on purpose: owner instead of responsible, action items without safety_flags. Plan two distinct UI components.
### 5.5 Mutual exclusion: meeting vs site report
When a day's transcripts are processed as a meeting, the meeting Lambda writes reports/{date}/{user}/.meeting_manifest.json listing consumed transcript keys. The daily report generator skips those. So a single date can have:
reports/2026-04-29/Jarley_Trainor/
  ├── daily_report.json          ← site walk only
  ├── meeting_minutes.json       ← meeting only (or under meeting_minutes/)
  └── .meeting_manifest.json     ← exclusion marker (don't render this)
UI should display them side-by-side or as toggleable views.
### 5.6 AWS Transcribe raw JSON
Returned **inside** /api/transcripts.segments[].text etc., but if you fetch a transcript file directly (admin tooling), the shape is AWS standard:
{
  "results": {
    "transcripts": [{ "transcript": "Full text..." }],
    "items": [
      {
        "type":          "pronunciation",
        "start_time":    "0.5",      // STRING, seconds RELATIVE to the segment file
        "end_time":      "1.0",
        "speaker_label": "spk_0",
        "alternatives":  [{ "content":"Hello", "confidence":"0.98" }]
      }
    ],
    "audio_segments": [               // diarized chunks
      { "start_time":"0.5","end_time":"12.1","speaker_label":"spk_0","transcript":"..." }
    ]
  }
}
Filenames carry critical metadata — never assume simple names:
Benl1_2026-04-29_08-14-32.json                       ← full file (no VAD offset)
Benl1_2026-04-29_08-14-32_off1465.8_to1729.8_srcwav.json ← VAD segment
absolute_time_of_word = base_time(filename)
                      + vad_offset(filename)
                      + word.start_time(json)
The frontend almost never needs to compute this — /api/transcripts already returns absolute time_label fields. But if you build admin tooling that reads transcript JSON directly, use the same logic in src/transcript_[[utils.py](http://utils.py)](http://utils.py).
---
## 6. User mapping (config/user_mapping.json)
Loaded once and cached for 5 minutes by the API. Devices ↔ humans ↔ sites.
{
  "_version": "2.1",
  "sites": {
    "sb1108-ellesmere": {
      "name":     "SB1108 Ellesmere College",
      "location": "Christchurch",
      "client":   "Ministry of Education"
    },
    "mpi":                    { "name":"MPI", "location":"Auckland", "client":"..." },
    "sb1131-northbrook-wanaka":{ "name":"SB1131 - Northbrook Wanaka", ... }
  },
  "mapping": {
    "Benl1": {
      "name":         "Jarley Trainor",
      "role":         "site_manager",
      "primary_site": "sb1108-ellesmere",
      "sites":        ["sb1108-ellesmere"]
    },
    "Benl2": { "name":"MPI1", "role":"worker", "primary_site":"mpi", "sites":["mpi"] },
    // ...
  },
  "reassignment_log": [
    { "device":"Benl5","previous_user":"MPI3","new_user":"James Lamb",
      "changed_date":"2026-02-25","reason":"..." }
  ]
}
**Notes for UI**
- device_id (Benl1) is the primary key — the **folder name in S3 is name.replace(' ','_')** (Jarley_Trainor). Always normalise.
- Old recordings keep the old folder name even after a reassignment. Don't assume current name matches every historical folder.
- A user can be on multiple sites[] but has one primary_site.
- Roles in mapping are worker | site_manager | pm. admin / gm exist only in the Cognito DynamoDB profile, not here.
---
## 7. S3 path conventions (so the UI can build keys for the presigner)
config/user_mapping.json
config/prompt_templates.json
config/prompt_templates_meeting.json
users/{display_name}/video/{YYYY-MM-DD}/{device}_{date}_{HH-MM-SS}.mp4   ← raw (may be H265)
users/{display_name}/audio/{YYYY-MM-DD}/{device}_{date}_{HH-MM-SS}.wav
users/{display_name}/pictures/{YYYY-MM-DD}/{device}_{date}_{HH-MM-SS}.jpg
audio_segments/{display_name}/{YYYY-MM-DD}/{device}_{date}_{time}_off{a}_to{b}_src{fmt}.wav
transcripts/{display_name}/{YYYY-MM-DD}/{device}_{date}_{time}_off{a}_to{b}_src{fmt}.json
web_video/{display_name}/{YYYY-MM-DD}/{device}_{date}_{HH-MM-SS}.mp4    ← H264 preview (only for H265 sources)
reports/{YYYY-MM-DD}/summary_report.json
reports/{YYYY-MM-DD}/summary_report.docx
reports/{YYYY-MM-DD}/{display_name}/daily_report.json
reports/{YYYY-MM-DD}/{display_name}/daily_report.docx
reports/{YYYY-MM-DD}/{display_name}/daily_report_debug.json     ← prompt-tuning, not for UI
reports/{YYYY-MM-DD}/{display_name}/meeting_minutes.json
reports/{YYYY-MM-DD}/{display_name}/.meeting_manifest.json     ← internal marker, hide
meeting_minutes/{YYYY-MM-DD}/{title}.json
**Filename time regex (BUG-01)** — always anchor after the date or you'll match the date instead of the time:
\d{4}-\d{2}-\d{2}_(\d{2})-(\d{2})-(\d{2})
Rule of thumb for media in the UI:
1. For **video clips inside a topic** → /api/video-segments (already chooses preview).
2. For **audio clips inside a topic** → /api/audio-segments.
3. For **photos referenced in topic.related_photos** → build key users/{folder}/pictures/{date}/{filename} and call /api/media/presigned-url.
4. For **report Word docs** → list via /api/reports/history, then /api/media/presigned-url on the key.
Presigned URLs **expire in 15 min**. Re-fetch when reopening modals; don't cache them in localStorage.
---
## 8. Frontend integration patterns / pitfalls
These are real bugs the existing frontend hit. The new UI MUST handle them.
### 8.1 Date math in NZDT (BUG-19)
new Date("2026-03-09T12:00:00") parses as local time. In NZDT (UTC+13), .toISOString().slice(0,10) shifts the date by 1 day. Use UTC arithmetic:
const [y,m,d] = "2026-04-29".split('-').map(Number);
const next = new Date(Date.UTC(y, m-1, d+1));
const nextStr = next.toISOString().slice(0,10);   // "2026-04-30"
### 8.2 Detecting "no report"
404 may arrive as 200 + content-type: text/html (CloudFront SPA fallback). Always:
const ct = res.headers.get("content-type") || "";
if (!ct.includes("application/json")) return { _notFound: true };
Also handle: { message:"No report for ...", date } (200 body) and { available_users:[...] } (admin disambiguation).
### 8.3 React audio/video state
audioRef.current.paused is a ref — does NOT trigger re-render. Drive Play/Pause UI from useState updated by audio.onplay/onpause (BUG-21).
### 8.4 Permission errors are first-class
403 { error: "Access denied to this user" } happens whenever a non-admin queries someone else. Render an empathetic "you don't have access" state, not a generic toast.
### 8.5 Worker UX simplification
For a worker, every endpoint forces user = self. The UI for workers can hide the user picker entirely.
### 8.6 Speaker labels
spk_0, spk_1 from Transcribe diarization are not stable across recordings (the same person is spk_0 in one file and spk_2 in another). Don't try to colour-code them globally. If the report supplies participants, prefer those names.
### 8.7 Empty-state arrays
safety_observations, safety_flags, action_items, key_decisions, related_photos, topics can all be []. A day with no recordings still produces a report shell with empty topics.
### 8.8 Topic IDs & action keys
Action checkbox state is keyed by ${topic_id}_${action_index}. If the report is regenerated, topic_ids may shift — historical action checkmarks could "move". Treat this as best-effort; for hard audit, use /api/actions history.
### 8.9 Time strings in topic data are display-formatted
time_range: "08:15 – 09:00" is already in NZDT clock form. To compute durations you must parse it yourself — there's no start_seconds/end_seconds field on topics.
### 8.10 Photo & media bandwidth
Original videos can be 200–300 MB each. Always render the H264 preview in /api/video-segments. For thumbnails of photos use <img loading="lazy">.
---
### 8.11 Polling cadence (NEW)
Historically the only mutable surface was action items (§4.10) and §10 said polling is fine there. The new **`GET /api/today`** (§4.13) is explicitly **pollable at ~30–60s** to drive the live Today list + optimistic "processing" cards. Keep other reads request-on-navigation; do not poll `/api/timeline` or aggregators.

## 9. Quick map — UI surface ↔ API endpoints
| Screen / Component | Primary endpoints |
|---|---|
| Login | Cognito InitiateAuth / RespondToAuthChallenge |
| App shell / context | GET /api/sites (role + accessible sites) |
| Calendar / date picker | GET /api/dates?months=3 |
| Daily report (timeline) | GET /api/timeline?date=&user=, GET /api/recording-stats |
| Topic detail drawer | GET /api/transcripts?...&start=&end=, GET /api/audio-segments, GET /api/video-segments |
| Photo modal | GET /api/media/presigned-url?key=users/{folder}/pictures/{date}/{file} |
| Action item checkbox | GET /api/actions?date=, POST /api/actions/toggle |
| Ask agent (chat per topic) | POST /api/ask |
| Reports archive | GET /api/reports/history, GET /api/media/presigned-url |
| Regenerate button | POST /api/reports/generate |
| User switcher (admin/pm) | GET /api/sites, GET /api/site-users?site= |
| Meeting minutes view | GET /api/media/presigned-url?key=reports/{date}/{user}/meeting_minutes.json (no dedicated endpoint yet — fetch the JSON directly) |
---
## 10. What's NOT in the API yet
Useful to know before designing flows that assume them:
- **No streaming / websockets** — everything is short-lived REST. Polling is fine for action items, and now also for the new `GET /api/today` (§4.13, ~30–60s). True streaming transcription stays a later roadmap item.
- **No edit endpoints** — reports are read-only. Action checkboxes are the only mutable surface (besides regenerate).
- **No comments / threading.**
- **No notifications endpoint.**
- **No file upload from the UI** — uploads happen device-side (RealPTT). *(On the roadmap: drag&drop upload + an ingest-normalizer that derives user/type/date from media metadata or a coarse date+half-day input — see pipeline ROADMAP.md.)*
- **No user/profile management UI hook** — Cognito admin operations are CLI-only.
- **/api/ask is stateless** — multi-turn chat must be reconstructed client-side and resent.
- **Meeting minutes has no list endpoint** — discover via /api/reports/history (matches *_report.json only) or by building S3 keys yourself.
If a screen needs any of the above, raise it before designing — backend work is required first.
---
## 11. Where to look in the source
| Concern | File |
|---|---|
| All HTTP routes & permission logic | src/lambda_fieldsight_[[api.py](http://api.py)](http://api.py) |
| Daily/weekly/monthly schema + Claude prompts | src/lambda_report_[[generator.py](http://generator.py)](http://generator.py), config/prompt_templates.json |
| Meeting minutes schema + Claude prompts | src/lambda_meeting_[[minutes.py](http://minutes.py)](http://minutes.py), config/prompt_templates_meeting.json |
| Time/transcript normalisation (filename + VAD math) | src/transcript_[[utils.py](http://utils.py)](http://utils.py) |
| Q&A grounding | src/lambda_ask_[[agent.py](http://agent.py)](http://agent.py) |
| User/role/site mapping | config/user_mapping.json |
| AWS infra (resource names, IAM, scheduling) | template.yaml, [[ARCHITECTURE.md](http://ARCHITECTURE.md)](http://ARCHITECTURE.md) |
| Known traps the FE has hit | [[CLAUDE.md](http://CLAUDE.md)](http://CLAUDE.md) (BUG-01 through BUG-34) |
Anything in this doc that contradicts those files should be treated as a doc bug. Open the file, take its word, and update this file.
