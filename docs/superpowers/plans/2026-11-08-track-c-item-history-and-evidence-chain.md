# Track C slice 1: shared item history and the evidence chain — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A site's earlier items become queryable across sessions (`find_items`), every final-pass item is linked by code to the transcript turn it came from (`event_links.supported_by`), and each item stores a derived `claim_type`, with no prompt change and no display change.

**Architecture:**
- `find_items` is an in-VPC repository function that the status line's reconcile step, jev TrackB's cross-day prior and the nightly agent all use.
- `item_support` is a pure module run by the non-VPC extractor on the final tier. It scores each child item against the topic's transcript turns with a shared ASCII + CJK-bigram tokeniser, and writes a `support` object into the artifact.
- The in-VPC item writer turns `support` into `event_links` rows after carry-forward, and stamps `claim_type`. The voiceprint writer re-derives `claim_type` once speaker names land.
- A measurement harness must pass pre-registered bars before `EMIT_ITEM_SUPPORT` goes on anywhere.

**Tech Stack:** Python 3.11 Lambdas, psycopg3, Aurora Postgres (local pgvector PG16 for tests), SAM, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-11-08-track-c-item-history-and-evidence-chain-design.md` (owner-approved 2026-11-08). Read it first. Section numbers below refer to it.

## Global Constraints

- Branch `feature/track-c-event-links` from `origin/develop`. PR to `develop`. Check that the base is not `main`/`release` before merging. Merge on green CI.
- Migration number: `0097` (take the next free number at merge time; expect to renumber).
- Flag `EMIT_ITEM_SUPPORT` defaults `false` in the template, in `TEST_EMIT_ITEM_SUPPORT` and in `PROD_EMIT_ITEM_SUPPORT`. When off, the extraction artifact is byte-identical to today's. Prod flags are the owner's.
- `stable_id` is the cross-session identity. `item_id` is same-source-key lineage only and is never a link endpoint.
- `event_links` has no `company_id`; company is derived through `sites.company_id` (§5.1).
- Every read of items or links goes through `deleted_predicates` (`CHILD_OF_VISIBLE_TOPIC`, `visible_topics_predicate`), never an inlined copy.
- Topic deletion is a revocable tombstone: links are hidden, never deleted, by a redaction. Only `delete_topics_for_source[_prefix]` removes rows; those removals cascade.
- `event_links` holds locators and speaker labels only, never transcript text. Logs carry ids, counts and codes only.
- `item_support` runs on the `final` tier only; never on `group` or `live`.
- `statuses` is a dict kind → list. An empty list raises `ValueError`. A status filter on `decision` or `finding` raises `ValueError`.
- Action statuses: `open, in_progress, blocked, done, closed`. Question statuses: `open, answered, dropped`.
- Ranking order: `score DESC, first_seen DESC, stable_id ASC`.
- Turn joins to `speaker_turn_names` use `turn_name_overlay` (`TOLERANCE_SEC = 0.5`), never equality.
- In-VPC code makes no new AWS API calls (BUG-36). The extractor makes no DB calls.
- Tests: `bash /c/Users/camil/fswork/run-tests.sh <args>` from the worktree root (`C:\Users\camil\wt\track-c`). Run one pytest at a time. Every SQL path gets a test under `tests/integration/`.
- No customer text in committed fixtures or results. Commit counts only; harness data under `scripts/item_support_eval/data/` is gitignored.
- No model identifier in commits, PR titles or code comments.
- Never `git stash`. Never `git add -A`. Add files by name.

## Review Focus

- **A Chinese-only item against a Chinese turn** ("二楼防水膜明天到" vs a turn containing "二楼的防水膜明天会到"). It must score above zero and link; ASCII folding must not erase it → Task 2 `test_cjk_bigrams_score_chinese_overlap`, Task 5 `test_chinese_item_links_to_chinese_turn`.
- **Twin items in one topic** ("unit 3 pre-line done", "unit 4 pre-line done") with two turns differing only in the unit. Each item links to its own turn or to nothing, never to the twin's → Task 5 `test_twin_items_never_link_to_each_others_turn`.
- **A carried-forward item.** The link must carry the stable_id *after* carry-forward moved it, not the fresh default → Task 7 `test_link_records_post_carry_stable_id`.
- **Redacted then undeleted topic.** `find_items` and the link reader hide its items while the redaction is active and show them again after `reverted_at` is set → Task 4 `test_redaction_hides_links_and_undelete_restores_them`.
- **A project moved to another company.** `find_items` called with the old company returns nothing for that site; with the new company it returns the site's items → Task 3 `test_moved_site_is_scoped_by_its_current_company`.

---

### Task 1: Migration 0097 — `event_links`, `claim_type` columns

**Files:**
- Create: `src/migrations/0097_event_links.sql`
- Test: `tests/unit/test_migration_event_links_shape.py`, `tests/integration/test_event_links_schema.py`

**Interfaces:**
- Produces: table `event_links`; columns `claim_type`, `claim_basis` on `action_items`, `findings`, `topic_decisions`, `topic_questions`.

- [ ] **Step 1: Write the migration** (the schema is the decision, so verbatim):

```sql
-- Track C slice 1 (spec 2026-11-08 §5.1, §5.4): one edge table for item relationships, and a
-- derived claim_type. Only supported_by is written by this slice; resolves (status line),
-- continues (continuity line) and supersedes are reserved for their owners' writers.
CREATE TABLE IF NOT EXISTS event_links (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  site_id             uuid NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  link_type           text NOT NULL
                        CHECK (link_type IN ('supported_by','resolves','continues','supersedes')),
  src_kind            text NOT NULL
                        CHECK (src_kind IN ('action_item','finding','decision','question')),
  src_stable_id       uuid NOT NULL,
  src_action_item_id  uuid REFERENCES action_items(id)    ON DELETE CASCADE,
  src_finding_id      uuid REFERENCES findings(id)        ON DELETE CASCADE,
  src_decision_id     uuid REFERENCES topic_decisions(id) ON DELETE CASCADE,
  src_question_id     uuid REFERENCES topic_questions(id) ON DELETE CASCADE,
  source_s3_key       text,            -- the extraction key of the pass that wrote it
  dst_kind            text NOT NULL
                        CHECK (dst_kind IN ('turn','action_item','finding','decision','question')),
  dst_stable_id       uuid,
  dst_turn            jsonb,           -- {source_stem, start_sec, end_sec, speaker_label, quote_status}
  method              text NOT NULL,
  score               real,
  rule_version        text NOT NULL,
  decision_record_id  uuid,
  created_at          timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT event_links_dst_shape CHECK (
    (dst_kind = 'turn') = (dst_turn IS NOT NULL AND dst_stable_id IS NULL)),
  CONSTRAINT event_links_dst_item CHECK (
    dst_kind = 'turn' OR dst_stable_id IS NOT NULL),
  CONSTRAINT event_links_supported_by_row CHECK (
    link_type <> 'supported_by' OR (
      (src_kind = 'action_item' AND src_action_item_id IS NOT NULL
         AND src_finding_id IS NULL AND src_decision_id IS NULL AND src_question_id IS NULL) OR
      (src_kind = 'finding' AND src_finding_id IS NOT NULL
         AND src_action_item_id IS NULL AND src_decision_id IS NULL AND src_question_id IS NULL) OR
      (src_kind = 'decision' AND src_decision_id IS NOT NULL
         AND src_action_item_id IS NULL AND src_finding_id IS NULL AND src_question_id IS NULL) OR
      (src_kind = 'question' AND src_question_id IS NOT NULL
         AND src_action_item_id IS NULL AND src_finding_id IS NULL AND src_decision_id IS NULL)))
);
CREATE INDEX IF NOT EXISTS idx_event_links_src ON event_links (src_stable_id, link_type);
CREATE INDEX IF NOT EXISTS idx_event_links_dst ON event_links (dst_stable_id) WHERE dst_stable_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_event_links_site_source ON event_links (site_id, source_s3_key);
CREATE INDEX IF NOT EXISTS idx_event_links_ai ON event_links (src_action_item_id) WHERE src_action_item_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_event_links_fi ON event_links (src_finding_id)     WHERE src_finding_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_event_links_de ON event_links (src_decision_id)    WHERE src_decision_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_event_links_qu ON event_links (src_question_id)    WHERE src_question_id IS NOT NULL;

-- Derived, never self-reported (assessment §2.4). NULL = unknown.
ALTER TABLE action_items    ADD COLUMN IF NOT EXISTS claim_type text;
ALTER TABLE action_items    ADD COLUMN IF NOT EXISTS claim_basis jsonb;
ALTER TABLE findings        ADD COLUMN IF NOT EXISTS claim_type text;
ALTER TABLE findings        ADD COLUMN IF NOT EXISTS claim_basis jsonb;
ALTER TABLE topic_decisions ADD COLUMN IF NOT EXISTS claim_type text;
ALTER TABLE topic_decisions ADD COLUMN IF NOT EXISTS claim_basis jsonb;
ALTER TABLE topic_questions ADD COLUMN IF NOT EXISTS claim_type text;
ALTER TABLE topic_questions ADD COLUMN IF NOT EXISTS claim_basis jsonb;
ALTER TABLE action_items    ADD CONSTRAINT action_items_claim_type_check
  CHECK (claim_type IN ('observed','stated','inferred'));
ALTER TABLE findings        ADD CONSTRAINT findings_claim_type_check
  CHECK (claim_type IN ('observed','stated','inferred'));
ALTER TABLE topic_decisions ADD CONSTRAINT topic_decisions_claim_type_check
  CHECK (claim_type IN ('observed','stated','inferred'));
ALTER TABLE topic_questions ADD CONSTRAINT topic_questions_claim_type_check
  CHECK (claim_type IN ('observed','stated','inferred'));
```

- [ ] **Step 2: Shape test** (unit, SQL text):
  - `test_only_one_migration_uses_0097`;
  - `test_event_links_has_no_company_id` (the string `company_id` does not occur in the CREATE TABLE);
  - `test_every_src_fk_cascades` (4 × `ON DELETE CASCADE` on the `src_*_id` lines);
  - `test_link_types_are_exactly_the_four`;
  - `test_claim_columns_on_all_four_tables`.
- [ ] **Step 3: Integration test** (real Postgres, `migrated_db_url`):
  - `test_supported_by_requires_exactly_the_matching_row_fk`: wrong kind → `CheckViolation`; two FKs → violation;
  - `test_turn_link_rejects_dst_stable_id`;
  - `test_item_link_requires_dst_stable_id`;
  - `test_deleting_an_action_item_row_cascades_its_links`;
  - `test_superseding_a_topic_keeps_its_links` (UPDATE `superseded_at`, the link row survives);
  - `test_bad_claim_type_rejected`.
  - Also run `tests/integration/test_migrations_apply.py` (idempotent re-apply) green.
- [ ] **Step 4: Commit** `git add src/migrations/0097_event_links.sql tests/unit/test_migration_event_links_shape.py tests/integration/test_event_links_schema.py && git commit -m "Migration 0097: event_links and derived claim_type columns"`

---

### Task 2: Shared tokeniser — `src/item_text_match.py`

**Files:**
- Create: `src/item_text_match.py`
- Test: `tests/unit/test_item_text_match.py`

**Interfaces:**
- Produces:
  - `tokens(text: str) -> list[str]`: lower-cased ASCII word tokens (`[a-z0-9]+`, English stop words dropped from the fixed list in the module), plus CJK character bigrams. A single CJK char between non-CJK chars is a unigram. Uses `evidence_match.is_cjk` and `evidence_match.strip_cjk_spacing`. It never ASCII-folds CJK away.
  - `overlap(item_text: str, turn_text: str) -> tuple[float, list[str]]`: a containment score `|T(item) ∩ T(turn)| / |T(item)|`, with token multiplicity ignored. Returns 0.0 when the item has no tokens. Also returns the matched tokens, sorted.
  - `RULE_VERSION = "lexical_v1"`.
  - Pure: no AWS, no DB imports. Bundled into the extractor and item-writer zips (check the zip lists in `src/template.yaml` / the build step that copies shared modules).

- [ ] **Step 1: Failing tests:**
  - `test_ascii_words_lowercased_and_stopwords_dropped`;
  - `test_cjk_bigrams_score_chinese_overlap` (`overlap("二楼防水膜明天到", "二楼的防水膜明天会到")[0] >= 0.5`);
  - `test_mixed_language_tokens` (`"Level 2 防水膜"` → contains `"level"`, `"2"`, `"防水"`, `"水膜"`);
  - `test_empty_item_scores_zero`;
  - `test_matched_terms_sorted_and_deterministic`.
- [ ] **Step 2: Run** `bash /c/Users/camil/fswork/run-tests.sh tests/unit/test_item_text_match.py -q`. Expected: FAIL (module missing).
- [ ] **Step 3: Implement** the three names above.
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `git add src/item_text_match.py tests/unit/test_item_text_match.py` + the zip-list file if changed, `-m "item_text_match: one tokeniser for item history and item support (ASCII words + CJK bigrams)"`

---

### Task 3: `find_items` — `src/repositories/item_history.py`

**Files:**
- Create: `src/repositories/item_history.py`
- Test: `tests/unit/test_item_history_args.py`, `tests/integration/test_item_history.py`

**Interfaces:**
- Consumes: `item_text_match.overlap`; `deleted_predicates.CHILD_OF_VISIBLE_TOPIC` / `visible_topics_predicate`.
- Produces:
  ```
  find_items(conn, *, company_id, site_id, kinds, statuses=None, since=None, until=None,
             recorded_by=None, responsible=None, exclude_source_keys=None, query=None,
             limit=10) -> list[dict]
  ```
  - **`kinds`:** a subset of `('action_item','finding','decision','question')`; an empty or unknown kind raises `ValueError`.
  - **Returned dict keys** (exactly): `kind, stable_id, item_id, text, responsible, deadline, status, first_seen, source, recorded_by, carried_from, score, matched_terms, place, origin`.
    - `source` is `{user_folder, date, session_base, source_s3_key}` of the **first_seen** row.
    - `place` is always `None` in this slice.
    - `origin` comes from the **live** row's `supported_by` link: `{source_stem, start_sec, end_sec, speaker_label, quote_status}`, or `None`.
  - **Text column per kind:** `action_items.text`, `findings.observation`, `topic_decisions.decision`, `topic_questions.question`.
    - `responsible` is `action_items.responsible`, `findings.entity_name`, `topic_decisions.decided_by`, or `None` for questions.
    - `status` is `None` for decisions.
  - **Scope:** the site must belong to `company_id` (join `sites`); otherwise return `[]`.
  - **Rows:** live rows only (`CHILD_OF_VISIBLE_TOPIC`).
  - **`first_seen`:** `min(created_at)` over all rows of that kind with the same `stable_id` and `site_id`, superseded passes included.
  - **`since`/`until`:** filter on the first_seen row's `topics.report_date` (inclusive dates).
  - **`recorded_by`:** the `{folder}` parsed from the first_seen row's extraction key `extractions/{folder}/{date}/{base}.json`. A key that does not parse gives `recorded_by=None`, and such a row never matches a `recorded_by` filter.
  - **`exclude_source_keys`:** removes items whose **live** row's `topics.source_s3_key` is in the list.
  - **`query`:** ranks by `overlap(query, text)` and never filters.
  - **Ordering:** `score DESC, first_seen DESC, stable_id ASC`, then truncated to `limit`.

- [ ] **Step 1: Failing unit tests** (argument validation, no DB):
  - `test_empty_status_list_rejected`;
  - `test_status_filter_on_decision_rejected`;
  - `test_status_filter_on_finding_rejected`;
  - `test_unknown_kind_rejected`;
  - `test_empty_kinds_rejected`.
- [ ] **Step 2: Failing integration tests** (seed via repository insert functions, never raw customer text: use synthetic strings):
  - `test_returns_only_this_sites_live_items`;
  - `test_moved_site_is_scoped_by_its_current_company` (Review Focus);
  - `test_superseded_pass_hidden_but_counts_for_first_seen` (two passes, same stable_id: one result, `first_seen` = the first pass's created_at, `source.source_s3_key` = the first pass's key);
  - `test_since_until_filter_on_first_seen_report_date`;
  - `test_statuses_dict_filters_per_kind` (`{'action_item': ['open','in_progress','blocked']}` excludes `done`/`closed`, leaves decisions unfiltered);
  - `test_exclude_source_keys_drops_this_sessions_items`;
  - `test_recorded_by_filters_on_first_seen_recorder`;
  - `test_chinese_query_ranks_chinese_item_first`;
  - `test_tie_order_is_first_seen_desc_then_stable_id`;
  - `test_origin_from_live_rows_supported_by_link` (insert a link on the superseded row and another on the live row; `origin` is the live one's);
  - `test_deleted_topic_items_hidden` (a `redactions` scope='deleted' tombstone).
- [ ] **Step 3: Run each file.** Expected: FAIL.
- [ ] **Step 4: Implement.** One SQL query per kind (a `UNION ALL` is the implementer's choice) returning candidates plus first_seen via a window or a correlated `min()`. Ranking happens in Python.
- [ ] **Step 5: Run both files.** Expected: PASS. Also run `tests/unit/test_live_topic_predicate_everywhere.py`: if it scans read sites, add `item_history.find_items` to its filtered list.
- [ ] **Step 6: Commit** `-m "find_items: site-scoped item history across sessions, CJK-aware ranking"`

---

### Task 4: `event_links` repository, deletion reach, company export

**Files:**
- Create: `src/repositories/event_links.py`
- Modify: `src/company_records_export.py` (`TABLES`, add an `event_links` `TableSpec` owned through `sites`)
- Test: `tests/integration/test_event_links_repo.py`, `tests/integration/test_company_records_export.py` (extend the existing file if it has a table-coverage test; otherwise add the case to the file that asserts "every table is in TABLES or excluded")

**Interfaces:**
- Produces:
  - `insert_supported_by(conn, *, site_id, src_kind, src_row_id, src_stable_id, source_s3_key, dst_turn: dict, score: float, rule_version: str, method='lexical_v1') -> uuid`: sets the FK column for `src_kind`.
  - `live_supported_by(conn, src_kind, src_row_ids: list) -> dict[row_id, dict]`: the `dst_turn` + score for live, visible rows only.
  - `links_for_stable_id(conn, *, site_id, stable_id, link_types=None) -> list[dict]`: both directions. Each endpoint must resolve to a visible live row (src via `src_stable_id`, dst via `dst_stable_id`); a turn endpoint needs only the src side.
  - `KIND_TABLE = {'action_item': 'action_items', 'finding': 'findings', 'decision': 'topic_decisions', 'question': 'topic_questions'}`, the one mapping every Track C module imports.

- [ ] **Step 1: Failing integration tests:**
  - `test_insert_supported_by_sets_the_right_fk`;
  - `test_live_supported_by_ignores_superseded_rows`;
  - `test_redaction_hides_links_and_undelete_restores_them` (Review Focus: tombstone → `links_for_stable_id` empty; set `reverted_at` → back);
  - `test_item_link_hidden_when_dst_endpoint_deleted` (insert a `resolves` row directly; delete-tombstone the dst topic → hidden);
  - `test_physical_source_delete_cascades_links` (`topics.delete_topics_for_source` → link rows gone);
  - `test_site_delete_cascades_links`;
  - export: `event_links` appears in the company export for the owning company, not for another company.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement** the repository functions and the `TableSpec` (follow `_child`-style site ownership, `check=_owned_by("_fs_site_co")`).
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `-m "event_links repository: visibility through deleted_predicates, cascade on physical delete, company export"`

---

### Task 5: `item_support` — pure scorer on the extractor side

**Files:**
- Create: `src/item_support.py`
- Test: `tests/unit/test_item_support.py`

**Interfaces:**
- Consumes: `item_text_match.overlap`, `RULE_VERSION`; turn dicts as assembled by `lambda_extract_session` (`text`, `speaker`, `source_filename`, `start_sec`, `end_sec`, `abs_start`); topic dicts with `evidence[]` (`status`, `segment_key`, `offset_sec`) and `time_range` `"HH:MM – HH:MM"`.
- Produces:
  - `attach_support(result: dict, turns: list[dict], session_date: str, *, floor: float, margin: float, nothing_close: float) -> dict`: mutates each child of every topic. Kinds and their text fields are `action_items[].action`, `findings[].observation`, `decisions[]` (dict `decision`, or a plain string), and `questions[]` (dict `question`, or a plain string). The extractor's own key names are used: check `EXTRACTION_SCHEMA` / `item_continuity.normalise_children` and mirror them. Each child gets `support`, which is one of:
    - `{"status": "linked", "source_stem", "start_sec", "end_sec", "speaker_label", "quote_status", "score", "candidate_path": "quote"|"window", "rule_version"}`;
    - `{"status": "none_close", "best": score, "rule_version"}`;
    - `{"status": "ambiguous", "best", "second", "rule_version"}`;
    - `{"status": "below_floor", "best", "rule_version"}`.

    Returns counts per status and candidate_path.
  - **Candidate paths, in order:**
    1. **"quote":** the turns that a topic evidence quote with status `verified`/`verified_fuzzy` resolved to (match on `source_stem` + `offset_sec` within `turn_name_overlay.TOLERANCE_SEC`).
    2. **"window":** if path 1 gives no link, all turns whose `abs_start` lies within the topic's `time_range` on `session_date`.
  - **Decision rule:**
    - `best >= floor` and `best - second >= margin` → `linked` (the second-best is computed over the same path's candidates);
    - `best < nothing_close` → `none_close`;
    - otherwise `ambiguous` (when the margin fails) or `below_floor`.
  - **Constants:** `FLOOR`, `MARGIN`, `NOTHING_CLOSE` are module constants set to `None` until Task 9 fixes them on the fit half. `attach_support` refuses (`ValueError`) when any argument is `None`, so an uncalibrated module cannot run in production.
  - `quote_status` is the citing evidence's status for path "quote", `None` for path "window".

- [ ] **Step 1: Failing tests** (synthetic turns and items):
  - `test_links_item_to_its_quoted_turn`;
  - `test_window_path_used_only_when_quote_path_fails`;
  - `test_tie_within_margin_is_ambiguous_not_linked`;
  - `test_twin_items_never_link_to_each_others_turn` (Review Focus; floor 0.5, margin 0.1: "unit 3 pre-line done" / "unit 4 pre-line done" with turns "unit 3 pre-line is done" / "unit 4 pre-line is done");
  - `test_chinese_item_links_to_chinese_turn` (Review Focus);
  - `test_nothing_close_marks_none_close`;
  - `test_string_decisions_and_questions_get_support`;
  - `test_uncalibrated_constants_refuse`;
  - `test_deterministic_across_two_runs` (same input twice → identical JSON);
  - `test_no_text_in_support_object` (no key holds turn or item text).
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `-m "item_support: link each final item to its transcript turn by lexical overlap, precision first"`

---

### Task 6: Wire `EMIT_ITEM_SUPPORT` and call `item_support` on the final tier

**Files:**
- Modify: `src/template.yaml` (parameter `EmitItemSupport`, default `'false'`, env `EMIT_ITEM_SUPPORT` on `ExtractSessionFunction`, next to `EmitEvidence` at ~line 1296 / 3912)
- Modify: `.github/workflows/deploy.yml` (`"EmitItemSupport=${{ vars.TEST_EMIT_ITEM_SUPPORT || 'false' }}"` beside `EmitEvidence`, ~line 396)
- Modify: `.github/workflows/deploy-prod.yml` (`"EmitItemSupport=${{ vars.PROD_EMIT_ITEM_SUPPORT || 'false' }}"`, ~line 364)
- Modify: `src/lambda_extract_session.py` (after the `verify_evidence(...)` call at ~line 2553, final tier only)
- Test: `tests/unit/test_extract_session_item_support_flag.py`

**Interfaces:**
- Consumes: `item_support.attach_support`, `FLOOR`, `MARGIN`, `NOTHING_CLOSE`.
- Produces: `support` on each child in final artifacts when the flag is on, plus one log line `item_support: {counts}`.

- [ ] **Step 1: Failing tests:**
  - `test_flag_off_artifact_is_byte_identical` (run the final path with the flag off on a fixture; the JSON equals today's golden output);
  - `test_flag_on_final_tier_attaches_support`;
  - `test_flag_on_group_and_live_tiers_do_not_attach`;
  - `test_support_failure_is_never_fatal` (attach_support raises → artifact written without `support`, warning logged);
  - `test_flag_is_wired_template_to_both_workflows` (text scan of the three files, the pattern used by the existing unwired-toggle guards; search `tests/unit` for `EmitEvidence` to find it).
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.** Module-level `EMIT_ITEM_SUPPORT = os.environ.get('EMIT_ITEM_SUPPORT', 'false').lower() == 'true'`, read like `EMIT_EVIDENCE`. Wrap the call in try/except → `logger.exception`.
- [ ] **Step 4: Run.** Expected: PASS. Then run `tests/unit -q -k "extract_session or template or workflow"`. Expected: no new failures against the develop baseline (record the baseline count first).
- [ ] **Step 5: Commit** `-m "Wire EMIT_ITEM_SUPPORT (default off) and attach item support on the final tier"`

---

### Task 7: Writer stores links after carry-forward; `claim_type` derive + write

**Files:**
- Create: `src/claim_type.py` (pure), `src/repositories/claim_type_store.py`
- Modify: `src/lambda_item_writer.py` (collect `(kind, row_id, support)` at the four insert sites, ~lines 1540-1555 and the action-item insert inside `upsert_topic`; write links + claim_type **after** the carry-forward savepoint, inside the same transaction)
- Test: `tests/unit/test_claim_type.py`, `tests/integration/test_item_writer_event_links.py`

**Interfaces:**
- Consumes: `event_links.insert_supported_by`, `event_links.KIND_TABLE`, `turn_name_overlay.build/lookup`, `voiceprints.live_turn_names(conn, company_id, session_base)`.
- Produces:
  - `claim_type.derive(support: dict | None, named: dict | None, recorder_user_id) -> tuple[str | None, dict]` returns `(claim_type, claim_basis)`. `named` is the `turn_name_overlay.lookup` result for the linked turn (`{user_id, state, ...}` or `None`).
    - `support.status == 'none_close'` → `'inferred'`;
    - `linked` with `named.user_id == recorder_user_id` → `'observed'`;
    - `linked` with a different non-null `named.user_id` → `'stated'`;
    - every other case → `None`.
    - `claim_basis` holds `{rule_version: "claim_v1", support_status, link_id?, named_user_id?, name_state?}` and never names or text.
    - Only `state == 'confirmed'` names count; tentative names → `None`.
  - `claim_type_store.recompute_session(conn, *, company_id, site_id, session_base) -> dict` re-derives every live item of that session that has a `supported_by` link, or whose last `claim_basis.support_status` was `none_close`, and returns counts. Items are found by `event_links.source_s3_key` ending `/{session_base}.json` plus the claim_basis of the live rows of topics with that source key.
  - `claim_type_store.write(conn, kind, row_id, claim_type, claim_basis)`.
  - For the recompute, the writer stores each child's `support.status` in `claim_basis` even when no link is written.

- [ ] **Step 1: Failing unit tests** (`derive`):
  - `test_none_close_is_inferred`;
  - `test_recorder_named_is_observed`;
  - `test_other_named_is_stated`;
  - `test_unnamed_is_null`;
  - `test_ambiguous_is_null`;
  - `test_tentative_name_is_null`;
  - `test_basis_has_no_text`.
- [ ] **Step 2: Failing integration tests** (write a synthetic final artifact through `write_extraction_items` as the existing writer integration tests do):
  - `test_linked_children_get_supported_by_rows`;
  - `test_link_records_post_carry_stable_id` (Review Focus: two passes of one key, the second carried by hash; the second pass's link `src_stable_id` equals the carried stable_id);
  - `test_none_close_child_written_inferred_without_link`;
  - `test_no_support_key_writes_nothing` (flag-off artifact → zero links, claim columns NULL);
  - `test_recompute_after_names_turns_null_into_observed_and_stated`;
  - `test_link_write_failure_does_not_fail_the_pass` (savepoint; items still written).
- [ ] **Step 3: Run.** Expected: FAIL.
- [ ] **Step 4: Implement.** Link + claim writes go in their own savepoint after carry-forward; on exception roll back to it and `logger.exception`. Print one EMF line, with counts per `support.status` and per claim_type.
- [ ] **Step 5: Run both files** plus the existing writer integration tests touched by the insert sites (`tests/integration -k item_writer`). Expected: PASS.
- [ ] **Step 6: Commit** `-m "Item writer: supported_by links after carry-forward; derived claim_type"`

---

### Task 8: Recompute when names land; backfill script

**Files:**
- Modify: `src/lambda_voiceprint_writer.py` (after each path that calls `record_turn_name` and commits: `_rebind`, `_propagation`, `_match_names`, `_inherit_labels`; one helper `_recompute_claims(conn, company_id, session_base)` called once per touched session)
- Create: `scripts/claim_type_backfill.py` (in-VPC invoke or local against a DB URL; `--site`, `--since`; dry-run default)
- Test: `tests/integration/test_voiceprint_writer_claim_recompute.py`, `tests/unit/test_claim_type_backfill_args.py`

**Interfaces:**
- Consumes: `claim_type_store.recompute_session`.
- Produces: no new public names. The voiceprint line is told the hook in the PR body.

- [ ] **Step 1: Failing tests:**
  - `test_match_names_triggers_one_recompute_per_session`;
  - `test_recompute_failure_never_fails_the_name_write` (names committed, warning logged);
  - `test_backfill_dry_run_writes_nothing`.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.** The helper runs in its own transaction after the names' transaction commits; it resolves `site_id` per item row, so it needs no session-to-site lookup.
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `-m "Re-derive claim_type when speaker names land; backfill script"`

---

### Task 9: Measurement harness and pre-registration

**Files:**
- Create: `scripts/item_support_eval/{build.py,run.py,label.py,score.py,README.md}`, `scripts/item_support_eval/.gitignore` (`data/`, `runs/`, `labels/`)
- Create: `docs/superpowers/specs/2026-11-08-item-support-eval-preregistration.md` (written and committed **before** any result exists; frozen after the first result row, later changes go in dated addenda)
- Test: `tests/unit/test_item_support_eval_score.py`

**Interfaces:**
- Consumes: `item_support.attach_support`, `lambda_extract_session.assemble_session_turns`, TEST bucket finals.
- Produces: `results/summary.json` (counts only) with `"verdict": "pass"|"fail"|"insufficient"`, and the frozen `FLOOR`, `MARGIN`, `NOTHING_CLOSE` written into `item_support.py` by a reviewed commit.

- [ ] **Step 1: Write the pre-registration** (bars verbatim from spec §7 and §9):
  - **Sets:** TEST finals with evidence since 2026-10-01, at least 40 sessions, split into a fit half and a held-out half by session hash (seed recorded).
  - **Gold:** a blind agent labeller sees the item text and the topic's turns (no scores, no heuristic output) and marks one turn, "several" or "none". The owner adjudicates a seeded random sample of about 45 items, plus every agent-vs-heuristic disagreement inside that sample. The owner always wins.
  - **Fit:** grid over `floor ∈ {0.30..0.90 step 0.05}`, `margin ∈ {0.05, 0.10, 0.15, 0.20}`, `nothing_close ∈ {0.10, 0.15, 0.20, 0.25}`. Choose the point with the highest fit-half coverage subject to fit-half link precision ≥ 0.97 and inferred precision ≥ 0.95. Ties go to the higher floor.
  - **Held-out bars:**
    1. Link precision: the one-sided 95% Clopper–Pearson upper bound of wrong/accepted ≤ 5% (needs ≥ 59 accepted links with 0 errors; < 59 accepted → insufficient). A link to an adjacent same-speaker turn carrying the same words counts as correct.
    2. Twin hard negatives = 0. A mechanical check flags any accepted link where the item and the linked turn both carry a token of the same place class with different values (`unit|level|lot|room|block|floor` + number, east/west/north/south, 东/西/南/北, `\d+楼`/`\d+层`). Every flag is shown to the owner; a confirmed one fails the bar.
    3. Run-to-run: two runs give byte-identical outputs.
    4. Agent–owner agreement on the adjudicated sample ≥ 85%.
    5. `inferred` precision: the one-sided 95% CP upper bound of (gold says a turn supported it)/(labelled `inferred`) ≤ 10%. Fewer than 20 such items → reported only.
    6. Each candidate path ("quote", "window") is judged on bars 1 and 2 separately. A path that fails is disabled in `attach_support` (constant `PATHS`), and the other may ship.
  - **Reported, not gated:**
    - coverage (all, per kind, per language, per path, per evidence_status);
    - claim_type distribution on TEST;
    - `find_items` p50/p95 latency on the largest TEST site (local DB copy not allowed; run through a TEST invoke or report "not measured").
  - **Coverage decision:** held-out coverage < 60% → open a separate pre-registration for the topic-quote-index prompt option.
- [ ] **Step 2: Failing scoring tests**: one per bar with tiny hand-built fixtures, including CP boundary cases (58/58 → insufficient, 59/59 → pass, 1 error in 59 → fail).
- [ ] **Step 3: Implement** the harness (pattern: `scripts/continuity_eval/`). `label.py` writes `labels/todo.agent.jsonl` and `labels/todo.owner.jsonl`, with the item text and turn texts local only, gitignored.
- [ ] **Step 4: Run tests.** Expected: PASS.
- [ ] **Step 5: Commit** the pre-registration first (its own commit, before any `run.py` output exists), then the harness.

---

### Task 10: Run the measurement and switch on for TEST (operational; needs the owner)

- [x] **Step 1:** `build.py` + `run.py` on TEST finals (fit half). Fix the constants; commit them to `item_support.py` with the fit numbers in the commit body.
  - Result 2026-11-09 (counts only; outputs gitignored). Set: 59 eligible TEST sessions since 2026-08-01 (Aug 13, Sep 4, Oct 30, Nov 12); fit half 30 sessions / 306 items, held-out 29 / 252. Quotes 454 verified, 454 resolved; 0 empty labeller views.
  - Agent gold, blind: 558 items labelled (489 `turn`, 69 `several`, 0 `none`).
  - Fit: **no feasible point** among the 208 grid points. Link precision cleared 0.97 everywhere (best 0.987: 79 links, 1 wrong), but the `inferred` constraint (>= 0.95) failed at every point: each point leaves >= 32 `none_close` items, and gold finds a supporting turn for every one. At nothing_close 0.10 all 32 are English item text over Chinese transcripts; word overlap cannot see across languages. Best fit-half coverage ~26% (79/306), under the owner's 60% line.
  - Verdict under the pre-registration: **fail**. Constants stay None; EMIT_ITEM_SUPPORT stays off; held-out half not scored.
  - Dataset caveats: the same recordings recur under several session refs (some across halves); 37 of 59 sessions come from one folder. Both bias toward the owner's own test recordings.
  - Next, per the owner's ruling (spec §9.2, coverage < 60%): a separate pre-registration for the topic-quote-index prompt option, which is also language-independent. Not started; it needs paid extraction runs, and the call count goes to the owner first.
- [ ] **Step 2:** The agent labels gold. `label.py` exports the owner sample. Hand the owner the file path and the count. The owner labels.
- [ ] **Step 3:** `score.py` on the held-out half. Commit `results/summary.json` and paste one line per bar into this step.
- [ ] **Step 4:** All bars pass → open the PR note asking the owner to set `TEST_EMIT_ITEM_SUPPORT=true`, then redeploy TEST (give the workflow name and run link). Any fail → record it here and stop; no flag change.
- [ ] **Step 5:** After one TEST day with the flag on: count links, claim_type values and EMF counts. Verify `find_items` returns `origin` for linked items through a TEST in-VPC invoke. Report to the owner. Prod (`PROD_EMIT_ITEM_SUPPORT`) is the owner's decision.

---

## Hand-offs

- **Status line (待办状态+语音建议):** after Task 3 merges, `find_items` is usable. After Task 4, `resolves` rows can be written via a new `event_links.insert_item_link(...)`; that function is theirs to add, with its own tests.
- **jev TrackB:** `find_items(kinds=..., limit=40)` with no `query` gives the recency-ordered prior source; `continues` is reserved.
- **Architecture line (site decision loop):** the nightly re-match agent consumes `find_items`, `event_links` and `claim_type_store.recompute_session`; its silent writes use `method='nightly_*'`.
- **Voiceprint line:** Task 8 adds one best-effort call after name writes; it is described in the PR body.
