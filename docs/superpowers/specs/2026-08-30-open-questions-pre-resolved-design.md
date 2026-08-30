# Spec: the meeting left a question open; resolve it before anyone goes looking

**Status:** proposal, second draft — first had five blocking findings, all confirmed against the code.
**Date:** 2026-08-30.
**Repo:** `fieldsight-pipeline`.

> This is the third layer of the Ask work. Layers 1 and 2 shipped on 2026-08-30 (#619,
> #622, #623): the search now understands *when* a question is about, and the answer says
> what it was built from. Those made **asking** work. This one is about the questions
> nobody asks — because the person who had them walked off site intending to look it up
> later, and often does not.

---

## The measured gap

Two real examples, both from the customer, both the same shape:

> *"I remember this pile is 150mm in 3604, but I can't remember. I'll have to go check."*

> *"I'm not sure whether CZ LiDAR still have any in stock."*

Each is **a stated fact with a stated uncertainty, deliberately deferred**. Today the
pipeline treats both as ordinary sentences. Neither is an action item (nobody was
assigned), a finding (nothing was found), or a decision (nothing was decided), so
extraction has no field for either and drops them: measured on one 71-minute session,
extraction keeps **5.0%** of the transcript's characters.

The meeting minutes therefore record a discussion in which nothing was open. The person
still has the question, and the answer is still hours of someone else's time away — the
designer waits on the engineer, and the engineer answers on their own schedule.

## What this is not

**Not search.** Nobody types a query. The system notices the question was left open and
resolves it unprompted, so the answer is already there when the record is read.

**Not a chatbot turn.** There is no conversation to be had on a site.

**Not a correction.** The system does not tell anyone they were wrong. It shows what the
standard actually says and lets the reader see for themselves that 150 was not one of the
options.

---

## 1. Open points live in the narrative, not in the structure

The single most important consequence, and the reason this belongs where it does.

*"I can't remember, I'll check"* survives no structured field. It is not an action item,
a finding, a decision or a question-to-someone. Any extraction pass that keeps 5% of the
characters and buckets the rest into those four fields **will drop it every time**, and
has been.

It survives in the **narrative**. So open-point detection belongs in `session_brief`
(shipped 2026-08-27, `SESSION_BRIEF` on for TEST, off for prod), which is the only pass
that reads the whole transcript as prose and is allowed to keep prose.

This is the first concrete argument for the narrative-first ordering that is not an
argument about style: it holds a class of information the structured fields cannot
represent, and that class is the most valuable one found so far.

### The detector

**A lexical GATE, then model-filled fields, then one mechanical constraint.** The three
are separate and the distinction is load-bearing -- a draft of this spec claimed the
detector was "lexical, not a classifier" and then had the model produce `kind` and
`subject`, which is a classifier wearing a rule's safety argument.

- **Gate (rules).** An open point exists ONLY where an uncertainty marker fires. A marker
  list that misses yields nothing, never a confident invention -- the `query_slots`
  property.
- **Fields (model).** `kind` is semantic; no marker list separates a standard from a
  supplier. The model fills it, inside the gate.
- **Constraint (code).** `subject` -- the ONLY string permitted to leave the building --
  **must be a verbatim substring of the checked quote**, enforced in code, or the open
  point is dropped. Without this, section 6's rule against free composition is violated by
  the mechanism this section describes.

An open point is a span where a speaker **asserts a fact AND marks it uncertain**. Both
halves are required — "I'm not sure" alone is hedging, not an open point.

Uncertainty markers (illustrative, the implementation plan pins the list):

- EN: *I think / I can't remember / not sure / I'll have to check / I'll confirm / from memory / off the top of my head / roughly*
- ZH: 我记得 / 记不清 / 不确定 / 回头查一下 / 回去确认 / 大概是 / 应该是

An open point carries a verbatim quote, because that is the only thing that makes it
checkable and a paraphrased uncertainty is indistinguishable from an invented one.

**The check does not exist yet on this path, and the plan must add it.** A first draft of
this spec claimed the brief already verifies quotes via `evidence_match.check_quote`. It
does not: `check_quote` has exactly one caller in the repo, `lambda_extract_session.py:378`
(the extraction path). What `session_brief` does is `_snap_to_quote` (`session_brief.py:224`)
-- a 70-character lowercase substring probe used to RE-ANCHOR a timestamp, not to verify a
quote. A quote that fails to snap stays in the brief; the one real brief's `stats` records
`unmatched: 2`.

So "the quote makes it checkable" is a property this feature has to build, not inherit.

## 2. Four kinds, because they resolve differently

| kind | example | resolver | what the reader gets |
|---|---|---|---|
| `standard` | pile size in NZS 3604 | model for structure, document for values | the cases it varies by, and where to look |
| `supply` | CZ LiDAR stock | the named company's own site | what was found, or that nothing was |
| `in_corpus` | "the date we agreed last time" | existing RAG | an answer with a citation |
| `needs_a_person` | engineer's sign-off | none | listed as still open, honestly |

**`needs_a_person` is not an omission.** A system that silently drops what it cannot
resolve is indistinguishable from one that resolved everything, and the reader has no way
to tell which they are looking at. It is displayed, unresolved, and says so.

## 3. Two dividing rules, and neither is about the topic

### Rule A — does the answer need a source?

> **Could this be copied into an email or an RFI? Then it needs a source.**

Clause numbers, standard references, prices, dimensions, dates: all on the "needs a
source" side. Judgements, orderings, and rewrites are not.

### Rule B — will this fact be the same in a year?

> **Same in a year** → the model may supply the **structure**; a document supplies the
> **values**. NZS 3604's clause structure does not change between model releases.
>
> **Not the same in a year** → external lookup, or say nothing. A model's answer about
> current stock is fabricated by construction: "now" does not exist in its weights.

The two customer examples sit one on each side of Rule B, which is why they are the
examples.

## 4. Structure beats precision, and this is a product rule

The customer stated it directly: *give the few cases and I'll apply them myself; a
chapter range is enough.*

That is not a concession, it is the safer artefact:

- **"This varies by load, ground condition and height — here are the cases, and it is in
  section N"** — a reader recognises a decision table, sees which row they are in, and
  knows where to verify. A wrong cell is visible as a wrong cell.
- **"140mm"** — indistinguishable from correct. Wrong, it goes into a variation.

(No real NZS values appear in this document, deliberately. A spec that warns against
fabricated standard values and then prints some as illustration is one copy-paste away
from being the thing it warns about.)

So a `standard` resolution renders as **a small table of cases plus a location**, never as
a single value. Where a specific value is shown it carries its source or is marked
unverified.

This also happens to be the cheap path: most questions stop at the structure and never
trigger a lookup at all.

## 5. Where it runs, and what it may never do

**In the slow pass.** The two-pass rule is already settled: the fast confirmation email
goes out at once and touches none of this; the enrichment lands behind the web link, and
is there before the reader is. Nobody waits on it.

**Never on the voice path.** Ask already spends **p90 8.6s** under API Gateway's 29s
ceiling — measured 2026-08-30 from CloudWatch `Duration` on `fieldsight-prod-ask-agent`,
n=80 over 30 days (p50 956ms, max 13.0s); retrieval itself is p50 56ms, so the time is all
synthesis. An
external fetch on top of that is unusable on a site, and `_voice_answer` must not reach
this code.

**Never blocking.** A failure here costs an enrichment. It may not cost the email, the
minutes, or the brief — the same posture `_store_brief` already takes.

## 6. What leaves the building

This is the section most likely to be skipped and least safe to skip.

An external lookup sends a query to a third party. **The meeting is not the query.**

- Only the **extracted term** goes out: a product name, a company name, a standard
  reference. Never the transcript, never the surrounding sentence, never the site,
  the client, the price, or who said it.
- The query is built from a **whitelisted set of fields** on the open point, not by
  handing the model a sentence and letting it compose freely — a free composition is a
  free exfiltration.
- Every outbound query is **logged in full**, because a claim that nothing sensitive left
  is worth nothing without a record of what did.
- `supply` lookups reveal the customer is interested in a supplier. That is a smaller
  disclosure than the transcript, and is still one — it is listed here so it is a decision
  rather than an accident.

### The brief is already outside the deletion machinery

Not this feature's defect, and this feature makes it worse, so it is named here rather than
discovered later.

`session_brief/` appears **zero times** in `deletion_mirror.py`, `deleted_predicates.py`
and `repositories/redactions.py`. The brief holds verbatim transcript quotes and is served
by a live endpoint. Deleting a recording hides its chunks and its topics; the brief keeps
the quotes and keeps serving them. That is precisely the shape this repository has already
shipped once and written down: *deletion leaks hide in frozen copies*.

It is not leaking today only because `SESSION_BRIEF` is false on prod and prod holds zero
briefs. **It becomes a live leak the moment that flag flips**, which makes registering
`session_brief/` as a deletion outlet a prerequisite for turning the brief on in prod —
independently of this feature. `open_points` adds more verbatim quotes plus a `subject`
recording supplier interest, so it widens the same hole rather than opening a new one.

### Fetched pages are hostile input

Retrieved chunk text is already treated as *"DATA, not instructions"* in
`RAG_SYSTEM_CONTEXT`, and that text comes from our own customers. A fetched web page is
attacker-controllable: anyone who can rank for a product name can put instructions in
front of the model.

So an external page gets the existing guard **and** these:

- it never reaches the same prompt as an action item, a to-do, or anything the model may
  propose a change from;
- its output is **display text plus a URL**, and cannot become a field on any row;
- the URL shown is the one actually fetched, never one the model produced.

## 7. Display-only, and the line that decides

The customer's constraint, stated 2026-08-30:

> Proposal cards are fine, but this must not complicate the system's interaction. Some of
> this does not need to be stored at all — showing it is the whole value.

So the storage question is **not** "is it useful" but:

> **Does it change system state?**

| | mechanism |
|---|---|
| changes an item's status, owner, date, or supersedes it | proposal card, stored, **confirm required** |
| explains, ranges, suggests, or resolves an open point | **rendered and gone**, no row, no confirm |

Everything in this spec is the second row. This matches the standing decision that
external knowledge is *fetched at query time, labelled with its source, and never written
back*, and it is why this feature adds **no table and no migration**.

The consequence is honest and should be stated: a resolution is **recomputed** when the
record is opened, not remembered. If that ever becomes too slow, caching is a separate
change with its own reasons — and a cache is not a record.

## 8. Interfaces

Nothing new is persisted. Two shapes:

**The brief gains `open_points[]`** — inside the existing `session_brief/{folder}/{date}/sid{id}/latest.json`:

```
{ "quote": "<verbatim, checked against the transcript>",
  "at": "HH:MM:SS",
  "claim": "<the fact asserted>",
  "kind": "standard" | "supply" | "in_corpus" | "needs_a_person",
  "subject": "<the whitelisted term an external query may use>" }
```

(No `speaker`. A draft had one; the brief names no speakers anywhere in its output, and its
own prompt tells the model that `spk_0` / `Speaker 1` "are NOT names -- they say which voice
spoke, not who the task is for". A speaker field here would have been a field invented to
round out a shape, which is the failure mode the sibling spec spent seven drafts on.)

**Everything else about resolution happens later and is never stored here**, so a brief
written today stays valid when the resolvers change.

### The resolver cannot live where the read does

`session_brief_read` is in `lambda_org_api`, and **org-api is in-VPC with no NAT** —
verified against the deployed function, not the template: `fieldsight-prod-org-api` has 3
subnets, as do `rag-search` and `item-writer`; `ask-agent` and `session-finalize` have
none. An outbound HTTPS call from org-api does not fail, it **black-holes until the
timeout with no log line at all** (BUG-36), and a design that puts the resolver behind the
read endpoint is a design that will be diagnosed for an evening as "the fetcher is slow".

**And "a resolver invoked by org-api" is not an option to weigh -- it is BUG-36 verbatim.**
`CLAUDE.md:864` is explicit: an in-VPC function cannot `lambda:InvokeFunction`, and a
cross-boundary call *originating* in-VPC goes through an S3 request artifact
(`extraction_requests/`, `session_finalize_requests/`, `reindex_requests/` -- one pattern,
three instances). That pattern is **asynchronous**, so "resolve synchronously on read,
behind org-api" cannot exist without a Lambda interface endpoint, which is new
out-of-band infrastructure that section 9 forbids.

The precedents a draft cited here were also backwards. `AskAgent → RagSearch` and
`device-report → its leaf` are **non-VPC → in-VPC**, the direction `CLAUDE.md:867`
explicitly permits and which is the OPPOSITE of what a resolver behind org-api needs.
(`finalize → ItemWriter` is not a precedent either: it is a proposal in the #598 plan, not
running code.)

So the resolve step lives on the **non-VPC surface** and the client reaches it directly --
the browser already talks to both gateways -- or it is asynchronous via the S3-artifact
pattern. The plan picks between those two. It does not pick "from org-api".

**If the plan picks asynchronous, section 7 constrains it and this is where the two meet.**
An async resolve has to put its result somewhere to be read later, and section 7 says this
feature stores nothing. Both are kept, because section 7's actual rule is *does it change
system state* -- and a derived resolution does not. So an async result is permitted as a
**cache**, under three obligations that a record does not have:

1. **Regenerable.** Losing it costs a recomputation and nothing else. Nothing may read it
   as the only copy of anything.
2. **Covered by the deletion outlet.** Same prefix registration as the brief itself (see
   section 6) -- a cache of verbatim quotes is a frozen copy, and the whole point of that
   section is that frozen copies are where deletion leaks live.
3. **Never authoritative.** It is not a proposal, it never gains a confirm, and no row
   anywhere may reference it.

A first draft of this spec named this tension in one sentence and the rewrite deleted the
sentence while keeping both sides of it. That is the relocation this repository keeps
finding in its own reviews, and it is written out here so the next reader can see it was
resolved rather than dropped.

**The honest minimal v1 is `standard` + `in_corpus` with no third-party web fetch** -- and
both of those still run non-VPC. A draft claimed `in_corpus` could be resolved in-VPC with
"the database and nothing else"; it cannot. Existing RAG requires a DashScope embedding
call, and `lambda_rag_search.py:12-14` says in capitals that it never embeds and never
calls a model -- ask-agent embeds first, then invokes it. `standard`'s structure answer is
a model call too. Nothing that touches a model can run inside this VPC. What v1 genuinely
avoids is the third-party FETCH, which is the part that needs an egress review.

**`GET /api/org/sessions/{id}/brief` must be widened; it does not return the brief whole.**
A draft of this spec said it did, citing its docstring. The docstring says "returned whole
rather than filtered"; the code (`lambda_org_api.py:2100-2107`) returns a five-key
projection -- `headline, sections, entities, tasks, stats`. Adding `open_points` to the
writer without adding it here would store them forever and serve them never, with every
writer test green: this repository's green-over-a-dead-path shape, and it would have been
built in on day one.

**It is already dropping two fields.** The stored brief carries `summary` and `open_todos`
(verified against the live TEST object) and the endpoint serves neither. That is a
pre-existing defect, not this feature's, and it is recorded here because it is the proof
that the projection is a whitelist and will silently swallow the next field too.

## 8b. Scope exclusions that would otherwise be discovered

- **Merged multi-device meetings produce nothing.** `process_finalize_request` skips
  re-derivation when `kind == "updated"` (`lambda_session_finalize.py:230-239`), so a merged
  meeting has no brief and therefore no open points. That is the case most likely to contain
  a question deferred *between two people*, and it is out of scope until the merge path
  produces a brief.
- **Prod is dark.** `SESSION_BRIEF` is false on prod. This feature ships into an environment
  where its host pass never runs, so the plan either owns the flag flip — which the
  deletion gap above blocks — or declares itself TEST-only. It may not leave this unsaid.

## 9. What this does not do

- **No new table, no migration.** If a draft of the plan adds one, the draft has drifted.
- **No new IAM in v1.** Scoped deliberately: a `supply` fetcher needs a role and an egress
  review, so the promise holds for `standard` + `in_corpus` and is void the moment the
  third-party fetch is in scope. An unscoped "no new IAM" would fire falsely against the
  very next change.
- **No confidence score.** It would be a calibrated statistic with no calibration behind
  it — the shape that produced an enrolment threshold rejecting every real window. A
  source link lets the reader judge; that is the honest instrument at this stage.
- **No "came up in N meetings" count**, and no count of any kind. #598 spent seven drafts
  defending a number nobody asked for.
- **No writing back to `name_aliases`** or any other production surface.
- **No change to the fast email.**
- **No voice path.**

## 10. Decisions still open

1. **Which document backs `standard`.** NZS is paywalled. Options: cite structure and
   location only and never quote (cheapest, legally clean, and already most of the value);
   or licence a corpus. **This is a commercial decision, not an engineering one**, and the
   first option is a complete v1 on its own.
2. **Whether `supply` ships in v1 at all.** It is the half that requires an external
   fetcher, a hostile-input boundary, and an egress review; `standard` and `in_corpus`
   need none of that. Splitting it out is the smaller first change.
3. **Whether the detector runs on TEST briefs first.** There are currently **two** briefs
   in existence, both on TEST (`Ben_UCPK2/2026-08-27` and `Ben_UCPK2/2026-08-29`); prod has
   zero. Any claim about detector precision made now rests on two sessions — the n=1 trap
   this repo has already been caught by, barely widened.
