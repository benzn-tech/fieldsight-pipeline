# Search Index From Extraction — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `report_chunks` rows from each extraction's Aurora topics within minutes of item-writer's commit, so whatever a user sees as a topic is searchable, and switch reads to that family behind a flag.

**Architecture:** item-writer (in-VPC) writes `index_requests/…` after commit → embed-report (non-VPC) builds chunks from the request plus THIS session's transcript turns, embeds with hash reuse, writes `index_vectors/…` → ingest (in-VPC) applies under the key's advisory lock, deciding currency from Aurora. Reads choose the family (`extractions/%` vs everything else) from `SEARCH_INDEX_READ`.

**Tech Stack:** Python 3.12 Lambdas, psycopg 3, Aurora PostgreSQL + pgvector, DashScope `text-embedding-v4` (1024), SAM, S3 notifications via `scripts/wire-s3-events.sh`.

**Spec:** `docs/superpowers/specs/2026-10-13-search-index-from-extraction-design.md` (Fable-reviewed 2026-10-14). Read it before any task.

## Global Constraints

- Works unchanged on develop (has `topics.superseded_at`, 0073/0074/0076) AND main (no such column; re-extraction deletes + re-inserts). Use `deleted_predicates.visible_topics_predicate(alias)` — it exists on both branches with branch-correct arms. Never reference `superseded_at` directly in new code.
- No migration. Families: session = `source_s3_key LIKE 'extractions/%'`; report = `source_s3_key IS NULL OR source_s3_key NOT LIKE 'extractions/%'`.
- Turns for a request come ONLY from `lambda_extract_session.gather_session_segments(bucket, folder, date, session_base)` + `assemble_session_turns(bucket, keys)`; `grp…` iterates the extraction's `mergedMembers` (each `{userFolder, date, sessionBase}` — verify field names in `lambda_extract_session.py:1875-1883`). Never `lambda_ingest._load_turns`.
- Chunk constants reused from `chunking.py`: `TARGET_CHARS=2600`, `OVERLAP_TURNS=2`, `TOPIC_SPLIT_CHARS=4500`, `ASSIGN_BUFFER_SEC=120`.
- Every DELETE/archive on `report_chunks` in the apply path carries `chunks.COMPANY_SCOPE` with `company_id = owner_company_id` (site's company).
- New request/vector artifacts written with `lake_put(..., owner_hint={"folder": folder, "session_base": base, "company_id": owner_company_id})`.
- Never `git add -A`; never `git stash`. Comments/commits in English. Commit trailer:
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` / `Claude-Session: https://claude.ai/code/session_014QUZA3uk6P8T7qYJoLRfQe`.
- Test harness: `export UV_LINK_MODE=copy AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_DEFAULT_REGION=ap-southeast-2; uv run --with-requirements requirements-test.txt python -m pytest -q <path>`. One pytest at a time.
- Revert-to-red: every guard test is shown failing with its fix reverted, once, before commit (say so in the commit body).

## Review Focus

1. A session recorded on the same day as another: no window may contain the other session's turns (Task 4 test with two sessions in one day).
2. Group merge race: member request applied AFTER the group's apply must not re-insert member chunks (Task 5 named test).
3. Recording deleted between request and apply: apply archives K's chunks and inserts nothing (Task 5).
4. Widening under `SEARCH_INDEX_READ=session`: the latest-date query must ignore report-family days (Task 2 Postgres test).
5. A NULL `source_s3_key` legacy chunk is report-family, never session (Task 2 test).

---

### Task 1: `session_index.build_chunks` (pure)

**Files:**
- Create: `src/session_index.py`
- Test: `tests/unit/test_session_index.py`

**Interfaces:**
- Consumes: `chunking.parse_time_range(tr) -> (start_sec, end_sec) | None`, `chunking.TARGET_CHARS`, `OVERLAP_TURNS`, `TOPIC_SPLIT_CHARS`, `ASSIGN_BUFFER_SEC`; turn dicts as produced by `assemble_session_turns`: `{speaker, text, abs_start (datetime), abs_start_str "HH:MM:SS", abs_end_str, src (filename)}` — verify the exact keys `assemble_session_turns` returns before writing the fixture; add `src` in the caller if absent.
- Produces: `build_chunks(request: dict, turns: list[dict]) -> list[dict]`; each chunk `{"chunk_type": "topic"|"transcript_window"|"session_digest", "chunk_text": str, "topic_id": str|None, "metadata": dict}`. `request` shape (Task 3 produces it): `{"extraction_key", "extracted_at", "tier", "site_id", "user_id", "owner_company_id", "report_date", "user_folder", "session_base", "member_bases": [str], "replaces": [str], "topics": [topic]}`, topic = `{"id", "title", "summary", "time_range", "category", "participants": [str], "decisions": [{"decision","rationale"}], "questions": [{"question"}], "action_items": [{"action","responsible","deadline"}], "findings": [{"observation","severity","domain"}], "work_class"}`.

Rules (spec §5, D7):
- Topic chunk text, one line each, omitted when empty: `[{time_range}] {title} ({category})`, `Participants: a, b`, summary, `Decision: {decision} — {rationale}`, `Action: {action} (responsible: X, due: Y)`, `Open question: {question}`, `Finding ({severity}): {observation}`. Split >4500 chars into parts, each part repeating the first line. `metadata = {"index":"session","tier":…,"time_range":…,"source_files":[session_base, *member_bases],"topic_title":title}`.
- `work_class == "non_work"` → no topic chunk, and its span (±0, NOT ±120) excludes turns from every window.
- Windows: assign each turn to the first work topic whose `parse_time_range` contains `abs_start` seconds-of-day ±120 s; pack per topic to ≤2600 chars on turn boundaries with 2-turn overlap; text lines `[{abs_start_str}] {speaker}: {text}`; `topic_id` = topic id. Unassigned turns: group by clock hour of `abs_start`, pack the same way, `topic_id=None`, `metadata.block={"from":"HH:00","to":"HH:59"}` clipped to the first/last turn's `HH:MM`. Window metadata: `{"index":"session","tier","window_span":"HH:MM:SS–HH:MM:SS","turns":n,"source_files":[sorted distinct turn src filenames]}` (en dash, as `chunking._window_metadata`).
- Digest: one chunk `Session {first HH:MM}–{last HH:MM}` + one line per work topic `- [{time_range}] {title}`; `topic_id=None`; metadata `{"index":"session","tier","source_files":[session_base,*member_bases]}`. Skip the digest when there are no work topics.

- [ ] **Step 1: Write failing tests** covering: (a) topic text layout with dict decisions/questions/actions/findings; (b) non_work topic yields no chunk and its turns appear in no window; (c) a turn at topic end +90 s goes to that topic, +200 s goes to an hour block; (d) two-turn overlap between consecutive windows of one topic; (e) unassigned turns at 09:10 and 10:20 form two blocks with `topic_id None`; (f) digest lists only work topics; (g) 5,000-char summary splits into 2 topic chunks both starting with the header line; (h) window `source_files` are the turn filenames, topic/digest `source_files` are `[session_base, *member_bases]`.

```python
from datetime import datetime
import session_index as si

def turn(hms, text, src="Ben_2026-10-12_11-49-00_sidaaaa_c0001.json", spk="spk_0"):
    h, m, s = map(int, hms.split(":"))
    return {"speaker": spk, "text": text, "abs_start": datetime(2026, 10, 12, h, m, s),
            "abs_start_str": hms, "abs_end_str": hms, "src": src}

REQ = {"extraction_key": "extractions/Ben/2026-10-12/sidaaaa.json", "extracted_at": "t1",
       "tier": "final", "site_id": "s", "user_id": "u", "owner_company_id": "c",
       "report_date": "2026-10-12", "user_folder": "Ben", "session_base": "sidaaaa",
       "member_bases": [], "replaces": [],
       "topics": [
         {"id": "T1", "title": "Ceiling Services", "summary": "Hangers need rework.",
          "time_range": "11:52 – 11:54", "category": "quality", "participants": ["Ben"],
          "decisions": [{"decision": "Rework hangers", "rationale": "spacing"}],
          "questions": [{"question": "Who signs off?"}],
          "action_items": [{"action": "Book inspector", "responsible": "Sam", "deadline": "Fri"}],
          "findings": [{"observation": "Hanger spacing 1.4m", "severity": "medium", "domain": "quality"}],
          "work_class": "work"},
         {"id": "T2", "title": "Banter", "summary": "chat", "time_range": "12:02 – 12:03",
          "category": "progress", "participants": [], "decisions": [], "questions": [],
          "action_items": [], "findings": [], "work_class": "non_work"}]}

def test_topic_text_layout():
    c = [x for x in si.build_chunks(REQ, []) if x["chunk_type"] == "topic"]
    assert len(c) == 1 and c[0]["topic_id"] == "T1"
    t = c[0]["chunk_text"]
    assert t.splitlines()[0] == "[11:52 – 11:54] Ceiling Services (quality)"
    assert "Decision: Rework hangers — spacing" in t
    assert "Action: Book inspector (responsible: Sam, due: Fri)" in t
    assert "Open question: Who signs off?" in t
    assert "Finding (medium): Hanger spacing 1.4m" in t

def test_non_work_span_is_excluded_from_windows():
    chunks = si.build_chunks(REQ, [turn("12:02:30", "private chat"), turn("11:53:00", "hangers")])
    texts = " ".join(c["chunk_text"] for c in chunks if c["chunk_type"] == "transcript_window")
    assert "private chat" not in texts and "hangers" in texts
```
(write (c)–(h) in the same style.)

- [ ] **Step 2: Run** `pytest tests/unit/test_session_index.py -q` → FAIL (module missing).
- [ ] **Step 3: Implement** `src/session_index.py` per the rules above (pure: no boto3, no psycopg).
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `git add src/session_index.py tests/unit/test_session_index.py`.

---

### Task 2: Read side — family filter on both SQL builders + rag-search switch

**Files:**
- Modify: `src/repositories/search_sql.py` (`_scope_predicate`, `build_search_sql`, `build_latest_date_sql`)
- Modify: `src/repositories/chunks.py` (`search_chunks`, `latest_visible_date` gain `source_family=None`)
- Modify: `src/lambda_rag_search.py` (read `SEARCH_INDEX_READ`, pass it, echo `index_family` in response)
- Test: `tests/unit/test_search_sql_family.py`, `tests/integration/test_search_family_pg.py`

**Interfaces:**
- Produces: `search_sql.family_predicate(alias) -> str` using bind `%(source_family)s`:
```python
def family_predicate(alias: str = "c") -> str:
    # NULL family = no filter (callers that predate the switch stay byte-identical).
    return (f"(%(source_family)s::text IS NULL "
            f"OR (%(source_family)s::text = 'session' AND {alias}.source_s3_key LIKE 'extractions/%%') "
            f"OR (%(source_family)s::text = 'report' AND ({alias}.source_s3_key IS NULL "
            f"OR {alias}.source_s3_key NOT LIKE 'extractions/%%')))")
```
  appended with `AND` in `_scope_predicate` and in `build_latest_date_sql`'s WHERE. `chunks.search_chunks(..., source_family=None)` and `chunks.latest_visible_date(..., source_family=None)` bind it. `lambda_rag_search`: `INDEX_FAMILY = os.environ.get("SEARCH_INDEX_READ", "report")`, passed to both calls; response gains `"index_family": INDEX_FAMILY`.

- [ ] **Step 1: Failing unit tests**: both builders' SQL text contains `family_predicate("c")`; `search_chunks`/`latest_visible_date` bind `source_family` (FakeConn capturing params); rag-search passes the env value to BOTH (monkeypatch `chunks.search_chunks` and `chunks.latest_visible_date`, assert kwargs) and echoes it.
- [ ] **Step 2: Failing Postgres test** (skips without `TEST_DATABASE_URL`; run via `fswork/run-tests.sh`): insert 3 chunks on one site — `reports/…` dated 10-12, `extractions/…` dated 10-08, NULL key dated 10-11; `latest_visible_date(..., on_or_before='2026-10-13', source_family='session')` == `'2026-10-08'`; with `'report'` == `'2026-10-12'`; search with `'session'` returns only the extractions row; NULL-key row only under `'report'`.
- [ ] **Step 3: Run** → FAIL. **Step 4: Implement.** **Step 5: Run** → PASS (unit + pg). Revert the `build_latest_date_sql` change once and show the pg test red.
- [ ] **Step 6: Commit** the four files.

---

### Task 3: Index request builder + item-writer hook

**Files:**
- Create: `src/index_request.py`
- Modify: `src/lambda_item_writer.py` (post-commit, beside `match_request.emit`, ~line 1622)
- Test: `tests/unit/test_index_request.py`, `tests/unit/test_item_writer_index_request.py`

**Interfaces:**
- Produces:
  - `REQUEST_PREFIX = "index_requests/"`, `VECTORS_PREFIX = "index_vectors/"`
  - `request_key(date, folder, base) -> f"index_requests/{date}/{folder}/{base}.json"`; `vectors_key(date, folder, base)` same under `index_vectors/`.
  - `live_topic_ids(conn, extraction_key) -> list[str]` — `SELECT t.id FROM topics t WHERE t.source_s3_key = %s AND {visible_topics_predicate('t')}`.
  - `build(conn, extraction_key, *, site_id, user_id, owner_company_id, report_date, user_folder, session_base, tier, extracted_at, member_bases=(), replaces=()) -> dict` — topics from `topics.get_topic_full(conn, id)` for each live id, mapped to the Task 1 topic shape (map `findings`, `action_items`; decisions/questions: read the children `get_topic_full` returns — if it does not return decisions/questions, add a small `SELECT decision, rationale FROM topic_decisions WHERE topic_id = ANY(%s)` / `topic_questions` query here, and verify those tables exist on main with `git show origin/main:src/migrations/` before relying on them; on main fall back to `[]` when absent).
  - The request also carries `members: [{"user_folder", "date", "session_base"}]` — for a `grp…` key, one entry per merged member (from the group extraction's member list); for a solo key, `[{"user_folder": user_folder, "date": report_date, "session_base": session_base}]`. Task 4 reads turns from exactly these.
  - `emit(s3_client, bucket, request) -> str` — `lake_put` JSON to `request_key(...)`; returns the key.
- item-writer: `SEARCH_INDEX_WRITE = os.environ.get("SEARCH_INDEX_WRITE", "false").lower() == "true"`. After `match_request.emit`, when `SEARCH_INDEX_WRITE`: open a short read connection, `build(...)`, `emit(...)`, all inside `try/except Exception: logger.exception(...)` + EMF metric `SearchIndexRequestFailed` (copy the EMF helper pattern used for `stranded` in `lambda_ingest._emit_stranded_metric`). `replaces` = the member extraction keys for `tier == "group"` (the keys `_supersede_member_topics` supersedes); `member_bases` = their session bases. Emit even when `topics_n == 0` — a windows-only session must still index its turns and clear stale chunks.

- [ ] **Step 1: Failing tests**: `build` maps a FakeConn topic row to the Task 1 shape; `live_topic_ids` SQL contains `visible_topics_predicate("t")`; item-writer emits exactly one request post-commit when the flag is on and none when off; an exception in `emit` does not raise out of the handler and logs; group tier request carries `replaces`.
- [ ] **Step 2–4:** run red → implement → run green.
- [ ] **Step 5: Commit.**

---

### Task 4: embed-report handler `embed_index_request`

**Files:**
- Modify: `src/lambda_embed_report.py` (new handler + routing in `lambda_handler`)
- Test: `tests/unit/test_embed_index_request.py`

**Interfaces:**
- Consumes: Task 1 `build_chunks`, Task 3 key helpers, `dashscope_utils.embed(texts) -> list[list[float]]`, `lambda_extract_session.gather_session_segments`, `assemble_session_turns` (import inside the function).
- Produces: `INDEX_REQUEST_RE = re.compile(r"^index_requests/[^/]+/[^/]+/[^/]+\.json$")`; `embed_index_request(key) -> dict` writes `index_vectors/{date}/{folder}/{base}.json` = `{**request, "chunks": [{**chunk, "embedding": [...]}]}` via `lake_put`.
- Turns: for `session_base.startswith("grp")` iterate the extraction's members (request.members, produced by Task 3); else one `(user_folder, report_date, session_base)`. Concatenate and sort by `abs_start`. Set `t["src"]` to the segment filename when `assemble_session_turns` does not.
- Reuse: read the existing vectors artifact (if any; NoSuchKey → empty) and map `sha256(chunk_text[:8000].encode()).hexdigest()` → embedding; embed only misses; when there are no misses, call `embed` zero times.

- [ ] **Step 1: Failing tests**: (a) **two sessions on one day** — `gather_session_segments` monkeypatched to return only this session's keys; assert no window contains the other session's text (fixture puts both in the listing the real function would filter; also test the real `gather_session_segments` with a stub S3 paginator listing both sessions' files); (b) reuse: second call with identical text → `embed` called 0 times; (c) group: turns from two members merged; (d) router: `index_requests/…json` goes to the new handler, `index_vectors/…` is ignored.
- [ ] **Step 2–4:** red → implement → green.
- [ ] **Step 5: Commit.**

---

### Task 5: ingest `apply_index_vectors` + backfill event

**Files:**
- Modify: `src/lambda_ingest.py` (router, new apply, backfill branch)
- Modify: `src/repositories/chunks.py` (add `delete_chunks_for_sources(conn, keys, company_id) -> int`)
- Test: `tests/unit/test_apply_index_vectors.py`, `tests/unit/test_index_backfill.py`

**Interfaces:**
- Produces: `INDEX_VECTORS_RE = re.compile(r"^index_vectors/[^/]+/[^/]+/[^/]+\.json$")`; `apply_index_vectors(key) -> dict` returning `{"index_applied": key, "outcome": "applied"|"stale"|"cleared", "chunks": n}`.
- Algorithm (spec D5), one transaction:
```python
with get_connection() as conn:
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (K,))
    live = set(index_request.live_topic_ids(conn, K))
    req_ids = {t["id"] for t in result["topics"]}
    if not live and req_ids:
        n = chunks.archive_chunks_for_session(conn, base, [], batch_id, company_id=cid)  # cleared
    elif req_ids and not (live & req_ids):
        outcome = "stale"
    else:
        chunks.delete_chunks_for_sources(conn, [K, *result["replaces"]], cid)
        for c in result["chunks"]:
            tid = c["topic_id"] if c["topic_id"] in live else None
            chunks.insert_chunk(conn, site_id, report_date, c["chunk_type"], c["chunk_text"],
                                c["embedding"], user_id=user_id, source_s3_key=K,
                                topic_id=tid, metadata=c["metadata"])
```
  Zero-topic request (windows-only): apply when `live` is also empty and the request's `extracted_at` equals the extraction object's `extracted_at` read with `s3().get_object` — ONLY in this branch (grant `GetObject extractions/*` to ingest only if needed; first check whether ingest already has it in template.yaml and, if not, skip windows-only sessions with a logged reason instead of adding the grant — document the choice in the commit).
  `batch_id` = `uuid.uuid4()`. `delete_chunks_for_sources` = `DELETE FROM report_chunks WHERE source_s3_key = ANY(%(keys)s)` + `COMPANY_SCOPE`.
- Backfill: `event.get("index_backfill")` → `{"limit", "dry_run"(default True), "user_folder"?, "date_from"?}`; `SELECT DISTINCT t.source_s3_key, t.site_id, t.user_id, t.report_date FROM topics t WHERE t.source_s3_key LIKE 'extractions/%' AND {visible_topics_predicate('t')} [filters] ORDER BY report_date DESC LIMIT %s`; for each, resolve `owner_company_id` from `sites.company_id`, `build` + `emit` unless dry-run; return `{"keys": n, "emitted": m, "dry_run": bool}`.

- [ ] **Step 1: Failing tests** (FakeConn): stale skip; cleared on empty live (tombstone); overlap applies and NULLs a topic not in live; **group race** — member request applied after the group's apply with the member's topics no longer live → cleared, nothing inserted; deletes carry `company_id`; the advisory lock is the first statement; backfill dry-run emits nothing and counts keys.
- [ ] **Step 2–4:** red → implement → green. Revert the "live ∩ req" check once and show the race test red.
- [ ] **Step 5: Commit.**

---

### Task 6: Edit routing + reindex regex fix

**Files:**
- Modify: `src/reindex.py` (`enqueue_topic_reindex`)
- Modify: `src/lambda_ingest.py` (`REINDEX_VECTORS_RE`)
- Test: `tests/unit/test_reindex_routing.py`

**Interfaces:**
- `REINDEX_VECTORS_RE = re.compile(r"^reindex_vectors/[^/]+/[^/]+/[^/]+\.json$")` (matches `reindex.vectors_key`).
- In `enqueue_topic_reindex`: if the topic's `source_s3_key` starts with `extractions/` AND `SEARCH_INDEX_WRITE` is on → build + emit an index request for that key (Task 3) and return its key; do NOT write a per-topic request. Otherwise the existing path, unchanged. org-api needs `SEARCH_INDEX_WRITE` in its env (Task 8).

- [ ] **Step 1: Failing tests**: the regex matches `reindex.vectors_key("2026-10-12","Ben","T1")`; extraction-sourced topic with flag on → exactly one index request, zero reindex requests; report-sourced → exactly one reindex request; flag off → existing behaviour.
- [ ] **Step 2–4.** **Step 5: Commit.**

---

### Task 7: Ask-agent rendering of session rows

**Files:**
- Modify: `src/lambda_ask_agent.py` (`_folder_from_source`, `_aggregate_topics`, `_rag_search_list` passes `index_family` from rag-search's response)
- Test: `tests/unit/test_aggregate_session_family.py`

**Interfaces:**
- `_folder_from_source("extractions/Ben/2026-10-12/sidaaaa.json") == "Ben"`.
- `_aggregate_topics(chunks, question="", browse=False, family="report")`: when `family == "session"`, windows with `topic_id None` group by `(source_s3_key, metadata["block"]["from"])` into one row `{"title": f"Recording {from}–{to}", "topic_id": None, "chunk_type": "transcript_window", …}` (same row keys as existing rows; read the existing row dict construction and mirror every key); `session_digest` rows titled `f"Session {span}"`. Distance cut-off and lexical-first unchanged. `family == "report"` → byte-identical to today (existing tests untouched).

- [ ] **Step 1: Failing tests** for the folder arm, block grouping (two windows same block → one row), digest row, and report-family unchanged.
- [ ] **Step 2–4.** **Step 5: Commit.**

---

### Task 8: Wiring — template, workflows, S3 notifications, IAM

**Files:**
- Modify: `src/template.yaml` (Parameters `SearchIndexWrite` default `'false'`, `SearchIndexRead` default `'report'` AllowedValues `[report, session]`; Environment on ItemWriter/OrgApi/Ingest (`SEARCH_INDEX_WRITE`) and RagSearch (`SEARCH_INDEX_READ`); IAM per spec §4.1)
- Modify: `.github/workflows/deploy.yml`, `.github/workflows/deploy-prod.yml` (`SearchIndexWrite=${{ vars.TEST_SEARCH_INDEX_WRITE || 'false' }}`, `SearchIndexRead=${{ vars.TEST_SEARCH_INDEX_READ || 'report' }}`, PROD_ likewise — copy the `AuthorityFlip` lines' exact shape)
- Modify: `scripts/wire-s3-events.sh` (EmbedReport ← `index_requests/` + `.json`; Ingest ← `index_vectors/` + `.json`)
- Test: extend `tests/unit/test_template_workflow_parameter_wiring.py` (or the file that pins `AuthorityFlip` wiring — grep for it) so both new parameters are wired in both workflows and reach the right functions; a test that the wire script's new rules have disjoint prefixes from every existing rule.

- [ ] **Step 1: Failing wiring tests.** **Step 2:** implement. **Step 3:** green + `sam validate --lint` via the repo's existing CI step (or `cfn-lint` pinned version per memory).
- [ ] **Step 4: Commit.**
- [ ] **Step 5 (after TEST deploy, Task 9):** `aws iam simulate-principal-policy` for each role/action/resource pair in spec §4.1 against the deployed TEST roles; all `allowed`.

---

### Task 9: TEST rollout + verification

- [ ] Open PR to develop; merge when CI green.
- [ ] Set repo vars: `TEST_SEARCH_INDEX_WRITE=true`, `TEST_SEARCH_INDEX_READ=session`, confirm `TEST_AUTHORITY_FLIP=true` (set if missing). Redeploy TEST (workflow_dispatch or the merge's run); confirm the run's `headSha`.
- [ ] Task 8 Step 5 (simulate-principal-policy).
- [ ] Backfill TEST: `aws lambda invoke --function-name fieldsight-test-ingest --payload '{"index_backfill":{"limit":500,"dry_run":true}}'` then `dry_run:false`. Check `index_vectors/` objects appear and `SELECT count(*) FROM report_chunks WHERE source_s3_key LIKE 'extractions/%'` grows on `fieldsight_test`.
- [ ] Re-run as test2 (sub `398e3438-70e1-704c-b2cc-b28e33d7ffb1`) via `fieldsight-test-ask-agent` mode=search: "What did we do for Level two inspection?" must return 10-08/10-12 Level 2 rows; "What did we do yesterday?" with `tz=Pacific/Auckland` must browse the latest recorded day.
- [ ] Chrome: dev site, test2, same two searches.

### Task 10: prod release

- [ ] Release branch from `origin/main` (`release/search-index-from-extraction-2026-10-14`), cherry-pick Tasks 1–8 commits; resolve against main's code (no `superseded_at`, item-writer delete+reinsert). Full suite green on the branch; CI green.
- [ ] Ship with `PROD_SEARCH_INDEX_WRITE=true`, `PROD_SEARCH_INDEX_READ=report`; owner approves the deploy; owner runs prod backfill (dry-run then apply).
- [ ] Ruler (fs-eval) on prod, both families ×3; flip `PROD_SEARCH_INDEX_READ=session` only if spec §2 criteria hold.
