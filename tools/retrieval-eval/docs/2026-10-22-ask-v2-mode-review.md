# Review: Ask v2 mode spec (`2026-10-22-ask-v2-mode-spec.md`)

Reviewed 2026-10-22 against `origin/develop` @ `23cb443c` (read via `git show`, never the
Dropbox checkout) and the ruler on branch `feat/retrieval-eval-baseline` (`run.py`,
`check_evidence.py`, `gold/v1.1.jsonl`). Nothing was run against AWS, Aurora, TEST or any
vendor; everything below is from source and from pure-function probes run offline
(`query_slots`, `metric_slots`, `lexical_terms`, `search_sql`) with the fspy interpreter.

Verdict: the seam choice (a) is right and the wiring is mostly real. Three things block a
first live run; the rest are shape fixes. Ranked.

## Citation spot-check (item 1)

Checked 34 cited anchors. 31 are exact or within 3 lines. Three are wrong:

| Spec says | Reality at 23cb443c |
|---|---|
| `_metric_answer` laa:1082 | `lambda_ask_agent.py:1050` (`_get_lambda_client().invoke` at :1083-1086, so the patch still covers it) |
| field-only users have `cognito_sub=''` (users.py:130) | `users.py:129-130` inserts `cognito_sub` **NULL**; `''` is the email. Conclusion unchanged: `get_user_by_sub` is `WHERE users.cognito_sub=%s` (:81), so a NULL-sub author is `not_provisioned` on every turn |
| `count_metric -> _metric (laa)` | `_metric` is `lambda_rag_search.py:84`; `_count_metric` (dispatch :337) calls it through `metric_fn` |

Confirmed exact (sample): `answer` :1224, `round1_calls` :567, `MAX_ROUNDS` :767, `_budgeted`
:221-246, `render_each` :258, `_item_line` :197, `sanitize` :56, `_REF_GROUP/_REF_TOKEN`
:270-273, `ASK_ROUTE` :1263, `web_on` :1334, `policy` :487, `_system_prompt` :911, offered
list :1402-1405, `DEADLINE_S` :766, `_deadline_s` :785, `ASK_CONVERSATION_MEMORY` :1275,
`scope_legacy` :1314-1318, `ask_tools.TOOLS` :53, `MAX_RESULT_CHARS/MAX_TEXT` :224-225,
`_post_search` :298, `_invoke` :452-466, `on_or_before` injection :497-499, dispatch
`conn.transaction()` :442, `MAX_DEADLINE_S` :34, `LATEST_QUERY_MIN_START_S` :49 and :433-435,
`upstream` :448-450, `lambda_rag_search` import :44, `_tools` :233-259, `_search` :313,
mode dispatch :317-325, `finally` :69-72, `INDEX_FAMILY` :56, `memberships._exec` :36-54,
`users.get_user_by_sub` :59, `scope.visible_scope` :22, laa `_get_lambda_client` :61,
`RERANK_ENABLED` :273, `_validate_scope` :932, `ask_voice_turn` `put_state` :132-133,
`write_convo` :213, `_SCREEN_DROP` :518, `_store_thread_turn` :612, `_work_screen` :642,
`ask_rewrite.py:93-94`, `web_answer.enabled` (:90-93 reads `ENABLE_WEB_ANSWER`),
`corroboration_client.api_key` (:85-91 reads `CORROBORATION_API_KEY`).

## Findings

### B1 (BLOCKER) — Identity: Q1 must be a stop/go gate, and the like-for-like claim depends on it

Evidence: `users.py:129-130` (field-only rows carry `cognito_sub` NULL); `users.py:72-81`
(`get_user_by_sub` matches on `cognito_sub`); `lambda_rag_search.py:245-250` (no caller ->
every call `not_provisioned`); `ask_orchestrator.py:1366-1368` (`fallback="not_provisioned"`,
no judge call). `scope.py:52-54`: a gm/admin caller gets company-wide `site_ids` and
`author_ids=None`; `scope.py:95-101`: only worker/site_manager tiers pin authors.

Why it blocks: with a NULL-sub author there is nothing to run as; with a gm/admin login the
visible scope is the whole company and every funnel number compares a company-wide search
with run.py's one-author-one-site search. The spec says this in §4 but as a note.

Fix: make it the first pilot step with abort semantics (read-only, one statement):
`SELECT cognito_sub IS NOT NULL AS has_login, global_role FROM users WHERE id=<author>`, then
`visible_scope` for that caller; abort unless (gold site in `site_ids`) and
(`author_ids is None` or gold author in `author_ids`). Record `has_login`, `global_role`,
`user_scope`, and the two booleans in the fingerprint. If the only usable login is
company-wide, say in the result header that the v2 numbers are company-scope, and run
`--scope-site` by default (it is the closest thing to run.py's site pin; `scope.site` at
:1357-1358 -> `_scope_args` :561-564 puts it on every round-1 call).

### B2 (BLOCKER) — Schema preflight is load-bearing and must run before any question; `memberships._exec`'s deploy tolerance is dead under the adapter

Evidence: `build_search_sql()` (printed offline, 4,177 chars) contains
`t.superseded_at IS NULL` twice; `build_latest_date_sql` once. run.py's `OPTIONAL_REWRITES`
(run.py:164-167) exists because the prod `fieldsight` database lacked that column; migration
`0074_unbind_superseded_chunks.sql` is on `origin/main` now, but the prod DB state was not
verifiable here. `memberships._exec` (:36-54) tolerates a missing `memberships.external`
(0085) only by `isinstance(exc, psycopg.errors.UndefinedColumn)` (:32-34); the adapter raises
botocore/`EvalAdapterError`, so that fallback never fires and a missing column fails the
whole `_tools` batch (`external_site_roles` :293 is called from `visible_scope` :63/:74, before
the dispatcher).

Fix: the §2.4 preflight becomes pilot step 0 with abort: `information_schema.columns` for
`topics.superseded_at`, `memberships.external`, `project_documents`/`project_doc_chunks`
(0088), `redactions.target_key`, `redactions.reverted_at`. Missing -> print the list and stop;
no rewriting inside imported repositories (keep that rule). Document that until it passes,
the develop-source run cannot happen on prod and the pilot's venue is `fieldsight_test`.

### B3 (BLOCKER) — The read-only mechanism is sound in Postgres terms but unproven on the Data API; add a probe that fails closed, and widen the deny list

Evidence: the whole guarantee rests on `SET TRANSACTION READ ONLY` inside a Data API
`transactionId` session. Postgres: a read-only transaction refuses INSERT/UPDATE/DELETE/MERGE,
COPY FROM, all CREATE/ALTER/DROP (temp tables included), TRUNCATE, GRANT/REVOKE, `nextval()`
and `setval()`, and `SELECT ... FOR UPDATE/SHARE` (SQLSTATE 25006). It does NOT refuse
`pg_advisory_lock()`, `pg_terminate_backend()`, `pg_cancel_backend()`, `pg_sleep()` or
`set_config()`; none of these appear in the reached SQL (grep over every repository module on
the path: only SELECT/WITH, no `FOR UPDATE`, `nextval`, temp tables or advisory locks), so
this is defence in depth, not a found hole. Whether the Data API runs `SET TRANSACTION` on the
same session as the later statements cannot be shown from source.

Fix:
1. Per transaction: `begin_transaction` -> `SET TRANSACTION READ ONLY` -> `SHOW
   transaction_read_only` must return `on`, else abort the run before any template is sent.
2. Pilot step 0, once per process: inside such a transaction execute `CREATE TEMP TABLE
   eval_ro_probe (x int)` and require SQLSTATE 25006. If it succeeds the transaction is rolled
   back and the temp table dies with the session: zero persistent effect either way.
3. Template deny list adds `FOR UPDATE|FOR SHARE|nextval|setval|pg_advisory|
   pg_terminate_backend|pg_cancel_backend|pg_sleep|set_config|dblink|lo_`; first keyword
   `SELECT`/`WITH` only; adapter-internal statements (`SET TRANSACTION`, `SHOW`, `SAVEPOINT`,
   `RELEASE`, `ROLLBACK TO`) go through a separate path that templates cannot reach.
4. Rollback: `close_cached_connection` (patched) rolls back; also wrap the per-question call in
   `try/finally` and an `atexit` that rolls back any open transaction id (KeyboardInterrupt
   between questions). The Data API also auto-rolls back a transaction idle for 3 minutes.
5. The profile needs `rds-data:BeginTransaction` and `rds-data:RollbackTransaction`; run.py
   only ever used `ExecuteStatement`. Check with the IAM simulator in the preflight.

### S1 (SHOULD) — `rows` is not "SQL retrieved it"; add an `sql` stage from the adapter

Evidence: the fake-invoke return is the dispatcher's output: after alias normalisation
(`_normalize_rows` dispatch :81-93; `_search` :523-529 rewrites `chunk_text`), after
`latest_activity`'s coverage filter and `_candidate_pool` slice (:317-323), after the JSON
round trip. Gold patterns carry raw transcript spellings (run.py header: alias normalisation
"NOT replicated"; `check_evidence.py` matches raw `chunk_text`). An active alias that rewrites
a pattern word yields a false `never_retrieved` at every stage.

Fix: the adapter sees every result set, so record the raw rows of `build_search_sql` and
`day_overview_rows` fetches as stage `sql` (pre-normalisation, pre-filter); keep `rows` as the
invoke return. `lost_at` gains `dispatcher_dropped` (sql true, rows false) and the alias case
is visible instead of being counted against retrieval.

### S2 (SHOULD) — The fallback set in §1 is incomplete; use `route.fallback is not None`

Evidence: besides the seven listed, `answer()` sets `not_provisioned` (:1367), `guard`
(:1452), `empty_answer` (:1455) and the `<reason>+no_synthesis` suffix (:1157). Any of them
means the funnel did not finish. Also separate `invalid` (adapter error) from `fallback`.

### S3 (SHOULD) — The vector literal is not "an implementation choice": statement size

Evidence: `%(q)s` occurs 3 times in `build_search_sql` (printed offline) and 4 times in
`build_doc_search_sql` (project_docs_sql.py:37-52). The Data API caps `sql` at 65,536
characters. With run.py's `%.7f` a 1024-float literal is ~11.3 KB -> ~38 KB total (fits); with
psycopg-equivalent `repr()` it is ~22 KB -> ~70 KB (refused on the first search). pgvector
stores float4, so `%.7f` loses at most a digit at small magnitudes, and run.py has measured
with it since September.

Fix: pin the float format to run.py's `%.7f`, assert `len(sql) < 60_000` before every send,
and state that the §2.4 parity check compares two renderings of the same format (it proves
scalar/array binding, not float precision). Alternative if the cap is ever hit: bind the vector
as a Data API parameter with a `%(q)s::vector -> CAST(:q AS vector)` rewrite (what run.py
does). Also record the largest Data API response (k=64 day searches return up to 128 rows with
`chunk_text` and `topic_summary`; the response cap is 1 MB).

### S4 (SHOULD) — Threads: the adapter must be thread-local, and `FAKE.invoke` serialised

Evidence: `run_round1` runs `_records_round1` on a pool thread (:714-726) and never joins it
(`shutdown(wait=False)` :758); after `records_late` that thread can still be inside
`lambda_handler` when `_judge_round` calls `tools.execute` on the main thread (:1202). A
module-global "current adapter" would be shared by two invokes.

Fix: `threading.local()` for the per-invoke adapter in the patched `get_cached_connection` /
`close_cached_connection`; a lock around `FAKE.invoke` so `rows` capture and the Data API
transaction are one-at-a-time; count `records_late` (it should be ~0 at 120 s).

### S5 (SHOULD) — Guards: allow credential endpoints; freeze credentials before arming

Evidence: botocore runs on urllib3 (`AWSHTTPSConnectionPool` inherits `urlopen`), so the host
guard sees it. A `fieldsight-deployer` profile that assumes a role or uses SSO resolves
`sts.*`/`portal.sso.*` lazily, possibly after the guard is armed.

Fix: resolve credentials once (`session.get_credentials().get_frozen_credentials()`) before
arming; allow `rds-data.ap-southeast-2.amazonaws.com` plus `sts.*.amazonaws.com`. The boto3
guard keeps `rds-data` only (plus `lambda` for the preflight before arming).

### S6 (SHOULD) — Pilot composition vs the gold: no `latest_activity` round-1 question exists

Evidence (offline, today=2026-10-22 over `gold/v1.1.jsonl`: 45 items, 39 answerable, 6
controls, dates 2026-09-02 and 2026-09-10 only): `query_slots.time_range` dates 0 questions;
`metric_slots.detect` with a date 0 (metric fast path never fires); `_LATEST` matches 0;
`_REQUIREMENTS` matches only v1-03; `lexical_terms.subject_terms` is empty for v1-12, v1-13,
v1-32 (Chinese). So round 1 is always `search_records(query=question)` with no date (+
`search_project_documents` for v1-03 when `PROJECT_DOCUMENTS` is on); `latest_activity`,
`day_overview`, `count_metric`, `find_people` are reached only when the judge chooses them.

Fix: the pilot is 2 answerable + 1 control from the gold (no "when did" item); say that this
gold measures one round-1 shape and the judge's choices after it. Record per question the
distinct tool-call sequences; a `latest_activity` comparison needs a gold v2.

### S7 (SHOULD) — Env allowlist by name is wrong-shaped; name the fallback provider

Evidence: `llm_utils` reads `LLM_PROVIDER` (default `anthropic`, :34), `QWEN_API_KEY`
(falls back to `DASHSCOPE_API_KEY`, :64), `QWEN_BASE_URL` (:65), `QWEN_MODEL` (:68),
`QWEN_MODEL_NONTHINKING`, `QWEN_ENABLE_THINKING`, `LLM_REASONING_EFFORT`,
`LLM_REASONING_HEADROOM`, `LLM_ANSWER_TOKEN_CEILING`, `LLM_HTTP_TIMEOUT`,
`LLM_PROVIDER_ERROR_WAIT`, `LLM_FALLBACK_MODELS`, `LLM_CHAIN_BUDGET_SECONDS` -- all at import.
`lambda_ask_agent` reads `ASK_ANSWER_TOKENS` (:71), `ASK_DEADLINE_SECONDS` (:77),
`RERANK_KEYWORD_CANDIDATES` (:288). `answer_language` reads `ASK_ANSWER_LANGUAGE` (not
`ANSWER_LANGUAGE*`). `llm_tools_client` reads `ASK_JUDGE_HTTP_TIMEOUT` (:65). §7 says "the
llm_utils fallback key" and also forbids the Anthropic key: if the worker's `LLM_PROVIDER` is
`anthropic`, `_fallback` synthesis has no key and ends `+no_synthesis`.

Fix: copy every non-secret variable from the two fetched configs (deny-pattern
`API_KEY|SECRET|PASSWORD|_TOKEN$`, suffix-anchored: a bare `TOKEN` would also drop
`ASK_ANSWER_TOKENS` (laa:71) and `LLM_ANSWER_TOKEN_CEILING` (llm_utils:129), which the
fallback synthesis reads), then inject exactly three secrets by name
(`CORROBORATION_API_KEY`, `DASHSCOPE_API_KEY`, and `QWEN_API_KEY` iff `LLM_PROVIDER=qwen`);
print the resolved judge chain, `LLM_PROVIDER`, `QWEN_BASE_URL` host and `ENABLE_RERANK` in
the pilot header; the host allowlist is derived from those values, not hard-coded.

### S8 (SHOULD) — Hard caps, and the cost line omits output tokens and the noise pair

Evidence: `chat` sends `max_tokens = 2048 + REASONING_HEADROOM_TOKENS` (llm_tools_client:294;
default 8,000, llm_utils:119) and `_fallback` sends `MAX_ANSWER_TOKENS` 8,000 + headroom; §6
counts input tokens only. `ChatTurn.usage` carries `completion_tokens` and
`completion_tokens_details.reasoning_tokens` (:177-180, :204-212). §6's 180 turns does not
include §5's same-config second process (another 90).

Fix: `--max-judge-calls` and `--max-tokens` (prompt+completion) with abort, defaults set from
the pilot extrapolation; the pilot prints prompt, completion and reasoning tokens per call and
the full-run extrapolation includes the noise pair.

### S9 (SHOULD) — Offline binding parity exists for free: the local Postgres

Evidence: `tests/integration/test_rag_tools_dispatch_sql.py` already patches
`lambda_rag_search.get_cached_connection` (its :28) and runs the real dispatcher against the
local pgserver (`/c/Users/camil/fswork/run-tests.sh`). The adapter's renderer can be proven
there with no AWS: for every template the registry sees, execute the psycopg-bound original
and the rendered literal on the same local connection and compare rows.

Fix: make that the renderer's main test; keep the live parity check as a smoke (once, in the
pilot), not every 10th statement.

### S10 (SHOULD) — `cited` should reuse the orchestrator's own parsers

Evidence: `_cited_card_refs(answer_text, shown)` (:1499-1515) and `_final_of(turn)` (:999)
already derive card refs from the raw `final_answer.answer`; the returned `answer` has refs
renumbered by `to_client` (:371-375), so the raw text must come from the recorded last
`ChatTurn`. Re-spelling `_REF_GROUP` in the ruler would be a second copy.

### S11 (SHOULD) — Noise floor and repeats for 39 questions: adequate as a floor, state the resolution

Evidence: 39 answerable items, so one question is 2.6 points of any percentage; `--repeat 2`
can detect that a question flips but cannot estimate a flip rate, and the "flip rate >10%"
rule in §5 is four questions. The spec's two defences (same-config second process as the
noise floor; a summary delta claimed only above the same-config flip count) are the right
shape and are enough for a measurement tool.

Fix: report counts, never percentages alone; per question, take the majority over repeats
before any cross-family comparison; print the minimum detectable delta (the same-config flip
count) in the result header so a reader sees it beside the number; keep `--repeat 2` as the
default and `--repeat 3` only when the pilot pair flips more than two questions.

### N1-N8 (NIT)

- N1: `%%` -> `%` only when psycopg would (parameters given); the reached templates carry 8
  `%%` in `build_search_sql` alone, so this is not optional.
- N2: `_tools` passes a `set` for `site_ids` into `day_overview_rows`; psycopg does not adapt
  `set`, so production already lists them before binding -- the adapter should raise on `set`
  (loud), not silently render it.
- N3: say the database by name (`--database fieldsight` = prod, `fieldsight_test` = TEST; both
  on the one cluster run.py already names), and that the TEST database is the smoke venue.
- N4: the registry's stack-frame attribution and the diff against the previous result file
  are over-engineering for a measurement tool: a sorted list of (template sha256, first 80
  chars) in the fingerprint is enough. `CodeSha256` can be replaced by the `SERVER_BUILD`
  value already in the fetched config.
- N5: Phase 1b belongs in its own spec; cut it from this one.
- N6: `would_exceed_40s` uses a constant 0.3 s per statement; use the measured per-statement
  `sql_ms` minus a 5 ms in-VPC allowance instead.
- N7: `check_evidence.py --family` should import `family_predicate` from
  `repositories.search_sql` rather than re-spell the `extractions/` rule.
- N8: name the pinned `now` in the results file name or label, since `latest_activity`'s
  default `on_or_before` and the system prompt's "Today" line both read it.

## What is fine as specified

- The seam (`tools`, `chat` parameters of `answer`, :1224; `_records_round1` ->
  `tools.execute` :645; `_judge_round` -> `tools.execute` :1202) exposes everything the funnel
  needs without patching internals: envelopes (`item`), every judge request's `tool` messages
  (`seen`, `final_view`), the last `ChatTurn` (`cited`), and `on_state`. `route` is only
  logged (:1263) -- the capture handler is the right way to get it.
- `DEADLINE_S` patch + unsetting `ASK_DEADLINE_SECONDS` (:785-795 returns `min(env, cap)`) is
  correct; `MAX_ROUNDS` still bounds rounds; `ask_tools._invoke` still caps a batch at 20 s
  (:454) and the dispatcher keeps its own clock (:419-435).
- Judge-call count: at most 3 per turn (rounds 1->2->3 then forced; loop :1388-1437) plus one
  `call_llm` on fallback. Embedding: one DashScope call per `execute` (:511-520).
- `FAKE.invoke` covering both `ask_tools._invoke` (:455) and `laa._metric_answer` (:1083) via
  the `_get_lambda_client` patch; `RAG_SEARCH_FUNCTION` is read at laa import (:52), so the
  env must be set first.
- `conn.transaction()` as SAVEPOINT: the dispatcher's per-call isolation (:442) and
  `memberships._exec` (:46-48) both need it, and savepoints are legal in a read-only
  transaction.
- Policy `never` (`ENABLE_WEB_ANSWER=false` -> `web_on` False -> `pol="never"` :1334-1337)
  removes the precheck, the web tool and the offer rule; the consequence is stated honestly.
