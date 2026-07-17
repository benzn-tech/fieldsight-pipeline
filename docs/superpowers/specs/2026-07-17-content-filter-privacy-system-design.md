# Content-Filter & Privacy System — Design (2026-07-17)

**Status:** Design / for review. Pairs with
`2026-07-17-visibility-permission-model-design.md` (they interlock at the
site_manager's authority and the layered review gate).

**Scope:** fieldsight-pipeline (extraction/classification, redaction store,
downstream enforcement) + fieldsight-ui (review UI, masked display). Adds a
three-layer content filter and a privacy-preserving soft-delete so field
recordings can flow to team/company analytics without exposing profanity, PII,
or personal off-work conversation.

---

## 1. Problem & intent

Field recordings faithfully capture everything said on site — including swearing,
personal/off-work chatter, and PII. Today that content flows into topics/findings
and (soon) company-level analytics with no cleansing and no human gate. The
customer (esp. the **site_manager**, who reviews the minutes first after an
inspection) needs:

1. Profanity / uncivil words **masked** in what people read.
2. Non-work conversation **removed** — but at the right **granularity** (a topic
   may hold several conversation segments; only the *personal* segments should
   go, not the whole topic).
3. The **right to delete** a topic or segment so a person's privacy is **not
   pulled into later team/company analysis** — while the **record is still
   correctly preserved** (recoverable, auditable), not truly destroyed.

## 2. Benchmark (how the field does it)
- **Heidi Health** (AI medical scribe): audio discarded immediately after
  transcription; **transcript is ephemeral, the structured note is the durable
  artifact**; the **clinician reviews/edits before it counts** (human is the
  final arbiter); auto **pseudonymization** (name/DOB/address → "Jane Doe") before
  any third-party sharing.
- **Contented.ai** (records → structured docs; used in **construction**):
  **faithful transcription** — explicitly keeps "lots of swearing" verbatim —
  and applies **structured templates** to the transcript rather than generating
  from scratch (no fabrication); never trains on customer data; never stores
  recordings.
- **Takeaways adopted here:** (a) transcribe faithfully, filter/mask at the
  **display and structured layers**, never mutate the raw transcript; (b) the
  **LLM extraction is itself the first non-work filter** (templates only pull
  relevant content); (c) **human review happens before the content is treated as
  official** — which for FieldSight means *before company aggregation*.

## 3. Design — three-layer pipeline

```
raw transcript (faithful, immutable)
      │
      ├─(F1) profanity/PII MASK  ──────────────► display layer (mask on render)
      │
      ├─(F2) work-relevance CLASSIFY (per turn) ─► auto-FLAG suspected non-work
      │                                            → site_manager CONFIRMs → soft-exclude
      │
structured topics / segments / findings
      │
      └─(F3) site_manager REVIEW ───────────────► soft-delete any topic/segment
                                                   (tombstone; excluded from analysis)
```

### 3.1 Filter 1 — profanity / PII masking (display layer)
- Underlying transcript/segment text is stored **faithfully** (Contented model).
- Masking is applied at **render time** and on any surface a human reads
  (minutes UI, topic detail). A `mask(text)` function replaces matched tokens
  with `f***`-style masks.
- Sources: a **profanity lexicon** (Open decision E1: standard list vs
  company-configurable) + **PII detection** (names not in the site roster,
  phone numbers, addresses → mask). PII masking mirrors Heidi's pseudonymization.
- **Also applied to analytics/RAG inputs** so masked tokens never reach
  cross-company embeddings/LLM prompts (Open decision E2: mask vs drop for RAG).

### 3.2 Filter 2 — non-work removal (auto-flag + human-confirm)
- **Granularity = the conversation turn/segment**, not the topic. The transcript
  already carries speaker turns with timestamps (`transcript_utils.normalize_transcript`);
  extraction attaches segments to each topic.
- The extraction step **classifies each segment** as `work` | `non_work`
  (personal life, off-topic banter) with a confidence.
- Segments classified `non_work` are **flagged (soft), not auto-removed** —
  surfaced to the site_manager highlighted. The site_manager **confirms** →
  the segment is soft-excluded (tombstoned, §3.4); **unconfirmed flags stay in
  the minutes** (a suspected-personal segment is never silently dropped).
- This keeps a topic's *work* segments intact while lifting only the confirmed
  personal ones out — answering the granularity concern.

### 3.3 Filter 3 — site_manager human review
- After an inspection, the site_manager opens the day's minutes (they are the
  first reviewer). Heidi-style **tabbed** view: structured minutes ↔ underlying
  segments.
- They can **soft-delete** any **topic** or **segment** (a superset of confirming
  F2 flags), with an optional reason.
- Review is also the **publish gate** (§3.5): reviewing/publishing releases the
  (redacted) minutes to company/team aggregation.

### 3.4 Soft-delete / tombstone model (the "delete but preserve" answer)
Never hard-delete. A redaction is a **tombstone** on the target:

```
redactions(
  id, company_id,
  target_type   ('topic' | 'segment' | 'finding'),
  target_id,
  reason,                       -- 'non_work' | 'privacy' | free text
  actor_user_id, actor_role,
  created_at,
  scope         ('analysis'      -- excluded from team/company analysis, still
                                 --   visible to the site_manager/recorder + admin
                | 'all')         -- hidden from everyone below admin
)
```

- **Original content is retained** (access-controlled), so the record is correct,
  **recoverable**, and **auditable** — answering *"如何保证记录能被正确保存"*.
- **Every downstream read honors the tombstone**: team/company aggregation,
  portfolio/insights roll-ups, **cross-project RAG**, and exports **exclude**
  redacted targets. This is the single enforcement point that keeps privacy out
  of later analysis — answering *"不纳入之后的分析"*.
- **Who still sees it** (Open decision E3): default = the site_manager/recorder
  and admin can still see redacted-for-`analysis` items (marked "excluded by X");
  everyone else cannot. `scope='all'` hides from all non-admins.
- **Recovery**: a redaction can be reverted (un-tombstone) by the actor or admin.
- **Optional hard purge** (Open decision E4): a scheduled job truly deletes
  content tombstoned `> N` days (GDPR-style erasure) — off by default, since the
  stated need is "preserve the record."

### 3.5 Review gate & state model (layered — from companion spec)
- Each daily report / topic set has a state: **`open` (site-immediate)** →
  **`reviewed` (published to company)**.
- **Site/self tier reads everything immediately** regardless of state
  (timeliness — the site_manager and their own site see items as they land).
- **Company/regional aggregation reads only `reviewed` topic sets, minus
  redactions.** So personal content never enters company analysis even briefly:
  it is either redacted before review, or the whole set is unpublished until
  reviewed.
- Auto-flagged (F2) but unconfirmed segments: included at site level, **excluded
  from the company tier until the site_manager reviews** (fail-safe toward
  privacy at the company tier, toward completeness at the site tier).

## 4. Data-model changes
- `redactions` table (above); indexed by `(company_id, target_type, target_id)`.
- `topics` / segment rows gain a derived `is_redacted` read helper (join or
  materialized flag) so hot read paths don't N+1.
- Report/topic-set `review_state` (`open` | `reviewed`) + `reviewed_by` /
  `reviewed_at`.
- Segment-level `work_class` (`work` | `non_work` | null) + `work_confidence`
  from extraction, and `f2_confirmed` (bool) once the site_manager acts.
- No change to the **raw transcript** artifacts (faithful, immutable).

## 5. Enforcement points (must all honor redaction + review_state)
`repositories/topics.list_topics_for_date`, the compliance/tasks/insights/
strategic aggregators, the RAG retrieval/embedding inputs (`lambda_ask_agent`,
embedding jobs), Word/report exports, and any company-tier dashboard query. A
shared `exclude_redacted(rows, tier)` / `company_visible(...)` helper keeps the
rule in one place.

## 6. Rollout
1. `redactions` table + `exclude_redacted` helper wired into aggregation/RAG
   (no UI yet) — establishes the enforcement backbone.
2. F1 masking (display + RAG input).
3. F3 review UI (site_manager soft-delete topics/segments) + `review_state`
   publish gate.
4. F2 auto-classification + confirm UI (needs the extraction-side classifier).
Each step ships independently; F3 before F2 so the human gate exists before any
automated flagging.

## 7. Open decisions (for your review)
- **E1** — profanity lexicon: standard list vs per-company configurable. Recommend
  **standard + per-company additions**.
- **E2** — RAG/analytics input: **mask** profanity/PII vs **drop** the token.
  Recommend **mask** (preserves meaning, hides the word).
- **E3** — post-redaction visibility: site_manager/recorder+admin still see
  (marked) vs fully hidden below admin. Recommend **still see, marked** for
  `analysis` scope.
- **E4** — hard-purge job: on (with N-day timer) vs off. Recommend **off by
  default**, configurable.
- **E5** — does F2 classification run in the existing extraction LLM call
  (cheaper, one pass) or a dedicated pass? Recommend **fold into extraction**.
- **E6** — publish granularity: whole daily report vs per-topic review. Recommend
  **per-report with per-topic redactions**.

## 8. Risks
- **Over-masking / over-flagging** erodes trust — F1/F2 must be tunable and F2
  never auto-drops (human confirm is the guard).
- **Redaction bypass** = privacy breach — every company-tier read path must go
  through the shared helper; add tests asserting redacted/unreviewed content is
  absent from each aggregation and from RAG.
- **Raw transcript retention** vs privacy law — the tombstone keeps originals;
  E4's purge is the escape valve if a customer/jurisdiction requires true erasure.
