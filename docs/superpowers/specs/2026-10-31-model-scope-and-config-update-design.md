# Model scope + model config update (benches, template digitise, Claude IDs)

Date: 2026-10-31 · Line: ASR+LLM优化v3 · Status: rev 2 (Fable review rev 1 applied, §Review log)

Owner decisions 2026-10-31 (chat):
1. Template digitise stops using gemini; it moves to muse or DeepSeek, **chosen by measurement first**.
2. Config update covers (a) the Claude model IDs and (b) the template digitise model.
3. The prompt x model benches test only **muse, DeepSeek** (OpenRouter) and **Haiku 5.5, Sonnet 5.5**
   (Anthropic, the Claude API key). gemini stays in the web bench as the **shipped baseline only**,
   because prod web search is gemini today. mimo is dropped everywhere.

Prod facts read 2026-10-31 (Lambda env, read-only):
- report / extract / minutes / rolling summary / Ask answer: `QWEN_MODEL=meta/muse-spark-1.3-contributor`.
- Ask judge (voice worker): `deepseek/deepseek-v4.1-flash` @ `coreweave/fp8`, reasoning off.
- web search + corroboration: gemini (`CORROBORATION_MODEL` unset -> code default).
- template digitise: `TEMPLATE_DIGITISE_MODEL=google/gemini-3.8-flash` (literal, template.yaml:2731),
  fallbacks empty; `template_types.PDF_ACCEPTED = True` (PDF goes as a `file` part).
- `LLM_PROVIDER=qwen` everywhere, so `CLAUDE_MODEL` (sonnet-4-6 / haiku-4-5) is read by no live
  call. But `deploy-prod.yml:376`, `template.yaml:247-251` and `llm_utils.py:34` all default the
  provider to `anthropic`, so the Anthropic path is one unset variable away from serving prod.
- OpenRouter model metadata (public `/api/v1/models`, 2026-10-31): deepseek-v4.1-flash input
  `text, image`; muse-spark-1.3-contributor `text, image, video, file`. Per-endpoint image support
  (coreweave) is not published -> measured in B1 step 0.
- Keys: the web bench reads `CORROBORATION_API_KEY` (`corroboration_client.api_key()`), the report
  bench `QWEN_API_KEY`/`DASHSCOPE_API_KEY` (`rb_callers.py:49`). Both hold the prod OpenRouter key
  today; a bench that empties it stops prod Ask and reports.

## Part A — bench model scope (tools/ only)

**Web bench (`tools/web-answer-bench`).**
- `round0_cells()` (matrix.py) stays the single source, with an explicit rule: gemini appears only
  as `draft_v1` (the shipped draft, and the input every verify cell checks) and `verify_v2`
  (shipped verify). Cells: draft gemini, deepseek, muse, sonnet, haiku x `draft_v1` (5); verify
  gemini x `verify_v2` (1); verify deepseek, muse, sonnet, haiku x `verify_v1`, `verify_v2` (8);
  single deepseek, muse, sonnet, haiku x `web_v1`, `merged_v1` (8). **22 cells, 330 runs**,
  Anthropic calls upper 270, OpenRouter 255, **525 total** (pins in
  test_web_answer_bench_matrix.py:336/354/458/466 move to these).
- Add a `muse` profile to `openrouter_bench.PROFILES` (reasoning low — muse refuses
  `enabled:false`, llm_utils.py:527-535; json not forced on web text). mimo profile kept, unused.
- muse was excluded 10-27 for "0 sources, claims to have searched"; it returns on owner instruction,
  and that failure is what `source_flag` / `hallucinated_source` grade.
- Resume is key-based (matrix.py:300-320) and `summarize()` reads every row: the new matrix needs a
  **fresh `--out` subdir** (`matrix_r0b`), the CLI refuses an `--out` holding rows of a removed cell.

**Report bench (`tools/report-bench`).** Planned models = muse, deepseek, haiku, sonnet. The
`MODELS` table (rb_requests.py:43-51) keeps gemini/mimo entries (tests build them); only the
planned set / `MODEL_ORDER` changes. Round 0: X 4 x 4 x 4 x 3 = 192; R 1 x 4 x 3 x 3 = 36;
T 3 x 4 x 4 x 3 = 144 (+ <= 144 retries). `rb_select.underperforming` already takes the median over
the models present (rb_select.py:76-98): no rule code change.

**OpenRouter spend cap (both benches).**
- Send `usage: {include: true}` at the send layer (`openrouter_bench.call`, `rb_callers.call`),
  outside `params_of` so recorded params / prompt sha do not change. Cost = response
  `usage.cost`; missing -> pinned upper price (constant `OR_PRICES_2026_10_31`: muse 0.10/0.20,
  deepseek coreweave 0.13/0.65, gemini 0.825/4.125 $/M).
- Stored in the shared ledger file as pool `openrouter` via a **pools-only writer** (like
  `add_report_spend`); never added to `spent_usd` / `global_spent` (those are Anthropic dollars and
  drive the $45 / $85 stops).
- Cap `--openrouter-cap` (default $10, both benches together). A call is refused once
  `spent >= cap`. A **dry run refuses** (exit 2, prints both numbers) when its OpenRouter upper
  exceeds the remaining cap, so a run is never planned to die halfway; the owner raises the cap or
  splits the run.
- **Separate key:** `--openrouter-key-file PATH` on both benches (owner creates a bench-only key with
  its own credit limit). Without it the bench falls back to its env var and prints
  `WARNING: using the prod OpenRouter key`. Keys appear only as sha256[:8].

Expected (pinned prices, 10-27/10-28 token sizes): web OpenRouter ~$1.5 (2 gemini cells ~$0.7,
grader ~$0.2, muse/deepseek ~$0.5), upper ~$4; report OpenRouter ~$2.5, upper ~$9 (T retries).

## Part B — template digitise model (measure, then switch)

**B1 bench (`tools/template-digitise-bench/`, tools only).** A screen, not a tail estimate
(n = 3 reps x 4 inputs).
- Inputs: the two 10-16 spike forms (`Dropbox/ADE/12.15 - Site Establishment Checklist (BICL).docx`,
  `Dropbox/ADE/Penina Site Inspection Checklist.docx`). `convert.ps1` (owner, Word COM) writes PDFs
  outside Dropbox; `raster.py` (spike settings: 150 dpi, photo = 2.5° rotate, blur 0.9, gradient,
  JPEG 70) makes `clean` + `photo` pages. Pillow + pymupdf + numpy go into `benchenv` (not fspy).
  Truth = `tests/fixtures/template_digitise/truth_{bicl,penina}.json`.
- Calls go through `template_vision.read()` (prod prompt, shrink, JSON retry). The runner sets
  `LLM_PROVIDER=qwen`, `QWEN_BASE_URL=https://openrouter.ai/api/v1`, `QWEN_API_KEY` (from
  `--openrouter-key-file`) and the arm's `TEMPLATE_DIGITISE_*` vars **before importing llm_utils**
  (import-time constants, llm_utils.py:34/64-67), one subprocess per arm.
- Step 0 ($0.001): one 1-page image call per model/provider; a text-only refusal or an answer that
  ignores the image ends that arm before the run.
- Arms (images): muse `low`; muse `medium`; deepseek@coreweave `off`; deepseek@coreweave `low`;
  gemini as shipped (1 rep, baseline). PDF arm: muse `low` on the two PDFs, 3 reps.
  Calls = 4 arms x 12 + gemini 4 + muse-PDF 6 = **58**, < $1 upper; OpenRouter pool as Part A.
- Score with the spike's `score.py` matching (ratio >= 0.8). **Invented** = a predicted item with no
  truth match **and** no missed truth item it paraphrases (an unmatched pred paired with a missed
  truth at ratio >= 0.5 is a wording change, not an invention). Also: recall, wording changes,
  section split, empty / cut-off (`PARTS_CUT_OFF`), latency p50 / max.
- Gate: recall >= 0.98 and zero invented on all 4 image inputs in 3/3 reps, no empty / cut-off,
  p50 <= 60 s. Winner = pass, then recall, then fewer wording changes, then p50. Neither passes ->
  stay on gemini, report numbers.
- **PDF coupling:** prod accepts PDF. If the image winner is DeepSeek (no `file` input), PDF uploads
  must go to a PDF-capable model: muse if the muse-PDF arm passes the same gate, else gemini stays
  for PDF only, else `PDF_ACCEPTED=False` (owner's call, reported). If OpenRouter's response shows
  the file-parser plugin was used/charged, that is reported with the cost.

**B2 code (src/, inert by default).**
- Extract `parse_reasoning(raw)` and `parse_provider(raw)` from `llm_tools_client._reasoning` /
  `_provider_pin` (llm_tools_client.py:98-121); the judge keeps calling them with its env names.
  `parse_reasoning` for a muse model id refuses `off` (logs, returns None -> field omitted).
- New env read only by `template_vision`: `TEMPLATE_DIGITISE_REASONING`, `TEMPLATE_DIGITISE_PROVIDER`,
  `TEMPLATE_DIGITISE_PDF_MODEL` (empty = same model as images).
- `call_llm_parts(..., reasoning=None, provider=None)`; None = byte-identical payload (existing pins
  test_llm_multipart_call.py:71-77, test_template_vision.py:136 stay the proof). With reasoning set
  the answer budget gets `REASONING_HEADROOM_TOKENS` added (reasoning counts against max_tokens).
- `template.yaml`: `TEMPLATE_DIGITISE_MODEL: !Ref TemplateDigitiseModel` etc., parameters wired in
  both workflows as `${{ vars.PROD_TEMPLATE_DIGITISE_MODEL || 'google/gemini-3.8-flash' }}` (and
  TEST_), defaults = today's values. Rollback = change a repo variable + redeploy, no code change.
  Wiring test test_template_digitise_wiring.py:79 moves to the parameter default.

**PR-3 is then variables, not code:** the owner sets `TEST_TEMPLATE_DIGITISE_*` from B1's winning
arm, deploys TEST, hand-tests a photo and a PDF upload, then sets `PROD_*` and releases.

## Part C — Claude model IDs (src/)

Change `claude-sonnet-4-6` -> `claude-sonnet-5-5` and `claude-haiku-4-5-20251001` ->
`claude-haiku-5-5` in `template.yaml` (10 places) and the defaults in `llm_utils`,
`lambda_report_generator`, `lambda_meeting_minutes`. `claude_utils.py` has no importer (all hits are
docstrings): left unchanged, noted dead. Tests that set and assert their own literal via
monkeypatch need no change.

`_call_anthropic` (the only live Anthropic sender; no prefill / tools / system / response_format —
force_json is dropped for anthropic, llm_utils.py:399-405) changes only for 5.x ids, gated by
`re.match(r"^claude-[a-z]+-5-", model)` (`claude-haiku-4-5-20251001` must not match):
- omit `temperature` (Haiku 5.5: non-default sampling 400s; Sonnet 5.5 treated the same);
- `max_tokens` = caller's + `REASONING_HEADROOM_TOKENS` (thinking is on by default and counts);
- effort: new keyword `enable_thinking=None` passed only when not None (the 65 tests stubbing the
  3-arg shape keep working, llm_utils.py:407-412). `enable_thinking is False` -> `output_config:
  {effort: "low"}` (Ask voice path, extract live pass); else `LLM_REASONING_EFFORT` when
  `low|medium|high`; else no field. Never `thinking.type=disabled`, never `budget_tokens`.
  `call_llm` passes its `enable_thinking` through on the anthropic branch (today dropped,
  llm_utils.py:207-209).
- 200 with `stop_reason == "max_tokens"` and no text -> error `max_tokens before any text`;
  `stop_reason == "refusal"` -> error `refusal`.
- 4.x ids: payload byte-identical to today. test_llm_utils.py:257-266 pins a 4.x id explicitly; a
  5.x sibling asserts no temperature, effort present, headroom added.
- **Merge gate for PR-2:** the owner runs `tools/anthropic-smoke.py` (2 calls through
  `_call_anthropic`: Haiku 5.5 with `LLM_TEMPERATURE=0` + `enable_thinking=False`, Sonnet 5.5 with
  effort high; < $0.05). `output_config.effort` over raw HTTP with `anthropic-version: 2023-06-01`
  is unproven in this repo (the SDK users are bench-only), so PR-2 merges only after both return
  text.
- No behaviour change on prod while `LLM_PROVIDER=qwen`.

## Order, PRs, conflicts

1. PR-1 (tools only): Part A + B1 bench + `tools/anthropic-smoke.py`. No deploy effect.
2. PR-2 (src, inert): Part C + B2. Merge after the smoke gate. Deploys to TEST with today's values.
3. Owner runs B1 (58 calls, bench key) -> sets the template-digitise variables (TEST, hand test, prod).
`template.yaml` moved 15 times since 10-17; the only open PR on it is release #1201. PR-2 is this
line's only template.yaml edit (10 CLAUDE_MODEL lines + the digitise env/parameters), line-local,
rebased on develop right before merge.

## Out of scope

Changing prod web search off gemini; `QWEN_MODEL` / fallbacks; prompt text; deleting claude_utils.

## Review log

rev 1 -> 2 (Fable, 15 findings, all applied): 1 muse refuses reasoning off -> muse arms low/medium;
2 usage.include + pools-only OR writer outside spent_usd; 3 key var names + pre-import env + Pillow;
4 enable_thinking threaded to anthropic; 5 4.x test pin + exact regex gate; 6 PDF coupling + PDF arm;
7 step-0 image probe; 8 smoke = merge gate; 9 digitise model as a parameter, wiring test; 10 keep
MODELS entries, dry run refuses over cap; 11 test pins, muse profile, fresh --out; 12 invented
defined, screen wording, 58 calls; 13 shared parsers, existing payload pins; 14 claude_utils left;
15 template.yaml churn noted.
