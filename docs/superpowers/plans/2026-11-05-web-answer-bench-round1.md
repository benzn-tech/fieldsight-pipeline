# Web-answer bench, round 1 and grader v2 — Implementation Plan

Spec: docs/superpowers/specs/2026-10-27-claude-web-answer-bench-design.md §4.5 (rev 4.3,
Fable review applied as rev 4.3.1).

**Goal:** apply the owner's gold rule for price / forecast unsure items, re-grade round 0
with grader_v2, re-check the grader on a fresh owner draw, then run one edit per incumbent.

## Tasks

1. **Gold v1.1** — `set.v1.jsonl`: t5 / u1 / u2 second expected fact (§4.5), `gold_rev`,
   u1 / u2 `confirmed: false` until the owner ticks them.
2. **grader_v2** — `prompts/grader_v2.txt` = grader_v1 with the unsure rubric lines
   replaced; `grade.py --grader` (default v2), `grader_prompt` on every row,
   `--only-ungraded` treats another grader's rows as pending, `--round` picks
   `grades_r<n>.jsonl` / `cells_r<n>.json`.
3. **Edits** — `prompts.EDITS`, `EDIT_RULE`, `with_rule`, files `<incumbent>_e1.txt`
   (test pins text == incumbent + rule before `## Question`).
4. **Round-1 runner** — `matrix.py --round 1 [--cells-r0]`: `round1_cells` (best round-0
   per (model, step), skip at ceiling, gemini verify only), drafts from
   `matrix_r0_calls.jsonl`, runs `r1-NNNN` into `matrix_r1_calls.jsonl`.
5. **Keep + draw** — `select_freeze.keep_round1` (keep_edit + stability guard),
   `round1_decisions`, `keep` subcommand; `stratified_draw` for the fresh owner check.
6. **Tests** in `tests/unit/test_web_answer_bench_matrix.py` (fakes only).

## Owner run order (after merge)

1. Owner page: tick u1 / u2 gold, give t5's acceptable reference range; label 10 fresh
   blind answers (drawn with `stratified_draw`, the first 10 refs excluded).
2. `run_stage0.sh regrade-v2-dry` → `regrade-v2 <N>` (≈ 330 grader calls, ≈ $0.03).
3. Claude compares v2 labels with the owner's fresh labels (≥ 8 / 10) and reports the
   v1→v2 flip count on non-unsure items.
4. `run_stage0.sh r1-dry` → state calls / $ → `r1 <upper>`; then grade round 1 and
   `select_freeze.py keep`.
