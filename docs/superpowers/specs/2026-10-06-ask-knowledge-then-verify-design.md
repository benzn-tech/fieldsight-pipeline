# Ask: records for the project, model-then-web for everything else (design, 2026-10-06)

Status: owner-approved 2026-10-06 ("开始"). Supersedes the web-answer route of
`2026-09-10-answering-from-the-open-web-design.md` where they disagree.

## What went wrong (prod, 2026-10-06)

The owner asked "Which NZ standard governs the length of flexi fire sprinkler piping, and
what is the specified limit?" twice and got "the excerpts do not contain it" both times:

1. The verdict sent it to the web. The lookup failed after 11 s with the vendor error
   `The operation was aborted`. It was not retried, and the reader was told nothing.
2. "search online Which NZ standard…" was refused by `question_admission`. Its
   sentence-opener filter only drops an opener at position 0, so `Which` counted as a name,
   and `which` occurs in the transcripts. The reader was told nothing.

The design also forbade any answer from the model's own knowledge.

## Measured (2026-10-06, gemini-3.8-flash via OpenRouter, n=1 per cell, 8 calls)

| strategy | sprinkler question | F4/AS1 balcony barrier |
|---|---|---|
| model only | 3.9 s, NZS 4541, "1.5 or 1.8 m by listing", says unsure | 2.3 s, 1000 mm (correct) |
| web only (today's prompt) | **aborted at 11.0 s** | 6.4 s, 1000 mm, 2 sources |
| model + web in one call | **aborted at 11.0 s** | 7.0 s, 1000 mm, richest |
| model draft, then web verifies the draft | **8.8 s, 5 sources**, corrected the clause, 1.8 m traced to NFPA 13 | — |
| web, then model fills gaps | 3.4 s, **invented "2.0 m"** | — |

Three lookups of the bare question failed identically at ~11 s; the same question carrying
a draft succeeded. A model asked to fill a gap after a failed search produced the most
confident wrong number.

## Decisions (owner, 2026-10-06)

**D1. Trust by claim type, not by weight.**
- Project facts (what was said or decided on this site, who, when, quantities) come only
  from the records.
- General knowledge (standards, codes, regulations, product data, methods) comes from the
  model, checked against the web.
- When the two disagree, the answer says so explicitly ("⚠ the records say 900 mm; F4/AS1
  requires 1000 mm").

**D2. Classify first, then run what the class needs.** After retrieval, one cheap call
(`gemini-3.8-flash`, effort low) returns:
`{"kind": "project"|"general"|"mixed", "records_answer": true|false}`.
This replaces the existing verdict call, so there is no extra call. The question kinds lead
to these flows:
- **project**: records answer only, which is today's grounded path. If retrieval found
  nothing, or `records_answer` is false, fall back to the general flow, so the worst case is
  "slower", never "nothing".
- **general**: the general flow. If the records hold related discussion, the records answer
  rides along as a second block.
- **mixed**: the records answer and the general flow run concurrently, then a compose call
  merges them with attribution and conflicts.

**D3. The general flow is model-then-web.**
1. Draft: the model answers from its own knowledge, says when it is unsure, and names the
   standard or clause.
2. Verify: the web plugin checks the draft. It keeps what sources confirm, corrects what
   they contradict (citing them), and marks the rest `[unverified]`. The search engine
   receives the question and the draft, never record excerpts.
3. If verify fails (any non-200 or empty reply), retry once with the same draft **only if**
   the remaining time covers another attempt.
4. If it still fails, return the draft **verbatim**, labelled unverified. The model is never
   asked to "fill the gap" after a failed search (measured: that invents numbers).

**D4. Admission keeps only the length cap.** `question_admission` drops the names-from-records
screen, the site-name screen and the commercial-term screen. It keeps "longer than 300
characters is not sent". Owner decision: commercial details in a question may reach the
search engine.

**D5. The reader is always told what happened to the outside lookup.** The `web` block carries
`status`, which the UI renders as one line:
- `verified`: "Checked against N web sources"
- `unverified`: "Model knowledge, not verified online (search failed)"
- `too_long`: "Not searched online (question too long)"
- `not_needed`: no line

**D6. Back office.**
- Each request logs one `ASK_ROUTE` line:
  `{"kind", "records_answer", "general_status", "retried", "ms": {...}}`
- The OpenRouter client logs the HTTP status of every failure; today only the message is
  kept.

**Unchanged.**
- A scoped Ask (day/site/author/topic), a pinned topic, and voice mode never take the general
  flow. They keep today's behaviour.
- `ENABLE_WEB_ANSWER=false` turns the whole general flow off, which falls back to today's
  records-only answer.

## Time budget (API Gateway 29 s; hard stop stays 27 s)

The slowest path is mixed:
- retrieval 1.3 + classify ~2.5 + draft ~3 + verify ~9 + compose ~3 ≈ 19 s;
- the records synthesis (~7 s) runs concurrently with draft and verify.

A verify retry (~9 s) is attempted only if at least 10 s remain before the hard stop.

## Response shape

The existing keys keep their meaning (`answer`, `citations`, `grounded`, `from_web`, `web`).
`web` becomes:

```
{"answer": str|None, "sources": [...], "status": "verified"|"unverified"|"too_long"|"not_needed",
 "kind": "project"|"general"|"mixed", "conflicts": [str, ...],
 "searched": bool, "timed_out": bool, "failed": bool, "refused": str|None}
```

- For a general question with no useful records: `answer` = the general answer,
  `grounded` = false, `from_web` = true.
- For mixed: `answer` = the composed answer, `citations` = the records cards, and `web`
  carries the general block and the conflicts.

## Cost

A general or mixed question costs classify + draft + verify (+ compose for mixed)
≈ 3–4 calls, against 2 today (verdict + lookup). A project question costs the same as today.
