# Plan: Ask tools show the part of a chunk that matches the query

Date: 2026-10-22. Spec: `docs/superpowers/specs/2026-10-22-ask-tools-item-window.md`, as
amended by `-review.md` (BLOCKER 1-3, SHOULD 1-6 are folded in below). Branch
`fix/ask-tools-item-window` off origin/develop 5608895a, worktree `C:/Users/camil/wt/fs-itemwin`.
Eval pieces on the fs-eval worktree `C:/Users/camil/wt/fs-eval` (`tools/retrieval-eval`,
ce6bb1ff; same repo, so the branch is visible to `git archive`).

Rules for every task: TDD (failing test, then the smallest change, then green); targeted
edits, never a whole-file rewrite; Python is `C:\Users\camil\fspy\Scripts\python.exe` only;
no AWS, model or network call before Task 9; nothing is pushed by the implementer, commits
are listed so the owner can see the cut points. Offline tasks 0-8 first; the two live runs
(Tasks 9 and 11) are gated and run by the owner's session.

Test commands (from each worktree root):
- pipeline unit: `C:/Users/camil/fspy/Scripts/python.exe -m pytest -p no:cacheprovider tests/unit/<file> -q`
- fs-eval offline: `EVAL_SRC_REF=fix/ask-tools-item-window C:/Users/camil/fspy/Scripts/python.exe -m pytest -p no:cacheprovider tools/retrieval-eval/tests/askv2 -q`
  (tests/askv2/conftest.py exports `src` at `EVAL_SRC_REF` with `git archive`, so the branch
  must be **committed** before an eval test can see the new `ask_tools`; the default
  origin/main is fine for Tasks 5-6.)

## Task 0 — baseline

Files: none changed.
1. In fs-itemwin run `tests/unit/test_ask_tools_execute.py tests/unit/test_lexical_terms.py
   tests/unit/test_ask_orchestrator_render.py`. Record pass counts.
2. In fs-eval run `tests/askv2` with the default `EVAL_SRC_REF`. Record pass counts.
   `tests/askv2/test_render_parity_pg.py` may need the local Postgres: run it alone via
   `bash /c/Users/camil/fswork/run-tests.sh` (one pytest at a time) before claiming the
   eval baseline green, or record it as skipped.
3. Re-run `winsim.py 600,1000,1200 0` with `PYTHONIOENCODING=utf-8` and keep the line
   `questions 24 head {...} centred {...}` in the PR notes (it is the §2 evidence).
Done when both suites are green at the baseline and the counts are written down.

## Task 1 — `_window_terms(query)`

Files: `src/ask_tools.py`, new `tests/unit/test_ask_tools_item_window.py`.
Tests first:
- `"Why did the roofing subcontractor pull out?"` → `["roofing","subcontractor","pull"]`
  (stopwords and `ASKING_WORDS` gone, order kept, deduplicated).
- `"When did we last talk about the crane?"` → `["crane"]`.
- `"屋面分包商为什么退出了"` → the CJK bigrams, in order, unchanged (no ASCII step).
- `"P3 方案"` → `["p3","方案"]`. `None`/`""`/all-asking → `[]`.
Implementation: `query_terms(query)` minus `lexical_terms.ASKING_WORDS`, dedupe in order.
Import `lexical_terms` at module level (pure, dependency-free: its docstring :12).
Commit: `ask_tools: terms a window centres on`.

## Task 2 — `_window(text, terms, cap, sep, keep_first)` (pure)

Files: `src/ask_tools.py`, `tests/unit/test_ask_tools_item_window.py`.
Signature: `_window(text, terms, cap, sep="\n", keep_first=False) -> (shown, first_unit_index)`.
`first_unit_index` is the index of the first kept unit (0 when nothing was skipped above;
used by Task 3 for `time_start`). Units = `text.split(sep)` (never `splitlines()`).
Fixtures build transcript windows with `session_index._window_line` over turn dicts, so a
format change breaks the test, not prod (spec §8).

Tests first (each one a name in the file):
1. `≤ cap` → returned unchanged, byte for byte (including a trailing `\r`, and CJK).
2. No term hit, text > cap → line-aligned head, `…` as the last line, `len ≤ cap`.
3. Evidence on line 30 of a 2,600-char window (char ~2,000), query names it → that line is
   in the result; result is whole lines; `…` first line; `len ≤ cap`.
4. **Extension (review BLOCKER 1a)**: only line 20 of 40 hits → result is ≥ cap − longest
   neighbouring line, contains lines before and after line 20, not line 20 alone. The
   extension order is deterministic: previous unit, then next, alternately, while it fits;
   when one side stops fitting keep trying the other.
5. Tie on coverage and hits between two regions → the earlier one.
6. Coverage beats hits: a line with `pour` three times loses to a run that has `pour` and
   `crane` once each.
7. **Oversize unit (review BLOCKER 1b)**: 12 lines, line 7 is 1,400 chars and holds the
   only hit → result is a character cut of line 7 around the hit, keeps `[HH:MM:SS] spk_1: `,
   has `…` at both ends, `len ≤ cap`. Same when the whole text is one 3,000-char unit with
   no `\n`. Snap to the nearest space outward; hard cut when no space lies within
   `SNAP_CHARS = 40` (a named constant, CJK text is the case).
   Find the hit with `re.search(re.escape(term), unit, re.IGNORECASE)` on the original
   unit (review NIT 1).
8. `keep_first=True` (topic/digest): line 1 always present and counted against the cap;
   the run search covers lines 2..n; with `…` between when lines were skipped.
9. `sep="\n\n"` (documents): paragraph units; a 1,500-char paragraph with the hit is cut
   as in 7 with no prefix rule.
10. Chinese query over Chinese text centres on the bigram hit; Chinese query over English
    text → head (same as 2). Mixed `P3 方案` hits through `p3`.
11. Property test (fixed seed, 200 random fixtures: unit counts 1-60, unit lengths 5-1,600,
    random ASCII/CJK, random term lists incl. none): `len(shown) ≤ cap` always; every
    unit of `shown` except `…` lines is a unit of `text` (or a cut of one when the chosen
    unit was oversize); result is identical on two calls (determinism).

Algorithm (write it as the docstring):
```
units = text.split(sep); if len(text) <= cap: return text, 0
head = [units[0]] if keep_first else []; body = units[len(head):]; room = cap - len(head_text) - (1 if head else 0)
score(unit) = (set of terms found, hit count) with substring on unit.lower() (CJK) /
              re.IGNORECASE search (Latin) -- precompute per unit
candidates: every run body[i..j] with joined length + markers <= room; every single unit
            longer than room, on its own
best = max over candidates by (len(distinct terms), hits, -i); no terms anywhere -> head rule
if best is a single oversize unit: char-cut it around its earliest hit (prefix kept)
else: extend best alternately (i-1, then j+1, ...) while it still fits with markers
markers: "…" line before when i > 0 (after head), "…" line after when j < len(body)-1
```
Markers count against the cap. O(units²) with ≤ ~60 units; a 4,500-char topic chunk of
100 short lines is 10⁴ inner steps — fine.
Commit: `ask_tools: a window centred on the query's terms`.

## Task 3 — wire `_window` into `_item`, `time_start`, constants, log counts

Files: `src/ask_tools.py`, `tests/unit/test_ask_tools_item_window.py`,
`tests/unit/test_ask_tools_execute.py` (additions only).
Tests first (through `ask_tools.execute` with the file's `FakeLambda`/`_echo`/`_ctx`):
1. `search_records` chunk row (`chunk_type: transcript_window`, 2,600-char
   `_window_line` text, `metadata.window_span`), query naming line 30 → `item["text"]` holds
   it, `len ≤ WINDOW_CHARS`, `item["time"]` is still the span, `item["time_start"]` is line
   30's region first kept line's `HH:MM:SS` (**review BLOCKER 3**).
2. The same row with no hit → head; `time_start` = span start (unchanged).
3. First kept unit without `[HH:MM:SS]` (a turn whose text holds `\n`) → `time_start` =
   span start. Topic chunk and document: `time_start` unchanged (`None`).
4. `topic` chunk (`session_index._topic_lines` fixture, 3,000 chars): line 1 kept.
   `session_digest`: line 1 kept.
5. `search_project_documents` row (`\n\n` paragraphs, 2,800 chars) → centred on the
   paragraph; `document` item has no `time_start`.
6. Topic row (`day_overview` summary 900 chars) → 600 head, unchanged
   (`test_text_capped_at_600` at :226 stays as is). Person, metric unchanged.
7. `latest_activity` with query `"When did we last talk about the crane?"` → the item
   centres on `crane` (ASKING_WORDS removed).
8. Card shape (review SHOULD 4): a windowed item's `text[:200]` ≠ `chunk_text[:200]` and
   starts with `…`; `test_chunk_item_carries_every_field_build_citations_reads` (:196-218,
   900-char chunk ≤ cap) stays green untouched.
9. Log line: `caplog` shows `windowed=1 head=1` on an envelope with one of each (review
   SHOULD 5). Counts only, no text.
Implementation:
- `WINDOW_CHARS = 1000` beside `MAX_TEXT = 600` (:225), with the §5 table in a comment.
- `_item`: chunk branch (:376) and document branch (:355) call
  `_window(text, _window_terms(args.get("query")), WINDOW_CHARS, sep, keep_first)`;
  `keep_first = chunk_type in ("topic", "session_digest")`; `sep = "\n\n"` for documents.
  Non-string `chunk_text` still goes through `_text`'s coercion first.
- `time_start`: only when `chunk_type == "transcript_window"` and `first_unit_index > 0`,
  `re.match(r"\[(\d\d:\d\d:\d\d)\]", first_kept_unit)` → group 1, else today's value.
- `_envelope` writes `meta["windowed"]` / `meta["head"]` counts and the log line at
  :569-573 prints them. Safe because `ask_orchestrator._header` (:125-166) reads only
  named meta keys (`error`, `field`, `empty_reason`, `latest_before`, `truncated`,
  `clamped`, `person_ambiguous`, `person_unresolved`): an unknown key never reaches the
  judge. Add a render test asserting the two keys do not appear in `_header`'s output.
Commit: `ask_tools: items show the part of a chunk that matches the query`.

## Task 4 — `MAX_RESULT_CHARS` provisional 12,500, comments, contract text

Files: `src/ask_tools.py:220-224`, `tests/unit/test_ask_tools_execute.py`,
`docs/superpowers/specs/2026-10-15-ask-retrieval-tools-design.md:192,221,356`.
Tests first:
- `test_eight_windowed_items_fit_one_envelope`: 8 chunk rows, each 2,600 chars with a hit
  so every item is a full `WINDOW_CHARS` window, with the **longest realistic** fields:
  `source` = `extractions/<36-char uuid>/2026-09-10/<40-char session id>_extraction.json`,
  `site_name` 40 chars, `user_name` 30, `topic_title` 80, both ids 36-char UUIDs →
  `len(items) == 8`, `truncated is False`, `_size(items) ≤ MAX_RESULT_CHARS`. Parameterise
  the overhead so Task 10 can re-pin it from the measured p95 (review SHOULD 1).
- :279/:289/:419 keep asserting against the constant; no literal 10,000 remains.
Implementation: `MAX_RESULT_CHARS = 12_500`; comment at :220 says ≈3,100 tokens and
why; the three lines in the tools design spec say 1,000-char windows / 12,500. State in
the comment that the number is provisional until Task 10.
Commit: `ask_tools: result budget 12,500 so eight windowed items fit`.

## Task 5 — fs-eval: rename `cut_600_cap` → `cut_text_cap`

Files (fs-eval): `askv2/funnel.py:124-128`, `tests/askv2/test_funnel.py:88-90`,
`docs/2026-10-22-ask-v2-mode-spec.md:38`, `docs/2026-10-22-ask-v2-mode-plan.md:181`.
Test first: rename `test_600_cap` → `test_text_cap`, expect `"cut_text_cap"`. Then the
code and the two doc lines (the comment at :124-127 says "past the item's text cap"
instead of "past MAX_TEXT"). `report.summarise` groups by label dynamically (:40), so
old and new result files still diff; note in `--diff` output that the label changed on
2026-10-22.
Commit (fs-eval): `retrieval-eval: cut_600_cap is cut_text_cap`.

## Task 6 — fs-eval: `--keep-rows DIR` and the post-search record

Files (fs-eval): `run_ask_v2.py`, `askv2/harness.py`, `tests/askv2/test_cli.py`,
`tests/askv2/test_harness.py`.
Tests first:
1. `parse_args` refuses `--keep-rows` under `results/` (same message style as :171-173).
2. `TurnRecord` gains `post_search = []`; `ToolsProxy.execute` wraps
   `ask_tools._post_search` for the duration of the call (restore in `finally`, as
   `_Patches` does) and appends `{"batch", "call_id", "name", "ids": [row ids in order]}`
   (**review BLOCKER 2**). Test with a fake `_post_search` returning rows reversed.
3. `run_turn(..., keep_rows=list)` appends per turn: `id`, `expect`, `date`, `turn`,
   `batches` (calls with args, `judge_calls_before`, and each envelope's `tool`, `id`,
   `meta`, items as `{ref, kind, chunk_id|topic_id, date, text_len}` — no text),
   `invokes` (the raw `rec.invokes`, rows included), `post_search`.
4. `main` writes `rows-<label>.json` to the directory (mirror of the `--keep-renders`
   block at :321-326), `ensure_ascii=False`.
Note in the docstring that rows hold transcript text: scratch only, never `results/`.
Commit (fs-eval): `retrieval-eval: --keep-rows dumps what the tools were given`.

## Task 7 — fs-eval: `replay_window.py` (offline, no model, no Data API)

Files (fs-eval): new `tools/retrieval-eval/replay_window.py`, new
`tests/askv2/test_replay_window.py`, a small synthetic dump fixture under `tests/askv2/`.
Usage: `replay_window.py --rows rows-before-session.json --gold gold/v1.1.jsonl
--src-ref <ref> --window 1000 --result-chars 12500 --out <scratch>/replay-<ref>-W.json`.
Run once per arm (`--src-ref 5608895a --window 600 --result-chars 10000` is the baseline;
the branch ref at 800/900/1,000/1,200). One subprocess per src ref, each calling
`harness.prepare_imports(ref, family)` so `ask_tools`' lazy imports resolve from that
export (review: two `ask_tools` versions never share one process). A `--compare A B`
mode diffs two outputs.
Per turn it:
1. Rebuilds each envelope with the arm's `ask_tools._envelope(name, call_id, args, rows,
   meta, ctx)` from the **post-search** rows (`post_search.ids` mapped into `invokes`
   rows; `day_overview`, `search_project_documents` (`rows[:k]`, ask_tools.py:560-561, no
   post-search), people and metrics from the invoke rows directly), with a fresh
   `RefAssigner` per turn, `WINDOW_CHARS`/`MAX_RESULT_CHARS` set from the flags on the
   imported module.
2. Tags envelopes with `args` and `round` (round = `judge_calls_before + 1`, as
   `run_ask_v2._calls_by_round`), appends them in order and calls
   `ask_orchestrator._budgeted` on the whole list, as the final judge call would see it.
3. Scores with `funnel._patterns` and the funnel's `hit` rule (import, do not re-spell):
   `evidence_in_item` (an item of the gold date matches), `evidence_in_render` (a kept
   `_item_line` of the gold date matches), per answerable question.
4. Reports: evidence-in-item count and the per-question flip list vs the baseline arm;
   render lines dropped (raw), turns with a drop, **evidence-bearing drops**
   (review SHOULD 2), rendered chars median/p90, envelopes `truncated`, per-item overhead
   (`_size(item) - len(text)`) max/p95 (review SHOULD 1), and the zh/mixed questions'
   outcome separately (review NIT 1).
Tests: the synthetic dump has three turns (a head-cut miss that the window recovers, a
turn that never had evidence, a turn whose baseline envelope was truncated) and asserts
each number; a determinism test (two runs, identical JSON).
Commit (fs-eval): `retrieval-eval: offline replay of the item window over kept rows`.

## Task 8 — branch hygiene before the live runs

1. fs-itemwin: full `tests/unit` green; `git diff --stat` shows only `src/ask_tools.py`,
   the two test files, the three spec lines and the two docs under `docs/superpowers`.
2. fs-eval: `tests/askv2` green with `EVAL_SRC_REF=fix/ask-tools-item-window` (needs the
   branch committed; Task 3-4 commits exist).
3. Hand the owner the two gated steps below with the exact commands.

## Task 9 — [GATED, the main session runs it] before-arm ruler with rows

`run_ask_v2.py` at `--src-ref 5608895a`, both families, `--repeat 2`, `--keep-rows
<scratchpad>/itemwin-rows`, the usual `--max-judge-calls/--max-tokens` from the pilot.
This run **is** the §6.2 before arm; nothing extra is spent on it. Outputs: two
`results/*.json` (before) and two `rows-*.json` (scratch).

## Task 10 — offline replay fixes W and the budget

1. Run `replay_window.py` per family: baseline arm (5608895a, 600/10,000) and the branch at
   800/900/1,000/1,200 with 12,500; `--compare` each against the baseline.
2. Decide by the review's gate: evidence-bearing render drops = 0 and evidence-in-item for
   the `cut_text_cap` questions ≥ 15/19 (session) with no unexplained regression, report
   family recovers v1-03. Largest W that passes; ladder 1,000 → 900 → 800. If drops come
   from `day_overview` at 12,500, keep 10,000 for non-windowed tools (per-tool table)
   instead of lowering W.
3. Set `MAX_RESULT_CHARS` from the measured overhead p95: `8 × (p95 + W + 2) + 2`, rounded
   up to 500; re-pin Task 4's fixture to the observed max `source`/`title` shape. If the
   number exceeds ~13,000, switch `_size` to the rendered fields (drop `source`, ids) and
   update the tools design spec §3.2 instead.
4. Read the flip list for the 7 zh/mixed questions; a stem- or boundary-caused miss is the
   only reason to revisit Q3/NIT 2.
5. Commit the final constants with the replay numbers in the message.

## Task 11 — [GATED, the main session runs it] after-arm ruler, then the PR

`run_ask_v2.py --src-ref fix/ask-tools-item-window` (committed), both families, `--repeat 2`,
same `--now` as Task 9. `--diff` against Task 9's files. Gates (review SHOULD 3): `seen`
and `cited` up on both families by more than the run's `min_detectable_delta`;
`cut_render_budget` 0; median prompt tokens ≤ +35%; judge ms reported, not gated; controls
gain no citations. Then the develop PR with: the §2 winsim line, the replay table (W ladder,
drops, overhead p95), the before/after funnel, the note that documents are untested on gold,
and the Q4 pointer for the orchestration line (repeated-ref re-render). Rollout per spec §9:
TEST deploy, one English and one Chinese Ask e2e, citation click lands on the shown line
and the card snippet begins with `…` when windowed.
