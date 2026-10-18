# Voice-Ask personal-activity queries — design (backend)

**Date:** 2026-07-17
**Status:** design draft (from GrandTime app session) — needs review + build in a pipeline session
**Repo:** fieldsight-pipeline (ask-agent). App side unchanged.

## Problem
The hands-free voice Ask (`/api/ask/voice` → prod ask-agent) answers **site-report** questions well — verified on device 2026-07-17: "Tell me about the door damage" → correct specifics ("reported Feb 9 2026, Jarley Trainor documented two damaged doors, emailed Jordan"). But **personal-activity / temporal** questions fail. Real `voice_ask_log` row:

> transcript: "What did I do in the past five days?"
> answer: "I don't have that information in the site reports available to me. The excerpts don't contain details about your personal activities…"

Root cause: the RAG corpus is site-scoped report chunks (semantic vector search over `report_chunks`). There is no "what did *I* (this caller) do over a date range" retrieval path. The agent correctly reports it lacks personal-activity data — the data exists, but not in a form this query reaches. (This is NOT the auth bug fixed app-side the same day, and NOT a data-location issue — prod+test share the Aurora and the data is present.)

## Data available (confirmed via Data API 2026-07-17, shared Aurora `fieldsight`)
- `recordings(id, company_id, user_id, site_id, kind, s3_key, started_at, ended_at, duration_s, …)` — `user_id` = caller identity; `started_at` for date ranges. 89 rows for company FieldSight in the last 5 days.
- `report_chunks(id, site_id, user_id, source_s3_key, topic_id, report_date, chunk_type, chunk_text, embedding, metadata, created_at)` — **has `user_id` and `report_date`**, so per-caller content is directly filterable by date without vector search.
- `topics`, `action_items(topic_id, site_id, text, responsible, deadline, priority, status)`, `findings`, `observations`, `safety_observations`.
- `voice_ask_log(id, company_id, caller_sub, transcript, answer, created_at)` — the caller identity is `caller_sub` (Cognito sub); map to `recordings.user_id` / `report_chunks.user_id`.

## Approaches
1. **Augment context with the caller's recent activity (recommended, simplest).** For every voice ask, in addition to the existing semantic RAG chunks, run a cheap structured query — the caller's `report_chunks` (and/or `recordings` + `action_items` they authored) over a recent window (e.g. last 7 days, `WHERE user_id = <caller> AND report_date >= now()-7d ORDER BY report_date`) — and inject a compact "Your recent activity" block into the LLM prompt. Site questions are unaffected; "what did I do" becomes answerable. Cost: a little latency + tokens per ask; bound the window and row count.
2. **Intent routing.** Classify the transcript as personal/temporal vs site-content (LLM or keyword heuristic: "I/my/did I", date words). Route personal → structured caller+date query; site → existing RAG. Cleaner separation, but adds a classification step and a failure mode (misroute).
3. **Agentic tool.** Give the agent a `get_my_activity(date_range)` tool it calls when needed. Most flexible, most work; overkill for one query shape now.

**Recommendation:** Approach 1 for the first cut (YAGNI) — always attach a small, date-bounded caller-activity block; measure latency/token impact; move to routing (2) only if the always-on block proves too costly or noisy. Whichever is chosen, keep the answer honest when the window is genuinely empty.

## Open decisions (for the pipeline session / user)
- **Window + granularity:** default look-back (7d?) and whether "past N days" in the transcript should override it (parse the number from the transcript).
- **What counts as "what I did":** report_chunks authored by the caller (richest), vs raw recordings list (thin), vs both. Recommend report_chunks (has chunk_text) + a recordings count/duration summary.
- **Scope:** caller-only, or "my sites" (sites the caller has membership on)? Personal-activity ⇒ caller-only by `user_id`.
- **Voice brevity:** the voice system prompt is already the concise variant (per GrandTime SP-Ask design) — the activity summary must stay short for TTS.

## Out of scope
- App changes (none — the app already sends the transcript and plays the answer).
- The auth token-expiry fix (done app-side 2026-07-17).
