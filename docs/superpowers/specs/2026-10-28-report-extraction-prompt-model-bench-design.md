# Daily report + session extraction — prompt × model bench

Date: 2026-10-28 · Line: ASR+LLM优化v3 (bench owner, per owner 10-27) · Status: rev 2
(Fable review applied, §10). Production prompt changes that come out of this go through
the report / extraction line (authors of #864, #1022-#1069, #914-#997); this spec
changes nothing in `src/`.

## 1. Problem

No model has ever had its own tuned prompt for the daily report or the session
extraction. Every vendor switch (Claude Sonnet 4.6 in July → qwen3.7-max → muse
09-07 → muse with luna / mimo fallback 10-08) reused the text it inherited; the one
model-driven edit was muse's `Bullet 1:` placeholder echo (31093b2d, minutes schema).
What is measured is model-vs-model on one prompt, and thin:

| model | task | evidence |
|---|---|---|
| muse | daily report replay (n=3, one thin day) | no invention; 19-41 s; reasoning cannot be turned off; reasoning eats `max_tokens` (empty 200) |
| DeepSeek V4.1 Flash @ coreweave | same replay | 3-4× faster; invents `key_decisions` 2/3, an "EOD" action 1/3 (schema example at lambda_report_generator.py:201 is the likely source) |
| mimo | fallback drill | thinking on: 0/4 parsed; off: 2/2 |
| gemini, Sonnet 5.5, Haiku 5.5 | — | nothing on these tasks |

Run-to-run variance on extraction is larger than prompt or model differences (27 runs,
action counts 1-9 at temperature 0; unions of repeated runs grow ~+5 items per run and
do not converge). So: n = 3 per cell, per-item gold written **before** looking at model
output, and comparisons only "better by more than the noise floor".

**Owner decisions (10-27):** models = muse, DeepSeek, gemini, mimo, Sonnet 5.5, Haiku
5.5; candidates = prompt versions changed in Sep–Oct plus the current one; a model that
underperforms gets its own tuned prompt, recorded per model.

## 2. Tasks and candidates

Inventory: every commit since 2026-08-25 compared by definition and by **rendered
prompt hash** on a fixed input, from a snapshot of `src/` at that sha imported cold
(`git archive <sha> src`, `python -I`); single-file imports against today's modules
are not faithful. P0 pins every candidate's rendered hash in a test.

**Import-time environment for every snapshot** (read at import by `llm_utils` and the
lambdas; a missing one silently changes the request): `LLM_PROVIDER=qwen`,
`QWEN_BASE_URL` = the OpenRouter URL (non-DashScope), `LLM_REASONING_EFFORT=high`,
`LLM_REASONING_HEADROOM=8000`, `LLM_TEMPERATURE` unset (prod sends none),
`EMIT_EVIDENCE=false`, `DECLARE_CONTINUITY=false`, `STATUS_SUGGESTIONS=false`
(lambda_extract_session.py:294, 297, 302), `S3_BUCKET=dummy`, `AWS_DEFAULT_REGION`.
A P0 test renders the payload with `build_qwen_payload(..., model=)` and asserts
`reasoning.effort == "high"`, the headroom, `json_object`, no temperature.

### 2.1 Extraction (final pass — the result prod keeps)

`lambda_extract_session.build_extraction_prompt` (+ `_instructions_block`,
`EXTRACTION_SCHEMA`). Seven text versions in the window; four candidates:

| id | sha | what it is |
|---|---|---|
| `X-0901` | 763b3082 | Sep-1 text (byte-identical to e62cbd30 unless names are passed) |
| `X-0917` | 83abd0d4 #864 | action wording + explicit admission test; do not guess responsible / deadline |
| `X-1005` | 8703e69a #1052 | everything before #1069 (markers, inspections) |
| `X-cur` | 82036cf2 #1069 = develop (flags off) | `responsible` = person / crew / trade / "Speaker" / null |

All candidates run on **today's** turn assembly (incl. #842 agent-playback filtering):
the bench compares prompt text, not the input pipeline. Marker / inspection-only steps,
the live (non-thinking) pass and group extraction are out of scope.

### 2.2 Daily report (nightly `lambda_report_generator`)

The rendered inline prompt is identical across Sep–Oct; prod and TEST serve the S3
`config/prompt_templates.json` (git copy v3.0, March). One candidate:

| id | what |
|---|---|
| `R-cur` | develop + the S3 template **as prod serves it**, read from the `prompt` field of an owner-folder `reports/<date>/<owner-folder>/daily_report_debug.json` (never `summary_report_debug.json`); if it differs from git v3.0 the debug copy wins and the diff is recorded |

The pre-#950 weather block is not a candidate: weather is now written by code, so a win
there could not ship. The daily report is therefore a **model** comparison with
per-model tuning (§6), not a version comparison.

### 2.3 Template / session report (`report_template.render_prompt`)

All Sep–Oct report-prompt churn is here (#859 → #997). Customer-facing. Candidates
(P0 re-runs the rendered-hash inventory on `render_prompt` and may merge identical
ones):

| id | sha | what |
|---|---|---|
| `T-0928` | e4e2ea19 #943 | uncovered topics named; before section modules |
| `T-1001` | 751481a3 (= f2191232^) | last version written from the transcript (incl. #975 modules, #983 actions once) |
| `T-cur` | develop | written from the extraction record (#997) |

`T-cur` consumes an extraction. Its input is one **pinned** extraction artifact per
session: muse `X-cur` rep 1, stored with its sha256 and reused by every T-cur cell. T-cur
is scored against gold *and* against its input list (coverage of the input's true items),
so a T-cur shortfall that is really muse's recall is visible. `T-1001` vs `T-cur`
compares pipelines (transcript vs record) and is reported as such.

Prod's retry is mirrored: on `finish_reason = length` (or Claude `max_tokens`) one retry
at effort low with 12 000 tokens (lambda_session_report.py:1010-1024); both attempts are
recorded, the run's outcome is the retry's.

## 3. Models and parameters (a cell = prompt × model × parameters)

| key | model | parameters |
|---|---|---|
| muse | meta/muse-spark-1.3-contributor | prod body via `llm_utils.build_qwen_payload(..., model=)`: effort high, answer + 8 000 headroom, `json_object` |
| deepseek | deepseek/deepseek-v4.1-flash | same prod body + `provider {only: ["coreweave/fp8"], allow_fallbacks: false}` (as the 10-16 replay) |
| gemini | google/gemini-3.8-flash | prod body + `corroboration_client.provider_preference` order |
| mimo | xiaomi/mimo-v2.6-pro | prod branch: reasoning off, no headroom, `json_object` (llm_utils.py:483-491) |
| sonnet | claude-sonnet-5-5 | Anthropic SDK direct from the bench, streaming: `thinking {type: adaptive}`, `output_config.effort` high, `output_config.format` json_schema built from the task's schema (the analogue of `json_object`; T is free text, no format), `max_tokens` = the same answer budget + 8 000, no temperature, `stop_reason` checked |
| haiku | claude-haiku-5-5 | as sonnet |

- **Why not `llm_utils._call_anthropic`:** it drops `force_json`, sends no thinking /
  effort, has no headroom, ignores `stop_reason`, and sends `temperature` when set
  (TEST's `0` is a 400 on these models) (llm_utils.py:395-455).
- **Answer budgets** pinned per task from prod: extraction `max_tokens_for(n)` =
  `min(8192 + 700 n, 32000)`; report `min(8192 + 700 n, 32000)`; template 8 000 (retry
  12 000). Headroom 8 000 for every reasoning vendor (mimo none, as prod).
- **Timeouts:** per call = prod `LLM_HTTP_TIMEOUT` 540 s; no fallback chain (the bench
  measures one model). Every OpenRouter response's `provider` is recorded.
- Reasoning tokens recorded for every vendor that reports them.

## 4. Inputs and data rules

- **Data:** owner-test folders only (Ben_Lin_test2 / test3, Ben_UCPK2 and the owner's
  own recordings), prod and TEST buckets, **read-only** (a read-only AWS profile or
  session policy). The collector refuses any other folder. No customer folder. All six
  vendors already process this data class in prod (Anthropic in July, OpenRouter
  muse / mimo / gemini / DeepSeek since Sep–Oct).
- **Set:** 4 extraction sessions (one dense > 40 min, one short, one mixed Chinese /
  English, the `task_admission_ground_truth.json` meeting) + 2-3 days for R (one dense)
  + the same 4 sessions for T. Held-out: 2 further sessions (§6).
- **Collection** reuses `scripts/extraction_ab.py`'s real turn assembly (`--turns-file`)
  and writes only under the bench `--out` (outside the repo).
- **Never write** `transcripts/` (fires the live extraction), `extraction_requests/`
  (fires the final pass), `extractions/`, `reports/`.

## 5. Gold and scoring

### 5.1 Gold, in two steps

1. **Blind listing first** (the denominator). Before any model output is shown, the
   owner reads each session's transcript on a local page and lists the action items
   (what, responsible, due) and decisions that were actually made. This fixes recall's
   denominator without anchoring on model output (memory: task-admission gold was only
   valid because the participant judged before seeing outputs). The task-admission
   meeting's 5 true / 3 false / 1 unresolved are pre-filled (unresolved → `unsure`).
2. **Bounded adjudication.** Model items are matched to the owner's list (CJK-aware fuzzy
   match, as `extraction_ab --analyse`); only **unmatched** items go to the owner, deduped
   across all cells, each marked `true` (owner missed it — added to gold) / `misread`
   (an utterance exists, but it is not a commitment / decision) / `fabricated` (no
   supporting utterance). P0 measures the unmatched pool on one session from a 1-rep
   pilot before any owner time is booked; if it exceeds 60 items per session, the page
   shows items produced by ≥ 2 reps first and the rest only on request.

Only action items and decisions are gold-scored. Topics and findings get mechanical
checks only. `continuity_eval/label.py` is a pattern for the page, not reused code.

### 5.2 Per-run metrics

- **Mechanical:** parsed (`extract_json`) · schema keys · finish normal (not length /
  max_tokens / refusal / empty 200) · placeholder scan (the 14 patterns of the 09-08
  replay: `Bullet N`, `[insert`, `{{`, `<field>`, `YYYY-MM-DD`, …) · latency · tokens ·
  reasoning tokens · $ · provider.
- **Against gold:** action recall and precision; decision recall and precision;
  `fabricated` count; `misread` count; `responsible` correct (person / crew / trade /
  Speaker / null); `due` kept when gold has one.
- **T:** coverage of gold-true actions / decisions and of the pinned input's true items
  (T-cur), fabricated count, every template section present. Matching T's free text to
  gold is done by fuzzy match + the owner confirming only uncertain matches.

### 5.3 Noise floor and failure

- Per (cell, session): an item is *stable* when all 3 reps agree on it (present /
  absent). A metric's **noise floor F** for a cell = the max over sessions of its range
  across the 3 reps.
- A cell **fails a session** when ≥ 2 of 3 reps are unparsed, truncated (after the T
  retry), contain a placeholder, or contain a `fabricated` item.

## 6. Rounds and the per-model tuning rule

- **Round 0:** every candidate × every model × 3 reps.
- **Underperforming** (owner's word, made testable; per model, using its best round-0
  cell per task):
  (a) fails ≥ 2 of the 4 sessions (§5.3); or
  (b) action recall below the **cross-model median** of best cells by more than that
      model's F on ≥ 2 sessions; or
  (c) median `fabricated` ≥ 1 on ≥ 2 sessions.
  `misread` is reported, not a trigger (most false items are misreads and every model
  has some).
- **Rounds 1-2:** only for underperforming models, at most 2 edits per (model, task),
  written against the observed failure (e.g. remove the quotable example DeepSeek
  echoes; shorten instructions that eat muse's reasoning). Same sessions × 3 reps.
  **Keep rule:** no session worse by more than F on the triggering metric, and at least
  one session better by more than F on it, with the gates of §5.3 never relaxed; new
  unmatched items go through §5.1 step 2 before judging.
- **Held-out:** before any recommendation, every frozen cell runs once on 2 new
  sessions × 3 reps; the result is reported as-is (no further selection).
- **Record:** `tools/report-bench/frozen.v1.json` per (task, model): candidate id or
  prompt text + sha256, parameters, round, metrics, `evidential: false` for the
  selection numbers and the held-out numbers alongside. This seeds a per-model prompt
  registry (prompt + parameters keyed by model; a switch reads its own entry and never
  overwrites another's); wiring it into `src/` is a later spec for the report /
  extraction line.

## 7. Cost, calls and the ledger

Input sizes ≈ extraction 15-60 K tokens; report 20-100 K; template 10-30 K.

| | cells | sessions / days | reps | runs |
|---|---|---|---|---|
| pilot (pool size, P0) | 6 (X-cur) | 1 | 1 | 6 |
| extraction round 0 | 4 × 6 = 24 | 4 | 3 | 288 |
| daily report round 0 | 1 × 6 = 6 | 3 | 3 | 54 |
| template report round 0 | 3 × 6 = 18 | 4 | 3 | 216 (+ ≤ 216 retries) |
| rounds 1-2 (≤ 2 edits × ≤ 6 models × 3 tasks) | ≤ 36 | 3-4 | 3 | ≤ 396 |
| held-out | ≤ 18 frozen | 2 | 3 | ≤ 108 |

Per Claude run (Sonnet, effort high, ~50 K in, answer ≤ 32 K + 8 K headroom): upper ≈
50 K × $2 + 40 K × $10 = **$0.50**, extraction worst case ≈ $0.58; expected ≈ $0.15.
Haiku ≈ 1/20 (prompts > 100 K tokens bill at $0.50 / $2.50 per MTok: counted at that
rate when the input exceeds 100 K).

- **One persisted ledger** shared with the web bench (`anthropic_ledger.json` in the
  same `--out`), with per-bench pools: web $45 (its own sub-pools unchanged), **report
  $40**; the global cap is $85, inside the $100 / month credit.
- Inside the report pool: **held-out reserve $9** (18 Sonnet runs × $0.50) is set aside
  before round 0; round 0 (≈ $14 expected, Sonnet ≈ 93 runs ≤ $47 upper) runs
  extraction → template → report and stops at $31, so a cap hit loses the least-changed
  task; rounds 1-2 run only with room above the reserve.
- OpenRouter (muse / DeepSeek / gemini / mimo ≈ 380 round-0 runs at ≤ $0.05): ≤ $19.
- Every live step: `--dry-run` prints calls and $ upper; `--confirm-calls N` required;
  the owner runs it (keys).

## 8. Phases

- **P0 (Claude, offline):** snapshot builder + rendered-hash and payload tests (§2);
  T inventory re-run; collector (owner folders only, read-only, never-write list);
  S3 template check from an owner-folder debug JSON; blind-listing page; adjudication
  page; ledger pools.
- **P1 (owner):** collect → blind listing (4 sessions) → pilot → round 0 →
  adjudication → score → per-model verdicts.
- **P2:** rounds 1-2 for underperforming models → held-out → `frozen.v1.json` → report
  to the owner and the report / extraction line.
- **Out of scope:** prod prompts, `LlmProvider`, the fallback chain, the live pass, group
  extraction, marker / inspection scoring, luna.

## 9. Risks

- Owner time: blind listing ≈ 10-15 min per session; adjudication bounded by §5.1.
- muse empty 200 under reasoning: a failed rep, not retried (except T's prod retry).
- Claude json_schema may reject a schema feature; then that cell runs with the prompt
  rule only and the fact is recorded.
- n = 4 sessions sees only large differences; held-out guards the selection, and every
  verdict is stated with its F.

## 10. Review log

rev 1 — draft from the read-only inventory of 2026-10-28 (rendered-hash method).

rev 2 — Fable review (NOT-READY), all applied:
1. BLOCKER pool size → §5.1 blind listing first (denominator), bounded adjudication of
   unmatched items only, pilot measures the pool before owner time; only actions /
   decisions gold-scored.
2. BLOCKER no noise floor → §5.3 F per cell, ≥ 2/3 reps to fail, cross-model median,
   fabricated vs misread; §6 keep rule by more than F.
3. HIGH T-0928 mis-described → §2.3 three candidates incl. 751481a3 (= f2191232^);
   P0 re-runs the T inventory.
4. HIGH env list → §2 import-time variables + payload test (incl. STATUS_SUGGESTIONS).
5. HIGH held-out budget → §7 reserve $9 before round 0; one shared ledger, global $85.
6. MED Claude parity → json_schema format, equal 8 000 headroom, reasoning tokens.
7. MED T vs prod → retry mirrored, pinned extraction artifact, T-cur scored against
   its input too, pipeline comparison labelled.
8. MED R-0928 dropped (could not ship).
9. MED timeouts 540 s, DeepSeek coreweave pin, provider recorded.
10. MED privacy → owner-folder debug JSON only, never `_summary`; `extraction_requests/`
    on the never-write list; read-only access.
11. LOW label.py as pattern only; unresolved → unsure; Haiku > 100 K pricing; budgets
    pinned; X-0901 on today's turn assembly stated; group extraction out of scope.
