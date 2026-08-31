# External corroboration for Ask — design

**Date:** 2026-08-31
**Status:** design, awaiting review
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

An unverified answer is obviously unverified. A block labelled 核实 that
contains a plausible fabrication is worse than nothing, because it spends
trust the product cannot re-earn — and the audience is a low-digital-literacy
site user who has no cheap way to check. Every design decision below that
looks over-cautious traces to this sentence.

---

## 2. The two paths, and which one v1 builds

The user described two triggers:

| | trigger | v1 |
|---|---|---|
| **P2** | 用户搜索时 — answer + contextual external verification below | **build** |
| **P1** | topic 内出现疑惑/不确定性 → 进一步搜索排查 | **defer** |

**v1 is P2 only.** Three reasons, in order of weight:

1. **P2 is user-initiated.** The user typed the question, so that question
   going outward is a consequence they can see. P1 fires unprompted on every
   meeting and sends conversation-derived queries to a third party **with
   nobody in the loop**. That is a privacy decision, not an implementation
   detail (§4).
2. **P1 has nowhere to render.** Its natural home would be a per-topic
   surface, and the one that exists — the `findings` table — holds something
   else entirely (§9, open decision 3).
3. P2 is bounded and synchronous-ish; P1 needs a worker, a queue, and a
   supersede rule for when the answer arrives after the meeting is over.

P1 is not cancelled. It is sequenced behind P2 shipping and the privacy
decision landing.

---

## 3. Measured constraints that shape the design

Every number here was read out of the repo or the account, not assumed.

**API Gateway caps the ask round-trip at 29 seconds.** `template.yaml` says so
in its own comment on `CLAUDE_MODEL`, and that is *why* ask runs haiku rather
than sonnet:

> the user-facing ask round-trip is capped by APIGW's hard 29s integration
> timeout on ApiFunction (Timeout: 30 above) — and RAG synthesis adds two
> extra hops (embed call + rag-search invoke) on top of the Claude call itself.

An external search plus a second LLM pass does not fit inside the request that
already spends its budget on embed + rag-search + synthesis. **Corroboration
therefore cannot ride along on `/ask`.** It is a second request.

That constraint pushes the design onto a shape the user already decided on
independently (memory `vizfield-two-pass-answers-first`): **快版秒出，第二遍推敲.**
The fast grounded answer renders immediately; corroboration arrives after and
fills in below it. The architecture and the product principle agree, which is
the only reason to trust either.

**`AskAgentFunction` has no `VpcConfig`.** It sits outside the VPC and has
ordinary internet egress. No S3-request-file hop is needed (BUG-36 governs the
*in*-VPC direction; this function is on the free side). `RagSearchFunction`,
which it invokes, *is* in-VPC — that hop stays as-is.

**It already holds `ANTHROPIC_API_KEY` and calls `api.anthropic.com` directly**
(`llm_utils.py:145`). No new vendor and no new secret is required if the search
is done through the Anthropic server-side web-search tool (§5, step 3).

---

## 4. The privacy red line

**Only entities leave. Never conversation text.**

This is the load-bearing rule of the whole feature. Construction meeting
content is commercially confidential — a claim dispute, a subcontractor's
pricing, a defect allocation. Sending any of it to a third-party search engine
is a contractual problem regardless of how good the answer is.

Concretely, what may be sent:

| allowed to leave | never leaves |
|---|---|
| a company name (`Naylor Love`) | anything about *our* dealings with it |
| a standard or code (`NZS 3604`, `AS/NZS 1170`) | prices, quantities, claim values |
| a product or material name | client names paired with commercial terms |
| a public role noun (`CEO`, `managing director`) | worker names, any person not in a public role |
| a regulator or authority name | site names, addresses, dates of our meetings |

Two implementation rules follow, and both matter:

- **The gate is deterministic code, not the model's judgment.** An LLM asked
  "is this safe to send?" will say yes under pressure from a plausible-sounding
  question. The allowlist of entity *kinds* and the denylist of co-occurring
  commercial terms are plain Python, unit-tested, and run *after* the
  extraction step regardless of what the extraction returned.
- **A query is built from the entity, not quoted from the transcript.**
  `"Naylor Love" CEO` — assembled from fields. Never a substring of what
  anyone said.

### The customer-facing half of this

Whether external lookup is acceptable **at all** under the pilot customers'
terms is a decision for the user, not for this spec (§9, open decision 1).
The flag defaults off; the code can be built and tested before that answer
exists, and must not be switched on for a customer before it does.

---

## 5. Backend design

### 5.1 New route

`POST /api/ask/corroborate` on the existing `AskAgentFunction`, dispatched in
`lambda_handler` alongside `/ask` and `/search`.

**Request** — the frontend echoes back what it received from `/ask`:

```json
{ "question": "…", "answer": "…", "caller_sub": "…" }
```

Note what is *absent*: the client does not send a list of claims or entities.
**The backend re-derives them.** A client-supplied entity list is a
client-supplied egress instruction, and the privacy gate would be checking the
caller's homework instead of the model's.

**Response:**

```json
{
  "corroborations": [
    { "entity": "Naylor Love", "kind": "company",
      "state": "corroborated",
      "summary": "…",
      "sources": [ {"title": "…", "url": "…", "published": "2026-03-11"} ],
      "retrieved_at": "2026-08-31T09:12:04Z" }
  ],
  "skipped": [ {"entity": "…", "reason": "commercial_context"} ],
  "partial": false
}
```

### 5.2 The three states

`state` is an enum, never a boolean, and never absent:

| state | meaning | why it must be distinct |
|---|---|---|
| `corroborated` | an external source agrees | the happy path |
| `not_found` | searched, nothing usable came back | **must be shown, not swallowed.** Silence reads as "fine" |
| `conflicts` | the web says something different from the recording | **the most valuable state and the easiest to lose.** A naive implementation renders it as just another summary and the contradiction never reaches the user |

`conflicts` is the whole reason a person would want this feature. If the
implementation cannot produce it, the feature is a search box with extra steps.
A test asserts a synthetic conflict surfaces as `conflicts` and not
`corroborated`.

### 5.3 Steps, with a time budget

The corroborate call is also behind API Gateway, so it also has ~29s. Budget:

| step | what | budget |
|---|---|---|
| 1 | **entity extraction** — haiku, `force_json`, returns `[{entity, kind, span}]` | ~2s |
| 2 | **privacy gate** — deterministic filter; cap at 3 entities | <10ms |
| 3 | **external lookup** — Anthropic web-search server tool, one call covering the surviving entities | ~10–15s |
| 4 | **reconcile** — compare against the answer, assign a `state` per entity | ~4s |

**Partial results are returned; the call never fails all-or-nothing.**
If step 3 times out with one entity done and two pending, return the one and
set `partial: true`. A per-step deadline is checked against a monotonic clock
started at handler entry, not a fixed `sleep`-shaped timeout.

**Cap at 3 entities.** Not for cost — for the reader. Six corroboration cards
under one answer is a wall, and the whole point is a low-literacy audience.

### 5.4 The switch

`ENABLE_EXTERNAL_CORROBORATION`, default `'false'`, passed by **both**
workflows (`deploy.yml` with `TEST_…`, `deploy-prod.yml` with `PROD_…`), read
at call time rather than import time.

The repo has a specific trap here — an env declared in the template but not
threaded through a workflow yields the default silently, with no error
anywhere (memory `fieldsight-unwired-toggle-trap`). The verification is not
"the test passes"; it is **read the deployed function's env and confirm the key
is present**, then flip a workflow default and confirm a test turns red.

### 5.5 What is deliberately NOT touched

- `_rag_answer` and the `/ask` response shape — unchanged. A frontend that
  never calls the new route sees exactly today's behaviour.
- `RagSearchFunction` — unchanged, stays in-VPC.
- `findings` — unrelated to this feature despite the name collision (§9).

### 5.6 Coordination with the other in-flight ask spec

`spec/ask-answers-with-numbers` (branch `spec/ask-answers-with-numbers`,
`docs/superpowers/specs/2026-08-31-ask-answers-with-numbers-design.md`) also
targets `lambda_ask_agent.py`. As of this writing it is **spec-only, no code**.

The two do not overlap in function: that one shapes the grounded answer, this
one appends a separate block after it. But both will edit `lambda_handler`'s
dispatch. **Whichever lands second rebases**; neither should assume the other's
line numbers.

---

## 6. Frontend design

All of it lives in `scripts/composites/ask-chat.js` (314 lines today).

### 6.1 The one thing that matters

**Grounded and external must never look like the same kind of statement.**

The existing answer is *from the customer's own recordings* — it is evidence
about their site. The corroboration block is *from the open web* — it is
evidence about the world. Rendering them in the same visual register invites
the reader to trust the second as much as the first, and the second is exactly
the one that can be wrong in ways they cannot detect.

Separation is carried by all of: a labelled divider (`来自公开网络 · 不是你的录音`),
a different surface token, the source domain shown inline on every claim, and
the retrieval date. Not by colour alone.

### 6.2 Shape

- `renderCorroboration(corroborations, skipped, partial)` — a sibling of the
  existing `renderCitations`, rendered **after** it.
- The second request fires once the answer has rendered; a quiet inline
  `正在核实…` placeholder holds the space. It never blocks the answer.
- A failed corroborate request renders nothing at all. **The answer must not
  acquire an error banner because an optional enrichment failed.**
- `not_found` renders as a plain line — `没有找到可靠的外部来源` — not as an
  absence.
- `conflicts` gets the strongest treatment on the block: the recording said X,
  the web says Y, both attributed.

### 6.3 Mounts

`AskChat` is mounted from `scripts/pages/timeline.js:2092` and inline in
`scripts/composites/search-palette.js:490`. Putting the block inside `AskChat`
covers both.

⚠️ CLAUDE.md records a real defect where a feature was wired to one of
**three** `AskChat` mounts and the route under test rendered a different one.
Before claiming done: enumerate every mount, open each, confirm the block
renders. Not grep — the DOM.

### 6.4 Flag

`FS.api.externalCorroboration`, injected via `amplify.yml` →
`/env.js` → `window.FS_ENV`, defaulting **false**, following the
`threadReview` precedent exactly. Note `update-branch --environment-variables`
is a whole-package replace — all existing `FS_*` vars must be resent.

---

## 7. How this gets verified

The repo's rule is that a green suite is evidence about the code you wrote, not
the code that runs. Specific to this feature:

- **The privacy gate is tested by trying to defeat it.** Feed it an answer that
  embeds a price next to a company name and assert the entity is skipped with
  `reason: commercial_context`. Then delete the rule and confirm the test goes
  red — a gate that passes because the input never reached it is not a gate
  (memory `ci-green-over-a-dead-path`).
- **Entity extraction must not ASCII-normalise.** This codebase has erased CJK
  three times with `[^a-z0-9]`-shaped normalisation. A Chinese company name is
  a test case, and it asserts the extracted string is non-empty and distinct
  from another Chinese name.
- **`conflicts` has its own test**, with a fixture where the recording and the
  synthetic search result disagree.
- **The switch is verified by reading the deployed env**, then by reverting one
  workflow line and confirming red.
- **Frontend is verified by opening both mounts and looking at the DOM.**

---

## 8. What this will cost

Per corroborated question: one haiku extraction call, one web-search-enabled
call, one reconcile call. Only on questions the user asks, only when the flag
is on, capped at 3 entities.

It is not free and it is not on the hot path. If it ever needs a cap, the cap
belongs on *questions per user per day*, logged when it bites — a silent cap
reads as "the feature is broken" (memory: no silent caps).

---

## 9. Open decisions — these need the user

1. **Is external lookup contractually acceptable for the pilot customers?**
   Blocking for switching the flag on; not blocking for building behind it.
   The answer may differ per customer, which would make this a per-company
   setting rather than a per-stack flag — worth knowing before the flag is
   written, because a per-company setting is a different shape.

2. **Search provider.** Recommendation: the Anthropic server-side web-search
   tool, because the key, the client, and the retry/backoff already exist in
   `llm_utils`. Alternatives (Brave, Tavily, Google PSE) each add a vendor, a
   secret, and a second failure mode for no gain that is visible from here.

3. **What `findings` is supposed to be.** The user's description of a "finding"
   — produced only on topic-internal uncertainty, or as search-time external
   verification — does not match the 189 rows in the `findings` table, which
   are per-topic observations across `progress` (117), `quality` (47) and
   `safety` (25) with severities. Either the table is misnamed relative to the
   intent, or extraction is producing rows outside the intended rule. **This
   spec does not touch `findings`** and does not depend on the answer, but P1
   cannot be designed until it is settled.

4. **P1's timing.** Once P1 exists, an uncertainty raised in a meeting may be
   resolved minutes or hours later. Does the resolution supersede the original,
   append to it, or notify? This is the same supersede-chain problem already
   recorded as a VizField differentiator, and it should be solved once.

---

## 10. Task split

### Backend — for the session that takes it

| # | task |
|---|---|
| B1 | `POST /api/ask/corroborate` route + dispatch in `lambda_ask_agent.lambda_handler` |
| B2 | Entity extraction (haiku, `force_json`), with the CJK test |
| B3 | **Privacy gate** — deterministic allowlist/denylist, cap 3, with the defeat-it test and the revert-check |
| B4 | External lookup via the Anthropic web-search tool |
| B5 | Reconcile → the three-state enum, with the `conflicts` fixture |
| B6 | Per-step deadline against a monotonic clock; `partial: true` on incomplete |
| B7 | `ENABLE_EXTERNAL_CORROBORATION` in `template.yaml` + **both** workflows; wiring test + revert-check |
| B8 | Confirm no regression in the `/ask` response shape |

### Frontend — this session

| # | task |
|---|---|
| F1 | `FS.api.externalCorroboration` flag + `amplify.yml` (resend all `FS_*`) |
| F2 | `renderCorroboration()` in `ask-chat.js`, visually separated per §6.1 |
| F3 | Deferred second request; answer never blocked, failure renders nothing |
| F4 | The three states rendered distinctly; `not_found` visible, `conflicts` strongest |
| F5 | Verify at **every** `AskChat` mount by opening the DOM |
| F6 | Dark theme — both grounds, per the token rules in CLAUDE.md |

### Order

F1–F2 can start against a hand-written fixture before B1 exists. F3 needs the
route. Nothing ships to a customer until decision 1 in §9 has an answer.
