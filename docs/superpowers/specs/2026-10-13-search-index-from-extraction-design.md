# Search index built from the extraction write path

Status: design (2026-10-13). Owner approved the restructure in principle ("改结构没问题，只要效果好，架构优质").
Review: Fable 2026-10-14 — approve with changes; C1–C3 and I1–I6 incorporated (D5, D9, §4.1, §5, §6, §7, §8, §10).

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
| D4 | Same two-hop shape as today: in-VPC writes a request, non-VPC embeds, in-VPC applies. | BUG-36: in-VPC Lambdas have no internet. `lambda_embed_report` and `lambda_ingest` already own those roles, so no new functions are needed. New S3 grants are required and listed in §4.1. |
| D5 | **Currency is decided in Aurora, at apply time, under the key's advisory lock.** The apply step reads K's live topics, `SELECT id FROM topics t WHERE t.source_s3_key = K AND <visible topic predicate>`. That predicate has two arms on main and three on develop (adds `superseded_at IS NULL`); the repo function reuses whichever `deleted_predicates` the branch carries. **Empty live set** → archive K's chunks and insert nothing. This covers a tombstoned source, a member superseded by a group merge, and S3 being ahead of Aurora. **Live set disjoint from `request.topics[].id`** → stale, skip. **Overlap** → apply, and set `topic_id` to NULL for any chunk whose topic is not in the live set. `extracted_at` is only a tiebreak, used when the request and Aurora both hold zero topics (a windows-only session). | Fable review C2. An S3 `extracted_at` comparison both re-inserts a member's chunks after a group merge (the member's object is unchanged) and skips forever when item-writer exits before writing (tombstone, identity miss, group suppress). Aurora is what the screen shows, so it is the authority. The rule is schema-agnostic: prod deletes and re-inserts topics, develop supersedes them. Ingest therefore never needs `GetObject extractions/*`. |
| D6 | The embedder reuses vectors by text hash from the key's previous vectors artifact. | Live passes re-run every ≥90 s during long sessions, and most chunk texts are unchanged between them. |
| D7 | Non-work topics produce no topic chunk, and transcript turns inside a non-work topic's span produce no window. | Same rule as `chunk_report` today, extended to windows (life-conversation separation). |
| D8 | Rollout is two switches: `SEARCH_INDEX_WRITE` (session index on/off) and `SEARCH_INDEX_READ` (`report` / `session`). Each is wired in three parts: a template Parameter, Environment on the functions that read it, and `--parameter-overrides` from `TEST_*`/`PROD_*` repo variables in both workflows (the same pattern as `AuthorityFlip`). WRITE goes on item-writer, org-api (edits) and ingest (backfill). READ goes on rag-search only, and rag-search echoes the family in its response so ask-agent renders from it rather than from a second env copy. Families: **session** = `source_s3_key LIKE 'extractions/%'`; **report** = everything else, including NULL. **No migration is needed.** | The two row families coexist in one table with no duplicate hits. The flip and the rollback are a repo-variable change plus a redeploy. |
| D9 | **Turns come from the session only.** The embedder uses `gather_session_segments(bucket, folder, date, session_base)` + `assemble_session_turns(bucket, keys)` (lambda_extract_session.py:661, :1669). For a `grp…` key it iterates `mergedMembers`, each with its own folder and date. It never uses the day-wide `_load_turns`. | Fable review C1. Day-wide turns would stamp other sessions' speech under K, so deleting session B would leave B's words searchable inside A's windows. |

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
   │ load THIS session's turns only (D9): gather_session_segments + assemble_session_turns;
   │   grp… → each mergedMembers entry with its own folder/date
   │ build chunks with session_index.build_chunks(request, turns)   ← pure
   │ reuse vectors from index_vectors/{…}/{base}.json by text hash, embed the rest;
   │   when every hash is reused, make zero DashScope calls
   ▼
S3  index_vectors/{date}/{folder}/{base}.json  {request…, chunks:[{…, embedding}]}
   ▼
ingest (in-VPC)                            new handler: apply_index_vectors
   BEGIN; pg_advisory_xact_lock(hashtext(K))
   live = K's live topic ids (D5)
   live empty            → archive K's chunks (archive_chunks_for_session batch), insert nothing
   live ∩ request = ∅    → skip (stale)
   otherwise             → DELETE chunks WHERE source_s3_key IN (K, *replaces) AND company scope
                           INSERT chunks; topic_id := NULL where topic not in live
   COMMIT
```

### 4.1 IAM grants this needs (verified missing, Fable review I1)

| Function | New grant |
|---|---|
| item-writer | `s3:PutObject` on `index_requests/*` |
| org-api (edit path) | `s3:PutObject` on `index_requests/*` |
| embed-report | `s3:GetObject` on `index_requests/*`, `s3:GetObject` + `s3:PutObject` on `index_vectors/*` (D6 reads the previous artifact). Its existing ListBucket condition (`transcripts/*`) already covers `gather_session_segments`. |
| ingest | `s3:GetObject` on `index_vectors/*`; `s3:PutObject` on `index_requests/*` (backfill) |

Each grant is checked with `simulate-principal-policy` against the deployed TEST role before
the PR merges (BUG-43 #3). A missing grant is swallowed as a logged warning at runtime, so the
test step is the only place it would surface.

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

`metadata.source_files` follows the rule the existing archive path depends on (Fable
review I3; `archive_chunks_for_session`, chunks.py:142-178). Windows carry the
**transcript filenames** they were built from, which name the member sids, so a deleted
member's speech inside a `grp…` window is still found. Topic and digest chunks carry
**every member session base**. A member's tombstone never matches a `grp…` key by prefix.
That is the same gap topics have today, and the archive arm covers it. All chunks also carry
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
| Recording deleted (tombstone) | The source arm of `visible_chunks_predicate` hides them at once (D3). Item-writer writes nothing for a tombstoned source, so a pending request finds an empty live set and archives K's chunks (D5). The archive keeps undelete symmetric: `restore_chunks_for_batch` re-binds only live topics. |
| Topic deleted (redaction) | The existing topic arm hides it. |
| Human content edit (org-api) | **Edits are routed by the topic's source family** (Fable review I2). Extraction-sourced topic → an index request for its extraction key, built from Aurora by the same builder item-writer uses. Report-sourced topic → the existing per-topic chain, with `REINDEX_VECTORS_RE` fixed to match `reindex_vectors/`, so those edits stop being dropped. One edit never takes both paths. The per-topic chain retires together with the report path. |
| Report path, report-sourced topics | Untouched while `SEARCH_INDEX_READ=report`. After the flip, report chunks are excluded by the read filter. Retiring `embed-report`'s `daily_report.json` trigger is a later cleanup PR. |

## 7. Read side

- **Both** SQL builders take `source_family` (D8 families): `build_search_sql` through
  `_scope_predicate` (both arms), and `build_latest_date_sql`, which has its own inline WHERE
  (search_sql.py:180-187, Fable review C3). Without the second one, widening picks a day that
  holds only the other family, and Ask answers empty. `lambda_rag_search` reads
  `SEARCH_INDEX_READ` (default `report`), passes it to both and echoes it in the response. A
  request field never overrides it. Both builders are tested on real Postgres.
- `_folder_from_source` (lambda_ask_agent.py:28-37) gains the `extractions/{folder}/…` arm,
  so session chunks deep-link with `&user=` (Fable review I5).
- `_aggregate_topics`, in session mode: rows group by `topic_id`. Windows with no topic
  group by `(source_s3_key, metadata.block)` into one row titled "Recording HH:MM–HH:MM".
  This **reverses the 2026-07-10 preference** that dropped topic-less windows. The decision is
  taken on evidence: the 09-15 ruler measured the rollup as the largest list gain (51% → 74%).
  It is kept only if the §8 ruler run confirms that gain. `session_digest` rows render as the
  session, and the ruler also decides whether the digest stays, because it can displace
  specific topics on broad queries. The `_NO_LEX_MAX_DIST` cut-off and the lexical-first
  order are unchanged.
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
1. **TEST**: merge with `SEARCH_INDEX_WRITE=true`, `SEARCH_INDEX_READ=session`, **and
   `TEST_AUTHORITY_FLIP=true`**. With the flip off, a non-flip nightly ingest supersedes
   (develop) or deletes (main) the day's whole `extractions/` prefix (lambda_ingest.py:766), and
   every session topic chunk goes dark after the nightly (Fable review I6). prod already runs
   with `PROD_AUTHORITY_FLIP=true`, which must be confirmed before prod step 3. Backfill TEST.
   Check test2's two questions in Chrome.
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
- Unit (FakeConn): apply. Cases: live set disjoint from the request → skipped; empty live set
  (tombstone, group-superseded member, S3 ahead of Aurora) → archived, nothing inserted;
  overlap → topics outside the live set get `topic_id NULL`; `replaces` deletes members;
  deletes carry the company scope; everything runs under the advisory lock. The group-merge
  race (member request applied after the group's) is a named test.
- Unit: the embedder reads only the session's own segments (a second session on the same day
  is present in the fixture and must not appear in any window).
- Unit: edit routing by source family (extraction → index request, report → per-topic chain,
  never both); `REINDEX_VECTORS_RE` matches the key `reindex.vectors_key` writes.
- Unit: request writer failures never raise into item-writer. The embedder reuses
  vectors by hash (embed call count asserted).
- SQL against local Postgres (`fswork/run-tests.sh`, pgserver + pgvector): the
  `source_family` filter on both arms of `build_search_sql` AND on `build_latest_date_sql`
  (NULL `source_s3_key` counts as report); the live-topic query on the branch's predicate; the visibility
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
- **Tenancy**: every apply-side DELETE carries the `COMPANY_SCOPE` pattern (chunks.py:37),
  keyed on item-writer's `owner_company_id` (the site's company, project-owned tenancy), so a
  request key never reaches another company's rows. `lake_put` owner hints carry the same
  company id.
- **Transient on develop**: a final's commit unbinds the live pass's chunks a few minutes
  before the final's apply lands. In that window they render title-grouped without a UUID.
  This is acceptable.
- **Carry-forward of human edits exists only on develop** (`carry_forward_apply.py`). On main,
  D1's "rows include edits" reduces to "rows as written". This is correct for each branch.
- **Cross-session conflicts**: item-writer is also edited by other sessions. The hook is a
  single post-commit call into a new module, to keep merge conflicts to one line.
