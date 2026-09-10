# When the records cannot answer, look it up

Status: spec, fifth draft. Four adversarial reviews; §9 records what each earlier
draft got wrong, because two of those mistakes shared a shape worth naming.

## 1. The gap

The differentiator is "the answer must not be limited to what we collected".
Corroboration does not deliver it and structurally cannot: it checks claims the
answer already made, and when the corpus cannot answer, the answer is *"the
provided excerpts do not contain information about X"* — a sentence with no
entities in it.

Measured on TEST, 2026-09-10, corroboration switched on:

    "what is the NZ standard for scaffolding"   chunks 5, searched=false, cards 0
    "what does NZS 3604 cover"                  same shape, same outcome

Four real site questions, zero cards. The owner found it by opening the front
end and asking for something outside the records.

## 2. What crosses which boundary

The red line is `corroboration_gate`'s, and its exact words matter because two
drafts read them wrong in opposite directions:

> Only the search step is covered here. Reconcile sends the answer to the LLM
> provider, and the answer is conversation-derived. That is a different
> statement from "only entities leave", defensible because **the same provider
> already saw the transcript during `/ask`** — but no *search engine* ever
> receives it.

So there are two boundaries, not one:

| | who sees it | rule |
|---|---|---|
| **search engine** | a third party that has never seen this account | entity names only, gate-screened |
| **LLM provider** | already receives `chunk_text` on every `/ask` | conversation-derived text is permitted, and already crosses |

Draft 1 proposed sending a paraphrased question to the SEARCH ENGINE and called
it reuse. Wrong boundary. Draft 2 corrected that and then had no step able to
turn entity findings back into an answer, because the question's intent cannot
be carried by names alone.

**Decision — three steps, each on the boundary that permits it:**

1. **Name the subjects.** Extract entities from the question. Gate-screened.
2. **Search them.** Entity names only reach the search engine. Rules unchanged.
3. **Compose an answer.** Question + web findings → the LLM provider, which
   already sees far more than this on every ask. **No policy moves.**

Step 3 is what draft 2 was missing, and it is why the answer can address the
question rather than describing whatever entities happened to be nameable.

**The stated cost:** a question with no nameable subject is not looked up.
"What did we decide about the slab" names nothing external. That is the same
trade the gate makes everywhere else — the price of the boundary, not an
oversight.

## 3. Extraction from a question is new code, not reuse

Draft 2 said "the same extraction, pointed at the question". It is not.
`EXTRACT_PROMPT` says *"List the named external entities the ANSWER refers
to"*, and its central rule is *"Never build the claim out of the question"*.
Pointed at a question it contradicts itself.

So this is a **new prompt**, inheriting none of the calibration the existing one
earned against 40 real sessions. Two consequences to carry rather than assume
away:

- its exclusion list (people, sites, project codes, the product itself) has to
  be re-derived for question text and measured, not copied
- `New Zealand` — draft 2's own worked example — is a place, which the prompt
  excludes and `ALLOWED_KINDS` refuses. **The example did not survive the
  machinery it was written to demonstrate.** A question's subject is often a
  category ("scaffolding standard") rather than a name, and the kind vocabulary
  has to say whether that is admissible before any of this works.

## 4. The trigger: two doors

Retrieval returns `chunks: 5` even when nothing is relevant, so "no chunks" is
not the whole signal. Review found it is not *no* signal either.

**Door A — retrieval returned nothing.** `lambda_ask_agent.py:1139` fires on
four real paths in `lambda_rag_search.py`: caller not provisioned (`:252`),
`site_count == 0` (`:280`), a date-filtered question with nothing to widen to
(`:295`), and a caller whose only content is deleted. The flagship case is
**`site_count == 0` — a provisioned user with no recordings yet**, asking
exactly the question in §1. That branch returns a fixed string and never calls
a model, so a trigger keyed only on the grounded answer would miss it.

**Door A opens on empty chunks with NO `error` set.** Stated as an exclusion,
because the inverse reading is the dangerous one and draft 3 left it ambiguous.
Checked: the empty-corpus paths carry no `error` key at all (`site_count == 0`
returns `{"chunks": [], "site_count": 0, ...}` at `:280`; widen-empty and
deleted-only fall through to the normal return), while `error` marks the defect
paths -- `"missing sub or query_embedding"` (`:243`), `"caller not provisioned"`
(`:252`). Serving an identity failure a web answer would mask a defect behind
working-looking output.

Two things this branch does not do, stated because both fail OPEN. It assumes
empty-without-error means empty corpus, so any future rag-search path returning
empty without setting `error` silently earns a web answer -- and `:280` already
shows the contract is not "errors are always set". And `site_count == 0` can
also mean a `site_filter` that matched nothing (`:270`), unreachable from `/ask`
only because ask never sends `site`.

*(Draft 2 claimed five paths. The missing-embedding one is unreachable from
ask-agent — `dashscope_utils.embed` raises, and ask-agent computes the vector
inside its own try, so that failure becomes the generic error envelope.)*

**Door B — the excerpts did not answer — and naming the subjects, in ONE call.**

Draft 4 had these as two steps, 2.80s + 6.45s. They take the same inputs: door B
judges from the question and the chunks, subject-extraction reads the question,
and neither needs the other's output. Measured 2026-09-11, n=8 each, same run,
alternating, on realistic chunk text:

    door B alone            1.99  2.60  2.64  2.72  2.80  3.13  3.24  MAX 3.30
    door B + subjects       1.91  2.03  2.17  2.28  2.38  2.46  2.48  MAX 3.23

**The merged call is not slower than door B alone; the 6.45s extraction step
disappears entirely.** Extraction was expensive because it was a separate call
re-reading the context. Merged, it reads a question already in the prompt and
emits one more array. 8 of 8 judged `answered: false` correctly and 8 of 8
produced a subject.

That is the difference between 1.68s of margin and 7.70s (§5), and it removes a
round trip and a failure mode rather than trading one for another.

**One question it opens, which is the owner's.** The subject it returns for §1's
question is `New Zealand standard for scaffolding`, kind `standard` — a
*category phrase*, not a proper name. Nothing in the gate refuses it today (it
is under the 60-char cap, carries no commercial term, has no digits), but the
gate was built to pass NAMES, and whether category phrases may reach a search
engine is the same kind of boundary judgement as the `Heidi` correction. Round 3
raised this as "the feature may never find anything"; it is now a specific
question with a yes or no, and §10 requires §1's own questions as acceptance
cases so the answer is visible rather than assumed.

The rejections below stand, and applied to the merged call they apply the same.
`force_json` on the grounded pass is rejected: the Anthropic branch never
receives it, the DashScope non-thinking branch silently drops `max_tokens`, the
language-retry call sites hardcode `force_json=False`, it collides with the tail
rule that is deliberately last in the prompt, and JSON mode measured 1671
reasoning tokens against 516.

Door B runs on `corroboration_client`, not `llm_utils`, and **that choice sends
excerpt text to the corroboration vendor.** Under §2 that is the LLM-provider
boundary and permitted — but it is a *different* provider from the one that saw
the transcript during `/ask`, so it is a new recipient, called out here rather
than buried. Fail closed: an unparseable verdict means no web answer.

Chunk *headers* are `topic_title` + date and say nothing about relevance; door B
needs `chunk_text`.

## 5. The budget, and what tonight measured about it

`call_llm` now takes a `deadline` (shipped 2026-09-11), so the grounded pass can
be bounded at all — which draft 2 assumed and draft 1 did not even name. That
was the larger blocker and it is gone.

The smaller one is now binding, and tonight measured it:

    grounded pass, prod       9.0 – 10.8 s
    door B classification     2.4 –  2.8 s
    web search (3 entities)   up to 11.4 s
    compose (new, §2 step 3)  unmeasured
    gateway ceiling          29 s

Corroboration's own three steps, sized from measured tails plus honest margins,
already fill a 27s stop exactly — 8% / 23% / 12% of cushion, nothing left to
give. **This path is longer than that one** and adds a step nobody has timed.

Draft 3 concluded from this that the path "does not fit behind API Gateway REST"
and offered the owner a choice between partial coverage and an architecture
move. Review then added the step draft 3 forgot to count -- its own
question-extraction call, max 6.45s -- and the sequential worst case became
**31.5s before compose** against a 29s ceiling. Door B looked decorative.

**That arithmetic assumes the steps queue, and they do not have to.**

Door B asks "do these excerpts answer the question". Its inputs are the question
and the chunks. `lambda_ask_agent.py:1133` has the chunks in hand; the grounded
synthesis call happens after that line. **Door B does not depend on the grounded
answer, and neither does anything downstream of it.** They are sequential
because they were written in reading order, not because one needs the other.

**The clock starts at the HTTP request, not at chunks-in-hand.** Draft 4 anchored
it at `:1133` and planted 9.0–10.8s there — but that figure is the WHOLE `/ask`
round trip, measured browser-side and quoted in this repo's own frontend comment.
The 29s ceiling does not exclude what happens before chunks land. Corrected from
the per-call split measured in prod logs the same night (total Duration minus the
model call): embed + rag-search is **1.2–1.4s**, and synthesis alone is 8.9–11.9s.

Every number below is measured. `compose` was `~3s` in draft 4 — invented for a
step the same section called unmeasured — and is now 8 runs on a realistic
findings payload.

    t=0.00  HTTP request
      +1.36  embed + rag-search ....................... t= 1.36
             |-- grounded synthesis ... 11.9s ......... t=13.26
             |-- gate (answered? + subjects) .. 3.23s .. t= 4.59
                  |-- web search ............ 11.40s ... t=16.00
                       |-- compose ............ 5.31s .. t=21.30

    worst case 21.30s   ceiling 29s   margin 7.70s
    with ENABLE_RERANK on (+~4s, default false)  25.30s   margin 3.70s

Draft 4's own arithmetic, recomputed from the request clock and with compose
measured, was **27.32s — 1.68s of margin, and 31.32s with rerank on, over the
ceiling.** The merged gate (§4) is what turned that into room.

Nothing is wasted in wall-clock: the web branch starts only if the gate says the
excerpts will not answer, and the gate has decided by 3.23s. **It is not free in
other currencies**, and §4 says so: the gate call ships `chunk_text` to the
corroboration vendor on every question, including the majority the corpus
answers.

This is not free, and the costs are engineering rather than a dilemma:
concurrency inside a Lambda that has none today, a deadline shared across
threads rather than one `left()`, and a failure mode where one branch dies and
the other must still return. The previous framing had none of that -- it asked
the owner to choose between shipping something decorative and rebuilding the
transport, because the steps had been assumed to queue.

**What survives from draft 3:** the margins are thin, the ceiling is not ours,
and a Function URL still lifts it and is still the same change streaming needs.
It is no longer a precondition for this feature.

Evidence the tails are real: with corroboration's own budgets at their tightest
honest setting, roughly **one run in six** still exceeds them.

## 6. Not mixing the two sources

The web answer is its own block under its own heading — the separation
corroboration already uses. An answer that blends them is worse than none: the
reader cannot audit it and has no reason to suspect they need to.

`searched` already exists for this and the rule holds: prose describing a search
is not evidence one happened. Measured on muse-spark, a model will narrate a
search it never ran and assert its findings.

**Two circularities to cut.** The UI fires `/ask/corroborate` on whatever the
answer contains, so a web-derived answer would be corroborated against the web.
And suppression cannot be the last thing shipped: the UI has no discriminator
today, so between the backend returning web answers and the UI learning about
them, every one gets corroborated in production.

**Decision:** the web fallback is gated on a REQUEST field only the new UI sends
— the same pattern `externalCorroboration` already uses. An old UI never
receives an answer it cannot label, and deploy order stops mattering.

## 7. Voice is out

`_voice_answer` calls the same `_rag_answer` with `mode: "voice"`, so the
opt-out is real and testable. A web fallback there adds 10s+ to a path a worker
holds a button for, and the voice prompt forbids URLs and formatting symbols.

## 8. Scope, in shippable order

0. Make the two branches concurrent (§5). No new capability, no new recipient:
   it changes only what waits for what, and it is what makes the rest fit.
   Shippable alone, with the grounded answer's own timing pinned so a
   regression is visible.
1. Door A opens when chunks are empty AND `error` is ABSENT (§4) — small,
   correct alone, no new capability.
1b. The gate decision: may a category phrase reach a search engine (§4)? Owner's
   call, and it gates everything after it — a "no" means this feature answers
   only questions naming something proper, which §1's own examples do not.
2. The merged gate on `corroboration_client`, fail-closed, reading `chunk_text`
   — one call, answering "did these excerpts answer it" and "what should be
   looked up", with its own exclusion list measured against real questions (§3).
4. The request-field gate (§6) — **before** any web answer can be returned.
5. Compose step and the web answer, deadline derived from what is left.
6. UI block, corroboration suppressed on it.

Out: corroboration itself, voice, streaming, the gate's policy including
`_PERSON_SHAPE`.

## 9. What the earlier drafts got wrong

Two of these share a shape: **assuming an interface accepted what was being
handed to it.**

- **"`if not chunks:` never fires."** It fires on four paths, and the flagship
  is a new account with no recordings.
- **"Screened by the existing gate."** Callable, but its rules assume a name; on
  a sentence, legitimate queries are refused (60-char cap, commercial terms,
  digits) and an embedded person's name passes, because `_PERSON_SHAPE` is
  anchored to the whole string.
- **"The same extraction, pointed at the question."** The prompt forbids exactly
  that, and the worked example did not survive `ALLOWED_KINDS`.
- **"A deadline on `_rag_answer`, shippable alone."** `call_llm` had no timeout
  parameter; bounding it required changing a module nine callers share. Done
  since, as its own change.
- **"`_looks_like_a_person` contradicts its docstring."** The docstring is
  stale; the code is a correction bought with a real leak — requiring two words
  "refused `Naylor Love` and let `Heidi` straight through". Withdrawn.
- **The step nobody counted, three times.** Draft 3 forgot its own
  question-extraction call. Draft 4 counted extraction and invented `~3s` for
  compose. BOTH anchored the clock at chunks-in-hand, so neither ever counted
  embed + rag-search — and draft 4 additionally planted a whole-round-trip
  measurement at that anchor. Every figure in §5 is now measured, and the clock
  starts where the gateway's does. The pattern is worth naming because it
  survived three reviews: **an arithmetic that always came out just fitting,
  because the parts that did not fit had not been written down.**

## 9a. What is still not solved, and is not arithmetic

Two hazards survive the margin and would survive any margin:

- **Nothing can cancel an attempt in flight.** The deadline is checked between
  attempts, and urllib3's timeout is per-read, not total — a slowly trickling
  response can outlive it.
- **An abandoned thread outlives the response.** Lambda freezes the sandbox; a
  thread still reading at the 45s `LLM_HTTP_TIMEOUT` resumes during the NEXT
  invocation, interleaving its log lines into another request's — in a codebase
  that adjudicates defects by log lines.

So §8 step 0 is not "no new capability, only what waits for what". It needs a
stated join-or-abandon policy and a merged failure envelope, and those belong in
the plan rather than being discovered during it.

## 10. What must be measured before it ships

1. Door B against reality — questions the records do and do not answer, n≥5
   each. Always-false never fires; always-true sends every question out.
2. The new extraction prompt's exclusion list, against real questions, for the
   leaks the existing one was calibrated against.
3. That a search ran before any web answer is shown.
4. Combined latency against 29s on prod-shaped numbers, worst case not median —
   including the compose step, which no one has timed.
