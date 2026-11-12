# Plan: site-wide answers in General (spec 2026-11-13-multi-unit-walk-site-wide.md)

Branch `feat/walk-site-wide` off develop. One PR to develop. No migration, no flag.
TDD for every task: failing test, code, green.

1. `walk_checklist._general` keeps `n` and `answer`; duplicates "answered twice";
   General ALL dropped OUTSIDE; `walk_report.general_section` shows item: answer --
   comment for numbered rows.
2. Selector: `_first_mark_place`, `_site_wide(general_rows, text, first_place,
   stretches)` with the guards (spec review item 4). Unit tests for every case in
   item 7.
3. Link in `build`: General computed first; `_unit_section` takes the map, links
   not-checked rows to state `site_wide` / origin `general` before `_apply_all_good`;
   shared `recount(unit)` helper recomputes answered / gaps / evidence; `render` and
   `summary` ("(G)") handle the state.
4. Overlay: base None for site_wide; [base, site, fallback]; `_draw` fallback with
   evidence; `_moved` replaces non-answered targets; move-source reset keeps the
   fallback; `_recount` uses the shared helper.
5. check_done: `walk_questions` skips site_wide; tests for `_walk_result` and
   `open_from_result`. Contract doc: one sentence.
6. `lambda_session_report` trace `site_wide` count.
7. Offline replay: `build` + `walk_questions` on run1/run2 walk text -> 59 -> ~39
   questions; Unit 2 silt fence "No (G)".
8. Full unit suite, PR, CI, merge, TEST deploy, re-inject EL walk.
