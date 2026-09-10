# When the records cannot answer, look it up

Status: spec, third draft. Two adversarial reviews; §9 records what each earlier
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

**Door A must branch on `error`.** "Caller not provisioned" also lands here and
is an identity-resolution failure; serving it a web answer masks a defect behind
working-looking output. Only the empty-corpus cases open the door.

*(Draft 2 claimed five paths. The missing-embedding one is unreachable from
ask-agent — `dashscope_utils.embed` raises, and ask-agent computes the vector
inside its own try, so that failure becomes the generic error envelope.)*

**Door B — the excerpts did not answer.** A separate, cheap classification call.
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

**So the honest position is that this does not fit behind API Gateway REST**,
and no rebalancing changes it. Two ways forward; the choice is the owner's:

- **accept partial coverage** — run the web step only while the deadline still
  covers it, else answer as today. Ships inside the current architecture; fires
  least often when the vendor is slow, which is when a user is least patient.
- **move the route off API Gateway REST** — a Function URL lifts the ceiling and
  is the same change streaming needs. Bigger, and it fixes both.

Evidence this is not theoretical: with corroboration's budgets at their tightest
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

1. Door A branches on `error` — small, correct alone, no new capability.
2. A question-extraction prompt with its own exclusion list, measured (§3).
3. Door B on `corroboration_client`, fail-closed, reading `chunk_text` (§4).
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

## 10. What must be measured before it ships

1. Door B against reality — questions the records do and do not answer, n≥5
   each. Always-false never fires; always-true sends every question out.
2. The new extraction prompt's exclusion list, against real questions, for the
   leaks the existing one was calibrated against.
3. That a search ran before any web answer is shown.
4. Combined latency against 29s on prod-shaped numbers, worst case not median —
   including the compose step, which no one has timed.
