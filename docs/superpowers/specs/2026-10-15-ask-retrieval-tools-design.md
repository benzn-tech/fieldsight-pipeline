# Ask retrieval tools — design

**Date:** 2026-10-15
**Owner line:** 搜索和向量 (search & vector). This spec defines the TOOL LAYER only.
**Companion spec (other line, ASR+LLM优化v3):** Ask orchestration v2 — parallel
evidence, judge/synthesiser, bounded tool loop, unified async transport. It
CONSUMES this spec's contract and owns everything about *when* a tool is called.
**Status:** revised after Fable review (approve with changes; all findings resolved, see §8).

## 1. Why

Today Ask chooses a route BEFORE it has seen what the records hold
(`_classify_for_route` in `lambda_ask_agent.py`): one model call labels the
question `project|general|mixed` and whether "the records answer it", using the
top-5 chunks of ONE similarity search. Owner-reported on TEST 2026-10-15:
"What did we do yesterday?" (nothing recorded on the 14th) retrieved chunks from
the 12th, was judged `project` + `records_answer=false`, and was answered from
the web ("inspect your site diary…"). #1120 patched the voice path; the screen
path still does it.

The root problem is not the classifier. It is that the retrieval surface is a
single verb — "embed the question, return k chunks" — so every question,
including ones that are not similarity questions at all, is forced through it:

| Question shape | What it really needs | What it gets today |
|---|---|---|
| "What did we do yesterday?" | the list of what happened on a day | top-k chunks nearest an embedding of "what did we do" |
| "When did we last pour concrete?" | the most recent matching day | top-k chunks, any date |
| "What did Mike say about scaffolding?" | a person resolved inside the ACL, then search | `author` = exact folder name, or nothing |
| "How long did I record on Monday?" | a number from SQL | already exists (`mode: "metric"`), reachable only via `metric_slots` regex |

The orchestration line wants an agent that picks tools. This spec gives it the
tools: small, deterministic, ACL-enforced, each answering one question shape,
all returning the same evidence envelope so a judge model can mix them with web
evidence without format work.

## 2. Non-goals

- Deciding WHEN to call a tool, the loop, the judge prompt, web search, the
  transport (sync vs async/turn-store), streaming, voice brevity. → companion spec.
- New ranking/embedding work. Tools reuse `search_chunks`, rerank, and the
  session/report family switch exactly as they are.
- Writes of any kind. Every tool is read-only.

## 3. The contract

### 3.1 Tool definitions (what the model sees)

Each tool is a JSON-Schema function definition, provider-neutral (renders to
OpenAI/OpenRouter `tools[]` and Anthropic `tools[]` without change). Names,
descriptions and schemas live in ONE module, `src/ask_tools.py`, as data
(`TOOLS: list[dict]`). The orchestrator imports them; it never restates them.

Arguments the model may set are only the ones listed. **Identity, company,
ACL, timezone and "today" are never arguments**: the executor injects them
from the authenticated caller (`caller_sub`) and the request (`tz`). A model
cannot widen its own visibility by naming a site, person or company it cannot
see — out-of-ACL values resolve to "no match", never to an error that confirms
existence.

Dates are ISO `YYYY-MM-DD` in the caller's local timezone. The orchestrator
puts today's local date in the system prompt; the executor additionally
accepts the fixed vocabulary `today|yesterday|day_before_yesterday|this_week|
last_week|this_month|last_month`, resolved by a new PUBLIC
`query_slots.span(today, name)` (today's private `_span`, exported; the
existing sentence-level `time_range()` is unchanged). If `today` cannot be
resolved (`resolve_today` returns None for a bad or absent `tz`), any relative
date is `meta.error="bad_args"` with `field="tz"` -- never "unfiltered".
Clamps: `day_overview` <= 7 days; every other dated tool <= 92 days; a clamp
is reported in `meta.clamped`.

**Shared visibility rules for every tool** (one helper, used by all five):
- sites = `scope.visible_scope(...).site_ids`; authors = its `author_ids`
  (None = no author filter, i.e. admin/gm/pm).
- topics reads apply `visible_topics_predicate` AND
  `work_class IS DISTINCT FROM 'non_work'` (NULL counts as work -- the rule the
  timeline and `session_index.build_chunks` already use).
- topics with NULL `user_id` stay hidden under a graded author filter (as
  `topics.py` does today).
- **Family rule on the topics side** (mirrors `/timeline`,
  `lambda_org_api.py` ~10307 + `topics.has_topics_for_source_prefix`):
  per (user, day) across the caller's `site_ids`, the extraction family
  (`source_s3_key LIKE 'extractions/%'`) is used when ANY not-superseded
  extraction topic exists — non_work and deleted ones included, exactly as
  the timeline's presence check counts them; otherwise the report family
  (`source_s3_key LIKE 'reports/%'`). Topics under any other prefix or with a
  NULL key are not shown (the timeline never shows them). NULL-`user_id` rows
  decide presence per (site, day). The outer row still applies every
  visibility arm and the non_work exclusion. The chunks side keeps
  `family_predicate` exactly as `search_records` does.
- Known v1 limitation: merged meetings (`merged_keys`) are not mirrored, so a
  joiner of a merged meeting does not see it in `day_overview`.
- Alias rewriting (`aliases.list_active`) is applied to every returned text
  field, as `_search` does today, so all tools spell names the same.
- `site` argument: a site UUID or a site name, resolved only within
  `site_ids` (slugs are often NULL); anything else resolves to no match.

#### v1 tools (this spec, phase 1)

1. **`day_overview`** — "what happened on these days"
   - args: `date_from` (req), `date_to` (opt, ≤ 7 days span), `site` (opt), `person` (opt)
   - returns one evidence item per visible topic in range (shared visibility
     rules above), chronological: title, local time, site, recorder, one-line
     summary, item counts (tasks/findings) -- no embedding, pure SQL.
   - `time` = the topic's stored `time_range` text (local "HH:MM - HH:MM", as
     the timeline shows it); omitted (never guessed) when NULL.
   - the SQL fetches `limit + 1` rows; the dispatcher returns `limit` and sets
     `meta.truncated` when the extra row exists.
   - empty result carries `meta.empty_reason` (see 3.3) and
     `meta.latest_before = {"date": ..., "from": "topics"}` -- the latest
     visible topics day before `date_from`, under the same rules (so "nothing
     yesterday, latest is the 12th" needs no second call). `from` names the
     table because chunks and topics can disagree when a topic has no chunk.

2. **`search_records`** — "find what was said about X"
   - args: `query` (req), `date_from`/`date_to` (opt), `site` (opt), `person` (opt), `k` (opt, 1–12, default 8)
   - wraps today's path: embed (DashScope, in ask-agent) -> `lambda_rag_search`
     `_search` -> the existing post-processing (`_admit_one_lexical_hit`, then
     `_rerank_chunks` when `ENABLE_RERANK` is on -- it is on in TEST and prod).
     `k` is the count AFTER that merge (rag-search returns up to `k*2` rows).
     Family switch (`SEARCH_INDEX_READ`) honoured as-is.
   - no implicit widening: an empty dated search returns empty with
     `meta.latest_before = {"date": ..., "from": "chunks"}`
     (`latest_visible_date`); widening is the orchestrator's decision.

3. **`latest_activity`** — "when did X last happen / what's the latest"
   - args: `query` (opt), `on_or_before` (opt, default today), `site` (opt), `person` (opt)
   - no `query`: the latest visible topics day (`topics.latest_visible_topic_date`, same rules as `day_overview` -- ruling 2026-10-16, so it agrees with `day_overview` rather than with chunks) + its `day_overview`.
   - with `query`, `meta.matched_date` names the chosen day even when no row survives the filter.
   - with `query`: the most recent day that has a chunk within a distance gate
     for `query`, + the matching items from that day. The gate is owned here
     (`ASK_TOOLS_MATCH_GATE`, default 0.55 = today's `_NO_LEX_MAX_DIST`);
     keyword-arm hits pass regardless, as in ask-agent. New SQL
     `latest_matching_date()` is an exact scan (the `+ 0` ordering forbids HNSW)
     bounded to the 92-day window ending `on_or_before`; it has its own latency
     target (3.4), not the 400 ms one.

4. **`find_people`** — "who is Mike"
   - args: `name` (req)
   - candidate set = users with a footprint on the caller's `site_ids` (topics
     OR recordings OR live membership -- a set-returning sibling of
     `recordings.author_footprint_in_sites`), intersected with `author_ids`
     when that is not None. External authors are found through the GLOBAL
     directory lookup (`users.get_by_folder_name_global` family), never a
     company-pinned one, and only if they are in the candidate set.
   - matches display name, first name, folder name, and active `name_aliases`
     rows with `kind='person'` whose `right_term` resolves to a candidate.
     There is no nickname column; nothing else counts as an alias.
   - each match carries `person_ref` = the user id. Ambiguity is returned, not
     resolved: two Mikes -> two refs.
   - `person` on other tools accepts a `person_ref` or a raw name. **Every call
     re-validates it** (candidate-set membership as above + the existing
     `author_ids is None or id in author_ids` check): a ref is never trusted
     because `find_people` minted it. A raw name that resolves to != 1 person
     yields `meta.person_ambiguous` / `meta.person_unresolved` and NO rows
     (never falls back to "everyone").

5. **`count_metric`** — "how long / how many"
   - args: `metric` (enum: the 5 in `_METRIC_UNITS`), `date_from`, `date_to`
   - wraps existing `mode: "metric"`; all seven of its zero kinds
     (`nothing_visible`, `nothing_recorded`, `no_rows_for_that_day`,
     `none_of_that_kind`, `nothing_of_yours`, `none_in_domain`, `no_topics`)
     pass through as `meta.empty_reason`. It takes no `site`/`person`; setting
     either is `bad_args`.

#### Phase 2 (same contract, separate PR, after v1 is consumed)

6. **`list_items`** — open/closed tasks, findings, decisions (`action_items`,
   `findings`, `decision_records` repos) with status/date/site/person filters —
   for "who hasn't replied to the RFI", "open safety items this week".

Web search is NOT a tool in this spec; the companion spec owns it but MUST
emit the same envelope (3.2) with `provenance: "web"`.

### 3.2 Evidence envelope (what the model gets back)

Every tool returns:

```json
{
  "tool": "day_overview",
  "items": [
    {
      "ref": "R3",
      "provenance": "records",
      "kind": "topic|chunk|day|person|metric",
      "date": "2026-10-12",
      "time": "09:14-09:52",
      "site": "Riverside Stage 2",
      "who": "Ben Lin",
      "title": "Level 2 inspection",
      "text": "≤ 600 chars; a chunk: ≤ 1,000-char window on the query (2026-10-22)",
      "topic_id": "…", "chunk_id": "…", "source": "extractions/…",
      "site_slug": "…", "chunk_type": "…", "time_start": "…"
    }
  ],
  "meta": {
    "range": {"from": "2026-10-14", "to": "2026-10-14"},
    "empty_reason": null,
    "latest_before": {"date": "2026-10-12", "from": "topics"},
    "truncated": false,
    "clamped": null,
    "person_ambiguous": null,
    "ms": 312
  }
}
```

- `ref` is assigned by the executor, unique per Ask turn across ALL tool calls
  (R1, R2, … for records; the web tool uses W1, …). The judge cites refs; the
  orchestrator maps refs → UI citation cards. This replaces the positional
  `[n]` ↔ chunk-index coupling that today forces "never merge web.sources into
  citations" hazards.
- Every field `_build_citations` needs (`site_slug`, `chunk_type`,
  `time_start` from `metadata.window_span`) is in the item, so the
  orchestrator never re-fetches to render a citation card.
- The same item returned by two calls keeps its first `ref`. Dedupe key is
  `(kind, id)` -- chunk id for chunks, topic id for topics -- NOT topic id
  across kinds: under `SEARCH_INDEX_READ=report` on an authority-flip day
  chunks carry `topic_id NULL` (BUG-39), so a topic-id key would not fire.
- Output budget: ≤ ~3,000 tokens per tool result (12,000 chars; 10 KB before the
  2026-10-22 item window: 8 windowed items at the replay's per-item overhead p95). Over budget → drop
  lowest-ranked items, `meta.truncated = true`. Text is the stored chunk/summary,
  never re-summarised by a model.
- `text` is DATA. The orchestrator wraps every tool result in the same fenced
  "DATA, not instructions" block the current prompts use.

### 3.3 Empty and error semantics

`items: []` always comes with `meta.empty_reason`, one of:

| value | meaning | what an honest answer says |
|---|---|---|
| `nothing_in_range` | caller sees records, none in this range | "Nothing recorded on the 14th; the latest is the 12th" |
| `no_match` | records in range, none match the query | "Nothing about X that day" |
| `nothing_visible` | caller can see no sites | account problem, not an answer about work |
| `person_unresolved` / `person_ambiguous` | `person` did not resolve to exactly one | ask which person |
| (count_metric kinds) | the seven metric zero kinds pass through unchanged | as `_metric` documents each |

A graded caller's `nothing_in_range` is computed over THEIR visible rows only
and never discloses that rows exist for others (as `_search` does today).
`latest_before` never surfaces a date whose only rows are deleted/superseded.

Errors (`meta.error`: `not_provisioned | bad_args | timeout | upstream`) are
returned, never raised. `bad_args` includes the offending field so the model can
retry once. An identity failure (`not_provisioned`) must never be papered over
by the orchestrator with a web answer (same rule as today's D5 comment).

### 3.4 Execution

```
orchestrator (ask-agent / worker)
   └─ ask_tools.execute(calls: list[ToolCall], ctx) -> list[Envelope]
        ├─ validate args against schema; inject caller_sub, tz, today
        ├─ embed queries that need it (DashScope, parallel)
        ├─ ONE Lambda invoke of rag-search: {"mode": "tools", "sub", "calls",
        │     "deadline_s"}: in-VPC, one pooled connection, one ACL resolution,
        │     calls run sequentially, each in its own try/except (a failure is
        │     meta.error="upstream" for THAT call; the others still return);
        │     a call that cannot start before deadline_s returns "timeout"
        │     without running
        ├─ rerank search_records results (existing _rerank_chunks)
        └─ assign refs, dedupe, enforce output budget
```

- Batch, not one invoke per tool: a model turn that asks for 3 tools pays one
  Lambda round trip + one ACL resolution, not three. rag-search stays the only
  in-VPC component; ask-agent keeps the egress-requiring work (embedding,
  rerank).
- Latency targets (in-region, warm): `day_overview`/`latest_activity` (no
  query)/`find_people`/`count_metric` <= 400 ms p95 SQL; `search_records`
  <= 1.5 s p95 including embed + rerank; `latest_activity(query)` <= 1.5 s p95
  SQL (exact scan, 92-day bound) -- measured; if missed, the window shrinks
  before anything else changes.
- **Hard ceiling:** `RagSearchFunction` `Timeout: 23` bounds a whole batch
  whatever the transport (the voice worker's longer timeout does not extend
  it). The executor passes `deadline_s = min(ctx.deadline_left, 20)` and caps
  a batch at 4 calls. Payload is not a constraint: a few 1024-float
  embeddings in, <= 12.5 KB per result out, far below the 6 MB sync limit.
- Per-call timeout passed in by the orchestrator (`ctx.deadline`); a call that
  cannot start before the deadline returns `meta.error = "timeout"` without
  running.
- Logging: tool name, arg SHAPE (which fields set, range length — not values),
  item count, empty_reason, ms, caller hash. Never query text or item text (same
  rule as `ask_voice_turn`).

### 3.5 Where the code goes

| File | Change |
|---|---|
| `src/ask_tools.py` (new) | `TOOLS` definitions, arg validation, date vocabulary, `execute()`, ref assignment, budget |
| `src/lambda_rag_search.py` | new `mode: "tools"` dispatcher; reuses `_search` internals and `_metric` |
| `src/repositories/chunks.py` | `latest_matching_date()` |
| `src/repositories/topics.py` | `day_overview_rows()`, `latest_visible_topic_date()` (shared visibility rules) |
| `src/repositories/recordings.py` / `users.py` | `author_candidates_in_sites()` (set-returning footprint), `find_visible_people(conn, site_ids, author_ids, name)` |
| `src/query_slots.py` | export `span(today, name)` (`_span` kept as an alias) |
| `src/lambda_ask_agent.py` | none in this spec (the companion spec wires `execute`) |
| `template.yaml` | none (rag-search already invoked by ask-agent and the voice worker) |

Additive only: no existing mode, field or default changes; no flag is needed
because nothing calls `mode: "tools"` until the orchestrator does.

## 4. Testing and acceptance

1. **Unit** (`tests/unit/test_ask_tools.py`): schema validation, injected
   fields cannot be overridden by args, date vocabulary resolution incl. NZ
   DST boundaries (`nz_time`), clamp, ref uniqueness + dedupe across calls,
   output budget truncation, every empty_reason path.
2. **PG integration** (local pgserver, `fswork/run-tests.sh`): the three new
   SQL functions executed for real against seeded rows incl. a deleted/
   superseded topic, a non_work topic, an out-of-ACL site, a graded-visibility
   author, and a session-family vs report-family day. "SQL never executed by
   any test" is not acceptable for any new query.
3. **ACL red test**: for each tool, a caller from company B naming company A's
   site/person/date gets the same response shape as "no match" — asserted on
   real SQL. Further negatives, all on real SQL: a `non_work` topic never
   appears in `day_overview`/`latest_activity`; a NULL-`user_id` topic stays
   hidden under a graded filter; a deleted recording's date never becomes
   `latest_before`; a graded caller's empty range does not reveal others'
   rows; a `person_ref` minted for company A and sent by company B returns the
   no-match shape; call 2 of 3 raising still returns calls 1 and 3.
4. **Tool-level eval on TEST data** (extend `feat/retrieval-eval-baseline`):
   - 10 "what did we do <day>" questions on days that HAVE Aurora topics
     (extraction days, or report days where ingest ran) -> `day_overview`
     returns every topic the Aurora-rendered timeline shows for that user-day
     (recall = 1.0 required). Days served verbatim from S3 `daily_report.json`
     with no Aurora topics are excluded and counted separately.
   - 10 "when did we last <X>" → `latest_activity` date equals the hand-checked
     date (≥ 9/10).
   - existing 39-question Ask set through `search_records` → hit rate not below
     current 82.1% (same scorer).
5. **Latency**: p50/p95 per tool measured in-region on TEST, 30 calls each,
   reported in the PR.

## 5. Rollout

1. PR to develop (this line), CI + PG tests, deploy TEST — inert until called.
2. Hand the contract to the orchestration line; they build against TEST.
3. Prod release with the orchestration release (tools are inert alone).

## 6. Decisions taken (owner-delegated)

- **Batch invoke in rag-search** over a new Lambda: keeps one in-VPC reader, one
  ACL code path. Cost if wrong: a later split, contract unchanged.
- **No implicit widening inside tools**: widening is a reasoning step; tools
  report `latest_before` and let the orchestrator/judge decide. Cost if wrong:
  one extra tool round on "yesterday" questions — mitigated because
  `day_overview` already returns `latest_before`.
- **Refs, not positional `[n]`**: removes a known hazard class; the UI mapping
  change is the orchestrator line's.
- **Five tools in v1**, `list_items` deferred: v1 covers every failure the owner
  hit; items need their own visibility review.

## 7. Open questions for the companion spec

- Does the judge see raw envelopes, or a compacted render? (This spec only
  guarantees ≤ ~3,100 tokens per result.)
- Max tools per turn / rounds — this spec supports any batch size; suggest ≤ 4
  calls per round, ≤ 3 rounds.

## 8. Fable review (2026-10-15) -- disposition

Verdict: approve with changes. Folded into the text above:
C1 non_work filter (shared visibility rules) / C2 find_people candidate set +
global lookup / I3 alias = `name_aliases kind='person'` only / I4 person_ref
re-validated per call / I5 topics-side family rule, `(kind,id)` dedupe,
`latest_before.from` / I6 own match gate + own latency line / I7 public `span`,
`bad_args: tz` / I8 rag-search 23 s ceiling, `deadline_s`, <= 4 calls, payload
note / I9 acceptance limited to Aurora-topic days / I10 citation fields in the
envelope / I11 per-call isolation / M12 seven metric zero kinds, no
site/person / M13 `time` source / M14 site by UUID or name / M15 alias rewrite
on all text / M17 clamps split (7 / 92).

Not taken: M16's premise that rerank is off in both environments --
`ENABLE_RERANK=true` on TEST and prod (set 2026-10-13/14; TEST log 2026-10-15
`rerank: 35 candidates -> 5`). The rest of M16 (k is post-merge) is taken.

## 9. Final-review amendments (2026-10-16)

- `latest_before` is `{"date": str | null, "from": "topics" | "chunks"}`; `date` is null when nothing earlier exists.
- Topic items carry `n_tasks` and `n_findings`. Chunk items carry no `who` (the live search SQL selects no author name).
- `latest_activity` rejects `date_from`/`date_to`; it takes `on_or_before`.
- The dispatcher refuses to START `latest_activity(query)` with < 5 s left (`meta.error="timeout"`).
- Live search SQL (`build_search_sql`, `build_latest_date_sql`) is pinned by sha256 golden tests.
