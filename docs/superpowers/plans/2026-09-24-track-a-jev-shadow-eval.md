# Track A — Jev shadow evaluation on decisions we already have labels for

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Measure whether TypeSafe's Jev (System One model) beats the gates this repo runs today, on the three or four decisions that already carry human verdicts in Aurora. Produce a findings doc whose decision rule is written down *before* the numbers exist.

**Architecture:** An offline harness, `scripts/jev_shadow_eval.py`, that (1) exports labelled pairs through the RDS Data API, (2) builds a structured, name-masked `state` per pair, (3) runs three arms — today's gate, Jev with one broad question, Jev with decomposed atomic questions — twice each, (4) scores accuracy, calibration and coverage-at-precision. One small production-shaped client, `src/systemone_client.py`, so Track C can reuse it. **Nothing in this track writes to Aurora or S3, and nothing runs in a Lambda.**

**Tech Stack:** Python 3.11, urllib (no SDK), RDS Data API via `aws rds-data`, pytest.

**Spec:** `docs/superpowers/specs/2026-09-24-event-graph-and-jev-assessment.md` §4, §6.

**Owner decisions already taken (2026-09-24):** data may leave the country; only structured event JSON is sent, never transcript text; person names are masked; TEST database first, prod labels only if TEST has too few.

## Global Constraints

- **Read-only, everywhere.** `extraction_ab.py`'s two rules apply verbatim: import the real code paths (`lambda_programme_matcher.build_prompt`, `parse_verdict`, `thread_match.score_pair` / `find_candidates`) rather than re-deriving them, and never publish to a real key.
- **Same config twice before reading any difference** (CLAUDE.md "Method rules that generalise"). `--runs 2` is the minimum; a single run measures sampling.
- **A control arm is what turns "no effect" into a finding.** Every question set has a control where the thing being judged is removed from the state; Jev must move toward "no" or the set is unreachable, not decided.
- **No transcript text in any state.** The state builder works from an allowlist of structured fields (Task 3). A test pins the allowlist.
- **Names are masked before leaving the process.** `name_aliases` reverse map plus `corroboration_gate._NAME_WORD` → `PERSON_n`. Company and product names stay (they are the signal for trade/entity questions).
- **The exported label files contain customer text and are never committed.** `scripts/fixtures/jev_eval/` is gitignored; only `counts.json` is committed.
- **Secrets from env only:** `DECISIONS_API_KEY` (= `OPENROUTER_API_KEY` on the chosen route). Never in a fixture, never in a log line. `llm_usage` logs tokens, never text.
- **Jev limits (from public docs, re-verify against docs.typesafe.ai before Task 2):** `POST https://api.typesafe.ai/v1/systemone`, `model: jev-latest`, `state` + `questions` map of `choice` (≤255 options) / `score` (2–10 levels) / `noul`; 64k context, `state` + longest question ≤ 32k tokens; 1200 rpm. Output is probabilities, never text.
- **The decision rule is pre-registered in Task 8 and copied into the findings doc before Task 6 runs.**

---

### Task 1: Export the labelled sets (read-only, RDS Data API)

**Files:**
- Create: `scripts/jev_eval/export_labels.py`
- Create: `scripts/fixtures/jev_eval/.gitignore` (contents: `*` newline `!.gitignore` newline `!counts.json`)
- Test: `tests/unit/test_jev_eval_export_shapes.py`

**Interfaces:**
- Produces one JSONL per set under `scripts/fixtures/jev_eval/`: `programme_match.jsonl`, `threads.jsonl`, `work_class.jsonl`, plus `counts.json`.
- Each row: `{"set", "id", "label", "features": {...}, "site_id", "company_id", "decided_at"}`. `label` ∈ `{yes, no}` after mapping.

- [ ] **Step 1: Queries, one per set.** Use the `begin-transaction` / `execute-statement` / `rollback-transaction` pattern from `scripts/verify_programme_schema.py:40-69` even though these are SELECTs, so the script cannot write by accident. Database `fieldsight_test` first; `--database fieldsight` only behind an explicit flag.

  - **programme_match** — `programme_progress_suggestions` where `state IN ('confirmed','rejected')`: `topic_title, topic_summary, task_name, task_status_before, task_progress_before, suggested_status, suggested_progress, confidence, match_evidence, report_date, state, decided_at`. Label: confirmed → yes, rejected → no. The row already holds the topic text the matcher saw (`programme_suggestions.py:_COLS`), so no join to `topics` is needed and topic-id churn is irrelevant.
  - **threads** — `topic_thread_suggestions` where `status IN ('confirmed','rejected')` joined to `topics` twice (the topic, and `thread_id`'s seed topic or `parent_topic_id`): both titles and summaries, `score, gap_days, status`. Note in `counts.json` how many rows lost a side to supersession (topic row gone); those are excluded.
  - **work_class** — `classification_feedback` joined to `topics` on `topic_id`: `title, summary, category, classifier_verdict, classifier_confidence, human_verdict`. Label: `confirm_non_work` → non_work is right; `reject_is_work` → non_work is wrong; `missed_personal` → work verdict was wrong. Rows whose topic no longer exists are counted and excluded.

- [ ] **Step 2: Count gate.** Write `counts.json` with per-set totals and positives. A set under 30 labels is tagged `"descriptive_only": true` and the findings doc may not draw a conclusion from it. If TEST has fewer than 30 for programme_match, re-run against `fieldsight` (prod) with the flag and say so in the doc.

- [ ] **Step 3: Test the shapes**, not the SQL (the doubles cannot run it): every row has the keys above, `label` is only ever `yes`/`no`, no key named `transcript`, `turns`, `quote`, `evidence` or `text_window` appears in `features`.

---

### Task 2: A production-shaped Jev client

**Files:**
- Create: `src/systemone_client.py`
- Test: `tests/unit/test_systemone_client.py`

**Interfaces:**
- `ask(state: str | dict, questions: dict, *, model=None, timeout=None, caller=None) -> dict` returning `{"answers": {name: {"noul": p} | {"choice": x, "probabilities": {...}, "confidence": c} | {"score": s, "probabilities": {...}}}, "usage": {...}, "latency_ms": int}`.
- Env: `DECISIONS_API_KEY` (required; on OpenRouter this is the existing `OPENROUTER_API_KEY` — the same secret `corroboration_client` already uses), `DECISIONS_URL` (default `https://openrouter.ai/api/alpha/decisions`; TypeSafe direct is `https://api.typesafe.ai/v1/systemone`), `DECISIONS_MODEL` (default `~typesafe/jev-latest` on OpenRouter, `jev-latest` direct), `DECISIONS_HTTP_TIMEOUT` (default 10).
- **Owner decision 2026-09-24: use the OpenRouter route.** Public notes on it (re-verify against `openrouter.ai/docs/guides/community/jev` before writing code): it is a dedicated **Decisions** endpoint, *not* chat completions — an OpenAI-compatible SDK cannot call it; the body is the same `model` / `state` / `questions` shape as TypeSafe's own API; the listing shows a **32k** context (TypeSafe direct says 64k), so the client's pre-send size check is sized for 32k total; the endpoint is marked **alpha**, so the client keeps `DECISIONS_URL` switchable and the findings doc records which route produced every row. Error codes seen: 400, 401, 402 (credits), 404, 413 (payload too large), 429, 5xx.

- [ ] **Step 1: Write the client** in the style of `llm_utils._call_qwen`: urllib, one retry on 429/5xx with the `Retry-After` header honoured, a 200 with an empty `answers` map treated as failure (`llm_utils.py:555-564` precedent). Refuse before sending when the serialised state plus questions exceeds a conservative 20k-token estimate (4 chars/token) — the OpenRouter listing caps the whole request at 32k, and a 413 or a silent truncation would be exactly the BUG-15 shape.
- [ ] **Step 2: Log usage** through `llm_usage` with `provider=openrouter-decisions` (or `typesafe` when `DECISIONS_URL` is the direct host), `model`, `caller`, `latency_ms`, `prompt_tokens` from the response usage block if present. Never the state.
- [ ] **Step 3: Tests with a fake transport** (monkeypatch `urllib.request.urlopen`): request body carries `model`, `state`, `questions` exactly as given; a `noul` answer is a float in [0,1]; a `choice` answer's probabilities sum to ~1 and the chosen option is in the options list; 429 → one retry then raise; oversize state raises before any request; missing API key raises with a message naming the env var; a 413 raises with the estimated token count in the message.

---

### Task 3: State builder with the field allowlist and name masking

**Files:**
- Create: `scripts/jev_eval/state.py`
- Test: `tests/unit/test_jev_eval_state.py`

**Interfaces:**
- `build_state(set_name, features, aliases) -> dict` returning a small JSON object; `mask_names(text, aliases) -> (text, mapping)`.

- [ ] **Step 1: Allowlist per set**, as a module constant; anything not listed is dropped, and a key that looks like transcript (`quote`, `turns`, `window`, `transcript`) raises.
  - programme_match: `observation.title, observation.summary, observation.date, observation.action_items[].text?` (only if present in features), `task.name, task.status, task.progress_pct, task.start, task.end`.
  - threads: `earlier.title, earlier.summary, earlier.date, later.title, later.summary, later.date, gap_days`.
  - work_class: `title, summary, category`.
- [ ] **Step 2: Masking.** Build a reverse map from `name_aliases` (`kind='person'`) exported with Task 1, then apply `_NAME_WORD` from `corroboration_gate` for capitalised two-token names not in the company/product alias set. Replace with `PERSON_1`, `PERSON_2` consistently within one state. The mapping is kept in memory for the run and never written.
- [ ] **Step 3: Tests.** A feature dict containing `transcript` raises; `Ben Lin said the slab is late` → `PERSON_1 said the slab is late`; `Naylor Love` (kind=company alias) survives; the same name twice in one state gets the same placeholder; output JSON serialises under 4k characters for a typical row.

---

### Task 4: Question sets — one broad, one decomposed, one control — per decision

**Files:**
- Create: `scripts/jev_eval/questions.py`
- Test: `tests/unit/test_jev_eval_questions.py`

**Interfaces:**
- `QUESTION_SETS[set_name] = {"broad": {...}, "decomposed": {...}, "composite": fn(answers)->float, "control": fn(state)->state}`.

- [ ] **Step 1: Write the sets.** The decomposed questions are *observable* sub-facts, the composite is code-side (the public 62.6% → 95% result came from exactly this move, plus labels and a fitted weighting — the weighting is Task 7).

  **programme_match**
  - broad: `noul match: "This site observation is about the scheduled task."`
  - decomposed:
    - `same_work_item` noul: "The observation and the task describe the same physical work item (e.g. the same doors, the same slab, the same duct run)."
    - `task_named` noul: "The observation mentions the task by name or an unambiguous synonym."
    - `same_trade` noul: "The observation and the task belong to the same trade."
    - `status_claimed` choice `[none, in_progress, completed, blocked, delayed]`: "What state does the observation say this work is in?"
    - `progress_stated` noul: "The observation states a percentage complete or an explicit completion."
  - composite v0 (before fitting): `0.5*same_work_item + 0.3*task_named + 0.2*same_trade`.
  - control: replace `task.name` with a task name drawn from a *different* site's programme (same trade word if possible).

  **threads**
  - broad: `noul same_subject: "The later topic is a restatement or follow-up of the earlier topic."`
  - decomposed: `same_subject_noun` noul ("share the same distinctive subject noun"), `same_location` noul, `continuation` noul ("the later text refers to a commitment, deadline or state the earlier one set up"), `generic_only` noul ("the overlap is only generic process words like installation, documentation, floor").
  - composite v0: `0.5*same_subject_noun + 0.3*continuation + 0.2*same_location - 0.4*generic_only`, clipped to [0,1].
  - control: swap `earlier` for a random topic from another site at a similar gap.

  **work_class**
  - broad: `choice work_class [work, non_work]`.
  - decomposed: `about_the_site` noul ("the conversation is about construction work, a site, a programme or a subcontractor"), `personal` noul ("the conversation is about the recorder's private life, family, health or unrelated business"), `recording_test` noul ("the conversation is about testing the recording device or app itself").
  - composite v0: `P(non_work) = max(personal, recording_test) * (1 - about_the_site)`.
  - control: title and summary replaced by a neutral sentence ("General discussion.").

- [ ] **Step 2: Tests.** Every set has all four keys; every `choice` has ≥2 options; every question text is under 300 characters; `control(state)` differs from `state` in exactly the intended key; composites return floats in [0,1] on boundary inputs.

---

### Task 5: The baseline arm is today's gate, imported, not re-derived

**Files:**
- Create: `scripts/jev_eval/baseline.py`

- [ ] **Step 1: programme_match baseline.** Import `lambda_programme_matcher` and call `build_prompt(topic, [candidate])` then `llm_utils.call_llm(..., max_tokens=512, force_json=True, caller="jev_eval_baseline_programme")` and `parse_verdict(raw, {task_id}, CONF_MIN)`; the baseline score is the parsed `confidence` (0.0 when the verdict is None). Read `LLM_PROVIDER` and the model from the *deployed* extract-session function the way `scripts/eval_task_admission.py:16-21` does, so the arm is what TEST runs and not the workflow default (the 2026-08-08 spec's lesson).
- [ ] **Step 2: threads baseline** is the stored `score` from `topic_thread_suggestions` (it is deterministic; no re-run needed). Record `MIN_SCORE` as its threshold.
- [ ] **Step 3: work_class baseline** is the stored `classifier_confidence` and `classifier_verdict`.
- [ ] **Step 4:** The baseline arm also runs twice for programme_match (it is sampled); threads and work_class baselines are single-valued by construction and say so in the output.

---

### Task 6: Runner

**Files:**
- Create: `scripts/jev_shadow_eval.py`
- Output: `scripts/fixtures/jev_eval/results/{set}.{arm}.run{n}.jsonl` (gitignored)

- [ ] **Step 1: CLI**: `--set programme_match|threads|work_class|all`, `--arms baseline,broad,decomposed,control`, `--runs 2`, `--limit N`, `--database fieldsight_test`. Requires `DECISIONS_API_KEY` for the Jev arms; refuses to start if the set's `counts.json` entry is missing.
- [ ] **Step 2: Per row, per arm, per run:** build state (Task 3) → questions (Task 4) → `systemone_client.ask` → write `{id, label, arm, run, answers, score, latency_ms, tokens}`. Concurrency 4, sleep on 429. Broad, decomposed and control arms share one `ask` call where possible? **No** — one call per arm, because the control changes the state; keep the arms independent.
- [ ] **Step 3: Cost and latency** totals printed at the end per arm, and written to `results/summary.json`.
- [ ] **Step 4: Idempotent**: re-running with the same args skips rows already present in the run file, so a 429 storm can be resumed.

---

### Task 7: Scoring

**Files:**
- Create: `scripts/jev_eval/score.py`
- Test: `tests/unit/test_jev_eval_score.py`

**Interfaces:**
- `score_set(rows_by_arm_run, threshold_policy) -> dict` per arm: `accuracy, precision, recall, brier, ece10, run_agreement, coverage_at_p95, threshold_at_p95, n`.

- [ ] **Step 1: Metrics.** Brier and 10-bin ECE on `score` vs label; run agreement = fraction of rows where run1 and run2 land on the same side of the arm's threshold; `coverage_at_p95` = share of rows the arm may auto-accept while precision on the accepted set stays ≥ 0.95.
- [ ] **Step 2: Split-half threshold fitting.** Fit the threshold (and, for `decomposed`, the composite weights by logistic regression on the sub-answers — plain numpy, no sklearn) on one half by `id` hash, score on the other; report both halves. Never fit and score on the same rows.
- [ ] **Step 3: Control check.** For each Jev arm, the mean score on the control arm must be lower than on the real arm by at least 0.2; if not, the set is flagged `unreachable` and excluded from the verdict.
- [ ] **Step 4: Tests** on synthetic rows: perfect calibration gives ECE ≈ 0; a constant 0.5 predictor gives Brier 0.25; coverage_at_p95 is 0 when no threshold reaches 0.95 precision; split halves are disjoint and together cover all ids.

---

### Task 8: Findings doc with the pre-registered decision rule

**Files:**
- Create: `docs/superpowers/specs/2026-XX-XX-jev-shadow-eval-findings.md` (date at write time)

- [ ] **Step 1: Write the decision rule first, before Task 6 runs, and commit it:**

  > Jev (decomposed) **replaces** today's gate on a set only if, on the held-out half: coverage_at_p95 ≥ the baseline's coverage_at_p95, AND run_agreement ≥ 0.95, AND ECE10 ≤ 0.10, AND the control check passes. Jev **augments** the gate (runs alongside as an extra signal in `decision_records`, Track C) if it meets two of the three numeric conditions. Otherwise it is **not adopted** for that set and the finding is recorded with the numbers.

- [ ] **Step 2: Table per set**, arms as rows: n, accuracy, precision, recall, Brier, ECE10, run agreement, coverage@p95, median latency, cost per 1000 rows. Every row carries provider, model, temperature, and the exact question text hash — the 2026-08-12 spec's first harness run recorded none of that and the data could not prove what produced it.
- [ ] **Step 3: Read the disagreements by hand.** For each set, the 20 rows where Jev-decomposed and the baseline disagree most: which one the human label agrees with, and whether the label itself looks wrong (suggestion labels were given by one reviewer under time pressure). A number is not a decision input until a person has read a sample.
- [ ] **Step 4: What this did not measure**, stated: task admission (no exported set — its ground truth is one session's fixture at `tests/fixtures/task_admission_ground_truth.json`, run it descriptively only if time allows), claim_type (no labels exist yet), latency inside a Lambda (this ran from a laptop/CI runner).

---

### Task 9: Hygiene

- [ ] `.gitignore` entry for `scripts/fixtures/jev_eval/*` except `counts.json` (Task 1) — verify with `git status` after a run that nothing under `results/` is staged.
- [ ] `tests/unit/test_jev_eval_never_writes.py`: import `scripts/jev_shadow_eval.py` under a monkeypatched boto3 that raises on any `put_object` / `execute-statement` without a preceding `begin-transaction`, and assert the module never calls `db.connection`.
- [ ] Add `typesafe` to the provider list `tests/unit` already pins for `llm_usage` log-line shape, if such a test exists (grep `LLM_USAGE provider=`).

---

## Verification (owner reads)

1. `counts.json` committed with per-set n and the descriptive-only flags.
2. Findings doc committed **with the decision rule dated before the results section**.
3. Results table plus the hand-read disagreement sample.
4. A one-paragraph recommendation per set: replace / augment / not adopted — and, if any set says replace, the shape of the Track C provider plug (it slots behind `decision_records`, Track B Task 7, and is called from a non-VPC function through the `match_requests/`-style S3 request file, BUG-36).

## Out of scope

Calling Jev from any Lambda; fine-tuning (Jev has none); Cloudflare or Pydantic routes (one route per run, so the numbers describe one thing); any change to matcher thresholds or prompts as a result of this track — that is Track C's call after the findings are read.
