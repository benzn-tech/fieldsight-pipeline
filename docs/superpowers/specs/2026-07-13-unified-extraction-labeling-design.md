# Unified Extraction + Rich Labeling + Correction Loop — Design Spec

**Date:** 2026-07-13
**Status:** Design approved in direction (user, 2026-07-13). Meaty backend redesign — write plan next, but the **authority migration (§6) and entity/severity reliability (§9) must be piloted before locking the schema.**
**Account/region:** 509194952652 / ap-southeast-2, `fieldsight-test` only.
**Related, reused:** programme-matcher / `programme_progress_suggestions` (2026-07-12) — the programme-impact link reuses it. Connects to the parked `docs/dashboard-first-inversion` direction.

## 1. Goal

Stop routing recordings into "meeting" vs "site inspection" at the FILE level (which today is an infra accident, not a content decision — see §2). Instead: **one uniform extraction flow** whose backend agent produces a **rich per-topic structure** and — the genuinely new, valuable signals — **who is responsible (entity)** and **how it hits the schedule (programme impact)**, so a manager can see "which entity/issue is hurting my project." Human corrections on the web/desktop flow back to improve the agent so systematic errors stop recurring. Real-time extraction becomes authoritative for the item store.

## 2. Current state (verified 2026-07-13) — why the split is wrong

- **The meeting/inspection "split" is NOT content-based.** `report-generator` (site) runs automatically on every transcript (EventBridge cron 05:00 NZDT, `template.yaml:499-519`). `meeting-minutes` has **zero automatic trigger** (no `Events:` block, absent from `wire-s3-events.sh`) — it only runs via a **manual CLI invoke** with `{meeting_title, attendees, meeting_type}`, racing the cron (BUG-18 `.meeting_manifest.json` is a human-discipline race, not enforced). "Inspection" is just the default fate of any transcript nobody manually pulled into a meeting run. Whole-date/whole-prefix, all-or-nothing — a walk containing a toolbox talk can't be represented.
- **Neither prompt classifies discourse type** (finding/discussion/decision/action). They differ only by domain category (site: safety/progress/quality; meeting: strategy/finance/hr/… 9 buckets + key_decisions/open_questions/follow_ups). The "precision" the split supposedly bought does not exist.
- **`extract-session` (Phase 4b) already runs uniformly on every transcript** (`wire-s3-events.sh` transcripts/ trigger) with one prompt, category `safety|progress|quality` — BUT it is **not authoritative**: the nightly report path (`lambda_ingest`) `delete_topics_for_source_prefix("extractions/{user}/{date}/")` and re-writes from `daily_report.json`; `item-writer` skips if the report is already ingested. So 4b is a provisional fast-path overwritten nightly.
- **Meeting richness is collapsed downstream**: `meeting-minutes.convert_to_daily_report_format` projects meetings back into `daily_report.json`, mapping 9 categories → progress/quality (**safety structurally unreachable**), overloading the `site` field with the meeting title; the UI never reads `_report_metadata.source`. A full `meeting_minutes.json` exists but is disconnected from Today/Timeline.
- **Today/Timeline reads only `reports/{date}/{user}/daily_report.json`** (`lambda_fieldsight_api.py:256`), NOT Aurora topics. Programme-feedback / RAG / /live-items / dashboards read Aurora topics. Two consumers, two truth sources.

## 3. Design decisions (settled with the user)

- **D1 — Unified flow.** Extend `extract-session` into THE single classifier over every transcript. Drop file-level meeting/inspection routing. (report-generator/meeting-minutes deprecated for the item store — see §6.)
- **D2 — Rich per-topic structure, not a flat "kind" label.** Merge the site + meeting schemas into ONE: a topic holds typed children — `findings` (safety/quality/progress observations), `decisions`, `actions`, `questions`. Don't invent a new enum; merge what already exists.
- **D3 — Two NEW signals per finding (the real value):** `entity` (responsible party) + `programme_impact` (linked task + severity). §4.
- **D4 — `origin` is DERIVED, soft, not a routing label.** Infer inspection-ish (has on-site safety/quality findings) vs meeting-ish (pure discussion/decisions, online) from content, store it as a hint for filtering — never as a hard upfront route.
- **D5 — entity granularity: LOOSE first.** Free-text `{name?, trade?}`, null when the transcript doesn't identify a party. No rigid subcontractor registry up front — the correction loop (§7) + a learned glossary converges it over time.
- **D6 — Real-time extraction authoritative** (the user's "option B"). §6.
- **D7 — Correction loop in TWO phases.** Phase 1 = editable + audited item store (web/desktop, permissioned). Phase 2 = distill corrections back into the extraction prompt. §7.
- **D8 — Single strong prompt first, agentic only if needed.** Start with one Claude call producing the rich structure; escalate to a segment→classify→merge agentic workflow only if long/mixed recordings prove single-prompt insufficient (measure on the pilot).

## 4. The unified extraction schema (merged, enriched)

Extend `extract-session`'s `EXTRACTION_SCHEMA`. Per topic:
```
topic:
  title, summary
  origin: 'inspection' | 'meeting' | 'mixed'   # DERIVED from content, soft
  findings: [                                   # safety/quality/progress observations
    { text, domain: 'safety'|'quality'|'progress',
      severity: 'none'|'minor'|'major',         # schedule/impact severity (see §9 reliability)
      entity: { name: <str|null>, trade: <str|null> },   # responsible party, loose, null-ok
      programme_impact: { task_id: <str|null>, severity: 'none'|'minor'|'major', note } # via matcher
    }, ...]
  decisions: [ { text, rationale, decided_by } ]   # from the meeting schema
  actions:   [ { text, responsible, deadline, priority } ]  # existing action_items
  questions: [ { text } ]                          # open questions
```
- `findings` merge/replace the current `safety_flags`; `domain` covers safety/quality/progress so a walk's quality + safety observations both land here (the user's "inspection has a lot of safety/quality").
- `decisions`/`questions` are NET-NEW to the site/4b path (they only existed in meeting-minutes) — this is the merge value: a site walk's decisions are now captured too.
- `programme_impact.task_id` is populated by **reusing the programme-matcher** (topic/finding → task). The matcher already exists; this feeds it and stores the link + a severity.
- Store a **provenance/edit layer** (§7): each field carries `source: 'ai' | 'human'` + audit, so human corrections aren't re-overwritten on re-extraction.

**CORRECTION (2026-07-14, Task 5 of `docs/superpowers/plans/2026-07-13-programme-impact-link.md`, verified against the shipped implementation):**
- `programme_impact` is **Aurora-side enrichment only — it is NEVER written back into the S3 extraction JSON** (`extractions/{user}/{date}/{session}.json`). The schema sketch above draws it inside the per-topic finding, which reads as if the extractor emits it; it does not. The finding→task link is computed DOWNSTREAM of item-writer (matcher, then the in-VPC writer applies it as `UPDATE findings SET programme_task_id=…` — migration 0010, `src/repositories/findings.py:apply_impact`). Writing `programme_impact` into the extraction JSON would re-trigger the `fs-write-extractions` S3 event on the same key → an infinite ingest loop (the BUG-13 family in the root `CLAUDE.md`). See plan D6.
- The shipped extractor's per-finding text field is **`observation`**, not `text` as drafted above (`src/lambda_extract_session.py` `EXTRACTION_SCHEMA`; `src/repositories/findings.py`). Treat those two files, not this sketch, as the source of truth for field names going forward.

## 5. Data model changes
- Aurora item store (`topics` + children, migration 0003 / 0006 lineage): add `findings` with `domain`/`severity`/`entity_name`/`entity_trade`/`programme_task_id`/`impact_severity`; add `decisions`, `questions` child tables (or a typed `topic_items` table). Add `source` (`ai`/`human`) + `edited_by`/`edited_at`/`ai_original` (jsonb, to preserve what the AI first said vs the human correction — both for audit and for §7 distillation).
- A `label_corrections` table (or reuse the edit-audit) capturing (topic/field, ai_value, human_value, corrected_by, at) — the training signal for Phase 2.

## 6. Authority migration (the delicate part — pilot carefully)

Make `extract-session` (Aurora topics) authoritative for the ITEM STORE:
- **Stop the nightly overwrite:** `lambda_ingest` must NOT `delete_topics_for_source_prefix("extractions/…")` and must NOT overwrite extraction-sourced topics; `item-writer` drops its "skip if report ingested" gate. Extraction topics persist.
- **Decouple the report DOCUMENT from the item store:** `report-generator` can keep producing `daily_report.json` as a *human-readable report artifact* (Word/PDF/JSON), but it no longer feeds the item store. `meeting-minutes` file-routing is retired (its decision/question extraction is now in the unified schema §4).
- **Migrate Today/Timeline off `daily_report.json`:** today `/api/timeline` reads only `daily_report.json`. To make the authoritative labeled topics show in Today/Timeline, `/api/timeline` (or the UI) must read the Aurora topics (via `/live-items`-style access) instead of / in addition to the report JSON. **This is the riskiest change** — it changes the read path for the main UI. Options: (a) a new topics-backed timeline endpoint the UI switches to; (b) a compatibility shim that renders topics into the `daily_report.json` shape the UI already consumes. Recommend (b) for a low-risk first cut, (a) as the clean end state. **Pilot on TEST before flipping.**
- Human-corrected topics (`source='human'`) are NEVER overwritten by a re-extraction (protect them, mirroring how `programme_progress_suggestions` protects decided rows).

**Breadcrumb for whoever plans this flip (from Task 5 of `docs/superpowers/plans/2026-07-13-programme-impact-link.md`, 2026-07-14):** `findings` (migration 0010) + their programme-impact link columns are live on TEST today, but they attach to the PROVISIONAL extraction topics and are wiped every night by `lambda_ingest`'s `delete_topics_for_source_prefix("extractions/…")` supersession — impact links are **same-day-only** until this section's authority flip ships (plan D5). When the nightly overwrite stops:
  1. findings/impact links become persistent automatically — no rework needed, since `apply_impact` is an idempotent keyed UPDATE by design (plan D4);
  2. the transitional double-write of safety-domain findings into BOTH `safety_observations` (legacy bridge, PR #46 `_derive_safety_flags`) and `findings` (0010) can be retired — repoint `src/repositories/rollup.py`'s counts at `findings` BEFORE dropping the bridge, or rollup counts silently zero out (plan D8);
  3. revisit whether the report-path artifact (`lambda_ingest` → item-writer, from `daily_report.json`) should also carry `findings` — today only session-sourced (`extractions/…`) artifacts do, so even post-flip, report-sourced topics will show `findings: []` unless the report path is extended too.

## 7. Correction loop (two phases)

**Phase 1 — editable + audited item store (web/desktop only):**
- An org-api edit endpoint on a topic/finding/label: change domain/severity/entity/programme-impact/text, gated by permission (worker can edit their own site's items? site_manager broader? admin all — confirm the RBAC), **not exposed on mobile**. Every edit writes `source='human'`, `edited_by/at`, and stashes `ai_original`. Audited, queryable.
- The human value becomes authoritative and is protected from re-extraction overwrite (§6).

**Phase 2 — distill corrections back into the agent (active learning, prompt-level):**
- Periodically (or on a threshold) read `label_corrections`, distill into:
  - **few-shot examples** injected into the extraction prompt ("recording said X → correct label is Y");
  - a **learned glossary / alias map** ("'老张的队' → waterproofing subcontractor XYZ"; "'that slab' → task T-12") added to the prompt context.
- This is **prompt-level active learning, not fine-tuning** (Claude API). It stops *systematic* recurring errors (a consistently-misidentified sub/term), which is exactly the "won't recur" the user wants. Store the distilled artifact in `config/` (hot-swappable like the existing prompt templates).

## 8. Phased implementation plan
1. **Unified schema + prompt (extract-session)** — merge site+meeting structure (findings/decisions/actions/questions) + derived origin; keep authority unchanged (still overwritten) so it's a safe, isolated first step. Pilot the extraction quality on real transcripts.
2. **Entity + severity extraction** — add to the prompt/schema; **calibrate on real data** (§9) before locking enums.
3. **Programme-impact link** — wire findings → programme-matcher (reuse), store link + severity.
4. **Authority flip** — stop the nightly overwrite; protect human edits; Today/Timeline compatibility shim (§6). The risky one — its own review + TEST pilot.
5. **Correction Phase 1** — edit+audit endpoint + web UI + RBAC + `source`/`ai_original` provenance.
6. **Correction Phase 2** — corrections → distilled few-shot + glossary → prompt. Measure recurrence drop.
7. Deprecate/retire meeting-minutes file routing + correct the stale docs (CLAUDE.md architecture diagram, lambda docstrings) that describe non-existent triggers.

## 9. Open questions & risks
- **Entity + severity reliability (biggest risk).** "Who's responsible" and "does this delay the task" are often not stated in the transcript. Expect frequent null/none (fail-open). **Pilot on real recordings (Task 1/2) and measure precision before committing the schema** — don't lock enums first.
- **Authority migration risk.** Today/Timeline reads `daily_report.json`; flipping to topics is delicate. Compatibility shim first; TEST pilot; keep the report document as a separate artifact.
- **RBAC for corrections.** Who can edit which items? (worker: own site; site_manager: managed sites; admin: all?) — confirm.
- **agentic vs single-prompt** — decide from Task 1 pilot quality.
- **overlap with programme-feedback** — programme_impact reuses the matcher; ensure one link table, not two.
- Connects to the parked dashboard refactor — the rich labels enable a clearer dashboard; sequence accordingly.
