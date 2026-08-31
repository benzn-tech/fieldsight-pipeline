# External corroboration for Ask — design

**Date:** 2026-08-31
**Status:** design, second draft (first draft's blocking defects listed in §11)
**Split:** backend (a separate session takes this) · frontend (this session ships it)

---

## 1. What this is

Today Ask answers only from the customer's own recordings. This adds a second,
clearly separated block underneath the answer: **what the open web says about
the named things in it.**

The user's framing, verbatim:

> 比如我问："前天会议 XX 提到了一个公司的 CEO 是谁来的？" 这时候不仅 search 给答案，
> 并且根据 context 比如之前聊到 construction，又提到 Naylor Love，
> 那就对这个 CEO 进行一个简单的搜索并呈现内容在答案下方。

It is one of the three differentiators recorded for VizField
(`PLAYBOOK.md:44` — *"answer + external web cross-verification"*). Neither
product has a line of it. This spec builds it in FieldSight first.

### The failure mode this is designed against

Not "no answer". **A confident wrong answer that looks verified.**

An unverified answer is obviously unverified. A block labelled *verified* that
contains a plausible fabrication is worse than nothing, because it spends trust
the product cannot re-earn — and the audience is a low-digital-literacy site
user with no cheap way to check. Every decision below that looks over-cautious
traces to this sentence.

---

## 2. The two paths, and which one v1 builds

| | trigger | v1 |
|---|---|---|
| **P2** | 用户搜索时 — answer + contextual external verification below | **build** |
| **P1** | topic 内出现疑惑/不确定性 → 进一步搜索排查 | **defer** |

**v1 is P2 only.** In order of weight:

1. **P2 is user-initiated.** The user typed the question, so that question
   going outward is a consequence they can see. P1 fires unprompted on every
   meeting and sends conversation-derived queries to a third party **with
   nobody in the loop** — a privacy decision, not an implementation detail (§4).
2. **P1 has nowhere to render.** Its natural home would be a per-topic surface,
   and the one that exists — the `findings` table — holds something else (§10,
   decision 3).
3. P1 needs a worker, a queue, and a supersede rule for a resolution that
   arrives after the meeting ended.

P1 is sequenced behind P2 shipping and the privacy decision landing.

---

## 3. The call graph, measured

The first draft of this spec verified the repo's *comments* and not its *call
graph*, and five of six blocking defects lived one layer below where it stopped
reading. Everything in this section was read out of the files named.

### 3.1 There is no route on the ask lambda

`AskAgentFunction` (`template.yaml:1510`) has **no API Gateway `Events` block
at all**. It is never reached by a URL. `ApiFunction` invokes it over
`lambda:InvokeFunction`.

Path dispatch lives in `lambda_fieldsight_api.py:1451`:

```python
elif path == '/api/ask'       and method == 'POST': return ask_question(body, caller)
elif path == '/api/ask/voice' and method == 'POST': return ask_voice(body, caller)
```

Inside `lambda_ask_agent.lambda_handler` the dispatch is on **body content**
(`audio` present, `mode == 'search'`), not on a path. There is no `/search`
route; "search" is a mode.

**Consequence:** a new capability needs an edit in *two* files, and the second
one was missing from the first draft entirely.

### 3.2 Identity is injected server-side, on purpose

`ask_question` (`lambda_fieldsight_api.py:1222-1228`) builds the payload itself
and sets `'caller_sub': caller.get('sub', '')` from the Cognito authorizer. The
client never supplies it — that is what stops a caller asking as someone else.

**Consequence:** the corroborate contract must be `{question, answer}` and
nothing else. A spec that says "the frontend echoes back `caller_sub`" is
instructing the implementer to trust the body. (It also could not work: the
`/ask` response is `answer/citations/model/grounded/basis` — `caller_sub` is
not in it.)

### 3.3 The LLM provider is qwen on TEST and anthropic on prod

```
deploy.yml:229       LlmProvider=${{ vars.TEST_LLM_PROVIDER || 'qwen' }}
deploy-prod.yml:246  LlmProvider=${{ vars.PROD_LLM_PROVIDER || 'anthropic' }}
```

`llm_utils.call_llm` dispatches on that. So on TEST it calls DashScope, not
Anthropic.

**Consequences, both real:**

- The Anthropic web-search server tool cannot be reached through `call_llm`.
  `_call_anthropic` sends `{model, max_tokens, messages}` with **no `tools`
  support**, and its response parsing joins `text` blocks only — discarding
  exactly the `web_search_result` and citation blocks the sources and URLs must
  come from.
- If the other steps went through `call_llm` they would run on a different
  model per environment, and every test would be exercising something other
  than what prod runs.

### 3.4 The timeout chain makes `call_llm` unusable here

| hop | limit | source |
|---|---|---|
| API Gateway integration | **29 s** | `template.yaml:1539` comment |
| `ApiFunction` | `Timeout: 30` | `template.yaml:3819` |
| `AskAgentFunction` | `Timeout: 60` | `template.yaml:1516` |
| one `call_llm` HTTP request | `LLM_HTTP_TIMEOUT: '45'` | `template.yaml:1524` |
| retries | `MAX_ATTEMPTS = 4`, backoff on 429/5xx | `llm_utils.py:74` |

**A single `call_llm` call is allowed to block for 45 seconds — longer than the
30-second proxy that is waiting for it.** With retries the worst case is about
187 seconds. And `call_llm` takes no timeout or deadline parameter
(`llm_utils.py:92`), so a caller cannot shorten it; `HTTP_TIMEOUT` is read from
env at import.

**Consequence:** the corroborate path does not use `call_llm` for any step. It
needs its own small client (§5.4). A "monotonic deadline checked between steps"
— which the first draft specified — cannot interrupt a call that is already
hanging, so it is not a safeguard.

### 3.5 What is true and load-bearing

`AskAgentFunction` has **no `VpcConfig`** (`template.yaml:1510-1562`), so it has
ordinary internet egress and needs no S3-request hop; BUG-36 governs the
in-VPC direction, and CLAUDE.md's BUG-43 note cites this function as the
non-VPC→VPC precedent.

The 29-second ceiling is why the ask path runs haiku rather than sonnet — the
template says so in its own comment. That constraint forces corroboration into
a **second request**, which lands on the shape the user decided independently
(memory `vizfield-two-pass-answers-first`): **快版秒出，第二遍推敲.** The
constraint and the product principle agree, which is the only reason to trust
either.

---

## 4. What may leave the account

**Only entities go to the search engine. Never conversation text.**

| allowed | never |
|---|---|
| a company name (`Naylor Love`) | anything about *our* dealings with it |
| a bare standard or code (`NZS 3604`) | a clause narrowed to the defect under dispute |
| a product or material name | prices, quantities, claim values |
| a public role noun (`CEO`) | worker names; any person not in a public role |
| a regulator or authority name | site names, addresses, our meeting dates |

The query is **assembled from fields** — `"Naylor Love" CEO` — never quoted from
a transcript. A test pins that the search step's prompt contains the entity
strings and nothing else.

**Implemented in `src/corroboration_gate.py`** (PR #656). It screens the entity
STRING only. An earlier draft of this section screened the sentence around the
entity as well; that was wrong in the expensive direction, because the sentence is
never sent and dropping the entity costs a corroboration on exactly the meetings
that matter most.

### 4.1 Two honest limits on that claim

**The gate is deterministic given its input, but its input is model-assigned.**
Extraction returns `[{entity, kind}]` and the allowlist filters on `kind`. A
kind label is only as good as haiku's classification: a person's name
misclassified as `company`, an outfit literally named "Ben Smith Contracting",
or a project codename shaped like a company all pass. So the gate also applies
deterministic **string-shape** checks that do not depend on the label —
length cap, reject entities carrying clause-level numbering beyond a bare
standard number, reject strings matching person-name shapes — and the residual
risk is accepted rather than denied.

What the gate *does* hold against: `"we're being sued by Naylor Love"` yields
the query `"Naylor Love"`, which leaks interest in a company and not the
dispute. That attack fails, and saying so is more useful than claiming the gate
is airtight.

**Step 4 sends the answer to the LLM provider.** Reconcile compares search
results against the answer, and the answer is conversation-derived. This is
defensible — the same provider already received the transcript during `/ask` —
but it is a different statement from "only entities leave", so it is stated
here rather than implied. Note the provider differs by stack (§3.3): on TEST
that is DashScope. No **search engine** ever receives the answer.

### 4.2 The customer-facing half

Whether external lookup is acceptable **at all** under the pilot customers'
terms is the user's decision (§10, decision 1). The flag defaults off; the code
can be built and tested before that answer exists and must not be switched on
for a customer until it does.

---

## 5. Backend design

### 5.1 Two files, not one

**`lambda_fieldsight_api.py`** — a new route beside the existing pair at :1451,
and a bridge modelled on `ask_question`:

```python
elif path == '/api/ask/corroborate' and method == 'POST':
    return corroborate_answer(body, caller)
```

`corroborate_answer(body, caller)` validates `question` and `answer`, builds
`{question, answer, mode: 'corroborate', caller_sub: caller.get('sub','')}`,
invokes `ASK_AGENT_FUNCTION`, and reuses `ask_question`'s `FunctionError`
handling verbatim — that guard exists so an unhandled exception in the agent
does not return a stack trace to the client, and the new route needs it for the
same reason.

**`lambda_ask_agent.py`** — a `mode == 'corroborate'` branch in
`lambda_handler`, matching the file's existing body-content dispatch style.
Not a path branch; this lambda has no paths.

**Request** `{question, answer}` — `caller_sub` is injected by the proxy.

**Response**

```json
{
  "corroborations": [
    { "entity": "Naylor Love", "kind": "company",
      "state": "corroborated",
      "claim": "CEO is …",
      "summary": "…",
      "sources": [ {"title": "…", "url": "…", "published": "2026-03-11"} ],
      "retrieved_at": "2026-08-31T09:12:04Z" }
  ],
  "dropped": [ {"entity": "…", "reason": "commercial_context"} ],
  "truncated": false,
  "timed_out": false
}
```

### 5.2 Four states, not three

`state` is an enum, never a boolean, never absent:

| state | meaning |
|---|---|
| `corroborated` | the answer makes a checkable claim about this entity **and** an external source agrees |
| `conflicts` | the answer's claim and the external source disagree |
| `not_found` | a checkable claim, searched, nothing usable came back |
| `no_checkable_claim` | the entity exists externally but the answer asserts nothing about it that can be checked |

The fourth state is the fix for the most likely way this feature quietly
becomes dishonest. Most Ask answers ("what safety issues were raised") assert
nothing externally checkable. Confirming that *Naylor Love is a real company*
and rendering it as `corroborated` invites the reader to hear it as *the answer
is verified* — the exact trust inflation §1 exists to prevent. **A
`no_checkable_claim` result is not rendered as verification**; §6.2 says how it
renders.

`conflicts` is the state the feature exists for and the easiest to lose to an
implementation that renders everything as a summary. It has a fixture test —
and that test proves the reconcile prompt *can* emit the token, not that real
traffic produces it. Whether it does is a question for the first week of TEST
data, not for a unit test.

### 5.3 Steps and budget

Usable budget is set by `ApiFunction`'s 30 s, not the agent's 60 s: an agent
still working at 35 s is returning to a proxy that died. Target **≤ 25 s**, with
a hard internal stop at 24 s.

| # | step | client | budget |
|---|---|---|---|
| 1 | entity extraction → `[{entity, kind, claim}]` | dedicated (§5.4), haiku, JSON | 4 s |
| 2 | privacy gate + cap 3 | pure Python | <10 ms |
| 3 | one web-search call covering the surviving entities | dedicated, Anthropic + `web_search` tool | 12 s |
| 4 | reconcile → assign a state per entity | dedicated, haiku | 6 s |

Step 1 extracts the **claim the answer makes** about each entity, not just the
entity — step 4 cannot assign `no_checkable_claim` without it, and asking for
it once is cheaper than inferring it twice.

**Cap at 3 entities** — for the reader, not for cost. Six cards under one answer
is a wall, and the audience is the reason this product exists.

**`truncated: true`** when more than 3 entities survived the gate. This is
deterministic and useful. It is *not* the same as `timed_out`.

**`timed_out: true`** when a step exceeded its slice. Step 3 is a single call,
so a timeout there yields **no** corroborations — there is no per-entity
progress to salvage. The first draft claimed both "one call" and "return the
one that finished", which cannot both be true.

### 5.4 A dedicated client, and why not `llm_utils`

Per §3.4, `call_llm` can block for 45 s with no way for a caller to shorten it,
retries four times, and cannot send `tools` or read `web_search_result` blocks.

The new client is small and lives beside the feature:

- explicit `timeout=` per call, computed from remaining budget
- **at most one retry**, and only if the remaining budget covers it
- Anthropic only, on every stack — so TEST and prod exercise the same model and
  the tests mean something. It reads `ANTHROPIC_API_KEY`, which
  `AskAgentFunction` already holds (`template.yaml:1516`) regardless of
  `LlmProvider`.
- parses `web_search_result` and citation blocks, which is the whole point

This is a deliberate divergence from the repo's shared client. The alternative —
threading a `deadline=` parameter through `llm_utils._post_with_retry` — changes
a module every other lambda imports, to serve one caller with an unusual
constraint. If a second caller ever needs it, that is the time.

### 5.5 The switch

`ENABLE_EXTERNAL_CORROBORATION`, default `'false'`, passed by **both** workflows
(`TEST_…` / `PROD_…`), read at call time.

The repo's trap: an env declared in the template but not threaded through a
workflow yields the default silently, with no error anywhere
(memory `fieldsight-unwired-toggle-trap`). Verification is not "the test
passes" — it is **read the deployed function's env**, then flip a workflow
default and confirm a test turns red.

⚠️ Both workflow files are currently a **rebase hot spot**: parallel sessions
add parameters on the same lines (PR #654 hit exactly this and had to be
rebased). Add the parameter, expect the conflict, keep both sides.

### 5.6 Untouched

`_rag_answer` and the `/ask` response shape; `RagSearchFunction`; `llm_utils`;
`findings`. A frontend that never calls the new route sees today's behaviour
exactly.

### 5.7 Coordination

`spec/ask-answers-with-numbers` (PR #653) also targets the ask path and is
**spec-only, no code** as of writing. It shapes the grounded answer; this
appends a block after it. Both will edit `lambda_fieldsight_api.py`'s dispatch
chain — **whichever lands second rebases.**

---

## 6. Frontend design

### 6.1 The one thing that matters

**Grounded and external must never look like the same kind of statement.**

The answer is evidence about the customer's site. The corroboration block is
evidence about the world. Rendering them in one register invites the reader to
trust the second as much as the first, and the second is the one that can be
wrong in ways they cannot detect.

Separation is carried by all of: a labelled divider, a distinct surface, the
source domain inline on every claim, and the retrieval date. Not colour alone.

Copy is **English**, matching the RAG prompts' own rule
(`lambda_ask_agent.py:512`, `:527` — "customer-facing responses are
English-only for now").

### 6.2 Rendering the four states

- `corroborated` — claim, source domain, date.
- `conflicts` — the strongest treatment on the block: *the recording said X, the
  web says Y*, both attributed.
- `not_found` — a plain line, *no reliable external source found*. **Rendered,
  not omitted** — an absence reads as "fine".
- `no_checkable_claim` — the weakest: the entity, and nothing that could be
  mistaken for verifying the answer. Never a tick.

`truncated` adds one line naming how many entities were not checked. A silent
cap reads as "we checked everything" (memory: no silent caps).

### 6.3 Failure is visible, not swallowed

A failed or timed-out corroborate request renders **a muted line** — *checking
unavailable* — plus `console.error`. It does not render nothing.

This repo has been burned three recorded times by the opposite choice
(fire-and-forget 403s swallowed, legacy-gateway 403 shown as an empty state,
1078 uploads with zero log lines). With the flag on and the route broken 100 %
of the time, "render nothing" is indistinguishable from working-and-empty. The
answer still must not acquire an error banner — the muted line lives inside the
corroboration block only.

The `checking…` → resolved/failed transition is specified: the placeholder is
replaced, never left hanging.

### 6.4 Mounts — four, not two

```
scripts/pages/timeline.js:2377
scripts/pages/timeline.js:3653
scripts/pages/timeline.js:3692
scripts/composites/search-palette.js:490
```

`timeline.js:865`'s own comment says AskChat is mounted in three places on that
page. The first draft of this spec said two, citing `timeline.js:2092` — which
is a variable read, not a mount — **while quoting the CLAUDE.md warning about a
feature wired to the wrong one of three mounts.** Because the block renders
inside `AskChat`, all four are covered by construction; F5 still opens each and
reads the DOM.

### 6.5 Plumbing

- The request goes through the api layer, not a raw `fetch` — `ask-chat.js:190`
  calls `window.FS.api.ask.ask`. A sibling wrapper is a required sub-task.
- Styling uses `fs-ask-chat__*` CSS classes and semantic tokens. **Not**
  `t.surface.X` from `fs-globals.js` — those are baked light-mode hex and
  silently break dark theme (frontend CLAUDE.md:211-231). `ask-chat.js` already
  does this correctly; follow it.
- Flag `FS.api.externalCorroboration`, injected via `amplify.yml` → `/env.js`,
  default false, following the `threadReview` precedent.
  `update-branch --environment-variables` is a whole-package replace — resend
  every `FS_*`.

---

## 7. Verification

- **The privacy gate is tested by trying to defeat it.** A price beside a
  company name → dropped with `reason: commercial_context`. A person-shaped
  entity labelled `company` by the extractor → dropped by the string-shape
  check. Then delete each rule and confirm the test goes red — a gate that
  passes because the input never reached it is not a gate
  (memory `ci-green-over-a-dead-path`).
- **A test pins the search step's prompt contents** — entities only, no answer,
  no transcript.
- **Entity extraction must not ASCII-normalise.** This codebase has erased CJK
  three times with `[^a-z0-9]`-shaped normalisation. A Chinese company name is a
  test case: non-empty, and distinct from a different Chinese name.
- **`conflicts` and `no_checkable_claim` each have a fixture.**
- **The switch is verified by reading the deployed env**, then by reverting one
  workflow line and confirming red.
- **The frontend is verified at all four mounts by opening the DOM**, not by
  grep.

---

## 8. Cost

Per corroborated question: one extraction call, one web-search call, one
reconcile call. Only on questions the user asks, only when the flag is on,
capped at 3 entities. `max_uses` on the web-search tool is the natural
per-request cap and is set explicitly.

If a per-user daily cap is ever needed, it is logged when it bites.

---

## 9. What this spec still assumes

Stated plainly so the implementer can check rather than inherit:

- **12 s for a multi-entity web-search call is an estimate, not a measurement.**
  Everything else in §3 was read out of a file; this was not. First
  implementation task after the client exists is to measure it and bring the
  number back — if it is 20 s, the shape changes.
- **`no_checkable_claim` will be the most common state.** If it isn't, the
  extraction step is inventing claims and that is a defect, not a surprise.

---

## 10. Open decisions — these need the user

1. **Is external lookup contractually acceptable for the pilot customers?**
   Blocking for switching the flag on; not for building behind it. If the answer
   differs per customer this becomes a per-company setting rather than a
   per-stack flag — **a different shape, worth knowing before the flag is
   written.**
2. **Search provider.** Recommendation: the Anthropic web-search server tool.
   The key is already on the function and the alternative adds a vendor and a
   secret. Note this pins the feature to Anthropic on every stack even though
   TEST's `LlmProvider` is qwen (§3.3) — deliberate, so tests mean something.
3. **What `findings` is supposed to be.** The user's description — produced only
   on topic-internal uncertainty, or as search-time verification — does not
   match the 189 rows in the table (`progress` 117, `quality` 47, `safety` 25,
   with severities). Either the table is misnamed relative to intent, or
   extraction produces rows outside the intended rule. **This spec touches
   nothing there and depends on no answer**, but P1 cannot be designed until it
   is settled.
4. **P1's timing.** An uncertainty raised in a meeting may resolve hours later.
   Supersede, append, or notify? Same supersede-chain problem already recorded
   as a VizField differentiator; solve it once.

---

## 11. What the first draft got wrong

Kept because the pattern repeats, not for the record's sake. Every one of these
was one layer below where the draft stopped reading — it verified the repo's
*comments* and not its *call graph*.

| | claim | reality |
|---|---|---|
| B-1 | route dispatched in `lambda_ask_agent.lambda_handler` | that function has no paths; `AskAgentFunction` has no API Gateway Events at all |
| B-2 | frontend echoes `caller_sub` back | identity is injected server-side by the proxy, and `/ask` never returns it |
| B-3 | `llm_utils` already calls Anthropic, so no new vendor | TEST runs qwen; and the anthropic branch supports no `tools` |
| B-4 | a monotonic deadline bounds each step | `call_llm` takes no timeout, blocks up to 45 s × 4 — longer than the caller |
| B-5 | one call, and partial results per entity | mutually exclusive |
| B-6 | AskChat is mounted twice | four times — while quoting the warning about miscounting mounts |

---

## 12. Task split

### Backend

| # | task |
|---|---|
| B1 | `/api/ask/corroborate` route + `corroborate_answer` bridge in **`lambda_fieldsight_api.py`**, reusing `ask_question`'s `FunctionError` guard |
| B2 | `mode == 'corroborate'` branch in `lambda_ask_agent.lambda_handler` |
| B3 | **The dedicated client** (§5.4): explicit timeout, ≤1 retry, Anthropic-only, `tools` + `web_search_result` parsing |
| B4 | Entity + claim extraction, with the CJK test |
| ~~B5~~ | **DONE — PR #656**, `src/corroboration_gate.py`. `screen(entities)` takes only the entities, **not** the answer: the sentence an entity sits in is never sent, so screening on it would drop legitimate lookups against a leak that cannot happen. Do not add an `answer` parameter. |
| B6 | Web-search step; measure the real latency and report it (§9) |
| B7 | Reconcile → the four-state enum, with `conflicts` and `no_checkable_claim` fixtures |
| B8 | Budget accounting against `ApiFunction`'s 30 s; `truncated` and `timed_out` set independently |
| B9 | `ENABLE_EXTERNAL_CORROBORATION` in `template.yaml` + both workflows; wiring test + revert-check; expect a rebase conflict (§5.5) |
| B10 | Confirm no regression in the `/ask` response shape |

### Frontend

| # | task |
|---|---|
| F1 | `FS.api.externalCorroboration` flag + `amplify.yml` (resend all `FS_*`) |
| F2 | `FS.api.ask.corroborate` wrapper beside the existing `ask` |
| F3 | `renderCorroboration()` in `ask-chat.js`, separated per §6.1, English copy |
| F4 | Four states rendered distinctly; `no_checkable_claim` never reads as a tick; `truncated` line |
| F5 | Deferred second request; answer never blocked; **failure renders a muted line, not nothing** (§6.3) |
| F6 | Verify at **all four** mounts by opening the DOM |
| F7 | Dark theme via CSS classes and semantic tokens only |

### Order

F1–F4 can be built against a hand-written fixture before B1 exists. F5 needs the
route. Nothing ships to a customer until §10 decision 1 has an answer.

---

## 11. As built (2026-09-01)

The backend shipped inert on `develop` in three PRs: #656 (B5, the gate), #658
(B3, the client), #659 (B1/B2/B4/B6/B7/B9/B10). `ENABLE_EXTERNAL_CORROBORATION`
is `false` in `template.yaml` and in both deploy workflows, so §10 decision 1 is
still the only thing standing between this and a customer seeing it.

Four things the design did not anticipate, each found by running the code rather
than by reading it:

**`output_config.effort` is a 400 on Haiku 4.5**, and §5.3 puts haiku on steps 1
and 4. A call site that simply left the client's default in place would have
produced a well-formed request against a real model that fails only in the
deployed environment. The client now drops `effort` for models that reject it,
rather than leaving each call site to remember.

**A `web_search_tool_result` block carries a list on success and a dict on
error, with HTTP 200 either way.** Iterating the dict yields its keys, the item
filter drops them, and zero results come back with nothing to say why — which
step 4 would report as `not_found`, a claim about the world rather than about
us. `Reply.search_error` exists for that distinction. It was found by mutation
check: the first version of the test asserted only "zero results" and stayed
green with the guard deleted.

**Thinking must stay on and `effort` is what gets turned down.** The obvious way
to protect the 12 s search budget — `thinking: {"type": "disabled"}` — is the one
change that can break the step outright: with thinking off, Opus 5 sometimes
writes a tool call into visible text instead of emitting `server_tool_use`. The
turn succeeds, the search never runs, and the caller reports `not_found`.

**The flag needed a fourth segment, not three.** `test_template_workflow_parameter_wiring`
already in the repo failed until both workflows passed the parameter: without the
`--parameter-overrides` line the parameter could only ever hold its template
default, and the documented "set a repo variable and redeploy" rollback would
have been a rollback that did nothing.

§9's latency question is unchanged and unanswerable from here — it needs TEST
traffic with the flag on, which needs decision 1.

---

## 12. §9 answered: the budget in §5.3 was wrong (2026-09-01)

Measured against the real API on synthetic content -- a question and an answer
written for the purpose, about companies and standards whose existence is
public. No recording, transcript or extraction was read.

**The design's model choice could not finish.** Opus 5 with the dynamic-filtering
search tool takes **17 s for a single entity** against a 12 s budget: three runs,
three timeouts, `timed_out: true` every time. `max_uses: 1` did not help (19.9 s)
-- what costs the time is the model, not the number of searches. The same model
with **no tool at all** still takes 9.3 s. Sonnet 5 is slower still (19.7 s).

| configuration | latency |
|---|---|
| Opus 5 + `web_search_20260209` | 16.9 s / 17.2 s |
| Sonnet 5 + `web_search_20260209` | 19.7 s |
| Opus 5, no tool | 9.3 s |
| Sonnet 5 + `web_search_20250305` | 9.7 s |
| **Haiku 4.5 + `web_search_20250305`** | **4.2 s / 4.7 s** |

So all three steps run haiku with the basic search tool. End to end on five
cases: **6.3-7.5 s, mean 5.6 s**, against a 24 s hard stop. The cost is stated
rather than hidden: the basic tool does no dynamic filtering, so raw results
reach the model's context. That is affordable at one to three entities and would
not be at thirty.

**Two findings the latency work surfaced, which latency was not looking for:**

*A verdict with no reason is not a card.* Haiku returned `conflicts` with an
**empty summary** on a claim that was in fact correct. "The web disagrees" with
nothing after it is an assertion the reader can neither check nor dismiss, so
`corroborated` and `conflicts` now require a summary and are dropped without one.
`not_found` and `no_checkable_claim` survive an empty summary -- those two are
complete statements on their own.

*The gate held against the case it was written for.* Given the answer *"We agreed
with Naylor Love that the variation would be priced at forty thousand dollars
before the claim goes in, and John Smith will sign it off"*, the only entity
extraction proposed was `Naylor Love`, the gate refused it as person-shaped, and
the run ended in 1.03 s having made **no search call at all**. No price, no
variation, no claim, no person's name left the account.

**Still open, and not answerable from here:** how often haiku assigns
`corroborated` to something it should not. The one false positive seen (a correct
claim initially called `conflicts`) went the safe direction; the dangerous
direction is the opposite one, and measuring it needs traffic, which needs
decision 1.

---

## 13. §5.2 measured, and the precision question answered (2026-09-01)

§5.2 left this to "the first week of TEST data". **That was the wrong
instrument.** Real traffic has no ground truth: an answer that comes back
`corroborated` in production looks identical whether the web agreed or the model
merely thought it did, and nobody is going to hand-adjudicate a week of them.
Precision is only measurable against cases where the answer is already known,
which means writing them.

`scripts/measure_corroboration_precision.py` is 16 labelled cases -- four true
checkable claims, four false ones, four claims that merely name a real entity
while asserting something only the customer's own records could settle, and four
that should leave nothing for the gate to pass -- run three times each.
Read-only, synthetic content, no customer data.

**The error that matters never occurred.** Across 144 runs of both prompt
versions, a false claim was reported as `corroborated` **zero times**; all four
false-claim cases came back `conflicts` on every run. That is the failure §1
exists to prevent, and it is the one the summary counts separately: a run with a
clean overall score and one such error is a failing run.

**One systematic error did occur, and the list-form prompt caused it.** Claims
about the customer's own job -- *"Naylor Love said the slab pour would move to
Thursday"* -- came back `not_found` 6 times out of 12. Not dangerous, but not
harmless either: "we searched and found nothing" about a sentence the web could
never have held suggests the web declined to back it up.

The cause was the prompt's shape, not the model's judgement. Four states in a
list invites one pass over all of them with the findings already in view, and the
findings sat right there saying *the sources do not address this* -- so the model
answered the question they suggested. The fix asks **checkability first, as its
own question, with an explicit instruction not to read the findings until it is
answered**.

| | list form | two-question form |
|---|---|---|
| accepted | 40/48 | **48/48**, twice (96/96) |
| claims about the customer's own job | 5/12 | **12/12** |
| false claim called `corroborated` | 0 | 0 |
| latency, mean | 6.1 s | 6.1 s |

**A false start worth recording.** The first attempt at this comparison
reported an improvement that had not happened: the edit's anchor did not match,
the assertion fired, and the run went ahead against the *old* prompt. 41/48
versus 40/48 was noise between two runs of the same prompt. The number that
matters is not the score, it is whether the thing you measured is the thing you
changed.

**Not a CI test.** It needs a key, a network and money, so it is an instrument
to be run deliberately when the prompts or the model change -- not a guard that
will notice on its own. The unit tests pin the module's refusals; this pins its
judgement, and only when someone runs it.

---

## 14. §4.1 measured on real recordings (2026-09-01)

Every test of the gate so far used sentences written for the test. §4.1 says the
gate's input is model-assigned and that this is the residual risk -- and a
residual risk measured only against invented sentences has not been measured.

`scripts/measure_gate_on_real_answers.py` runs **step 1 and step 2 only** against
40 real prod extractions and prints the exact strings that would have reached a
search engine. It cannot call out: the search function is replaced with one that
raises, so a script that reads real customer content is structurally unable to
send any of it. That is enforced rather than promised.

**On privacy, the gate held.** Across 40 real sessions: no person's full name, no
price, no commercial term, no transcript fragment. 31 of the 40 produced no
external entity at all, which matches §5.2's expectation that most answers assert
nothing externally checkable.

**On usefulness it did not, and the two point the same way.** Of the 22 strings
that would have left, three were worth looking up. The rest were the customer's
own job codes (`UCPK`, `Raven`), our own product name, and brands nobody needs
corroborated -- `Microsoft`, `iPad`, `UberEats`, `Newstalk ZB`, `Wi-Fi`. Each one
spends a slot out of three: **the cap bit on 3 of the 11 sessions that produced
any entity**, so the useless strings were crowding out the useful ones *and*
leaving the account for nothing.

| | strings out | distinct | cap bit |
|---|---|---|---|
| as designed | 22 | 20 | 3 |
| + extraction asked to skip them | 18 | 16 | 2 |
| + `_NOT_WORTH_LOOKING_UP` in the gate | 7 | 7 | 0 |
| + the one-word person rule | **4** | **4** | **0** |

The four survivors are `University of Otago`, `Platform Construction Limited`,
`VXT`, `DB` -- which is the set a reader would have wanted.

### Three findings, each from the measurement rather than from review

**A prose instruction is not a filter.** Asking extraction to leave ubiquitous
brands out took 22 to 18 and was partly ignored: `McDonald's`, `iPad` and
`Outlook` still came through, and `AWS` newly appeared. The reliable place is the
gate, as code, unit-tested -- the same argument §4 already makes about privacy,
now applying to noise.

**The person rule had a hole at one word, and it was pointing backwards.** It
required two capitalised words, so `Naylor Love` was refused while **`Heidi`
walked straight through** on real data. One capitalised word is *more* ambiguous
than two: nothing in `Heidi`, `Raven` and `Tenix` says which is a person, which is
a job code and which is a firm. The bound is now one, and the cost -- one-word
firms with no corporate marker are refused -- is the trade already accepted for
`Naylor Love`, in the same direction. Acronyms keep passing: `VXT` and `DB` have
no lowercase run, so they were never this shape.

**Extraction was building the claim out of the question.** Asked *"who supplied
the plasterboard"* with an answer stating the firm is a large materials
manufacturer, it returned `claim: "supplied the plasterboard"` -- the question's
predicate, which is a fact about the customer's job, so step 4 correctly answered
`no_checkable_claim` and no card appeared. In production that would have made the
feature useless for exactly the case it exists for: any *"who did the X"* question
would produce a claim about our own job and never a card. Caught because the
precision set dropped from 48/48 to 45/48 after an unrelated change, and the
regression was investigated rather than written off as variance.

**A project code still cannot be told from a short firm name.** `Raven` is now
refused, but by the person rule and not because anything identified it as a job
code -- and a customer's job codes are that customer's, so they cannot be
enumerated in a shared gate. What can leave in that case is a word with no meaning
outside the account. Stated, not denied.
