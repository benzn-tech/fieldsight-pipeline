# Plan — answering from the open web

Spec: `docs/superpowers/specs/2026-09-10-answering-from-the-open-web-design.md`
(sixth draft, five adversarial reviews).

Seven steps. Every one leaves the tree in a state where the feature is off and
nothing a user sees has changed, because the flag it hangs from stays false
until step 7 and step 7 is the owner's.

**Two decisions are the owner's and gate what follows them.** They are named at
the steps that need them rather than collected at the end, so nobody builds past
one by accident.

## Step 0 — the branches stop waiting for each other

The whole budget depends on the grounded synthesis and the web chain running at
once. This changes only that, and it is worth shipping alone because it is where
the hazards live.

- Fork after `_rerank_chunks` (`lambda_ask_agent.py:1137`), not at chunks-in-hand:
  under rerank the two lines see different chunk sets, and the answer is built
  from the reranked five.
- A **join-or-abandon policy, written down**: how long the response waits, what
  it returns when one branch is late, and what happens to the late branch. Not
  "engineering detail" — an abandoned thread in Lambda resumes during the NEXT
  invocation and interleaves its log lines into another request's, in a codebase
  that adjudicates defects by log lines.
- A **merged failure envelope**: the grounded-error path has no slot for a web
  block today, and "grounded died, web succeeded" has no defined answer.
- **No module-global scratch shared between branches.** A frozen thread resuming
  mid-write is worse than interleaved logs.

Tests: both branches run concurrently and neither's result depends on the
other's; a late branch cannot extend the response past the shared deadline; the
grounded timing pin includes the language-retry case, or it pins a number that
does not happen.

Nothing calls the web branch yet. This step is a scheduler.

## Step 1 — door A opens on an empty corpus, not on a broken caller

`if not chunks:` (`lambda_ask_agent.py:1145`) fires for both, and they are
opposite situations.

Open the door when chunks are empty **and `error` is absent**. `error` marks the
defect paths — "missing sub or query_embedding", "caller not provisioned" —
while the empty-corpus paths carry no `error` key at all.

Tests: each rag-search shape reaches the right verdict; the identity failure is
asserted **not** to open it. Both fail-open admissions from the spec go in the
docstring, because a future rag-search path returning empty without setting
`error` earns a web answer silently.

Correct on its own, and shippable before any web call exists.

## Step 1b — OWNER DECISION: may a category phrase reach a search engine?

Everything after this depends on the answer, and the answer is not technical.

The gate passes names. The subject this feature extracts for its own motivating
question is `New Zealand standard for scaffolding` — a category phrase. It
passes the gate today, and `kind: standard` also grants it the digit exemption
built for bare standard numbers.

- **Yes** → continue.
- **No** → this feature answers only questions naming something proper, which
  the questions in §1 do not. Say so and stop; do not build steps 2–7.

## Step 2 — the gate: one call, no claim

One call answering "did these excerpts answer it" and "what should be looked
up", on `corroboration_client`, fail-closed.

- Subjects are `{entity, kind}` — **no `claim` field**. `screen()` never
  inspects `claim` and forwards it verbatim into the search-enabled call; that
  is safe only while claims come from the answer. A test in the same family as
  the existing pass-through pin holds it.
- A field-level parse policy: verdict parseable but subjects malformed is a
  state, and the spec does not name it. Pick one and log it.
- Its own exclusion list, measured against real questions rather than copied
  from the answer-side prompt.

**Stability is a ship gate, not a nicety.** The same question measured four
times returned two different subjects and, once, none at all. The empty case
means the records are declared unable to answer and then nothing is looked up —
the reader gets silence. Requirement: the same question yields a usable subject
on n≥10 consecutive runs, or this step is not done.

## Step 3 — budgets, from tails rather than from maxima of small samples

Per-step budgets and an internal hard stop, the way corroboration has one. Not
"compose gets what is left".

Sized from measurement, and the measurements that exist are already known to be
thin: the gate's 3.23s was a maximum over ONE question, and a second question
took 6.80s. Size from n≥20 per step, on questions at different distances from
the corpus.

**Write the rerank constraint into the code, not just here.** With rerank on the
path is 29.16s against a 29s ceiling. It defaults false and nothing turns it on
today; whoever turns it on later will break this feature and have no reason to
connect the two. A startup check or a template comment that names the other
flag.

**The language retry is counted, bounded, or skipped while the web branch is
active.** It is a second full synthesis and it was in no draft's arithmetic.

## Step 4 — the request field, before any web answer can be returned

The web fallback is gated on a request field only the new UI sends.

This is **not** the `externalCorroboration` pattern — that is a client-side build
flag the backend has never seen, and calling it precedent would be inventing one.
It is the first request-gated feature on this route.

Without it, between the backend returning web answers and the UI learning what
they are, every web answer gets corroborated against the web in production. The
response also needs a discriminator the UI keys suppression on; a request field
alone does not stop the new UI corroborating its own web answer.

Ships before step 5. Inert until then.

## Step 5 — search, compose, and the answer

- Search: gate-passed names only.
- Compose: question + findings → the LLM provider, prose, brief.
- **Compose needs an honesty mechanism, not a prompt line.** Reconcile survives
  this with a code-enforced enum and a drop-on-out-of-enum; compose is free
  prose, and "the model answered from its weights" is indistinguishable from
  "the model answered from thin findings". Citations-required, a findings-echo
  check, or structure — decided here, not discovered later.
- `searched` says the web was consulted, derived from results and never from
  prose.

## Step 6 — the UI block

Its own block under its own heading, never merged into the answer.
Corroboration suppressed on it. An older backend that sends none of this
renders exactly as it does today.

## Step 7 — OWNER DECISION: turn it on, on TEST, and measure

Acceptance is the four questions from §1 of the spec, end to end, with cards.
Anything less means the feature ships and the measurement that opened the spec
still reads zero.

Also measured here: the gate against questions the records DO answer (n≥5 each
way — always-false sends every question out, always-true never fires), that a
search ran before any web answer is shown, and the combined latency against 29s
on prod-shaped numbers, worst case rather than median.

This is the first time anything derived from a customer's meeting leaves for a
search engine on this path. The owner says when.

## Ordering and conflicts

0, 1 and 4 are independent and safe at any time. 1b gates 2 onward. 2 → 3 → 5 →
6 are ordered. Step 3 touches budgets other work may also be tuning; re-check
`origin/develop` immediately before merging it.

Nothing in 0–6 changes what a user sees while the flag is false, which it is
throughout.
