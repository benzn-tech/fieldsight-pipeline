# Multi-unit inspection (backend line) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One walk through several units produces one checklist report: a shared header, a General section, one checklist per unit, and a summary table. On site, a final check settles open items and wrong-unit findings by device voice or on the phone.

**Architecture:**
- The extraction learns three things for each spoken check: the units named in the opening, the units he walked into, and when he entered each one.
- Item-writer stores these on `inspection_windows` and gives every multi-unit window a stable `walk_key`. The key ties together walks that span several recordings.
- A new pure module, `walk_checklist`, does the checklist work for a walk. The model writes rows with a Unit column. Code then:
  - keeps only rows whose evidence appears in the transcript (`checklist.rebuild`, applied per unit);
  - applies "all good";
  - flags units not in the opening list;
  - builds the summary table.
- The report worker (`lambda_session_report`) and the final check (`check_done`) both use this module, so the report and the device can never disagree on what counts as open.
- On-site answers go into a new `walk_answers` table. A pure overlay step applies them to every report generation: fill, conflict, move.

**Tech Stack:** Python 3.11 Lambdas, Aurora Postgres (psycopg 3), SAM `template.yaml`, pytest. Local Postgres comes from `bash /c/Users/camil/fswork/run-tests.sh`.

**Spec:** `docs/superpowers/specs/2026-11-03-multi-unit-inspection-design.md`. Read it first. Its "Builder-check answers" and "Owner decisions" sections are binding.

## Global Constraints

- **Silence is never Yes.** An item that nothing addressed has an empty Answer, and its Comment is `checklist.NOT_COVERED`.
- **"All good" covers the current unit only.** Each item it answers says so in its Comment: `covered by '<quote>' (HH:MM)`.
- **Evidence rule:** a row survives only if `checklist.evidence_found(quote, transcript_words)` holds. Use the same transcript text the model was shown.
- **Customer wording:** item text and order always come from the template, never from the model.
- **Where a finding goes:**
  - a finding that names no unit, or that comes before the first unit, goes to a `General` section;
  - it is never dropped, and never guessed into a unit.
- **Multi-unit trigger:** a walk is multi-unit when the opening names units or a count, or when he enters two or more distinct units. Otherwise the check behaves exactly as today. No existing test may change its expectation.
- **Walk identity:** `(company_id, user_folder, report_date, template_id, opening start_at)`. Never `inspection_windows.id`, which is regenerated on every re-extraction.
- **`inspection_match.GENERIC`** gains `unit units lot lots today`.
- **Final check:**
  - Read the item's **text**, never its number.
  - Read back every No for confirmation.
  - If 3 or fewer questions are open, ask them on the device. With more, the device says one line and the phone shows the list.
  - The confirming state times out after 30 s.
- **Confirmation answers:**
  - Each answer is report evidence and is stored with its transcript.
  - It is never routed to general Ask.
  - Anything that is not pass / fail / N/A / a move is skipped.
- **Wrong unit:** a move takes the No to the named unit and replaces a Yes there. The origin item becomes not checked. The report shows "moved from <unit> on site (HH:MM)".
- **On-site answer vs recording:**
  - An item that was not checked on site takes the recording's answer.
  - When the on-site answer and the recording disagree, keep both and flag the item for the inspector. Neither one silently wins.
- **Web "settle later" is out of scope** (next phase).
- **Budgets:**
  - The final check stays inside `check_done.RUN_BUDGET_S` (30 s) and must report how far its evidence reaches (`covered_until`, `partial`).
- **Working rules:**
  - Repo files are CRLF. Use positional edits, never whole-file rewrites.
  - Never `git add -A`.
  - Python is `C:/Users/camil/fspy/Scripts/python.exe`.
  - Integration tests run one pytest at a time via `bash /c/Users/camil/fswork/run-tests.sh`.

## Review Focus

1. **A second recording of the same walk with no opening statement.** It must join the open walk (same `walk_key`), so there is one report. Test: Task 5, `test_a_later_recording_without_an_opening_continues_the_walk`.
2. **Re-extraction after answers were given on site.** Answers are keyed by walk_key + unit + item text, so they must survive window regeneration and a template version bump. Test: Task 7, `test_answers_survive_a_new_template_version_by_item_text`.
3. **Unit names spoken in different forms** ("unit two", "Unit 2", "U2", "lot one fifty"). They must resolve to one unit, never two sections. Test: Task 2, `test_spoken_forms_of_one_unit_are_one_unit`.
4. **A unit referred to, not entered** ("next door Unit 5 is the same"). It must not start a Unit 5 stretch, but its finding goes to Unit 5. Tests: Task 3 (no `entered` mark for it), and Task 6, `test_a_finding_for_another_unit_goes_to_that_unit`.
5. **A single-unit check that mentions "room 102".** It must stay single-unit, so today's report is byte-identical. Test: Task 2, `test_one_room_mention_is_not_a_walk`, and Task 9, `test_a_single_unit_check_is_queued_exactly_as_before`.

---

### Task 1: Unit words don't break template matching

**Files:**
- Modify: `src/inspection_match.py:21-28` (GENERIC)
- Test: `tests/unit/test_a_spoken_check_finds_its_checklist.py` (append)

**Interfaces:** Produces: `inspection_match.GENERIC` with the five new words.

- [ ] **Step 1: Write the failing test**

```python
def test_unit_words_do_not_dilute_the_match():
    tpl = {"id": "t1", "name": "Wall Framing Inspection", "scope": "org",
           "body": {"sections": [{"title": "Wall framing", "kind": "checklist", "items": ["Studs"]}]}}
    for said in ("wall framing inspection today, Lot 150 to 154",
                 "wall framing check, units 1 to 5 today"):
        t, s = inspection_match.match({"name": said, "kind": ""}, [tpl])
        assert t is tpl and s == 1.0, (said, s)
```

- [ ] **Step 2:** Run `C:/Users/camil/fspy/Scripts/python.exe -m pytest tests/unit/test_a_spoken_check_finds_its_checklist.py -q`. Expected: FAIL, score 0.5 and 0.4/None.
- [ ] **Step 3:** Add `"unit", "units", "lot", "lots", "today"` to `GENERIC`. Comment: "multi-unit walks name the unit in the opening (spec 2026-11-03)".
- [ ] **Step 4:** Re-run the test, plus `tests/unit/test_check_done_helpers.py` and `test_lambda_check_gaps.py`. Expected: PASS.
- [ ] **Step 5:** Commit `fix(inspection-match): unit/lot/today are not check-type words`.

### Task 2: Unit names and the multi-unit trigger (pure)

**Files:**
- Create: `src/units.py`
- Test: `tests/unit/test_a_walk_knows_its_units.py`

**Interfaces:**
- Produces:
  - `PREFIXES = ("unit", "lot", "room", "house", "townhouse", "apartment", "apt", "flat", "villa", "u")`
  - `normalize_unit(text: str) -> str | None`. Returns `"Unit 2"`, `"Lot 150"` or `"Room 102"`: the prefix in title case, then the number as digits. Number words up to "nine hundred ninety nine" are accepted, including "one fifty" → 150. `u` maps to `Unit`. Returns None when there is no prefix + number.
  - `opening_units(names: list[str], count: int | None) -> dict`. Returns `{"names": [normalized, deduped, in order], "count": int | None}`. A range named as "Lot 150 to 154" arrives from the model already expanded (Task 3 prompt). `names` wins over `count`.
  - `is_multi_unit(window: dict) -> bool`. True when `window["units"]["names"]` is non-empty, or `units["count"] >= 2`, or the `entered` marks name 2 or more distinct units.
  - `unit_stretches(window: dict) -> list[dict]`. Returns `[{"unit": str, "from": "HH:MM:SS", "to": "HH:MM:SS"}]`. Each entered mark opens its unit's stretch, and the stretch closes at the next mark or at the end of the window's segment. Time inside a segment but before the first mark is `{"unit": None}` (General).
  - `unit_at(stretches, hhmmss) -> str | None`.

- [ ] **Step 1: Write the failing tests.** Their names:
  - `test_spoken_forms_of_one_unit_are_one_unit`: "unit two", "Unit 2", "U2" and "unit-2" all give `"Unit 2"`; "lot one fifty" gives `"Lot 150"`.
  - `test_words_without_a_number_are_not_a_unit`: "the unit", "lot of studs" → None.
  - `test_one_room_mention_is_not_a_walk`: units empty, entered `[Room 102]` → False.
  - `test_two_entered_units_make_a_walk`.
  - `test_a_count_makes_a_walk`: count 5, no names → True.
  - `test_stretches_close_at_the_next_unit_and_at_a_segment_end`: two segments, three marks. Pin the exact list.
  - `test_before_the_first_unit_is_general`: `unit_at` returns None.
- [ ] **Step 2:** Run `...pytest tests/unit/test_a_walk_knows_its_units.py -q`. Expected: FAIL (no module).
- [ ] **Step 3:** Implement `src/units.py` with the signatures above. No DB access, no network.
- [ ] **Step 4:** Run it. Expected: PASS.
- [ ] **Step 5:** Commit `feat(units): unit names, stretches and the multi-unit trigger`.

### Task 3: The extraction reports the opening's units and each unit he enters

**Files:**
- Modify:
  - `src/lambda_extract_session.py`:
    - `EXTRACTION_SCHEMA` (the `inspections` entry, ~line 1253);
    - `clean_inspections` (~1057);
    - `time_inspections` (~1082);
    - the prompt text that explains `inspections`.
- Test:
  - `tests/unit/test_a_spoken_check_opens_its_window.py` (append);
  - `tests/unit/test_an_inspection_of_a_place_marks_the_place.py` (must stay green).

**Interfaces:**
- Consumes: `units.normalize_unit`, `units.opening_units`.
- Produces: each cleaned or timed inspection dict gains two keys:
  - `"units": {"names": [...], "count": int | None}`;
  - `"entered": [{"unit": str, "at": "HH:MM", "at_s": "HH:MM:SS" | absent, "quote": str}]`, at most 40 entries.

  `"entered"` holds only marks where he walks **into** a unit. A unit he refers to ("back to Unit 2, forgot...", "next door Unit 5 is the same") is not entered.

- [ ] **Step 1: Write the failing tests:**
  - `test_the_opening_units_are_kept_normalized`: the model gives names `["lot 150","Lot 151"]` → `{"names": ["Lot 150", "Lot 151"], "count": None}`.
  - `test_an_entered_mark_without_a_unit_is_dropped`.
  - `test_entered_marks_are_timed_to_the_second`: reuse the turns fixture style from the existing window tests; the mark's quote lies in the second turn → `at_s` is that turn's second.
  - `test_a_check_without_units_cleans_as_before`: the old dict plus `units {"names": [], "count": None}` and `entered []`.
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3: Extend the schema and the cleaners.**
  - Add `"units_named"`, `"unit_count"` and `"entered": [{"at", "unit", "quote"}]` to the schema entry.
  - Prompt sentence (our words): "units_named lists every unit the opening names, a range expanded (Lot 150 to 154 is Lot 150, Lot 151, ... Lot 154); unit_count is the number when he gives only a count. entered lists each moment he walks INTO a unit (Unit 2, Lot 167, Room 102), not a unit he only mentions."
  - Time `entered` quotes with the existing `_time_one_marker` helper (same stream and tolerance).
  - Fail open: an error in timing keeps the minutes.
- [ ] **Step 4:** Run the new tests and the whole `tests/unit/test_a_spoken_check_*` / `test_an_inspection_*` set. Expected: PASS.
- [ ] **Step 5:** Commit `feat(extract): inspections carry opening units and entered units`.

### Task 4: Storage — window units, walk keys, walk answers

**Files:**
- Create:
  - `src/migrations/0096_multi_unit_walks.sql`;
  - `src/repositories/walk_answers.py`.
- Modify: `src/repositories/inspection_windows.py`:
  - `replace_for_session`, `for_day` and `for_session` read and write the new columns;
  - new `open_walks(conn, company_id, user_folder, date, template_id) -> list[dict]`.
- Test: `tests/integration/test_multi_unit_walk_storage_sql.py`.

**Migration** (both `ADD COLUMN IF NOT EXISTS`, idempotent):

```sql
ALTER TABLE inspection_windows ADD COLUMN IF NOT EXISTS units jsonb;     -- {"names": [...], "count": n}
ALTER TABLE inspection_windows ADD COLUMN IF NOT EXISTS entered jsonb;   -- [{"unit","at","at_s","quote"}]
ALTER TABLE inspection_windows ADD COLUMN IF NOT EXISTS walk_key text;   -- NULL = single-unit check
ALTER TABLE checklist_reports ADD COLUMN IF NOT EXISTS walk_key text;
CREATE UNIQUE INDEX IF NOT EXISTS uq_checklist_reports_walk
  ON checklist_reports (walk_key) WHERE walk_key IS NOT NULL;
CREATE TABLE IF NOT EXISTS walk_answers (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id uuid NOT NULL REFERENCES companies(id),
  user_folder text NOT NULL, report_date date NOT NULL, walk_key text NOT NULL,
  unit text NOT NULL, item_text text NOT NULL,
  answer text CHECK (answer IN ('Yes','No','N/A')),          -- NULL when the row is a move
  comment text NOT NULL DEFAULT '', move_to_unit text,
  source text NOT NULL CHECK (source IN ('device','phone')),
  quote text NOT NULL DEFAULT '', voice_note_id uuid,
  answered_at text NOT NULL,                                 -- HH:MM:SS NZ, the day's clock
  answered_by uuid REFERENCES users(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (answer IS NOT NULL OR move_to_unit IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_walk_answers_walk ON walk_answers (walk_key, created_at);
```

**Interfaces:**
- Produces:
  - `walk_answers.add(conn, row: dict) -> str` (id);
  - `walk_answers.for_walk(conn, company_ids: list, walk_key: str) -> list[dict]`. The list is oldest first. An empty `company_ids` reads nothing.
  - `inspection_windows.open_walks(...)` returns the day's windows for that template that carry a `walk_key`, oldest first, with `walk_key, start_at, units, session`.
  - `for_day` and `for_session` also return `units`, `entered` and `walk_key`.

- [ ] **Step 1: Write the failing integration tests:**
  - `test_window_units_and_walk_key_round_trip`;
  - `test_walk_answers_read_only_within_the_company`;
  - `test_a_move_row_needs_no_answer_but_needs_a_target`: a row with both NULL raises.
- [ ] **Step 2:** Run `bash /c/Users/camil/fswork/run-tests.sh tests/integration/test_multi_unit_walk_storage_sql.py -q`. Expected: FAIL.
- [ ] **Step 3:** Write the migration and the repository functions.
- [ ] **Step 4:** Run the test, then `tests/integration/test_spoken_checks_are_kept_per_recording.py` and the migrations test, one run at a time. Expected: PASS.
- [ ] **Step 5:** Commit `feat(db): 0096 multi-unit walks and on-site answers`.

### Task 5: Walk identity across recordings

**Files:**
- Modify:
  - `src/units.py`: add `walk_key` and `assign_walk`;
  - `src/lambda_item_writer.py`: `_store_inspections` (~1872).
- Test:
  - `tests/unit/test_a_walk_knows_its_units.py` (append);
  - `tests/integration/test_spoken_checks_are_kept_per_recording.py` (append one case).

**Interfaces:**
- `units.walk_key(company_id, user_folder, date, template_id, start_at) -> str`. Returns `"walk:" + sha1 hex` of the five values joined by `|`.
- `units.assign_walk(window: dict, open_walks: list[dict], company_id, user_folder, date) -> str | None`. The rules:
  - no `template_id`, or the window is not multi-unit and has no opening → None;
  - the window has an opening (units named or a count) → a new key from its own `start_at`;
  - no opening, and there is an open walk for the same template earlier that day → that walk's key, the latest one;
  - an otherwise single-unit window with no open walk → None.
- `_store_inspections` sets `row["walk_key"]` before `replace_for_session`. It reads `open_walks` from **other** sessions (it excludes this session, which is about to be replaced).

- [ ] **Step 1: Write the failing tests:**
  - `test_a_later_recording_without_an_opening_continues_the_walk`;
  - `test_a_new_opening_starts_a_new_walk`;
  - `test_the_key_is_stable_across_re_extraction`: same inputs, same key;
  - integration: two sessions on one day give one `walk_key` on both rows.
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement. `_store_inspections` stays inside its savepoint, as today.
- [ ] **Step 4:** Run the unit test and the integration file. Expected: PASS.
- [ ] **Step 5:** Commit `feat(item-writer): a walk keeps one key across its recordings`.

### Task 6: Walk checklist — rows by unit, "all good", General, summary (pure)

**Files:**
- Create: `src/walk_checklist.py`
- Test: `tests/unit/test_a_walk_checklist_is_answered_per_unit.py`

**Interfaces:**
- Consumes:
  - `checklist.rebuild`, `checklist.ANSWERS`, `checklist.NOT_COVERED`, `checklist.evidence_found`;
  - `units.normalize_unit`, `units.unit_at`.
- Produces:
  - `MODEL_COLUMNS = ["Unit", "Item no", "Answer", "Comment", "Responsible", "Due", "Evidence"]`.
  - `shape_sentence() -> str`. It instructs the model:
    - one row per (unit, item) the recording addressed;
    - Unit is the unit the finding is ABOUT (a finding for "back to Unit 2" is Unit 2);
    - Item no `ALL` with Answer Yes marks "this one's all good" for that unit;
    - Unit `General` for findings that name no unit.
  - `rows_by_unit(text: str) -> dict[str, list[str]]`. Maps a normalized unit, or `"General"`, to its pipe rows, with the Unit cell removed so the rows are `checklist.MODEL_COLUMNS`-shaped. Rows whose unit does not normalize go to General.
  - `build(model_text, items, transcript_text, opening, stretches=None, report_date=None) -> dict`. Its parts:
    - `"units"`: `[{"unit", "paragraphs", "line_refs", "report", "flagged_unlisted": bool}]`, in walk order. The order is the opening names first, then units first seen in the rows in row order.
    - `"general"`: `[{"comment", "evidence"}]`.
    - `"summary"`: `{"columns": items, "rows": [[unit, cell...]]}`, where a cell is `Yes | No | N/A | Not checked | Check`.
  - In `build`:
    - every unit's rows go through `checklist.rebuild` with the same `transcript_text`;
    - an `ALL` row whose evidence survives fills that unit's unanswered items with Yes, Comment `covered by '<quote>' (HH:MM)`, and the evidence is the quote;
    - the HH:MM is the time of the transcript line holding the quote;
    - when `stretches` is given, an `ALL` quote spoken outside that unit's stretch is dropped, with reason `"all good outside the unit"` in the report;
    - a unit named in the opening with no rows still gets a section, with every item not checked;
    - when the opening has a count but no names, nothing is flagged.

- [ ] **Step 1: Write the failing tests.** Fixture: 3 items, opening `["Unit 1","Unit 2","Unit 3"]`, and a transcript with timestamped lines.
  - `test_silence_is_not_checked_in_every_unit`.
  - `test_all_good_answers_only_its_own_unit_and_says_so`: the Comment starts `covered by '`.
  - `test_all_good_does_not_overwrite_a_no`.
  - `test_a_finding_for_another_unit_goes_to_that_unit`: the row says Unit 2 while the stretch is Unit 3 → it lands in Unit 2.
  - `test_a_row_without_evidence_is_dropped_per_unit`.
  - `test_an_unlisted_unit_gets_a_flagged_section`.
  - `test_general_rows_are_kept_not_guessed`.
  - `test_the_summary_has_a_row_per_unit_and_a_cell_per_item`: pin a 3x3 grid exactly.
  - `test_all_good_outside_its_stretch_is_dropped`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement `walk_checklist.py`.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit `feat(walk-checklist): per-unit checklist with all-good, General and summary`.

### Task 7: On-site answers overlay — fill, conflict, move (pure)

**Files:**
- Create: `src/walk_answers_overlay.py`
- Test: `tests/unit/test_on_site_answers_meet_the_recording.py`

**Interfaces:**
- Consumes: the output of `walk_checklist.build` and the rows from `walk_answers.for_walk`.
- Produces: `apply(walk: dict, answers: list[dict], items: list[str]) -> dict`. It returns the walk with rows rewritten, and `walk["conflicts"] = [{"unit", "item", "on_site", "recording"}]`.
  - Answers match by `unit` and `item_text`. Item text is compared after whitespace and case folding, never by number. An answer whose item is no longer in the template is kept under `walk["unmatched_answers"]`.
  - Answers are applied oldest first. For the same unit and item, a later on-site answer replaces an earlier one.
  - **Fill:** the recording has the item as not checked → the on-site answer goes in. Its Comment is `"<comment> (on site, HH:MM)"`; it has no evidence ref, and its quote is in `report["evidence"]`.
  - **Agree:** the recording's answer equals the on-site answer → the recording row stays, with `(confirmed on site HH:MM)` appended.
  - **Conflict:** the answers differ → Answer is empty, and the Comment is `"On site: <a> (HH:MM). Recording: <answer> — '<recording quote>'. Inspector to choose."`. The summary cell is `Check`, and the conflict is appended to `conflicts`.
  - **Move:** `move_to_unit` is set. The source unit's No row moves to the target unit's same item.
    - The target's Yes or not-checked row is replaced. A target No is merged: comments joined with `; `.
    - The source item becomes not checked.
    - The comment gains `(moved from <source> on site, HH:MM)`.
    - A move whose source item is not a No is ignored and listed in `unmatched_answers`.
  - The summary is recomputed from the rows afterwards.

- [ ] **Step 1: Write the failing tests:**
  - `test_on_site_answer_fills_a_not_checked_item`;
  - `test_agreeing_answers_keep_the_recording_and_note_the_confirmation`;
  - `test_a_disagreement_keeps_both_and_flags_it`, which pins the Comment string;
  - `test_a_move_takes_the_no_to_the_named_unit_and_clears_the_source`;
  - `test_a_move_replaces_a_yes_in_the_target`;
  - `test_answers_survive_a_new_template_version_by_item_text`: the items are reordered and the answer still lands;
  - `test_a_later_answer_replaces_an_earlier_one`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit `feat(walk): on-site answers fill, flag conflicts and move findings`.

### Task 8: The report worker writes a walk report

**Files:**
- Modify: `src/lambda_session_report.py`, at the checklist step around line 1034 (`checklist.apply`) and the prompt construction above it.
- Create: `src/walk_report.py` (assembly only, pure).
- Test:
  - `tests/unit/test_a_walk_makes_one_report.py`;
  - the existing `tests/unit/test_a_spoken_check_makes_its_own_report.py` stays green.

**Interfaces:**
- Consumes:
  - the request body key `"walk"`: `{"key", "units": {...}, "entered": [...], "stretches": [...]}`, written by Task 9;
  - `walk_checklist.build`, `walk_answers_overlay.apply`, and `walk_answers.for_walk`.
- Produces: `walk_report.sections(template, main_prose, walk) -> list[dict]`. It returns `_prose_sections`-shaped dicts in this order:
  1. the template's non-checklist sections, from the main call;
  2. `General`;
  3. for each unit, a level-1 section titled `"<unit> — <checklist title>"`, with `flagged_unlisted` turned into a first paragraph: `"Not in the opening list — confirm this unit."`;
  4. `Summary`, a pipe table with Unit + one column per item;
  5. the catch-all.

  The returned list also carries `checklist_reports`-compatible per-unit reports, for provenance, keyed by the section title.

- **Behaviour when `body.get("walk")` is set:**
  - **Main call:** the template with its checklist sections removed (header, other sections and catch-all only).
  - **Walk call(s):**
    - The prompt is `walk_checklist.shape_sentence()` plus `checklist.items_block(items)` plus the transcript.
    - When the walk transcript has more than 6000 words, split it at unit stretch boundaries into parts of 6000 words or less. Call them in parallel, each with its own share of `_model_budget_seconds`, and concatenate the texts before `build`.
    - Each part keeps its lines' `[HH:MM:SS]` heads.
  - **Photos:** placed by `units.unit_at(stretches, photo_time)` into that unit's section; None → General.
  - Then `walk_answers_overlay.apply` runs with the stored answers. A read failure logs and proceeds without them, never failing the report.
- The stored result JSON gains `"walk": {"key", "units": [{"unit", "report"}], "summary", "conflicts", "unmatched_answers"}`. Task 12's GET reads this key.
- With no `walk` key, the code path is unchanged.

- [ ] **Step 1: Write the failing tests:**
  - `test_a_walk_report_has_one_header_general_units_and_summary`: inject a fake `call_llm` that answers the main and walk prompts by marker; assert the titles in order;
  - `test_a_long_walk_is_split_at_unit_boundaries`: the fake records the prompts, and no part crosses mid-stretch;
  - `test_a_photo_goes_to_the_unit_current_at_its_time`;
  - `test_stored_answers_are_applied_and_a_read_failure_is_not_fatal`;
  - `test_no_walk_key_is_todays_report`: the snapshot of prose titles is unchanged.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement `walk_report.py`, then branch in `lambda_session_report` on `body.get("walk")`. Do not reorder any existing code on the non-walk path.
- [ ] **Step 4:** Run the new tests, the existing checklist report tests, and `tests/unit/test_a_checklist_*`. Expected: PASS.
- [ ] **Step 5:** Commit `feat(report): one walk, one report with a section per unit`.

### Task 9: Auto-generation queues one report per walk

**Files:**
- Modify:
  - `src/checklist_reports.py`: `auto_generate`, `already_made`, `_record`, `request_body`;
  - `src/lambda_org_api.py`, around line 5215: accept and pass through `body["walk"]`, validated: `key` matches `^walk:[0-9a-f]{40}$`; stretches are HH:MM:SS; at most 40 units.
- Test:
  - `tests/unit/test_a_spoken_check_makes_its_own_report.py` (append);
  - `tests/integration/test_multi_unit_walk_storage_sql.py` (append).

**Interfaces:**
- Consumes: `inspection_windows.for_day` (now with `walk_key`, `units` and `entered`), `units.unit_stretches`.
- Produces:
  - For a walk, the request body has `segments` equal to every window's segments across the walk's sessions, in time order. It also gets `walk = {"key", "units", "entered", "stretches"}`.
  - `checklist_reports` keeps one row per walk, `ON CONFLICT (walk_key) WHERE walk_key IS NOT NULL DO UPDATE`.
  - `already_made` for a walk compares the stored `segments` with the walk's full segments, so a second recording regenerates the report.
- Windows with `walk_key` NULL take today's path, unchanged.

- [ ] **Step 1: Write the failing tests:**
  - `test_a_walk_over_two_recordings_is_queued_once_with_both_stretches`;
  - `test_a_second_recording_regenerates_the_walk_report`;
  - `test_a_single_unit_check_is_queued_exactly_as_before`: the body dict equals today's;
  - org-api: `test_a_malformed_walk_key_is_refused_400`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
  - `auto_generate` runs from the final extraction of one session. For a walk, it reads `for_day` to collect all of the walk's windows.
- [ ] **Step 4:** Run the unit and integration tests, one integration run at a time. Expected: PASS.
- [ ] **Step 5:** Commit `feat(checklist-reports): one auto report per walk across recordings`.

### Task 10: The final check covers every unit of the walk

**Files:**
- Modify:
  - `src/check_done.py`: `_answer`, `_prompt`, `say_ok`, and a new `walk_questions`;
  - `src/lambda_check_gaps_db.py`: return `walk` facts.
- Test:
  - `tests/unit/test_check_done_run.py` (append);
  - `tests/unit/test_check_gaps_db.py` (append).

**Interfaces:**
- **DB hop:** when the live window has a `walk_key`, `facts["walk"] = {"key", "units", "entered", "sessions": [session ids of the walk today, oldest first], "answers": walk_answers.for_walk(...)}`. The db hop uses the caller's company ids, as for `for_session`.
- **`check_done._answer`:**
  - When `facts.get("walk")` is set, stored transcripts are gathered for **every** walk session. Only the current session's chunks are heard.
  - The gap pass prompt uses `walk_checklist.shape_sentence()`. Its result goes through `walk_checklist.build` (with no stretches, since live marks lag), then through `walk_answers_overlay.apply` with `facts["walk"]["answers"]`.
- **`walk_questions(walk_result, items) -> list[dict]`:** returns `[{"kind": "not_checked" | "confirm_no", "unit", "item_text", "comment"}]`, in walk order:
  - each unit's not-checked items come first, then its No items;
  - items under conflict are `"confirm_no"` when the recording side is No, otherwise `"not_checked"`.
- **New response keys** (existing keys unchanged):
  - `"walk": {"key", "units": [{"unit", "not_covered": [...], "failed": [...]}]}`;
  - `"questions": [...]`;
  - `"mode": "device" if len(questions) <= 3 else "phone"`.
- **Say line (our words):**
  - Device mode: the first question, with item text and no numbers. Not checked: `"Unit 3: wall bracing — pass?"`. A No: `"Unit 3, wall bracing failed, east side two short — right?"`.
  - Phone mode: `"<n> items open across <k> units — the list is on your phone."`.
- **Budget:**
  - The walk sessions' stored transcripts are assembled inside the existing read phase.
  - With more than 5 walk sessions, assemble the newest 5 and set `partial = True`.

- [ ] **Step 1: Write the failing tests:**
  - `test_final_check_lists_each_units_open_items_by_text`;
  - `test_three_or_fewer_questions_stay_on_the_device`;
  - `test_more_than_three_go_to_the_phone`;
  - `test_a_no_is_read_back_for_confirmation`;
  - `test_answers_already_given_are_not_asked_again`;
  - `test_a_walk_over_two_recordings_reads_both`: the fake `gather` records the session bases;
  - `test_single_unit_check_done_is_unchanged`: today's response dict, with no new keys.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `tests/unit/test_check_done_*.py tests/unit/test_lambda_check_gaps.py tests/unit/test_check_gaps_db.py`. Expected: PASS.
- [ ] **Step 5:** Commit `feat(check-done): the final check covers every unit of a walk`.

### Task 11: Confirmation answers from the device

**Files:**
- Create: `src/walk_confirm.py` (pure parse, plus `handle` with injected deps).
- Modify: `src/lambda_check_gaps.py`, so that `event["action"] == "confirm"` routes to `walk_confirm.handle`.
- Modify: `template.yaml`, only if the check-gaps function needs a new permission (it already has DB hop access; verify, don't assume).
- Test: `tests/unit/test_a_confirmation_answer_is_kept_as_evidence.py`.

**Interfaces:**
- **Request** (device → Ask line → check-gaps): `{"action": "confirm", "caller_sub", "walk_key", "unit", "item_text", "kind": "not_checked" | "confirm_no", "audio_b64" | "text", "pressed_at"}`.
- **`parse_answer(text: str, kind: str, units: list[str]) -> dict | None`**. The rules apply in this order:
  1. a unit named after "no"/"wrong"/"that was"/"that's", e.g. "no, that was Unit 2" → `{"move_to": "Unit 2"}` (only for `confirm_no`, and only for a unit in `units`);
  2. it starts with pass / yes / good / ok / right / correct → `{"answer": "Yes"}`, or for `confirm_no`, "right/correct/yes" → `{"answer": "No", "confirmed": True}`;
  3. it starts with fail / no / failed → `{"answer": "No", "comment": the rest}`;
  4. "not applicable / n a / na / doesn't apply" → `{"answer": "N/A"}`;
  5. anything else → None (skip).
- **`handle(event, *, stt, db_write, now) -> dict`:**
  - Transcribe with `stt` (the same Ask-key STT as check_done).
  - Parse, then write one `walk_answers` row through `db_write`, with `source='device'`, `quote` = the transcript, and `answered_at` = NZ HH:MM:SS.
  - Return `{"status": "saved" | "skipped", "say": str, "next": None}`.
  - Say lines: `"Got it."`, or for a move `"Moved to Unit 2."`, or for a skip `"Skipping this one — it's on your phone."`.
  - The device keeps the question list and asks the next question itself.
- The check-gaps function must never call general Ask; a test asserts no Ask import is used.

- [ ] **Step 1: Write the failing tests:**
  - one parse case per rule, plus `"what's the weather"` → None;
  - `test_a_move_names_only_a_unit_of_the_walk`;
  - `test_the_transcript_is_stored_as_the_quote`;
  - `test_a_skip_writes_nothing`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit `feat(check-gaps): confirmation answers, moves and skips`.

### Task 12: Phone routes — open items and answers

**Files:**
- Modify:
  - `src/lambda_org_api.py`: two routes next to the inspections route (~5187);
  - `template.yaml`: the API events for the two routes, following the existing inspections route. Read [[fieldsight-org-api-new-route-iam-trap]] first.
- Test:
  - `tests/unit/test_phone_settles_a_walk.py`;
  - `tests/integration/test_multi_unit_walk_storage_sql.py` (append).

**Interfaces:**
- `GET /api/inspections/walks/{walkKey}/open?user=<folder>`:
  - authorization exactly as the existing `inspections` route (`_resolve_org_media_folder`, what="inspections"); read it and copy its checks, never fewer;
  - returns `{"walkKey", "units": [{"unit", "items": [{"item_text", "kind", "comment"}]}]}`;
  - it is computed from the **latest walk report result** for that key (`checklist_reports.result_key` → its stored per-unit reports), overlaid with the answers;
  - before any report exists, it returns 404 `{"error": "not ready"}`, and the device's final-check list is what the phone shows (contract, Task 13).
- `POST /api/inspections/walks/{walkKey}/answers`:
  - body `{"unit", "item_text", "answer": "Yes" | "No" | "N/A", "voice_note_id"?, "move_to_unit"?}`;
  - writes `walk_answers` with `source='phone'` and `answered_by` = the caller;
  - validates that the unit belongs to the walk and that the item_text is in the template.
- [ ] **Step 1: Write the failing tests:**
  - `test_open_items_need_the_same_access_as_the_inspections_route`: another company's caller → 403/404, as that route returns;
  - `test_an_answer_for_a_unit_outside_the_walk_is_refused`;
  - `test_a_phone_answer_is_stored_with_the_caller`;
  - `test_open_items_drop_what_was_answered`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the unit tests, then the integration tests (one run). Expected: PASS.
- [ ] **Step 5:** Commit `feat(org-api): phone routes to settle a walk on site`.

### Task 13: Cross-line contract and release

**Files:**
- Create: `docs/contracts/2026-11-03-multi-unit-final-check.md`.

The contract document gives each of the other lines the exact request and response shapes from Tasks 10-12, plus who owns what (spec "Ownership"):
- **Ask line:** route a `check_done` press as today. While the device is in the confirming state, forward its press as `action: "confirm"`, never to general Ask.
- **Device line:** the confirming state:
  - entered when the response has `mode: "device"`;
  - exits when the list is done, on a long-press, or after 30 s idle;
  - on exit, the remaining questions go to the phone.
  - It speaks the TTS of `say` and of each question, and records push-to-talk answers.
- **Phone line:** the list from the `questions` of the final-check response (pushed, or fetched with the GET in Task 12), Pass / Fail / N/A buttons, a hold-to-record voice note (migration 0089 voice notes → `voice_note_id`), and "move to another unit" on a No.

- [ ] **Step 1:** Write the contract document. Copy each JSON shape from the tests of Tasks 10-12; don't re-derive them.
- [ ] **Step 2:** Run the full unit suite, then the integration files touched, one at a time. Expected: PASS. Compare with the develop baseline before calling anything pre-existing.
- [ ] **Step 3:** Commit `docs(contract): multi-unit final check for the Ask, device and phone lines`. Open the PR to develop, then merge it on green after checking that the base is develop.
- [ ] **Step 4:** On TEST, record a real 3-unit walk. Measure:
  - the final-check ms against the 30 s budget;
  - the report's per-unit correctness.

  Report both to the owner. Show the owner the summary table (spec: shown before it is final).
