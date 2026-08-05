# Recurring-item threading — design

**Status:** approved 2026-08-05. Increment 1 in progress.
**Repos:** `fieldsight-pipeline` (schema, threading, status), `fieldsight-ui`
(card facts, review queue, ordering).

## The problem

A commitment made on site is restated on later days. "Sub A said the ground
floor walls finish Wednesday" comes back on Wednesday — either done, or
slipped. Today the second recording produces a **brand-new topic and
brand-new action items** with no link to the first, so the system cannot:

- close out an item when a later recording says it is done;
- notice that something has been promised three times and slipped twice;
- explain why an item deserves the top of the list.

That last one is the reported symptom: *"我总是抓不住重点."*

## What the data says (measured on prod, 2026-08-05)

Every number here came from querying prod, and two of them overturned an
assumption that had already been written down as fact.

| signal | reality |
|---|---|
| `action_items.deadline` (date) | **0** of 175 open items |
| `action_items.deadline_text` (free) | **18** of 175, and good: `"This Friday, 2026-07-25"`, `"Friday afternoon (2026-07-24)"`, `"Tomorrow"` |
| `priority` | high **44%** / medium 47% / low 9% |
| action-TEXT recurrence across days | 9 pairs out of ~15,000 — effectively none |
| topic-TITLE recurrence across days | clearly present |

Two corrections worth keeping:

1. **The deadline is captured and then dropped.** The extractor fills
   `deadline_text`; nothing on the backend parses it into the `deadline`
   date column, while the FRONTEND already has a parser for exactly this
   text (`today-adapter.js:resolveDeadline`). "No deadline data" was wrong —
   it is stranded, not absent. (Also: one row stores the literal string
   `"null"`.)
2. **Recurrence must be measured on the SUBJECT, not the commitment.** A
   first pass compared action-item text, found nothing, and concluded
   recurrence did not exist. The user said otherwise and was right: the LLM
   rephrases every time, so lexical overlap between commitments is near
   zero while the subject repeats plainly.

## The unit is the subject, and a thread is a cluster

A read-only probe over 175 open items and 140 topics settled the shape.

Matching **action items** to each other produced the cross-product of two
related topics: `Check door stop availability` paired with `Install door
handles` at the same score as every other combination, because the
similarity lives in the subject and not in what was promised about it.
Those are different commitments and pairing them would be wrong.

Matching **topics** worked. With a crude IDF-weighted lexical scorer, no
embeddings, roughly 8 of the top 11 candidates were real:

```
Door Hardware Specifications (02-09) ↔ Door Hardware Installation Progress (03-02)
Door Hardware Issues         (02-09) ↔ Door Hardware Installation Progress (03-02)
Carpentry + Door Installation(02-26) ↔ Door Hardware Installation Progress (03-02)
Electrical Work + Floor Box  (02-09) ↔ Floor Boxes Installation LH3        (02-26)
UCPK Onboarding Neil & James (07-23) ↔ UCPK Meeting with Neal and James    (07-31)
```

Note the first three: **a thread is a connected component, not a pair.**
Door hardware is one thread spanning four topics.

Two filters earned their place, and neither is a tuning knob:

- **Only topics that carry open work can join a thread.** A thread exists to
  track outstanding commitments; a topic with nothing open is not one. This
  removed almost all noise in one step — every false candidate in the
  unfiltered run had `open: 0+0`.
- **Cap the gap.** The surviving false positives share a signature: generic
  process words (`documentation`, `installation`, `floor`) across 128–135
  days. Real threads in the sample sit at 4–21 days.

## The machinery already exists

`src/lambda_programme_matcher.py` is the same shape: rank candidates by
embedding (`rank_by_embedding`, `SIM_MAX_DIST` 0.55 / `TOP_K`), then ONE
Claude call that must pick from the embedding survivors or pick none, with a
confidence floor. Swap "finding → programme task" for "new topic → open
thread" and the pattern carries over, including its fail-safe: below
threshold, it declines.

### …but its embedding half does not transfer (measured 2026-08-05)

This section originally said the embedding shortlist was the upgrade path
for recall. That was an assumption, and measuring it refuted it.

`report_chunks` already carries a DashScope vector for every chunk — 93 of
them bound to a topic on prod, reachable **in-VPC with no outbound call**, so
this looked free. Over the 753 candidate pairs those cover:

```
min 0.332   p10 0.507   median 0.614   max 0.923
```

The whole corpus is compressed into a narrow band, because a chunk embedding
of site talk captures *"this is construction"* far more strongly than *"this
is about door hardware"*. **RAG's own `SIM_MAX_DIST` of 0.55 sits above the
10th percentile here** — applied to this problem it would accept most pairs
on the site.

The separation is real but thin: the known-true Door Hardware pair reads
0.365 against 0.679 and 0.716 for unrelated radio topics — usable only with a
threshold near 0.4, tuned on one example.

The lexical scorer separates the **same pair** better: 0.46 against ~0 for
unrelated ones. That is not luck. The signal here is a shared distinctive
noun ("door hardware", "floor box") — exactly what IDF isolates and exactly
what a whole-chunk embedding averages away.

So the upgrade is **not** "swap in embeddings". If recall needs to improve,
the candidates are: embed the TITLE alone rather than the chunk, or hand the
lexical shortlist to Claude the way the programme matcher does and let it
judge. Both are worth measuring before either is built.

## Decisions

1. **Status:** `open → solved → closed`. `solved` is AI-proposed and awaits a
   human. **The AI never writes `closed`.** A transcript saying "yeah that's
   done" often means something else, so the proposal must carry its evidence
   (recording, quote, timestamp) and a human confirms. Rejection returns it
   to `open` and records why — that is the only feedback signal this will get.
2. **One terminal state.** `completed` and `closed` collapse to `closed` plus
   a reason (`completed` / `cancelled` / `superseded`). Two near-synonymous
   terminal states drift, and every report then has to query both.
3. **Never auto-raise `priority`.** 44% of open items are already `high`; a
   ratchet reaches 100% within weeks and destroys the field. Store **facts**
   instead — `times_raised`, `slip_count`, `last_raised_at` — and let the
   ordering read them. "Raised 3×, slipped twice" is derivable and *provable*;
   "high" is a judgement the extractor is demonstrably bad at. The facts also
   make the ordering explainable, which is the actual fix for 抓不住重点.
4. **Facts on the card, proposals in a queue.** The counts need no
   confirmation and belong on the Today card (and in the sort). The
   judgements — "this is done", "rename it" — go to a review queue, reusing
   `scripts/composites/suggestion-review.js` (Sprint 11 programme
   suggestions: confirm / adjust-then-confirm / reject, plus an evidence
   deep-link back to the recording).
5. **Never silently rewrite a title.** The original text stays for audit; a
   better framing ("Ground floor walls delayed again — Sub A resourcing") is
   a *proposed edit* a human accepts, through the content-correction audit
   path that already exists. The item stays one item with a "raised N times"
   timeline beneath it.
6. **Ids:** the existing `action_items.id` UUID is the identifier. A short
   human-quotable number is explicitly out of scope for now.

## What makes or breaks it

**A wrong link is worse than no link.** Attaching Wednesday's walls to the
wrong thread silently closes or escalates the wrong work, and nobody finds
out. So:

- the FIRST link into a thread is confirmed by a human ("is this last week's
  item?"); once confirmed, later mentions join that thread automatically;
- below the confidence floor, treat it as a new subject — the existing
  matcher already declines rather than guessing.

## Increments

**1. Thread the subjects (backend, inert).** Migration `0032`:
`topic_threads` + `topics.thread_id` (nullable). Threading in the
item-writer path. Derive `times_raised` / `last_raised_at`. No UI, no
behaviour change — nothing renders differently until increment 2.

**2. Facts on the card + ordering.** org-api passes the counts through;
Today shows 【raised 3×】【slipped 2×】 and `today-ordering.js` reads them.

**3. Proposals.** `solved` status + the review queue + proposed title edits.

**4. Parse `deadline_text` into `deadline`.** Independent of the above and
cheap — the frontend parser ports over. Unlocks `slip_count` (a slip is a
stated deadline that passed while the item stayed open), which increment 2's
ordering wants.

## Out of scope

- Site-facing short ids.
- Suppressing the extractor's recording-artifact topics (`Unclear
  Communication Recording`, `Unintelligible Audio Segment`, `Recording
  Test`). These are pure noise in every list — they dominated the unfiltered
  probe — but they are an extraction-quality problem, not a threading one.
