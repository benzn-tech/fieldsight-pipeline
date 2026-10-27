# Web-answer prompt × model matrix — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run every historical web-answer prompt on every admitted model (spec §4.4) on the 5 `tune` items, grade the answers blind, and freeze one prompt + parameters per (step, model) in `frozen.v1.json`.

**Architecture:** Bench-only code under `tools/web-answer-bench/`; nothing in `src/` changes. Claude calls reuse `claude_verify.verify` (official SDK, `max_retries=0`). OpenRouter calls (gemini, DeepSeek, mimo, grader) build their own payload per model profile and reuse `corroboration_client`'s HTTP session and response parser. One persisted ledger with pools enforces the $ caps. The owner runs every live step (keys); Claude builds and unit-tests offline.

**Tech Stack:** Python 3.11; `anthropic` SDK only in `C:/Users/camil/wt/benchenv`; tests under `C:/Users/camil/fspy` (CI-identical, SDK absent → tests use fakes).

**Spec:** `docs/superpowers/specs/2026-10-27-claude-web-answer-bench-design.md` (rev 4.2), §4.2, §4.4, §5.1, §6, §7.

## Global Constraints

- Models / parameters (spec §4.0, §4.4): Sonnet 5.5 `claude-sonnet-5-5` + `web_search_20250305`, `max_uses` 3; Haiku 5.5 `claude-haiku-5-5` + `web_search_20250305`, `max_uses` 2; both `output_config {effort: "low"}`, no `thinking` field, `max_tokens` 4096, `user_location` NZ. gemini `google/gemini-3.8-flash` with `corroboration_client.provider_preference`, `reasoning {effort: low}`. DeepSeek `deepseek/deepseek-v4.1-flash`, `provider {only: ["coreweave/fp8"], allow_fallbacks: false}`, `reasoning {enabled: false}`. mimo `xiaomi/mimo-v2.6-pro`, `reasoning {enabled: false}` (llm_utils.py:483-488), **draft only** (web-plugin support unmeasured). muse excluded.
- Web search on OpenRouter = `plugins: corroboration_client.WEB_PLUGIN`.
- Budgets: draft timeout `web_answer._draft_timeout(22.1)`; verify / single call 14 s; one Claude `pause_turn` continuation; OpenRouter verify ≤ 1 retry only if ≥ 10 s remain (as web_answer.py:336-345).
- Reps 3 per (cell, item); items = the 5 `tune` rows; verify cells verify gemini's stored `draft_v1` for the same item / rep.
- Ledger (spec §7): total cap $45 Anthropic; pool `matrix` ≤ $18; scored run refuses to start unless `cap − spent ≥ reserve` ($23). Cheapest cells first.
- Privacy: question / draft / answer text only in `--out` files; stdout and summaries carry ids, numbers, outcomes. `--out` must be outside the repo. Keys never printed.
- Every live command: `--dry-run` prints calls and $ upper; live needs `--confirm-calls N` equal to it.
- Grader: DeepSeek V4.1 Flash @ coreweave via OpenRouter, one call per answer, blind (no cell / model label, shuffled with a stored seed, `[unverified]` and link markup stripped), labels `correct | partial | wrong | correctly_unsure | hallucinated_source`.

## Review Focus

1. A Claude answer with zero text but results (tool-only stop) → outcome `failed`, never graded as an empty "correct".
2. An OpenRouter 200 with `choices` missing / empty content → `failed`, recorded, no crash, no retry storm.
3. The matrix pool exhausted mid-cell → stop before the next call, write the partial summary, exit 3; the scored-run reserve is never touched.
4. Grader returns non-JSON or an unknown label → that answer is `ungraded` (counted), never silently `wrong`.
5. A prompt text file drifts from the shipped constant → test fails (draft_v1 == `web_answer.DRAFT_PROMPT`, verify_v2 == `web_answer.VERIFY_PROMPT`).

---

### Task 1: Prompt texts as files, pinned to their source

**Files:**
- Create: `tools/web-answer-bench/prompts/{draft_v1,verify_v1,verify_v2,web_v1}.txt` (`merged_v1.txt` exists)
- Create: `tools/web-answer-bench/prompts.py`
- Test: `tests/unit/test_web_answer_bench_matrix.py`

**Interfaces:**
- Produces: `prompts.load(pid: str) -> str`, `prompts.sha(pid) -> str`, `prompts.CANDIDATES: dict[str, str]` mapping id → step (`draft` | `verify` | `single`).

- [ ] Step 1: tests — `load("draft_v1") == web_answer.DRAFT_PROMPT`; `load("verify_v2") == web_answer.VERIFY_PROMPT`; `load("verify_v1")` equals `git show 518900a8:src/web_answer.py` VERIFY_PROMPT (text pasted into the test as a literal); `load("web_v1")` equals 518900a8^ WEB_PROMPT literal; every verify text contains `{question}` and `{draft}`, every draft / single text contains `{question}` and not `{draft}`; `CANDIDATES == {"draft_v1": "draft", "verify_v1": "verify", "verify_v2": "verify", "web_v1": "single", "merged_v1": "single"}`.
- [ ] Step 2: run, expect FAIL (module missing).
- [ ] Step 3: write the four files (exact text from git, `newline=""`), `prompts.py` reads `HERE/prompts/<id>.txt` as UTF-8.
- [ ] Step 4: run, PASS. Step 5: commit.

### Task 2: Ledger pools

**Files:** Modify `tools/web-answer-bench/ledger.py`; Test: `tests/unit/test_web_answer_bench_matrix.py`

**Interfaces:**
- Produces: `Ledger(path, cap=CAP_USD)` unchanged for Stage 0, plus `Ledger.add(dollars, pool="stage0")`, `Ledger.pool_spent(pool) -> float`, `Ledger.pool_exhausted(pool, pool_cap) -> bool`, `Ledger.room() -> float`. Constants `MATRIX_POOL_CAP = 18.0`, `SCORED_RESERVE = 23.0`.
- File format: `{"spent_usd": total, "cap_usd": cap, "pools": {pool: spent}}`; an existing file without `pools` loads with its total counted as `stage0`.

- [ ] Tests: old-format file (spent 1.42) → `pool_spent("stage0") == 1.42`, `room() == 43.58`; adding 17.9 to `matrix` → `pool_exhausted("matrix", 18)` False, after +0.2 True; total exhaustion still at 45; existing Stage 0 ledger tests still pass.
- [ ] Implement, run, commit.

### Task 3: OpenRouter bench caller

**Files:** Create `tools/web-answer-bench/openrouter_bench.py`; Test: same test file.

**Interfaces:**
- Consumes: `corroboration_client.http_session()`, `API_URL`, `WEB_PLUGIN`, `provider_preference`, the module's response parser (read the module; reuse its `Reply` parsing — `searched` = at least one url citation / result as `corroboration_client` defines it).
- Produces: `PROFILES: dict[str, dict]` keyed `gemini | deepseek | mimo | grader`; `build_payload(profile: str, prompt: str, *, web: bool, max_tokens: int) -> dict`; `call(profile, prompt, *, web, timeout, max_tokens=1024, retry_left_s=None, http=None, clock=time.monotonic) -> dict` returning the same keys as `claude_verify.verify` (`text, searched, result_count, source_count, outcome, latency_ms, calls, input_tokens, output_tokens, http_status, error_code`), `outcome ∈ ok | no_search | failed | timeout | http_error`.

- [ ] Tests (fake http): payload for deepseek has `provider == {"only": ["coreweave/fp8"], "allow_fallbacks": False}` and `reasoning == {"enabled": False}`; mimo `reasoning == {"enabled": False}`, no `provider`; gemini `reasoning == {"effort": "low"}` and `provider == provider_preference("google/gemini-3.8-flash")`; `web=True` adds `plugins == WEB_PLUGIN`, `web=False` none; 200 without choices → `failed`; timeout exception → `timeout`; web call with text but no citations → `no_search`; one retry only when `retry_left_s >= 10`; key read from env `CORROBORATION_API_KEY`, absent → `failed` with no request.
- [ ] Implement, run, commit.

### Task 4: Claude draft (no tool) via `claude_verify`

**Files:** Modify `tools/web-answer-bench/claude_verify.py`; Test: stage0 test file.

**Interfaces:** `build_request(model, tool_version, prompt, max_uses=MAX_USES)` with `tool_version=None` → no `tools` key; `verify(..., tool_version=None)` then returns `ok` on text with `searched=False` (a draft is not expected to search).

- [ ] Tests: request without `tools` when `tool_version=None`; text-only response → `ok`; existing tests unchanged.
- [ ] Implement, run, commit.

### Task 5: Cells and the round-0 runner

**Files:** Create `tools/web-answer-bench/matrix.py`; Test: matrix test file.

**Interfaces:**
- Consumes: Tasks 1-4, `stage0.load_tune`, `stage0.unsure_ids`, `stage0.reclassify`.
- Produces: `Cell = (step, prompt_id, model_key)` with `MODEL_KEYS = ("gemini", "deepseek", "mimo", "sonnet", "haiku")`; `round0_cells() -> list[Cell]` (draft: gemini, deepseek, mimo, sonnet, haiku × draft_v1; verify: gemini, deepseek, sonnet, haiku × verify_v1, verify_v2; single: same four × web_v1, merged_v1) = 21 cells; `plan(cells, reps=3) -> {"runs", "anthropic_upper", "openrouter_upper", "anthropic_usd_upper"}`; CLI `matrix.py --out D --round 0 --search-price-per-1000 P (--dry-run | --confirm-calls N)`; outputs `D/matrix_r0_calls.jsonl` (one row per run: cell, item, rep, outcome, latency, tokens, searches, cost, prompt sha, text) and `D/matrix_r0_summary.json` (per cell: n, outcomes, p50 / max ms, searched rate, $; no text).
- Order: gemini drafts first (they feed verify), then cells by expected $ ascending (OpenRouter, Haiku, Sonnet).
- A verify cell's run uses `drafts[(item, rep)]` from gemini `draft_v1`; a failed gemini draft for that (item, rep) → the verify run is skipped and recorded `no_draft`.

- [ ] Tests: `len(round0_cells()) == 21`; plan numbers for 21 cells × 5 × 3 match a hand count written into the test (runs 315); dry run prints and makes no call; verify prompt contains the stored draft of the same (item, rep); pool exhaustion → exit 3 with summary written; `no_search` on a non-unsure item is `failed` (via `reclassify`); nothing in stdout / summary contains the sentinel question or answer text.
- [ ] Implement, run, commit.

### Task 6: Blind grader

**Files:** Create `tools/web-answer-bench/grade.py`, `tools/web-answer-bench/prompts/grader_v1.txt`; Test: matrix test file.

**Interfaces:**
- Consumes: `openrouter_bench.call("grader", ...)`, the gold rows (`expected`, `variants`, `kind`).
- Produces: `blind(text) -> str` (strips `[unverified]`, markdown links → their text, bare URLs → host); `grade_one(question, gold, answer, domains) -> {"label", "raw_ok"}`; CLI `grade.py --out D --calls matrix_r0_calls.jsonl --seed S (--dry-run | --confirm-calls N)` writing `D/grades_r0.jsonl` (run id → label) and `D/cells_r0.json` (per cell, per item: the 3 labels, majority label — no majority → worst — and `stable`); `ORDER = ["wrong", "hallucinated_source", "partial", "correctly_unsure", "correct"]` worst→best.
- Grader prompt: rubric of spec §5.1, returns only JSON `{"label": ...}`; an `unsure` item is `correct`-equivalent only as `correctly_unsure`.

- [ ] Tests: `blind` removes markers and links; shuffle with a fixed seed is reproducible and the grader input never contains cell / model ids; non-JSON or unknown label → `ungraded`; majority / worst / stable on hand-made triples; hallucinated-source flag passes through when §5.2 domain check flags a literal (`domains` not containing the answer's literal host).
- [ ] Implement, run, commit.

### Task 7: Keep rule, freeze file, owner spot-check sheet

**Files:** Create `tools/web-answer-bench/select_freeze.py`; Test: matrix test file.

**Interfaces:**
- Produces: `best_round0(cells_json, step, model) -> prompt_id` (most items at majority `correct`/`correctly_unsure`, tie → fewer `wrong`, tie → lower p50 ms); `keep_edit(incumbent, edit) -> bool` (spec §4.4: no item worse by majority, ≥ 1 item better where incumbent stable); `write_frozen(path, entries)` → `frozen.v1.json` entries `{step, model, prompt_id, text, sha256, params, round, tune_labels, evidential: false}`; `spot_check_sheet(grades, n=10, seed) -> markdown` for the owner (question, gold, answer, blank label column; no grader label shown).

- [ ] Tests on hand-made cells: best pick, each keep-rule branch, frozen entry fields and `evidential is False`, sheet contains no grader labels and no model names.
- [ ] Implement, run, commit.

### Task 8: Owner wrapper and docs

**Files:** Modify scratchpad `run_stage0.sh` (not committed) — modes `matrix-dry`, `matrix`, `grade-dry`, `grade`; append to plan an "Owner run order" section with the exact commands and printed upper bounds.

- [ ] Dry runs executed by Claude (no keys needed); numbers pasted into the owner message.
- [ ] Full test file run under fspy plus `tests/unit/test_answering_from_the_open_web.py`; commit.

## Owner run order (filled in Task 8)

1. `matrix-dry` → read calls / $; 2. `matrix` (round 0); 3. `grade`; 4. owner spot-check (10 answers); 5. rounds 1-2 per spec §4.4 (Claude writes the edits, owner runs); 6. freeze.
