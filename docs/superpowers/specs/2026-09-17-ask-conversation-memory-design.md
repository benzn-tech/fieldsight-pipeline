# Ask remembers the turn before it

**Date:** 2026-09-17
**Status:** design — awaiting review
**Scope:** backend (`lambda_fieldsight_api`, `lambda_ask_agent`, one new pure
module), web (`fieldsight-ui`), device (`GrandTime`). No schema change, no
change to `lambda_rag_search`.
**Supersedes nothing.** Continues PR #833 (step 2 of the continuity work:
forward the turns, retrieve with nothing).

---

## 0. What is true today, measured

A follow-up question cannot work, and the reason is not that continuity was
built badly. **It was never built at all past the first hop.** Verified by
reading each side rather than by reading the PR that describes them:

| Where | What it sends / reads today |
|---|---|
| `fieldsight-ui` `ask-chat.js:483` `requestBodyFor()` | `question`, `date`, `site_id`, `author_folder`, `topic_row_id`, `scoped`. **No history.** |
| `fieldsight-ui` `ask-chat.js:696` | Keeps a chat log, and clears it when the scope changes. Used for rendering only; never posted. |
| `GrandTime` `AskApiClient.kt:35` | `audio`, `format`, `mode`. **No history, no `tz`.** |
| `lambda_fieldsight_api.py:1213` `ask_question` | Proxies the screen body. |
| `lambda_fieldsight_api.py:1401` `ask_voice` | **Already forwards `tz` AND `history`.** `_clean_voice_history` (`:1366`) keeps `{question, answer}` turns, caps at `MAX_VOICE_HISTORY_TURNS = 6` / `MAX_VOICE_HISTORY_CHARS = 2000`, drops bad turns individually and WARNs how many. Pinned by `tests/unit/test_ask_voice_forwards_history.py`. |
| `lambda_ask_agent.py:1172` `_rag_answer` | Reads `question`, `caller_sub`, `k`, `tz`, `now`, scope fields. **`history` is not among them.** |
| `lambda_ask_agent.py:1644` (voice) | Reads `body["history"]`, computes `history_turns`, **logs it and discards it.** |

So `history_turns` has been 0 on every real call since #833 shipped, because
the only client that could send it does not. That is exactly what the counter
was added to reveal, and it revealed it.

**Retrieval is the hard half, not the prompt.** `_rag_answer` embeds the
question text alone (`lambda_ask_agent.py:1268`) and `query_slots.time_range()`
parses a calendar range out of that same text. A follow-up such as *"when is he
finishing it?"* carries no noun to embed and no date to parse, so the vector
lands nowhere in particular and the range comes back `(None, None)` — which
means *search everything*. Putting the previous turn into the answering prompt
would not fix this: the model would understand the question and still be handed
the wrong excerpts.

**Single-turn time anchoring is already solved and is not in scope.**
`query_slots.py` resolves `昨天 / yesterday / 前天 / 上周 / last week / 最近 N 天`
against the caller's own zone, and `ask.js` has sent `Intl.DateTimeFormat()
.resolvedOptions().timeZone` since 2026-08-30. A live prod answer on 2026-09-17
resolved "yesterday" to 16 Sept correctly. Two gaps remain and only these two:
a follow-up whose time word lives in the *previous* turn, and the **device**,
which sends no `tz` at all.

**Retrieval is airtight about deletion; nothing else is.**
`repositories/search_sql.build_search_sql()` applies
`visible_chunks_predicate("c")` inside the one query that both Search and Ask
run, and `lambda_rag_search` returns those rows unchanged. A deleted recording
cannot come back through retrieval. It can come back through a copy taken
before the deletion — which is what a chat history is.

**Every chunk already carries its cosine distance.** `build_search_sql()`
selects `c.embedding <=> %(q)s::vector AS distance`, and
`lambda_rag_search.py:391` returns the rows as `chunks`. `_aggregate_topics`
already reads `c.get("distance")` and already compares it against
`_NO_LEX_MAX_DIST = 0.55` (`lambda_ask_agent.py:745`, applied at `:865`). That
number was measured by the retrieval ruler and **must not be relaxed**;
relaxing it to 0.65 made every control question return 18–26 rows.

---

## 1. Decisions (user, 2026-09-17)

1. **History reaches the rewrite step only. It never reaches the answering
   prompt.**
2. **A distance gate runs before the web-answer verdict**, so the verdict model
   call is not spent on questions the records obviously cannot answer. Its
   threshold is **not yet measured** for this use (§4.4), so it may only save a
   model call — it may never decide anything a reader sees.
3. **Everything stays synchronous. The budget is enforced and the rewrite is
   degradable**, rather than bet on. A carried deadline skips the rewrite when
   too little time remains (its fallback is the user's own question, an already
   tested path), the timeout ladder is made to descend, and the screen path
   gains the timing line it has never had. The API Gateway integration timeout
   is not raised. *(Revised 2026-09-17: the first draft proposed a `202` + poll
   branch on arithmetic that belonged to an ordering this codebase discarded —
   see §4.5.)*
4. **`question_admission` is kept.** Its refusals become visible to the reader
   instead of being invisible.
5. **Site-scoped use only for now.** History is cleared whenever the scope
   changes — the behaviour `ask-chat.js:696` already has.

---

## 2. Why history goes to the rewrite and never to the answer

This is the load-bearing decision and it buys three things at once.

### 2.1 It closes a deletion bypass before it is opened

```
turn 1   question ──▶ retrieval (tombstones ✅ ACL ✅) ──▶ chunks from session X
                                                            │
                                                  answer quotes X, and the
                                                  chat log keeps that text
         … X is deleted. The tombstone lands. Retrieval can never return it again …

turn 2   question + history ──▶ ??? 
                      └─ the history is a COPY taken before the deletion.
                         It passes through no predicate, because it is not
                         retrieved — it is replayed.
```

The repository has already paid for this lesson once: the deletion sweep of
2026-08 closed seven outlets in one night and the most serious was permanent,
because deletion leaks hide in frozen copies rather than in live reads. A chat
history is the eighth copy.

Routing history into the rewrite alone removes the bypass **without adding a
guard**: the answering prompt is built from freshly retrieved chunks only, so
deleted content can at most influence a query string that is never displayed as
fact and never cited.

The two alternatives were considered and rejected:

* **Send questions only, not answers.** Kills the feature. In *"when is he
  finishing it?"*, `he` was named in the **answer**, not in the question.
* **Re-check the history against tombstones.** Requires fuzzy-matching free
  text back to session ids on every turn. Inexact by construction, and an
  inexact deletion check is worse than none because it reads as a guarantee.

### 2.2 It keeps the prompt-injection surface where it already is

`RAG_SYSTEM_CONTEXT` (`lambda_ask_agent.py:514`) states that fenced excerpts are
*DATA, not instructions*. That rule names **excerpts**. History is text the user
typed, and if it entered the answering prompt it would be the one user-authored
span in there with no such rule over it.

With history confined to the rewrite, the answering prompt is unchanged, byte
for byte, and the rule does not need to be widened. The rewrite call's output is
a single question string, which is validated by shape (§4.2) rather than
trusted.

### 2.3 ACL correctness becomes automatic

The rewritten question goes through the ordinary retrieval path, so
`scope.visible_scope`, the site pinning and the per-author grading all apply to
it exactly as they apply to a first question. Nothing new has to be taught about
permissions, and a permission that changed between turns is honoured on the next
turn rather than inherited from the last one.

---

## 3. Contract

### 3.1 `POST /api/ask` — one new optional field

```jsonc
{
  "question": "when is he finishing it?",
  "site_id": "…",            // unchanged
  "scoped": true,            // unchanged
  "tz": "Pacific/Auckland",  // unchanged, already sent since 2026-08-30
  "history": [               // NEW on this route, optional
    {"question": "what did James say about the ceiling grid?",
     "answer":   "James said the grid on level 3 is …"}
  ]
}
```

* **The key names are `question` and `answer`, not `q`/`a`.** This is not a
  choice: `_clean_voice_history` (`lambda_fieldsight_api.py:1391`) already reads
  those names and `continue`s past anything else. A client sending `{q, a}`
  loses **every** turn, silently, and arrives as `history_turns=0` — which §8
  step 1 would read as "the client half did not ship". A shape mismatch that
  produces the same observation as an unshipped client is the worst available
  failure.
* **`history` is optional, and absent is not `[]`.** A body with no history
  sends **no key**; a user who cleared their chat also sends **no key**, and the
  difference is carried in the log value, not on the wire. The deployed voice
  proxy states the rule in place (`:1428`): *"ABSENT, never an empty list …
  'I have no history' is a different statement from 'my conversation is empty'
  — collapsing them would make the agent's `history_turns` count mean two
  things."* §4.7 exists to keep that distinction; §3.1 must not undo it.
* **Caps are the ones already deployed: 6 turns, 2000 characters per field.**
  `MAX_VOICE_HISTORY_TURNS` and `MAX_VOICE_HISTORY_CHARS`
  (`lambda_fieldsight_api.py:1362-1363`) carry a recorded rationale — six
  because a follow-up refers to the last question, not to the start of a shift.
  The screen path adopts the same numbers so one field does not have two caps
  depending on which door it came through. **This spec does not change them.**
* Anything else in the list is dropped **per turn**, without an error — a
  malformed history must never fail an Ask that would otherwise succeed — and
  the count of dropped turns is WARNed, as the voice path already does.
* A body carrying no usable history produces a rag-search payload **key-for-key
  identical** to today's. This is the same posture `date` has.

### 3.2 `POST /api/ask/voice` — the same field

Identical shape — **and the backend half of this route is already built.**
`ask_voice` forwards both `tz` and a cleaned `history` today, with caps, drop
logging and a test. What is missing is only the **device**, which sends
`audio`/`format`/`mode` and nothing else.

That asymmetry sets the order in §9: the device's `tz` is a live defect fix
against a field the backend has accepted for some time, and it is not part of
this feature's flag.

### 3.3 Response — one new field

```jsonc
{
  "answer": "…",
  "citations": [ … ],
  "asked": "when is James finishing the level 3 ceiling grid?",   // NEW
  "basis": { … }
}
```

`asked` is the question that was actually retrieved with. It is `null` when no
rewrite happened. **It must be rendered.** A rewrite that silently changes the
question is the same failure mode `query_slots` refused a model for: *a wrong
range is invisible — the answer looks fine and is about the wrong week.* Showing
the rewritten question is what makes that failure visible without a measurement
campaign.

**And rendering it is the one place §2.1's containment is partial.** `asked` is
built from the previous turn, so if the record behind that turn has since been
deleted, the nouns it lifted — a person, a level, a topic title — are re-shown
in the UI even though the answer below is built only from live retrieval. This
is stated rather than hidden: the deleted content cannot be quoted, cited or
answered from, but a fragment of it can appear in the line that says what was
searched for. The mitigation is that `asked` is a single question the reader
just took part in composing, not a passage of recovered content, and it is
**never** sent onward to the web (§4.3). If that residue is judged unacceptable,
the lever is to render `asked` only when the rewrite drew on no history — which
also removes most of its value. §10 carries this as an open question rather
than pretending it is settled.

---

## 4. Backend changes

### 4.1 The four places a field has to be added

`ask_voice` is **done** (see §0). The remaining edits, named individually
because the "add it twice" trap in this file is about the *second* place, and
naming only the gateway would repeat it:

| # | File | Edit |
|---|---|---|
| 1 | `lambda_fieldsight_api.py:1213` `ask_question` | Forward `history` (screen path). Reuse `_clean_voice_history` — one cleaner, one set of caps. |
| 2 | **`lambda_ask_agent.py:1667`** | `_voice_answer` builds `_rag_answer`'s body **from scratch**: `{"question", "caller_sub", "mode", "k", "tz"}`. Add `history` here or the voice path silently has none, however well the gateway forwards it. |
| 3 | **`lambda_ask_agent.py:1702`** | `_voice_answer`'s **return** is hand-built too, so `asked` stops here unless listed. The file records that this already happened once to `basis`. |
| 4 | `lambda_ask_agent.py:1172` `_rag_answer` | Read `history`; §4.3. |

**The trap, in the file's own words** (`lambda_ask_agent.py:1662`): *"`tz` is
listed explicitly because this body is BUILT, not passed through: anything the
screen path gains is absent here until someone adds it twice."* Rows 2 and 3
are that second place. A change that stops at the gateway ships a feature that
works on the web and is inert on the device, with nothing failing.

### 4.2 New module `src/ask_rewrite.py`

Pure, no boto3, no psycopg — same posture as `query_slots.py`.

```python
def standalone_question(question, history, *, call, timeout, clock=time.monotonic):
    """The question, rewritten so it stands on its own, or the original.

    Returns (text, rewritten: bool). NEVER raises, and NEVER returns something
    that is not a question the caller could have typed.
    """
```

Rules, each of which exists because of a named failure already in this
repository:

* **No history → return the original, `rewritten=False`, with no model call.**
  The common case must cost nothing.
* **Any failure returns the original.** A timeout, a non-200, an unparseable
  reply, an empty string: all produce `(question, False)`. `query_slots.py`
  states the principle — *a rule that does not recognise a phrase returns
  nothing, and nothing means "do not filter"* — and the same applies here:
  falling back to the user's own words is always safe.
* **Shape validation before the result is used.** Reject and fall back if the
  reply is longer than 300 characters, contains a newline, or is empty. The
  rewrite's output goes on to be embedded and displayed; it is not free text.
* **The transport is `corroboration_client.call`**, the established cheap lane
  (`web_answer.py` uses it for the verdict). Settled here rather than left to
  the implementer, because the two candidate clients differ in ways that matter
  below. Budget `ASK_REWRITE_BUDGET`, default **4.0 s**.
* **That lane does NOT add reasoning headroom, and this spec's first draft said
  it did.** `llm_utils` adds `max_tokens + REASONING_HEADROOM_TOKENS`
  (`llm_utils.py:447`) with the measurement attached — *"at max_tokens=1200
  this model produced 1197 reasoning tokens and content=''"*.
  `corroboration_client.call` sends `max_tokens` **raw** (`:216`, default 1024).
  Since muse-class reasoning cannot be switched off and reasoning tokens are
  spent before the answer, a rewrite asking for "enough tokens for one question"
  would get HTTP 200 and an empty string — which `standalone_question` would
  correctly treat as a failure and fall back from, so the feature would simply
  never work while nothing failed. **Pass `max_tokens` sized for thinking plus a
  question, not for a question.**
* **The model default is `google/gemini-3.8-flash`, from
  `CORROBORATION_MODEL` (`corroboration_client.py:73`).** Measured
  2026-09-17: neither `CORROBORATION_MODEL` nor `CORROBORATION_CHEAP_MODEL` is
  set on `fieldsight-prod-ask-agent` or `fieldsight-test-ask-agent`, so the
  code default is what runs. (The first draft cited
  `CORROBORATION_CHEAP_MODEL`; that variable exists in no deployed environment.)
* **`timeout` below `MIN_USEFUL_TIMEOUT` (2.0 s) is refused by the client
  itself** (`corroboration_client.py:78`, `:206`). So the deadline rule in §4.5
  must skip the rewrite when fewer than 2.0 s remain, rather than call with a
  sliver and receive an error it would then fall back from — same outcome, one
  wasted round trip.
* **The credential is the lane's own, `CORROBORATION_API_KEY`**
  (`corroboration_client.py:199`), wired from `secrets.OPENROUTER_API_KEY`
  through the `CorroborationApiKey` parameter in both workflows. Measured
  2026-09-17: **set on both stacks.** It is named here because a missing key
  does not raise — it returns `Reply(error=…)`, which this module turns into
  the original question. The feature would be inert and silent, so §8 asserts
  the key is readable rather than assuming it.

Prompt, written so the model cannot editorialise:

```
Rewrite the final question so it can be understood on its own, using the
conversation only to resolve what words like "he", "it", "that" or "then"
refer to.

Change nothing else. Do not answer it. Do not add detail that is not in the
conversation. If the final question already stands on its own, return it
unchanged.

Return only the question, on one line.
```

### 4.3 `lambda_ask_agent._rag_answer` — where the rewrite goes

**This is a restructuring of the function head, not a one-line insert.** The
first draft of this spec said "insert at `:1268`" and drew a diagram with
`time_range` below it. That diagram is unimplementable: `time_range` runs at
**`:1222`**, forty-six lines *above* `:1268`, and above the `try` as well.

The required edit is therefore:

1. Move `resolve_today` / `time_range` / `_validate_scope` / `_scope_range`
   **inside** the `try` block.
2. Put the rewrite immediately above them.

Step 1 is not cosmetic. The code says in place why those calls sit above the
`try` (`:1225`): *"Pure helpers that never raise, so computing them above the
try adds no raw-500 path"*, and `:1243` records that something was there once
and was moved. `standalone_question` makes a network call, so leaving it above
the `try` would reintroduce exactly the raw-500 path those comments exist to
prevent. Moving the pure helpers down with it keeps one ordering and one
guarded region.

```
read body (+ history)
      │
      ▼
ask_rewrite.standalone_question(question, history)      ← NEW
      │  asked = the rewritten text
      ▼
query_slots.time_range(question, today)                 ← the ORIGINAL first;
      │                                                   only if it yields
      │                                                   (None, None) recompute
      │                                                   from `asked`. Never
      │                                                   reopens the metric route.
      │
      ▼
dashscope_utils.embed([asked])[0]                       ← the embed call in
                                                          `_rag_answer`, NOT the
                                                          identical one in
                                                          `_rag_search_list`
      │
      ▼
rag-search invoke                          ← UNCHANGED, no edit to that lambda
      │
      ▼
build_rag_prompt(question, chunks, …)      ← the USER'S question, not `asked`,
                                             and NO history
```

Three details in that diagram are load-bearing:

* **`build_rag_prompt` keeps receiving the user's original wording** and gains
  no `history` parameter at all. The reader asked a question; the answer should
  be to the question they asked, and the rewrite exists to fix *retrieval*.

* **`time_range` reads the rewritten text ONLY when the original yielded
  `(None, None)`.** This rule is narrow on purpose. `q_from` is not just a date
  — it is a router, and three other branches read it:

  * the **metric route** (`:1262`): `metric_slots.detect(question) if (q_from
    and not narrowed)`. A rewrite that introduces a date word would flip an
    undated question onto the counting route — and `detect()` and
    `_metric_answer()` both still receive the **original** text, so the number
    would answer a window the user never named. `:1249` records that this route
    was deliberately restricted to questions that name a time.
  * `_scope_range(scope_req, q_from, q_to)`, whose `has_q` decides `widen`,
    `body_date_sent`, and whether a body `date` is dropped as
    `overridden_by_question`.
  * `scoped = narrowed or plan["body_date_sent"]` (`:1340`), which decides
    whether the web fallback runs at all.

  So a hallucinated date word does not merely shift a range; it silently
  re-routes the request. Hence: **a rewrite may supply a missing time anchor, it
  may never open the metric route.** Compute `q_from, q_to` from the original
  first; only if both are `None` recompute from `asked`; and pass the ORIGINAL
  `question` to `metric_slots.detect` with the original's `q_from`, so a rewrite
  can never turn a non-counting question into a counted one.

* **The web-answer branch is fed the ORIGINAL question, never `asked`.**
  `asked` is assembled from the previous turn, which may quote a record that has
  since been deleted; sending it to `question_admission.screen()` and on to a
  third-party search engine would export exactly what §2.1 exists to contain.
  The screening signals are computed from the retrieved chunks either way.

**Trap, from this repository's own record.** The identical rag-search payload
literal appears in `_rag_search_list` at **`lambda_ask_agent.py:887`** (the
Search list, `k=30`, carries `site`). An edit intended for Ask once landed there
instead; Ask went on behaving as before and **every test stayed green, because
they all drove the helper rather than the route.** The rewrite belongs at
`:1268` and nowhere else, and at least one test must drive `_rag_answer` itself.

### 4.4 The distance gate, before the verdict

`web_answer.answer()` currently spends a model call on **every** question to
decide whether the records answered it, including the majority that they plainly
do. `web_answer.py:33` says so and names the concurrent shape as the way out;
this is a cheaper way out that costs nothing.

```python
# In _rag_answer, after chunks come back and after _rerank_chunks.
nearest = min((c.get("distance") for c in chunks
               if c.get("distance") is not None), default=None)
```

* `nearest is None` → **do not gate.** A missing distance must not be read as a
  far one. `_aggregate_topics` defaults absent distances to `1.0`, which is
  correct for ranking and would be a silent bug here.
* `nearest > _DISTANCE_GATE` **and no chunk is lexical** → the records do not
  answer it; skip the verdict call.
* otherwise → the verdict call runs as it does today.

**The verdict is skipped with a keyword, never by emptying `chunks`.**
`web_answer.answer(question, chunks)` is one function, and `chunks` is also
where `question_admission.screen()` derives two of its three signals from:

```python
corpus = "\n".join((c.get("chunk_text") or "") for c in (chunks or []))
sites  = [str(c.get("site_name") or "") for c in (chunks or []) if c.get("site_name")]
```

So calling `answer(question, [])` to skip the verdict would **also disarm the
site-name and own-records checks**, leaving only the length and commercial-term
rules — sending client and site names to a search engine, which is the exact
outcome §4.6 argues must not happen. The signature gains
`answer(question, chunks, *, skip_verdict=False)` and the chunks are always
passed. A test asserts `screen()` received a non-empty list on the gated path.

**The lexical arm is carried over, and this is why the number is re-derived,
not reused.** In `_aggregate_topics` the 0.55 is applied to a *group* score
after collapsing chunks by (date, site, topic), and always with an escape
hatch: `rows = [r for r in rows if r["lexical"] or r["score"] <= _NO_LEX_MAX_DIST]`.
Dropping that arm would declare a question that literally names a topic's title
unanswerable and route it to the web — precisely the case the arm exists for.
A per-chunk gate is a different measurement from a per-group filter, so
**`_DISTANCE_GATE` is its own constant seeded at 0.55 and is not
`_NO_LEX_MAX_DIST`.** It must be measured before the gate is trusted; until
then it is a cost optimisation whose worst outcome is paying for a verdict call
we could have skipped. `_NO_LEX_MAX_DIST` itself is **not** touched and **not**
relaxed — the ruler measured what relaxing it does.

**Interaction with `basis.widened`.** When retrieval widened to the nearest day
with content, distances are against a day the user did not ask about. The gate
does not run when `basis["widened"]` is true: otherwise an answer can report
"based on 2026-09-16" while the same request is being sent to the open web.
`lambda_rag_search.py:368` warns about this class in place — *"the day this one
gains [a distance threshold], an unconditional flag would start lying with
nothing to catch it."*

### 4.5 The budget is enforced and degradable, not bet on

The first draft of this spec proposed a `202` + poll branch, on the arithmetic
`1.36 + 11.9 + 3.2 + 11.4 = 27.9 s`. **That number belongs to the arrangement
this codebase discarded.** It sits under the heading *"Why the verdict runs
BEFORE the grounded answer"*, and `lambda_ask_agent.py:1386` gives the figures
for the ordering actually deployed: *"answering first and looking up second is
33.2s … while asking first fits either way (21.3s when it looks up, 16.5s when
it does not)."* The async branch was justified by a budget the code does not
have.

**Measured on prod, 2026-09-17, 14-day window:**

| | value |
|---|---|
| LLM call (the dominant term), `qwen done:` | n=**27** · p50 8.1 s · p90 11.4 s · p99 = max = **17.9 s** |
| `fieldsight-prod-ask-agent` whole invocation | p50 2.1 s · p90 9.1 s · p99 15.5 s · **max 19.1 s** |
| Cold starts | 49 of 174 (28 %), **+0.57 s** avg, 0.67 s max |
| Throttles / Errors, 14 d | **0 / 0** |
| Language-retry (`Ask answer language leaked`), 30 d | **0** |

So a 4 s rewrite fits: 19.1 + 4 = 23.1 against 29. **But it cannot be
guaranteed, and the honest reason is the sample.** `p99 == max` is not a
percentile; it is the largest of 27 observations wearing a percentile's name.
The repository has already paid for budgets computed from samples too small to
show the tail, and the comment's own 21.3 s **exceeds the 19.1 s maximum this
window captured** — evidence that the real distribution reaches past what was
measured here.

So the design does not bet on the budget. It makes the budget enforceable and
makes the rewrite the first thing sacrificed:

1. **A deadline, carried.** `_rag_answer` records a monotonic start and passes
   the remaining budget down. The rewrite runs only if at least
   `ASK_REWRITE_BUDGET` remains; otherwise it is skipped and the original
   question is used. Because `standalone_question` already falls back to the
   original on every failure, a skipped rewrite is an existing, tested code
   path — **the rewrite can never be the thing that blows the budget.**
2. **A descending timeout ladder.** It currently ascends, which is why an
   overrun surfaces as a gateway 504 with the model call still running and
   still billing:

   | | today | required |
   |---|---|---|
   | API Gateway integration | 29 s | 29 s (unchanged) |
   | `ApiFunction` `Timeout` | **30 s** | < 29 s |
   | `AskAgentFunction` `Timeout` | **60 s** | < ApiFunction |
   | `LLM_HTTP_TIMEOUT` (ask-agent) | **45 s** | < AskAgentFunction |

   `AskAgentFunction`'s 60 s is unreachable on the user path anyway — its
   caller dies at 30 — so the ladder is documentation of an intent nothing
   enforces.
3. **A timing line on the screen path.** There is none today: the only
   per-call timing on `/ask` is `llm_utils`'s `qwen done:`, which is why the
   table above has n=27 for a route with 5115 gateway invocations. One
   structured line (`retrieval`, `rewrite`, `verdict`, `synthesis`, `total`),
   mirroring the voice path's, is a **prerequisite**: the tail has to be
   measurable before anyone promises it.

**The API Gateway timeout is not raised — a choice, not a limit.** The 29 s is
often described as a hard AWS ceiling. Measured in this account 2026-09-17, it
is not:

| | |
|---|---|
| `L-E5AE38E3` *Maximum integration timeout in milliseconds* | **29000 ms, `Adjustable: true`** — and the current value IS the AWS default, so no increase has ever been requested |
| AWS Lambda's own maximum | **15 minutes**, nowhere near either number |
| `ApiFunction` `Timeout: 30` | **ours**, set in `template.yaml`, and today unreachable because the gateway gives up at 29 first |

So the honest statement is: *the gateway's 29 s is a quota we could ask to
raise; Lambda imposes nothing here; the 30 s is our own.* It stays where it is
because raising it makes **every** reader wait longer for the benefit of the
slowest branch, while §4.5's deadline keeps the common answer fast. That is a
product decision and it can be revisited — with one thing checked first, see
§10.6.

### 4.6 A refusal becomes visible

`question_admission.screen()` already returns a sentence naming the reason
(`"names a site from this account (X)"`). `web_answer._spent(refused=…)` already
carries it to the client. Nothing renders it, so the reader sees only *"No
relevant records found"* and experiences a guard as an outage.

The web renders the refusal. No backend change.

**Why the guard stays.** Reading `screen()` end to end: it refuses a question
over 300 characters, one carrying a commercial term, one naming a site present
in the retrieved chunks, or one containing a capitalised run that appears in the
retrieved excerpts. Every one of those conditions means *this question is about
the customer's own world* — and the open web cannot answer a question about
their site, their staff or their contract. Removing the guard therefore buys
close to no capability while sending client names, site names and dispute
amounts to a third-party search engine. The guard also already carves out the
questions this feature exists for: it deliberately does not refuse capitalised
pairs like *New Zealand* or *Building Code*, and `NZ` counts as a currency only
when a digit sits within 12 characters of it, because *"what is the NZ standard
for scaffolding"* was once refused as a price.

`template.yaml` records a further reason that is not technical:
`ENABLE_EXTERNAL_CORROBORATION` is off *"until the pilot contracts are confirmed
to permit sending entity names to a third-party search engine."*

### 4.7 `history_turns` is logged on both paths

The voice path logs it (`lambda_ask_agent.py:1695`). The screen path gains the
same field. Without it, *"the web client does not send history"* and *"it sends
an empty list"* are the same observation on the web — which is the exact
ambiguity #833 added the counter to remove on the device.

### 4.8 A failure tells the reader nothing and tells us everything

**Owner decision, 2026-09-17.** The two halves are deliberately asymmetric.

**What the reader sees: one reassuring line, whatever went wrong.** No cause, no
timing, no mention of speed — *"they do not care"*. The same text for a timeout,
a 504, a 502 and an unreachable agent, because a reader on a site cannot act on
the difference and a technical distinction only invites them to diagnose it:

> **FieldSight is busy at the moment.** Your question has not been lost — please
> try again shortly. If it keeps happening, contact the FieldSight team.

This **replaces** today's two messages, which name the cause:
*"The agent took too long to answer. It may still be working — try asking
again."* and *"Could not reach the agent. …"*. Those were a deliberate
improvement in their time (`ask-chat.js:928` records why: *"a timeout is not an
unreachable agent, and saying so sent the reader at the backend while it was
answering correctly"*) — the distinction was right, and the place to spend it is
the log, not the screen.

**What we see: the distinction, in full.** The client already separates the
cases and must keep doing so — `_fetch.js:117` sets `e.timeout = true` on an
abort and `:248` sets `err.status` for an HTTP failure. Nothing about that
changes except where it is spent. The backend records, on one structured line:

* which stage the budget went to (§4.5.3: `rewrite`, `retrieval`, `verdict`,
  `synthesis`, `total`)
* the failure class: client abort / gateway 504 / agent `FunctionError` /
  invoke exception
* `history_turns`, `rewritten`, and whether the distance gate fired

**A gap this exposes, and it is not hypothetical only by luck.**
`lambda_fieldsight_api.py:50` builds `lambda_client = boto3.client('lambda')`
with no `Config`, so botocore's default read timeout governs the
`RequestResponse` invoke of the Ask Agent — and that default is longer than
`ApiFunction`'s own 30 s. When the agent hangs, `ApiFunction` is killed by the
runtime **before** its `except` runs, so the handler's
`logger.error("Ask agent invocation failed: …")` never executes and the only
trace is a bare `Task timed out`. The fix is a `botocore.config.Config` whose
`read_timeout` sits **below** `ApiFunction`'s timeout, so the invoke fails
inside our code where it can be described.

Measured 2026-09-17, prod, 30 days: **zero** `Task timed out`, **zero**
`Ask agent invocation failed`, **zero** `FunctionError`, and a maximum
`ApiFunction` duration of 19.1 s against its 30 s. **This path has never
executed in production.** It is being hardened before it is needed, which is
also why nothing here can be validated by waiting for it to happen — §8 forces
it instead.

---

## 5. Client changes

### 5.1 `fieldsight-ui`

* `ask-chat.js:483` `requestBodyFor()` adds `history`: the last turns of the log
  it already keeps, as `{question, answer}` — **those key names, not `q`/`a`;
  the server drops anything else, per turn, silently.** Omit the key entirely
  when there is nothing to send (§3.1: absent is not `[]`).
* `tz` is **not** added here. `scripts/api/ask.js:83` already injects the
  browser zone into every `/ask` body; `requestBodyFor` never sees it.
* The existing clear-on-scope-change at `:696` is **kept and is load-bearing**.
  Carrying a conversation from one site to another would retrieve site B with
  site A's referents.
* Render `asked` when it is present and differs from what was typed
  ("Searched for: …"), and render `web.refused` when present.
* **Replace both failure messages with the single reassuring line in §4.8.**
  Keep `err.timeout` / `err.status` in the code — they still decide what is
  reported, just not what is shown. One `catch`, one message, no cause.

`fieldsight-ui` **has no CI** — an empty check list on that repo is not
evidence. Local test results are the only evidence there.

### 5.2 `GrandTime`

Two separate changes, deliberately not shipped together (§9):

* **Step 0 — `AskApiClient.kt:35` adds `tz`** (the device's IANA zone id).
  Behind no flag, depends on nothing else here: without it `resolve_today`
  returns `None` and every spoken *"yesterday"* searches all of time. The
  backend has accepted the field for some time.
* **Later — the same body adds `history`**, as `{question, answer}` turns, kept
  in memory only and cleared when the app is backgrounded. **The count is the
  server's: up to `MAX_VOICE_HISTORY_TURNS` (6), each field truncated at
  `MAX_VOICE_HISTORY_CHARS` (2000).** The device does not carry its own smaller
  number — one field with two caps depending on which client sent it is how a
  cap stops being a cap.

---

## 6. What this deliberately does NOT do

* **No retrieval over the union of both turns' embeddings.** The rewrite
  produces one question and one vector. A union changes what `k` means and what
  the distance gate in §4.4 is measuring.
* **No server-side conversation store.** History lives with the client, arrives
  on the request, and is never persisted. Nothing to retain, expire or delete.
* **No history in the answering prompt.** §2.
* **No change to `lambda_rag_search`.** It receives a vector, not text.
* **No cross-site conversation.** §1 decision 5.
* **The distance gate does not change what is retrieved.** It decides only
  whether the *verdict model call* is worth making. Chunk selection is
  untouched.
* **Rerank stays off.** The ruler measured it inside its own run-to-run noise,
  with 3–11 of 45 calls exceeding the 3 s production timeout.

---

## 7. Tests

Unit, `pytest`. There is no `tests/unit/test_lambda_ask_agent.py` — the route's
tests are split by path, and new cases join the matching file:
`test_lambda_ask_agent_rag.py` (the `/ask` route), `test_lambda_ask_agent_voice.py`
(the voice path), `test_lambda_fieldsight_api_ask.py` and
`test_lambda_fieldsight_api_ask_voice.py` (the proxies). Follow the doubles
those already use.

Two existing files are the precedent for this work and should be read before
writing a line: `test_ask_voice_forwards_history.py` (the deployed history
contract, including absent-vs-empty) and `test_ask_tz_is_forwarded.py` (the
backend half of step 0, **already pinned** — step 0 adds only the device side).

**`ask_rewrite`**
1. No history → original returned, `rewritten=False`, and the `call` double is
   **never invoked** (assert the call count, not just the text).
2. A pronoun follow-up with history → the double's reply is returned,
   `rewritten=True`.
3. The double raises → original returned, no exception escapes.
4. The double returns a 400-character reply → original returned.
5. The double returns a reply containing `\n` → original returned.
6. The double returns `""` → original returned.
7. Budget exceeded → original returned.

**`_rag_answer` — driven through the route, not the helper**
8. A body with `history` embeds the **rewritten** text. Assert on the argument
   passed to `dashscope_utils.embed`.
9. A body with `history` builds the answering prompt from the **original**
   question and the prompt contains **no** history text. Assert the absence
   explicitly; this is §2's whole claim.
10. A body without `history` produces a rag-search payload **key-for-key
    identical** to today's. This is the byte-identical guarantee of §3.1.
11. `time_range` is computed from the rewritten text, not the original.
12. A malformed `history` (a string, a list of strings, a list of `{}`) answers
    normally.

**Distance gate**
13. Chunks whose nearest distance is 0.61 → verdict **not** called, lookup path
    taken.
14. Chunks whose nearest distance is 0.40 → verdict called.
15. Chunks with **no** `distance` key → verdict called (absent ≠ far).
16. Empty chunks → unchanged from today (retrieval returning nothing is already
    the verdict).

**Proof the tests guard the change.** For each of 8, 9, 10 and 13: revert that
one change alone, watch the test go red, restore it. Not a plausible mutation —
the real one. This repository has twice shipped a guard that hung on the very
field a defect removed, so the assertion was skipped and the test passed.

**A seam, so the stub count can be non-zero.** Tests 8–16 drive `_rag_answer`,
which reaches a live model through `standalone_question` — so every one of them
must stub it, and a stub-count ratio would report twenty-to-zero as a defect
when it is the design. The rule that actually applies here is *does any test
call it for real*, and that needs a seam: `_rag_answer` takes the rewrite's
transport the way `standalone_question` takes `call`, so tests 1–7 exercise the
**real** `standalone_question` against a fake transport. Without that seam, the
module's own rules are only ever asserted against a double of itself.

**Added by review, each covering a way the rewrite changes control flow rather
than text:**

17. A rewrite that introduces a date word into a question that had none →
    `metric_slots.detect` is **not** reached (the metric route stays closed).
18. Same case → `plan["body_date_sent"]` and `scoped` are unchanged from the
    no-rewrite run, so the web-fallback decision does not move.
19. The original question already yields a range → `time_range` is **not**
    recomputed from `asked`.
20. The web-answer branch receives the **original** question, not `asked`.
21. The gated path calls `web_answer.answer` with a **non-empty** `chunks` list
    (so `question_admission.screen` keeps its site and own-records signals).
22. `basis["widened"]` is true → the distance gate does not run.
23. A history of `{q, a}` (the wrong key names) is dropped per turn and the
    request still answers, with the drop count WARNed.
24. `asked` survives `_voice_answer`'s hand-built return (`:1702`) — the field
    exists on the voice response, not only on the screen one.

---

## 8. Verification on TEST (after deploy to `develop`)

Each step names what to look at, and what a failure looks like.

1. **The counter proves the wiring.** Ask one question from the web with no
   prior turn: the Ask log line shows `history_turns=0`. Ask a follow-up:
   `history_turns=1`. If it stays 0, the client half did not ship — that is the
   whole reason the counter exists.
2. **The rewrite is visible.** Ask *"what did James say about the ceiling
   grid?"*, then *"when is he finishing it?"*. The response's `asked` must name
   James and the grid. Record the exact text; it is the evidence.
3. **Citations move.** The follow-up's citations must differ from the citations
   a bare *"when is he finishing it?"* returns with no history. Same question,
   two bodies — the comparison is the measurement.
4. **Run 2 and 3 five times.** A prompt's behaviour is not established by one
   run; `prompt-instruction-present-is-not-obeyed` was measured on this same
   Ask prompt. Count how many of five rewrites are faithful and decide from the
   count.
5. **The answer is not built from history.** Ask a follow-up whose previous
   answer contains a distinctive phrase, and confirm that phrase appears in the
   new answer **only** if a citation supports it.
6. **Deletion.** Ask about a recording; delete that recording; ask a follow-up
   in the same chat. The new answer must not restate the deleted content, and
   must carry no citation to it. This is §2.1 and is the single most important
   check here.
7. **Scope change clears it.** Switch site mid-conversation and confirm the
   next request carries **no `history` key at all** — not an empty list (§3.1).
8. **Device.** From a device on the new build, ask *"what happened yesterday?"*
   and confirm the answer is about yesterday — `tz` arriving is what makes this
   possible at all.
9. **The distance gate.** `ENABLE_WEB_ANSWER` is **already `'true'` on TEST** —
   confirm by reading the deployed env back, do not set it. Ask a question with
   no possible match ("Which NZ standard covers timber design?" against a corpus
   with no such content) and confirm the log shows the lookup taken with **no**
   verdict call. Then ask one the records DO answer and confirm the verdict call
   still happens — a gate that never lets the verdict run is indistinguishable
   from a gate that is always open, from the log alone.
10. **Nothing changed for everyone else.** A body with no `history` returns the
    same answer and the same citations as before the deploy, for three
    questions recorded before it.
11. **Force a failure — it will not happen on its own.** Zero timeouts, zero
    invoke failures and zero `FunctionError` in 30 days of prod (§4.8), so this
    path can only be verified deliberately. On TEST, make the Ask Agent hang
    past `ApiFunction`'s timeout (a temporary sleep, or point
    `ASK_AGENT_FUNCTION` at a function that does), then check **both halves**:
    * **The reader** sees the single reassuring line from §4.8 — *"FieldSight is
      busy at the moment…"* — with **no** mention of timeouts, speed, or the
      agent. Repeat with the agent returning a 502 and confirm the text is
      **identical**; a difference the reader can see is the defect.
    * **We** see one structured backend line naming the failure class and the
      stage the budget went to. If the only trace is a bare `Task timed out`,
      the `botocore` `Config` from §4.8 is missing or its `read_timeout` is not
      below `ApiFunction`'s — the invoke is still outliving the handler.

---

## 9. Rollout

**Step 0, ahead of everything and behind no flag: the device sends `tz`.**
Without it `resolve_today` returns `None` and every spoken *"yesterday"*
searches all of time. The backend has accepted the field for some time, so this
is a one-field client change that fixes a live defect, and it depends on
nothing in this spec. Shipping it first also starts the `history_turns`
measurement that answers §10.3.

Then:

* `ASK_CONVERSATION_MEMORY` gates §4.2 and §4.3. Off on both stacks at merge.
  The flag must be wired in **all three places** — template parameter, both
  workflows, the function env — or it reads as its default and nothing fails
  loudly (`fieldsight-unwired-toggle-trap`). The verification is to read the
  deployed function's env back, not to read the template. `ASK_REWRITE_BUDGET`
  and `_DISTANCE_GATE` are wired the same way, in the same three places.
* **The distance gate (§4.4) is not flagged separately**, because the branch it
  sits on is already behind `ENABLE_WEB_ANSWER`. Measured 2026-09-17, and it is
  **not** what reading the workflow defaults suggests:

  | stack | `ENABLE_WEB_ANSWER` on the deployed `ask-agent` |
  |---|---|
  | TEST | **`'true'` — the web-answer path is live there today** |
  | prod | **absent, because the feature is not on `main` at all**: `origin/main` carries neither the template line nor `web_answer.py` nor `question_admission.py`. PR #832 is the promotion. |

  So the gate is exercised on TEST from the moment it ships, and cannot execute
  on prod until #832 lands. An absent variable here is the expected state, not
  the unwired-toggle shape — checked, because those two look identical from the
  outside.
* The §4.5 timeout ladder is **not** behind any flag and is not part of the
  feature: it is a configuration defect fix. It may ship on its own.
* Order: **device `tz`** → backend (inert, flag off) → web → device `history` →
  flag on for TEST → §8 → owner decides about prod.
* Rollback is the flag, and the flag's off-path is the code that runs today.

---

## 10. Open questions

1. **Is the `asked` residue acceptable?** §3.3: a rendered `asked` can re-show
   nouns lifted from a record deleted between turns. The alternative removes
   most of the visibility this spec relies on. An owner decision, not a
   technical one.

   **Answered 2026-09-20, by the owner: yes, the residue is acceptable.**
   The reasoning now lives in
   `docs/superpowers/specs/2026-09-20-ask-answer-sees-the-searched-question.md`
   §2. What was weighed: had the answer been no, the honest fix would have
   been to also remove the rendered `Searched for:` line, since that line —
   not the answering prompt — is where the residue actually reaches a
   person. Keeping that line while merely withholding `asked` from the
   prompt would have been a distinction without a protection.
2. **`_DISTANCE_GATE` has no measurement.** It is seeded at 0.55 because that
   number was measured for a *different* comparison (a per-group filter with a
   lexical escape hatch). Until it is measured per-chunk, the gate is a cost
   optimisation whose worst case is paying for a verdict call we could have
   skipped — it must never be allowed to decide anything a reader sees.
3. **Does the device need history at all**, or only `tz`? `tz` fixes a live
   defect on its own; spoken follow-ups have never been possible, so there is
   no measurement of whether people attempt them. Step 0 of §9 ships `tz` and
   starts that measurement; a fortnight of `history_turns` answers it.
4. **Should a rewritten question be editable by the reader** before it is used?
   It would make the failure correctable instead of merely visible. Out of
   scope here; it needs a UI decision.
5. **The screen path's timing line (§4.5.3) does not exist yet**, which is why
   the prod table in §4.5 has n=27 for a route with 5115 gateway invocations.
   Every latency claim in this document is therefore about a sample too small
   to show a tail, and should be re-read once that line has run for a fortnight.
6. **Can this account's 29 s actually be raised, and should it?** The quota is
   `Adjustable: true` (§4.5), but the prod gateway `fieldsight-prod`
   (`ys94qy2tk0`) and the TEST gateway are both **EDGE**-optimised endpoints,
   while only the idle `fieldsight-api` (`khfj3p1fkb`) is REGIONAL. Whether a
   raised integration timeout applies to an edge-optimised endpoint is **not
   established here and was deliberately not guessed** — the cost of guessing
   is a support case that is granted and then does nothing. Confirm with AWS
   before raising it, and note that §4.5 declines to raise it on product
   grounds regardless of the answer.
