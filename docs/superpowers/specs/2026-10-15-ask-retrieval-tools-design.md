# Ask retrieval tools — design

**Date:** 2026-10-15
**Owner line:** 搜索和向量 (search & vector). This spec defines the TOOL LAYER only.
**Companion spec (other line, ASR+LLM优化v3):** Ask orchestration v2 — parallel
evidence, judge/synthesiser, bounded tool loop, unified async transport. It
CONSUMES this spec's contract and owns everything about *when* a tool is called.
**Status:** draft for Fable review.

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
accepts the small fixed vocabulary `today|yesterday|this_week|last_week|
this_month|last_month` and resolves it with `query_slots` (same code as the
search box, #1100) so a model that does not do date arithmetic still lands on
the right day. Ranges longer than 92 days are clamped and the clamp is reported
in `meta.clamped`.

#### v1 tools (this spec, phase 1)

1. **`day_overview`** — "what happened on these days"
   - args: `date_from` (req), `date_to` (opt, ≤ 7 days span), `site` (opt), `person` (opt)
   - returns one evidence item per visible topic in range, chronological:
     title, local start–end time, site, recorder, one-line summary, item counts
     (tasks/findings/decisions) — no embedding, pure SQL over `topics` with
     `visible_topics_predicate` + the caller's ACL.
   - empty result carries `meta.empty_reason` (see 3.3) and `meta.latest_before`
     = the latest visible day before `date_from` (so "nothing yesterday, latest
     is the 12th" needs no second call).

2. **`search_records`** — "find what was said about X"
   - args: `query` (req), `date_from`/`date_to` (opt), `site` (opt), `person` (opt), `k` (opt, 1–12, default 8)
   - wraps today's path: embed (DashScope, in ask-agent) → `lambda_rag_search`
     `_search` → rerank. Family switch (`SEARCH_INDEX_READ`) honoured as-is.
   - no implicit widening: an empty dated search returns empty with
     `meta.latest_before`; widening is the orchestrator's decision.

3. **`latest_activity`** — "when did X last happen / what's the latest"
   - args: `query` (opt), `on_or_before` (opt, default today), `site` (opt), `person` (opt)
   - no `query`: the latest visible day (`latest_visible_date`) + its `day_overview`.
   - with `query`: the most recent day that has a chunk within the search
     distance gate for `query`, + the matching items from that day.

4. **`find_people`** — "who is Mike"
   - args: `name` (req)
   - returns the people visible to the caller (ACL: author set of
     `scope.visible_scope`) whose display name / first name / alias matches,
     each with an opaque `person_ref` the other tools accept as `person`.
     Ambiguity is returned, not resolved: two Mikes → two refs.
   - `person` on other tools accepts either a `person_ref` or a raw name; a raw
     name that resolves to ≠1 person yields `meta.person_ambiguous` /
     `meta.person_unresolved` and NO rows (never falls back to "everyone").

5. **`count_metric`** — "how long / how many"
   - args: `metric` (enum: the 5 in `_METRIC_UNITS`), `date_from`, `date_to`
   - wraps existing `mode: "metric"`; its three-zero semantics become
     `meta.empty_reason`.

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
      "text": "≤ 600 chars",
      "topic_id": "…", "chunk_id": "…", "source": "extractions/…"
    }
  ],
  "meta": {
    "range": {"from": "2026-10-14", "to": "2026-10-14"},
    "empty_reason": null,
    "latest_before": "2026-10-12",
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
- The same chunk/topic returned by two calls keeps its first `ref` (dedupe by
  `chunk_id`/`topic_id`).
- Output budget: ≤ 2,500 tokens per tool result (≈ 10 KB). Over budget → drop
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
        ├─ ONE Lambda invoke of rag-search: {"mode": "tools", "sub", "calls": [...]}
        │     in-VPC, one pooled connection, one ACL resolution, calls run
        │     sequentially on that connection (each ≤ 1 SQL round trip)
        ├─ rerank search_records results (existing _rerank_chunks)
        └─ assign refs, dedupe, enforce output budget
```

- Batch, not one invoke per tool: a model turn that asks for 3 tools pays one
  Lambda round trip + one ACL resolution, not three. rag-search stays the only
  in-VPC component; ask-agent keeps the egress-requiring work (embedding,
  rerank).
- Latency targets (in-region, warm): `day_overview`/`latest_activity` (no
  query)/`find_people`/`count_metric` ≤ 400 ms p95 SQL; `search_records`
  ≤ 1.5 s p95 including embed + rerank. A batch's wall time ≈ max(embed) + sum(SQL).
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
| `src/repositories/chunks.py` | `day_overview_rows()`, `latest_matching_date()` |
| `src/repositories/users.py` (or `directory.py`) | `find_visible_people(conn, author_ids, site_ids, name)` |
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
   real SQL.
4. **Tool-level eval on TEST data** (extend `feat/retrieval-eval-baseline`):
   - 10 "what did we do <day>" questions → `day_overview` returns every topic
     the web timeline shows for that user-day (recall = 1.0 required).
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
  guarantees ≤ 2,500 tokens per result.)
- Max tools per turn / rounds — this spec supports any batch size; suggest ≤ 4
  calls per round, ≤ 3 rounds.
