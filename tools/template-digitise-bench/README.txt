Template-digitise model bench (offline tooling; nothing in src/ changes).
Spec: docs/superpowers/specs/2026-10-31-model-scope-and-config-update-design.md (rev 2) Part B1.
Plan: docs/superpowers/plans/2026-10-31-model-scope-and-config-update.md Task 4.
A screen, not a tail estimate (n = 3 reps x 4 image inputs). The owner runs every keyed step.

Rules
- --inputs / --out (and convert.ps1 -Out) outside the repo and outside Dropbox; every command refuses
  otherwise. Item text lives only in <out>/arms/ and <out>/probes/; stdout and summary.json carry ids,
  counts, seconds and dollars.
- The key is an OpenRouter key file, required for a live run (no env fallback). Owner 2026-10-31: the
  prod key, written to a file from the prod Lambda env; the --openrouter-cap pool is then the only
  guard, so check the prod balance first. Printed only as sha256[:8].
- OpenRouter dollars: pool "openrouter" of --ledger (pass the benches' shared anthropic_ledger.json so
  the $10 --openrouter-cap covers all three benches; default <out>/anthropic_ledger.json). The dry run
  exits 2 when its upper is over the room left; each read is checked before and charged after
  (usage.cost, else the pinned 2026-10-31 price, never $0).
- Until PR-2 (feat/claude-55-and-digitise-knobs) is on the checkout, src ignores
  TEMPLATE_DIGITISE_REASONING / _PROVIDER: run.py reads that from the source (call_llm_parts takes
  reasoning= and provider=, template_vision reads the env) and refuses every arm but gemini_shipped
  ("needs PR-2"). Run the real bench on a checkout that has PR-2.

Interpreters / dirs
  PY  = C:/Users/camil/wt/benchenv/Scripts/python.exe   (Pillow, pymupdf, numpy; not fspy)
  IN  = e.g. C:/Users/camil/wt/digitise-bench/inputs
  OUT = e.g. C:/Users/camil/wt/digitise-bench/out1     (a fresh dir per run)
  KEYFILE = the bench-only OpenRouter key file

Sequence (owner)
 1. Word -> PDF (Word COM, source opened read-only; Bypass because this machine disables scripts):
    powershell -NoProfile -ExecutionPolicy Bypass -File tools/template-digitise-bench/convert.ps1 -Src "C:\Users\camil\Dropbox\ADE\12.15 - Site Establishment Checklist (BICL).docx" -Out $IN -Name bicl
    powershell -NoProfile -ExecutionPolicy Bypass -File tools/template-digitise-bench/convert.ps1 -Src "C:\Users\camil\Dropbox\ADE\Penina Site Inspection Checklist.docx" -Out $IN -Name penina
 2. Pages (spike settings: 150 dpi clean PNG; photo = 2.5 deg rotate, blur 0.9, gradient, JPEG 70):
    $PY tools/template-digitise-bench/raster.py --pdf $IN/bicl.pdf --out $IN
    $PY tools/template-digitise-bench/raster.py --pdf $IN/penina.pdf --out $IN
 3. $PY tools/template-digitise-bench/run.py --inputs $IN --out $OUT --dry-run
    Planned: 6 arms, 58 reads (+3 step-0 probes): muse low / muse medium / deepseek@coreweave/fp8 off /
    deepseek@coreweave/fp8 low on 4 image inputs x 3 reps (48), gemini as shipped x 1 rep (4), muse low
    on the 2 PDFs x 3 reps (6). "runnable now" is what this checkout can send.
 4. $PY tools/template-digitise-bench/run.py --inputs $IN --out $OUT --openrouter-key-file $KEYFILE \
        --ledger <shared anthropic_ledger.json> --confirm-calls <runnable reads it printed>
    Step 0 runs first (one 1-page image read per model / provider); an arm whose probe is refused,
    fails or matches no truth item (it ignored the image) is skipped.
 5. Re-score without calls: $PY tools/template-digitise-bench/score.py --out $OUT
    Truth = tests/fixtures/template_digitise/truth_{bicl,penina}.json. Gate per arm: recall >= 0.98 and
    zero invented on every input in 3/3 reps, no empty / cut-off read, p50 <= 60 s. Winner = pass, then
    recall, then fewer wording changes, then p50; none passes -> stay on gemini. PDF: if the image
    winner cannot take a PDF, the muse-PDF arm must pass the same gate, else it is the owner's call
    (gemini for PDF only, or PDF_ACCEPTED=False). Per-read usage numbers (file-parser charges, if any)
    are in <out>/arms/*/*.json "calls".
