# Life-Conversation Separation — Design (2026-07-21)

**Status:** Design / for review.

**Parent:** This is the concrete, buildable slice of Filter 2 (non-work removal)
from `2026-07-17-content-filter-privacy-system-design.md`. It **locks the open
choices** for life-separation and **narrows scope** to what ships now. F1
(profanity/PII masking) and the `review_state` publish-gate (§3.5 of the parent)
are explicitly **out of scope** here (see §9).

**Scope:** fieldsight-pipeline (extraction-time classification, `redactions` +
`classification_feedback` stores, company-tier enforcement) + fieldsight-ui
(review buttons on the timeline, hidden/recoverable display of removed topics).

---

## 1. Problem & intent

Field recordings capture personal / off-work conversation (lunch, weekend, family
talk) alongside site work. Today every extracted topic flows into
topics/findings and into company-tier analytics + cross-project RAG with no
separation. The customer needs personal conversation **kept out of later
team/company analysis and RAG**, while the **record is preserved** (recoverable,
auditable) — never destroyed, never used to train on the personal content.

The user also wants a **feedback signal**: when a human confirms the machine
correctly identified personal talk, that *verdict* should improve the classifier
over time **without ever re-ingesting or training on the personal content
itself**.

## 2. Decisions locked in this brainstorm

- **Approach 1 (lean)** — classify + flag + human-confirm + soft-exclude. **No**
  `review_state` publish-gate; work content is never blocked, flows to the
  company tier immediately as it does today. Only *suspected-personal* topics are
  held back.
- **Granularity: topic-level** (a whole topic is `work` or `non_work`), with an
  `is_mixed` flag reserved to drive a future segment-level upgrade. No new
  segment/turn table now.
- **Never auto-delete**: `non_work` is flagged (soft); a human confirms before it
  becomes a permanent redaction.
- **Feedback = verdict only, never content**: the human decision (classifier
  right / wrong / missed) is stored as metadata + confidence; **zero personal
  text** is retained in the feedback store, and personal content is never
  embedded into RAG or fed to any training/tuning input.
- **Confirmed-personal display: hidden + recoverable** — removed from the normal
  minutes flow, retained in the DB, viewable in a "removed / personal" audit area
  and by admin (decision #1).
- **Review buttons on all topics** — not only flagged ones — so a *missed*
  personal topic can still be removed, and that action is captured as a
  false-negative signal (decision #2).

## 3. Classification (extraction-time, no extra LLM call)

The extraction pass (`lambda_extract_session.py`) already sends the transcript to
the LLM and gets back topics. Extend its prompt + output schema so each topic
carries:

- `work_class` ∈ `{work, non_work}` — is this topic site work, or personal /
  off-work conversation?
- `work_confidence` ∈ `[0,1]`.
- `is_mixed` (bool) — the topic contains both work and personal talk (the signal
  that topic-level is too coarse here → future segment-level).

Folding into the existing call (parent E5) avoids a second pass. Conservative
prompt bias: when unsure, classify `work` (a suspected-personal topic is only
*held*, never dropped, so a false `work` is recoverable via the manual button;
we bias the automatic step toward not over-flagging).

## 4. Data model

- **`topics`** gains `work_class` (text, nullable), `work_confidence` (float,
  nullable), `is_mixed` (bool default false). Backfill null = untouched
  (treated as `work` by enforcement — see §6).
- **`redactions`** (from parent §3.4) — the tombstone written when a human
  confirms/removes a topic:
  ```
  redactions(id, company_id,
             target_type   'topic',            -- segment/finding reserved
             target_id,                          -- topics.id
             reason         'non_work' | 'privacy' | free text,
             actor_user_id, actor_role,
             scope          'analysis' | 'all',  -- default 'analysis'
             created_at)
  ```
  Original content is retained; a redaction is revertible by the actor or admin.
- **`classification_feedback`** (NEW — the privacy-preserving feedback loop):
  ```
  classification_feedback(id, company_id, topic_id,
             classifier_verdict     'non_work' | 'work' | null,  -- what the LLM said
             classifier_confidence  float,
             human_verdict          'confirm_non_work'   -- flagged non_work, human agrees (TP)
                                  | 'reject_is_work'      -- flagged non_work, human says work (FP)
                                  | 'missed_personal',    -- NOT flagged, human removes as personal (FN)
             topic_category         text,   -- coarse label only (e.g. 'progress'), NOT the transcript
             actor_user_id, created_at)
  ```
  **Stores no transcript / personal text** — only the two verdicts, the
  confidence, and a coarse category. This is the entire feedback signal.

## 5. Human review — the buttons (fieldsight-ui)

On the timeline/minutes topic detail:

- **Suspected-personal topic** (`work_class=non_work`, no redaction yet): marked
  "疑似个人 · 待确认". Two controls:
  - **确认个人 + 移除** → POST redaction (reason=`non_work`, scope=`analysis`) →
    topic soft-removed from the minutes flow + `classification_feedback`
    (`human_verdict='confirm_non_work'`, TP).
  - **其实是工作** → **sets `topics.work_class='work'`** (human override; no
    redaction) → topic released to company tier / RAG +
    `classification_feedback` (`human_verdict='reject_is_work'`, FP). The
    original LLM verdict is preserved in the feedback row, not on the topic.
- **Any other (work) topic**: a lower-emphasis **标为个人 + 移除** → redaction +
  `classification_feedback` (`human_verdict='missed_personal'`, FN).
- **Removed area**: soft-removed topics are hidden from the default flow but
  listed in a "已移除 / 个人" section (site_manager + recorder + admin), each
  revertible (un-tombstone).

Actor: the reviewer is any **site-authority** on the site (site_manager / pm /
admin), reusing the existing content-edit permission gate (`content:edit` /
graded site authority) rather than inventing a new role — the same people who
already edit content this session.

## 6. Enforcement (single choke point)

A shared helper `exclude_for_company(rows)` (extending the parent's
`exclude_redacted`) drops, from **company-tier reads only**:

1. topics with an active `redactions` row (`scope in ('analysis','all')`), and
2. topics with `work_class='non_work'` (the fail-safe: suspected-personal never
   reaches the company tier before a human looks). The **其实是工作** action flips
   the column to `work`, which releases it; **确认个人** writes a redaction, which
   removes it — so a `non_work` value always means "auto-held, awaiting a human".

`work_class` null or `work`, and no redaction → included, unchanged.

**Read paths that MUST route through it** (company/cross-site/analytics tier):
- `repositories/rollup.py` portfolio/insights aggregators.
- Cross-project RAG **embedding inputs** — `lambda_embed_report.py` /
  `reindex.py` `enqueue_topic_reindex`: a `non_work`/redacted topic is **not
  embedded** (so personal text never enters the vector store), and
  `lambda_ask_agent.py` retrieval already reads only what was embedded.
- Word/report exports and any company-tier dashboard query.

**Site / self tier retains access to every topic** — the site_manager and the
recorder still reach every topic (confirmed-personal ones relocated to the
"已移除 / 个人" area, not the default flow; suspected-personal marked inline), for
timeliness and to run the review. This mirrors the parent §3.5 site-immediate /
company-gated split, but *only* for personal content (no whole-day publish gate).

## 7. Feedback loop (privacy-preserving improvement)

`classification_feedback` accumulates labeled verdicts (metadata only). Uses:

- **Measure** — precision = `confirm_non_work / (confirm_non_work + reject_is_work)`;
  recall proxy from `missed_personal` counts. A simple periodic report (no PII).
- **Tune** — adjust `work_confidence` hold threshold; add **categorical** guidance
  to the extraction prompt (e.g. "lunch / weekend / family talk = non_work")
  derived from aggregate patterns — **never** the raw personal text.
- **Upgrade signal** — a high `is_mixed` rate (or high FP on mixed topics) is the
  quantitative trigger to build segment-level separation.
- **Guarantee** — personal content is never a training/tuning input and never
  embedded; only the human's verdict + confidence + coarse category leave the
  topic.

## 8. Reconciliation with what shipped this session

- **findings single-source (Phase F)** — `non_work` topics carry findings too;
  enforcement excludes them via the topic redaction, so rollup's findings/
  safety_observations union must also honor `exclude_for_company` (add the
  redaction filter to the rollup queries).
- **editable content (Phase D) + content_edits audit** — the review buttons reuse
  the `content:edit` permission and the `orgRequest` PATCH/POST plumbing;
  redaction history lives in its own table but surfaces in the same
  ContentHistory panel.
- **RAG reindex chain (Phase C/E)** — the `reindex_requests/ → embed → ingest`
  chain already re-embeds a topic on edit; confirming/reverting a redaction
  reuses it (a redacted topic enqueues a *delete-only* reindex so its vectors
  are removed; a revert re-embeds).
- **graded roles / contributors (this session)** — the reviewer authority reuses
  `_allowed_site_ids` / site-authority; no new ACL.

## 9. Out of scope (deferred, tracked)

- **F1 profanity/PII masking** — separate concern; not required for
  life-separation.
- **`review_state` open→reviewed publish-gate** (parent §3.5) — gates *all*
  content; deliberately excluded (Approach 1). Personal content is gated by the
  redaction + §6 fail-safe instead.
- **Segment-level separation** — reserved; the `is_mixed` flag + feedback loop
  decide when to build it.
- **Hard-purge job** (parent E4) — off; the tombstone preserves the record.

## 10. Rollout (independently shippable slices)

1. **Data + enforcement backbone**: migrations (`topics` columns, `redactions`,
   `classification_feedback`) + `exclude_for_company` wired into rollup + RAG
   embedding inputs (no UI, no classifier yet) — establishes the choke point.
2. **Classifier**: extend `lambda_extract_session.py` prompt/schema →
   `work_class`/`work_confidence`/`is_mixed`; `lambda_item_writer.py` persists.
   Fail-safe hold becomes live.
3. **Review UI + endpoints**: `POST /api/org/redactions` (+ revert),
   `POST /api/org/classification-feedback`; timeline buttons + removed-area.
4. **Feedback report**: the precision/recall/`is_mixed` roll-up (metadata only).

Ship 1 before 2 so the enforcement exists before anything is flagged.

## 11. Risks

- **Over-flagging** erodes trust → conservative `work` bias + never auto-drop
  (human confirm is the guard) + the FP feedback (`reject_is_work`) surfaces it.
- **Enforcement bypass = privacy leak** → every company-tier read path goes
  through the one helper; tests assert `non_work`/redacted topics are absent from
  each aggregator and from the RAG embedding inputs.
- **Feedback store leaking content** → schema forbids transcript columns; a test
  asserts only verdict/confidence/category are written.
- **Recorder's own personal talk visible to site_manager** — accepted: the
  site_manager is the reviewer and already sees the raw minutes; company tier
  never sees it.
