# Editable Content Correction (A + D) — Design (2026-07-20)

**Status:** Design / for review.

**Scope:** fieldsight-pipeline (edit endpoints, audit history, re-index hook,
alias store) + fieldsight-ui (inline content editors, history view, glossary
confirm). This is sub-project **A (+D)** of the larger content-correction /
glossary / privacy block (decomposed 2026-07-20). Deliberately OUT of scope:
**B** = feeding aliases to AWS Transcribe Custom Vocabulary (separate follow-on;
this spec only builds the alias *store*), and **C** = the content-filter /
privacy system (its own spec, `2026-07-17-content-filter-privacy-system-design.md`).

---

## 1. Problem & intent

AI transcription (STT) and extraction are not always correct — subcontractor
names, product names, and general phrasing get mis-transcribed. Users need to
**correct the extracted structured content** they read, and have that correction:

1. Persist and be **audited** (who / when / before → after) — never a silent
   overwrite. Matches the user's stated flow: "AI generates → user edits → it's
   recorded in history."
2. **Reflect in RAG** — the "Ask" answers must use the corrected text, not the
   stale original.
3. **Never mutate the raw transcript** — the transcript artifact stays the
   faithful, immutable record (Contented/Heidi model, shared with the C spec).
4. Optionally **feed a glossary** (per-site/company alias store) so the same
   name transcribes correctly next time — the foundation for B.

Two enablers surfaced during design:

- **D — report-sourced items are not editable.** Editability requires the
  item to carry its durable Aurora id. The `/timeline` shim
  (`lambda_org_api.render_report_shape`) only surfaces action-item ids on the
  branch guarded by `topics.has_topics_for_source_prefix("extractions/{user}/{date}/")`.
  Topics ingested from the **report** path (`source_s3_key = reports/{date}/{user}/daily_report.json`)
  fall through to **S3-verbatim** and lose their ids — so their content (which
  DOES exist in Aurora with ids) can't be edited. Verified live on SB1108:
  2026-07-17 topics (`extractions/…`) are editable; 2026-07-16 topics
  (`reports/…daily_report.json`) are not, despite both having Aurora rows.

- **RAG re-index is a delete-and-replace, and the plumbing exists.** Embeddings
  cannot be find-replaced; they must be **recomputed**. The `report_chunks`
  table already keys each chunk by `source_s3_key` + `topic_id`, and
  `chunks.delete_chunks_for_source(source_s3_key)` already exists. So an edit
  can delete the affected chunks and re-insert freshly-embedded ones. The
  vector store is a **rebuildable derived index**, not immutable data.

## 2. Design decisions (settled in brainstorming)

- **D1 — Correction model = free-text field editing.** The user rewrites the
  whole text of a field (like editing a task field, but the value is content).
  NOT term-level select-and-replace.
- **D2 — Glossary capture = diff + confirm.** After an edit, the system diffs
  before → after to surface changed proper-noun-like tokens as **glossary
  candidates**; the user / site_manager confirms which become aliases (keeps
  the alias store clean of diff noise).
- **D3 — Structured content is materialized in place.** The corrected text is
  written into the Aurora structured row (topics / action_items / findings /
  observations). Display and analytics then read the corrected text directly —
  no read-time normalization of structured content.
- **D4 — Raw transcript is immutable.** The transcript S3 artifact is never
  written. Its *vector representation* is re-embedded from an alias-normalized
  copy (§5.3), and RAG synthesis normalizes retrieved transcript text
  (belt-and-suspenders). This resolves the "transcript still says the old name"
  wrinkle without touching the record.
- **D5 — Alias store = the glossary (B foundation).** A confirmed correction
  becomes a scoped `name_aliases` row (wrong → right, kind, site/company). It
  affects **future** reads/embeds/synthesis + (later, in B) Transcribe. It is
  **not** retroactively find-replaced across all historic content by default;
  an explicit "apply to existing" action is an optional escalation.
- **D6 — Re-index granularity = per topic.** A single edit deletes and
  re-embeds only the affected topic's chunks (`delete_chunks_for_topic`), not
  the whole report. Cheaper on DashScope calls, more surgical.
- **D7 — Two-tier authority.** Per-item correction mirrors the existing task
  ACL (`patch_action_item`): author, the site's pm/site_manager, admin/gm, or
  platform_admin (cross-company). Promoting a correction to a
  company/site **alias** requires **site_manager+** (higher stakes — affects
  RAG + future STT company-wide).

## 3. Editable fields

Free-text (whole-field) editing on the human-readable content fields, across
the item store's tables:

| Table | Editable text/name fields |
|---|---|
| `topics` | `title`, body/summary prose |
| `action_items` | `text`, `responsible` |
| `findings` | `observation`, `recommended_action`, `entity_name`, `entity_trade` |
| `safety_observations` / quality | observation text, entity/name fields |

**Excluded:** categorical / enum fields (`domain`, `severity`, `category`,
`status`, `priority`, `deadline`). Those are extraction *judgments* or task
*metadata*, not transcription errors — and `status`/`priority`/`deadline`/
`responsible` are already editable via the existing task path
(`patch_action_item`). This spec ADDS the free-text *content* fields;
`responsible` is shared (already editable as a task field, also a name field).

## 4. Architecture — end-to-end flow of one correction

```
UI: user rewrites a content field (Timeline / topic detail)
   │
   ▼
PATCH /api/org/content/{table}/{id}   (new)
   │  1. ACL (D7 per-item tier)
   │  2. write corrected text → Aurora row            (materialize, D3)
   │  3. append edit-history row (before/after/actor)  (audit)
   │  4. diff before→after → candidate terms           (D2, returned to UI)
   │  5. enqueue re-index for this topic               (async)
   ▼
Re-index worker (per topic)
   │  a. delete_chunks_for_topic(topic_id)
   │  b. re-chunk: structured = corrected text;
   │     transcript = normalize(raw_transcript, aliases)  (D4 — S3 untouched)
   │  c. DashScope embed (text-embedding-v4, non-VPC)
   │  d. insert_chunk(...) → upsert report_chunks
   ▼
(optional) user/site_manager confirms candidate → name_aliases row (D5, D7 alias tier)
   → affects FUTURE normalize() at re-embed + RAG synthesis; feeds B later
```

RAG read path (`lambda_ask_agent` / `lambda_rag_search`): retrieval matches the
re-embedded (corrected/normalized) chunks; **synthesis also applies
`normalize()`** to retrieved chunk text before the LLM (catches any chunk not
yet re-embedded — eventual-consistency safety net).

## 5. Components & data model

### 5.1 D — surface ids for report-sourced topics (enabler)
`render_report_shape` already emits `action_items[].id`. The gap is the
**branch selection** in the `/timeline` shim: report-sourced `(user,date)` days
serve S3-verbatim (no ids). Fix: when Aurora topics exist for the `(site,date)`
regardless of `source_s3_key` prefix, serve the Aurora-rendered shape (with
ids) instead of S3-verbatim — OR merge Aurora ids onto the verbatim doc by
`(topic, action_index)`. Chosen: **prefer the Aurora-rendered shape whenever
Aurora topics exist for the caller's accessible sites on that date**, so
report-sourced content becomes editable exactly like extraction-sourced. The
byte-identical-verbatim contract is retained only for days with **no** Aurora
topics at all.

### 5.2 Edit endpoint + audit
- New `PATCH /api/org/content/{table}/{id}` (org-api). Body: the changed field(s)
  new text. Validates table/field against an allow-list (§3), ACL per D7.
- Writes corrected text to the Aurora row; appends an **edit-history** row.
  Reuse the audit shape from `action_item_audit` (migration 0017) generalized
  to `content_edits(id, company_id, table_name, row_id, field, before, after,
  actor_user_id, actor_role, created_at)`.
- Returns the updated row + the diff candidate terms (D2).

### 5.3 Re-index hook
- Add `chunks.delete_chunks_for_topic(conn, topic_id)` (one-line SQL sibling of
  `delete_chunks_for_source`).
- Re-embed uses the existing `chunking.py` (`chunk_report` + `chunk_transcripts`)
  and DashScope `text-embedding-v4` (as `lambda_embed_report`). The transcript
  chunks are embedded from `normalize(transcript_text, active_aliases)` — the
  raw S3 transcript is read but never written.
- Async: the edit endpoint enqueues the re-index (the embed step is a non-VPC
  DashScope call — must not block the write). Mechanism: reuse the existing
  embed lambda invocation path per `(topic / source, date, user)`.

### 5.4 Alias store (glossary / B foundation)
```
name_aliases(
  id, company_id, site_id NULL,        -- NULL = company-wide
  wrong_term, right_term,
  kind        'person' | 'product' | 'company' | 'other',
  source      'correction' | 'manual',
  status      'active' | 'retired',
  created_by, created_at )
```
- `normalize(text, aliases)`: whole-word, case-aware substitution. PURE
  (unit-testable), used at re-embed (transcript) and RAG synthesis.
- Confirming a diff candidate (D2) writes a `name_aliases` row (D7 alias tier).
- **B (out of scope here):** a later job maps `name_aliases` → AWS Transcribe
  Custom Vocabulary per company. This spec only builds the store + normalize.

### 5.5 Frontend
- Inline **content editors** in the Timeline / topic-detail view: each editable
  text field (§3) becomes a free-text editor gated by the D7 per-item ACL
  (`FS.can(user, P('content','edit'))` UX-only; backend enforces).
- **History** view per item (reuses the Details/History tab pattern already in
  tasks.js) showing the content_edits trail.
- **"Add to glossary"** confirm: after save, show the diff candidate terms with
  a checkbox to promote to an alias (site_manager+).

## 6. Error handling & consistency
- The edit write (Aurora + audit) is **atomic and synchronous**; success does
  not depend on re-embed.
- Re-index is **async + retried**; until it completes, RAG may return the old
  embedding — mitigated by the synthesis-time `normalize()` safety net (§4).
  This is acceptable eventual consistency (seconds-to-minutes), and is stated
  in the UI as "correction saved; search updates shortly" only if needed.
- A failed re-index is logged and re-enqueued; it never rolls back the edit.
- `normalize()` over-application risk (D5): aliases are scoped (site/company)
  and applied only to FUTURE reads by default; no blind global historic
  find-replace.

## 7. Testing
- **normalize()** — pure unit tests: whole-word boundary, case handling, no
  partial-token corruption, multiple aliases, scope precedence.
- **Edit endpoint** — ACL matrix (author / site authority / admin / cross-company
  platform_admin / outsider-deny), field allow-list, audit row written.
- **D fix** — `/timeline` shim returns ids for a report-sourced `(user,date)`
  that has Aurora topics (the SB1108 2026-07-16 case), still byte-verbatim when
  no Aurora topics exist.
- **Re-index** — `delete_chunks_for_topic` removes exactly that topic's chunks;
  re-embed inserts the corrected/normalized chunks (mock DashScope).
- **Frontend** — `node --check`; editor renders per ACL; history shows edits.

## 8. SAFETY & QUALITY must stay consistent with edits (D8 single-source)

**Requirement (raised in review):** SAFETY and QUALITY are not separate content —
they are the daily-extracted findings **grouped by `domain`** (`safety`/`quality`).
So when a user corrects a finding's text, the SAFETY/QUALITY views MUST reflect
it automatically. Any path that shows a stale copy is a bug.

**Current obstacle — the D8 transitional dual-write.** Safety findings are
written to BOTH the new `findings` table (0010: has `domain`, `severity`, and
`status DEFAULT 'open'`) AND the legacy `safety_observations` table (0003), via
the extractor's `_derive_safety_flags` bridge. The two rows are **not linked**
(no `finding_id` on `safety_observations`). Today's SAFETY reads come from
`safety_observations`, in two places:
- `rollup.portfolio_counts` safety subquery (`FROM safety_observations …`);
- `topics.list_topics_for_date` attaches `safety_observations` as a topic child
  (the Timeline / live-items / SAFETY-page read path).

If the content endpoint edits a `findings` row, `safety_observations` stays
stale → SAFETY shows the old text. Editing `safety_observations` directly (it is
in the v1 allow-list) has the mirror problem.

**Decision — retire D8; make `findings`-by-`domain` the single source.** SAFETY/
QUALITY become pure views over `findings`:
- `rollup.portfolio_counts` safety count → `findings WHERE domain='safety' AND status='open'` (open-high = `severity='major'`).
- `topics.list_topics_for_date` (both shapes) → attach `findings WHERE domain IN ('safety','quality')` in the child slot the SAFETY/QUALITY UI reads, instead of `safety_observations`.
- Stop the `_derive_safety_flags` dual-write into `safety_observations` (leave the table in place, unread, for rollback; drop later).
- Content **allow-list**: safety/quality corrections edit the `findings` row
  (`observation`, `entity_name`, …); **remove `safety_observations` from the
  editable set** so there is only one editable source.

**Sequencing:** this retirement must land **before** the Phase D frontend
content editors, so the first real content edit already propagates to SAFETY/
QUALITY. It is added as **Phase F** of the plan, scheduled before Phase D.

## 9. Scope boundary (what this spec delivers)
**Delivers:** editable structured content (free-text, materialized) + audit
history + the D id-surfacing fix + per-topic delete-and-re-embed re-index +
`name_aliases` store + `normalize()` + the diff-candidate glossary confirm +
the D8 single-source retirement (§8) so edits propagate to SAFETY/QUALITY.

**Not here:** B's AWS Transcribe Custom Vocabulary integration (uses the store
built here); C's content-filter / privacy (masking, non-work removal,
soft-delete — separate spec); retroactive "apply alias to all existing content"
(optional later escalation).
