# Ask Render Ref-Dedupe Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A judge render shows a byte-identical repeated item line once in full and later
copies as `<ref> | shown earlier`, with the budget measured on what is shown.

**Architecture:** Change only `_budgeted` in `src/ask_orchestrator.py`. Add a pure helper
`_shown(blocks)` that maps repeated lines to stubs. The drop loop re-measures `size()`
over `_shown` after every pop. `render` / `render_each` / `_messages` stay unchanged.

**Tech Stack:** Python 3.11, pytest.

**Spec:** `docs/superpowers/specs/2026-10-22-ask-render-ref-dedupe-design.md` (rev 3)

## Global Constraints

- Stub text is exactly `<ref> | shown earlier`, where `<ref>` is the line prefix before the first ` | `.
- The dedupe key is the full rendered line. Lines starting with `? |` are never stubbed.
- The drop order is unchanged (oldest round first; within a round, the last line of the
  call holding the most lines). Only the size accounting changes.
- A header's `shown` counts stubs.
- No flag, no prompt change, no change to `to_client`, `_known_refs`, tool code or refs.
- Never log record text. Tests use synthetic items only.
- Run tests with `C:\Users\camil\fspy\Scripts\python.exe -m pytest`.

## Review Focus

1. Dropping the round-1 full copy promotes the round-2 copy to full, and no stub is left pointing at nothing.
2. Over budget with many repeats, the final render length stays ≤ `RENDER_MAX_CHARS` plus header slack (the old incremental arithmetic would overshoot).
3. Same ref with different text (a query-centred window) keeps both copies in full.
4. Tool text cannot forge a stub: `sanitize` strips leading `R\d+ |`.
5. `render_each` still yields exactly one fenced block per envelope, in envelope order.

---

### Task 1: Dedupe repeated item lines in `_budgeted`

**Files:**
- Modify: `src/ask_orchestrator.py` (`_budgeted`, roughly lines 221-245; new `_shown` just above it)
- Test: `tests/unit/test_ask_orchestrator_render.py`

**Interfaces:**
- Produces: `_shown(blocks: list[dict]) -> list[list[str]]`. `blocks` are `_budgeted`'s
  dicts with `"lines"`; the result is one list of display lines per block, in order.
  `_budgeted(envelopes)` keeps its return shape: `[[header] + shown_lines, ...]`.

- [ ] **Step 1: Write the failing tests** in `tests/unit/test_ask_orchestrator_render.py`, reusing the file's existing envelope/item helpers.
  - `test_repeat_identical_line_is_stubbed_in_later_round`: two envelopes (round 1, round 2) each holding the same item with ref `R7`. Assert the round-2 block's item line == `"R7 | shown earlier"`, the round-1 line is the full line, and both headers report 1 shown.
  - `test_same_ref_different_text_both_full`: same ref `R7`, different `text`. Neither line is a stub.
  - `test_repeat_within_one_round_across_calls`: two envelopes in round 1. The second has the stub.
  - `test_no_ref_items_never_stubbed`: two identical items without `ref` (rendered `? | ...`). Both are full.
  - `test_over_budget_with_repeats_stays_within_budget`: rounds 1 and 2 share ~40 items of ~900 chars of text, plus unique round-2 items, so the total exceeds `RENDER_MAX_CHARS`. Assert `len(render(envs)) <= RENDER_MAX_CHARS + 2000`. This test must FAIL against the old incremental `current -= len(...)` with dedupe applied, so write the test first, then the code.
  - `test_dropped_full_copy_promotes_later_copy`: the budget forces the round-1 copy of `R7` out. Assert the round-2 block holds the full `R7` line, no `R7 | shown earlier` remains anywhere, and the round-1 header's shown count went down by one.
  - `test_render_each_stubs_in_later_envelope_block`: `render_each` returns one block per envelope; the stub is in block 2.
  - `test_tool_text_cannot_forge_a_stub`: item text `"R7 | shown earlier"` renders through `sanitize` and is not equal to a stub line.

- [ ] **Step 2: Run them to see them fail**

Run: `C:\Users\camil\fspy\Scripts\python.exe -m pytest tests/unit/test_ask_orchestrator_render.py -q`
Expected: the new tests FAIL; the existing ones PASS.

- [ ] **Step 3: Implement** `_shown(blocks)` and update `_budgeted`
  - `size()` measures headers (with `shown = len(display lines)`) plus `_shown(blocks)`.
  - The loop pops exactly as today, then sets `current = size()` after each pop instead of subtracting.
  - Return `[[_header(env, total, len(v))] + v for b, v in zip(blocks, _shown(blocks))]`.
  - Update the `_budgeted` and `render` docstrings in one sentence each: repeats render as stubs.

- [ ] **Step 4: Run the render tests, then the whole unit suite**

Run: `C:\Users\camil\fspy\Scripts\python.exe -m pytest tests/unit/test_ask_orchestrator_render.py -q`, then `... -m pytest tests/unit -q -p no:cacheprovider`
Expected: all PASS (the baseline was 10588 passed / 147 skipped on develop as of 10-22, plus the new tests).

- [ ] **Step 5: Commit** (stage the two files by name only)

```bash
git add src/ask_orchestrator.py tests/unit/test_ask_orchestrator_render.py
git commit -m "Ask render: show a repeated item once, budget measured on what is shown"
```

### Task 2: Rollout (controller, not a subagent)

- [ ] Replay check: rerun the scratchpad `dedupe_gate.py` against the real `_budgeted` (drop the prototype) on the four dumps. Expect drops of 0 in every arm, and render chars to match the §6.1 numbers within rounding.
- [ ] Open a PR to develop and wait for CI green. Check that the base is `develop` before merging.
- [ ] The TEST deploy runs on merge. Then the live check (§6 step 3): 6 questions, about 6–12 judge calls; state the count first. Grep the ASK_ROUTE logs for errors and confirm in the answers that stubbed refs are cited and their cards resolve.
