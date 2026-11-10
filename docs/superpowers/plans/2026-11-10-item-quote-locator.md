# Item quote locator (Track C slice 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** On the final extraction tier, the model marks each child item with the `[HH:MM:SS]` stamp of the
line where it was first raised. Code resolves that stamp to exactly one turn the model was shown, or refuses,
and stores the result through the slice 1 evidence-chain pipeline. A pre-registered A/B measurement decides
whether any flag goes on.

**Architecture:** A new flag `EMIT_ITEM_LOCATOR` threads `locator: bool` into the shared instruction block
(final single-session tier only; off is byte-identical). A new pure module `item_locator.py` resolves and
strips `said_at`. `item_support.attach_support` gains a `locator` path that runs alone. The item writer
records `method` and the new statuses, and `claim_type` moves to rule `claim_v2`. Measurement extends
`scripts/item_support_eval/`.

**Tech Stack:** Python 3.11 (`C:\Users\camil\fspy\Scripts\python.exe`), pytest, real Postgres for
integration tests (`fswork/run-tests.sh`), SAM template, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-11-10-item-quote-locator-design.md` (slice 1:
`docs/superpowers/specs/2026-11-08-track-c-item-history-and-evidence-chain-design.md`).

## Global Constraints

- Branch from `origin/develop`, PR to `develop`. Merge your own develop PR when CI is green, after
  checking that the base is not `main` or `release`.
- No migration in this slice (`event_links.method` is text, `score` is nullable). If one turns out to be
  needed, take the next free number at merge time.
- `EMIT_ITEM_LOCATOR` defaults to `false` everywhere. Wire it as template parameter `EmitItemLocator`,
  pass `vars.TEST_EMIT_ITEM_LOCATOR || 'false'` and `vars.PROD_EMIT_ITEM_LOCATOR || 'false'` from the two
  workflows, and set the env var on `ExtractSessionFunction` only. Prod flags and prod data are the
  owner's.
- With the locator off, the live, final and group prompts are byte-identical to today's.
- `said_at` never reaches an artifact. It is stripped on every final and group code path, in place,
  without reordering, after continuity resolve and `_run_linker`, and never touching
  `status_suggestion`.
- Log lines carry exception type names and counts only. No item text, no transcript text.
- No customer text in committed fixtures (synthetic text only). Eval data, runs and labels stay
  gitignored.
- No model identifiers in commits, PR titles or code comments.
- Never use `git stash`. Never run `git add -A` on develop. Worktrees live in `C:\Users\camil\wt`.
- Every SQL path gets a real-Postgres integration test.
- The prompt wording is frozen before the first measured call, and its sha256 is recorded in every run
  file.
- Paid model calls happen only in Task 9, after the owner has been given the exact count.

## Review Focus

1. **Elided transcript:** a stamp that names a turn in the elided middle → `not_shown`, never `linked`
   (Task 1 + Task 3 tests).
2. **Locator on but a group extraction:** the group prompt is unchanged and its children carry no
   `said_at` (Task 2 + Task 4 tests).
3. **Continuity fallback raises:** children still lose `said_at`; linker links still land on the same
   children (Task 4 tests).
4. **An action item with a `status_suggestion`:** after the strip, the suggestion object is
   byte-identical (Task 3 test).
5. **Two speakers start in the same second:** `ambiguous_stamp`, no link (Task 3 test).

---

### Task 1: Rendering reports which turns the model was shown

**Files:**
- Modify: `src/transcript_utils.py` (`elide_middle`)
- Modify: `src/lambda_extract_session.py` (`render_transcript`)
- Test: `tests/unit/test_transcript_shown_indices.py`

**Interfaces:**
- Produces: `elide_middle(...)` stats gain `head_lines: int` and `tail_lines: int`. Untruncated:
  `head_lines = len(lines)`, `tail_lines = 0`. `render_transcript(...)` stats gain `shown: list[int]`,
  the indices into `turns` whose lines appear in the rendered text, in order.

- [ ] **Step 1: Write failing tests.**
  - `test_untruncated_shows_every_turn`: 3 short turns → `stats["shown"] == [0, 1, 2]`.
  - `test_truncated_shown_excludes_the_middle`: 50 turns of 100 chars with `limit=2000` →
    `shown == list(range(h)) + list(range(50 - t, 50))`, where `h = stats["head_lines"]`,
    `t = stats["tail_lines"]`, and `len(shown) == 50 - stats["lines_omitted"]`.
  - `test_existing_stats_keys_unchanged`: `chars, lines, lines_omitted, truncated` keep their values
    on both fixtures.
- [ ] **Step 2: Run** `C:\Users\camil\fspy\Scripts\python.exe -m pytest tests/unit/test_transcript_shown_indices.py -q` → FAIL.
- [ ] **Step 3: Implement.** Record `len(head)` and `len(tail)` in stats. `render_transcript` maps them
  to indices.
- [ ] **Step 4: Run** the new tests, plus `tests/unit -k "transcript or elide or render"` → PASS.
- [ ] **Step 5: Commit** `feat(extract): rendering reports the turn indices the model was shown`.

### Task 2: Gated prompt block, flag wiring, byte-identical guarantee

**Files:**
- Modify: `src/lambda_extract_session.py` (`EMIT_ITEM_LOCATOR` constant next to `EMIT_ITEM_SUPPORT`;
  `_instructions_block(locator=False)`; `build_extraction_prompt(..., locator=False)`;
  `build_group_prompt` passes `locator=False` explicitly; the single-session call site passes
  `locator=final and EMIT_ITEM_LOCATOR`)
- Modify: `src/template.yaml`, `.github/workflows/deploy.yml`, `.github/workflows/deploy-prod.yml`
- Test: `tests/unit/test_extraction_locator_prompt.py`

**Interfaces:**
- Produces: `_instructions_block(locator: bool = False) -> str`;
  `build_extraction_prompt(..., locator: bool = False)`; the module constant `EMIT_ITEM_LOCATOR: bool`;
  the module constant `LOCATOR_FIELD = "said_at"`.

- [ ] **Step 1: Write failing tests.**
  - `test_locator_off_prompts_are_byte_identical`: before editing, capture golden strings from
    `origin/develop` for (a) a live prompt, (b) a final prompt, (c) a group prompt, on a fixed
    synthetic session. Commit them as test fixtures under `tests/unit/fixtures/locator_prompt_golden/`
    (synthetic text only). Assert equality after the change, with `locator=False`.
  - `test_locator_on_adds_said_at_to_all_four_kinds`: in the schema block, `"said_at"` appears once
    inside each of `action_items`, `findings`, `decisions`, `questions`.
  - `test_locator_on_wording`: the block contains `FIRST raised`, `null`, and a sentence naming
    `status_suggestion` and saying `said_at` is not its `at`. The text
    `Do NOT put evidence inside action_items or findings` is still present when `EMIT_EVIDENCE` is on.
  - `test_group_prompt_never_has_locator`: with `EMIT_ITEM_LOCATOR` monkeypatched true,
    `build_group_prompt` output contains no `said_at`.
  - `test_flag_wired`: `template.yaml` has the parameter `EmitItemLocator` with default `'false'` and
    env `EMIT_ITEM_LOCATOR: !Ref EmitItemLocator` on `ExtractSessionFunction` only. Each workflow
    passes `EmitItemLocator=${{ vars.<ENV>_EMIT_ITEM_LOCATOR || 'false' }}` (mirror how the
    `EmitItemSupport` assertions are written in `tests/unit/test_extract_session_item_support_flag.py`).
- [ ] **Step 2: Run** them → FAIL (except the golden test, which passes before the edit; that is its
  point).
- [ ] **Step 3: Implement.** The wording follows spec §4.1. Keep the block adjacent to the existing
  numbered rules. Do not edit `status_suggestions.INSTRUCTION`.
- [ ] **Step 4: Run** the new file, plus `tests/unit -k "prompt or extract"` → PASS. Run cfn-lint on
  the template, as CI does.
- [ ] **Step 5: Commit** `feat(extract): EMIT_ITEM_LOCATOR prompt block, final tier only, default off`.

### Task 3: `item_locator` resolver and strip (pure)

**Files:**
- Create: `src/item_locator.py`
- Test: `tests/unit/test_item_locator.py`

**Interfaces:**
- Consumes: `lambda_extract_session._parse_at` semantics. Move the HH:MM:SS parse into
  `item_locator`, or import a shared helper, so `item_locator` has no import cycle with the Lambda.
  `item_support._window_turns` / `_TIME_RANGE` for the range and the midnight shift.
  `turn_name_overlay._stem`.
- Produces:
  - `RULE_VERSION = "locator_v1"`;
  - `STATUSES = ("linked", "no_locator", "unparseable", "not_shown", "no_line", "ambiguous_stamp", "outside_topic")`;
  - `resolve(child: dict, topic: dict, turns: list, shown: set[int], session_date: str) -> dict`,
    returning a support object. `{"status": "linked", "source_stem", "start_sec", "end_sec",
    "speaker_label", "quote_status": None, "method": "locator_v1", "rule_version": "locator_v1",
    "score": None, "range_checked": bool}` or `{"status": <refusal>, "method", "rule_version",
    "range_checked"?}`;
  - `strip(topics: list) -> int`, which pops the top-level `said_at` in place from every dict child of
    the four kinds and returns the count. Malformed shapes are skipped, never raised on.

- [ ] **Step 1: Write failing tests** (synthetic turns with `abs_start_str`, `source_filename`,
  `start`/`end`, `speaker`):
  - `linked` on an exact stamp inside the range, with locator keys equal to slice 1's `DST_TURN_KEYS`
    shape;
  - `9:05:03` normalised to `09:05:03` → linked;
  - `"9am"` → `unparseable`; `None` and an absent key → `no_locator`;
  - stamp matching only an index outside `shown` → `not_shown`; no match anywhere → `no_line`;
  - two shown turns with the same `abs_start_str` (same file, different speakers) → `ambiguous_stamp`;
  - a turn 2 minutes outside `time_range` → `outside_topic`; 50 s outside → linked (1-minute widening);
  - a range crossing midnight (`23:58 – 00:03`) → linked for `00:01:10`;
  - unparseable `time_range` → linked with `range_checked: False`;
  - `strip`: removes `said_at` from all four kinds, leaves `status_suggestion` (with its own `at`)
    byte-identical (`json.dumps` equality), keeps list identity and order (`is` and index checks),
    and survives a topic whose `findings` is a string;
  - the support object contains no item or turn text (assert no value equals the turn's `text`).
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(item-support): pure line-locator resolver and said_at strip`.

### Task 4: Wire the locator into extraction

**Files:**
- Modify: `src/item_support.py` (`PATHS`/known paths include `"locator"`; `attach_support(result,
  turns, session_date, *, floor, margin, nothing_close, shown=None, locator=False)`)
- Modify: `src/lambda_extract_session.py` (call site ~L2669; strip placement; group path; continuity
  fallback)
- Test: `tests/unit/test_item_support.py` (extend), `tests/unit/test_extract_locator_wiring.py`

**Interfaces:**
- Consumes: Task 1 `stats["shown"]` (keep the stats from `build_extraction_prompt`; it already
  returns `transcript_stats`). Task 3 `resolve`/`strip`.
- Produces: with `locator=True`, `attach_support` sets each child's `support` from `item_locator.resolve`,
  never consults the lexical constants, and does not raise when they are `None`. It counts every
  `item_locator.STATUSES` value. With `locator=False`, behaviour is unchanged (still raises on `None`).

- [ ] **Step 1: Write failing tests.**
  - `test_attach_locator_runs_with_none_constants`, and `test_attach_lexical_still_raises_on_none`.
  - `test_counts_cover_every_locator_status` (no `KeyError` for any status).
  - In `test_extract_locator_wiring.py`, drive `extract_session` with a mocked `call_llm` that
    returns children carrying `said_at` (follow the mocking pattern in
    `tests/unit/test_extract_session_item_support_flag.py`):
    - flags `EMIT_ITEM_LOCATOR` + `EMIT_ITEM_SUPPORT` on → written children carry `support` with
      `method == "locator_v1"`, and no `said_at`;
    - `EMIT_ITEM_LOCATOR` off but the model volunteers `said_at` anyway → the written artifact has no
      `said_at` (strip regardless of flags);
    - locator on, support off → no `support`, no `said_at`;
    - continuity fallback: monkeypatch `item_continuity.normalise_children` to raise with the locator
      on → no `said_at` in the written children (TrackB's ask 1);
    - `CONTINUITY_LINKER` + `DECLARE_CONTINUITY` + locator on, with a stubbed `_run_linker` returning
      links by ref → each link lands on the same child object (by text) as with the locator off
      (TrackB's ask 2);
    - live tier with locator on → the prompt has no `said_at`, and nothing is resolved;
    - `extract_group` with the model volunteering `said_at` → stripped (extend
      `tests/unit/test_group_evidence_strip.py`).
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** Order in the final path: continuity resolve /
  fallback → linker → `verify_evidence` → `attach_support` on the deep copy (as now) → `strip` on
  `parsed_topics` unconditionally. Strip in the group path too.
- [ ] **Step 4: Run** the new and extended tests, plus `tests/unit -k "extract or continuity or item_support"` → PASS.
- [ ] **Step 5: Commit** `feat(extract): locator path in attach_support; said_at stripped on every path`.

### Task 5: Writer, metrics, `claim_type` v2

**Files:**
- Modify: `src/repositories/claim_type_store.py` (`SUPPORT_STATUSES`; pass `method` to
  `insert_supported_by`)
- Modify: `src/lambda_item_writer.py` (`_SUPPORT_METRIC`)
- Modify: `src/claim_type.py` (`RULE_VERSION = "claim_v2"`; `INFERRED_FROM_NO_LOCATOR = False`)
- Test: `tests/unit/test_claim_type.py` (extend), `tests/integration/test_item_support_locator_write.py`

**Interfaces:**
- Produces: `claim_type.derive(support, named, recorder_user_id)`. For `linked`, the result is as v1.
  `no_locator` → `("inferred", basis)` only when `INFERRED_FROM_NO_LOCATOR`, else `(None, basis)`.
  Every other locator status → `(None, basis)`. `basis["rule_version"] == "claim_v2"`. The v1
  `none_close` → `inferred` mapping is kept for lexical supports, which this slice never produces.

- [ ] **Step 1: Write failing tests.**
  - Unit: each locator status maps as above, with both values of `INFERRED_FROM_NO_LOCATOR`
    (monkeypatched).
  - Unit: `_SUPPORT_METRIC` and `SUPPORT_STATUSES` contain every `item_locator.STATUSES` value.
  - Integration (real Postgres; reuse the committed-connection harness and id-based cleanup from
    `tests/integration/test_supersede_two_passes.py`): run `write_extraction_items` on an extraction
    whose children carry a locator `linked` support and a `no_locator` support. Then:
    - `event_links` has one row with `method = 'locator_v1'`, `rule_version = 'locator_v1'`,
      `score IS NULL` and `dst_turn` keys equal to `DST_TURN_KEYS`;
    - `claim_type` is NULL on both rows (no names, flag constant false);
    - deleting the item row cascades the link (the slice 1 FK).
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** the unit tests, then the
  integration test via `fswork/run-tests.sh` (one pytest at a time) → PASS.
- [ ] **Step 5: Commit** `feat(item-writer): locator method and statuses stored; claim_type rule v2`.

### Task 6: Measurement harness, extraction arms

**Files:**
- Create: `scripts/item_support_eval/arms.py`
- Modify: `scripts/item_support_eval/store.py` (paths `runs/locator/<arm>/<rep>/<ref>.json`)
- Test: `tests/unit/test_item_support_eval_arms.py`

**Interfaces:**
- Consumes: the Task 10 manifest and sessions under `data/` (`turns`, `date`, `folder`, `base`,
  `split`), `scripts/jev_eval/baseline.py:load_deployed_llm_env`, `llm_utils.call_llm`/`extract_json`,
  and `build_extraction_prompt(..., speaker_names=None, continuity_block="", locator=<arm>)`.
- Produces: `prompt_sha(locator: bool) -> str` (sha256 of the instruction block);
  `plan_calls(stage) -> int`; `run_arm(sessions, arm, rep, *, dry_run)`. Each run file holds
  `{ref, arm, rep, prompt_sha, manifest_digest, topics (after resolve + strip, support attached for B),
  shown, usage, latency_s, error}`. The CLI is `arms.py {count|run} --stage {fit|heldout|side}`.
  `count` prints the exact call count and makes no call. `run` refuses unless `--confirm-count N`
  equals `plan_calls(stage)`.
- The side set: `build` fetches the six status-line sessions into `data/side/`. For those, arms use
  the deployed final's `continuity_block` (`item_continuity.render_block(prior,
  status_suggestions.INSTRUCTION)` over the published prior), and the run file adds the extractor's
  `status_suggestions` counts (offered / kept / `dropped_by`).

- [ ] **Step 1: Write failing tests** with a fake `call_llm` and a fake env loader:
  - `count` makes 0 calls and returns 120 / 116 / 24 for fit / heldout / side on a manifest of
    30/29/6;
  - `run` without a matching `--confirm-count` exits non-zero before any call;
  - a run file has `prompt_sha`, and B's prompt sha differs from A's;
  - B topics contain `support` and no `said_at`; A topics contain neither;
  - an LLM error is recorded per session, and the run continues;
  - an existing run file is skipped (resume), not re-called.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(eval): locator A/B extraction arms with staged, confirmed call counts`.

### Task 7: Measurement harness, labels and bars

**Files:**
- Modify: `scripts/item_support_eval/label.py` (`export_locator`, `owner_sample_locator`)
- Modify: `scripts/item_support_eval/score.py` (`score_locator`)
- Test: `tests/unit/test_item_support_eval_locator_score.py`

**Interfaces:**
- Consumes: Task 6 run files; `score.cp_upper`, `twin_flags`, `normalise_gold`; `item_text_match.overlap`.
- Produces:
  - `export_locator(run_dir)` writes blind rows for B1 items: item text plus topic turn views, with
    no `said_at` and no support status;
  - `owner_sample_locator(rows, seed)` takes 40 held-out items, 20 `linked`, 12 `no_locator` and
    8 refusals, filling a short stratum from `linked`;
  - `match_items(run_x, run_y)` is greedy one-to-one, same kind,
    `s = max(overlap(a, b), overlap(b, a)) ≥ 0.5`, ties broken by kind, then position;
  - `score_locator(...) -> dict` holds bars 1–8 exactly as spec §6, the fit gate (bars 1, 3, 4,
    `insufficient` below 59 links), the pooled and per-half figures, and the reported diagnostics:
    - coverage by kind and by script pair;
    - `ambiguous_stamp` / `not_shown` / `range_checked: false` rates;
    - the share of links whose `said_at` equals a topic `evidence.at` (Task 6 keeps the raw `said_at`
      in a private run-file field for this diagnostic only);
    - the token and latency deltas;
    - the side-set status-suggestion table.

- [ ] **Step 1: Write failing tests** on small synthetic runs:
  - bar 3: correct iff the linked turn is in gold `turns`, with no adjacency allowance; CP bound
    values match `cp_upper`;
  - bar 6: 0 of 28 → `reported_only`, inferred off; 0 of 29 → pass; 3 of 29 → fail;
  - bar 1: identical A1/A2/B1/B2 → pass; B dropping 30% of items → fail;
  - bar 7: one link moved to another turn among 10 matched → 0.9 → pass at the boundary;
  - fit gate: 58 links → `insufficient`;
  - the owner sample's strata sizes and determinism under the seed;
  - exported blind rows contain no `said_at` / `support` keys.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(eval): locator labels, owner sample and pre-registered bars`.

### Task 8: Freeze, PR, deploy to TEST with flags off

**Files:**
- Create: `docs/superpowers/specs/2026-11-10-item-quote-locator-preregistration.md`, with the spec §6
  bars verbatim, the B prompt sha256 from `arms.prompt_sha(True)`, the owner-sample seed, the manifest
  digest, and the exact call counts.

- [ ] **Step 1:** Write the pre-registration doc. Commit it before any paid call.
- [ ] **Step 2:** Full unit suite: `C:\Users\camil\fspy\Scripts\python.exe -m pytest tests/unit -q` → PASS.
  Run the integration tests touched (Task 5, slice 1's event_links tests) → PASS.
- [ ] **Step 3:** Push, then open the PR to `develop`. The title has no model names, and the body ends
  with the attribution lines. Wait for CI green. Check that the base is `develop`, then merge.
- [ ] **Step 4:** Confirm the TEST deploy run succeeds. Run `aws lambda get-function-configuration` on
  `fieldsight-test-extract-session` (profile `fieldsight-deployer`) and confirm
  `EMIT_ITEM_LOCATOR=false`. Over the next 30 minutes, the extract-session and item-writer logs show
  0 errors.
- [ ] **Step 5:** Tell jev TrackB and 待办状态+语音建议 the merge sha.

### Task 9: Run the measurement and record the result

- [ ] **Step 1:** `arms.py count --stage fit` and `--stage side`. Send the owner the exact numbers
  (expected 120 + 24) before running.
- [ ] **Step 2:** Run fit and side, passing the confirmed counts.
- [ ] **Step 3:** Blind agent labelling of fit B1 items (the labeller never opens run supports). Score
  the fit gate. On `fail` or `insufficient`, stop and go to Step 6.
- [ ] **Step 4:** `arms.py count --stage heldout`, then run it. Label held-out B1 blind and export the
  owner sample to `labels/todo.owner.locator.jsonl`.
- [ ] **Step 5:** Score all bars except bar 8. Bar 8, and the final verdict, wait for the owner's
  labels.
- [ ] **Step 6:** Record counts only, with no customer text, in a dated result section of the
  pre-registration doc. Send it as a docs PR to develop. If the side set shows fewer suggestions kept
  under B, message the status line. Set `INFERRED_FROM_NO_LOCATOR` only per bar 6, in a follow-up PR
  after the verdict. Flags are flipped only after a pass: TEST by me, prod by the owner.
