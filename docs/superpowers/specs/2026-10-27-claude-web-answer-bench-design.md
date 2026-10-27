# Claude + web search for Ask's web answer — bench design

Date: 2026-10-27 · Line: ASR+LLM优化v3 (Ask orchestration / web) · Status: rev 3.1 (Fable
review applied, §11). No code, no live call has been made for this spec.

## 1. Problem

Ask's general-knowledge chain (`web_answer.general_answer`: draft without web →
verify with OpenRouter's web plugin → one retry if time allows → else the draft
verbatim, "unverified") runs on `google/gemini-3.8-flash` via `corroboration_client`.

| evidence (owner-run, generic questions) | n | result |
|---|---|---|
| drafts, order AI Studio→Vertex (spec 2026-10-23 §6.1) | 40 | AI Studio, p50 3.73, p90 4.17, max 4.5 s |
| verifies, web plugin, same order | 10 | always Vertex; p50 8.5 s, 2/10 over 12 s (13.5, 32.7) |
| AI Studio only, 2026-10-24 (`verify_aistudio_bench.jsonl` + `control_aistudio_bench.jsonl`, owner scratchpad) | 13 + 3 | 13/13 plugin verifies HTTP 429; 3 interleaved no-plugin drafts ok, 3.45-6.39 s — AI Studio on OpenRouter will not serve the plugin |
| 08-31 spec §12, one entity, Anthropic search | 1-3 each | Sonnet 5 + `web_search_20260209` 19.7 s; Opus 5 16.9/17.2 s (`max_uses: 1` 19.9 s); Sonnet 5 + `web_search_20250305` 9.7 s; Haiku 4.5 + `web_search_20250305` 4.2/4.7 s |

**The budget prod hands the chain** (derived from code):
- *Orchestrated path, both channels.* `ask_orchestrator.answer` is called only from
  `ask_voice_turn.py:669` and `:781`, i.e. AskVoiceWorkerFunction, whose
  `ASK_DEADLINE_SECONDS` is 45 (template.yaml:2593). AskAgentFunction's `'24'`
  (template.yaml:2447) does not apply: its turn_start only enqueues
  (lambda_ask_agent.py:2378-2383). `_deadline_s("screen") = min(45, 40) = 40`,
  voice 45 (ask_orchestrator.py:815-824), so `_web_left = min(WEB_WAIT_S − t,
  deadline − now − 14)` (ask_orchestrator.py:696-698) is **≈ 25 − t on both
  channels**. Screen `auto` first pays the precheck, 2.5-2.9 s
  (measured, `PRECHECK_TIMEOUT_S` comment, ask_orchestrator.py:489; call site
  702-705), so t_pre ≈ 2.9 s at worst.
- *Legacy sync path:* `27 − elapsed − compose reserve 4` (lambda_ask_agent.py:868-869),
  ≈ 20 s; not benched separately (P0 states whether it still serves web turns).

The owner has US$100/month Anthropic credit. Question: is Claude + Anthropic's
server-side web search a better verify, or a better whole web answer — and how would
we know? Two gaps:
- **No accuracy measure.** `tools/ask-routing-eval/` is rule-based (`score.py`) and
  scores routing, latency and the gate, not truth. This spec adds one.
- **Prompts are not kept per model.** `DRAFT_PROMPT` / `VERIFY_PROMPT` have been
  gemini-only since #1056; the muse / deepseek work was judge / corroboration
  experiments, not these prompts. A prompt history inventory is being produced
  separately. P2 adds variants so a switch never overwrites a tuned prompt.

**Why corroboration left Anthropic** (eff6063d, on unmerged branch
`origin/docs/corroboration-off-anthropic`; removed in bd9c3266): latency — Anthropic
search took 10 s+ on Opus/Sonnet — plus vendor consolidation, not cost. That is
exactly what Stage 0 (§4.0) re-measures before anything is spent on the full bench.

## 2. Decision rule

One budget (§4.1): both channels hand the chain ≈ 25 − t_pre.

**Hard gates (any failure = arm rejected):**

| gate | B (whole answer) | C / C′ (verify only) |
|---|---|---|
| owner-confirmed hallucinated source (§5.2) | 0 | 0 |
| no-answer items correctly unsure | ≥ 3 of 4 | ≥ 3 of 4 |
| source presence on web-needing items | ≥ 90 % with ≥ 1 result-block source | same, on verified answers |
| raw failure rate (arm's own call gave no answer; `max_tokens` stops excluded, reported separately, §4.3) | ≤ A's verify-failure rate | — (draft fallback exists) |
| verify latency | — | p50 ≤ VERIFY_FLOOR 9 s, p90 ≤ VERIFY_BUDGET 14 s |
| verified share | — | ≥ A's |
| end-to-end p90 / chain budget exhausted (any step timed out) | ≤ 20 s / ≤ 1 of n | ≤ 20 s / ≤ 1 of n |
| cost per answer, incl. per-search fee (B: + the parallel gemini draft, §4.2) | ≤ US$0.10 | ≤ US$0.10 |
| projected monthly spend (prod web turns/month from P0 × cost) | ≤ US$60 | ≤ US$60 |

**Accuracy — paired, per item.** Order labels correct = correctly-unsure > partial >
wrong = hallucinated-source. Against A on the same item (same rep): win / tie / loss.
- **Non-inferior:** losses ≤ 1 over the scored set.
- **Better:** wins − losses ≥ 3, *and* the score difference (correct 1, partial 0.5,
  unsure-correct 1, else 0) exceeds the noise floor F (§5.3).

**Switch** when an arm passes every hard gate, is non-inferior, and
is better on accuracy, on source presence, or on the over-12 / over-25 s tail. If
several qualify: better accuracy → lower end-to-end p90 → cheaper. Faster but losing
on accuracy is a **no**.

**What 20 items can see.** 85 % vs 95 % correct is 17 vs 19 of 20 — inside binomial
noise; only gross differences are visible. "No difference" means "not
distinguishable at n=20", never "equal".

## 3. Gold set

25 generic NZ construction questions, no customer records, no site names. File
`tools/web-answer-bench/set.v1.jsonl`: `{id, split (tune|scored), kind, question,
expected, variants, source, confirmed}`; P0 adds `as_of`, `confirmed_by`,
`confirmed_on` when the owner confirms a row.

- **Splits.** `tune` (5) only for prompt iteration and Stage 0. `scored` (20: 8
  standard, 8 current, 4 no-reliable-answer) is never shown to a prompt author.
- **Confirmation (P0).** The owner confirms each gold fact against the source.
  Unconfirmed items are excluded (not scored); the result states how many.
  TO-CONFIRM rows carry no figure on purpose — the author was not sure.
- **Current items** carry `as_of`; scored run within 14 days of confirmation.
- **Disagreements.** An answer that contradicts gold *and* cites a supporting source
  goes to the owner. Gold wrong → fix, bump `v1` → `v1.1`, re-grade every arm on that
  item from stored answers (no re-run). Listed in the result either way.

| id | split | kind | question | expected key fact(s) | source to confirm |
|---|---|---|---|---|---|
| s1 | score | standard | Under NZBC B2, what minimum durability must structural elements have? | not less than 50 years (also 15 / 5 yr tiers) | NZBC B2, B2.3.1 |
| s2 | score | standard | Which NZBC clause covers external moisture, and its acceptable solution? | Clause E2; E2/AS1 | NZBC E2 |
| s3 | score | standard | At what fall height does NZBC F4 require a barrier? | 1 m or more | NZBC F4, F4.3.1 |
| s4 | score | standard | Which wind zones does NZS 3604 cover, and what applies above them? | Low…Extra High; above = specific engineering design | NZS 3604:2011 §5 |
| s5 | score | standard | What is NZS 3604's scope limit on height / storeys? | TO-CONFIRM | NZS 3604:2011 §1.1 |
| s6 | score | standard | Which timber hazard class is required for in-ground piles? | TO-CONFIRM (H5 expected) | NZS 3602 / 3604 |
| s7 | score | standard | What exposure classifications does NZS 3101 use for concrete durability? | TO-CONFIRM | NZS 3101 Part 1 §3 |
| s8 | score | standard | Statutory timeframe for a BCA to decide a building consent? | 20 working days | Building Act 2004 s48 |
| r1 | score | current | Current MBIE building levy rate and threshold? | TO-CONFIRM | MBIE levy page |
| r2 | score | current | Current BRANZ levy rate? | TO-CONFIRM | BRANZ |
| r3 | score | current | Latest amendment of E2/AS1 and its effective date? | TO-CONFIRM | MBIE document history |
| r4 | score | current | Status of the consent exemption for small standalone dwellings ("granny flats")? | TO-CONFIRM | MBIE / legislation.govt.nz |
| r5 | score | current | Current NZ adult minimum wage? | TO-CONFIRM | employment.govt.nz |
| r6 | score | current | Is NZS 3604 under revision; latest published step? | TO-CONFIRM | Standards NZ / MBIE |
| r7 | score | current | What changed in the most recent Building Code update cycle? | TO-CONFIRM | MBIE |
| r8 | score | current | Must scaffolding work be notified to WorkSafe, from what fall height? | TO-CONFIRM (5 m expected) | HSW (GRWM) Regs 2016 / WorkSafe |
| u1 | score | unsure | What will the MBIE building levy be in 2028? | cannot know | n/a |
| u2 | score | unsure | What does 25 MPa ready-mix cost per m³ in Christchurch this week? | no reliable public figure | n/a |
| u3 | score | unsure | What does NZS 3604:2024 say about bracing? | false premise — TO-CONFIRM no 2024 edition | Standards NZ |
| u4 | score | unsure | How many consents did Selwyn District Council issue last Tuesday? | not published daily | n/a |
| t1 | tune | standard | What does NZBC H1 cover; how many climate zones does H1/AS1 use? | TO-CONFIRM | NZBC H1 |
| t2 | tune | standard | Which NZBC clause covers internal moisture? | Clause E3 | NZBC E3 |
| t3 | tune | current | Current LBP licence renewal cycle? | TO-CONFIRM | lbp.govt.nz |
| t4 | tune | current | Current Schedule 1 exemption size for a detached single-storey building? | TO-CONFIRM | Building Act Sch 1 |
| t5 | tune | unsure | What will timber framing cost per linear metre next quarter? | unsure | n/a |

## 4. Stage 0 and arms

All runs local, owner-run, sequential. Questions are the gold text verbatim. Voice
rules (`voice_web.VOICE_RULES`) are not appended; the voice prompt path is deferred.

### 4.0 Stage 0 — speed probe (before the full bench)

Verify step only, 12 calls per configuration (the 5 `tune` items × 2-3 reps), each
verifying a gemini draft generated once per item (5 OpenRouter calls):
1. Sonnet 5.5 (`claude-sonnet-5-5`) + `web_search_20260209` (the version the docs list
   for Sonnet 5.5), effort low;
2. Sonnet 5.5 + `web_search_20250305` — **unconfirmed** for Sonnet 5.5 (08-31 §12's
   9.7 s was Sonnet 5); a 400 is recorded as "rejected", not "slow";
3. Haiku 5.5 (`claude-haiku-5-5`, $0.10 / $0.50 per MTok) + `web_search_20250305` as
   the first guess — verify against the docs before the run.

**Stop rule (verify budget only).** n = 12 cannot see a p90, so Stage 0 gates on p50
and max: continue only if some configuration has verify p50 ≤ VERIFY_FLOOR 9 s and
max ≤ VERIFY_BUDGET 14 s; otherwise stop and spend nothing on the full bench. The
fastest passing tool version per model is frozen for the bench; a model with no
passing configuration drops its arm. Arm B is **not** probed in Stage 0; its spend in
the full bench is guarded by the hard stop (§6).

**Stage 0 definition** (`tools/web-answer-bench/stage0.py`):
- *Inputs.* The 5 tune rows (t1-t5) of `tools/web-answer-bench/set.v1.jsonl`, question
  only, no VOICE_RULES. Env `ANTHROPIC_API_KEY` + `CORROBORATION_API_KEY` (exit 2 if
  missing). `--out` outside the repo; `--search-price-per-1000` required; `--dry-run`
  prints calls and a $ upper bound; `--confirm-calls N` must equal the planned count.
- *Drafts.* 5 OpenRouter calls via `corroboration_client.call(DRAFT_PROMPT.format(
  question=q), timeout=_draft_timeout(22.1), max_tokens=1024, effort="low")`,
  mirroring web_answer.py:323; text + draft_ms stored.
- *Configs.* (1) `claude-sonnet-5-5` + `web_search_20260209`; (2) `claude-sonnet-5-5`
  + `web_search_20250305` (unconfirmed); (3) `claude-haiku-5-5` + `web_search_20250305`.
  HTTP 400 on the tool → config "rejected", no further calls for it.
- *Per verify call.* Official `anthropic` SDK, `max_retries=0`,
  `VERIFY_PROMPT.format(question, draft)`, tool `{max_uses: 3, user_location:
  approximate NZ / Pacific/Auckland}`, `output_config {effort: "low"}`, no thinking
  field, `max_tokens` 4096, timeout 14 s; one `pause_turn` continuation with the
  remaining time (counts as a call, latency summed); a second pause = failed.
- *Reps.* 12 per config, round-robin t1..t5, t1..t5, t1, t2; sequential per config.
- *Record per call.* config, item, rep, model, tool version, latency_ms, in / out
  tokens, `server_tool_use.web_search_requests`, result count, source count, searched
  (non-empty `web_search_tool_result` list), error-block code, stop_reason,
  continuation flag, HTTP status, prompt sha256. Answer text only in `--out` files.
- *Per-config summary.* p50 and max over all 12 wall times (failures / timeouts
  included, any outcome other than an answer or a max_tokens stop recorded as over 14 s, so a fast failure cannot pass the max gate), searched rate, max_tokens stops, refusals,
  rejected flag, $ = tokens + searches × price.
- *Stop rule* as above.

### 4.1 Budget

One budget for every arm: **25 − t_pre** with t_pre = 2.9 s (the screen precheck's
upper end, ask_orchestrator.py:489; call site 702-705) = 22.1 s, the tighter of the
two channels.
**Pairing:** C and C′ verify A's stored draft, and their chain budget for an item /
rep is `22.1 − A.draft_ms` of that same item / rep, so `VERIFY_RETRY_MIN_LEFT` (10 s,
web_answer.py:341) admits or refuses the retry exactly as it would have in A.
Retries are also reported separately per arm.

### 4.2 Arms

- **A — shipped.** `web_answer.general_answer(question, budget)` imported at the bench
  commit: gemini-3.8-flash on OpenRouter, provider order, shipped prompts. Calls:
  draft 1 + verify 1 + retry ≤ 1. Its drafts are stored per item and rep.
- **B — Sonnet 5.5 single-call answerer.** One call with the web_search tool
  (`max_uses` 3, `user_location {type: approximate, country: NZ, timezone:
  Pacific/Auckland}`), `output_config {effort: low}`, no prefill, never
  `thinking: {type: disabled}` (400). Prompt `claude_answer_v*` (round 0 = DRAFT +
  VERIFY rules merged). B has no draft, so "never nothing" needs a fallback: P2 runs
  the gemini draft **in parallel** and returns it unverified when B fails. The bench
  simulates that from A's stored draft, records B's raw failure rate, and adds the
  draft's cost to B's per-answer cost.
- **C — Sonnet 5.5 verify** and **C′ — Haiku 5.5 verify** (pre-registered). Each
  verifies **A's stored draft for the same item and rep** (paired verify test, no new
  draft calls) with the `VERIFY_PROMPT` text (round 0) or a `claude_verify_v*`
  candidate; question + draft only. Same retry rule as A. The tool settings above
  (`max_uses` 3, `user_location` NZ) apply to every Claude call, B, C and C′. Failure → the draft,
  "unverified".

### 4.3 Claude call contract

- **Bench:** official `anthropic` SDK, `max_retries=0` (the SDK retries by default;
  a retried call measures backoff, not the model). **P2:** see §8.
- **Timeout** = the step's remaining budget, exactly as A: verify
  `min(VERIFY_BUDGET, left())` (web_answer.py:350). A `pause_turn` continuation gets
  the **remaining** step budget, counts as a call and in latency; a second pause =
  failed step.
- **`searched`** = at least one `web_search_tool_result` block whose content is a
  non-empty results list. An error object in that block (`max_uses_exceeded`,
  `too_many_requests`, `unavailable`, …) arrives as HTTP 200 and is a failed search.
  Prose claiming a search is not evidence (`_verify_ok`).
- **`stop_reason`:** `refusal` → failed step (C/C′: draft unverified; B: raw failure).
  `max_tokens` is raised to 4096 for Claude calls (prod verify's 2048 is too tight once
  thinking and tool-use output count against it); any `max_tokens` stop is still a
  failed step, reported separately and excluded from the raw-failure gate (§2).
- **`thinking: {type: between_tools}`** (effort ≤ high, no other thinking field) is a
  tune-round variable only. Expected failure mode: the tool call written as visible
  text instead of a real `server_tool_use` — `searched` catches it. Not a free
  latency knob.
- **Sources** = result-block URLs, deduped, ≤ 4 (as `_sources`).
- **Billing:** tokens (results count as input) plus a per-search fee; the price is
  taken from the pricing page by the owner and passed to the harness (§6).

### 4.4 Prompt iteration — prompt × model matrix

No prompt has ever been tuned for a model other than gemini (inventory: 55 prompts,
134 versions, none model-conditional; per-model tuning lived in parameters). So
before the arms are frozen, every historical candidate runs on every model that can
do the step. Only `tune` items (5) are used; the author sees tune answers and grades,
never `score` items.

**Cell** = (step, prompt version, model, parameters). Parameters are part of the
cell, not a constant: effort / reasoning setting, provider pin, `max_uses`.

**Historical candidates** (texts read from git, sha256 recorded per cell):

| id | step | source | note |
|---|---|---|---|
| `draft_v1` | draft | `DRAFT_PROMPT`, 518900a8 (unchanged since) | only draft version |
| `verify_v1` | verify | `VERIFY_PROMPT`, 518900a8 ("colleague") | on gemini it wrote review-style replies (b8c96342 comment); re-tested on other models |
| `verify_v2` | verify | `VERIFY_PROMPT`, b8c96342 (shipped) | |
| `web_v1` | single call | `WEB_PROMPT`, 518900a8^ | arm B shape |
| `merged_v1` | single call | `draft_v1` rules + `verify_v2` rules in one prompt | arm B round 0 (§4.2) |
| `search_v1` | verify | `corroboration.SEARCH_PROMPT`, e73e90c2, one entity = the draft | the only prompt that ran on Claude + web_search (claude-opus-5 / haiku-4.5) |

**Models by step** (admission by capability, with the evidence):
- *draft* (no search): gemini-3.8-flash (shipped), DeepSeek V4.1 Flash @ coreweave
  (reasoning off), Sonnet 5.5, Haiku 5.5 (effort low).
- *verify / single call* (must search): gemini-3.8-flash + OpenRouter web plugin
  (shipped), DeepSeek V4.1 Flash + OpenRouter web plugin (never measured; one cell),
  Sonnet 5.5 + `web_search_<version Stage 0 picked>`, Haiku 5.5 + `web_search_20250305`.
  A Claude config that failed Stage 0's gate is not tuned.
- **Excluded:** muse-spark (web plugin: 16-29 s and 0 sources while claiming to have
  searched, ledger 09-08 / 09-21) — over `VERIFY_BUDGET` before accuracy matters.

**Round 0** = every admitted (step, candidate, model) cell, 5 items × 3 reps. Verify
cells verify gemini's stored `draft_v1` for that item / rep (paired, as §4.2), so a
verify difference is the verify prompt's, not the draft's. Draft cells are graded as
standalone answers (they are what the reader gets when verify fails).
**Rounds 1-2** = per model, the best round-0 verify / single-call cell gets at most 2
edits (`<model>_verify_v*`, `<model>_answer_v*`), each 5 × 3; an edit is kept only
if it is not worse on any item and better on at least one (paired, tune only).
Total rounds per model ≤ 3, as before.

**Freeze.** Per (step, model) the chosen prompt + parameters are written to
`tools/web-answer-bench/frozen.v1.json` (text, sha256, parameters, round, tune
grades) before any `score` run. This file is the seed of P2's `web_prompts.VARIANTS`
— a model switch reads its own entry and never overwrites another model's.

**Gate.** Round 0 runs only after (1) Stage 0 has picked the Claude configs and (2)
the owner has confirmed the 5 `tune` gold answers; grades need `grade.py` (§5.1).

## 5. Grading

### 5.1 Blind grader

New `tools/web-answer-bench/grade.py` (the routing eval's `score.py` is rule-based
and has no model grader; `llm_tools_client` defaults to gemini, llm_tools_client.py:59,
the same family as A). It calls **DeepSeek V4.1 Flash via OpenRouter with provider
`coreweave`**, the prod judge's model and provider, using the existing OpenRouter key
(the one `llm_tools_client` reads, llm_tools_client.py:246). One call per answer:
question, gold `expect` / `accept`, answer text, normalised source domains, no arm
label. Answers shuffled (seed stored); `[unverified]` markers and link markup
stripped from the grader's copy. Residual style leakage is noted, not solved.

Rubric: `correct` (all key facts, nothing false) · `partial` (some key facts, nothing
false) · `wrong` (a false figure or clause) · `correctly_unsure` (no-answer items) ·
`hallucinated_source` (§5.2).

### 5.2 Hallucinated source — symmetric across vendors

Gemini's result blocks are Google grounding redirects whose host is in the title
(`_domain`, web_answer.py:431-435), and A's prompt asks for sources inline, so the
real hosts gemini writes appear in no result block as URLs. Comparison is therefore
**by domain**: the set of domains of **all** `search_results` (not `_sources`, which
keeps ≤ 4, web_answer.py:413-424) via `_domain(url, title)` (title-host for gemini,
URL-host for Claude) against the hosts of **http(s) literals** in the answer text. A
literal whose domain is not in that set is **flagged**. First, on the tune items,
check whether gemini's inline literals are `vertexaisearch.cloud.google.com`
redirects; if so, such a literal counts as in-set when it equals a result-block URL,
and the rule is fixed before the scored run. Named
documents ("NZS 3604 §5") are not checked mechanically. Whether a page actually
contains the claim is checked **only by the owner, only on flagged pages**; the veto
in §2 counts owner-confirmed cases.

### 5.3 Owner spot-check, noise floor, n

- Owner grades 32 scored answers (8 per arm) blind to the grader. Report agreement
  and Cohen's κ; agreement < 80 % → the grader's labels are not used.
- Noise floor: 6 scored items (2 standard, 2 current, 2 unsure) × 3 reps per arm.
  F = max over arms of (items whose label changed across reps) / 6 × 20, rounded up,
  min 1. Used only for "better by more than F".
- Latency n per arm = 32 (20 scored + 12 repeats) ≥ the n ≥ 20 floor.

## 6. Measurement, privacy, keys

- **Per call:** arm, item, rep, step, model, served_by, latency ms, input / output
  tokens, `usage.server_tool_use.web_search_requests`, result count, source count,
  stop_reason, continuation, error class, prompt sha. Per answer: grade, cost.
- **No text in logs.** Answers go only to the output file. P2 Lambda logs carry the
  numbers above, never question or answer text.
- **Outputs stay local.** `--out` required, must resolve outside the repo (owner's
  scratchpad); the harness refuses a path inside the worktree.
- **Spend.** `--search-price-per-1000` is required (owner sets it from the pricing
  page). `--dry-run` prints call counts, search upper bound and a dollar upper
  bound; a live run needs `--confirm-calls N`. Hard stop: a **persisted ledger file
  in `--out`**, cumulative across Stage 0 and the full bench, counting Anthropic
  dollars only (tokens + `web_search_requests` × search price); no call is made once
  it reaches US$45.
- **Bench keys: two.** Anthropic (the owner creates it) and the existing OpenRouter
  key (gemini + grader). An owner-run wrapper (not committed) puts them in the
  environment for one process; the harness reads env only, never prints them,
  redacts them from exception text, exits 2 if one is missing.
- **Prod keys (P2/P3)** mirror `CorroborationApiKey` (deploy.yml:311,
  deploy-prod.yml:341): repo secret `WEB_ANSWER_ANTHROPIC_API_KEY` → NoEcho Parameter
  `WebAnswerAnthropicApiKey` (default '') passed by both workflows → env
  `WEB_ANSWER_ANTHROPIC_API_KEY` on AskAgentFunction and AskVoiceWorkerFunction only.
  **Not `ClaudeApiKey`**: it already lands on those functions *and* on the report /
  minutes / session-report functions, and `deploy-prod.yml` defaults `LlmProvider` to
  `anthropic` — swapping that secret could bill report generation to the credit key.

## 7. Cost

Stage 0 (separate, runs first): 5 gemini drafts + 3 configs × 12 verifies (≤ 2 calls
each with continuation) = **≤ 77 calls, ≤ 216 searches, ≤ ~$9** (Sonnet 48 calls ×
≈ $0.133 upper ≈ $6.4, Haiku ≈ $0.2, searches 216 × price — ≈ $2.2 at $10 / 1 000);
expected ≈ $3.

Full bench: 47 runs per arm (20 scored + 12 repeats + 15 tune), 4 arms, 188 answers.

| | upper bound | expected |
|---|---|---|
| OpenRouter gemini (A: 47 × ≤ 3) | 141 | ~103 |
| Anthropic Sonnet (B 47 × ≤ 2, C 47 × ≤ 2) | 188 | ~105 |
| Anthropic Haiku (C′ 47 × ≤ 2) | 94 | ~52 |
| grader, DeepSeek via OpenRouter (1 per answer) | 188 | 188 |
| **total model calls** | **611** | **~448** |
| Anthropic searches (`max_uses` 3 + `user_location` NZ on every Claude call) | 846 | ~235 |

Dollars (tokens per search ≈ 15 k is the dominant unknown):
- Sonnet tokens: upper 188 × (46 k × $2 + 4.1 k × $10)/M ≈ **$25** (max_tokens 4096);
  expected 105 × (20 k × $2 + 0.7 k × $10)/M ≈ **$5**.
- Haiku tokens: ≤ $0.7.
- Searches: 846 / 235 × the per-search price — ≈ $8.5 / $2.4 at $10 / 1 000.
- OpenRouter (gemini + plugin + DeepSeek grading): under $5.
- **Full bench ≈ $13 expected, ≤ ~$39 upper; Stage 0 ≤ ~$9.** Hard stop $45.

Prompt matrix (§4.4), tune items only, 5 × 3 = 15 runs per cell:

| cells | round 0 | calls (upper, with continuation / retry) |
|---|---|---|
| draft: 4 models × `draft_v1` | 4 | 60 (no search) |
| verify: gemini, DeepSeek × `verify_v1/v2`, `search_v1` | 6 | 90 OpenRouter |
| verify: Sonnet, Haiku × `verify_v1/v2`, `search_v1` | 6 | 180 Anthropic |
| single call: gemini, DeepSeek × `web_v1`, `merged_v1` | 4 | 60 OpenRouter |
| single call: Sonnet, Haiku × `web_v1`, `merged_v1` | 4 | 120 Anthropic |
| rounds 1-2: ≤ 2 edits × 8 (model, step) | ≤ 16 | ≤ 300 (Claude share ≤ 240) |
| grader, 1 per answer | — | ≤ 840 (DeepSeek, cheap) |

Anthropic ≤ 540 calls, ≤ 1 620 searches upper (expected ~2 per call ≈ 700).
Dollars: Sonnet ≤ 270 calls × $0.133 ≈ $36 upper, expected ≈ $7; Haiku ≤ $1.5;
searches $16 upper / $7 expected at $10 / 1 000; OpenRouter < $6. **Matrix ≈ $15
expected.** The upper bound exceeds the remaining hard-stop room, so the ledger
stops it first: matrix + full bench share the one $45 ledger, and the harness
refuses to start the scored run if fewer than the full bench's expected $13 remain.

## 8. Phases

- **P0 — inputs (owner).** Confirm the gold set (exit: ≥ 18 of 20 scored items);
  check the per-search price; create the Anthropic key. From prod logs of the
  **orchestrated path (AskVoiceWorkerFunction)** read the monthly web-turn volume and
  the failure rates from `ASK_ROUTE` `web_failed` / `web_late`
  (ask_orchestrator.py:1407, 775) plus the surviving WARNING "verify attempt … failed"
  line (web_answer.py:359). Verify ms cannot be read there: `web_tool` strips `_trace`
  (ask_orchestrator.py:630-631), route `ms` holds only judge / total, and the INFO
  lines (web_answer.py:342, 354) are dropped — latency comes from the bench.
- **P1 — Stage 0, then the bench (owner-run).** Build `tools/web-answer-bench/`
  (unit tests on recorded SDK responses: `searched`, error blocks, `pause_turn`,
  refusal / max_tokens, domain matching, C budget pairing, blinding, paired scoring —
  no live call). Stage 0 → stop or continue → tune rounds → freeze → scored run +
  repeats → grade → owner spot-check → result appended here.
- **P2 — if an arm wins: TEST behind a flag.**
  - *Client.* Trade: (a) recover `bd9c3266^:src/corroboration_client.py`, a tested
    ~250-line urllib3 client for `/v1/messages` whose `_parse` already separates the
    error dict from the result list and handles refusal, with fixtures in the old
    tests — no layer, no cold start; or (b) the SDK + a new layer
    (`infra/dashscope-layer` pattern). **Recommend (a):** Lambdas ship stdlib +
    urllib3 only. The recovered file lived at today's client's path, so it lands
    under a new module name (e.g. `src/anthropic_search_client.py`), fixtures with it. Add `max_tokens` / `pause_turn` handling and return the same `Reply`
    shape so `_verify_ok`, `_sources`, `_domain` are unchanged.
  - *Prompt variants* — only where a model choice exists: the verify prompt and B's
    merged prompt (classify / compose always run on `CHEAP_MODEL` gemini,
    web_answer.py:241-244, 390-394). `src/web_prompts.py`:
    `VARIANTS = {"default": {...}, "claude": {...}}`, family from the model id,
    `prompt_for(step, model)` falls back to default. `web_answer.DRAFT_PROMPT` /
    `VERIFY_PROMPT` stay as aliases of the default texts (pinned by
    tests/unit/test_answering_from_the_open_web.py:356, 374 and test_voice_web.py).
  - *Flag* `WebAnswerVerifyProvider` (`openrouter` | `anthropic`, default
    `openrouter`, read per call); for B also the parallel gemini draft.
  - *TEST gate:* routing eval's web turns (call count stated first) + the scored set
    through the TEST worker; latency within §2; no lost turn.
- **P3 — prod.** Owner sets the secret and flag (after §9 Q3); release alone; read a
  week of served_by, latency, search counts and spend against the credit.
- **Rollback:** repo variable `WebAnswerVerifyProvider=openrouter` + redeploy (a
  console edit is wiped by the next SAM deploy). The gemini path stays while the flag
  exists.

## 9. Risks and open questions

- **Q2.** Monthly web-turn volume — unknown until P0. At ~$0.05–0.10 per answer the
  credit covers ~1 000–2 000 answers.
- **Q3. Privacy boundary moves.** Today one OpenRouter key sees `chunk_text` and the
  web chain. B / C send question + draft to Anthropic, which does not see
  `chunk_text`, and its search backend is a new third party for the question's words
  (the `ENABLE_EXTERNAL_CORROBORATION` comment ties that exposure to pilot
  contracts). `corroboration_gate`: "check the two API keys". Owner sign-off before P3.
- **Q4.** On screen, t_pre (the 2.5-2.9 s precheck) is the lever on the chain's
  budget, not the vendor; noted, out of scope.
- Search-result token size drives cost and latency; `max_uses` may drop after Stage 0.
- Rate limits on a new low-tier key; `too_many_requests` blocks recorded separately.
- Grader leakage by style; the owner spot-check and κ are the check.
- Gold decay (`as_of` + 14 days). n = 20 sees only gross accuracy differences.
- Sonnet 5.5 thinking shape (`disabled` = 400) pinned by a unit test on a recorded
  response, so a model-id swap cannot 400 in prod.

## 10. Out of scope

Shipping a Claude draft (it is measured in §4.4 only; gemini draft is 3.7 s p50);
changing ANSWER_RESERVE_S / deadlines;
classify / compose models; voice prompt rules.

## 11. Review log

rev 1 — draft.

rev 2 — Fable review, all applied:
1. BLOCKER hallucinated-source veto vendor-asymmetric → §5.2: domain comparison via
   `_domain` (title-host gemini, URL-host Claude), http(s) literals only; semantic
   check by owner on flagged pages; veto counts owner-confirmed cases.
2. BLOCKER A not "as deployed" at 25 s → §1 budgets read from template.yaml:2447,
   2593, ask_orchestrator.py:483-487/696-698/814-824, lambda_ask_agent.py:868-869;
   §4.1 voice run + offline screen replay (superseded rev 3); every gate names its
   column (superseded rev 3); P0 confirms from `ASK_ROUTE` `ms` (superseded rev 3).
   Added Q4 (screen verify rarely fits today) (superseded rev 3).
3. HIGH 08-31 §12 evidence → §1 table; §4.0 Stage 0 with stop rule; Haiku 5.5 arm C′
   pre-registered. Added a Sonnet + basic-tool config to Stage 0 because §12 shows
   the basic tool halved Sonnet 5's time.
4. HIGH Q1 answered → §1 (eff6063d / bd9c3266, latency + consolidation); §8 P2
   client trade, recommend recovering the urllib3 client; SDK only in the bench.
5. HIGH noise-floor non-inferiority → §2 paired per item (losses ≤ 1, wins − losses
   ≥ 3); F only for "better by more than F"; 85 % vs 95 % stated indistinguishable.
6. HIGH grader → §5.1 new grade.py, DeepSeek V4.1 Flash via OpenRouter / coreweave
   with the existing key; third secret dropped (§6).
7. MEDIUM `between_tools` failure mode → §4.3.
8. MEDIUM continuation timeout = remaining step budget; C uses
   `min(VERIFY_BUDGET, left())` → §4.3.
9. MEDIUM B gate vs A's verify-fail rate; parallel draft in B's cost → §2, §4.2.
10. MEDIUM C reuses A's stored draft → §4.2; −47 calls (C′ likewise).
11. MEDIUM hard stop includes `web_search_requests` × price (CLI arg); dry-run $ → §6.
12. MEDIUM registry scope verify + B only; aliases kept for the named tests → §8.
13. MEDIUM `refusal` / `max_tokens` → §4.3.
14. LOW key wiring mirrors CorroborationApiKey → §6.
15. LOW AI-Studio 429 evidence cited with files and controls → §1.
16. LOW §1 corrected: prompts gemini-only since #1056; inventory referenced.

rev 3 — Fable second pass (NOT-READY, one blocker), all applied:
1. BLOCKER screen budget read from the wrong Lambda (AskAgentFunction's 24 s only
   enqueues; the orchestrator runs in AskVoiceWorkerFunction at 45 s, screen deadline
   min(45, 40)) → §1 rewritten: ≈ 25 − t on both channels, legacy ≈ 20 s; §4.1 one
   budget 25 − 2.9 = 22.1 s; offline screen replay dropped; Stage 0 stop rule = verify
   gate only; Q4 rewritten as "t_pre is the lever"; every "≈ 10 s on screen / verify
   rarely finishes" statement removed; §2 gate columns removed.
2. HIGH C/C′ retry pairing → §4.1: budget = 22.1 − A.draft_ms per item / rep; retries
   reported separately.
3. MEDIUM (a) P0 log path named (AskVoiceWorkerFunction), rates from `web_failed` /
   `web_late` + the WARNING line; verify ms from the bench only → §8 P0.
   (b) Sonnet 5.5 + basic tool marked unconfirmed, 400 = "rejected"; Haiku basic tool
   first guess → §4.0. (c) Stage 0 raised to 12 per config, gated on p50 + max →
   §4.0, §7. (d) Claude max_tokens 4096; `max_tokens` stops reported separately and
   excluded from the failure gate → §4.3, §2. (e) domain set = all `search_results`
   → §5.2.
4. LOW vertexaisearch inline-literal check on tune items first → §5.2; recovered
   client gets a new module name → §8; expected cost ≈ $13 → §7 (upper also rises to
   ≈ $39 from max_tokens 4096; hard stop raised to $45).

rev 3.1 — Fable third pass (READY-WITH-FIXES), all applied:
1. §2 gate "over WEB_WAIT_S 25 s" → "chain budget exhausted (any step timed out)".
2. 2.5-2.9 s cited to `PRECHECK_TIMEOUT_S` comment, ask_orchestrator.py:489; 702-705
   named as the call site (§1, §4.1).
3. Hard stop = persisted ledger in `--out`, cumulative Stage 0 + full bench, Anthropic
   $ only, $45 (§6).
4. B not probed in Stage 0; guarded by the hard stop (§4.0).
5. `max_uses` 3 + `user_location` NZ on all Claude calls (§4.2, §7).
6. Rev-2 lines on offline screen replay / screen verify tagged superseded.
7. Full Stage 0 definition block added to §4.0 per Fable's checklist.

rev 4 — owner request 10-27: tune prompts per model before freezing. §4.4 rewritten
as a prompt × model matrix (historical candidates from git × every model able to do
the step, parameters part of the cell, freeze file seeds P2 variants); §7 matrix
cost; §10 Claude draft now measured (not shipped). Pending Fable delta review.
