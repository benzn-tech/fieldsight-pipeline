Report / extraction prompt x model bench (offline tooling; bench-only, nothing in src/ changes).
Spec: docs/superpowers/specs/2026-10-28-report-extraction-prompt-model-bench-design.md (rev 2.1).
Plan: docs/superpowers/plans/2026-10-28-report-extraction-bench-p0.md.
The owner runs every live / keyed step (AWS read, OpenRouter, Anthropic); P0 tests use fakes only.
Modules are named rb_* so they never collide with tools/web-answer-bench modules in one interpreter.

Rules that hold for every step
- --out (and the snapshot cache) must be outside the repo and outside Dropbox; every command refuses otherwise.
- Prompt, transcript and answer text live only under --out; stdout carries ids, counts, hashes, dollars.
- Never run this bench and the web bench at the same time: they share <out>/anthropic_ledger.json
  (read-modify-write). rb_run.py holds <out>/bench.lock while it runs; do not start the web bench while
  that file exists (delete it by hand only if no bench is running).
- Anthropic pools in the shared ledger: report $40 (held-out reserve $9; pilot + round 0 + rounds 1-2 stop
  at $31); web $45 untouched; global $85.
- Every live run: --dry-run first, then --confirm-calls N with N = the "to make" number it printed.

Interpreters
  PY  = C:/Users/camil/wt/benchenv/Scripts/python.exe   (live steps: has the anthropic SDK)
  OUT = a directory outside the repo and Dropbox, e.g. C:/Users/camil/wt/report-bench-out
  CACHE = C:/Users/camil/wt/benchcache                   (git-archive snapshots of src/)
  Keys (environment, owner): QWEN_API_KEY = the OpenRouter key (as llm_utils._call_qwen reads it);
  ANTHROPIC_API_KEY for Claude; AWS: a read-only profile.

P1 sequence (owner)
 1. Edit tools/report-bench/owner_folders.json: check the folder names (test3 = Ben_Lin_Test?), set
    "confirmed": true.
 2. Write sessions.json (outside the repo): [{"folder", "date", "session_base", "bucket",
    "role": "set"|"heldout"}] - 4 set sessions (dense > 40 min, short, mixed zh/en, the task-admission
    meeting sid93396a6ac8434fdf908c25a50cc7e167) + 2 held-out.
 3. $PY tools/report-bench/rb_collect.py --sessions sessions.json --out $OUT --profile <read-only profile>
    Optional: <OUT>/inputs/X/<session>.names.json = the artifact's speakerNames rows, if it has them.
 4. $PY tools/report-bench/rb_collect.py --debug-cmd --bucket <bucket> --date <day> --folder <owner folder> --out $OUT
    then run the printed `aws s3 cp` line (owner folder only, never _summary).
 5. $PY tools/report-bench/rb_template_check.py --debug $OUT/inputs/R/<folder>_<date>.debug.json --out $OUT
    exit 0: use config/prompt_templates.json as --r-template; exit 2: reconstruct the served template
    into a prompt_templates.json-shaped file and use that (record its sha256).
 6. $PY tools/report-bench/rb_prerender.py --out $OUT --cache $CACHE --tasks X R --r-template <file>
 7. $PY tools/report-bench/rb_gold.py blind --out $OUT --session <sid>        (each of the 4 set sessions;
    add --prefill-task-admission for sid93396a6...). Open the page, list items, Save; put the file at
    $OUT/gold/gold_<sid>.json. BEFORE any model output is looked at.
 8. $PY tools/report-bench/rb_run.py --phase pilot --task X --out $OUT --dry-run
    $PY tools/report-bench/rb_run.py --phase pilot --task X --out $OUT --confirm-calls 6
 9. $PY tools/report-bench/rb_gold.py pool --out $OUT --phase pilot --session <pilot sid>
    (> 60 rows for a session: two-rep rows come first, the rest behind "show the rest").
10. Round 0, extraction first:
    $PY tools/report-bench/rb_run.py --phase round0 --task X --out $OUT --dry-run    then --confirm-calls N
11. Pin T's input (muse X-cur rep 1 per session) and pre-render T:
    $PY tools/report-bench/rb_prerender.py --out $OUT --pin-tcur $OUT/runs/pilot.jsonl $OUT/runs/round0.jsonl
    $PY tools/report-bench/rb_prerender.py --out $OUT --cache $CACHE --tasks T
12. $PY tools/report-bench/rb_run.py --phase round0 --task T R --out $OUT --dry-run  then --confirm-calls N
    Exit 3 = the report pool stopped Claude; the summary names the cut cells (accepted outcome).
13. Adjudicate per session (and per R day, key <folder>_<date>):
    $PY tools/report-bench/rb_gold.py adjudicate --out $OUT --phase round0 --scope <sid or day>
    open the page, Save verdicts, then
    $PY tools/report-bench/rb_gold.py apply --out $OUT --phase round0 --scope <sid or day> --verdicts <file>
14. $PY tools/report-bench/rb_score.py --out $OUT --phase round0
15. $PY tools/report-bench/rb_select.py --out $OUT underperforming
P2
16. Write <= 2 edits per underperforming (task, model): tools/report-bench/edits/<task>/<model>/e1.json
    {"base": "<candidate>", "why": "...", "replace": [[old, new], ...]}; then
    $PY tools/report-bench/rb_select.py --out $OUT edits
    $PY tools/report-bench/rb_run.py --phase rounds12 --out $OUT --dry-run / --confirm-calls N
    adjudicate new unmatched items (step 13 with --phase rounds12), score --phase rounds12.
17. $PY tools/report-bench/rb_select.py --out $OUT freeze [--keep keep.json]   -> frozen.v1.json
18. Held-out: rb_run.py --phase heldout --task X ..., then --pin-tcur $OUT/runs/heldout.jsonl,
    rb_prerender --tasks T, rb_run.py --phase heldout --task T R ...; score --phase heldout; the
    held-out numbers go into frozen.v1.json alongside (reported as-is, no further selection).
