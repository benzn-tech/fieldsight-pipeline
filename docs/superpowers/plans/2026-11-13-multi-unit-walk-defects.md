# Plan: multi-unit walk defects D1-D4 (spec 2026-11-13-multi-unit-walk-defects.md)

Branch `fix/multi-unit-walk-defects` off develop. One PR to develop. No migration, no flag.

1. D2 reason, TDD: test that a two-word quote is dropped with "evidence too short" in
   `checklist.rebuild`, `walk_checklist._general`, `walk_checklist._all_good`; add
   `checklist.EVIDENCE_TOO_SHORT` and `checklist.evidence_reason(quote, words)`
   (None | too short | not in transcript), used by all three.
2. Prompt sentences, TDD on the strings: `checklist.shape_sentence` and
   `walk_checklist.shape_sentence` gain the three-word evidence and unconfirmed-claim
   sentences; the walk sentence gains the back-reference scope. The extraction UNITS
   bullet gains the `resumed` sentence. Update any snapshot or prompt-text tests that
   pin these strings.
3. D3, TDD: `walk_report.corrections_block(answers)` -> str or "" (moves and
   confirm_no overrides only, at most 30 lines x 200 chars, fenced as data).
   `lambda_session_report`: when `walk` has answers, append the block to the transcript
   text passed to `render_prompt`. Test: the block reaches the prompt for a walk with a
   move, and is absent with no answers or for an ordinary check.
4. Replays (walk call and final-check prompt), 5 each, against the spec's pass
   criteria. If a criterion fails, adjust the wording once and re-measure; if it still
   fails, report it rather than tune further.
5. Full unit suite, PR, CI, merge to develop, TEST deploy, then re-inject the EL wav on
   TEST, run the final check and the answers, regenerate, and compare.
