# When the records cannot answer, look it up

Status: spec, second draft. The first draft was reviewed and three of its
foundational decisions were wrong; §8 records what changed and why, because two
of them were wrong in ways worth not repeating.

## 1. The gap, and what was mis-scoped

The differentiator is "the answer must not be limited to what we collected".
The 2026-09-08 spec claimed that was ~90% built and needed only a vendor swap.
It was not.

**Corroboration checks claims the answer already made.** When the corpus cannot
answer, the answer is *"the provided excerpts do not contain information about
X"* — a sentence with no entities in it. Extraction finds nothing, the gate
screens nothing, no search runs. It is structurally incapable of firing in
exactly the case the differentiator names.

Measured on TEST, 2026-09-10, corroboration switched on:

    "what is the NZ standard for scaffolding"   chunks 5, searched=false, cards 0
    "what does NZS 3604 cover"                  chunks 5, searched=false, cards 0

Four real site questions produced zero cards. The owner found this by opening
the TEST front end and asking for something outside the records — the first
thing anyone would do.

## 2. What goes out: entities from the QUESTION

The red line is in `corroboration_gate`'s own module docstring: **only entities
go out, never conversation text.** It was bought with a real leak — the gate's
first version required two capitalised words, which let a bare given name reach
the allowed list on a real session.

The first draft proposed sending a model-paraphrased *lookup query* screened by
that same gate, and called it reuse. It is not. Every rule in the gate assumes a
NAME: `MAX_ENTITY_CHARS` is 60, `_COMMERCIAL` refuses `cost|price|contract|
claim|variation|delay` (which is most of what a construction lookup asks
about), digits are refused unless the kind is `standard`, and `_PERSON_SHAPE` is
anchored `^…$` so it only matches when the WHOLE string is 1–3 capitalised
words. On a sentence the guarantees invert in both directions: legitimate
queries are refused, and *"scaffold sign-off requirements Neil Christchurch NZ"*
sails through carrying a person and a place.

**Decision: extract entities from the QUESTION, not a query from it.**

Corroboration extracts `{entity, kind, claim}` from the answer. The same
extraction, pointed at the question, yields `scaffolding standard` /
`New Zealand` from the measurement above. Those are names, which is the shape
the gate was built for and the shape the red line already permits. Nothing new
leaves the account, no policy moves, and the privacy machinery is used as
designed rather than borrowed.

The cost is honest and stated: a question with no nameable subject gets no web
answer. "What did we decide about the slab" names nothing external and will not
be looked up. That is the same trade the gate already makes everywhere else.

## 3. The trigger: two doors, and neither is a model's prose

Retrieval returns `chunks: 5` even when nothing is relevant, so "no chunks" is
not the whole signal — but review found it is not *no* signal either, and the
first draft got this backwards.

**Door A — retrieval returned nothing.** `lambda_ask_agent.py:1139` fires on at
least five real paths in `lambda_rag_search.py`: caller not provisioned (`:252`),
`site_count == 0` (`:280`), missing embedding (`:243`), a date-filtered question
with nothing to widen to (`:295`), and a caller whose only content is deleted.
The most important of these is **a new pilot account with no recordings yet**,
asking exactly the question in §1. That branch returns a fixed string today and
never calls a model, so a trigger that depends on the grounded answer would miss
the flagship case — the same indictment this spec makes of corroboration.

**Door B — the excerpts did not answer.** A separate, cheap classification call:
given the question and the excerpt headers, did these answer it? Measured on the
deployed vendor, this shape of call is 2.4–2.8s (n=8).

**What was rejected, and why it matters:**

| | |
|---|---|
| string-match the refusal prose | a phrase is not an interface; this repo has shipped seven consumers hanging off the literal `"failed"` |
| **make the grounded pass return an envelope (`force_json`)** | **rejected on review.** `force_json` is provider-dependent: the Anthropic branch never receives it, the DashScope non-thinking branch silently DROPS `max_tokens`, and the language-retry call site hardcodes `force_json=False` so a CJK-leaked answer's retry would return prose into a caller expecting JSON. It also collides with `answer_language.tail_rule()`, which is deliberately last in the prompt, and JSON mode measured 1671 reasoning tokens against 516 — inflating the very budget §4 depends on |
| always search | sends every question to a third party and pays the latency on questions the records answer perfectly well |

A separate call costs one round trip and touches nothing about how the answer
itself is produced. That is the whole reason to prefer it.

## 4. The budget, and where the real risk is

The first draft framed the risk as the web search overrunning. The code says
otherwise.

The web call through `corroboration_client` is properly bounded: per-attempt
timeout, a floor below which it refuses to start, and a retry only inside a
stated budget. **The grounded pass is the unbounded one.** It goes through
`llm_utils._post_with_retry`: up to 4 attempts at 45s each with backoff — the
four defects `corroboration_client`'s docstring lists as its reasons for
existing. One slow vendor attempt alone blows the 29s gateway ceiling before a
web search is even reached, and there is no `left()`-style clock anywhere in
`_rag_answer`.

**Decision: the clock comes first.** `_rag_answer` gets a deadline of its own,
shared by every step on the path, before any web step is added. Without it this
feature makes an existing unbounded path longer, and the caller sees a raw 504
and the UI's "Could not reach the agent" — not the honest fallback.

Numbers to size it from, and where each came from:

    grounded pass, TEST      5.7 – 8.8 s
    grounded pass, prod      9.0 – 10.8 s   (the repo's own recorded range)
    classification call      2.4 – 2.8 s    (n=8, this vendor)
    web search              10.3 – 11.4 s   (n=3, this vendor)
    gateway ceiling         29 s            (not ours to raise)

Prod worst case is the one that binds: 10.8 + 2.8 + 11.4 = 25 s before embed,
rag-search and gate. It does not fit with room. **So the web step runs only
when the deadline still covers it, and when it does not, the answer is the one
we have today — "the records do not cover this" — never an error.**

## 5. Voice is out

`_voice_answer` calls the same `_rag_answer` and pipes the result straight into
TTS. A web fallback there adds 10–11s to a path a worker is holding a button
for, and the voice prompt forbids URLs and formatting symbols, so sources cannot
be rendered. The voice path opts out by `mode`, explicitly, with a test.

## 6. Not mixing the two sources

The web answer is its own block under its own heading — the same separation
corroboration already uses ("From the open web — not from your recordings"). An
answer that silently blends them is worse than no answer: the reader cannot
audit it and has no reason to suspect they need to.

`searched` already exists for the other half of this and the same rule applies:
prose describing a search is not evidence a search happened. Measured on
muse-spark, a model will narrate a search it never ran and assert its findings.

**One circularity to cut:** the UI fires `/ask/corroborate` on whatever the
answer contains. If the answer is now sometimes web-derived, that corroborates
the web against the web. Suppressed explicitly.

## 7. Scope

In:
1. A deadline on `_rag_answer`, shared by every step (§4) — first, and shippable
   alone.
2. Entity extraction from the question, screened by the existing gate (§2).
3. A classification call for door B; door A is the existing empty-chunks branch
   (§3).
4. The web answer via `corroboration_client.call(web=True)` — client, credential
   and vendor already exist and are not re-litigated.
5. Response carries the web answer, sources with `domain`, and a flag saying the
   web was consulted, separate from the grounded answer.
6. UI renders it as a distinct block; corroboration suppressed on it.

Out: corroboration itself, voice, streaming, the 29s ceiling, and the gate's
policy — including `_PERSON_SHAPE`, see §8.

## 8. What the first draft got wrong

Recorded because two of these were wrong in the same direction — assuming a
mechanism did what its name suggested.

- **"`if not chunks:` never fires."** It fires on five paths, and the one that
  matters most is a new account with no recordings — the flagship case.
- **"Screened by the existing gate."** Technically callable, materially a
  different guarantee on sentences than on names, and a policy change dressed as
  reuse.
- **"`_looks_like_a_person` matches one word, contradicting its docstring."**
  The docstring is stale; the code is a deliberate correction. Its comment
  records that requiring two words "refused `Naylor Love` and let `Heidi`
  straight through — found on real sessions, where a bare given name reached the
  allowed list." Stantec and Bluebeam being refused is a known, accepted cost of
  a rule bought with a real leak. The first draft invited the owner to reconsider
  it on a false premise; that invitation is withdrawn.

## 9. What must be measured before it ships

1. That door B tracks reality — questions the records DO and do not answer,
   n≥5 each. Always-false is a feature that never fires; always-true sends every
   question out.
2. That a search actually ran before any web answer is shown.
3. The combined latency against 29s, on prod-shaped numbers, worst case not
   median.
4. That the deadline in §4 actually bounds the grounded pass — by driving it,
   not by reading it.
