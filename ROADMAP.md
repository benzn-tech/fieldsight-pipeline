# FieldSight Product Roadmap

> Last updated: 2026-03-22
> Owner: Ben
> Status tracking: ⬜ Not started | 🔲 Blocked | 🟡 In progress | ✅ Done

---

## Speaker identity — roadmap (owner decisions, 2026-09-29)

Where the voiceprint work goes next. Ordered by value, not by ease. Items marked
**parked** are decided-for but not scheduled; they stay here so they are not lost.

### Know who is speaking without a rename
- **Candidates narrowed to who is on site today.** Compare a voice against the few
  people signed in, not the whole company. Needs upstream integrations: SignOnSite,
  1Breadcrumb, Outlook/Teams calendar attendees.
- **Enrol at site sign-in.** 15–20 s of natural speech (name, company, trade, today's
  task), not a read-out script; the embedder keeps the cleanest 10 s. Consent is taken
  in the same act.
- **Self-introduction → name proposal.** "Hi, this is Petros from Cassidy" proposes
  that voice as Petros via the bell.
- **Voice sign-in on shared devices.** Replaces or backs up QR login; devices rotate
  monthly.

### A better library
- **Condition-aware profiles.** Samples clustered by condition (wind, indoor, vehicle,
  phone, language); a turn is scored against each person's nearest condition centroid.
- **Bell asks the most informative passages first** (scores near the threshold), so a
  company reaches the 20 corrections that calibrate its floor sooner.

### Using the identity
- **Who said it ≠ who owns it.** A task records the speaker (resolved by voiceprint or
  by the site roster), the company/trade it binds, and the GC-side owner (the SM/PM for
  that package). A subcontractor who is not in the library is never made the task owner.
- **Variation and safety evidence.** Who instructed extra work; who gave and who
  acknowledged a toolbox talk.

### Privacy and trust
- **Speaker-based opt-out.** A person who opts out once is redacted from every later
  recording automatically, not by a one-off deletion.
- **Self-serve consent page — parked.** The person named gets a link to see, hear and
  withdraw their own voiceprint.

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
| DynamoDB tables | ⚠️ Defined, OFF | Set `ENABLE_DYNAMODB=true` + backfill |
| transcript_utils.py | ✅ Deployed | Bundled with meeting minutes Lambda |
| Report generator v3.4 | ✅ Production | Needs transcript_utils migration later |
| Transcribe callback | ⚠️ Partial | Step 5 function name mismatch |
| Face blurring pipeline | ⬜ Designed | Fargate-based, not built |
| Batch H264 conversion | ⬜ Script ready | `batch_convert_h264.py` |
| Audio normalization | ⬜ Designed | EBU R128, needs validation |
