# Retrieval-eval ruler: Ask v2 mode (`--mode ask-v2`)

Status: DRAFT spec, 2026-10-22. No code yet. All citations are `origin/develop` @ `23cb443c`
(the ref release PR #1157 ships with `ASK_AGENTIC=screen,voice`) unless marked `run.py`
(this branch). Line numbers may move by a few lines; the function names are the anchor.

## 0. Why

`run.py` measures the LEGACY Ask path: one k=5 search -> `_admit_one_lexical_hit` -> prompt.
Ask v2 (`ask_orchestrator.answer`, :1224) is a model-driven loop: a deterministic round 1
(`round1_calls` :567), then up to `MAX_ROUNDS=3` (:767) rounds where the judge model picks
tools (`ask_tools.TOOLS` :53) whose results it reads rendered (`render_each` :258). What
reaches the model now depends on which tools and args the model chose, the per-tool cut
(`ask_tools._post_search` :298), the 600-char item cap (`MAX_TEXT` :225), the 10k-char
per-result budget (:224) and the 8k-token render budget across rounds (`_budgeted` :221).
None of that is measured today.

## 1. Goal and metrics

Per question, per repeat, the **funnel** — each stage a boolean "evidence present", using the
same rule as `run.py first_hit`: the item's date equals the gold `date` and any `match_any`
pattern matches the text.

| Stage | Where it is captured | Present means |
|---|---|---|
| `rows` | fake Lambda `invoke` return (rag-search output, before ask_tools cuts) | SQL retrieved it |
| `item` | `tools.execute` proxy (envelopes) | survived post-search k cut, `MAX_TEXT` 600 cap, 10k budget |
| `seen` | `chat` wrapper: every `role:"tool"` message of every judge request | the model read it at least once |
| `final_view` | `chat` wrapper: the last judge request's tool messages | still in view when it answered (older rounds are dropped first, :221-246) |
| `cited` | final `final_answer.answer` refs (`_REF_GROUP`/`_REF_TOKEN` :270-273) mapped to items | the answer cites an evidence-carrying ref |
| `answer_text` (phase 2) | returned `answer` | see §9 |

For `rendered` matching, split tool messages into item lines (`_item_line` :197 format
`R3 | <date> <time> | site | who | title — text`) and require the gold date on the line.
`sanitize` (:56) collapses whitespace; patterns are word-level so this is accepted.

Derived per question: `lost_at` = first stage that is false: `never_retrieved` (rows),
`cut_post_search` / `cut_600_cap` (in a row's `chunk_text` but not its item text: the cap,
detectable by matching the row past char 600), `cut_render_budget` (item but not seen),
`seen_not_cited`. Controls (`expect: unanswerable`): record `grounded`, `basis`, `citations`
count; report "control answered with citations" as the false-answer count.

Process record per turn (no text beyond §8): tool name + args per call per round (args include
the model's own `query` strings, dates, k, site, person), `route` dict from the orchestrator
(its `ASK_ROUTE` log line, :1263: policy, rounds, calls, round1b,
fallback, refs_r, ms; `route` is not returned, so a capture handler keeps that log record,
§7), judge call count, HTTP attempts, tokens, elapsed.

**Comparison axes:** `--family session|report` (one process each, same day, same
`EVAL_SRC_REF`), plus `run.py --family X` on the same day as the legacy reference. Turns whose
`route.fallback` is in {`metric`, `scope_legacy`, `exception`, `judge_error`, `judge_timeout`,
`no_time_for_final`, `forced_final_ignored`} did not run the v2 funnel to completion: they are
reported in a separate column, never scored as misses.

## 2. The seam — decision

Options considered:

- **(a) CHOSEN. Real orchestrator + real `ask_tools` + real rag-search code in-process, behind
  a fake Lambda client, with rag-search's Aurora connection replaced by a read-only Data API
  adapter.** Zero copies of production logic; only transport is substituted.
- **(b) Re-implement per-tool SQL in the ruler (as `Searcher` does).** Rejected:
  `rag_tools_dispatch.py` is ~460 lines of semantics (person/site resolution :104-175,
  latest_activity subject coverage :263-326, latest_before, alias normalisation :81,
  ACL via `lambda_rag_search._tools` :233-259 then `_search` :313). A copy measures the copy;
  `run.py` already re-spells one statement and needs asserted rewrites to stay honest.
- **(c) Invoke deployed prod.** Rejected. The full Ask worker writes S3 turn state
  (`put_state`, `ask_voice_turn.py:133`), conversation memory (:213) and threads
  (`_store_thread_turn` :612-640, called from `_work_screen` :642-712) under a real caller —
  prod writes are owner-only. Invoking only rag-search `mode:"tools"` is read-only but still
  impersonates a real `sub` on prod Lambda, runs only the deployed code with the deployed
  `SEARCH_INDEX_READ` (a family comparison would need an owner prod config flip), and by design
  logs carry no text (orchestrator module docstring), so it cannot show what the model saw.
  Possible later: an owner-run 3-question end-to-end cross-check of (a).

### 2.1 How (a) is wired

1. `src/` exported at `EVAL_SRC_REF` exactly as `run.py _export_src` (default
   `origin/develop` until #1157 lands, then `origin/main`); SHA in the fingerprint.
2. Env set BEFORE imports (several modules read at import): copied non-secret values from the
   deployed `${P}-ask-voice-worker` (judge: `ASK_JUDGE_MODEL/FALLBACKS/PROVIDER/REASONING`,
   `ENABLE_RERANK`, `RERANK_CANDIDATE_K`, `DASHSCOPE_*`, `LLM_*`, `PROJECT_DOCUMENTS`,
   `ANSWER_LANGUAGE*`) and `RagSearchFunction` (`SEARCH_INDEX_READ`, `PROJECT_DOCUMENTS`).
   Overrides in §3. `SEARCH_INDEX_READ` = `--family`; assert `lambda_rag_search.INDEX_FAMILY`
   (:56) and `lambda_ask_agent.RERANK_ENABLED` (laa:273) hold the intended values after import.
3. Call `ask_orchestrator.answer(body, channel="screen", on_state=rec, tools=proxy,
   chat=chat_wrapper, turn_id=<hex>)` — the documented seams (`tools`, `chat` parameters,
   :1224; tools default :1345, chat default :1383).
   - `proxy` delegates `TOOLS`, `offered_tools`, `RefAssigner` to `ask_tools`; `execute`
     records envelopes then returns them unchanged; `ToolContext(**kw)` returns
     `ask_tools.ToolContext(**{**kw, "lambda_client": FAKE})`.
   - `chat_wrapper(msgs, tools, **kw)` deep-copies `msgs`, calls the real
     `llm_tools_client.chat`, records the returned `ChatTurn` (tool calls, usage, model).
4. Patch `lambda_ask_agent._get_lambda_client` (laa:61) to return `FAKE` too, so the metric
   fast path (`_metric_answer` laa:1082) and any legacy path can never build a real Lambda
   client. `FAKE.invoke(FunctionName, InvocationType, Payload)`: asserts
   `FunctionName == laa.RAG_SEARCH_FUNCTION` (env set to `eval-in-process`), `RequestResponse`,
   `mode in {"tools","metric"}`; runs `lambda_rag_search.lambda_handler(json.loads(Payload),
   None)` (:59; mode dispatch in `_search` :317-325); records `rows` per call; returns
   `{"StatusCode": 200, "Payload": BytesIO(json.dumps(result, default=str))}` — the same JSON
   round trip Lambda marshalling applies.
5. Patch `lambda_rag_search.get_cached_connection` (imported name, :44) to return ONE adapter
   instance per invoke — `_tools` (:243) and the nested `_search` (:402, via `_run_search`)
   both call it and must land in the same transaction, inside the dispatcher's savepoint —
   and `close_cached_connection` (called in `lambda_handler`'s `finally`, :69-72, exactly as
   prod's exit path) to roll that transaction back and discard the instance (§2.2).
6. Every patched name is asserted to exist with the expected kind before patching; missing ->
   `SystemExit("seam moved: <name>")`, the `run.py REWRITES` rule.
7. Process-wide guards: `boto3.client`/`Session.client` wrapper allows only `rds-data` (plus
   `lambda` for the read-only config fetch in the preflight, before guards are armed); an
   urllib3 `HTTPConnectionPool.urlopen` wrapper allows only the judge host (OpenRouter), the
   DashScope host, the `llm_utils` fallback host and `rds-data.ap-southeast-2.amazonaws.com`,
   counts requests per host, and raises on any other host (so web/TTS/S3 are impossible even
   if a flag slips).

### 2.2 Data API connection adapter

Surface (all that the reached code uses; anything else raises `EvalAdapterError`):
`conn.cursor(row_factory=None|dict_row)` (also as context manager, users.py:363),
`cursor.execute(sql, params)` returning the cursor, `fetchone/fetchall/iteration/rowcount`,
`conn.execute(...)`, `conn.transaction()` (dispatch :442, `memberships._exec` :36-54),
`conn.closed`.

- **Binding:** psycopg templates use `%s` and `%(name)s` with Python values including lists
  (uuid arrays, float vectors). Data API has no array parameters, so values are rendered
  client-side as SQL literals: str/UUID/date -> quoted with `''` escaping (NUL rejected),
  lists of str -> untyped `'{...}'` literal (Postgres infers the array type from context, as
  with psycopg's unknown-typed dumps), list of floats -> `'[...]'` (vector input syntax),
  dict -> JSON literal, None/bool/int/float -> keywords/numbers; `%%` -> `%`. The exact
  renderer is an implementation choice; its correctness is proven by the parity check (§2.4).
- **Results:** `includeResultMetadata=True`; coerce by `columnMetadata.typeName`
  (uuid -> `uuid.UUID`, date -> `datetime.date`, timestamp(tz) -> `datetime`, json/jsonb ->
  parsed, numeric -> `Decimal`, float -> float, arrays -> list), dict or tuple per
  `row_factory`. Not `formatRecordsAs=JSON`: psycopg returns typed objects and the code calls
  e.g. `str(...)`/`.isoformat()` on them.
- **Read-only, mechanically (the secret is the cluster master `rds!cluster-...`, run.py, so
  the role does not protect us):**
  1. Each fake `invoke` = one Data API transaction: `begin_transaction` ->
     `SET TRANSACTION READ ONLY` -> statements -> `rollback_transaction`. The adapter has no
     code path to `commit_transaction` (a unit test greps its source). Postgres rejects any
     write with SQLSTATE 25006.
  2. `conn.transaction()` -> `SAVEPOINT eval_n` / `RELEASE` / `ROLLBACK TO SAVEPOINT`, so the
     dispatcher's per-call isolation behaves as in prod.
  3. Statement allowlist on the template text: first keyword `SELECT` or `WITH`, and no
     `INSERT|UPDATE|DELETE|MERGE|CREATE|ALTER|DROP|TRUNCATE|GRANT|COPY|CALL|DO` token outside
     string literals; otherwise refuse before sending.
- **Errors are never silent.** `rag_tools_dispatch.run` turns any exception into
  `meta.error="upstream"` (:448-450); the adapter therefore records every SQL/adapter error
  out of band, and a turn with one is `invalid`, never a miss. >2 invalid turns in a run ->
  abort.

### 2.3 SQL the chosen path issues (all SELECT, all through §2.2)

Per batch: `users.get_user_by_sub` (users.py:59), `scope.visible_scope` (scope.py:22: per role
`sites.list_company_sites|list_all_sites`, `memberships.caller_site_roles`,
`external_site_roles`, `worker_user_ids_for_sites`). Per tool:
`day_overview` -> `topics.day_overview_rows`, `aliases.list_active`,
`topics.latest_visible_topic_date`; `search_records` -> `_search` (again `get_user_by_sub`,
`visible_scope`, `_pin_site`/`sites.get_company_site_by_slug`, `users.get_by_folder_name*`),
`chunks.search_chunks` (= `search_sql.build_search_sql`), `aliases.list_active` x2,
`chunks.latest_visible_date`; `latest_activity` -> `chunks.latest_matching_date`,
`search_chunks`, `chunks.subject_coverage` (or the topics pair without query);
`find_people` -> `users.find_visible_people`; person/site args -> `sites.list_sites_by_ids`,
`users.person_in_reach`, `users.get_by_id`; `count_metric` -> `_metric` (laa) ->
`memberships.external_site_companies`, `findings.count_by_domain` or
`redactions.deleted_session_bases` + `recordings.range_stats`, `topics.has_topics_in_range`;
`search_project_documents` (only if `PROJECT_DOCUMENTS` on) -> `project_docs.search`.

### 2.4 Drift detection

- No production logic copied; drift can only be in the seam. Checked by: the asserted patch
  points (§2.1.6); a **SQL template registry** — sha256 of each distinct template plus the
  issuing repository function (stack frame), stored in the fingerprint and diffed against the
  previous result file (new/removed templates printed); **schema preflight** —
  `information_schema.columns` for every table.column the registry's templates reference that
  `run.py OPTIONAL_REWRITES` has needed before (`topics.superseded_at`) and the 0088 project-docs
  tables; missing -> abort with the list (no rewriting inside imported repositories);
  **binding parity** — on the pilot, and on every 10th `build_search_sql` statement after,
  also run the same template through `run.py`'s typed-parameter spelling (`REWRITES` + the
  family substitution, proven since September) with the same values and assert identical
  `(id, distance)` lists. Built WITHOUT `OPTIONAL_REWRITES`: that strips the
  `superseded_at` predicate, so the comparison would measure the rewrite, not the binding; **deployed-vs-ref** — record the
  deployed functions' `CodeSha256`/SERVER_BUILD and warn when it is not `EVAL_SRC_REF`.

## 3. Disabled / neutralised for eval

| What | Where | How |
|---|---|---|
| Web answer, precheck, `web_search` tool | `web_on` :1334 -> `policy` :487; precheck :534 (corroboration_client); `web_tool` :589 | `ENABLE_WEB_ANSWER=false` -> policy `never`; urllib3 host guard as backstop |
| Conversation rewrite | :1275 `ASK_CONVERSATION_MEMORY` | body has no `history` -> `standalone_question` returns at ask_rewrite.py:93 with no call; env also forced `false` |
| S3 turn state / convo / threads | worker only (`ask_voice_turn` :133, :213, :612-712) | worker not used; boto3 guard forbids `s3` |
| `on_state` writes | `_States` :884 | recorder list (statuses kept for the trace) |
| Voice / STT / TTS / voice audit | `_work_voice`, `laa._invoke_voice_audit` (laa ~2104) | channel `screen` only; `VOICE_AUDIT_FUNCTION` unset |
| Legacy corroboration | `laa._rag_answer` via `scope_legacy` :1315 | body never sends `author_folder`/`topic_row_id`; if `route.fallback=="scope_legacy"` the turn is flagged |

Consequence to report, not hide: policy `never` changes the system prompt vs prod `auto`
(`_system_prompt` :911: no `web_rule`, `_NO_OFFER_RULE`) and removes `web_search` from the
offered list (:1403-1404). The gold is records-only, so phase 1 measures records retrieval
under `never`. Phase 1b (optional): `ENABLE_WEB_ANSWER=true`, precheck real (1 cheap call),
`web_tool` replaced by a recorder that returns `meta.status="failed"`; questions where the
precheck said yes or the model called `web_search` are listed, excluded from family diffs.

**Timing.** Prod gives the screen 40 s (`DEADLINE_S` :766, `_deadline_s` :785 caps
`ASK_DEADLINE_SECONDS`). Data API round trips from a laptop are slower than in-VPC SQL and
would produce `records_late`/forced finals that measure our network. Patch
`ask_orchestrator.DEADLINE_S["screen"] = 120` (asserted) AND unset `ASK_DEADLINE_SECONDS`
(the copied worker env carries `'45'`, and `_deadline_s` returns `min(env, cap)`, so the
patch alone would give 45). The round count stays bounded by `MAX_ROUNDS`, so the raised
budget removes laptop-latency artifacts without allowing more rounds than prod. Record per
turn `judge_ms`, `tools_ms`, `sql_ms`; flag `would_exceed_40s` when
`judge_ms + tools_ms - sql_ms + 0.3 s x statements > 40 s`.
`timing_distorted` = `would_exceed_40s`, or `route.records_late`, or any envelope with
`meta.error == "timeout"` — the dispatcher keeps its own real-time 20 s batch budget
(`MAX_DEADLINE_S` :34, `deadline_s` from ask_tools `_invoke` :454) and refuses a
`latest_activity(query)` with <5 s left (:49, :433-435); the model would read those as
empty results. Rerank (only if `ENABLE_RERANK` is on in the copied env): set
`DASHSCOPE_RERANK_TIMEOUT_SECONDS=20` as `run.py` does and count calls over prod's budget,
since a laptop timeout falls back to arrival order and changes which rows become items.
Flag counts are reported; a family diff driven by flagged turns is not claimed.

## 4. Identity and scope

- `caller_sub` = the pinned author's `users.cognito_sub` (read once, read-only, for user
  `f531eb99-...`), so `_tools` (:239-259) resolves the real caller and `visible_scope` runs
  the real ACL. No Cognito: the API layer only injects `caller_sub` (worker:
  `rag_body["caller_sub"]`, `_work_screen`), which is exactly the body field we set.
  **If that user has no login (field-only users are inserted with `cognito_sub=''`,
  users.py:130), there is no sub to run as -> blocking question Q1.**
- Body = what `_work_screen` builds: request minus `_SCREEN_DROP` (ask_voice_turn:518) plus
  `caller_sub`: `{"question", "tz": "Pacific/Auckland", "now": <pinned ISO>}`. `now` is pinned
  per run (default: run start) and recorded — `resolve_today` (:1288) drives relative dates and
  `latest_activity`'s default `on_or_before` (ask_tools:497); the corpus grows after the gold.
- The author cannot be pinned through the body: `author_folder` routes to `scope_legacy`
  (:1314-1318). Default runs are **unscoped** (palette Ask). `--scope-site` adds
  `site_id=<gold site>` (`_validate_scope` laa:932 -> round-1 `scope.site` :1357), which also
  makes the turn `scoped` (policy `never` regardless). Fingerprint records `global_role`,
  `user_scope`, visible site count and author count (counts only). If `user_scope != SELF`,
  results are not like-for-like with `run.py`'s author-pinned numbers, and the report says so.

## 5. Determinism and noise

- Judge output is nondeterministic. Default `--repeat 2`; the pilot measures the flip rate
  (questions whose `seen`/`cited` differ between two repeats of one config). Flip rate >10% ->
  `--repeat 3` for real runs.
- Per question report `seen k/n`, `cited k/n`, and the set of distinct tool-call sequences.
  A family difference is claimed only for questions stable within each family and different
  across them, and a summary delta only when it exceeds the same-config flip count.
- Before any claim: run the identical config twice (two processes) on the same day; that pair
  is the noise floor recorded in the result.
- Run `check_evidence.py` the same day, per family. It has no family filter today -> add
  `--family` (prerequisite), otherwise `never_retrieved` cannot be told from "not indexed in
  that family".

## 6. Cost

Per turn, from the loop: round 1 has no model call; the judge is called at most 3 times
(rounds 1->2->3 then forced final; loop :1388, `MAX_ROUNDS=3`), plus at most 1 fallback
synthesis (`llm_utils.call_llm` :1135) on fallback paths; each judge call is 1 HTTP attempt
normally, up to 2 x (1 + len(`ASK_JUDGE_FALLBACKS`)) on tool_choice 400s / 5xx. Embeddings: 1
DashScope call per `execute` batch with a search-shaped call (<= 3). Rerank: only if
`ENABLE_RERANK` on, <= 1 per search-shaped call (<= 4 per round). Input tokens per judge call:
system ~1.5k + render <= 8k tokens (`RENDER_MAX_TOKENS` :41) -> <= ~10k.

Full run = 45 q x 2 families x 2 repeats = 180 turns: judge calls ~360 typical, 540 max
(+ <= 180 fallback); <= ~5.4M judge input tokens worst case; embeds <= 540; plus the
parity statements (Data API only). No dollar figure here: the pilot prints actual counts and
the token usage the judge returns, and the full-run estimate is extrapolated from that and
**shown before the full run starts** (`--yes` required to proceed).

**Pilot first:** `--pilot` = 3 questions (one `search_records`-shaped, one "when did" ->
`latest_activity`, one control), 1 family, 1 repeat, plus full binding parity. Prints per
host HTTP counts, judge calls, tokens, SQL statements, template registry, invalid turns.

## 7. Secrets

Vendor keys (`CORROBORATION_API_KEY` for the judge via `corroboration_client.api_key`,
`DASHSCOPE_API_KEY`, the `llm_utils` fallback key) are read at runtime from
`lambda get_function_configuration` of `${P}-ask-voice-worker` (read-only call, profile
`fieldsight-deployer`) into `os.environ` of the eval process only, through an explicit
allowlist: the three keys above plus the §2.1.2 flags. Everything else in that response
(ElevenLabs, Anthropic and other keys) is never injected. Never printed, logged,
written to results, or passed on a command line; the fingerprint records `{"name": present}`
only. The env dict is deleted after injection. Logging: the root logger is set to INFO (the
orchestrator logs `ASK_ROUTE` at INFO on the root logger, :1263) but its only handler is an
in-memory capture that keeps `ASK_ROUTE` records and drops the rest; nothing goes to the
console, so no vendor response body is echoed.

## 8. Data handling

Results contain prod transcript snippets and the model's answers. Same rule as existing
`results/`: written there, only on this branch (`feat/retrieval-eval-baseline`), never merged
to develop/main. Per turn stored: funnel flags, tool calls+args, route, counts, the
evidence-bearing line excerpts (<= 160 chars each), the final answer text and refs. Full
renders/messages only with `--keep-renders`, into the scratchpad, never into `results/`.
`caller_sub` is stored only as `ask_voice_turn.sub_hash`.

## 9. Out of scope, risks, open questions

Out of scope: voice channel, web answers, multi-turn history/threads, access-control
correctness, latency (in-region), answer quality grading. Phase 2 (`answer_text`): beyond
`cited`, word-pattern matching on the answer is weak (answers paraphrase; patterns carry ASR
spellings), so phase 2 means a blind human/LLM grade of the answer against the gold quote —
a separate spec.

Risks: (1) adapter type fidelity — mitigated by parity + invalid-turn accounting; (2) timing
bias from the raised deadline — flagged turns; (3) a judge model change in the deployed env
between runs invalidates comparisons — model chain is in the fingerprint and diffs refuse
across different chains; (4) `latest_activity` picks the newest matching day, which moves as
the author records more — `now` pinned and recorded, compare same-day only; (5) running wakes
prod Aurora from scale-to-zero (as `run.py` already does); (6) `PROJECT_DOCUMENTS` on means
round 1 adds `search_project_documents` for requirement words (`_REQUIREMENTS` :465; gold
v1-03 says "contract") — kept as prod behaves, recorded.

Open questions:
- **Q1 (blocking).** Does user `f531eb99-...` have a `cognito_sub`? If not, whose login do we
  run as, with that person's agreement, and does its `visible_scope` include the gold site?
- Q2. Measure at `origin/develop@23cb443c` now, or wait for `origin/main` after #1157? (Spec
  default: develop now, re-run at main.)
- Q3 (owner, non-blocking). A read-only Postgres role + secret for the ruler would add a
  role-level guarantee on top of `READ ONLY` transactions.
- Q4. Is phase-1 policy `never` acceptable as the headline, with 1b as the prod-policy
  diagnostic?
- Prerequisite: the `fspy` env imports `psycopg` (db/connection.py imports it at module level)
  and `pgvector` is not required by the adapter.
