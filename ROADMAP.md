# FieldSight Product Roadmap

> Last updated: 2026-06-03
> Owner: Ben
> Status tracking: ⬜ Not started | 🔲 Blocked | 🟡 In progress | ✅ Done
> Detailed phased plan + file:line evidence: see **DASHBOARD-FIRST-INVERSION.md**

---

## ★ North Star — Dashboard-first (item store = source of truth)

**Direction (in-flight):** Invert today's *report-first* model — where `reports/{date}/{user}/daily_report.json` IS the dashboard payload that `get_timeline` returns verbatim — into a **dashboard-content-first** model:

- Generation **first** writes the structured **item store** in DynamoDB — `ITEM#`, `TODAY#`, and (new) `DEADLINE#` rows — as the authoritative commit.
- The **report** (`daily_report.json` / `.docx`) becomes a **secondary, on-demand, frozen projection** of those rows, used for storage & accountability (追责) — not the thing that creates dashboard content.
- **Dashboard, Today, and Search all read the same item store.** During migration `daily_report.json` stays **byte-compatible** so the API contract and the UI `today-adapter` keep working untouched.

See **DASHBOARD-FIRST-INVERSION.md** for the phased migration (Phase 0–4), contract deltas, and the full risk table. Verified code defects this surfaced are logged as **BUG-35..BUG-41** in CLAUDE.md.

---

## P0 — Ask Agent (对纪要追问)

**Goal:** Users can ask questions about any report/meeting minutes and get answers grounded in transcript + report data.

**Status:** ⬜ Not started

**Model choice:** Use cheaper model (Haiku 4.5 or equivalent) — this is retrieval + summarization, not complex reasoning. Reserve Sonnet for report generation.

**Architecture:**
```
Frontend chat input → API Gateway → lambda_ask_agent.py
  1. Load report JSON from S3: reports/{date}/{user}/daily_report.json
  2. Load raw transcript(s) from S3: transcripts/{user}/{date}/*.json
  3. Normalize via transcript_utils.normalize_transcript()
  4. Build prompt: system context + report JSON + transcript text + user question
  5. Call Claude Haiku → return answer
  6. Stateless — no conversation memory needed (each question is independent)
```

**Key decisions needed:**
- [ ] Scope: per-report only, or cross-date search?
- [ ] Frontend: inline chat panel in report view, or separate page?
- [ ] Rate limiting: per-user query cap?

**Estimated effort:** 1-2 days (Lambda + API Gateway + frontend chat input)

**Dependencies:** None — all infrastructure exists

---

## P0 — Knowledge Base Search (知识库检索)

**Goal:** Global search across all reports — find any topic, action item, safety flag, decision by keyword across all dates and users.

**⬆ Priority raised + reframed (2026-06):** This is the **primary human interface**, not a side feature. The "report nobody reads" insight: a daily report is a compliance substrate; humans actually interact by *asking in plain language* ("tell me what was done yesterday and why"). So search/Ask becomes the front door, and the report demotes to an on-demand compliance export.
- **Retrieval = hybrid** over the **item store**: lexical/BM25 (names, RFI numbers, the NZ vocab) + **semantic embeddings** + structured filters (site/date/person/category/safety). Then **RAG feeds the Ask Agent** (retrieve ~10–50 relevant items → answer with citations).
- **Context window is NOT the lever.** Do **not** stuff a year of transcripts into context — a single site is ~5–12M+ tokens/year, beyond 1M with poor recall. 1M is for (a) retrieved slices per query and (b) *bounded* synthesis ("everything about foundations last month"). Open-ended recall = the index, not the window.
- **Destination:** a small managed **OpenSearch** domain or **Bedrock Knowledge Bases** (don't hand-roll vector search); mind OpenSearch Serverless' cost floor at this scale.
- **Depends on:** the item store (Inversion Phase 1).

**Status:** ⬜ Not started

**Current state of DynamoDB:**
- ⚠️ `ENABLE_DYNAMODB` = **false** (default in code, not set in src/template.yaml)
- Tables defined in SAM template: `fieldsight-items`, `fieldsight-reports`, `fieldsight-audit`
- Write functions exist in `lambda_report_generator.py` (lines 736-820) — gated behind flag
- **No data has been written yet** — turning on only affects future reports

**Phase 1 — Enable + Backfill:**
- [ ] Set `ENABLE_DYNAMODB=true` in Lambda env vars (report generator + meeting minutes)
- [ ] Run backfill: invoke report generator with `{"report_type": "daily", "force": true}` for past 30 days
- [ ] Verify items appear in DynamoDB console

**Phase 2 — Search API:**
```
GET /api/search?q=concrete+pour&from=2026-01&to=2026-03
→ lambda_search.py
→ DynamoDB scan with FilterExpression on topic_title, summary, key_decisions
→ Return matching topics with report links
```

**Phase 3 — Flat Knowledge Base (长期):**
- OpenSearch or PostgreSQL full-text search for cross-date, cross-site retrieval
- Embedding-based semantic search if keyword matching isn't enough
- Feed into Ask Agent for grounded cross-report Q&A

**Estimated effort:** Phase 1: 2 hours | Phase 2: 1 day | Phase 3: 1 week

**Dependencies:** Phase 1 is prerequisite for Phase 2

---

## P1 — Custom Vocabulary (自定义行业词库)

**Goal:** Improve transcription accuracy for NZ/AU construction terminology.

**Status:** ⬜ Not started

**How it works:**
- AWS Transcribe supports [Custom Vocabulary](https://docs.aws.amazon.com/transcribe/latest/dg/custom-vocabulary.html)
- Upload a vocabulary table (TSV) to S3
- Add `VocabularyName` parameter to `lambda_transcribe.py` `build_transcribe_params()`
- Zero code change beyond one parameter addition

**TODO:**
- [ ] Research NZ/AU construction terminology (BRANZ, NZS standards, trade terms)
- [ ] Build vocabulary TSV: columns = Phrase, SoundsLike, IPA, DisplayAs
- [ ] Terms needed: GIB, BRANZ, dwang, nog, purlin, soffit, fascia, sarking, DPM, H1/H3/H5, NZBC, CCC, PS1/PS4, LBP, PIR, PIMs, RFI, EOT, PC Sum, Provisional Sum, variations, defects liability, practical completion, weathertightness, E2/AS1, Roskill, Hiab, Acrow props, boxing, falsework
- [ ] Upload to S3: `config/custom_vocabulary_construction_nz.txt`
- [ ] Add to transcribe params: `VocabularyName` in `lambda_transcribe.py`
- [ ] Test on 5 diverse recordings, compare before/after

**Estimated effort:** Research: 2 hours | Implementation: 30 min | Validation: 1 hour

**Dependencies:** None

---

## P1 — One-Pager Report (HTML)

**Goal:** Auto-generate a single-page visual summary — executive-friendly, scannable, embeddable in frontend.

**Status:** ⬜ Not started

**Ben's direction:** HTML over PPT — better fit for frontend embed, no download required, responsive, printable.

**Approach: HTML one-pager served via CloudFront**
```
report JSON exists
  → lambda_onepager.py reads JSON
  → Renders Jinja2 HTML template:
     - Executive summary bullets
     - Safety risk heatmap (high/med/low counts)
     - Top 3 action items with owners
     - Timeline bar (topics across the day)
     - Key decisions
  → Saves: reports/{date}/{user}/daily_onepager.html
  → Frontend: iframe embed or link, direct print support
```

**Why HTML > PPT:**
- Direct frontend embed — no download step
- Responsive on mobile
- Browser print = PDF export for free
- Interactive (collapsible sections, links to full report)
- Zero new dependencies (vs python-pptx)

**Estimated effort:** Template: 1 day | Lambda: half day | Frontend link: 1 hour

**Dependencies:** None — reads existing report JSON

---

## P2 — Near Real-Time Processing + Voice Feedback

**Goal:** Reduce latency from overnight batch to <15 min. Long-term: live voice agent with eyes, ears, brain, and mouth.

**Status:** ⬜ Not started (conceptual)

**Phase 1 — Event-Driven Minutes (~15 min latency):**
- `lambda_transcribe_callback` already deployed (Steps 1-4 done)
- On Transcribe completion → auto-invoke `lambda_meeting_minutes`
- Fix needed: Step 5 function name mismatch (`fieldsight-transcribe` vs `fieldsight-transcribe`)
- Result: meeting ends → 10-15 min → minutes available

**Verified 2026-06 (against `feature/p2-dashboard-digest-qaqc-realtime`):**
- The `AUTO_REPORT` hook (transcribe_callback → async-invoke report generator on COMPLETED) **exists on feature/p2 but does NOT work as coded** → see **BUG-35 / BUG-36** (payload key + username-form mismatch). Fixing it removes the 05:00 report-cron boundary.
- Latency is gated by **two daily cron boundaries, not compute**: (1) the 20:00 RealPTT pull (orchestrator cron), (2) the 05:00 report cron that targets *yesterday*. Levers: land the **fixed** AUTO_REPORT, shrink orchestrator cron to `cron(0/15 * * * ? *)`, and add an optimistic ledger-backed **`GET /api/today`** (processing cards).
- **Multi-segment transcripts cause redundant full-day Claude rebuilds** → **BUG-37**; add debounce/coalescing from day one.
- Drag&drop upload (a new ingestion mouth) bypasses the 20:00 pull entirely — see the new *Drag & Drop Upload + Ingest Normalizer* item.

**Phase 2 — Streaming Transcribe (~30s latency):**
- AWS Transcribe Streaming API (WebSocket)
- Device streams audio → handler → real-time transcript accumulation
- Trigger incremental analysis every N minutes

**Phase 3 — Voice Agent (Ben's full vision):**
```
Camera (eyes)    → video stream → frame analysis (safety, context)
Mic (ears)       → audio stream → real-time transcribe → conversation understanding
Claude (brain)   → synthesize visual + audio → generate insights/warnings
Earpiece (mouth) → TTS feedback → "注意: Block C scaffold inspection overdue"
                                → "Reminder: concrete pour in 30min"
                                → Real-time meeting participation & feedback
```

**Architecture considerations:**
- Persistent connection (WebSocket/gRPC), not request/response
- Edge compute for latency (AWS Wavelength or on-device inference)
- Cost: streaming Transcribe ~2x batch pricing
- Privacy: continuous analysis vs triggered recording
- Agent meeting participation: Claude joins as a "participant" providing real-time input

**Estimated effort:** Phase 1: 2 hours | Phase 2: 1 week | Phase 3: multi-month R&D

**Dependencies:** Phase 1 needs callback fix. Phase 3 needs device independence from RealPTT.

---

## P0 — Report→Dashboard Inversion (item store = source of truth)

**Goal:** Make the dashboard item store the primary artifact; the report becomes an on-demand frozen projection. File-level steps: **DASHBOARD-FIRST-INVERSION.md §6**.

**Status:** ⬜ Not started

**Phases (each independently shippable):**
- **Phase 0 — Same-day latency wins:** land the *fixed* AUTO_REPORT (BUG-35/36), orchestrator cron `0/15`, optimistic `GET /api/today`, debounce (BUG-37/38).
- **Phase 1 — Shadow-materialize:** set `ENABLE_DYNAMODB=true`; write `ITEM#` **+ new `DEADLINE#` + `TODAY#`** rows (BUG-39) before the S3 report put; dual-write, nothing reads it yet.
- **Phase 2 — Report→projection:** split `build_digest()` / `project_report_from_items()`; render byte-compatible `daily_report.json` from rows (model on `lambda_meeting_minutes.convert_to_daily_report_format`); freeze the `.docx` export for 追责.
- **Phase 3 — Read cutover:** serve dashboard/Today from item rows (timeline shape preserved, report-file fallback). ⚠️ **access control moves from `{user}`-folder isolation to query-layer filtering — BUG-25 risk.**
- **Phase 4 — Incremental materialize:** per-meeting append/merge (idempotent on `source_transcript_keys`) instead of whole-day Claude rebuild; optional server-side rollups.

**Dependencies:** Phase 1 unlocks BOTH the dashboard read-cutover AND the Knowledge Base Search above.

---

## P1 — Fast Mode (quick to-do-list)

**Goal:** For an on-site SM⇄PM meeting, surface a shareable, actionable to-do list in *minutes* (not hours), before the full analysis finishes.

**Status:** ⬜ Not started

**Approach:**
- Ingest **audio-only + images** (defer video); **VAD-chunk parallel transcription** to reach minute-level (reuse `lambda_vad.py` segments → parallel `StartTranscriptionJob`).
- Summarize with **Haiku 4.5** → a flat action list (skip the heavy report structure).
- Write **provisional** `ITEM#`/`TODAY#` rows (`provisional:true`, `confidence`, `materialized_at`); the full pipeline later **idempotently reconciles** them (Phase 4 mechanism, keyed by `source_transcript_keys`).
- Surface as Today cards with a "draft / 待复核" badge + a **share/export** action (see Export to Email).

**Bottleneck:** the transcription floor. **v1** = batch + VAD-chunk (minute-level). **v2** = streaming/Whisper for sub-minute first response — higher cost/complexity; note that *streaming only helps live capture, not finished file uploads* (it is paced to real-time).

**Dependencies:** item store (Inversion Phase 1) for provisional rows; Export to Email for the share-out.

---

## P1 — Drag & Drop Upload + Ingest Normalizer

**Goal:** Let users upload their own video/audio/images directly; lowest-latency ingestion path (bypasses the 20:00 RealPTT pull).

**Status:** ⬜ Not started

**Approach:**
- Presigned **multipart direct-to-S3** upload to a staging prefix (e.g. `uploads/{cognito_sub}/raw/`).
- A small **`ingest-normalizer` Lambda** derives: **user** ← Cognito identity (more reliable than filename), **type** ← MIME, **date/time** ← media metadata (EXIF `DateTimeOriginal`, MP4/MOV `creation_time`) → fallback to user-provided **coarse range (date + half-day)** → fallback to upload time.
- Normalizer copies the object to the canonical key `users/{display_name}/{type}/{date}/...` so the **existing S3-event pipeline picks it up unchanged**.

**Watch-outs:** timezone normalization to the NZDT date convention; dedup by content hash; large-video multipart (don't proxy bytes through Lambda).

**Dependencies:** none for the path itself; pairs naturally with Fast Mode.

---

## P2 — Export to Email (SES)

**Goal:** One-click "send to email", available contextually across panels (Today, dashboard, report, meeting, search results).

**Status:** ⬜ Not started

**Approach:**
- One shared UI `ExportButton` component, parameterized by `{kind, id}`.
- Backend `POST /api/export {kind, id, recipients, format}` → renders + **freezes an immutable snapshot** (keyed by `generated_at`) so the emailed artifact can't drift from later QA/QC edits → **SES** send → log to **`AuditTable`** (who / what / recipients / when — itself part of the accountability chain).
- Only export what the caller can see (rides on the same access control).

**Dependencies:** SES verified sender domain; frozen-projection (Inversion Phase 2).

---

## Completed Items (2026-03-22 session)

- ✅ `lambda_meeting_minutes.py` v1.1 — generic meeting minutes generator
- ✅ `transcript_utils.py` — shared transcript normalization (unified time extraction)
- ✅ `prompt_templates_meeting.json` v1.1 — meeting prompt templates
- ✅ Offset-aware timestamp extraction (VAD segments + full audio)
- ✅ Per-speaker-turn absolute timestamps from Transcribe word items
- ✅ Attendee name constraint (explicit attendees override device mapping)
- ✅ Executive summary as bullet array
- ✅ Daily report compat layer (meeting minutes → frontend-compatible JSON)
- ✅ Full audio re-transcription workflow (bypass VAD for meetings)
- ✅ Transcript truncation fix (20K → 120K chars, dynamic max_tokens)

---

## Infrastructure Status

| Resource | Status | Action needed |
|---|---|---|
| DynamoDB item store | ⚠️ Defined, OFF | `ENABLE_DYNAMODB=true` + write `DEADLINE#`/`TODAY#` (BUG-39); becomes source of truth — Inversion Phase 1 |
| transcript_utils.py | ✅ Deployed | Bundled with meeting minutes Lambda |
| Report generator v3.4 | ✅ Production | Needs transcript_utils migration later |
| Transcribe callback | ⚠️ Partial | Step 5 name mismatch; AUTO_REPORT broken-as-coded (BUG-35/36); no `'reported'` ledger writer (BUG-40) |
| Face blurring pipeline | ⬜ Designed | Fargate-based, not built |
| Batch H264 conversion | ⬜ Script ready | `batch_convert_h264.py` |
| Audio normalization | ⬜ Designed | EBU R128, needs validation |
| AUTO_REPORT event hook | ⚠️ On feature/p2, broken | Fix payload key + username form (BUG-35/36) + debounce (BUG-37) before landing |
