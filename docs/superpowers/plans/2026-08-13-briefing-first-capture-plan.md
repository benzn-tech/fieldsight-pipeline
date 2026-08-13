# Briefing-first capture — implementation plan

**Date:** 2026-08-13
**Spec:** `docs/superpowers/specs/2026-08-13-briefing-first-capture-design.md` (source of truth — do not re-litigate its decisions)
**Branch base:** `develop`
**Principle carried through every task:** the briefing path is **ADDITIVE**. `lambda_extract_session`'s existing extraction contract, `lambda_item_writer`, `lambda_ingest`, and every current consumer keep working byte-for-byte on site walks and named team syncs (5/5 and 14/14 assignee fill there — correct, do not touch). Cutover of any consumer is a separate, later decision.

---

## Reading list for the executing engineer (no other context assumed)

| file | why |
|---|---|
| `src/lambda_extract_session.py` | `assemble_session_turns` (~line 991), `_dedup_turn_boundaries` (~592), `extraction_key`/`extract_session` write path, `parse_final_request` (~1751) — the `extraction_requests/` artifact channel and WHY it exists (BUG-36: in-VPC lambdas cannot invoke lambdas; they write S3 request artifacts through the gateway endpoint) |
| `src/lambda_rolling_summary.py` | the pattern for a sibling LLM lambda that reuses `gather_session_segments` + `assemble_deduped_turns` |
| `src/lambda_item_writer.py` | the in-VPC S3-event → Aurora writer pattern the new entity writer mirrors |
| `src/lambda_ingest.py` + `src/chunking.py` | where `report_chunks` transcript windows are cut today and where topic coupling happens |
| `src/lambda_ask_agent.py` (`_aggregate_topics`) | BUG-39: where topic-less windows are discarded at READ time |
| `src/repositories/chunks.py`, `src/repositories/search_sql.py` | repo conventions (dict_row, `%s::vector`) |
| `src/migrations/` | highest on develop is **0040**; **0039 is RESERVED** by 0038's header; **0041 is TAKEN** by `0041_user_deletion.sql` on `feat/user-deletion-schema` (open PR #459) — the next free number is **0042** |
| `src/template.yaml` | `ExtractSessionFunction` (~1843, non-VPC, inline S3 policies), `FinalizeSweepFunction` (~2185, in-VPC, writes `extraction_requests/`), `ItemWriterFunction` (~2544, in-VPC Aurora writer), `OrgApiFunction` (~1526) |
| `CLAUDE.md` | BUG-33 (S3 events wired manually, not via SAM), BUG-36 (in-VPC egress black-holes; S3 request-artifact pattern), BUG-38 (migrations merged to `main` run on prod automatically), BUG-39, BUG-41, BUG-43 (account concurrency = 10; arrival-rate × duration budget; IAM for reading your own output; `simulate-principal-policy`, never guess) |

**Standing rules for every task below:**

- Tests run per the harness in the `fieldsight-backend` skill / CLAUDE.md: FakeConn/FakeCursor doubles for repos, `org.lambda_handler(make_event(...))` with monkeypatched repo funcs for org-api, dummy AWS env for module-level boto3 clients. Any new SQL also gets an RDS-Data-API check inside a rolled-back transaction (CLAUDE.md "Run the SQL against a real database").
- Any new CFN resource or new S3 prefix a function role touches: verify the deploy role `github-actions-fieldsight-deploy` with `aws iam simulate-principal-policy` BEFORE merging — a missing grant is CREATE_FAILED and a **full stack rollback**. New `AWS::Serverless::Function` resources and inline role policies are types the role already creates (18 functions exist); the check is still mandatory, not optional.
- S3 → Lambda notifications are wired **manually** per BUG-33 (`scripts/wire-s3-events.sh`), never as SAM `Events`. A new prefix trigger is an out-of-band step and must be added to that script, not done as a one-off CLI call that leaves no trace.
- Deploys: `develop` → TEST stack automatically; `main` → PROD (approval-gated). **Migrations merged to `main` run against the prod DB automatically** — every migration task below states whether it may go to `main`.

---

## Task order and why

```
T1  turn dedup in the shared assembly        (prerequisite — spec §4: "a prerequisite, not an optimisation")
T2  migration 0042: FTS + trigram + entities (schema first; additive; nothing reads it yet)
T3  briefing generation lambda + S3 artifact (writes briefings/ + turns sidecar; server-side alias
                                              validation + anchor re-derivation live HERE)
T4  in-VPC briefing writer → Aurora          (consumes briefings/; fills the T2 tables)
T5  BUG-39 fix: windows survive without a topic (independent of T3/T4; can ship any time after T2)
T6  org-api read endpoints: briefing + search  (needs T2+T4 for search, T3 for briefing)
T7  rollout wiring, monitoring, TEST-only gating
```

T1, T2, T5 are each independently shippable and improve the EXISTING pipeline on their own. T3→T4→T6 is the new additive path. Nothing in T1–T7 modifies the extraction schema, the extraction prompt, `extractions/` artifact shape, or item-writer's contract.

---

## Task 1 — Turn deduplication in the shared assembly, before any LLM call

**Goal.** Remove batch-window near-duplicate turns (measured 1,157 of 3,715 turns = 31% on the reference session) in `assemble_session_turns`, so every consumer — Tier-2 extraction, Tier-1 rolling summary, finalize email, group merge, and the new briefing pass — gets the same clean stream and every LLM call sheds ~⅓ of input cost.

**Where and why there.** `src/lambda_extract_session.py`, immediately around `_dedup_turn_boundaries` (line ~592) inside `assemble_session_turns` (line ~1073). That function's docstring already declares it "the one clean word stream shared by" all consumers — this is the only place a dedup reaches everyone. Do NOT put it in `render_transcript` (rolling summary and email would miss it) or in the briefing lambda (extraction would miss it).

**What changes.**
- `_dedup_turn_boundaries` today only handles the ~2s ring-buffer overlap between ADJACENT turns whose time ranges overlap. Batch-window re-transcription produces near-duplicates that are not necessarily adjacent-pair prefix overlaps. Add a second pass, `_dedup_batch_window_repeats(turns)`, that detects a later turn whose time range overlaps an earlier one AND whose text is a near-duplicate (normalized-token similarity above a threshold; reuse `chunk_stitch.dedup_overlap` / `text_normalize` machinery rather than inventing a new matcher), and drops or trims the later copy.
- **Hard invariant, pinned by tests:** the pass is a strict NO-OP on non-overlapping (legacy whole-file / VAD / sequential) turns — same guarantee `_dedup_turn_boundaries` documents. Time overlap is the gate; text similarity alone must never drop a turn (people genuinely repeat themselves).
- Record what was removed: extend the stats already flowing out of `assemble_session_turns` (`announcement_stats` dict) with `{"batch_dedup": {"turns_dropped": N, "turns_seen": M}}` so the extraction artifact reports it, exactly as `device_announcements` does today. Anything that silently discards input has to leave a number behind (this file's own stated rule).
- Do NOT drop backchannel turns ("mm", "yeah" — the 423 in the spec's measurement) in the shared assembly. The spec collapsed them **for the experiment's cost measurement**; the shared stream is also the record the viewer and evidence verification read. If backchannel suppression is wanted for prompts, that is a later, prompt-side decision — out of scope here.

**Files touched.** `src/lambda_extract_session.py`; `tests/unit/test_extract_session_dedup*.py` (new); possibly `src/chunk_stitch.py` if a helper is factored there.

**Tested how.**
- Unit: synthetic turn lists — overlapping near-duplicate pairs (dropped/trimmed), overlapping different-content pairs (kept), sequential identical text (kept — the no-op invariant), empty/None `abs_end` (kept).
- Regression: run the existing extraction test suite untouched — every current test must pass with zero edits, proving the pre-chunk pipeline is byte-for-byte unchanged.
- Measurement: replay the reference session's transcripts (`sid15770a…`, 2026-08-13) through `assemble_session_turns` locally against TEST S3 and confirm the drop count is in the ~30% band and the rendered transcript still reads coherently end to end.

**How you'd know it FAILED in production.** Two opposite signatures: (a) over-dedup — extraction artifacts' `transcript_stats.chars` fall sharply while `batch_dedup.turns_dropped` is far above ~35% of `turns_seen`, and evidence verification (`verify_evidence`) starts failing to match quotes that used to match (the quoted words were deleted); (b) under-dedup — `turns_dropped` ≈ 0 on batched sessions while LLM input token counts stay flat. Watch the new stats field in `extractions/*.json` on TEST for a few real sessions before merging to main. Because the stats ride the artifact, the check needs no new infrastructure.

**IAM / VPC.** None. No new resources, no new S3 prefixes, no template change.

**Migrations.** None.

---## Task 2 — Migration 0042: Postgres FTS + trigram + entity tables

**Goal.** Give Aurora the literal-keyword instruments the retrieval layer needs (spec §3 measured: zero GIN/tsvector/trigram indexes today, `"Plaud"` → 0) and the entity/alias store, plus a turn-level transcript store that carries **word-level time anchors** natively (today they die when windows are cut to a topic's minute-granularity `time_range`).

**Migration number: `0042`** — 0040 is the highest on develop, 0038's header reserves 0039, and **0041 was claimed by `feat/user-deletion-schema` (PR #459) while this plan was being written**. That is exactly the collision to watch for: re-check `src/migrations/` on `origin/develop` AND every open PR immediately before writing the file, and renumber upward. Never reuse a number — two migrations sharing one number means whichever deploys second is silently skipped by the runner.

**What changes — `src/migrations/0042_briefing_search.sql`:**
- `CREATE EXTENSION IF NOT EXISTS pg_trgm;` (0001 created `vector` + `pgcrypto` the same way; the migration role can create extensions).
- **`session_turns`** — the queryable transcript, decoupled from topics by construction:
  - `id uuid PK default gen_random_uuid()`, `company_id uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE`, `site_id uuid REFERENCES sites(id)` (nullable — BUG-43 root-cause 3 showed site can be legitimately unresolvable), `user_id uuid REFERENCES users(id)`, `session_base text NOT NULL`, `report_date date NOT NULL`, `turn_index int NOT NULL`, `speaker text`, `text text NOT NULL`, `abs_start timestamptz`, `abs_end timestamptz`, `source_filename text`, `start_sec double precision` (in-file offset for audio seek — the pair `(source_filename, start_sec)` is what makes an anchor playable), `created_at timestamptz default now()`.
  - `tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED` + `CREATE INDEX ... USING gin (tsv)`.
  - `CREATE INDEX ... USING gin (text gin_trgm_ops)` — trigram, for `PV Tech` / partial / misspelled queries.
  - `UNIQUE (session_base, turn_index)` — the writer's idempotency key (delete-then-insert per session, mirroring item-writer's `delete_topics_for_source` pattern).
  - btree on `(company_id, report_date)` and `(session_base)`.
- **`session_entities`** — `id uuid PK`, `company_id uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE`, `site_id uuid NULL`, `session_base text NOT NULL`, `report_date date NOT NULL`, `name text NOT NULL`, `kind text CHECK (kind IN ('person','company','product','standard','quantity','other'))`, `note text`, `mention_count int`, `created_at`. `UNIQUE (session_base, name)`.
- **`entity_aliases`** — `id uuid PK`, `entity_id uuid NOT NULL REFERENCES session_entities(id) ON DELETE CASCADE`, `alias text NOT NULL`, `status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','rejected'))`, `rejected_reason text` (`'collides_with_canonical'` / `'common_words_phrase'` — the spec's two validation rules, recorded rather than silently dropped), `UNIQUE (entity_id, alias)`. Distinct from migration 0020's `name_aliases` (the human-confirmed correction glossary) — different lifecycle, do not merge them.
- **FTS over existing chunks:** `CREATE INDEX idx_report_chunks_text_fts ON report_chunks USING gin (to_tsvector('english', chunk_text));` and a trigram GIN on `chunk_text`. Expression index, not a generated column — **zero change to the `report_chunks` row shape or any existing writer**. Table is 532 rows; plain `CREATE INDEX` (no `CONCURRENTLY` — the migration runner is transactional and the table is tiny).

**Files touched.** `src/migrations/0042_briefing_search.sql` only. (Repo modules come in T4/T6.)

**Tested how.** Run the migration against the TEST DB via `lambda_migrate` on a develop deploy; then RDS-Data-API assertions in a rolled-back transaction: insert a turn with `PV Tech`, confirm `tsv @@ plainto_tsquery('english','tech')` hits, `text % 'PB Tech'` (trigram) hits, `UNIQUE` conflicts behave, `ON DELETE CASCADE` from `session_entities` → `entity_aliases` works. Commit the same cases as `tests/integration/test_briefing_search_schema.py` (skips cleanly without `TEST_DATABASE_URL`).

**How you'd know it FAILED in production.** The migration itself: `lambda_migrate` logs on the deploy run — a failed migration fails the deploy loudly, not silently. After that: these tables are write-only until T4/T6, so the only prod risk is `pg_trgm` creation being refused (extension not on the cluster's allowlist) — which would have failed identically on TEST first.

**IAM / VPC.** None — migrations ride the existing `MigrateFunction` (in-VPC, already has DB creds).

**Prod-affecting migration — flag.** Additive-only (new tables, new indexes, one extension, no ALTER of existing tables), so it is SAFE for `main` — but per the sequencing rule, **keep it on `develop` until T3/T4 have proven the artifact shape on TEST**, because a schema change that later needs an ALTER on prod is worse than landing it once, correct. Nothing here is destructive; there is no rollback hazard beyond dropping empty tables.

---

## Task 3 — Briefing generation pass: new non-VPC lambda writing a new S3 artifact

**Goal.** One pass over the full deduped transcript per closed session, producing the spec §4 container (`headline / sections[] / entities[] / tasks[]`, free text in the fields, `basis: committed|inferred` on tasks), written to S3 — with **alias validation and time-anchor re-derivation performed server-side in this lambda before the artifact is written**, never left to a client.

**Architecture (follows existing conventions exactly).**
- **New function `BriefingFunction`** (`src/lambda_briefing.py`), **non-VPC** — it calls the LLM over the internet, exactly like `ExtractSessionFunction` and `RollingSummaryFunction` (template comment at ~1837: "NOT in VPC"). It must NOT be in-VPC: BUG-36, an in-VPC lambda's LLM call black-holes until timeout.
- **Trigger: S3 request artifact**, prefix **`briefing_requests/{device_sid}.json`** — the same channel shape as `extraction_requests/` and for the same reason (BUG-36: the in-VPC finalize sweep cannot invoke a lambda; it CAN write S3 through the gateway endpoint). Body mirrors `parse_final_request`'s contract: `{"userFolder", "date", "sessionBase"}`. A distinct prefix, not a marker inside `extraction_requests/` — that prefix's notification already targets `ExtractSessionFunction` and routing two LLM passes through one function couples their timeout/concurrency budgets.
- **Writer of the request:** `lambda_finalize_claim` (the finalize sweep) writes `briefing_requests/{sid}.json` right where it writes `extraction_requests/` today, behind a new env switch `ENABLE_BRIEFING` (CFN Parameter, default `false`; TEST turns it on via `deploy.yml` parameter override — the additive path is dark on prod until deliberately enabled).
- **Outputs (all under existing conventions):**
  - `briefings/{user_folder}/{date}/{session_base}.json` — the validated briefing artifact (schema below).
  - `briefings/{user_folder}/{date}/{session_base}.turns.json` — the deduped turn stream the briefing was derived from: `[{turn_index, speaker, text, abs_start, abs_end, source_filename, start_sec}]`. This sidecar is what T4 loads into `session_turns` — the in-VPC writer must not re-assemble turns itself (it can't call the shared assembly's S3 pagination cheaply, and two assemblies would drift; one derivation, written down, consumed twice).
- **Reuses:** `gather_session_segments` + `assemble_session_turns` from `lambda_extract_session` (import, exactly as `lambda_rolling_summary.process_transcript_key` does), `llm_utils.call_llm(..., force_json=True, enable_thinking=True)`, `transcript_utils` (bundled in the zip per the standing rule).

**Server-side post-processing — the two spec rules, implemented here:**
1. **Alias validation** (`_validate_aliases(entities)`): (a) drop an alias equal (case/whitespace-normalized) to any OTHER entity's canonical `name` (the `Claude`-as-alias-of-`Plaud` failure); (b) drop an alias composed entirely of common English words (the `record include` failure) — ship a small stopword/common-word list as a code constant seeded from a standard frequency list; an alias survives only if at least one token is NOT in the list or it contains a digit. Dropped aliases are kept in the artifact under `entities[].rejected_aliases` with reasons — counted, never silently discarded (T4 stores them as `status='rejected'`).
2. **Time-anchor re-derivation** (`_snap_anchors(briefing, turns)`): the model's `at` is recollection and measured wrong (spec: `13:33:11` vs actual `14:33:11`). For every bullet and task carrying a quote, find the quote in the real turns (reuse `evidence_match.py` — the fuzzy quote-matching machinery `verify_evidence` already uses — do not write a second matcher); for items without a quote, match on their rarest terms. Replace `at` with the matched turn's `abs_start` and attach `{source_filename, start_sec}`. An unmatched anchor is **counted, not silently kept**: the item keeps the model's `at` but gains `"anchor": "unmatched"`, and the artifact's top level records `anchor_stats: {snapped, corrected, unmatched}` (the spec's 4-corrected/2-unmatched measurement becomes a permanent artifact field).

**Artifact schema (top level):** `{schema_version: 1, session_base, user_folder, date, headline, sections[], entities[], tasks[], anchor_stats, dedup_stats, transcript_stats, generated_at}`. Tasks carry `basis` verbatim from the model; **nothing in this pipeline may promote an `inferred` task into anything that reads as a record** (spec §8 — surfacing is T6/frontend's contract, but the field must never be dropped here).

**Idempotency / cost controls.** Same-key overwrite (the `extraction_key` collide-on-purpose pattern); a re-request regenerates. Concurrency budget per BUG-43: this adds one ~125s LLM call per session **close** (not per chunk) — arrival rate is session closes, so steady-state occupancy is well under one slot; still, set function `Timeout: 600`, `LLM_HTTP_TIMEOUT: 540` (copy ExtractSession's numbers and reasoning) and a `MIN_REBRIEF_INTERVAL_S` throttle reading the existing artifact's `generated_at` (copy `lambda_rolling_summary`'s read-your-own-output throttle) so a retry storm cannot burn money.

**Files touched.** `src/lambda_briefing.py` (new), `src/lambda_finalize_claim.py` (write the request artifact, ~5 lines behind the switch), `src/template.yaml` (BriefingFunction + FinalizeSweep policy line + `EnableBriefing` parameter), `scripts/wire-s3-events.sh` (the `briefing_requests/` notification — BUG-33), `.github/workflows/deploy.yml` (TEST parameter override `EnableBriefing=true`), tests.

**Tested how.**
- Unit (LLM injected, no network — the `summarize_turns(call_llm=...)` pattern): prompt construction includes full transcript; alias rule 1 and rule 2 each with the spec's own examples (`Claude`/`Plaud`, `record include`); anchor snapping corrects a wrong `at` against synthetic turns and counts an unmatchable one; artifact schema pinned; throttle honored; malformed LLM JSON → no write, logged (never write a malformed contract — M-9's rule).
- TEST end-to-end: close a real session on TEST, confirm `briefing_requests/` → `briefings/{...}.json` + `.turns.json` appear, spot-check anchors against the audio.

**How you'd know it FAILED in production.**
- Silent-failure signature #1: sessions close but no `briefings/` objects appear → the S3 notification wiring (out-of-band, BUG-33) or the FinalizeSweep PutObject grant is missing. Check: `aws s3 ls briefings/` for the day, then FinalizeSweep logs, then `simulate-principal-policy`.
- Signature #2: artifacts appear with `anchor_stats.unmatched` climbing toward `snapped` → the quote matcher is broken against real ASR text (every anchor a guess). The stat is in the artifact precisely so this is a one-line query, not an investigation.
- Signature #3 (BUG-43 shape): CloudWatch `Throttles` > 0 on the account while briefing runs — briefing is eating concurrency slots. It is close-triggered so this should not happen; if it does, the throttle interval and a reserved-concurrency cap are the levers.

**IAM (deploy role + function role) — explicit.**
- `BriefingFunction` role (inline in template, deploy role already creates these): `s3:GetObject` on `transcripts/*`, `voice_ask/*`, `audio_segments/*` (batch maps — same reason as ExtractSession's grant at template ~1919), `briefing_requests/*`, `briefings/*` (reads its own output for the throttle — **BUG-43 lesson 3: a writer that reads its own output needs the Get grant or the throttle silently dies**); `s3:PutObject` on `briefings/*`; `s3:ListBucket` with prefix condition `transcripts/*`, `briefings/*`, `voice_ask/*` (without ListBucket a missing key answers 403 not 404 — the `read_existing_extraction` UNKNOWN trap, documented at template ~1935).
- `FinalizeSweepFunction`: add `s3:PutObject` on `briefing_requests/*` beside its existing `extraction_requests/*` line.
- Deploy role: no new resource TYPES (one more Serverless::Function, inline policies, one Parameter). Run `simulate-principal-policy` for `lambda:CreateFunction`/`iam:PutRolePolicy` against the new names anyway before merging.

**New VPC endpoint requirement.** **None.** BriefingFunction is non-VPC (full egress). FinalizeSweep's new write is S3, which it already reaches via the existing gateway endpoint.

**Migrations.** None.

---

## Task 4 — In-VPC briefing writer: `briefings/` → Aurora (`session_turns`, `session_entities`, `entity_aliases`)

**Goal.** Land the briefing's entities/aliases and the deduped turn stream in Aurora so keyword/entity search (T6) has rows to serve. Mirrors `ItemWriterFunction` exactly: in-VPC, S3-event-triggered, psycopg layer, CFN-injected PG creds (BUG-36: creds via `{{resolve:secretsmanager:...}}` at deploy time, zero runtime AWS calls other than S3 through the gateway endpoint).

**What changes.**
- **New `src/lambda_briefing_writer.py`**: on an S3 event for `briefings/{folder}/{date}/{base}.json` (ignore `.turns.json` events — one trigger, filename-gated in code, the way extract-session routes on key shape):
  1. Read the briefing artifact + its `.turns.json` sidecar.
  2. Resolve identity: `resolve_company` / `recordings.site_for_day(...) or resolve_site(...)` / `resolve_user` — **copy `lambda_item_writer`'s exact current priority order** (BUG-41's rule: the app's `recordings.site_id` is authoritative; env fallbacks are last). An identity-bridge miss writes company-scoped rows with `site_id NULL` rather than zero rows — turns must survive without a site the same way windows must survive without a topic; log the miss loudly (the "identity bridge miss — zero writes" trap).
  3. In one transaction (the `conn.transaction()` + raise-to-rollback pattern — a `return` does not roll back, CLAUDE.md Testing section): delete-then-insert `session_turns` keyed on `session_base` (idempotent re-run, mirroring `delete_topics_for_source`); upsert `session_entities` per entity; insert `entity_aliases` including `status='rejected'` rows with reasons.
- **New `src/repositories/briefing_store.py`**: `replace_turns(conn, ...)`, `upsert_entities(conn, ...)` — dict_row conventions, mirror `repositories/chunks.py`.
- **Template:** `BriefingWriterFunction` — copy `ItemWriterFunction`'s `Layers` (PsycopgLayer) / `VpcConfig` / PG env block **verbatim** (the template's own comments instruct exactly this for siblings), `Condition: HasDb`.
- **Wiring:** `scripts/wire-s3-events.sh` gets the `briefings/` prefix notification, suffix-filtered to `.json` (the `.turns.json` events will also match `.json` — hence the in-code gate; do not try to express "ends with .json but not .turns.json" in S3 filters).

**What does NOT change.** `lambda_item_writer` is untouched. `topics`/`action_items`/`findings` are untouched — briefing tasks/sections do NOT get written into the existing item tables in this phase (that would be a consumer cutover, explicitly deferred by the spec §7).

**Tested how.** Unit with FakeConn (mirror `tests/unit/test_action_items_repo.py`): idempotent double-run leaves one row set; rejected aliases persisted with reason; identity miss → site NULL rows + loud log, not zero writes; transaction unwinds on a mid-write raise. RDS-Data-API rolled-back-transaction checks for the real SQL (unique conflicts, cascade). TEST e2e: after T3 writes a briefing on TEST, confirm rows: `SELECT count(*) FROM session_turns WHERE session_base='sid...'`.

**How you'd know it FAILED in production.** `briefings/` artifacts exist but `session_turns` is empty for those sessions. Three usual suspects in order: the manual S3 notification was never wired on the prod bucket (BUG-33 — it is NOT in the template, so a prod cutover checklist item), the function role lacks `s3:GetObject` on `briefings/*` (simulate, don't guess), or the identity bridge missed (grep logs for the miss message). None of these throw anything a dashboard shows — the check is "artifact count vs. row count per day", worth a one-line scheduled query or at minimum a runbook entry.

**IAM — explicit.** Function role: `s3:GetObject` on `briefings/*`; `s3:ListBucket` prefix `briefings/*` (the 403-vs-404 trap again — the sidecar read must distinguish "not yet written" from "denied"). VPCAccessPolicy. Deploy role: same answer as T3 — existing resource types, verify with `simulate-principal-policy`.

**New VPC endpoint requirement.** **None** — S3 gateway endpoint exists and is the only AWS service this function touches; Postgres is direct-to-cluster inside the VPC. **Rule check before any future edit: an in-VPC function that grows a call to ANY new AWS service needs that service's VPC endpoint first (BUG-36).**

**Migrations.** Depends on T2 (0042) being applied. No new migration.

---

## Task 5 — Decouple `report_chunks` transcript windows from `topic_id` (BUG-39: 220 of 373 discarded)

**Goal.** A transcript window without a topic survives to the reader. Today `report_chunks.topic_id` is already nullable and `chunking.py` already ingests an "unassigned" bucket — the loss is (a) at READ time in `lambda_ask_agent._aggregate_topics`, which by explicit comment "只保留有 topic_id 的 chunk" drops every topic-less window, and (b) authority-flip days matching zero report-topics to Aurora topics so everything lands NULL. This task fixes the read path and stops the writer coupling windows to topics; it deliberately does NOT try to re-attach topics (that is BUG-39's fix ①, a separate remediation).

**What changes.**
- `src/lambda_ask_agent.py` (`_aggregate_topics`): topic-less `transcript_window` chunks are no longer discarded — they aggregate under a per-session/per-date "transcript" group (the chunk's `metadata.window_span` + `report_date` + `user_name` give the display frame), so RAG answers can cite un-topiced speech. Keep the existing behaviour for topic-ed chunks byte-for-byte; the change is additive to the aggregation, not a rewrite. NOTE the 2026-07-10 user-pref comment at that site records the discard as a deliberate choice at the time — this plan supersedes it per the spec's requirement ("a transcript window must be storable without a topic"); say so in the code comment replacing it.
- `src/chunking.py` (`_window_metadata` / `chunk_transcripts`): windows keep their per-turn word anchors — add `"turn_anchors": [{abs_start_str, abs_end_str, src}]` (one entry per turn in the window) to the window metadata so the minute-granularity `time_range` stops being the only surviving time information (spec §3 requirement 1). Metadata-only, additive; the embedded `chunk_text` is unchanged, so **existing sidecar embedding hashes still match** (the sha256-of-text contract between ingest and embed-report at `lambda_ingest.embed_from_sidecar` is not disturbed — this is the trap that fails whole reports; test pins it).
- `src/lambda_rag_search.py`: stop excluding NULL-topic windows anywhere it does (verify — the vector search itself doesn't filter on topic; the loss was in ask-agent's aggregation).
- **Backfill/remediation of the existing 220 NULL-topic rows: none needed** — the rows already exist; fixing the reader resurfaces them.

**Files touched.** `src/lambda_ask_agent.py`, `src/chunking.py`, `src/lambda_rag_search.py` (audit; possibly no change), tests (`test_chunking*.py`, ask-agent aggregation tests).

**Tested how.** Unit: an aggregation input mixing topic-ed and NULL-topic windows returns both, NULL-topic grouped by session/date; chunking metadata carries anchors; **hash-stability test**: `chunk_text` for a fixed report+turns fixture is byte-identical before and after (guards the embedding-sidecar contract). TEST e2e: ask a question on a TEST day known to have only NULL-topic windows; expect hits where today's count is 0.

**How you'd know it FAILED in production.** The exact BUG-39 signature returning: middle-panel Search returns 0 for words that are audibly in the transcript, `SELECT count(*) FROM report_chunks WHERE topic_id IS NULL AND created_at > <deploy>` growing while ask-agent answers cite none of them. Conversely, over-inclusion would show as RAG answers citing raw casual talk with no topic frame — check the aggregation grouping, not the search. Direct-invoke `fieldsight-prod-rag-search`/ask-agent (the BUG-39 debugging path, documented in CLAUDE.md) reproduces either in minutes.

**IAM / VPC / migrations.** None, none, none. Purely code. Independently shippable; also independently valuable (it un-breaks today's search).

---

## Task 6 — org-api read endpoints: briefing + keyword/entity search with time anchors

**Goal.** The mobile web page's data source (spec §5): serve the briefing, and serve literal keyword + entity-alias search over the transcript with word-level `HH:MM:SS` anchors and ±3-turn context. All alias handling and anchoring is already server-side (T3); these endpoints only READ.

**What changes — `src/lambda_org_api.py` + new repo module `src/repositories/transcript_search.py`:**
- `GET /api/org/sessions/{session_base}/briefing?date=&user=` — rebuilds the S3 key `briefings/{folder}/{date}/{session_base}.json` server-side and serves it, **copying `session_rolling` (line ~1322) verbatim as the template**: same caller-scope checks (the folder/user authorisation logic), same in-VPC S3 read through the gateway endpoint, same 404-when-absent shape. Never trust a client-supplied S3 key.
- `GET /api/org/search/transcript?q=&date_from=&date_to=&session=&k=` — FTS (`tsv @@ websearch_to_tsquery`) unioned with trigram similarity (`text % q`, ranked by `similarity()`) over `session_turns`, **scoped by the caller's accessible sites/company exactly the way `search_chunks` takes `accessible_site_ids`** — reuse the existing ACL machinery (`repositories/acl.py` / scope resolution), including the `None`-means-no-filter / `[]`-means-deny-all convention (memory: empty-list-means-no-filter — do not invert it). Each hit returns `{session_base, report_date, speaker, text, abs_start, source_filename, start_sec, turn_index}`; a `context=3` param returns ±3 turns by `turn_index` (spec §5's expandable context).
- **Entity-alias expansion, server-side:** before running FTS, look up `q` against `session_entities.name` and active `entity_aliases.alias` (scoped); expand the query to `name + aliases` so `PB Tech` finds the `PV Tech` turns (the spec's 9-mention recovery). Rejected aliases are never expanded.
- `GET /api/org/sessions/{session_base}/entities?date=&user=` — the entity chips with real mention counts, from `session_entities` (+ aliases).
- Routes register in `dispatch` beside the other `/sessions/` regex routes (~line 589).

**What does NOT change.** `/search`, `/ask`, `lambda_rag_search` (semantic path) untouched — spec §3 keeps pgvector as the semantic instrument; this adds the literal one beside it.

**Tested how.** org-api handler tests through `org.lambda_handler(make_event(...))` with `transcript_search` functions monkeypatched (the `test_org_api_sessions.py` pattern): auth scoping (a caller without site access gets no rows — pin the deny case, BUG-25's class), 404 on missing briefing, context window edges. Repo SQL: FakeConn for shape + RDS-Data-API rolled-back checks for ranking/`NULLS LAST` behaviour (the CLAUDE.md list of SQL defects the unit suite cannot catch). TEST e2e with `scripts/invoke_org_api.py` (**`MSYS_NO_PATHCONV=1`** — BUG-42, or every route 404s and reads like a routing bug).

**How you'd know it FAILED in production.** (a) Briefing 404s while `briefings/` objects exist → key-derivation or scope bug — compare the exact key in the log against `aws s3 ls`. (b) Search returns 0 for a word visibly in a session → either T4 never wrote rows for that session (artifact-vs-rows check from T4) or scope filtering ate them — re-run as platform_admin: if admin sees rows and the user doesn't, it is ACL/graded-roles (the BUG-38 `GRADED_ROLES` asymmetry — "member works, site list empty = environment variable, not code"). (c) 504s on these routes → an in-VPC egress was added by accident (BUG-36's 29s-hang signature); these endpoints must touch only Postgres + S3-via-gateway.

**IAM — explicit.** `OrgApiFunction` role: add `s3:GetObject` on `briefings/*` (+ `s3:ListBucket` prefix `briefings/*` for 404-vs-403). Nothing else — DB access exists. Deploy role: no new types; simulate anyway.

**New VPC endpoint requirement.** **None**, and keep it that way: any new AWS service call inside org-api needs its endpoint first (the QR/DynamoDB 504 recurrence under BUG-36 is the cautionary tale).

**Migrations.** Reads T2's tables; no new migration.

---

## Task 7 — Rollout wiring, monitoring, and TEST-only gating

**Goal.** Nothing reaches prod half-migrated, dark features stay dark, and each failure mode above has a place someone will actually see it.

**What changes.**
- **Feature switch:** `EnableBriefing` CFN Parameter (added in T3) wired like the existing pattern: `deploy.yml` passes `EnableBriefing=true` (TEST on), `deploy-prod.yml` passes nothing (prod defaults `false`). The switch gates the finalize sweep's request write — with it off, T3/T4 functions deploy but never fire. Copy the `PROD_AUTHORITY_FLIP` repo-variable pattern for the eventual prod enable (a deliberate variable flip + redeploy, not a code merge).
- **Workflow-wiring test:** extend the `test_template_workflow_parameter_wiring.py` family to pin that prod does NOT enable briefing implicitly and TEST does — one repo-variable edit must not be able to silently light it up on prod (the ElevenLabs-key lesson: guarded by a test, not a comment).
- **Manual wiring checklist** (BUG-33 — none of this is in the template, so it must be written down as prod-cutover steps in `scripts/wire-s3-events.sh` comments): `briefing_requests/` → BriefingFunction; `briefings/` (suffix `.json`) → BriefingWriterFunction; run on the TEST bucket now, on the prod bucket only at enablement.
- **Monitoring hooks (cheap, artifact-borne — no new infra):** the per-day counts that detect every silent failure named above: sessions closed vs. `briefings/` objects (T3 wiring dead), `briefings/` objects vs. `session_turns` distinct sessions (T4 wiring/identity dead), `anchor_stats.unmatched` ratio (T3 matcher broken), `batch_dedup.turns_dropped` ratio (T1 over/under-dedup). Land them as a short runbook section in `MONITORING.md` with the exact one-line queries/CLI; if a scheduled check is wanted later, it rides the existing sweep, not a new function.
- **Migration-to-main order:** merge sequence to `main` is 0042 (T2) together with or before T4/T6 code — never code that reads tables prod does not have. Since migrations on `main` auto-run on prod (BUG-38), the merge that carries 0042 IS the prod schema change; it is additive and safe, but it goes in the same release as the dark-switched code, not weeks apart, so no drift window exists.

**Tested how.** The wiring test above; a TEST full-path smoke: record → close → `briefing_requests/` → `briefings/` → rows → `GET /briefing` + `GET /search/transcript` end to end, checked with the four monitoring queries.

**How you'd know it FAILED in production.** The four counts, plus the standing BUG-43 metrics (account `Throttles`, org-api `5XXError`) during the first enabled week.

**IAM / VPC / migrations.** Nothing beyond what T2–T6 declared.

---

## Explicitly out of scope (per spec §7 — do not do these here)

- Web grounding cards (§6) — needs a backend fetch/search endpoint; specified separately.
- Standards lookup (NZS #### → clause).
- Retiring or modifying any current consumer (extractor, item-writer, reports, ask-agent semantic path).
- Backfilling `session_turns` for historic sessions (the corpus predating T3). Feasible later by replaying `briefing_requests/` artifacts per session; deliberately not in this plan.
- The mobile web page itself (frontend repo).

## Known risks the plan carries (from the spec §8, with owners in-plan)

- **Inferred tasks read as records** — `basis` is preserved end-to-end (T3 artifact, T4 rows if ever stored, T6 passthrough); presentation duty lands on the page, but the backend never drops the field.
- **Assignee structurally unavailable** — the briefing path never guesses an assignee; unassigned stays unassigned (T3 prompt requirement + no server-side fill).
- **Corpus-scale page weight** — solved by design here: cross-session search is served by T2/T6 server index, never by embedding turns in the page.
