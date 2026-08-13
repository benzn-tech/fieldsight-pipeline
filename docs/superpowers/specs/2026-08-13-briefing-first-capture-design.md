# Briefing-first capture — design

**Date:** 2026-08-13
**Branch:** `docs/briefing-first-design` (off `develop`)
**Status:** design, approved in principle; prototype built and verified
**Prototype:** `https://claude.ai/code/artifact/42e6f2c8-150e-48c4-910e-a64a6923bcf9`

---

## 1. Why

The pipeline structures first and renders the structure. A session becomes
`topics[] → {action_items, findings, decisions, questions}` and that JSON is
what every surface reads. The transcript stays in S3 and nothing reads it
again.

Measured on one real session (`sid15770a…`, Ben_UCPK2 × Sam, 2026-08-13,
13:25–14:36, 71 min):

| | value |
|---|---|
| transcript into the model | 103,974 chars (nothing truncated) |
| extraction content out | 5,242 chars — **5.0 %** |
| distinct proper nouns kept | 32, of which most are title-case words from topic titles |
| distinct numbers kept | **2** (`500`, `600`, surviving only inside an evidence quote) |

Everything the meeting was actually about — Westland, Rick Dunmore, CityCare
Water, Rob Speier, Riccarton, `$2,000/year`, `$150/month` — is absent. There is
no slot for a cost, a channel, or a person who was merely mentioned, so those
facts have nowhere to land.

### The controlled experiment

Same model (`qwen3.7-max`, `enable_thinking=true`), same meeting, three arms:

| arm | prompt | transcript | out chars | proper nouns | numbers | wall clock | tokens |
|---|---|---|---|---|---|---|---|
| A · prod | current, ~90 lines | 104 K | 5,242 | 32 | 2 | — (in-lambda) | — |
| B · control | current, verbatim | 244 K | — | — | — | 77.4 s | 118,129 (3,705 reasoning) |
| C · free | two requirements, no schema | 244 K | 5,423 | **54** | **7** | **40.3 s** | **111,988** (1,179 reasoning) |

Two results decide the design:

1. **The constraint does not buy brevity.** A and C are the same length
   (5,242 vs 5,423 chars) and C carries roughly double the facts. What the
   schema costs is density, not words.
2. **The constraint is not free.** B burned 3,705 reasoning tokens satisfying
   the schema against C's 1,179, and took 47 % longer for a worse answer.

A third result reframes the problem. Arm B, given a *more complete* transcript
and the *identical* prompt, produced **9 topics but only 3 action items** —
down from A's 6. Six of nine topics returned an empty `action_items` array.
The list is not a reliable record of the meeting and nothing signals when it
thins out.

### What is not broken

Per-session fill rates for `responsible` on 2026-08-13 are **bimodal, not
poor**:

| session | items | with assignee | kind |
|---|---|---|---|
| `sid1f48c0b9b` | 14 | **14** | team sync, people named |
| `sidb6d5d2abb` (Neil) | 5 | **5** | site walk |
| `sid92ecfafec` (Sam_Yu) | 5 | **5** | site |
| `sid15770a…` | 6 | **0** | two-person strategy chat |
| `sid1c85a77d` | 6 | **0** | solo dictation |
| `sid7538b6718` | 3 | **0** | two-person product chat |

Either 100 % or 0 %. The prompt says *"do NOT guess"* and it obeys: the 0 %
sessions genuinely contain nobody naming anybody. **The extractor is behaving
correctly.** What is wrong is that a schema with an `action_items` array
compels the model to fill it even when the conversation contains no actions, so
strategy directions get verbed into fake tasks (`Target market strategy --
focus high-hourly professionals`) that dilute the two real ones.

---

## 2. Principle

> **The timestamped transcript is the system of record. Topics, actions and
> summaries are lossy projections of it — views, not records.**

Today the projection is authoritative in Aurora and the original is unread in
S3. Inverting that ordering is the whole design.

---

## 3. What the retrieval layer must do

Measured against prod on 2026-08-13:

| check | result |
|---|---|
| `"Plaud"` anywhere in `report_chunks` | **0** |
| `"Heidi"` | **0** |
| keyword indexes (GIN / tsvector / trigram) | **none** — vector only |
| `report_chunks` total | 532 rows, 2026-02-09 → **08-12** |
| chunks for 2026-08-13 | **0** |
| `transcript_window` rows carrying a `topic_id` | 153 of 373 — the other 220 are discarded at read time (BUG-39) |

So a user asking *"which brands came up yesterday?"* cannot be served at all:
the words are not indexed, `lambda_rag_search` is pure pgvector (a poor
instrument for a rare proper noun), and windows without a topic are dropped.

Four requirements, three of whose inputs already exist:

1. **Word-level time anchors** — already computed by `transcript_utils`
   (`base + vad_offset + word.start`, BUG-09). They are discarded when chunks
   are cut to a topic's minute-granularity `time_range`.
2. **Literal keyword search** — Postgres FTS (`tsvector` + GIN) and trigram.
   This, not embeddings, is the right tool for `Plaud`, `150 lumens`, `NZS 3604`.
3. **Semantic search** — existing pgvector, for *"the privacy objection"*.
4. **An entity index** — the only new store. Per session: people, companies,
   products, standards, quantities, each with the spellings that actually
   occur in the audio.

**A transcript window must be storable without a topic.** The current
`chunk → topic` foreign dependency is what loses 220 of 373 rows.

### Why the entity index is not optional

ASR wrote **`PV Tech`** for **PB Tech**. A literal index answers `0` for the
string a person would type. In the prototype the entity's alias list recovers
**9 mentions**. Any keyword-only design fails this class of query, and it is
the common case for exactly the proper nouns worth searching.

Alias generation is a model output and **must be validated**. Unfiltered, the
model offered `Claude` as a spelling of `Plaud` — which folds 14 mentions of
the AI tool into a competitor device — and `record include` as a spelling of
`Riccarton clinic`. Two rules remove both classes:

1. an alias may not equal another entity's canonical name;
2. an alias composed entirely of common English words is a misheard phrase.

Applying them corrected Plaud from 37 mentions to 23.

### Time anchors are recomputed, never trusted

The model writes `at` from recollection and gets it wrong: the photo-linking
discussion came back as `13:33:11` when the audio has it at `14:33:11`. Every
anchor is re-derived by matching the model's own cited quote (or, for an item
with no quote, its rarest terms) against the real turns. On this session **4 of
23 anchors were corrected and 2 could not be matched** — and the unmatched ones
are counted rather than silently kept.

---

## 4. Generation

One pass over the full transcript, no schema over the prose. The JSON is a
container; each field holds free text.

```
headline   one sentence, concrete
sections[] { title, bullets[] { text, at, quote } }
entities[] { name, aliases[], kind, note }
tasks[]    { text, why, at, assignee, due, basis }
```

Prompt requirements, in place of the current ~90 lines:

- **Scannability.** Names, companies, products, numbers, amounts are the
  payload, not decoration. A reader must not have to open anything.
- **Self-sizing length.** One or two sentences per bullet. Neither telegraphic
  (`Procurement strategy -- productize as standard IT` is a failure: the reader
  cannot tell what to do) nor a paragraph.
- **Coverage.** Every subject discussed for more than ~2 minutes gets a bullet.
- **Aliases.** Give the correct spelling as `name` and the spellings actually
  present in the transcript as `aliases`.
- **Task admission: only what a specific person can finish and tick.**
  Strategy directions and exploratory ideas are bullets, not tasks. Two real
  tasks beat six invented ones.
- `basis: committed | inferred` — whether somebody took it on, or the model
  concluded it should be done.

Removed: `category`, `work_class`, `work_confidence`, `is_mixed`, `origin`,
`severity`, `domain`, `priority` as *forced* fields on this path. `severity` is
defined as impact on the programme; a meeting with no programme had 8 of them
invented. Anything a downstream consumer genuinely needs is derived later from
the prose, where it can be absent.

**Do not remove the current extractor.** On site walks and named team syncs it
already produces correct, assignable tasks (5/5, 14/14). The briefing pass is
additive; the cutover of each consumer is a separate decision.

### Measured cost

125 s, 110,150 in + 8,340 out (4,829 reasoning), producing 6 sections /
20 bullets / 17 entities / 3 tasks from 244 K chars.

Deduplication first: batch-window overlap re-transcribes the same audio, so
**1,157 of 3,715 turns (31 %) were near-duplicates** and 423 more were pure
backchannel. Collapsing them before the call removes about a third of the input
cost and is a prerequisite, not an optimisation.

---

## 5. Delivery surface

GrandTime has no WebView; it opens links with `Intent.ACTION_VIEW`
(`FilesScreen.kt:450`). The surface is therefore **a mobile web page opened in
the system browser** — no app work required.

Three tabs: **简报 / 问它 / 待办**.

- **简报** — sections of bullets; each bullet has a time chip that reveals the
  supporting quote. Entity names and quantities are marked inline.
- **问它** — a search field plus entity chips carrying real mention counts.
  A hit shows `HH:MM:SS`, speaker, the line, and expandable ±3 turns of context.
- **待办** — sortable by time raised / due date / unassigned-first; multi-select
  with one batch assign. **An unassigned task shows as unassigned**; the field
  is never filled by guessing and empty columns are not rendered as dashes.

### Verified in the prototype

| behaviour | result |
|---|---|
| `PB Tech` (never spelled that way in the audio) | 9 hits, first at `13:40:14` *"Yeah. PV Tech?"* |
| `Heidi` | 37 hits + web card + correction + 4 citations |
| `VXT` | 11 hits + web card + correction |
| a string that does not occur | 0 hits, no false positives |
| batch select two → assign | both assigned; select-all → clear works |

One real defect was found and fixed: re-rendering the task list on every
checkbox change detached the remaining checkboxes, so a second tap was silently
dropped — precisely the interaction batch-assign exists for. Selection state now
lives on the row and only the action bar redraws.

---

## 6. Web grounding

Asked *"which brands came up?"*, recall alone is not the valuable answer.
Checking what was said against public fact is:

- **Heidi** — the meeting assumed `$500–550` and a purely bottom-up B2C motion.
  Public pricing is **US$150/month billed annually (US$1,800/yr)**, and Health
  NZ bought **1,000 licences** for emergency departments with 1,000+ more in
  approval. The top-down path the meeting ruled out is the one the competitor
  actually walked.
- **VXT** — the meeting read it as a transcription product that won the lawyer
  niche. VXT **discontinued transcription in 2025** and now sells VoIP.
- **Plaud** — NotePin US$159 / NotePin S US$179 / Note Pro US$189, subscriptions
  US$17.99–29.99/mo. Consistent with the meeting's "a few hundred bucks".

Two of three premises the meeting reasoned from were wrong. This is the
strongest argument for the feature and it is not "search".

**In the prototype these cards are pre-computed from real searches and labelled
as such on the page.** The published artifact runtime grants only `downloads`
and `mcp`; it cannot fetch. Production requires a backend endpoint.

---

## 7. Out of scope here

- Standards lookup (`NZS ####` → clause text → compliance answer). Same
  mechanism as the brand cards; this session's meeting had no standards in it,
  so it is unvalidated and specified separately.
- Aurora migrations for FTS/entities, and the chunk rewrite that decouples
  windows from topics. Sequenced after the expression is accepted.
- Retiring any current consumer.

## 8. Risks

- **Inferred tasks.** The unconstrained model derives tasks from themes rather
  than from commitments — its thinking trace contains no step that looks for
  who took what on. It reads well and can put words in a user's mouth; on a
  site that could send someone to do work nobody asked for. `basis` must be
  surfaced, and `inferred` must never be presented as a record of the meeting.
- **Assignee may be structurally unavailable.** Removing phone interaction
  removed the context a screen would have collected. Speaker identity and site
  are not reliably recoverable from audio. A product that depends on assignment
  is building on a field that cannot be filled; the wearer's own list is the
  formulation that survives.
- **Page weight.** 2,135 turns embed as 182 KB (220 KB page). Fine for one
  session, not for a corpus — cross-session search needs the server index.
