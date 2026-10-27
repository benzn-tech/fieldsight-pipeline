# Claude web-answer bench — Stage 0 speed probe Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An owner-run script that measures whether any Claude model + web search can verify a gemini draft inside the verify budget (p50 ≤ 9 s, max ≤ 14 s), before any money is spent on the full bench.

**Architecture:** Three focused files in `tools/web-answer-bench/`: `claude_verify.py` (one verify = request build, call, `pause_turn` continuation, parse), `ledger.py` (persisted Anthropic spend, $45 hard stop), `stage0.py` (CLI: validation, dry-run, drafts, round-robin loop, summary, stop rule). The Anthropic client and the draft function are injected, so unit tests use fakes and need neither the SDK nor network; `anthropic` is imported lazily inside `stage0.main` only for a live run.

**Tech Stack:** Python 3.11 stdlib; official `anthropic` SDK (live runs only, in `C:/Users/camil/wt/benchenv`, which sees fspy's packages through a `.pth`; fspy itself stays identical to CI); `corroboration_client` / `web_answer` from `src/` for the drafts.

**Spec:** `docs/superpowers/specs/2026-10-27-claude-web-answer-bench-design.md` §4.0 (Stage 0 definition), §6, §7.

## Global Constraints

- Configs: `("claude-sonnet-5-5", "web_search_20260209")`, `("claude-sonnet-5-5", "web_search_20250305")`, `("claude-haiku-5-5", "web_search_20250305")`.
- Tool dict: `{"type": <version>, "name": "web_search", "max_uses": 3, "user_location": {"type": "approximate", "country": "NZ", "timezone": "Pacific/Auckland"}}`.
- Request: `max_tokens=4096`, `output_config={"effort": "low"}`, no `thinking` field, no prefill, prompt `web_answer.VERIFY_PROMPT.format(question=q, draft=d)`; client `max_retries=0`.
- Verify timeout 14 s total; one `pause_turn` continuation with the remaining time (re-send user message + assistant content, no extra user turn); a second pause = failed.
- Drafts: `corroboration_client.call(DRAFT_PROMPT.format(question=q), timeout=_draft_timeout(22.1), max_tokens=1024, effort="low", caller="bench_draft")`.
- Reps: 12 per config, order t1..t5, t1..t5, t1, t2.
- Prices per MTok: sonnet $2 / $10, haiku $0.10 / $0.50. Upper-bound assumption per call: 46 000 input, 4 096 output tokens, 3 searches.
- Planned calls 5 + 3×12 = 41; upper bound with continuations 5 + 3×24 = 77; `--confirm-calls` must equal 77.
- Hard stop: ledger `anthropic_ledger.json` in `--out`, cumulative across runs, no Anthropic call once spent ≥ $45.
- Never print or summarise question / draft / answer text; answer text only in `--out/stage0_calls.jsonl`.
- Env for a live run: `ANTHROPIC_API_KEY`, `CORROBORATION_API_KEY`; missing → exit 2. `--out` inside the repo → exit 2.

## Review Focus

1. A tool-result block whose `content` is an error object (HTTP 200) must give `searched=False` with the `error_code` recorded — test in Task 1.
2. A 400 on a config's first call must stop that config without touching the others — test in Task 3.
3. A ledger that already holds ≥ $45 from an earlier run must stop before the first call — test in Task 2 / Task 3.
4. A `pause_turn` arriving after the 14 s budget is spent must fail without a continuation — test in Task 1.
5. A sentinel in the question or answer must not appear in stdout or `stage0_summary.json` — test in Task 3.

---

### Task 1: `claude_verify.py` — one verify call

**Files:**
- Create: `tools/web-answer-bench/claude_verify.py`
- Test: `tests/unit/test_web_answer_bench_stage0.py`

**Interfaces:**
- Produces: `build_request(model: str, tool_version: str, prompt: str) -> dict` (kwargs for `messages.create`, without `timeout`); `parse(response) -> dict` with keys `text, searched, result_count, source_count, error_code, stop_reason, input_tokens, output_tokens, search_requests`; `verify(client, *, model, tool_version, prompt, budget_s=14.0, clock=time.monotonic) -> dict` returning the parse keys plus `latency_ms, continuation, http_status, outcome` where outcome ∈ `ok | failed | timeout | rejected | max_tokens | refusal | paused_twice | http_error`.

- [ ] Step 1: tests — `test_request_shape` (tool dict, 4096, effort low, no `thinking`); `test_error_object_content_is_not_searched` (content `{"type": "web_search_tool_result_error", "error_code": "too_many_requests"}` → `searched False`, `error_code "too_many_requests"`); `test_results_list_is_searched` (2 results → `result_count 2`); `test_pause_turn_one_continuation_then_ok` (2 calls, second `timeout` = remaining, second request's messages = [user, assistant(content)], latency summed, `continuation True`); `test_second_pause_fails` (`outcome "paused_twice"`); `test_pause_after_budget_spent_no_continuation`; `test_400_is_rejected`; `test_timeout_is_timeout`; `test_refusal_and_max_tokens_outcomes`.
- [ ] Step 2: run, expect import failure.
- [ ] Step 3: implement. Exceptions classified by duck typing: `status_code == 400` → rejected; class name containing `Timeout` → timeout; other `status_code` → http_error. Usage summed across both calls; `search_requests` from `usage.server_tool_use.web_search_requests` (0 when absent). Blocks read with `getattr` so dicts-as-objects fakes work.
- [ ] Step 4: run, expect PASS.

### Task 2: `ledger.py` — persisted hard stop

**Interfaces:**
- Produces: `class Ledger(path: str, cap: float = 45.0)` with `.spent: float`, `.exhausted() -> bool`, `.add(dollars: float) -> None` (writes the file each time); `call_cost(model: str, input_tokens: int, output_tokens: int, searches: int, price_per_1000: float) -> float`.

- [ ] Step 1: tests — `test_ledger_persists_across_instances`; `test_ledger_exhausted_at_cap`; `test_call_cost_sonnet` (`call_cost("claude-sonnet-5-5", 1_000_000, 100_000, 10, 10.0) == 2.0 + 1.0 + 0.1`).
- [ ] Step 2-4: fail, implement, pass.

### Task 3: `stage0.py` — CLI, loop, summary, stop rule

**Interfaces:**
- Consumes: Task 1 `verify`, `build_request`; Task 2 `Ledger`, `call_cost`.
- Produces: `CONFIGS`; `load_tune(path) -> list[dict]` (id, question for t1-t5); `order(ids, reps=12) -> list[str]`; `plan() -> dict` (`planned 41, upper 77, searches_upper 216`); `cost_upper(price_per_1000) -> float`; `validate(argv, env, repo_root) -> argparse.Namespace` (exits 2); `run(args, *, items, client, draft_fn, clock, stdout) -> tuple[int, dict]` (exit code, summary); `summarize(records) -> dict`; `stop_rule(summary) -> dict` (`proceed`, `frozen` {model: tool}, `dropped` [models]); `main(argv=None) -> int`.

- [ ] Step 1: tests — `test_validate_requires_keys_for_live_run` (exit 2), `test_validate_refuses_out_inside_repo`, `test_validate_requires_search_price`, `test_confirm_calls_must_equal_upper`; `test_plan_counts` (41 / 77 / 216); `test_cost_upper_at_10_per_1000` (≈ 8.70); `test_dry_run_prints_counts_and_dollars_no_calls`; `test_round_robin_order` (`t1..t5,t1..t5,t1,t2`); `test_rejected_config_stops_only_itself`; `test_ledger_hard_stop_across_runs` (pre-filled ledger at 45 → no client call, exit 3); `test_summary_counts_refusals_and_max_tokens_separately`; `test_timeout_counts_as_14s_in_max`; `test_stop_rule_freezes_fastest_tool_per_model_and_drops_others`; `test_no_question_or_answer_text_in_stdout_or_summary`.
- [ ] Step 2: run, expect failure.
- [ ] Step 3: implement. Drafts once per item before any Anthropic call; `draft_ms` stored. Each verify call record also carries `config, item, rep, model, tool_version, prompt_sha256` and is appended to `stage0_calls.jsonl` (the only file with text); `stage0_summary.json` holds the per-config summary + stop rule. Ledger checked before every verify (a verify makes ≤ 2 requests, so the cap can be overshot by at most one verify); exhausted → stop, write summary, return exit code 3. A failed gemini draft → exit 4 before any Anthropic call.
- [ ] Step 4: run the new tests plus `tests/unit/test_answering_from_the_open_web.py` with `C:/Users/camil/fspy/Scripts/python.exe -m pytest`.
- [ ] Step 5: commit (named files only).
