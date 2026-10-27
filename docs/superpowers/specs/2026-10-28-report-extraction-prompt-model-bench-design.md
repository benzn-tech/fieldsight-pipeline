# Daily report + session extraction — prompt × model bench

Date: 2026-10-28 · Line: ASR+LLM优化v3 (bench owner, per owner 10-27) · Status: rev 1.
Production prompt changes that come out of this go through the report / extraction
line (authors of #864, #1022-#1069, #914-#997); this spec changes nothing in `src/`.

## 1. Problem

No model has ever had its own tuned prompt for the daily report or the session
extraction. Every vendor switch (Claude Sonnet 4.6 in July → qwen3.7-max → muse
09-07 → muse with luna / mimo fallback 10-08) reused the text it inherited; the one
model-driven edit was muse's `Bullet 1:` placeholder echo (31093b2d, minutes schema).
What is measured is model-vs-model on one prompt, and thin:

| model | task | evidence |
|---|---|---|
| muse | daily report replay (n=3, one thin day) | no invention; 19-41 s; reasoning cannot be turned off; reasoning eats `max_tokens` (empty 200) |
| DeepSeek V4.1 Flash | same replay | 3-4× faster; invents `key_decisions` 2/3, an "EOD" action 1/3 (schema example at lambda_report_generator.py:201 is the likely source) |
| mimo | fallback drill | thinking on: 0/4 parsed; off: 2/2 |
| gemini, Sonnet 5.5, Haiku 5.5 | — | nothing on these tasks |

Run-to-run variance on extraction is larger than prompt or model differences (27 runs,
action counts 1-9 at temperature 0; unions do not converge). So: n ≥ 3 per cell and
per-item ground truth, or nothing can be concluded.

**Owner decisions (10-27):** models = muse, DeepSeek, gemini, mimo, Sonnet 5.5, Haiku
5.5; candidates = prompt versions changed in Sep–Oct plus the current one; a model that
underperforms gets its own tuned prompt, recorded per model.

## 2. Tasks and candidates

Inventory: every commit since 2026-08-25 compared by definition and by **rendered
prompt hash** on a fixed input (snapshot of `src/` per sha imported cold with
`python -I`; single-file imports against today's modules are not faithful).

### 2.1 Extraction (final pass — the result prod keeps)

`lambda_extract_session.build_extraction_prompt` (+ `_instructions_block`,
`EXTRACTION_SCHEMA`). Seven text versions in the window; four candidates:

| id | sha | what it is |
|---|---|---|
| `X-0901` | 763b3082 | Sep-1 text (byte-identical to e62cbd30 unless names are passed) |
| `X-0917` | 83abd0d4 #864 | action wording + explicit admission test; do not guess responsible / deadline |
| `X-1005` | 8703e69a #1052 | everything before #1069 (markers, inspections) |
| `X-cur` | 82036cf2 #1069 = develop | `responsible` = person / crew / trade / "Speaker" / null |

`EMIT_EVIDENCE` **false** (prod, deploy-prod.yml:323) and `DECLARE_CONTINUITY` false;
both read at import, set before import. Marker / inspection-only steps are not separate
candidates (no ground truth scores them in this rev).

### 2.2 Daily report (nightly `lambda_report_generator`)

The rendered inline prompt is **identical** across Sep–Oct; prod and TEST serve the S3
`config/prompt_templates.json` (git copy v3.0, March). What changed is the input shape
(300 K transcript limit #778, weather block removed #950, name block removed #1078).
Candidates:

| id | what |
|---|---|
| `R-cur` | develop + the S3 template **as prod serves it** (read from a recent prod `daily_report_debug.json` `prompt` field; if it differs from git v3.0, the debug copy wins and the diff is recorded) |
| `R-0928` | `R-cur` with the pre-#950 weather block (days with coordinates only) |

The S3-vs-git question is answered from the debug JSON before any run.

### 2.3 Template / session report (`report_template.render_prompt`)

All Sep–Oct report prompt churn is here (#859 → #997). Customer-facing. Two candidates:

| id | sha | what |
|---|---|---|
| `T-0928` | e4e2ea19 #943 | last version written from the transcript |
| `T-cur` | develop (f2191232 #997 onward) | written from the extraction record |

`T-cur` consumes an extraction, so its input is the **muse `X-cur` extraction of the
same session** (fixed across cells), so a T difference is the report prompt's.

## 3. Models and parameters (a cell = prompt × model × parameters)

| key | model | parameters |
|---|---|---|
| muse | meta/muse-spark-1.3-contributor | prod shape: `reasoning.effort` high, answer + 8 000 headroom, `json_object` (via `llm_utils.build_qwen_payload(..., model=)`, the exact prod body) |
| deepseek | deepseek/deepseek-v4.1-flash | same prod shape (as the 10-16 replay); second cell `reasoning {enabled:false}` @ coreweave only if the first fails §5 gates |
| gemini | google/gemini-3.8-flash | prod shape + `corroboration_client.provider_preference` order |
| mimo | xiaomi/mimo-v2.6-pro | prod branch: reasoning off, no headroom, `json_object` (llm_utils.py:483-488) |
| sonnet | claude-sonnet-5-5 | Anthropic SDK direct from the bench: `thinking {type: adaptive}`, `output_config.effort` high, `max_tokens` = answer budget + 16 000, no temperature, streaming, `stop_reason` checked |
| haiku | claude-haiku-5-5 | same as sonnet, effort high |

Not through `llm_utils._call_anthropic`: it drops `force_json`, sends no thinking /
effort, has no headroom, ignores `stop_reason`, and TEST's `temperature: 0` is a 400 on
these models. JSON for Claude comes from the prompt's own "return only JSON" rule,
parsed with `llm_utils.extract_json` — the same parser every model gets; parse rate is a
metric. The template report (§2.3) is free text for every model.

## 4. Inputs

- **Data:** owner-test folders only (Ben_Lin_test2 / test3, Ben_UCPK2 and the owner's
  own recordings), from the prod and TEST buckets, read-only. No customer folder. All six
  vendors already process this data class in prod (Anthropic in July, OpenRouter
  muse / mimo / gemini / DeepSeek since Sep–Oct), so no new processor is added.
- **Set:** 4 extraction sessions (one dense > 40 min, one short, one mixed Chinese /
  English, one with inspections / location talk) + their days for R (2-3 days, at least
  one dense) + the same 4 sessions for T.
- **Collection** reuses `scripts/extraction_ab.py`'s real turn assembly
  (`--turns-file` output) and writes everything under the bench `--out` (outside the
  repo). Never copy into `transcripts/` (fires the live extraction), never write to
  `extractions/` or `reports/`.

## 5. Ground truth and scoring

### 5.1 Gold (human, per item)

Extraction and report outputs are open lists, so gold is built **union-then-adjudicate**
(as `scripts/continuity_eval/label.py`): all round-0 outputs for a session are pooled;
near-duplicates merged (CJK-aware fuzzy match as `extraction_ab --analyse`); the owner
marks each pooled item `true` / `false` / `unsure`, blind to model and cell, on a local
labelling page. Pooled kinds: action items (with responsible / due), decisions, topics
(title only), findings. A true item that no model produced cannot be added by pooling —
the owner can add it on the page ("missed").

The session behind `tests/fixtures/task_admission_ground_truth.json` is included and its
existing 5 true / 3 false labels are pre-filled.

### 5.2 Per-output metrics

- **Mechanical (no human):** parsed (`extract_json` ok) · all schema keys · `finish`
  normal (not length / max_tokens / refusal) · placeholder scan (the 14 patterns of the
  09-08 replay: `Bullet N`, `[insert`, `{{`, `<field>`, `YYYY-MM-DD`, …) · latency ·
  input / output / reasoning tokens · $.
- **Against gold:** per kind, precision = true / produced, recall = true / (gold true);
  **invented** = items marked false that assert a decision or a commitment (the DeepSeek
  failure); `responsible` correct (person / crew / trade / Speaker / null vs gold);
  `due` kept when gold has one.
- **Template report (T):** coverage of gold-true actions / decisions, invented count,
  section completeness (every template section present). Free text is matched to gold by
  the owner on the same page (item present yes / no) — n is small (4 sessions × cells).

### 5.3 Labels over reps

Per (cell, session) the 3 reps give 3 values per metric; report the median and the
spread. A cell **fails** a session if any rep is unparsed, truncated or has a
placeholder, or the median `invented` > 0.

## 6. Rounds and the per-model tuning rule

- **Round 0:** every candidate × every model × 3 reps (§7 counts).
- **Underperforming** (owner's words, made testable): a model whose best round-0 cell
  (a) fails any session per §5.3, or (b) has median action recall more than 0.15 below
  the best model's best cell on two or more sessions, or (c) median invented > 0 on any
  session.
- **Rounds 1-2:** only for an underperforming model, up to 2 edits to its best cell
  (`<model>_<task>_v*`), written against its observed failures (e.g. remove the quotable
  example that DeepSeek echoes; for muse shorten instructions that eat reasoning). Each
  edit runs on the same sessions × 3 reps. Keep rule (as the web bench): no session's
  median worse on the failed metric, at least one session better where the incumbent's 3
  reps agreed; parse / placeholder gates never relaxed.
- **New gold during rounds 1-2:** new pooled items from an edit's outputs go to the
  owner's page before the edit is judged.
- **Record:** `tools/report-bench/frozen.v1.json` per (task, model): prompt text or sha,
  parameters, round, metrics, `evidential: false` for any number used to select it.
  This is the seed of a per-model prompt registry (prompt + parameters, keyed by model;
  a model switch reads its own entry and never overwrites another's) — wiring it into
  `src/` is a later spec for the report / extraction line.
- **Selection bias:** the 4 sessions are used both to select and to report; the report
  says so. A held-out confirmation (2 new sessions, frozen cells only, 3 reps) runs before
  any production recommendation.

## 7. Cost and calls

Approximate input sizes: extraction 15-60 K tokens; report 20-100 K; template 10-30 K.

| | cells | sessions / days | reps | runs |
|---|---|---|---|---|
| extraction round 0 | 4 × 6 = 24 | 4 | 3 | 288 |
| daily report round 0 | 2 × 6 = 12 (R-0928 only on days with coordinates) | 3 | 3 | ≤ 108 |
| template report round 0 | 2 × 6 = 12 | 4 | 3 | 144 |
| rounds 1-2 (≤ 2 edits × ≤ 6 models × 3 tasks) | ≤ 36 | 3-4 | 3 | ≤ 396 |
| held-out confirmation | ≤ 18 frozen | 2 | 3 | ≤ 108 |

Anthropic share = 2/6 of runs. Per Claude run (Sonnet, effort high, ~50 K in, ≤ 16 K
thinking + ≤ 16 K answer): upper ≈ 50 K × $2 + 32 K × $10 = **$0.42**, expected ≈ $0.15;
Haiku ≈ 1/20 of that.
- Round 0: 540 runs → Sonnet 90 runs ≤ $38 upper, ≈ $14 expected; Haiku ≤ $2.
- **Ledger: $40 Anthropic cap for this bench**, separate from the web bench's $45 (both
  inside the $100 / month credit). Round 0 is ordered extraction → template → report, so
  a cap hit loses the least-changed task. Rounds 1-2 and held-out run only with room.
- OpenRouter: muse / DeepSeek / gemini / mimo ≈ 360 round-0 runs at ≤ $0.05 → ≤ $18.
- Every live step: `--dry-run` prints calls and $ upper; `--confirm-calls N` required;
  owner runs (keys).

## 8. Phases

- **P0 (Claude, offline):** snapshot builder (per-sha `src/` + cold import) with tests
  that the rendered hashes match §2; input collector (owner-test folders only, read-only;
  refuses any other folder); S3 template check from a debug JSON.
- **P1 (owner-run):** collect inputs → round 0 → pool → **owner labels** → score →
  per-model verdicts.
- **P2:** rounds 1-2 for underperforming models → held-out → `frozen.v1.json` → report to
  the owner and the report / extraction line.
- **Out of scope:** changing prod prompts, `LlmProvider`, or the fallback chain; the live
  (non-thinking) extraction pass; marker / inspection scoring; luna.

## 9. Risks

- Owner labelling time: pooled items ≈ 4 sessions × ~30 = ~120 marks + T matches; the
  page is built to take ~30 min.
- muse's reasoning can eat the budget (empty 200): counted as a failed rep, not retried.
- The S3 template may differ from git: §2.2 reads the prod copy first.
- n = 4 sessions sees only large differences; held-out guards the selection.
- Claude JSON without a structured-output mode may fail to parse; that is a measured
  outcome, and a round-1 edit may add `output_config.format` (json_schema) as a
  parameter change.

## 10. Review log

rev 1 — draft, from the read-only inventory of 2026-10-28 (rendered-hash method).
