# FieldSight Product Roadmap

> Last updated: 2026-03-31
> Owner: Ben
> Status tracking: ⬜ Not started | 🔲 Blocked | 🟡 In progress | ✅ Done

---

## P0 — Ask Agent (对纪要追问)

**Goal:** Users can ask questions about any report/meeting minutes and get answers grounded in transcript + report data.

**Status:** ✅ Done

**Model choice:** Claude Haiku 4.5 (retrieval + summarization, not complex reasoning)

**Completed:**
- ✅ `/api/ask` route in API Lambda (proxies to fieldsight-ask-agent)
- ✅ Frontend: inline "ASK ABOUT THIS TOPIC" panel in TopicDetail view
- ✅ Rate limiting: 5 asks/minute per user (in-memory)

---

## P0 — Knowledge Base Search (知识库检索)

**Goal:** Global search across all reports.

**Status:** ✅ Done (Phase 1 + 2)

**Completed:**
- ✅ DynamoDB enabled (`ENABLE_DYNAMODB=true`)
- ✅ Backfill triggered: past 30 days async invoked (2026-02-24 to 2026-03-25)
- ✅ `GET /api/search?q=...&start_date=...&end_date=...&category=...` in API Lambda
- ✅ Frontend: Search tab (🔍 Search) with date range, text, category filters + result cards

**Phase 3 — Semantic Search (长期):** ⬜
- [ ] OpenSearch or PostgreSQL full-text search
- [ ] Embedding-based semantic search
- [ ] Feed into Ask Agent for grounded cross-report Q&A

---

## P1 — Custom Vocabulary (自定义行业词库)

**Goal:** Improve transcription accuracy for NZ/AU construction terminology.

**Status:** ✅ TSV built (129 entries), ✅ lambda_transcribe.py supports VOCABULARY_NAME

**Remaining:**
- [ ] Upload TSV to S3: `config/custom_vocabulary_construction_nz.txt`
- [ ] Create vocabulary: `aws transcribe create-vocabulary ...`
- [ ] Set `CustomVocabularyName` parameter in deployment
- [ ] Test on 5 diverse recordings, compare before/after

**Estimated effort:** 1 hour (deploy + validate)

---

## P1 — Critical Dates Calendar Integration

**Goal:** Auto-link critical dates from reports to the calendar view with visual indicators.

**Status:** ✅ Done

**Completed:**
- ✅ `GET /api/calendar-events?from=YYYY-MM-DD&to=YYYY-MM-DD` API route
- ✅ DynamoDB DEADLINE# query + S3 report JSON fallback
- ✅ CalendarPicker: orange dot for deadlines, red background for high urgency, tooltip on hover

---

## P1 — Topic Priority User Override

**Goal:** Users can adjust topic priority (override Claude's classification).

**Status:** ✅ Done

**Completed:**
- ✅ `POST /api/topics/priority` + `GET /api/topics/priority` API routes
- ✅ DynamoDB PRIORITY# records (preserves original_priority)
- ✅ Inline priority selector in TopicDetail header (▲ High / — Med / ▼ Low)

---

## P1 — One-Pager Report (HTML)

**Goal:** Single-page visual summary, executive-friendly, embeddable in frontend.

**Status:** ✅ Done

**Completed:**
- ✅ `GET /api/onepager?date=...&user=...` API route
- ✅ On-the-fly HTML generation from daily_report.json with S3 caching
- ✅ "📄 One-Pager" button in TopicDetail header, opens in new tab (browser print = PDF)

---

## P2 — Site Dashboard View (站点总览)

**Goal:** Login → see all sites as cards with today's summary → click into timeline.

**Status:** ✅ Done

**Completed:**
- ✅ `GET /api/dashboard` API route (aggregates per-site stats)
- ✅ `SiteDashboard` component: grid of site cards with topic_count, action_count, safety_count, deadlines, top topics
- ✅ "🏗 Sites" tab in NavBar (visible to site_manager/pm/admin only)

---

## P2 — PM/Admin Digest Reports (Agent 5)

**Goal:** Role-specific daily digest tailored to PM/Admin priorities.

**Status:** 🟡 Backend done (API routes ready, digest Lambda not yet deployed)

**Hierarchy:**
```
Worker        → detailed daily topics (existing)
Site Manager  → site daily summary (existing)
PM            → cross-site digest: blocking items, overdue actions, safety trends (NEW)
Admin/GM      → global dashboard + anomaly alerts (NEW)
```

**Architecture:**
```
New report_type: "digest"
  → Input: multiple daily reports across sites
  → Output: prioritized executive view
  → Storage: reports/{date}/digest/{role}/{user}.json
  → Optional: email/Slack push via SNS
```

**Estimated effort:** 2-3 days

---

## P2 — QA/QC Content Correction System (Agent 3)

**Goal:** Users can correct report errors, corrections propagate to weekly/monthly reports, system learns from corrections.

**Status:** ✅ Layer 1 done (user corrections UI + API)

**Three layers:**

**Layer 1 — User Corrections:** ✅
```
User clicks "Edit" on topic → modifies text → saves
  → POST /api/reports/correction → DynamoDB CORRECTION# record
  → GET /api/corrections?date=...&topic_id=... → load corrections
  → Frontend merges corrections on load, shows "✏ Corrected" badge
  → Edit modal with original text preservation
```

**Layer 2 — Upward Propagation:**
```
Weekly report generator checks corrections for source daily reports
  → Injects into prompt: "NOTE: corrected items: ..."
  → Weekly/monthly auto-include latest corrections
```

**Layer 3 — System Memory:**
```
Corrections accumulate → periodic pattern analysis:
  - Transcription errors → add to Custom Vocabulary
  - Classification errors → add to prompt_templates rules
  - config/prompt_corrections.json (append-only rule list)
```

**Scalability:** Minimal impact — corrections are sparse data (<1KB each), prompt additions <500 tokens.

**Estimated effort:** 3-5 days

---

## P2 — Near Real-Time Processing + Voice Feedback

**Goal:** Reduce latency from overnight batch to <15 min.

**Status:** ✅ Phase 1 done (auto-report trigger on transcription complete)

**Phase 1 — Event-Driven Minutes (~15 min):** ✅ Done
- ✅ `lambda_transcribe_callback.py`: auto-invokes report generator (async) on COMPLETED
- ✅ Supports both `realptt_` and `fieldsight_` job prefixes
**Phase 2 — Streaming Transcribe (~30s):** 1 week
**Phase 3 — Voice Agent (full vision):** multi-month R&D

---

## P3 — Analytics Agent (Agent 4)

**Goal:** User behavior analysis — click heatmaps, feature usage, user personas.

**Status:** ⬜ Not started

**Architecture:**
```
Frontend event tracking (clicks, dwell time, feature usage)
  → S3 raw events → Athena / CloudWatch RUM
  → Weekly analytics digest: most-used features, drop-off points
  → User persona clustering: power users, scan readers, deep divers
```

**Estimated effort:** 1-2 weeks

---

## P3 — Official Website + Custom Domain

**Goal:** Separate marketing site from product app.

**Status:** ⬜ Not started

**Architecture:**
```
www.fieldsight.co.nz  → marketing (static, GitHub Pages / Vercel)
app.fieldsight.co.nz  → product (CloudFront → S3, current app)
Route53: manage both subdomains
ACM: SSL certificates for both
```

**Estimated effort:** 1-2 days

---

## P3 — Face Blurring Pipeline

**Status:** ⬜ Designed, not built. Fargate-based.

---

## P3 — Batch H264 Conversion

**Status:** ⬜ Script ready (`batch_convert_h264.py`), not deployed

---

## P3 — Audio Normalization (EBU R128)

**Status:** ⬜ Designed, needs validation

---

## Completed Items

### 2026-03-31 session (P1 + P2 implementation)
- ✅ P1: Critical Dates Calendar — `GET /api/calendar-events`, CalendarPicker deadline indicators
- ✅ P1: Topic Priority Override — `POST/GET /api/topics/priority`, inline selector
- ✅ P1: One-Pager Report — `GET /api/onepager`, on-the-fly HTML generation + S3 cache
- ✅ P2: Site Dashboard — `GET /api/dashboard`, SiteDashboard component, "🏗 Sites" tab
- ✅ P2: Digest Reports — `POST/GET /api/digest` API routes (Lambda TBD)
- ✅ P2: QA/QC Corrections Layer 1 — `POST /api/reports/correction`, `GET /api/corrections`, edit modal + "✏ Corrected" badge
- ✅ P2: Near Real-Time — auto-report trigger in `lambda_transcribe_callback.py`
- ✅ P0+: Global Ask Panel — floating 💬 panel, removed from TopicDetail
- ✅ P0+: Participant display fix — left panel shows owner + count, right shows full badges
- ✅ P0+: Analytics logging — S3 analytics events, `POST /api/analytics/events`
- ✅ P0+: Frontend EventTracker — batch event tracking (7 event types)

### 2026-03-25 session (P0 completion)
- ✅ `/api/ask` route in API Lambda — proxies to fieldsight-ask-agent (sync invoke)
- ✅ `/api/search` route in API Lambda — DynamoDB per-date queries with text/category filter
- ✅ Ask Agent chat panel in TopicDetail ("ASK ABOUT THIS TOPIC" section)
- ✅ Search tab (🔍 Search) in NavBar with SearchTab component
- ✅ Rate limiting: 5 asks/minute per user
- ✅ DynamoDB backfill triggered for 2026-02-24 to 2026-03-25
- ✅ Cleanup: deleted 9 legacy realptt-*/sitesync-* Lambdas, EventBridge rule, Cognito pool
- ✅ Cognito migration: `sitesync-users` → `fieldsight-users` (new pool)
- ✅ API Gateway authorizer updated to new pool
- ✅ DynamoDB user profiles migrated (Ben/Jarley/David with correct subs)
- ✅ S3 web bucket: `sitesync-web` → `fieldsight-web-509194952652`
- ✅ CloudFront origin updated to new web bucket
- ✅ Lambda handler fix: `lambda_sitesync_api` → `lambda_fieldsight_api`
- ✅ Change Password UI (ChangePasswordModal + NavBar button)
- ✅ DragDivider component (reusable vertical/horizontal)
- ✅ LeftPanel ↔ TopicDetail resizable (drag divider)
- ✅ VideoPopup multi-video sequential playback (auto-advance on ended)
- ✅ VideoPopup video/transcript resizable (drag divider)
- ✅ Site filter in /api/timeline (selectedSite passed to API)
- ✅ Participant labels on topic list + TopicDetail header
- ✅ Executive Summary resizable height (DragDivider)
- ✅ `lambda_ask_agent.py` — Haiku-powered report Q&A
- ✅ `custom_vocabulary_construction_nz.txt` — 129 NZ construction terms
- ✅ `lambda_transcribe.py` — Custom Vocabulary support (LanguageIdSettings)
- ✅ Cleanup script for legacy resources (realptt-*/sitesync-*)

### 2026-03-23 session
- ✅ template.yaml — 10 Lambda definitions, all resources unified under fieldsight-*
- ✅ Presigned URL permission check (RBAC enforcement)
- ✅ ENABLE_DYNAMODB=true in report generator
- ✅ SITE_NAME parameterized (not hardcoded)
- ✅ CLAUDE_MODEL unified to claude-sonnet-4-6

### 2026-03-22 session
- ✅ `lambda_meeting_minutes.py` v1.1
- ✅ `transcript_utils.py` — shared transcript normalization
- ✅ `prompt_templates_meeting.json` v1.1
- ✅ Offset-aware timestamp extraction (VAD segments + full audio)
- ✅ Per-speaker-turn absolute timestamps
- ✅ Executive summary as bullet array
- ✅ Full audio re-transcription workflow (bypass VAD for meetings)

---

## Infrastructure Status

| Resource | Status | Notes |
|---|---|---|
| S3 data bucket | ✅ `fieldsight-data-509194952652` | Production |
| S3 web bucket | ✅ `fieldsight-web-509194952652` | Production |
| CloudFront | ✅ `E12IVML224YUEE` | Points to new web bucket |
| Cognito | ✅ `fieldsight-users` (q88pd6XXr) | 3 users: Ben (admin), Jarley (site_mgr), David (site_mgr) |
| API Gateway | ✅ `khfj3p1fkb` | Authorizer: 7npn3y → new pool |
| DynamoDB tables | ✅ Created | `ENABLE_DYNAMODB=true` |
| EventBridge | ✅ `fieldsight-transcribe-state-change` | Active |
| Lambda (10) | ✅ All fieldsight-* | Handlers verified |
| Legacy resources | ✅ Cleaned up | realptt-*/sitesync-* deleted |
| Custom Vocabulary | ⬜ Not deployed | TSV ready, needs `create-vocabulary` |

---

## Agent Architecture (Future)

| Agent | Name | Status | Function |
|---|---|---|---|
| 1 | Pipeline Agent | ✅ Existing | Download → VAD → Transcribe → Report |
| 2 | Ask Agent | ✅ Backend done | Report Q&A (Haiku) |
| 3 | QA Agent | ⬜ P2 | Auto-detect report anomalies, learn from corrections |
| 4 | Analytics Agent | ⬜ P3 | User behavior analysis, personas, usage patterns |
| 5 | Digest Agent | ⬜ P2 | Role-specific daily/weekly digests for PM/Admin |
