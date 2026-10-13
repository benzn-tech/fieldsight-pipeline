# Self-introduction → name suggestion — implementation plan (backend)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Every task is test-first, and every task ends with the **revert-to-red check**: put the production change back the way it was and confirm the new test fails before moving on (CLAUDE.md, "Replay the actual defect").

**Goal:** When somebody on a recording says "Hi, this is Petros from Cassidy", the bell offers that voice as Petros. A person confirms or rejects; nothing is named automatically.

**Spec:** `docs/superpowers/specs/2026-09-29-self-introduction-name-suggestions-design.md`. This plan corrects the spec where the code contradicts it (see §Spec corrections) and the corrections win.

**Tech stack:** Python 3.12 Lambdas, psycopg3 + Aurora Postgres, SAM template `src/template.yaml`. **No new IAM, no new S3 prefix, no new trigger** — the detector runs in `lambda_extract_session`, the result rides the existing extraction artifact, `lambda_item_writer` writes rows over the connection it already holds, and org-api's confirm delegates to `speaker_corrections`, which already writes `voiceprint_requests/`.

---

## Spec corrections (read before Task 1)

Verified against the worktree at `56dcb03` (origin/develop, 2026-09-29).

1. **Normalised turns say `speaker`, not `speaker_label`.** `transcript_utils._build_turn` (`src/transcript_utils.py:385-394`) emits `{speaker, text, start_sec, end_sec, abs_start, abs_end, ...}`; `assemble_session_turns` adds `source_filename` (`src/lambda_extract_session.py:1381`). The artifact renames it to `speaker_label` on the way out (`:2014-2018`) and the comment at `:2006-2010` records that reading `speaker_label` here once produced an empty list on every session, silently. `self_introduction.find` therefore reads `t["speaker"]` and emits `speaker_label`; Task 1's test feeds it `normalize_transcript` output, not hand-built dicts.
2. **The writer's insert must live INSIDE the connection block.** `_request_rebind`/`_request_match` are called at `src/lambda_item_writer.py:1265/1275`, *after* `with get_connection() as conn:` (`:856`) has closed — psycopg3's `with conn:` closes the connection (`:1182-1188` explains the crash that placement caused once). A DB insert "on the same route as speaker_turns" would raise on every run. The insert goes beside `location_markers.replace_for_day` (`:1044-1049`), best-effort, inside the block.
3. **Confirm needs a `sid`, not only a date.** `speaker_corrections` refuses a session id without `sid<32hex>` (`src/lambda_org_api.py:2691-2693`) as well as one without a date (`:2683-2685`). Legacy whole-file / VAD recordings (`Benl1_2026-07-06_10-00-00_off0.0_to60.0_srcwav.json`) carry no sid, so their suggestions could never be confirmed. The writer skips an introduction whose `source_filename` gives `turn_name_overlay.session_base(...) is None`.
4. **The "already named" rule is per FILE, not per cluster.** `speaker_turn_names` has no `speaker_label`; the candidate query's clause (`src/repositories/label_group_candidates.py:195-204`) compares `split_part(turn_ref,'@',1)` with the `.json`-stripped filename and `superseded_at IS NULL`. Reused verbatim, it suppresses every cluster in a file where anyone was named. Accepted: an introduction in a file somebody has already worked on is the low-value case.
5. **3 s is enough to propagate, not to enrol.** The homogeneity guard refuses windows under ~10 s (`lambda_org_api.py:2798-2803`; PRs #601-603). Spec §1's "turns shorter than 3 s are skipped (too short for the enrolment)" conflates the two. Kept at 3 s for *detection*; the confirm path marks the **longest turn of the same (file, label) cluster** exactly as `_apply_confirmed_proposal` does (`:2553-2563`), so a 4 s "Hi, I'm Petros" followed by a 40 s explanation enrols on the 40 s. This needs the transcript read (`_session_turns`) at confirm time only — never on the badge or the list.
6. **Migration number is `0071`** (latest on origin/develop is `0070_one_sample_per_rounded_window.sql`). Re-check at merge.
7. **Regex false positives on site speech** (spec §1 rules, judged against how the ASR capitalises): "Hi, this is Level 2 east", "Morning, this is Block C", "Hello, this is Cassidy" (a company), "I'm Gib fixing" and Mandarin "我叫他过来" all pass the spec's rules. Task 1 adds a **place/thing stop list** on the first token of X (Level, Block, Site, Stage, Grid, Zone, Room, Unit, Team, Bay, Tower, days of the week, "Recording"), rejects an X followed by a digit, requires the Han X not to start with a pronoun (他/她/你/它/我/大家), and "I'm here" is a stop phrase, not a context (the spec lists it as both). Precision is measured in Task 1 Step 4 on real transcripts before anything ships.
8. **The Mandarin "spaced characters" claim is unverified.** Stripping spaces is harmless either way; the test carries both spellings.

**Unchanged from spec, confirmed against code:** all routes are 404 while `SPEAKER_IDENTITY_MODE == "off"` (every neighbour at `lambda_org_api.py:2415,2498,2603` does this); roles = `_CORRECTION_ROLES` (`:1899`); company from `caller["company_id"]` only; the confirm runs inside `conn.transaction()` with `_ConfirmationNotApplied` (`:2515-2531`); `start_sec`/`end_sec` are in-file offsets, which is the coordinate `speaker_corrections` validates against `_off{T}_to{E}_` (`:2661-2677`); the audio key for Listen is `audio_segments/{folder}/{date}/{stem}.wav` where `stem` = `source_filename` without `.json` (`turn_name_overlay._stem`).

## Global constraints

- **Plain words only, no scores** on any customer-facing field.
- **The detector is pure** (`src/self_introduction.py`): no I/O, no logging of transcript text.
- **Never fatal.** A detector exception must not fail an extraction; a store exception must not fail an item write (mirror `location_markers` at `lambda_item_writer.py:1044-1049`).
- **`ON CONFLICT DO NOTHING`**, never `WHERE NOT EXISTS` in Python: the re-run rule lives in the constraint (`speaker_name_proposals.propose` docstring).
- **Real SQL runs against a real database before merge** (CLAUDE.md Testing). Every repository function gets an `tests/integration/` case using the `db` fixture in `tests/conftest.py`, and the same statements are run once by hand through the RDS Data API in a rolled-back transaction on `fieldsight_test`.
- **Count the stubs.** For every function monkeypatched in a unit test, one test somewhere calls it for real.

---

### Task 1: The detector — `src/self_introduction.py`

**Files:**
- Create: `src/self_introduction.py`
- Test: `tests/unit/test_self_introduction_patterns.py`
- Script (measurement, not shipped): `scripts/labelling/intro_precision.py`

**Interface:**
```python
def find(turns, min_turn_sec=3.0) -> list[dict]
# turns: assemble_session_turns output (keys: speaker, text, start_sec, end_sec, source_filename)
# each result: {"source_filename", "speaker_label", "start_sec", "end_sec",
#               "heard_name", "company_name" (or None), "quote"}   # quote = the matched sentence, <= 200 chars
```
One result per `(source_filename, speaker_label)`: the first hit wins, so a person who introduces themselves twice in one call is asked once.

- [ ] **Step 1 — the contract test first.** In the test module build turns with `transcript_utils.normalize_transcript` (an Transcribe-shaped `items` payload with `speaker_label` per word) plus `source_filename`, exactly as `assemble_session_turns` would, and assert `find()` returns `speaker_label == "spk_0"`. This is the test that goes red if anyone reads `t["speaker_label"]` (correction 1). Run: red (module missing).
- [ ] **Step 2 — positives.** "Hi, this is Petros from Cassidy" → `heard_name="Petros"`, `company_name="Cassidy"`; "my name is Sam Yu" ; "my name's Ben" ; "I'm Petros, from Cassidy" ; "I am Petros with Fletcher" ; "let me introduce myself, I'm Petros Pan" ; "G'day, this is Dave" ; Mandarin "我叫王小明" , "我 叫 王 小 明" (spaced), "我的名字是李雷" , "我是张伟，来自中建".
- [ ] **Step 3 — negatives (each named in the test name).** "This is Benny from Performance" (no greeting → not first person); "I'm going to check the slab"; "I'm sure it's fine"; "I'm here with Dave"; "Hi, this is Level 2 east"; "Morning, this is Block C"; "Hello, this is Cassidy Construction" (allowed to pass or fail — see Decision 3; the test pins whichever the owner picks); "I'm Gib fixing all day"; "我是说这个不行"; "我叫他过来"; a 2.5 s turn "Hi I'm Petros" (below `min_turn_sec`); the same speaker introducing twice in one file → one result.
- [ ] **Step 4 — implement** with compiled regexes, a `_STOP_FIRST_TOKENS` set and a `_STOP_PHRASES` set (`going, sure, here, sorry, done, not, just, off, back, good, fine, on, in, at`). Han names: 2–4 chars, not starting with a pronoun, cut at `，。来自 从 的 是`. Latin names: 1–3 tokens matching `[A-Z][a-z'’-]+`, none in the stop set, not followed by a digit. Spaces between single Han characters are removed before matching (both spellings in the test).
- [ ] **Step 5 — measure precision on real transcripts** with `scripts/labelling/intro_precision.py`: download the last 30 days of `transcripts/` for two companies from **TEST** (read-only; `--profile fieldsight-deployer`), run `find()` over `assemble_session_turns`, print every hit with its quote. Hand-label them. **Ship only if precision ≥ 0.8**; otherwise tighten in Step 4 and re-run. Paste the numbers into this plan's §Findings. A rule with zero hits on 30 days is also a finding — say so.
- [ ] **Revert-to-red:** `git stash push -u -m intro-task1 -- src/self_introduction.py` is NOT allowed (shared stash stack); instead rename the module temporarily and confirm the whole test file fails on import, then restore.

---

### Task 2: The artifact carries `self_introductions` (final pass only)

**Files:**
- Modify: `src/lambda_extract_session.py` (the extraction dict, beside `'speaker_turns'` at ~`:2014`)
- Test: `tests/unit/test_self_introductions_ride_the_artifact.py`

- [ ] **Step 1 — test first.** Drive `extract_session` the way `tests/unit/test_confirmed_names_reach_the_prompt.py` does (fake S3 with two transcript objects, fake LLM), where one turn says "Hi, this is Petros from Cassidy" and lasts 5 s. Assert the written artifact has `self_introductions == [{... "speaker_label": "spk_0", "heard_name": "Petros", ...}]` when `final=True` and `self_introductions == []` when `final=False`. Assert the artifact's `speaker_turns` and `self_introductions` agree on `(source_filename, speaker_label)` spelling — the seam `test_anonymous_speaker_rebind.py:77` guards for the other field.
- [ ] **Step 2 — implement.** `'self_introductions': (self_introduction.find(turns) if final else [])`, wrapped so an exception logs (`logger.exception`) and yields `[]`. Add a one-line comment pointing at correction 1.
- [ ] **Step 3 — never-fatal test:** monkeypatch `self_introduction.find` to raise; the extraction is still written with `self_introductions == []`.
- [ ] **Revert-to-red:** remove the dict entry, run the test file, confirm red, restore.

---

### Task 3: Migration `0071_speaker_intro_suggestions.sql`

**Files:**
- Create: `src/migrations/0071_speaker_intro_suggestions.sql`
- Test: `tests/integration/test_speaker_intro_suggestions_sql.py` (uses the `db` fixture; skips without `TEST_DATABASE_URL`); also `tests/integration/test_migrations_apply.py` picks it up automatically.

- [ ] **Step 1 — test first.** Insert a company, then a suggestion row; insert the same `(company_id, session_base, source_filename, speaker_label)` again with `ON CONFLICT DO NOTHING RETURNING id` → second returns no row. `state='maybe'` violates the CHECK. `decided_by` must reference `users(id)`.
- [ ] **Step 2 — write the migration**, header in the house style (why the table exists, why it is not `speaker_name_proposals`: that table asks "is this <existing person>?" and needs a `voiceprint_id NOT NULL`; this one asks "who is this new voice?" and has none):
  ```sql
  CREATE TABLE IF NOT EXISTS speaker_intro_suggestions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    session_base text NOT NULL, source_filename text NOT NULL, speaker_label text NOT NULL,
    user_folder text NOT NULL, session_date date NOT NULL,
    start_sec double precision NOT NULL, end_sec double precision NOT NULL,
    heard_name text NOT NULL, company_name text, quote text NOT NULL,
    state text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','confirmed','rejected')),
    decided_by uuid REFERENCES users(id), decided_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (company_id, session_base, source_filename, speaker_label)
  );
  CREATE INDEX IF NOT EXISTS speaker_intro_suggestions_pending
    ON speaker_intro_suggestions (company_id, created_at DESC) WHERE state = 'pending';
  ```
- [ ] **Step 3 — run it for real** on `fieldsight_test` through the RDS Data API inside `begin-transaction` … `rollback-transaction` (CLAUDE.md Testing; `MSYS_NO_PATHCONV=1`). Paste the transaction id and the two statements' outcomes into §Findings. **This is a TEST-database read/rollback only; nothing is committed and prod is not touched.**
- [ ] **Revert-to-red:** delete the file; the integration test errors on a missing relation.

---

### Task 4: Repository — `src/repositories/speaker_intro_suggestions.py`

**Files:**
- Create: `src/repositories/speaker_intro_suggestions.py`
- Tests: `tests/unit/test_speaker_intro_suggestions_repo.py` (SQL-text assertions through a recording double) and the integration file from Task 3 (the same statements, for real).

**Interface:**
```python
store(conn, company_id, session_base, user_folder, session_date, intros) -> {"inserted": n, "skipped_named": n, "skipped_no_sid": n}
pending(conn, company_id, limit=20) -> list[dict]      # newest first; NO transcript read
pending_count(conn, company_id) -> int
decide(conn, company_id, suggestion_id, state, decided_by) -> dict | None   # WHERE state='pending', RETURNING the row
```

- [ ] **Step 1 — tests first (integration).** `store` inserts one row per intro; a second `store` inserts zero; an intro whose file already has a live `speaker_turn_names` row (insert one with `turn_ref = '<stem>@12.0'`, `superseded_at IS NULL`) is skipped and counted in `skipped_named`; a superseded row does not block; an intro whose `source_filename` has no `sid` is skipped and counted in `skipped_no_sid`. `decide` flips exactly once (second call returns None). `pending` returns the fields the API needs and orders newest first. Company scoping: rows of another company are invisible to `pending`, `pending_count`, `decide`.
- [ ] **Step 2 — unit SQL-text tests** (mirror `tests/unit/test_candidate_sql_is_shaped_the_way_postgres_needs.py`): the exclusion uses `split_part(n.turn_ref, '@', 1) = regexp_replace(%s, '[.]json$', '')` and `superseded_at IS NULL`; `store` raises `ValueError` without `company_id` (`_require_company`, same as its two neighbours).
- [ ] **Step 3 — implement.** The "already named" check is one `SELECT EXISTS` per distinct file (an artifact holds a handful), executed before the insert; `ON CONFLICT (company_id, session_base, source_filename, speaker_label) DO NOTHING RETURNING id`. `turn_name_overlay.session_base(source_filename)` gates the sid.
- [ ] **Revert-to-red:** drop the `NOT EXISTS`/`EXISTS` clause → the "skipped_named" integration test fails; drop the `state='pending'` predicate → the double-decide test fails.

---

### Task 5: The writer stores suggestions inside its connection block

**Files:**
- Modify: `src/lambda_item_writer.py` (beside `location_markers.replace_for_day`, ~`:1044-1049`)
- Tests: `tests/unit/test_the_writer_stores_introductions.py` (FakeConn/FakeS3 from `tests/unit/test_lambda_item_writer.py`), plus a seam test in `tests/unit/test_self_introductions_ride_the_artifact.py` (Task 2) that hands the artifact `extract_session` wrote to `write_extraction_items`.

- [ ] **Step 1 — tests first.** (a) An artifact with `self_introductions` → `speaker_intro_suggestions.store` is called with `(company_id, session_base, user_folder, date, intros)` **while the connection is open** (the double records `closed=True` on `__exit__`; assert the call arrived before it). (b) Missing field / `[]` → not called. (c) `store` raising → topics are still written and the handler returns normally (`logger.exception` seen in caplog). (d) Seam: Task 2's artifact → the writer's call carries the same `(source_filename, speaker_label)` pairs. (e) Stub count: `store` is monkeypatched here and called for real in Task 4's integration test — name that file in a comment.
- [ ] **Step 2 — implement**, in a `try/except Exception: logger.exception(...)` block, gated on `extraction.get("tier") == "final"` (belt and braces — live artifacts carry `[]`) and logging the returned counts (`inserted / skipped_named / skipped_no_sid`) at INFO so "did it run" is answerable from the log.
- [ ] **Step 3 — do NOT gate on `SPEAKER_IDENTITY_MODE` in the writer** unless Decision 1 says otherwise; the rows hold no biometric data and the confirm is gated at org-api. Record the choice in the code comment.
- [ ] **Revert-to-red:** comment out the `store` call → tests (a) and (d) fail; move it below the `with` block → test (a)'s "before close" assertion fails.

---

### Task 6: org-api — badge count, list, decide

**Files:**
- Modify: `src/lambda_org_api.py` — route table (`~:775-782`), `list_name_proposals` (`:2397`), new `list_name_suggestions`, `decide_name_suggestion`, `_apply_confirmed_suggestion`
- Tests: `tests/unit/test_the_bell_counts_introductions.py`, `tests/unit/test_a_confirmed_introduction_is_a_human_correction.py` (modelled on `tests/unit/test_a_confirmed_proposal_is_a_human_correction.py`, including its `FakeConn.outcomes` transaction probe)

- [ ] **Step 1 — badge test first.** `GET /name-proposals` without `?voiceprint=` returns `introductions: 3` when `speaker_intro_suggestions.pending_count` returns 3; the existing `people`/`total` are unchanged; **no transcript read happens** (assert `_read_org_transcripts` is not called — the countless shape must stay countless, `test_the_bell_can_ask_who_is_waiting.py`'s reason).
- [ ] **Step 2 — list test.** `GET /name-suggestions` → `{"suggestions": [{id, heardName, companyName, quote, date, userFolder, sessionBase, sourceFilename, speakerLabel, startSec, endSec, createdAt}]}` straight off the rows, no transcript read, no score field anywhere in the body. 404 when `SPEAKER_IDENTITY_MODE == "off"`; 403 for a `worker`.
- [ ] **Step 3 — decide tests.** `POST /name-suggestions/{id}` body `{"decision": "confirmed", "display_name": "Petros Pan"}`:
  - queues **one** `voiceprint_requests/` artifact whose `correction.display_name == "Petros Pan"` (edited name wins) and `correction.source_filename == row.source_filename`;
  - with `display_name` absent, `heard_name` is used;
  - the window marked is the **longest turn of the same (file, label) cluster** from `_session_turns` (correction 5); when `_session_turns` returns nothing for that cluster, fall back to the row's own `start_sec/end_sec` (the intro is at least 3 s, propagation still works) — test both;
  - `rejected` → 200, nothing queued;
  - `"dismissed"`, `""` → 400, nothing queued;
  - a refused correction (monkeypatch `speaker_corrections` → 400) → response 400 and `FakeConn.outcomes == ["rollback"]`;
  - the session id passed to `speaker_corrections` is the row's `source_filename` (carries date + sid);
  - a row of another company → 404 (`decide` returns None).
- [ ] **Step 4 — implement.** Copy the shape of `decide_name_proposal` + `_apply_confirmed_proposal`, reusing `_ConfirmationNotApplied`. `display_name` is `.strip()`-ed and refused when empty after strip with a 400 that says so. Add the two routes next to `/name-proposals`. Extend `list_name_proposals`'s countless branch with `"introductions": speaker_intro_suggestions.pending_count(conn, company_id)`.
- [ ] **Step 5 — writer seam** (the test the proposals file calls "the assertion this whole file exists for"): replay the queued artifact into `lambda_voiceprint_writer.lambda_handler` with `asserted: True` and assert `source == "correction"` reaches `record_turn_name`. Same reason: a confirmation that lands as propagation calibrates nothing.
- [ ] **Revert-to-red:** remove the `with conn.transaction()` wrapper → the rollback test fails; hard-code `heard_name` → the edited-name test fails.

---

### Task 7: No template change — prove it, and wire the counts into the log

**Files:**
- Read only: `src/template.yaml` (`:2193` org-api env, `:3558-3563` item-writer env)
- Test: `tests/unit/test_intro_suggestions_need_no_new_grant.py`

- [ ] **Step 1** — a unit test that imports the three modules and asserts `speaker_intro_suggestions.py` and `self_introduction.py` contain no `boto3`/`s3` reference (the feature is DB-only on the writer side and reuses `speaker_corrections` on the org-api side). This is the guard against a later "just read the transcript from the writer" that would need the grant `lambda_item_writer.py:1029-1035` says the role does not have.
- [ ] **Step 2** — grep `template.yaml` for `SpeakerIdentityMode` on `OrgApiFunction`: already present (`:2193`); nothing to add. Note it in the PR description.

---

### Task 8: TEST verification (after merge to develop; owner or operator)

- [ ] Deploy to TEST; record a session in which the wearer says "Hi, I'm <name> from <company>" for at least 4 s; finalize.
- [ ] `aws logs tail /aws/lambda/fieldsight-test-item-writer` shows `intro suggestions: inserted=1 skipped_named=0 skipped_no_sid=0` for that session (**a missing line means the code did not run, not that nothing was heard** — CLAUDE.md "Lambda INFO logs").
- [ ] `GET /api/org/name-proposals` on TEST returns `introductions: 1`; `GET /api/org/name-suggestions` shows the quote.
- [ ] `POST /api/org/name-suggestions/{id}` `{decision: confirmed}` → 202; the transcript viewer shows the name on the cluster; `speaker_turn_names` has a `source='correction'` row for it (`aws rds-data execute-statement --database fieldsight_test`).
- [ ] Re-run the final extraction for the session (org-api regenerate) → the writer logs `inserted=0` and the bell count does not grow.
- [ ] Paste the numbers into §Findings before the prod deploy.

---

## Frontend (fieldsight-ui, `dev` branch — separate repo, not this plan's code)

API contract the backend provides (all under `/api/org`, Cognito-authorised, roles `admin|gm|pm|site_manager|platform_admin`, 404 when speaker identity is off):

| Call | Response |
|---|---|
| `GET /name-proposals` | existing `{people, total}` **plus** `introductions: <int>` |
| `GET /name-suggestions` | `{"suggestions": [{"id", "heardName", "companyName"\|null, "quote", "date" ("YYYY-MM-DD"), "userFolder", "sessionBase", "sourceFilename", "speakerLabel", "startSec", "endSec", "createdAt"}]}` newest first, at most 20 |
| `POST /name-suggestions/{id}` | body `{"decision": "confirmed"\|"rejected", "display_name"?: string}` → `confirmed`: 202 with `speaker_corrections`' body (`propagation`, `enrolment`, `enrolmentMayBeRefused`, `linkedTo`, ...); `rejected`: 200 `{"suggestionId", "decision": "rejected"}`; 400 for any other decision or an empty `display_name`; 404 when already decided or not this company's |

Work items:
1. Bell: add a "New voices" row per suggestion — "Someone introduced themselves as *Petros* — save their voice?" — driven by `introductions` in the badge poll and `GET /name-suggestions` on open. **Do not** add the suggestion count into the existing `total` client-side arithmetic without checking the badge component reads `total` (it does).
2. Dialog: quote, **Listen** using the same key rule as the proposals dialog (`audio_segments/{userFolder}/{date}/{stem}.wav`, `stem = sourceFilename` minus `.json`, seek to `startSec`), name field prefilled with the closest roster name (`GET /members` display names, case-insensitive prefix/edit-distance ≤ 2) and "heard as: *Petrus Pang*" underneath when it differs; buttons **Save as <name>** (POST confirmed with the field value), **Not a name** (POST rejected), **Decide later** (close; no call).
3. After Save, reuse the enrolment-outcome notice that the rename path shows (`propagation` / `enrolment` fields are identical).
4. `scripts/api/*.js`: the request body is whitelisted there — add `display_name` to the allowed keys for this call or the edited name is silently dropped (memory: "ui-api-layer-whitelists-request-body").

---

## Decisions for the owner (blocking the tasks marked)

1. **Should the writer skip detection/storage when `SPEAKER_IDENTITY_MODE == "off"`?** Rows are text, not biometrics, and the bell is already 404 when off — the plan stores them so the queue is full on the day identity is switched on. Says no → add the gate in Task 5 Step 3.
2. **Detection floor 3 s (propagate) vs 10 s (enrol on the intro itself).** Plan keeps 3 s and enrols on the cluster's longest turn (Task 6 Step 3). Says "the intro must be the enrolment window" → raise `min_turn_sec` to 10 and drop the longest-turn logic.
3. **"Hello, this is Cassidy Construction"** — treat a capitalised multi-token X ending in a company word (`Construction|Builders|Electrical|Plumbing|Ltd|Limited|Group`) as a company, not a name (plan's default), or accept it and let the human reject it? Default is to reject; Task 1 Step 3 pins whichever is chosen.
4. **List cap of 20** in `GET /name-suggestions` — a screen-size choice like `PROPOSAL_PAGE`; env `SUGGESTION_PAGE` if the owner wants it movable.

## Findings (filled during execution)

- Task 1 Step 5 precision: _pending_
- Task 3 Step 3 Data API transaction id and outcome: _pending_
- Task 8 TEST run: _pending_
