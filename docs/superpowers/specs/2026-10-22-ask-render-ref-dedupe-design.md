# Ask render: show a repeated item once (ref dedupe) — design

Status: rev 3, 2026-10-22 (Fable review applied; §6 step 1 gate PASSED, see §6.1). Line: ASR+LLM优化v3 (Ask orchestrator). Review: Fable before plan.

## 1. Problem

The Ask v2 judge reads evidence as one fenced render per tool call, rebuilt before every
judge call (`ask_orchestrator._messages` → `render_each` → `_budgeted`). The budget is
`RENDER_MAX_TOKENS = 8000` (`RENDER_MAX_CHARS = 32000`) across all rounds. Over budget,
older rounds lose items first.

Refs are stable inside a turn: `ask_tools.RefAssigner.assign(kind, ident)` gives the same
`(kind, ident)` the same ref in every call. Kinds differ by tool: `search_records` /
`latest_activity` return `chunk` items (chunk id), `day_overview` returns `topic` items
(topic id), and metrics carry the call id, so they never repeat. Repeats therefore come
from the same kind seen twice: `day_overview` ↔ `day_overview` (topic summaries,
query-independent, so byte-identical) and `search_records` ↔ `search_records` /
`latest_activity` (chunks; byte-identical today, but query-centred windows after the
search line's change, see D1). `_budgeted` renders every occurrence in full. The search line's logged run had 9 of 55 turns with 4–5 tool calls,
where repeats are most likely. Each repeat spends budget twice on identical text, and
because older rounds are dropped first, the repeat can push out *other* round-1
evidence.

The search line is about to raise per-item text from 600 to ~1,000 chars (centred on the
query terms) and `MAX_RESULT_CHARS` from 10,000 to 12,500. That makes each repeat cost
more, and three full calls at 1,200 already reach ~31K of 32K.

## 2. Goal / non-goals

Goal: an item whose rendered line is identical to one already shown in full in this
render is shown as a one-line stub. The full text appears exactly once, wherever it
survives the budget.

Non-goals:
- Changing the budget value, the drop order, tool output, refs, citations or client
  cards. `to_client` / `_known_refs` read envelopes, not the render, and stay as they are.
- Merging items that share a ref but differ in text (see D2).
- The search line's per-item window and `MAX_RESULT_CHARS` (theirs).

## 3. Decisions

- **D1 — Dedupe key is the full rendered line, not the ref.** A line is stubbed only when
  an earlier surviving line in the same render has the same ref *and* is byte-identical.
  After the search line's change, `search_records` centres its window on each call's
  query terms, so the same chunk can come back with a different window in round 2. That
  second window is new evidence and must stay in full.
- **D2 — Same ref, different text: both stay in full.** No partial merge or diff. This is
  rare and correctness wins over the extra bytes.
- **D3 — The full copy is the first surviving occurrence in render order** (envelope
  order, then item order), which is the order the judge reads the messages. Later
  identical ones become stubs.
- **D4 — Budget is computed on what is shown.** The drop order is unchanged (oldest round
  first; within a round, the last line of the call holding the most lines), but the
  running total is **recomputed as `size()` of the shown lines after every pop**. The
  incremental `current -= len(popped) + 1` is wrong under dedupe. Popping a stub saves
  only the stub. Popping a full copy that has a later twin promotes the twin, so it saves
  only about `len(stub)`. That item then moves to the later block, and the earlier
  header's `shown` drops. The final render is built from the shown lines after the loop,
  so a stub can never point at a dropped copy. Termination is unchanged: each pop removes
  one line. Cost is O(n²) on n ≤ ~60 lines, which is negligible.
- **D5 — Stub format:** `R7 | shown earlier` (not "above": a stub can sit several
  fenced tool messages after its full copy, and "above" can read as the previous line). It keeps the leading `ref |` shape every
  item line has, so the judge can still cite it. The literal text is fixed, and a stub is
  produced only by the renderer, never from tool text. `sanitize` already prevents tool
  text from forging a line start.
- **D6 — Header counts.** A stub counts as shown: the item is visible to the judge, so
  `_header(env, total, shown)` keeps reading `shown = len(lines)`, stubs included.
- **D7 — No prompt change.** `same as above` reads plainly. The live check in §6 confirms
  the judge still cites stubbed refs correctly. If it does not, a one-line note in
  `_SYSTEM` is the fallback (a measured follow-up, not this change).
- **D8 — No flag.** This is a pure render change with no data, contract or client effect.
  Rollback is a revert. It goes develop → TEST → the next release, like any orchestrator
  change.

## 4. Change

`src/ask_orchestrator.py` only.
- `_budgeted(envelopes)` keeps full lines in `blocks[i]["lines"]` as today. A helper
  `_shown(blocks) -> list[list[str]]` walks blocks in order, keeps a set of full lines
  already shown, and maps a repeat to `<ref> | shown earlier`. The dedupe key is the line
  itself: the ref is its prefix, and `sanitize` keeps `|` out of fields, so equal lines
  mean the same ref and the same text. The ref for the stub is the prefix before the
  first ` | `.
- `size()` measures `_shown(blocks)`, the loop re-measures after each pop (D4), and the
  return value is `_shown(blocks)` with headers counting shown lines.
- Lines starting `? |` (no ref) are never stubbed.

## 5. Tests (`tests/unit/test_ask_orchestrator_render.py`)

1. Same ref, identical text, two rounds → the first is in full, the second is
   `R7 | shown earlier`, and both headers count it as shown.
2. Same ref, different text → both in full.
3. Same ref within one round across two calls → the second call's copy is the stub.
4. Over budget, where the drop removes the round-1 full copy → the round-2 occurrence is
   rendered in full (no stub pointing at nothing).
5. `render_each` keeps its per-envelope split, with stubs in the later envelope's block
   and one budget across all.
6. A tool text containing `shown earlier` or `R7 | shown earlier` is sanitized like
   today and does not create a stub.
7. Size: three envelopes sharing items render fewer chars than before, and the existing
   budget tests (`test_over_budget_*`) still pass unchanged.
8. Budget with repeats: over budget, with rounds 1 and 2 sharing many items, assert
   `len(render) <= RENDER_MAX_CHARS + header slack`. This catches the D4 arithmetic.
9. Promotion: when the round-1 full copy is dropped, the round-2 block holds the full
   line and the round-1 header's shown count falls by one.
10. `?`-ref items with identical text are both rendered in full.

## 6. Verification

1. **Go/no-go before the plan (no model calls):** take real per-turn envelopes from the
   search line's in-process orchestrator eval, with their new windowing applied. Count
   the byte-identical repeats per tool pair, the render chars before and after, and the
   items `_budgeted` drops before and after. If repeats free under ~5% of the budget on
   the multi-call turns, stop: the change is not worth its complexity, and the finding
   goes back to the search line as "no win here".
2. Unit tests plus the full `tests/unit` suite.
3. Live on TEST: 6 questions known to take 3 or more tool calls, biased toward
  re-asking a day already shown (the `day_overview` repeat is where stubs appear), about 6–12 judge calls
  plus about 6 retrieval embeddings. The count is stated before running. Check that answers
  still cite refs that were stubbed in later rounds, and that the cards resolve.

### 6.1 Gate result (2026-10-22)

Data: the search line's replay dumps of 90 turns per family (45 gold questions × 2) on
prod data. Tool calls are held fixed; arms are before (600 / 10,000) and after
(WINDOW_CHARS=1000, MAX_RESULT_CHARS=12,000). Computed with the real `_item_line` /
`_header` and a prototype of D4 (`size()` recomputed per pop).

| arm | multi-call turns | turns with a repeat | budget freed: mean / p90 / max | `_budgeted` drops, before → with dedupe |
|---|---|---|---|---|
| session, before | 72 | 59 | 9.3% / 18.4% / 28.9% | 0 → 0 |
| report, before | 76 | 70 | 11.1% / 19.9% / 41.6% | 0 → 0 |
| session, after | 72 | 56 | 9.9% / 24.3% / 34.8% | 3 → 0 |
| report, after | 76 | 67 | 12.1% / 26.0% / 37.2% | 16 → 0 |

Repeats, after arm: session has 261 `search_records → search_records`, 30
`search_records → latest_activity` and 5 the other way. Report has 284 / 59 / 5. So centred
windows still land on the same span; §1's worry that windowing would kill chunk repeats
did not hold. The mean saving of ~3.5K chars is about 20–25% of the median render (13.9K
session, 17.2K report), which also cuts judge prompt size and latency. The drop count
matches the search line's metric on 179 of 180 turns. Decision: go.

## 7. Risks

- The judge could treat a stub as an empty item. Mitigated by D3 (the full copy is
  always earlier in the same conversation) and checked in §6.
- Byte-identical comparison misses near-duplicates (for example a different `date`
  field). That is acceptable: it falls back to today's behaviour.

## 8. Review log

- rev 1: draft.
- rev 2 (Fable): D4 recomputes the total after every pop (HIGH); §1 repeat shapes
  corrected (chunk vs topic kinds); replay made an unconditional go/no-go gate; stub
  wording "shown earlier"; the line itself is the dedupe key; tests 8–10 added.
- rev 3: gate passed (§6.1).
