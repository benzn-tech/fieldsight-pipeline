# Search index built from the extraction write path

Status: design (2026-10-13). Owner approved the restructure in principle ("改结构没问题，只要效果好，架构优质").
Review: Fable, before the implementation plan.

## 1. Problem (measured 2026-10-13)

Search and Ask read `report_chunks`. Those rows are only written when a nightly
`daily_report.json` lands (`lambda_embed_report` → `embeddings/{date}/{folder}/vectors.json`
→ `lambda_ingest.ingest_report`). The web UI, by contrast, shows the topics the
extraction path writes to Aurora per session. The index and the screen read two
different topic sets, produced by two independent model passes.

Measured consequences:

| Symptom | Evidence |
|---|---|
| TEST index frozen at 2026-10-01 | All TEST report schedules are `DISABLED`. test2 (`Ben_Lin_test2`) has ~60 live topics on 10-06/08/09/12, including "Level 2 Pour Booking and Hold Point", and **0 chunks**. "What did we do for Level two inspection?" returns only 10-01 Level One rows (2/2 runs). |
| Same-day content unsearchable on prod | Chunks exist only after the nightly report. |
| 30–50% of prod topics have a linked chunk | Weekly counts, Aug 24 – Oct 12. |
| Link is title-only | `_overlap_or_title_score` reads `topics.occurred_at`, which no writer sets. prod 10-12: 7 report topics vs 7 extraction topics, 3 linked. "Level 2 Ceiling Services and Hanger Rework 11:52–11:55" did not link to "Ceiling Services Pre-Close Documentation 11:52–11:54". |
| Report ≠ extraction | The report merges (16→9) and covers spans with no extraction topic (report to 12:30, extraction to 12:12). |
| Human content edits never reach the index | `reindex.py` writes vectors to `reindex_vectors/…`; `lambda_ingest.REINDEX_VECTORS_RE` only matches `reindex_requests/…vectors.json`, so the apply branch never runs and the key is logged as "non-embeddings" and skipped. |

The vector store itself is not the bottleneck: prod has 1,464 rows (25 MB). The planner
uses `(site_id, report_date)` plus an exact sort, and HNSW is not used.

## 2. Goal and non-goals

Goal: whatever a user can see as a topic on screen is searchable minutes after the
extraction lands. The chunk carries that topic's id from the moment it is written. The
chunk follows the topic through re-extraction, edits and deletion.

Success criteria:
- The retrieval ruler (gold v1 + a v2 that adds short-identifier and "unlinked window"
  items) scores at least the current prod config on every category, measured 3× to get
  a noise floor, before the read switch flips on prod.
- test2's "What did we do for Level two inspection?" returns the 10-08/10-12 Level 2
  topics on TEST.
- No chunk of a deleted recording or a deleted topic is ever returned (existing
  visibility predicate plus the source arm, §6).

Non-goals (YAGNI): image/caption chunks; per-action-item chunks (actions stay folded
into the topic chunk, and the keyword arm already reaches them); reranking (measured
separately); HNSW tuning; a "live layer" UI.

## 3. Decisions

| # | Decision | Why |
|---|---|---|
| D1 | The index source is the **Aurora topic rows of one extraction key**, read after item-writer commits. It is not the raw extraction JSON. | The rows include carried-forward human edits and the real UUIDs. The JSON has neither. |
| D2 | **Every tier is indexed** (live and final). The latest extraction for a key replaces the previous chunks atomically. | Same-day search within minutes. Both tiers write the same key, so replace-by-key handles it. Unchanged text reuses vectors (D6), so re-embedding cost stays small. |
| D3 | Chunk `source_s3_key` = the **extraction key** (`extractions/{folder}/{date}/{base}.json`). | The existing recording-tombstone arm of `visible_chunks_predicate` (prefix match on `extractions/{folder}/{date}/sid…` / `grp…`) then hides deleted recordings directly. Today it never matches, because chunks carry `reports/…`. |
| D4 | Same two-hop shape as today: in-VPC writes a request, non-VPC embeds, in-VPC applies. | BUG-36: in-VPC Lambdas have no internet. `lambda_embed_report` and `lambda_ingest` already own those roles, so no new functions or IAM principals are needed. |
| D5 | Staleness is decided by `extracted_at`. It is never decided by `superseded_at`. | prod/main has no 0073/0074/0076 (stable identity), and re-extraction there deletes and re-inserts topics. One rule has to work on both schemas: a request is current iff its `extracted_at` equals the `extracted_at` of the extraction object at apply time. |
| D6 | The embedder reuses vectors by text hash from the key's previous vectors artifact. | Live passes re-run every ≥90 s during long sessions, and most chunk texts are unchanged between them. |
| D7 | Non-work topics produce no topic chunk, and transcript turns inside a non-work topic's span produce no window. | Same rule as `chunk_report` today, extended to windows (life-conversation separation). |
| D8 | Rollout is two switches: `SEARCH_INDEX_WRITE` (session index on/off) and `SEARCH_INDEX_READ` (`report` / `session`). Search filters by `source_s3_key` prefix. **No migration is needed.** | The two row families coexist in one table with no duplicate hits. The flip and the rollback are an env change. |

## 4. Data flow

```
item-writer (in-VPC)                       commit topics for extraction_key K
   │ post-commit, if SEARCH_INDEX_WRITE
   ▼
S3  index_requests/{date}/{folder}/{base}.json
   │  {extraction_key, extracted_at, tier, site_id, user_id, report_date,
   │   replaces: [other extraction keys whose topics K superseded (group merge)],
   │   topics: [ {id, title, summary, time_range, category, participants,
   │              decisions[], questions[], action_items[], findings[], work_class} ] }
   ▼
embed-report (non-VPC)                     new handler: embed_index_request
   │ load the session's transcript turns (assemble_session_turns / _load_turns)
   │ build chunks with session_index.build_chunks(request, turns)   ← pure
   │ reuse vectors from index_vectors/{…}/{base}.json by text hash, embed the rest
   ▼
S3  index_vectors/{date}/{folder}/{base}.json  {request…, chunks:[{…, embedding}]}
   ▼
ingest (in-VPC)                            new handler: apply_index_vectors
   BEGIN; pg_advisory_xact_lock(hashtext(K))
   if extraction object's extracted_at != request.extracted_at → skip (stale)
   if the source is tombstoned → delete K's chunks, insert nothing
   DELETE report_chunks WHERE source_s3_key IN (K, *replaces)
   INSERT chunks (topic_id kept only if the topic row still exists)
   COMMIT
```

Prefixes: `index_requests/` and `index_vectors/` are distinct, so the S3 notification
rules stay unambiguous. This is the same lesson as `reindex_requests/` vs
`reindex_vectors/` (reindex.py:25-31). Both rules are added to
`scripts/wire-s3-events.sh`. The deploy workflows already run it with `--apply` on TEST
and prod. Both new artifact families are written with `lake_put`, so the lake ownership
index records them.

Request writing never fails the item-writer. It runs post-commit, and a failure is
logged and counted as an EMF metric `SearchIndexRequestFailed`. A missed request is
repaired by the backfill (§8).

## 5. Chunk shapes (`src/session_index.py`, pure)

All chunks carry `metadata.source_files` (the session base), so the existing
`archive_chunks_for_session` (chunks.py:28-37) still finds them. They also carry
`metadata.index = "session"` and `metadata.tier`.

1. **topic**: one per work topic. The text keeps today's `_topic_text` layout so the
   keyword arm and the ruler stay comparable: `[time_range] title (category)`,
   Participants, summary, `Decision:` (from `decisions[].decision`, plus the rationale
   when present), `Action:` (with responsible and deadline), `Open question:` (from
   `questions[].question`), and `Finding (severity):` lines. It splits at 4,500 chars as
   today. `topic_id` is the real UUID.
2. **transcript_window**: the session's turns are bucketed by **these same topics'**
   `time_range` ±120 s and packed to 2,600 chars with 2-turn overlap (today's
   constants). `topic_id` is set for assigned windows. Unassigned turns become windows
   with `topic_id NULL` and `metadata.block = {from, to}`: the session's own span,
   split by clock hour. That is the "rollup" the 09-15 ruler measured (20.5% → 74%),
   now produced at write time.
3. **session_digest**: one per session. It is a code-built list of the session's topic
   titles with time ranges, plus the session span, and `topic_id NULL`. It serves broad
   questions ("what did we do yesterday on Level 2") without a model call.

The reuse of `chunking.py`: `_topic_text` and the window packer are refactored to
accept a neutral topic dict, so the report path keeps working unchanged until it is
retired.

## 6. Lifecycle

| Event | What happens to the index |
|---|---|
| Live pass → newer live pass → final | Each commit writes a request. The apply step replaces K's chunks. Stale requests are skipped (D5). |
| Group merge (`grp…`) | The group's request lists the member keys in `replaces`. The apply step deletes the members' session chunks in the same transaction. |
| Recording deleted (tombstone) | The source arm of `visible_chunks_predicate` hides them at once (D3). The apply step also deletes them, and later requests for a tombstoned key insert nothing. |
| Topic deleted (redaction) | The existing topic arm hides it. |
| Human content edit (org-api) | `reindex.enqueue_topic_reindex` writes an **index request for the topic's extraction key** (built from Aurora, the same code as item-writer), replacing the per-topic path. The topic-only reindex chain and its unreachable apply branch (`REINDEX_VECTORS_RE`) are removed once the session read is live. Until then the regex is fixed in the same PR, so edits on report-sourced topics stop being dropped. |
| Report path, report-sourced topics | Untouched while `SEARCH_INDEX_READ=report`. After the flip, report chunks are excluded by the read filter. Retiring `embed-report`'s `daily_report.json` trigger is a later cleanup PR. |

## 7. Read side

- `search_sql.build_search_sql` takes `source_family` (`report` → `source_s3_key LIKE 'reports/%'`,
  `session` → `LIKE 'extractions/%'`), applied in `_scope_predicate`, so both arms and
  `latest_visible_date` honour it. `lambda_rag_search` reads `SEARCH_INDEX_READ` (default
  `report`) and passes it through. A request field never overrides it.
- `_aggregate_topics`, in session mode: rows group by `topic_id`. Windows with no topic
  group by `(source_s3_key, metadata.block)` into one row titled "Recording HH:MM–HH:MM".
  `session_digest` rows render as the session. The `_NO_LEX_MAX_DIST` cut-off and the
  lexical-first order are unchanged.
- Ask uses the same chunks. Citations already read `metadata.window_span`.
- The search box's time-phrase handling and date-browse ship separately
  (`feat/search-box-time-phrases`) and are orthogonal.

## 8. Backfill and rollout

The backfill is an ingest Lambda event, `{"index_backfill": {"limit": N, "dry_run": true|false,
"user_folder"?: str, "date_from"?: str}}`. It is dry-run unless `dry_run` is false. It
writes one index request per live extraction key, built from Aurora with the same
builder item-writer uses. It is in-VPC, and the embedding happens downstream as usual. It is idempotent: a re-run replaces with the same text, and
D6 reuses the vectors.

Order:
1. **TEST**: merge with `SEARCH_INDEX_WRITE=true`, `SEARCH_INDEX_READ=session`. Backfill
   TEST. Check test2's two questions in Chrome.
2. **prod**: release branch from `main` carrying only this change (main ≠ develop: 0073/0074/0076
   are absent on prod, and D5 keeps the code schema-agnostic). Ship with
   `SEARCH_INDEX_WRITE=true`, `SEARCH_INDEX_READ=report`. Then the owner runs the backfill
   (dry-run, then apply).
3. Run the ruler on prod, read-only, both families, 3× each. Flip `PROD_SEARCH_INDEX_READ=session`
   only if §2's criteria hold. Rollback is flipping it back.
4. Later PR: retire the report-path chunking and the topic-only reindex.

Cost: about 15 chunks per session. With D6 reuse, live re-passes embed only changed
text. DashScope `text-embedding-v4` costs a fraction of a cent per session. The prod
backfill is about 300 sessions, under $0.10.

## 9. Testing

- Unit, pure: `session_index.build_chunks`. Cases: topic text layout including dict
  decisions and questions; non-work exclusion for topics and windows; window ↔ topic
  assignment by `time_range`; unassigned windows → hour blocks; digest content;
  `source_files` stamping; the 4,500-char split.
- Unit (FakeConn): apply. Cases: a stale `extracted_at` is skipped; a tombstoned source
  deletes and inserts nothing; `replaces` deletes members; a missing topic row leads to
  `topic_id NULL`; everything runs under the advisory lock.
- Unit: request writer failures never raise into item-writer. The embedder reuses
  vectors by hash (embed call count asserted).
- SQL against local Postgres (`fswork/run-tests.sh`, pgserver + pgvector): the
  `source_family` filter on both arms and on `latest_visible_date`; the visibility
  predicate hides a tombstoned `extractions/…/sid…` chunk.
- Revert-to-red: each guard test is shown failing with its fix reverted (memory:
  ci-green-over-a-dead-path).
- TEST e2e: test2 backfill, then search for "Level two inspection" and Ask "what did we
  do yesterday". Chrome check on the dev site.
- Ruler: §8 step 3.

## 10. Risks

- **Live churn**: many requests during a long session. Mitigated by D5 (stale skip) and
  D6 (hash reuse). The apply step is one small transaction.
- **Window bucketing assumes topic `time_range` is the device wall clock**, as the report
  path does today. Topics without a parsable range get no windows. Their turns fall to
  hour blocks, so nothing is dropped.
- **Two families during dual-write**: storage roughly doubles (about 25 MB → 50 MB on
  prod). This is negligible.
- **Cross-session conflicts**: item-writer is also edited by other sessions. The hook is a
  single post-commit call into a new module, to keep merge conflicts to one line.
