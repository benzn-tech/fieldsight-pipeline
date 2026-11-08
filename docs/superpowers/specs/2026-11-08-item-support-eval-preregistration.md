# item_support measurement: pre-registration

Date: 2026-11-09. Spec: `2026-11-08-track-c-item-history-and-evidence-chain-design.md` §7 and §9.
Harness: `scripts/item_support_eval/`. Code under test: `src/item_support.py`.

This document is written and committed before any harness output exists. It is frozen when the
first result row exists. Any later change goes in a dated addendum at the end, and the addendum
says whether it was written before or after the result it might affect.

## 1. What is measured

`item_support.attach_support` links each final item (action, finding, decision, question) to one
transcript turn, or says why it links none (`none_close`, `ambiguous`, `below_floor`). An item
whose status is `none_close` gets `claim_type = 'inferred'`. Three constants are fixed here, on
the fit half: `FLOOR`, `MARGIN`, `NOTHING_CLOSE`. The bars below are judged on the held-out half.

## 2. Sets

- **Source:** the TEST bucket, `extractions/{folder}/{date}/{base}.json`, read-only.
- **Eligible session:** the extraction's `tier` is `final`; its date folder is on or after
  2026-10-01; and at least one topic has an evidence entry with status `verified` or
  `verified_fuzzy` ("with evidence").
- **Turns:** rebuilt now with `lambda_extract_session.gather_session_segments` and
  `assemble_session_turns`, the same calls the final pass makes. The session date passed to
  `attach_support` is the date folder.
- **Minimum:** at least 40 eligible sessions. With fewer, the overall verdict is `insufficient`.
- **Split:** sessions are sorted by `sha256("item-support-eval-2026-11-09:" + folder/date/base)`.
  The first `ceil(n/2)` are the fit half; the rest are the held-out half. Seed:
  `item-support-eval-2026-11-09`.
- Prod is read only if the owner provides it. Not part of this registration.

## 3. Gold

- **Unit:** one item. Item reference = session reference, topic index, list name, child index.
- **What the labeller sees (the topic's turns):** the item text and kind, and the turns that are
  either cited by one of the topic's `verified`/`verified_fuzzy` quotes, or whose start lies in
  the topic's `time_range` widened by 2 minutes on each side. Each turn is shown with its
  whole-session index, speaker label, clock time and text, in time order. The labeller never
  sees a score, a status, a candidate path or any other heuristic output.
- **Answer:** one turn index; or `several` with the list of turn indices that each say it; or
  `none`.
- **Labellers:** a blind agent labels every item in both halves (the fit needs gold too). The
  owner labels, with the same blind view, a random sample of 45 held-out items, seed
  `item-support-owner-2026-11-09`. Then, for every item in that sample where the agent's answer
  and the heuristic (at the fitted point) disagree, the owner is shown both and decides. The
  owner always wins. Gold for an item = the owner's last answer, else the agent's.
- **Correct link:** the linked turn is the gold turn, or one of the `several` turns, or is the
  turn just before or after one of them (index ±1), has the same speaker label, and contains
  every item token that the gold turn contains ("same words", tokens from
  `item_text_match.tokens`). A link on an item whose gold is `none` is wrong.
- **Supported (for `inferred`):** gold is a turn or `several`.

## 4. Fit (fit half only)

- **Grid:** `floor ∈ {0.30, 0.35, …, 0.90}` (13 values), `margin ∈ {0.05, 0.10, 0.15, 0.20}`,
  `nothing_close ∈ {0.10, 0.15, 0.20, 0.25}`. 208 points, all run with `PATHS = ("quote", "window")`.
- **Constraint:** fit-half link precision ≥ 0.97 and fit-half `inferred` precision ≥ 0.95 (point
  estimates). Precision with nothing to count (0 links, or 0 `inferred`) satisfies its constraint.
- **Choice:** the feasible point with the highest fit-half coverage (linked items / all items).
- **Ties:** higher floor, then higher margin, then higher nothing_close (more items get an
  `inferred` answer, since the constraint already holds).
- **Missing gold:** a grid point where any linked or `inferred` fit item has no gold is not
  feasible (the agent labels every item, so this only happens on an unfinished label file).
- **No feasible point:** the constants stay `None`, the verdict is `fail`, and nothing is held out.
- The chosen point and its fit numbers go into `item_support.py` by a reviewed commit, with the
  numbers in the commit body.

## 5. Held-out bars (all gated bars must pass before `EMIT_ITEM_SUPPORT` is on anywhere)

1. **Link precision.** One-sided 95% Clopper–Pearson upper bound of wrong/accepted ≤ 5%. Fewer
   than 59 accepted links → `insufficient` (59 is the smallest n where 0 errors clears 5%). With
   59 or more, pass iff the bound ≤ 0.05. A link to an adjacent same-speaker turn carrying the
   same words counts as correct (§3). Any accepted link without gold makes this bar
   `insufficient` (bar 5 likewise for an `inferred` item without gold); an unlabelled item no bar
   reads changes nothing.
2. **Twin hard negatives = 0.** A mechanical check flags any accepted link where the item and the
   linked turn both carry a token of the same place class with different values. Classes
   (text NFKC-normalised, English lower-cased):
   - `unit|level|lot|room|block|floor` + number (`level` and `floor` are one class);
   - east/west/north/south, and 东/西/南/北 (one direction class, Chinese mapped to English);
   - `\d+楼`, `\d+层` (in the `level` class).
   Every flag is shown to the owner with the item and the turn. A confirmed flag fails the bar.
   An unanswered flag makes it `insufficient`. No flags → pass.
3. **Run-to-run.** Two runs at the fitted point give byte-identical outputs (sha256 of the run
   files).
4. **Agent–owner agreement** on the owner's blind sample ≥ 85% (same answer: same turn, or the
   same `several` set, or both `none`). No sample → `insufficient`.
5. **`inferred` precision.** One-sided 95% CP upper bound of (gold says a turn supported it) /
   (labelled `inferred`) ≤ 10%. Fewer than 20 such items → reported only, and the bar does not
   enter the overall verdict.
6. **Per path.** Each candidate path (`quote`, `window`) is judged on bars 1 and 2 separately,
   over the accepted links it made (`candidate_path`). A path ships only if both pass for it. A
   path that fails, or is `insufficient`, is disabled in `PATHS`. If exactly one path survives,
   the held-out half is run again with `PATHS` set to that path alone, and bars 1–5 are judged
   on that run (its candidate set differs: the window path now meets items the quote path used
   to take). No surviving path: `fail` if either path failed, else `insufficient`.

**Overall verdict:** `fail` if any gated bar fails; else `insufficient` if any gated bar is
insufficient, or there are fewer than 40 sessions, or the scored run's `PATHS` is not the set of
paths bar 6 keeps (the single-path rerun is still owed); else `pass`.

## 6. Reported, not gated

- Coverage: all, per kind, per language (an item with any CJK character is `zh`, else `en`), per
  path, per topic `evidence_status`.
- `claim_type` distribution on TEST. Offline, with no speaker names, only `inferred` vs unknown is
  derivable (`claim_type.derive(support, None, None)`); `observed`/`stated` are reported as "not
  measured" unless a TEST in-VPC count is supplied.
- `find_items` p50/p95 latency on the largest TEST site, through a TEST invoke only (a local DB
  copy is not allowed). Otherwise "not measured".
- Status counts (`linked`, `none_close`, `ambiguous`, `below_floor`), and the count refused by the
  distinguishing-token guard.
- Items whose labeller view was empty (no cited turn, no turn in the widened range).

## 7. Coverage decision (owner, §9.2)

Held-out coverage < 60% → open a separate pre-registration for the topic-quote-index prompt
option. At or above 60%, the prompt stays unchanged.

## 8. Amendments before any result (2026-11-09)

These record what changed in the code or the rules after the plan was written and before any
result existed.

- **A1. Runner-up over the union.** `item_support` takes the runner-up for the margin over the
  union of quote and window candidates, so a cited turn cannot win over an uncited, equally good
  turn in the window.
- **A2. Distinguishing-token guard.** A link is refused when an item digit token or direction word
  (east/west/north/south/left/right, 东西南北左右) is missing from the turn: status
  `below_floor`, `guard = distinguishing_token`.
- **A3. `MIN_ITEM_TOKENS = 2`.** A shorter item is `below_floor` whatever it scores.
- **A4. `PATHS` constant** `("quote", "window")`. A disabled path still supplies runner-ups.
- **A5. Constants are `None`** until this harness fixes them; `attach_support` refuses `None`.
- **A6. Bar 2 also flags Chinese numerals.** `一二三四五六七八九十两` (and digits) directly before
  `号/楼/层/区/栋` are a place token: `楼`/`层` in the `level` class, `区`/`栋` in the `block`
  class, `号` its own class. Chinese numerals are read as numbers, so `三楼` and `3楼` are the same
  value. The bar catches these whether or not the code guard does yet (the guard for them lands in
  the final fix wave).
- **A7. Bar 2 also flags a single capital letter** after `unit|level|lot|room|block|floor`
  (`Block C` vs `Block D`). Neither the guard nor the verbatim bar covers letters; the flag goes
  to the owner like any other.
- **A8. Tie-break completed** past "higher floor" (§4), so the fit has exactly one answer.
- **A9. Gold view widened by 2 minutes** (§3), so a labeller can name a turn the heuristic could not
  see. This can only make bars 1 and 5 harder.
- **A10. Bar 6 re-run rule** (§5.6) for a single surviving path.
- **A11. Missing gold** makes a fit point infeasible (§4), and makes bar 1 (accepted links) or
  bar 5 (`inferred` items) `insufficient` (§5).

## 9. Known costs, reported and not gated

- An exact cited turn plus an identical uncited repeat in the window is `ambiguous` (the runner-up
  ties it). Lost coverage, not a wrong link.
- The guard refuses `东西` ("things"), because it contains two direction characters.
- Bar 5's arithmetic: with 0 supported items, the CP bound clears 10% only from 29 `inferred`
  items. 20–28 `inferred` items with 0 supported fail as written.
- Bar 1 below 59 accepted links is `insufficient` even when it already holds errors.
- Turns are rebuilt from today's transcripts, which can differ from the ones the final pass saw
  (a re-transcribed segment). The labeller and the heuristic see the same rebuilt turns.

## Addenda

(none)
