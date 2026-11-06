# Forgiving Word reader + upload audit — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Word checklists in common layouts read by rule. A Word file no rule can
read falls back to the model reader. Every upload is audited by a second model,
and the owner is emailed about misreads.

**Architecture:** The rules extend `template_docx`. The fallback renders the Word
file to text and goes through `template_vision`'s call path. A new pure module,
`template_audit`, builds the audit request, parses the answer and applies the
code guards. The worker runs the audit inline after writing the result, stores
`template_digitise_audits/…`, and publishes problems to `AlertTopic`.

**Tech Stack:** Python 3.11, python-docx, boto3, `llm_utils.call_llm_parts`
(OpenRouter), and SAM `src/template.yaml`.

**Spec:** `docs/superpowers/specs/2026-11-06-digitise-reader-and-audit-design.md`.
Read the whole spec before starting any task.

## Global Constraints

- Python: `C:\Users\camil\fspy\Scripts\python.exe` (x64 3.11, same deps as CI).
  Run pytest one file or one directory at a time. A local Postgres backs the
  integration tests, and only one pytest run may use it at a time.
- No real model calls in tests. Fake `llm_utils.call_llm_parts` with monkeypatch.
- Claude models are never routed through OpenRouter. The audit models are
  `google/gemini-3.8-flash` (default) and `meta/muse-spark-1.3-contributor` (ALT).
- Never `git add -A` or `git add .`; add named files. Never `git stash`. Commit
  with LF endings. The repo is on Windows with CRLF warnings, which are fine.
- Module docstrings say why. Match the density of the surrounding comments.
- Existing messages and constants keep their names. New user-facing text is
  exactly as the spec words it.
- Verdict strings: `ok`, `problems`, `missed`, `not_a_checklist`, `unsure`,
  `skipped`.
- Route strings: `docx`, `docx_model`, `vision`.

## Review Focus

1. **A Penina- or BICL-style form must read exactly as before.** R1/R2 must not
   steal a header row that today's `_kind` treats as a header with a "Yes/No/NA"
   cell 2 (Penina). Task 1 has a regression test on both synthetic layouts and
   on the local real forms.
2. **"OK" or "Pass" inside item text must not make a row an answer column.**
   Task 1 tests an item reading "Pass-through sealed OK".
3. **Model claims of a missing row that are not in the source must not reach
   the email.** Task 3 tests this.
4. **An audit exception or timeout must leave the written result untouched,
   and the invocation must return `{"ok": True}`.** Task 4 tests this.
5. **A stack without an alert topic must not crash.** With `ALERT_TOPIC_ARN`
   unset, `problems` is stored and logged and nothing is published. Task 4
   tests this.

---

### Task 1: Rule reader — combined answer column, Pass/Fail words, legacy checkboxes

**Files:**
- Modify: `src/template_docx.py`
- Test: `tests/unit/test_template_docx.py` (add tests; the existing ones must
  still pass)

**Interfaces:**
- Produces: `template_docx.read(source)`, whose signature and return shape are
  unchanged.

- [ ] **Step 1: Write failing tests**, each building its form with python-docx,
  using the file's existing `_row` and `_save` helpers:
  - `test_combined_answer_column_reads_items`: header
    `No. | Item | Yes / No / N/A | Comment`, a merged section row "Wall Framing",
    and 3 item rows with blank answer cells. Expected:
    `[{"title": "Wall Framing", "items": [<3 texts>]}]`.
  - `test_combined_column_variants`: parametrise the header cell over
    `"Y/N/NA"`, `"Yes/No"`, `"Pass / Fail / N/A"`,
    `"Complies / Does not comply / N/A"` and `"YES | NO | N/A"`. Each reads the
    same 2 items.
  - `test_separate_pass_fail_columns`: header
    `Item | Pass | N/A | Fail | Comment` with 2 items reads both.
  - `test_ok_alone_is_not_an_answer_header`: header `Item | OK | Comment`
    (no no-word) reads `[]`.
  - `test_pass_in_item_text_is_not_an_answer`: a combined-column form whose item
    text is "Pass-through sealed OK" still reads it as an item, with no extra
    section.
  - `test_written_answer_row_is_not_an_item`: in a combined-column table, a row
    whose answer cell holds "Checked by J. Smith on site, all good" is not an
    item.
  - `test_legacy_form_checkbox_is_answer_cell`: inject
    `<w:fldSimple>`-free `w:ffData/w:checkBox` XML into an answer cell by hand,
    as the w14 test does, and read it as an item.
  - `test_bicl_and_penina_layouts_unchanged`: the existing synthetic BICL and
    Penina builders give exactly what they give today. Assert against the
    literal expected lists.
- [ ] **Step 2: Run** `C:\Users\camil\fspy\Scripts\python.exe -m pytest tests/unit/test_template_docx.py -q`.
  Expected: the new tests FAIL and the old ones pass.
- [ ] **Step 3: Implement.**
  - Extend `_YES` / `_NO` / `_NA` (spec R2). `ok` is a yes-word only when the
    same header row also has a no-word.
  - Add combined-cell detection (spec R1). A header cell whose words, split on
    `/ | ,` and whitespace, are all answer words, and include a yes-word and a
    no-word, is one answer column.
  - Generalise `_read_by_columns` so `cols` can be that single column.
  - Add legacy `w:ffData`/`w:checkBox` to `_is_answer_cell` (spec R3).
  - Update the module docstring's rule list.
- [ ] **Step 4: Run** the same command. Expected: all pass. Then run it locally
  with the real forms present (`TEMPLATE_DOCX_BICL` / `_PENINA` defaults).
  Expected: the real-form tests pass, not skip.
- [ ] **Step 5: Commit** `feat(digitise): read combined and Pass/Fail answer columns in Word forms`.

### Task 2: Model fallback for Word files no rule can read

**Files:**
- Modify: `src/template_docx.py` (add `render_text`), `src/template_vision.py`
  (add `read_text`), and `src/lambda_template_digitise.py` (the docx branch)
- Test: `tests/unit/test_template_docx.py`, `tests/unit/test_template_vision.py`,
  `tests/unit/test_template_digitise_worker.py`

**Interfaces:**
- Produces:
  - `template_docx.render_text(source) -> str`, the spec R4 rendering:
    paragraphs as lines, `[table N]` markers, cells joined `" | "`, merged cells
    once, and checkbox glyphs written as `[ ]` / `[x]`.
  - `template_vision.read_text(text, deadline=None) -> (raw_sections | None, message | None)`.
    It has the same contract as `read()`, and uses `TEXT_PROMPT` and
    `model()` with `request_settings(model())`.
  - `template_vision.TEXT_PROMPT`: `PROMPT` with its first line replaced by
    the spec's line.
  - `template_vision.MAX_TEXT_CHARS = 200_000`.
  - Worker result `route: "docx_model"`, `model: <name>`.

- [ ] **Step 1: Failing tests.**
  - `test_render_text_tables_and_paragraphs`: a doc with a heading paragraph,
    a merged row and a checkbox glyph renders exact expected lines.
  - `test_read_text_uses_text_prompt_and_reader_model`: monkeypatch
    `call_llm_parts`. Assert the parts are `[{"type": "text", "text": TEXT_PROMPT + "\n\n" + text}]`,
    or the prompt and the text as two text parts (the builder picks one and
    asserts it), and that `model=model()`, the reasoning and the provider come
    from `request_settings`.
  - Worker:
    - `test_docx_with_no_rule_rows_falls_back_to_model`: a table with no answer
      column, a faked model answer with 2 items, gives status `done`,
      route `docx_model`, and 2 items.
    - `test_docx_fallback_finds_nothing_is_unreadable_no_rows`: the faked model
      returns empty sections, which gives `unreadable` with reason `MSG_NO_ROWS`.
    - `test_docx_too_long_for_fallback_keeps_old_refusal`: render text over
      `MAX_TEXT_CHARS` gives `unreadable` with `MSG_WORD_NO_TABLE`, and the
      model is not called.
    - `test_docx_rule_read_never_calls_model`: a combined-column form makes no
      model call.
- [ ] **Step 2: Run** the three test files. Expected: the new tests FAIL.
- [ ] **Step 3: Implement.** The worker's docx branch:
  1. Rule read.
  2. If there are 0 items, `render_text`.
  3. Over the cap: old refusal.
  4. Else `read_text` with the worker's remaining deadline.
  5. `template_draft.clean`.
  6. 0 items: `unreadable` with `MSG_NO_ROWS`.
  7. Else `done`, with route `docx_model`.

  Map model errors exactly as the vision branch does. Correct the stale
  retry sentence in the worker docstring (spec A1).
- [ ] **Step 4: Run** the three files. Expected: all pass.
- [ ] **Step 5: Commit** `feat(digitise): read a Word form no rule recognises with the model reader`.

### Task 3: `template_audit` — request, parse, code guards, verdict (pure)

**Files:**
- Create: `src/template_audit.py`
- Test: `tests/unit/test_template_audit.py`

**Interfaces:**
- Consumes: `template_docx.render_text`, `template_vision._pdf_part` /
  `_image_part` (reuse them; do not copy), and `llm_utils.call_llm_parts` /
  `extract_json`.
- Produces:
  - `PROMPT: str`, per spec A2. The source is data; there are 4 verdicts; it
    lists the non-items; findings are capped at 20 per list.
  - `audit_model(read_model: str | None) -> (name, reasoning)`, per spec A3.
    It returns the default from env `TEMPLATE_DIGITISE_AUDIT_MODEL`, or ALT
    (`TEMPLATE_DIGITISE_AUDIT_ALT_MODEL`, reasoning `low` parsed through
    `llm_utils.parse_reasoning`) when the default equals `read_model`.
  - `build_parts(source: dict, result: dict) -> list`. `source` is
    `{"kind": "docx"|"pdf"|"images", "text": str|None, "pages": list|None}`.
  - `run(source, result, deadline=None) -> dict`, the audit record:
    `verdict, source_items, missing, extra, reworded, split_or_merged, summary,
    dropped_claims, model, elapsed_s, error`. It makes 1 call, retries once on
    an unparseable answer if 30 s are left, and returns `unsure` on any failure.
  - `check(answer: dict, result: dict, source_text: str | None) -> dict`, the
    A4 guards and the final verdict, with no I/O.
  - `normalise(text) -> str`, as the spec A4 defines it.

- [ ] **Step 1: Failing tests** (fake the LLM, no network):
  - `test_missing_claim_not_in_docx_source_is_dropped`: the model claims
    "Check roof flashing" is missing, and the source text lacks it. It ends up in
    `dropped_claims`; the verdict is `ok`.
  - `test_missing_claim_in_source_counts`: the claim appears in the source, so
    the verdict is `problems`, with 1 in `missing`.
  - `test_missing_claim_already_in_draft_is_dropped`.
  - `test_pdf_missing_needs_count_gap`: no source text, `source_items` equal to
    the draft count, so the claim is dropped. With `source_items` one more, it
    counts.
  - `test_extra_with_bad_n_dropped`, and `test_reworded_must_match_draft_text`.
  - `test_model_ok_with_checked_findings_is_problems`.
  - `test_empty_draft_missed_needs_checked_missing_for_docx`.
  - `test_empty_draft_not_a_checklist`.
  - `test_unparseable_twice_is_unsure`, and `test_model_error_is_unsure`.
  - `test_audit_model_switches_to_alt_when_reader_is_default`, and
    `test_audit_model_default_for_rule_read` (`read_model` None).
- [ ] **Step 2: Run** `…python.exe -m pytest tests/unit/test_template_audit.py -q`.
  Expected: FAIL (no module).
- [ ] **Step 3: Implement** to pass. `max_tokens` is 32000.
  `caller="template_audit"`. The fallback env is
  `TEMPLATE_DIGITISE_AUDIT_FALLBACK_MODELS`.
- [ ] **Step 4: Run.** Expected: all pass.
- [ ] **Step 5: Commit** `feat(digitise): audit module that checks a read against its source`.

### Task 4: Worker runs the audit, stores it, emails problems

**Files:**
- Modify: `src/lambda_template_digitise.py`
- Test: `tests/unit/test_template_digitise_worker.py`

**Interfaces:**
- Consumes: `template_audit.run`, `template_audit.audit_model`.
- Produces:
  - `audit_key(company_id, request_id) -> "template_digitise_audits/{company}/{id}.json"`.
  - The worker keeps the source it read (docx text, or pages) so it can hand it
    to the audit without reading S3 a second time.

- [ ] **Step 1: Failing tests** (moto or the file's existing S3 fake, whichever
  it uses; fake the LLM):
  - `test_audit_off_writes_nothing`: with `TEMPLATE_DIGITISE_AUDIT` unset, there
    is no audits key and no call.
  - `test_audit_ok_stored_not_published`.
  - `test_audit_problems_published_to_topic`: `ALERT_TOPIC_ARN` is set, and the
    fake SNS client records one publish. The subject starts with
    `[FieldSight ` and is at most 100 characters. The body names the file, the
    verdict and the findings.
  - `test_audit_problems_without_topic_logs_and_stores`.
  - `test_audit_exception_leaves_result_and_returns_ok`: `run` raises. The
    result object is byte-identical to the no-audit run, and the handler
    returns `{"ok": True}`.
  - `test_unread_source_writes_skipped`: a too-large source gives
    `verdict: "skipped"`, with no call and no publish.
  - `test_audit_skipped_when_under_60s_left`: a fake context with 70 s
    remaining, minus the 30 s write reserve, makes no call and logs
    `audit skipped: no time`.
  - `test_audit_log_line_shape`: caplog at INFO shows the spec A5 line.
- [ ] **Step 2: Run** the worker test file. Expected: the new tests FAIL.
- [ ] **Step 3: Implement.**
  - Call `_audit(...)` after `lake_put` of the result, inside its own
    try/except.
  - `lake_put` writes the audit record with owner hint company.
  - Publish with `boto3.client("sns")` only for `problems` / `missed`, and only
    when `ALERT_TOPIC_ARN` is set.
  - `STAGE` comes from the existing env, or add `STAGE` to the function's env
    in Task 5. Check which exists.
- [ ] **Step 4: Run** the worker tests, then the whole digitise group:
  `…python.exe -m pytest tests/unit/test_template_docx.py tests/unit/test_template_vision.py tests/unit/test_template_vision_settings.py tests/unit/test_template_draft.py tests/unit/test_template_audit.py tests/unit/test_template_digitise_worker.py tests/unit/test_template_digitise_routes.py -q`.
  Expected: all pass.
- [ ] **Step 5: Commit** `feat(digitise): audit every upload and email misreads to the owner`.

### Task 5: Infra — parameters, env, IAM, workflows, retention, listing

**Files:**
- Modify:
  - `src/template.yaml`: parameters `TemplateDigitiseAudit`,
    `TemplateDigitiseAuditModel` and `TemplateDigitiseAuditAltModel`; the
    function env (`TEMPLATE_DIGITISE_AUDIT*`, `ALERT_TOPIC_ARN` via `!If`, and
    `STAGE` if needed); a conditional `SNSPublishMessagePolicy` copied from
    the ExtractionBacklog function.
  - `.github/workflows/deploy.yml`: TEST vars, with `||` defaults
    `true` / `google/gemini-3.8-flash` / `meta/muse-spark-1.3-contributor`.
  - `.github/workflows/deploy-prod.yml`: PROD vars, with `||` defaults
    `false` / the same models.
  - `scripts/wire-bucket-lifecycle.sh`: a 90-day rule for
    `template_digitise_audits/`, and the header comment line 10.
- Create: `scripts/template-audits.sh [--env test|prod] [--days N]`
  (aws s3api list + get, one line per audit, newest first; profile
  `fieldsight-deployer`, and for prod the prod bucket naming the existing
  scripts use).
- Test: `tests/unit/test_template_digitise_wiring.py`. Extend it so it pins
  each new Parameter -> env, the `!If` on the topic and policy, and the
  workflow vars with their defaults. Read the existing tests first and follow
  their style.

- [ ] **Step 1: Failing wiring tests.**
- [ ] **Step 2: Run** the wiring test. Expected: FAIL.
- [ ] **Step 3: Implement.** Then run `cfn-lint` at the version CI pins
  (see `.github/workflows` for the pin) on `src/template.yaml`.
- [ ] **Step 4: Run** the wiring tests and `bash -n scripts/template-audits.sh`.
  Expected: pass.
- [ ] **Step 5: Commit** `chore(digitise): wire the upload audit (params, IAM, workflows, 90-day retention)`.

### Task 6: Audit bench (M3), run on TEST after deploy

**Files:**
- Create: `tools/template-digitise-bench/audit_bench.py`. Read the existing
  `run.py` there first and reuse its loading and LLM helpers.

**Interfaces:**
- Consumes: `template_audit.run`, `template_docx.read/render_text`,
  `template_vision.read`, `template_draft.clean`.

- [ ] **Step 1: Write the bench.**
  - Cases (spec M3): known-good BICL, Penina, the clean council DOCX, the
    council original PDF and the new Wall Framing DOCX. Known-bad: the old
    combined-column Wall Framing DOCX, faked as an empty result; a good draft
    with 3 rows deleted; a good draft with "Signed:" and
    "Reinspection required?" added.
  - Paths come from argv or env, with the defaults under
    `C:/Users/camil/Dropbox/fs-test/multi-unit/` and `C:/Users/camil/Dropbox/ADE/`.
  - Each case runs N=3 times. The bench prints a table of verdicts, counts and
    dropped claims, and the pass bars: 0 false alarms out of 15, and each bad
    case caught at least 2 out of 3.
  - It needs `--yes` to make real calls. Without it, it prints the call count
    and exits.
- [ ] **Step 2: Dry run** (no `--yes`). Expected: the call count is printed,
  and no network call is made.
- [ ] **Step 3: Commit** `test(digitise): audit noise-floor bench`.
- [ ] **Step 4 (controller, after the TEST deploy):** report the call count,
  run it with `--yes` using the TEST OpenRouter key from the TEST Lambda's
  configured secret path (not the env dump), and paste the table into the PR
  description.
