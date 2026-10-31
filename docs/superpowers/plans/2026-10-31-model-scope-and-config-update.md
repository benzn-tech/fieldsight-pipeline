# Model scope + model config update Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or superpowers:executing-plans. Steps use `- [ ]`.

**Goal:** Re-scope both prompt x model benches to muse/DeepSeek/Haiku/Sonnet with an OpenRouter cap, add a template-digitise model bench, and make the Claude 5.5 IDs + per-model digitise settings safe to deploy (inert by default).

**Architecture:** Two PRs from develop. PR-1 touches only `tools/` + tests of tools. PR-2 touches `src/` (llm_utils, llm_tools_client, template_vision, template.yaml, two workflows, two lambda defaults) + tests; inert while `LLM_PROVIDER=qwen` and the new vars are empty.

**Spec:** `docs/superpowers/specs/2026-10-31-model-scope-and-config-update-design.md` (rev 2) — executors read it; values below are copied from it.

## Global Constraints
- Python tests: `C:/Users/camil/fspy/Scripts/python.exe -m pytest` (no installs into fspy). Live/bench deps (Pillow, pymupdf, numpy, anthropic) only in `C:/Users/camil/wt/benchenv`.
- No live model call from the implementer; every keyed step is the owner's.
- Never `git add -A` / `.`; never `git stash`. Commit trailers: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` and `Claude-Session: https://claude.ai/code/session_01YBf9b5himhWTUdTLrgHG6M`.
- Never print keys (sha256[:8] only) or prompt/answer/transcript text to stdout.
- 4.x Claude payloads and `call_llm_parts(reasoning=None, provider=None)` payloads stay byte-identical (existing pins are the proof).

## Review Focus
1. Bench `--out` holding rows from a removed cell (mimo, gemini verify_v1) -> refuse, don't silently summarize them.
2. OpenRouter response without `usage.cost` -> charged at pinned upper, never $0.
3. `TEMPLATE_DIGITISE_REASONING=off` with a muse model -> field omitted + warning, not a 400 in prod.
4. Anthropic 200 whose only blocks are `thinking` (stop_reason max_tokens) -> error, not `""` success.
5. `claude-haiku-4-5-20251001` must not match the 5.x gate.

---

## PR-1 (branch `feat/bench-model-scope`, tools only)

### Task 1: OpenRouter pool, key file, usage.include
Files: `tools/web-answer-bench/ledger.py`, `openrouter_bench.py`, `matrix.py`, `grade.py`; `tools/report-bench/rb_ledger.py`, `rb_callers.py`, `rb_run.py`; tests under `tests/unit/test_web_answer_bench_*.py`, `test_report_bench_*.py`.
- Produces: `ledger.add_openrouter_spend(path, usd)`, `ledger.openrouter_room(path, cap) -> float` (pools-only writer; never touches `spent_usd`/global); `OR_PRICES_2026_10_31 = {"meta/muse-spark-1.3-contributor": (0.10, 0.20), "deepseek/deepseek-v4.1-flash": (0.13, 0.65), "google/gemini-3.8-flash": (0.825, 4.125)}` ($/M); `or_cost(resp_json, model, in_tok, out_tok) -> float` (usage.cost else pinned upper); `load_or_key(key_file, env_name) -> (key, warned: bool)`.
- CLI on matrix.py, grade.py, rb_run.py: `--openrouter-key-file`, `--openrouter-cap` (default 10). Dry run exit 2 when OR upper > remaining cap.
- Tests: usage.include sent but absent from `params_of`/prompt sha; missing cost -> upper; OR spend leaves `spent_usd` and global check unchanged; refusal at cap; dry-run exit 2; key-file path wins over env; env fallback prints the prod-key warning; key never printed.

### Task 2: web bench cells
- `round0_cells()` -> 22 cells per spec Part A; `PROFILES["muse"]` (reasoning effort low, no json forcing for web text). Update pins at test_web_answer_bench_matrix.py:336/354/458/466 to 22 / 330 / anthropic 270 / OR 255 / 525 (recompute by hand count in the test; if the code's plan() differs, the hand count in the spec is the authority — report any mismatch).
- Refuse `--out` containing rows whose cell is not in `round0_cells()` (exit 2, names count). Update `run_stage0.sh` usage text only if it lives in repo (it does not: scratchpad).

### Task 3: report bench planned models
- Planned set / `MODEL_ORDER` = muse, deepseek, haiku, sonnet; `MODELS` entries for gemini/mimo kept. Dry-run counts: X 192, R 36, T 144 (+<=144 retries). README.txt updated (models, cap, key file).

### Task 4: template-digitise bench
Files: create `tools/template-digitise-bench/{README.txt,convert.ps1,raster.py,run.py,score.py}` + `tests/unit/test_template_digitise_bench.py`.
- `convert.ps1 -Src <docx> -Out <dir>`: Word COM SaveAs PDF (wdFormatPDF=17), refuses an `-Out` under Dropbox, never writes next to the source.
- `raster.py --pdf P --out D`: spike settings (150 dpi clean PNG; photo = rotate 2.5 expand fill (200,195,185) bicubic, GaussianBlur 0.9, gradient 0.78-1.05 x 0.92-1.0, JPEG q70).
- `run.py --inputs D --out O --openrouter-key-file F (--dry-run | --confirm-calls N)`: arms exactly as spec B1 (muse low, muse medium, deepseek@coreweave/fp8 off, deepseek@coreweave/fp8 low, gemini shipped 1 rep, muse-PDF low 3 reps) = 58; step 0 probe per model (1 page) recorded and an arm whose probe fails is skipped; each arm runs in a subprocess with env set before importing llm_utils (`LLM_PROVIDER=qwen`, `QWEN_BASE_URL=https://openrouter.ai/api/v1`, `QWEN_API_KEY`, `TEMPLATE_DIGITISE_MODEL/REASONING/PROVIDER`); calls `template_vision.read()`; OR pool via Task 1 helpers. Until PR-2 lands, REASONING/PROVIDER are ignored by src — run.py checks `hasattr(template_vision, "reasoning")`-style capability and refuses non-baseline arms if absent (prints "needs PR-2").
- `score.py`: spike matching (ratio >= 0.8), invented = unmatched pred with no missed truth at ratio >= 0.5; per arm recall, invented, wording changes, sections, empty/cut-off, p50/max; gate + winner per spec; writes `O/summary.json` (no item text on stdout).
- Tests: scorer on hand-made truth/pred (paraphrase = wording change not invention; true invention counted); gate/winner ordering; dry run 58 calls, no network; subprocess env built correctly; convert.ps1 path refusal tested via a python wrapper only if trivial, else documented.

### Task 5: anthropic smoke
- `tools/anthropic-smoke.py --key-file F`: sets env before import; Haiku 5.5 (`LLM_TEMPERATURE=0`, `enable_thinking=False`) and Sonnet 5.5 (`LLM_REASONING_EFFORT=high`) via `llm_utils._call_anthropic`; prints model, ok, stop info, text length only. Refuses to run if `_call_anthropic` lacks the 5.x gate (needs PR-2).

## PR-2 (branch `feat/claude-55-and-digitise-knobs`, src)

### Task 6: Claude 5.x payload
Files: `src/llm_utils.py`, `tests/unit/test_llm_utils.py` (+ new `tests/unit/test_anthropic_5x_payload.py`).
- `_is_claude_5x(model) -> bool` = `re.match(r"^claude-[a-z]+-5-", model)`.
- `_call_anthropic(prompt, max_tokens, deadline=None, caller="unknown", enable_thinking=None)` per spec Part C; `call_llm` forwards `enable_thinking` only when not None.
- Tests: 4.x payload byte-identical (pin test_llm_utils.py:257-266 to an explicit 4.x id); 5.x: no temperature, `max_tokens + REASONING_HEADROOM_TOKENS`, `output_config.effort` low when enable_thinking False, = env when set, absent otherwise; max_tokens-no-text and refusal -> errors; haiku-4-5 id not 5.x.

### Task 7: model IDs
- template.yaml 10 `CLAUDE_MODEL` lines -> 5.5 ids; defaults in llm_utils, lambda_report_generator, lambda_meeting_minutes. claude_utils untouched. Run full unit suite; fix only tests that read the default constant.

### Task 8: digitise knobs
Files: `src/llm_tools_client.py`, `src/llm_utils.py` (`call_llm_parts`), `src/template_vision.py`, `src/template.yaml`, `.github/workflows/deploy.yml`, `deploy-prod.yml`, tests (test_llm_multipart_call.py, test_template_vision.py, test_template_digitise_wiring.py, llm_tools_client tests).
- `parse_reasoning(raw, model=None)`, `parse_provider(raw)` extracted; judge behaviour unchanged (its tests pass untouched). muse + `off` -> None + warning.
- `call_llm_parts(..., reasoning=None, provider=None)`; reasoning set -> headroom added.
- `template_vision`: reads `TEMPLATE_DIGITISE_REASONING`, `_PROVIDER`, `_PDF_MODEL` (PDF pages -> PDF model when set).
- template.yaml parameters `TemplateDigitiseModel` (default `google/gemini-3.8-flash`), `TemplateDigitiseReasoning`, `TemplateDigitiseProvider`, `TemplateDigitisePdfModel` (default ''); workflows pass `${{ vars.PROD_TEMPLATE_DIGITISE_MODEL || 'google/gemini-3.8-flash' }}` etc. (TEST_ in deploy.yml). Wiring test reads the parameter default.

### Task 9: verify
- Full unit suite green (baseline count first); cfn-lint per repo CI pin if available locally, else rely on CI.
