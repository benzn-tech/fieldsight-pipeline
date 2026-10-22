# Plan: Ask v2 mode for the retrieval-eval ruler

Companion to `2026-10-22-ask-v2-mode-spec.md` as amended by
`2026-10-22-ask-v2-mode-review.md` (B1-B3, S1-S10, N1-N8 folded in). Branch
`feat/retrieval-eval-baseline`, worktree `C:/Users/camil/wt/fs-eval`. Python:
`C:\Users\camil\fspy\Scripts\python.exe` (psycopg 3.3.6, pgvector, boto3 present).

Rules for every task: TDD with fakes where the thing is testable offline; one task = one
commit; no task touches AWS, prod, TEST or a vendor until Task 11 (the pilot), which is gated
on a printed call/token count. Nothing here is imported by a Lambda. Results go to
`tools/retrieval-eval/results/` on this branch only.

Layout (new files under `tools/retrieval-eval/`):

```
askv2/__init__.py
askv2/env.py          worker/rag-search config fetch, env copy, secret injection   (T8)
askv2/adapter.py      Data API read-only connection adapter                         (T2-T4)
askv2/render.py       psycopg template -> SQL literal renderer + allow/deny list    (T2)
askv2/harness.py      FAKE lambda client, patches, guards, log capture, proxies     (T5-T6)
askv2/funnel.py       pure scoring over recorded artefacts                          (T7)
askv2/report.py       per-run summary, noise floor, extrapolation                   (T10)
run_ask_v2.py         CLI: --pilot / --yes / --family / --repeat / --scope-site     (T9-T11)
tests/askv2/          pytest, offline; `test_*_pg.py` need the local pgserver       (all)
```

Local Postgres tests run one at a time via
`bash /c/Users/camil/fswork/run-tests.sh tools/retrieval-eval/tests/askv2/test_x_pg.py -q`.

## Task 0 — Source export and seam assertions (offline)

Files: `askv2/harness.py` (`export_src`, `assert_seams`), `tests/askv2/test_seams.py`.

- Reuse `run.py._export_src(EVAL_SRC_REF)` (default `origin/develop`; `origin/main` once
  #1157 lands). Set the import-time env BEFORE `sys.path.insert`: run.py's placeholders
  (`S3_BUCKET`, `ANTHROPIC_API_KEY`, `AWS_DEFAULT_REGION`, `DASHSCOPE_EMBED_*`,
  `DASHSCOPE_RERANK_TIMEOUT_SECONDS=20`) plus `RAG_SEARCH_FUNCTION=eval-in-process`,
  `ENABLE_WEB_ANSWER=false`, `ASK_CONVERSATION_MEMORY=false`, `SEARCH_INDEX_READ=<family>`,
  and (T8) the copied worker values; `ASK_DEADLINE_SECONDS` unset.
- `assert_seams()` checks, with `inspect`, that each patch point exists with the expected
  kind, else `SystemExit("seam moved: <name>")`: `ask_orchestrator.answer` signature has
  `tools`, `chat`, `turn_id`; `ask_orchestrator.DEADLINE_S` dict; `lambda_ask_agent.
  _get_lambda_client` function; `lambda_ask_agent.RAG_SEARCH_FUNCTION == "eval-in-process"`;
  `lambda_rag_search.get_cached_connection` / `close_cached_connection` names; `lambda_rag_
  search.INDEX_FAMILY == family`; `lambda_ask_agent.RERANK_ENABLED` equals the copied env;
  `ask_tools.{TOOLS, offered_tools, RefAssigner, ToolContext, execute}`;
  `ask_orchestrator._cited_card_refs`, `_final_of`; `db.connection.get_connection`.
- Test: import the exported `src` (same placeholders), call `assert_seams()`; a second test
  monkeypatches one name away and expects `SystemExit`.

## Task 1 — Gold pre-pass classification (offline, pure)

Files: `askv2/funnel.py` (`classify_question`), `tests/askv2/test_classify.py`.

- For each gold item compute with the real modules: `query_slots.time_range(q, today)`,
  `metric_slots.detect(q)` (fast path only when dated), `_LATEST`, `_REQUIREMENTS`,
  `lexical_terms.subject_terms(q)`. Output the expected round-1 shape (`search`,
  `day+search`, `latest`, `+documents`) and `metric_fast_path`.
- Test pins today's offline result on `gold/v1.1.jsonl`: 0 dated, 0 metric, 0 latest,
  requirements == {v1-03}, no-subject == {v1-12, v1-13, v1-32}. (This is what the pilot
  header prints; review S6.)

## Task 2 — Template renderer and statement allow/deny list (offline)

Files: `askv2/render.py`, `tests/askv2/test_render.py`.

- `render(sql, params) -> str`: psycopg grammar -- `%s` positional (tuple/list), `%(name)s`
  named (dict), `%%` -> `%` only when params are given, no params -> returned unchanged.
  Literals: `str` -> `'...'` with `''` escaping, NUL rejected; `uuid.UUID`, `date`,
  `datetime` -> quoted ISO; `bool` -> TRUE/FALSE; `int`; `float` -> `repr`; `None` -> NULL;
  `list[str|UUID]` -> `'{...}'` (elements quoted `"..."`, `\` and `"` escaped);
  `list[float]` (len >= 64) -> `'[...]'` with `%.7f` (run.py:261); `dict` -> JSON literal;
  anything else (`set`, nested lists, bytes) -> `EvalAdapterError`.
- `check_template(sql)`: first keyword `SELECT` or `WITH`, else refuse; tokens outside string
  literals must not include `INSERT|UPDATE|DELETE|MERGE|CREATE|ALTER|DROP|TRUNCATE|GRANT|
  REVOKE|COPY|CALL|DO|LOCK|SET|FOR UPDATE|FOR SHARE|nextval|setval|pg_advisory|
  pg_terminate_backend|pg_cancel_backend|pg_sleep|set_config|dblink|lo_`.
- `assert len(rendered) < 60_000`.
- Tests: each literal type; `%%` with and without params; a `set` raises; the real
  `build_search_sql()` renders under 60 KB with a 1024-float vector and has no `%(` left;
  `build_doc_search_sql()` likewise; deny-list hits on `SELECT pg_advisory_lock(1)`,
  `SELECT 1 FOR UPDATE`, `WITH x AS (DELETE ...)`, `SET TRANSACTION READ WRITE`;
  a string literal containing the word `DELETE` passes.

## Task 3 — Result coercion (offline)

Files: `askv2/adapter.py` (`coerce_rows`), `tests/askv2/test_coerce.py`.

- From `columnMetadata[*].typeName` and `records[*][*]` (the `{stringValue|longValue|
  doubleValue|booleanValue|isNull|arrayValue}` shape): `uuid` -> `uuid.UUID`; `date` ->
  `datetime.date`; `timestamp`/`timestamptz` -> `datetime`; `json`/`jsonb` -> parsed;
  `numeric` -> `Decimal`; `float4/8` -> float; `int2/4/8` -> int; `bool`; `text`/`varchar`;
  `_uuid`/`_text` arrays -> list; unknown typeName -> raw string plus a recorded warning.
- Rows as `dict` (label -> value) when `row_factory is psycopg.rows.dict_row`, else tuples.
- Tests from canned metadata fixtures, including `max(t.report_date)` (label `max`, tuple
  `row[0]`), a `jsonb` `metadata` column and a NULL `distance`.

## Task 4 — Adapter lifecycle with a fake rds-data client (offline)

Files: `askv2/adapter.py` (`DataApiConnection`, `DataApiCursor`), `tests/askv2/test_adapter.py`.

- `DataApiConnection(client, cluster, secret, database, log)`: `__init__` ->
  `begin_transaction`, `SET TRANSACTION READ ONLY`, `SHOW transaction_read_only` must be
  `on` (else `EvalAdapterError` and rollback). Surface: `cursor(row_factory=None)` (also a
  context manager), `execute(sql, params)` returning the cursor, `fetchone/fetchall/__iter__/
  rowcount`, `conn.execute`, `conn.transaction()` -> `SAVEPOINT eval_n` / `RELEASE` /
  `ROLLBACK TO SAVEPOINT` on exception (re-raised), `closed`, `close()` -> `rollback_
  transaction` once. Every `execute_statement` carries `transactionId` and
  `includeResultMetadata=True`. Internal statements bypass `check_template` through one
  private method; templates always go through it.
- Records per statement: template sha256, first 80 chars, `ms`, row count, response bytes,
  the raw coerced rows for templates whose sha matches `build_search_sql()` /
  `day_overview_rows` (the `sql` stage, review S1). Errors are appended to `log.errors`
  before re-raising.
- A module-level grep test: `adapter.py` source contains no `commit_transaction`.
- Tests with a scripted fake client: begin/SET/SHOW ordering; `SHOW` returning `off` aborts
  with rollback; nested savepoints release in order; an exception inside `transaction()`
  issues `ROLLBACK TO` and re-raises; `close()` is idempotent; a statement error is recorded
  and re-raised; every call carries the transaction id; the renderer and deny list are
  invoked for templates and not for internals; `KeyboardInterrupt` during execute still
  rolls back (`try/finally`).

## Task 5 — Renderer parity on the local Postgres (pgserver, no AWS)

Files: `tests/askv2/test_render_parity_pg.py`.

- Using the repo's `db` fixture pattern (`tests/conftest.py`, migrated local DB) seed a
  small world as `tests/integration/test_rag_tools_dispatch_sql.py` does, then for each
  template+params pair the dispatcher issues (captured by wrapping `conn.execute`/`cursor`
  on the real psycopg connection), execute the psycopg-bound original and `render()`'s
  literal through psycopg with no params; assert identical rows. Covers `build_search_sql`,
  `build_latest_match_date_sql`, `build_subject_coverage_sql`, `day_overview_rows`,
  `list_sites_by_ids`'s untyped `ANY(%s)`, `aliases.list_active`, `find_visible_people`.
- This replaces the spec's every-10th-statement live parity; the live check stays as one
  pilot smoke (Task 11).

## Task 6 — Harness: fake Lambda client, patches, guards, log capture, proxies (offline)

Files: `askv2/harness.py`, `tests/askv2/test_harness.py`.

- `FakeLambda(adapter_factory, lock, log)`: `invoke(FunctionName, InvocationType, Payload)`
  asserts `FunctionName == "eval-in-process"`, `RequestResponse`, `mode in {"tools",
  "metric"}`; under the lock runs `lambda_rag_search.lambda_handler(json.loads(Payload),
  None)`; records `rows` per call id; returns `{"StatusCode": 200, "Payload":
  BytesIO(json.dumps(result, default=str))}`.
- Patches (all asserted by Task 0): `lambda_rag_search.get_cached_connection` -> thread-local
  adapter (created on first call per invoke); `close_cached_connection` -> close + drop;
  `db.connection.get_connection` -> raises; `lambda_ask_agent._get_lambda_client` -> FAKE;
  `ask_orchestrator.DEADLINE_S["screen"] = 120.0`.
- Guards: `boto3.client`/`Session.client` wrapper (allow `rds-data`; `lambda` only before
  `arm()`); `urllib3.HTTPConnectionPool.urlopen` wrapper with a host allowlist derived from
  the resolved config (judge host, `QWEN_BASE_URL` host if provider is qwen, DashScope,
  `rds-data.ap-southeast-2.amazonaws.com`, `sts.*.amazonaws.com`), per-host counters, raise
  on any other host. Credentials frozen before arming.
- Log capture: root logger INFO, single in-memory handler keeping `ASK_ROUTE` records
  (parsed JSON) and `LLM_TOOLS` counts, dropping everything else; `logging.lastResort`
  never fires because a handler is attached.
- `ToolsProxy(ask_tools, fake)`: delegates `TOOLS`, `offered_tools`, `RefAssigner`;
  `ToolContext(**kw)` -> `ask_tools.ToolContext(**{**kw, "lambda_client": fake})`;
  `execute(calls, ctx)` records `(calls, envelopes)` per batch and returns the envelopes.
- `chat_wrapper(real_chat)`: deep-copies `msgs` + `tools`, calls through, records the
  `ChatTurn` (tool calls, usage incl. completion and reasoning tokens, model, error).
- Tests: the fake invoke round-trips `date`/`UUID` through JSON; wrong function name raises;
  the urllib3 guard raises on `example.com` and counts the allowed host; boto3 guard refuses
  `s3`; two threads get two adapters; the log handler keeps `ASK_ROUTE` and drops a line with
  text; the proxy returns envelopes unchanged (identity) and `ToolContext` is a real
  `ask_tools.ToolContext` (`dataclasses.replace` works on it).

## Task 7 — Funnel scoring as pure functions (offline)

Files: `askv2/funnel.py`, `tests/askv2/test_funnel.py`.

- Inputs per turn: `sql_rows` (adapter), `invoke_rows` (fake), `batches` of envelopes
  (proxy), `judge_requests` (chat wrapper: messages per call), `final_turn` (last
  `ChatTurn`), `route` (log), `result` (what `answer()` returned), adapter errors.
- Stages with `run.py.first_hit`'s rule (date equal and any `match_any` matches): `sql`,
  `rows`, `item` (envelope item text, 600-capped), `seen` (any `role:"tool"` content line
  whose `_item_line` date field is the gold date and whose text matches), `final_view` (last
  request only), `cited` (refs from `ask_orchestrator._cited_card_refs(final_answer.answer,
  shown)` mapped to items, reuse not re-spell). `lost_at` = first false stage, with
  `cut_text_cap` (was `cut_600_cap` until 2026-10-22) detected by matching the row's `chunk_text` past char 600,
  `dispatcher_dropped` (sql true, rows false), `cut_render_budget` (item true, seen false),
  `seen_not_cited`. Round-1 vs judge-round split for `rows` (round 1 is deterministic).
- Turn status: `invalid` (adapter error), `fallback` (`route.fallback is not None`),
  `timing_distorted` (`route.records_late`, any envelope `meta.error == "timeout"`, or
  `judge_ms + tools_ms - sql_ms + sum(max(0, stmt_ms - 5)) > 40_000`), else `scored`.
  Controls: `grounded`, `basis`, `citations` count -> `control_answered_with_citations`.
- Tests on hand-built artefacts: each stage true/false; 600-cap case; render-budget drop
  (`_budgeted` with a tiny `RENDER_MAX_CHARS` monkeypatch on real `render_each`); text-only
  final (`ChatTurn(text="... [R1]")`); range `[R1-R3]`; a `+no_synthesis` fallback is not a
  miss; an alias-normalised row counts at `sql` but not `rows`; a control with citations.

## Task 8 — Config and secrets (offline with a fake Lambda API)

Files: `askv2/env.py`, `tests/askv2/test_env.py`.

- `fetch_configs(lambda_client, prefix)`: `get_function_configuration` for
  `${P}-ask-voice-worker` and `${P}-rag-search` (names confirmed against the template at
  pilot time); copies every non-secret variable (deny pattern `API_KEY|SECRET|PASSWORD|_TOKEN$`,
  case-insensitive, suffix-anchored so `ASK_ANSWER_TOKENS` and `LLM_ANSWER_TOKEN_CEILING`
  survive) into `os.environ`; injects exactly `CORROBORATION_API_KEY`,
  `DASHSCOPE_API_KEY`, and `QWEN_API_KEY` iff `LLM_PROVIDER == "qwen"`; deletes the fetched
  dicts; returns a fingerprint of `{name: present}` plus the non-secret values that matter
  (`ASK_JUDGE_MODEL/FALLBACKS/PROVIDER/REASONING`, `ENABLE_RERANK`, `RERANK_CANDIDATE_K`,
  `RERANK_KEYWORD_CANDIDATES`, `PROJECT_DOCUMENTS`, `SEARCH_INDEX_READ` (overridden by
  `--family`), `LLM_PROVIDER`, `QWEN_BASE_URL` host, `SERVER_BUILD`).
- `ASK_DEADLINE_SECONDS` is popped after the copy; `ENABLE_WEB_ANSWER=false`,
  `ASK_CONVERSATION_MEMORY=false` forced; `VOICE_AUDIT_FUNCTION` unset.
- Tests: deny pattern keeps ElevenLabs/Anthropic keys out while `ASK_ANSWER_TOKENS` and
  `LLM_ANSWER_TOKEN_CEILING` are copied; the three keys land; the
  fingerprint has booleans only; `ASK_DEADLINE_SECONDS` absent afterwards; host allowlist
  derived correctly for `LLM_PROVIDER=anthropic` (fallback host absent -> `+no_synthesis`
  expected and printed).

## Task 9 — One turn end to end against fakes (offline)

Files: `run_ask_v2.py` (`run_turn`), `tests/askv2/test_turn_offline.py`.

- `run_turn(item, *, chat, fake, proxy, now, scope_site)` builds the body
  `{"question", "tz": "Pacific/Auckland", "now": <pinned ISO>, "caller_sub": <sub>}` (+
  `site_id` under `--scope-site`), calls `ask_orchestrator.answer(body, channel="screen",
  on_state=recorder, tools=proxy, chat=chat, turn_id=<hex>)`, collects the artefacts and
  scores them with Task 7. Per turn it stores funnel flags, tool calls + args, route,
  counts, evidence line excerpts (<= 160 chars), answer text and refs; full renders only
  with `--keep-renders` into the scratchpad.
- Test: the real orchestrator + real `ask_tools` + real `rag_tools_dispatch` +
  `lambda_rag_search` with the adapter over a scripted fake rds-data client (canned
  `columnMetadata`/records for `get_user_by_sub`, `visible_scope`'s membership reads,
  `build_search_sql`, `aliases.list_active`, `latest_visible_date`) and a `ScriptChat` as in
  `tests/unit/test_ask_orchestrator_judge.py`; `dashscope_utils.embed` monkeypatched to a
  fixed vector. Asserts: one Data API transaction per invoke, rolled back; `SET TRANSACTION
  READ ONLY` first; the funnel reaches `cited`; `route` captured from the log; zero HTTP
  calls through the guard.

## Task 10 — Run driver, report, caps (offline)

Files: `run_ask_v2.py` (`main`), `askv2/report.py`, `tests/askv2/test_report.py`.

- CLI: `--gold`, `--label`, `--family report|session` (required), `--repeat` (default 2),
  `--scope-site` (default on, `--no-scope-site` to lift), `--now` (default run start,
  recorded), `--database` (`fieldsight` | `fieldsight_test`), `--pilot`, `--yes`,
  `--max-judge-calls`, `--max-tokens`, `--keep-renders`, `--out`.
- Result file: fingerprint (git commit, `src_ref`/`src_sha`, env fingerprint, model chain,
  template registry as sorted `(sha256, head80)`, gold sha, database, family, `now`,
  `caller`: `sub_hash`, `has_login`, `global_role`, `user_scope`, site/author counts,
  `gold_site_visible`, `gold_author_in_scope`), per-repeat per-question records, summary:
  per stage k/n over scored turns, `lost_at` histogram, fallback/invalid/timing columns,
  flip rate between repeats, controls answered with citations, HTTP counts per host,
  judge calls, prompt/completion/reasoning tokens, SQL statements and ms, largest response.
- Caps abort the run (results so far are still written). `--yes` required when not
  `--pilot`. A refusal to diff two result files whose model chains differ lives here too.
- Tests: summary arithmetic on synthetic records; flip-rate; the cap abort path; the diff
  refusal.

## Task 11 — PILOT (first live step; gated)

Files: none new. Prerequisite: `export AWS_PROFILE=fieldsight-deployer`.

Step 0 (read-only probes, abort on any failure, printed before anything else):
1. IAM simulate `rds-data:BeginTransaction/ExecuteStatement/RollbackTransaction` and
   `lambda:GetFunctionConfiguration` for the profile.
2. Fetch configs (Task 8); print the resolved judge chain, provider pin, `LLM_PROVIDER`,
   fallback host, `ENABLE_RERANK`, `PROJECT_DOCUMENTS`, `SEARCH_INDEX_READ` deployed vs
   `--family`, `SERVER_BUILD` vs `EVAL_SRC_REF`.
3. Read-only probe: `begin_transaction` -> `SET TRANSACTION READ ONLY` -> `SHOW` -> `CREATE
   TEMP TABLE eval_ro_probe (x int)` must fail with SQLSTATE 25006 -> rollback.
4. Schema preflight (review B2): the listed columns/tables exist in `--database`.
5. Identity (review B1): author `f531eb99-...` `has_login`, `global_role`; `visible_scope`
   via the adapter; `gold_site_visible` and `gold_author_in_scope` both true, else stop and
   ask the owner (Q1).
6. One `build_search_sql` statement for gold v1-01's question through the adapter AND
   through run.py's typed spelling (`data_api_sql()` refactored to take
   `optional_rewrites=False`); assert the same `id` list and distances within 1e-6.

Step 1: `--pilot` = v1-01, v1-03 (documents round 1 if on), v1-20 (control), `--repeat 2`,
one family, `--database fieldsight_test` first if the TEST DB has a usable caller, then
`fieldsight`. Prints: per-host HTTP counts, judge calls, prompt/completion/reasoning tokens,
SQL statements and p50/max ms, largest response bytes, template registry, invalid/fallback/
timing counts, flip rate on the pair, and the full-run extrapolation
(45 q x 2 families x 2 repeats + the noise pair = 270 turns) in calls and tokens.

Gate: the full run (Task 12) is not started until the pilot's printed counts are read and
`--yes` is given with `--max-judge-calls` / `--max-tokens` set from them.

## Task 12 — Full runs and the legacy reference (live, gated)

- Same day, same `EVAL_SRC_REF`, same `--now`: `--family report`, `--family session`, then the
  identical-config second process for the noise floor; `run.py --family X` and
  `check_evidence.py --family X` (Task 13) on the same day.
- Claims only per review/spec §5: per-question stability within a family before a
  cross-family difference; summary delta above the same-config flip count.

## Task 13 — `check_evidence.py --family` (offline test, live run in Task 12)

Files: `check_evidence.py`, `tests/askv2/test_check_evidence_family.py`.

- Add `--family`, importing `family_predicate` from the exported `repositories.search_sql`
  and binding `source_family` as a Data API parameter; test the SQL assembly offline.

## Task 14 — README and results hygiene

- README: what `--mode ask-v2` measures, the stage definitions, the status columns, the
  read-only mechanism and its probe, the identity caveat, how to read the noise pair, the
  "never merge `results/` to develop/main" rule restated.
