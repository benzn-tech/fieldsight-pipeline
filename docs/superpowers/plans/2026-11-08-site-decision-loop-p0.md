# Site Decision Loop P0 (Shadow) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn two kinds of site events (checklist gaps, overdue to-dos) into decision cards that are stored, routed and shown to the owner for labelling, but never sent, so the owner can judge whether the loop picks the right things before anything reaches a customer.

**Architecture:** A pure decision module (`decision_hub.py`, `decision_rules.py`) holds every rule (late check, dedupe key, rule match, card shape, recipients) with no I/O, so it can be replayed. A scheduled VPC Lambda (`lambda_decision_scan.py`) emits events from data, then processes queued events through the pure hub and four new repositories, with attempts and back-off kept in `event_runs`. Org-api serves the shadow cards to platform admins and records labels; a fieldsight-ui page shows them.

**Tech Stack:** Python 3.11 Lambdas, psycopg 3, Aurora Postgres, AWS SAM (`src/template.yaml`), EventBridge schedules, pytest (unit plus integration against real Postgres via `TEST_DATABASE_URL`), vanilla-JS fieldsight-ui with `node --test`.

**Spec:** `docs/superpowers/specs/2026-11-05-site-decision-loop-design.md` (rev 5) and `docs/handoffs/2026-11-06-site-decision-loop-p0.md`. Read §4.1, §4.2, §4.5 steps 1-3, §4.7, §5, §6.2, §6.5, §8, §12 and §17 before starting.

## Global Constraints

- Flag `SITE_DECISION_LOOP` with values `off` / `shadow`, default `off` on every stage. SAM parameter `SiteDecisionLoop`; workflow vars `TEST_SITE_DECISION_LOOP` and `PROD_SITE_DECISION_LOOP`, each defaulting to `'off'`. With `off`, the scan function returns without opening a DB connection.
- **Nothing is sent in P0:** no email, push, voice or SES call anywhere in this plan. Cards are routed (recipients computed) and stored.
- No card timers in P0: `decision_cards.status` stays `open`; labelling never changes `status`; no `defaulted` ledger rows are written.
- No model calls anywhere in P0 code.
- Every new table carries `company_id` and `site_id`; every repository read filters by both.
- `decision_ledger` is append-only, enforced by a trigger (single DB role, no grants).
- Retries: a retryable failure sets the event back to `queued` with `not_before = now + backoff` (5 min doubling to a 1 h cap); on the 3rd failure a fault card is created; on the 5th the event becomes `failed` (terminal, never re-claimed) and that fault card is bumped (`version` + 1, `consequence` says it stopped). No second card.
- Fault card identity: `kind='fault'`, `rule_id='hub.fault'`, `purpose='fault'`, `subject_ref=<event_id>`, `event_id=<the failing event>`. A too-deep chain: `purpose='too_deep'`, `subject_ref=<chain_id>`, so one chain raises one card. Recipients: company admin / gm / regional_manager plus platform admins.
- Chain depth: events with `chain_depth > 3` get status `too_deep`, are not dispatched, and raise one fault card to ops.
- Dedupe key = (company_id, site_id, type, subject_ref, bucket). Bucket: `todo.overdue` = NZ calendar day of the scan; `checklist.gap` = none (empty string).
- Open-card key = (company_id, site_id, rule_id, subject_ref, purpose), unique only while `status = 'open'`.
- Shadow labels: `needed` / `not_needed` / `wrong_evidence`.
- Recipients (v1 roles, §6.4):
  - site role `pm` or `site_manager`, from `memberships.members_for_site(conn, company_id, site_id)`;
  - company `admin` / `gm` / `regional_manager`, from `users.list_company_users(conn, company_id)` filtered on `global_role`;
  - `platform_admin` (ops), from the cross-company user directory in `repositories/users.py`.
  Rows carry `id`, `global_role` and `site_role`.
- Safety item test: case-insensitive substring of any of `("safety", "h&s", "health and safety", "hazard")` in the checklist title or item text.
- Late window (plan's call, spec §14 leaves it): `checklist.gap` is late when its report date is more than 7 days before the scan; `todo.overdue` is never late (`occurred_at` = scan day).
- An event's `today_nz` = `nz_time.to_nz(received_at).date()`, used both when emitting and in replay.
- Overdue card deadline: end of the next NZ working day (Saturday works, Sunday does not, §6.6), via a new `decision_hub.next_working_day_end_nz(today_nz)`.
- Company status: shadow always blocks companies that are not active, whatever `COMPANY_STATUS_ENFORCE` says.
- Checklist results live under `session_report_results/{folder}/{date}/{session}/{request_id}.json` in the lake bucket.
- **Invariant this plan relies on:** `checklist_reports` rows are created only behind the `tier == "final"` gate (`lambda_item_writer.py:1662-1667`); `requeue_walk` reuses the same rows. A future generator that writes rows for non-final extractions must also mark them, or the gap scan will start carding live extractions.
- NZ time through `nz_time.py`, never a fixed +13.
- Python: `C:\Users\camil\fspy\Scripts\python.exe`. Integration tests: one pytest process at a time against the local pgserver (`fswork/run-tests.sh`); CI has a Postgres service in `.github/workflows/test.yml`.
- Migration number: `0097` unless develop has moved on when you start; take the next free number and keep it everywhere this plan says 0097.

## Review Focus

1. **A session that lies on zero or several sites.** `checklist_reports` has no `site_id`; the site comes from `recordings.session_site_companies`. Expected: zero or several sites → no event, counted as `skipped_ambiguous_site` in the scan result and logged; never guess a site. Test in Task 4.
2. **A checklist re-run that replaces `result_key` with the same gaps.** Expected: no second event (dedupe) and no second open card. Test in Task 4.
3. **A report whose S3 result is missing or not yet `done`.** Expected: skipped this scan, picked up on a later scan; never an error that stops the scan. Test in Task 4.
4. **A site whose company is not active (suspended or exited).** Expected: events refused at auth (status `blocked`), no card. Test in Task 4.
5. **Two scans overlapping (EventBridge retry, or a slow run).** Expected: no duplicate events or cards; a run claimed by one scan is not processed by the other (`FOR UPDATE SKIP LOCKED`); the seen-table insert is `ON CONFLICT DO NOTHING` in the same transaction as the events. Test in Task 4.
6. **An S3 read that returns 403, or a malformed result.** Expected: that report is counted in `errors` and retried on the next scan; the scan continues. ListBucket is granted on the prefix so a missing key is a 404, not a 403. Test in Task 4.
7. **A walk report covering several units and sessions.** Expected: `subject_ref` uses the gap's own `n` and text (`checklist.py:182-186`), not list position, so a vanished gap does not shift the others' keys. Test in Task 4.

---

### Task 1: Migration 0097, the four tables and the ledger trigger

**Files:**
- Create: `src/migrations/0097_site_decision_loop.sql`
- Test: `tests/integration/test_site_decision_loop_schema.py`

**Interfaces:**
- Produces tables:
  - `site_events`: `event_id uuid pk`, `seq bigserial unique`, `company_id`, `site_id`, `source text`, `type text`, `subject_ref text`, `bucket text not null default ''`, `occurred_at timestamptz`, `received_at timestamptz default now()`, `summary text`, `link text`, `payload jsonb`, `chain_id uuid`, `chain_depth int default 0`, `dedupe_key text not null`, `status text`, `rule_id text`, `attempts int default 0`, `not_before timestamptz`, `last_error text`.
    - `status` CHECK in (`received`, `late`, `unmatched`, `duplicate`, `merged`, `queued`, `running`, `done`, `nothing`, `failed`, `blocked`, `too_deep`).
    - Partial unique index on `dedupe_key` WHERE `status <> 'duplicate'`.
  - `event_runs`: `run_id uuid pk`, `company_id`, `site_id`, `attempt int`, `handler text`, `claim_id uuid`, `lease_until timestamptz`, `started_at`, `ended_at`, `outcome text`, `result_hash text`, `error text`, `tokens_in int`, `tokens_out int`, `cost_usd numeric`, `model_calls int default 0`, `models jsonb`.
    - `outcome` CHECK in (`done`, `nothing`, `failed`, `unknown`, `cancelled`).
  - `run_events`: `run_id`, `event_id`, primary key on both.
  - `decision_scan_seen`: `report_id uuid`, `result_key text`, `seen_at timestamptz default now()`, primary key (`report_id`, `result_key`). A report is marked seen only once its result read `done`.
  - `decision_cards`: `card_id uuid pk`, `event_id`, `company_id`, `site_id`, `rule_id`, `subject_ref`, `purpose text`, `card_key text`, `kind text`, `question`, `consequence`, `options jsonb`, `evidence jsonb`, `urgency text`, `deadline_at`, `timeout_default text`, `recipients jsonb`, `version int default 1`, `delivered_refs jsonb default '[]'`, `status text`, `shadow_label text`, `labelled_by uuid`, `labelled_at timestamptz`, `label_note text`, `created_at`.
    - `kind` CHECK in (`approve`, `choose`, `fact`, `fault`, `rule`).
    - `status` CHECK in (`open`, `answered`, `done`, `timed_out`, `superseded`).
    - `shadow_label` CHECK in (`needed`, `not_needed`, `wrong_evidence`) or NULL.
    - Partial unique index on `card_key` WHERE `status = 'open'`.
  - `decision_ledger`: the columns in spec §5.3.
    - `kind` CHECK in (`decided`, `declined`, `defaulted`).
    - `source` CHECK in (`card`, `timeout_default`, `meeting`).
    - Trigger `decision_ledger_append_only`: BEFORE UPDATE OR DELETE, raises `decision_ledger is append-only`.

- [ ] **Step 1: Write the failing tests** in the style of `tests/integration/test_member_last_seen.py`, applying the file twice:
  - `test_migration_is_idempotent`: running 0097 twice raises nothing.
  - `test_ledger_rejects_update_and_delete`: insert one ledger row; `UPDATE` and `DELETE` each raise `psycopg.errors.RaiseException` with message containing `append-only`.
  - `test_dedupe_index_allows_duplicate_rows_only_as_duplicate`: a second row with the same `dedupe_key` and status `queued` raises `UniqueViolation`; with status `duplicate` it inserts.
  - `test_one_open_card_per_key`: two `open` cards with one `card_key` raise `UniqueViolation`; an `open` plus a `superseded` card with that key insert.
  - `test_check_constraints`: an unknown `status`, `kind` or `shadow_label` raises `CheckViolation`.
- [ ] **Step 2: Run** `C:\Users\camil\fspy\Scripts\python.exe -m pytest tests/integration/test_site_decision_loop_schema.py -v`. Expected: FAIL (file missing).
- [ ] **Step 3: Write `0097_site_decision_loop.sql`.** Header comment: spec path plus "single DB role, so the ledger is append-only by trigger". `CREATE ... IF NOT EXISTS` throughout, and `CREATE OR REPLACE FUNCTION` + `DROP TRIGGER IF EXISTS` for the trigger, so it is idempotent. Indexes: `site_events (status, not_before)`, `site_events (company_id, site_id, type, subject_ref)`, `decision_cards (company_id, site_id, status)`.
- [ ] **Step 4: Run the tests.** Expected: PASS. Also run `tests/integration/test_migrations_apply.py`; expected: PASS.
- [ ] **Step 5: Commit** `feat(decision-loop): migration 0097, event/run/card/ledger tables`.

### Task 2: Pure decision module (rules, late, dedupe, cards, recipients)

**Files:**
- Create: `src/decision_rules.py`, `src/decision_hub.py`
- Test: `tests/unit/test_decision_hub.py`

**Interfaces:**
- `decision_rules.RULES: dict[str, Rule]`, keyed by event type; P0 has `"checklist.gap"` and `"todo.overdue"`. `Rule` is a frozen dataclass:
  - `rule_id: str`
  - `bucket: Callable[[Event, date], str]`
  - `late_after_days: int | None`
  - `purpose: str`
  - `build_card: Callable[[Event], CardDraft]`
- `decision_hub.Event`: frozen dataclass mirroring the `site_events` columns used by the hub (event_id, company_id, site_id, source, type, subject_ref, occurred_at, received_at, payload, chain_id, chain_depth).
- `decision_hub.CardDraft`: dataclass with:
  - kind, question, consequence;
  - options (list of `{"id","label","recommended"}`);
  - evidence (list of `{"quote","at"}`);
  - urgency (`"injury"` | `"normal"`);
  - deadline_at, timeout_default;
  - recipient_roles (list of str).
- `decision_hub.dedupe_key(event: Event, today_nz: date) -> str`: `"|".join` of company_id, site_id, type, subject_ref, bucket.
- `decision_hub.card_key(event: Event, rule: Rule) -> str`.
- `decision_hub.is_late(event: Event, rule: Rule, today_nz: date) -> bool`.
- `decision_hub.match(event: Event) -> Rule | None`.
- `decision_hub.too_deep(event: Event) -> bool`: `chain_depth > 3`.
- `decision_hub.recipients(card: CardDraft, site_members: list[dict], company_users: list[dict], platform_admins: list[dict]) -> list[dict]`: returns `[{"user_id","role"}]`; input rows carry `id`, `global_role`, `site_role`.
- `decision_hub.next_working_day_end_nz(today_nz: date) -> datetime`: 23:59:59 NZ on the next day that is not Sunday.
- `decision_hub.is_safety(text: str) -> bool`.
- `decision_hub.backoff_seconds(attempts: int) -> int`: `min(300 * 2 ** (attempts - 1), 3600)`.
- `decision_hub.FAULT_AT = 3`, `decision_hub.STOP_AT = 5`.

- [ ] **Step 1: Write failing unit tests** (pure, no DB):
  - `test_dedupe_key_overdue_buckets_by_nz_day`: same item, two different NZ days → two keys; same day → one key.
  - `test_dedupe_key_gap_has_no_bucket`: two scans on different days → one key.
  - `test_late_gap_after_7_days`: report date 8 days before today → late; 7 days → not late.
  - `test_overdue_never_late`.
  - `test_unknown_type_matches_nothing`.
  - `test_too_deep_only_above_3`: depth 3 → False, depth 4 → True.
  - `test_backoff_schedule`: attempts 1..6 → 300, 600, 1200, 2400, 3600, 3600.
  - `test_overdue_card_shape`. Payload `{"text":"Order rebar","deadline":"2026-11-01","responsible":"Tom"}`:
    - kind `choose`;
    - question contains `Order rebar` and `1 Nov 2026`;
    - option ids `remind`, `done`, `new_date`, `not_needed`, with `remind` recommended;
    - `timeout_default == "nothing changes"`;
    - `deadline_at` = end of the next NZ working day: from Saturday 8 Nov 2026 that is Monday 10 Nov 23:59:59 NZ; from Friday it is Saturday.
  - `test_gap_card_shape`: payload `{"checklist":"Pre-pour","item":"Cover to reinforcing","kind":"not_covered","session":"s1","quote":null}`:
    - kind `choose`;
    - option ids `remind`, `accept`, `reassign`, with `remind` recommended;
    - `accept` carries `"reason_required": True`.
  - `test_gap_on_safety_item_is_injury_urgency_and_copies_admin`: item `"Edge protection hazard"` → urgency `injury`; `recipient_roles` includes `company_admin`.
  - `test_recipients_v1_roles`: site members with site_role pm, site_manager, worker; company users with global_role admin and worker; one platform admin. A normal card goes to pm + site_manager only; a fault card goes to the admin + the platform admin.
  - `test_card_key_stable_across_versions`.
- [ ] **Step 2: Run** `python -m pytest tests/unit/test_decision_hub.py -v`. Expected: FAIL (module missing).
- [ ] **Step 3: Implement both modules.**
  - No imports of psycopg, boto3 or org modules: the module must stay pure for the replay judge.
  - Dates go through `nz_time`.
  - Card copy is English, per spec §6.3 and §6.5.
- [ ] **Step 4: Run the tests.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(decision-loop): pure rules and hub functions`.

### Task 3: Repositories (events, runs, cards, ledger) with tenancy filters

**Files:**
- Create: `src/repositories/site_events.py`, `src/repositories/event_runs.py`, `src/repositories/decision_cards.py`, `src/repositories/decision_ledger.py`
- Test: `tests/integration/test_site_decision_loop_repos.py`

**Interfaces:**
- `site_events`:
  - `insert(conn, ev: dict) -> tuple[dict, bool]`: returns `(row, is_new)`. Two statements: `INSERT ... ON CONFLICT DO NOTHING RETURNING`; if nothing returned, insert again with status `duplicate` and `dedupe_key` suffixed `|dup|<event_id>`; `is_new=False`.
  - `claim_due(conn, now, limit=50) -> list[dict]`: only rows in `queued` with `not_before <= now` or NULL, `ORDER BY seq`, `FOR UPDATE SKIP LOCKED`; sets status `running`.
  - `set_status(conn, event_id, company_id, site_id, status, **fields)`.
  - `queued_same_subject(conn, company_id, site_id, type, subject_ref) -> list[dict]`: rows with status `queued`.
- `decision_scan_seen`: `unseen_reports(conn, max_age_hours=48) -> list[dict]` (in `checklist_reports.py`) and `mark_seen(conn, report_id, result_key)` (`ON CONFLICT DO NOTHING`).
- `event_runs`:
  - `start(conn, company_id, site_id, event_ids: list, attempt: int, handler: str) -> dict`: also writes `run_events`.
  - `finish(conn, run_id, company_id, site_id, outcome, error=None, result_hash=None)`. The attempt count used by the fault and stop rules is `site_events.attempts`.
  - `add_event(conn, run_id, event_id)`.
- `decision_cards`:
  - `insert_open(conn, card: dict) -> dict | None`: returns None when an open card with that `card_key` exists (ON CONFLICT DO NOTHING).
  - `find_open(conn, company_id, site_id, card_key) -> dict | None`.
  - `supersede(conn, company_id, site_id, card_key) -> int`.
  - `bump(conn, card_id, company_id, site_id, consequence) -> dict`: version + 1.
  - `list_shadow(conn, limit, offset, label=None) -> list[dict]`: platform-admin read, every company, newest first.
  - `set_label(conn, card_id, label, note, user_id) -> dict | None`.
- `decision_ledger`:
  - `decided(conn, company_id, site_id, subject_ref) -> dict | None`: the latest row of kind `decided` or `declined` that no later row supersedes. `defaulted` never counts.
  - `append(conn, row: dict) -> dict`.

- [ ] **Step 1: Write failing integration tests:**
  - `test_insert_dedupes_into_duplicate_row`.
  - `test_claim_due_skips_locked_rows`: two connections; the second claim gets none of the first's rows.
  - `test_claim_due_respects_not_before`.
  - `test_reads_are_tenant_scoped`: `set_status`, `find_open` and `supersede` with another company's id touch 0 rows.
  - `test_insert_open_returns_none_when_open_card_exists`.
  - `test_ledger_decided_ignores_defaulted_and_superseded`.
  - `test_set_label_does_not_change_status`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement** the four modules in the style of `repositories/status_suggestions.py`: `dict_row`, explicit `_COLS`, parameterised SQL, `company_id` and `site_id` in every WHERE except `list_shadow` (platform admin) and `set_label`.
- [ ] **Step 4: Run them.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(decision-loop): repositories for events, runs, cards, ledger`.

### Task 4: Scan and process Lambda (emitters + hub loop)

**Files:**
- Create: `src/lambda_decision_scan.py`
- Modify: `src/repositories/action_items.py` (add `overdue_open(conn, today_nz) -> list[dict]`); `src/checklist_reports.py` (add `unseen_reports`, `mark_seen`, `read_result_status`); `src/lambda_org_api.py` (call `checklist_reports.read_result_status` instead of its own copy)
- Test: `tests/integration/test_decision_scan.py`

**Interfaces:**
- Consumes Tasks 2 and 3.
- `lambda_handler(event, context)`: `event` is `{"scan": "checklist_gap" | "todo_overdue" | "process"}`. Returns:
  `{"emitted": n, "duplicates": n, "skipped_ambiguous_site": n, "skipped_pending": n, "processed": n, "cards": n, "faults": n}`.
- `emit_todo_overdue(conn, today_nz) -> dict`:
  - one event per item from `action_items.overdue_open`: status `'open'`, `deadline < today_nz`, `deadline IS NOT NULL`;
  - `company_id` from the item's site; `subject_ref = stable_id`; `occurred_at` = scan time;
  - payload carries text, deadline and responsible.
- `emit_checklist_gaps(conn, read_result) -> dict`:
  - for each row from `checklist_reports.unseen_reports(conn)`, call `read_result(result_key)`. This is `checklist_reports.read_result_status(result_key, s3_client) -> (status, error, gaps)`: org-api's `_checklist_report_status` / `_checklist_gaps` moved there, with org-api importing it. Status not `done` → skip, not marked seen, counted in `skipped_pending`. Any exception → counted in `errors`, not marked seen.
  - Site from `recordings.session_site_companies(conn, row["user_folder"], str(row["report_date"]), row["session"])`; skip unless exactly one site. The event's `company_id` is **that site's company**, not `checklist_reports.company_id` (the recorder's home company).
  - One event per gap item: `subject_ref = f"{session}|{checklist}|{n}|{text}"`, from the gap's own `n` and `text`; payload carries checklist, item text, kind (`not_covered` / `failed`), comment, report_date.
  - A gap present on the report's previous card set and absent now → `decision_cards.supersede` for its card key.
  - `mark_seen(report_id, result_key)` in the same transaction as that report's events.
- `process(conn, now) -> dict`. For each event from `site_events.claim_due`:
  - auth (company status via `tenancy_status.company_status`, not active → `blocked`);
  - `too_deep` → `too_deep` + one fault card to ops;
  - late → `late`;
  - `match` → `unmatched` if none;
  - fold in other `queued` same-subject events (`run_events`, status `merged`);
  - run start;
  - `decision_ledger.decided` → `done` with no card;
  - `decision_cards.insert_open` with recipients from `members_for_site` → `done`; or `nothing` when an open card exists.
  - Outcomes:

    | Case | event status | run outcome | card |
    |---|---|---|---|
    | card inserted | `done` | `done` | new open card |
    | ledger says decided | `done` | `nothing` | none |
    | open card with this key exists | `nothing` | `nothing` | none |
    | exception, attempts < 5 | `queued`, `not_before = now + backoff` | `failed` | fault card when attempts reaches 3 |
    | exception, attempts = 5 | `failed` (terminal) | `failed` | fault card bumped |
- Flag: `SITE_DECISION_LOOP != "shadow"` → return `{"skipped": "flag off"}` before `get_connection()`.

- [ ] **Step 1: Write failing integration tests.** Use real Postgres, a fake `read_result`, and the `now` and `today_nz` parameters to control time:
  - `test_flag_off_opens_no_connection` (monkeypatch `get_connection` to raise).
  - `test_overdue_item_becomes_one_open_card_per_item`: two daily scans → 2 events (2 buckets) but 1 open card.
  - `test_gap_rerun_same_gaps_makes_no_second_card` (Review Focus 2).
  - `test_gap_disappearing_supersedes_its_card`.
  - `test_ambiguous_site_is_skipped_and_counted` (Review Focus 1).
  - `test_pending_report_is_picked_up_later` (Review Focus 3).
  - `test_inactive_company_blocks` (Review Focus 4).
  - `test_overlapping_process_runs_do_not_double_process` (Review Focus 5, two connections).
  - `test_failures_back_off_fault_at_3_stop_at_5`: a handler forced to raise; attempts 1-5. Between attempts the event is `queued` with growing `not_before`; exactly one fault card, version 2 after the 5th; the event ends `failed` and `claim_due` never returns it again.
  - `test_s3_403_counts_error_and_continues` (Review Focus 6).
  - `test_walk_gap_keys_survive_a_vanished_gap` (Review Focus 7).
  - `test_event_company_is_the_sites_not_the_recorders`: an external recorder (home company A) records on company B's site → the event is B's.
  - `test_decided_subject_gets_no_card`.
  - `test_too_deep_event_not_dispatched_and_faults_once`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement** the handler, emitters, `process`, and the two repository helpers. Move `_checklist_gaps` / `_checklist_report_status` into `checklist_reports.read_result_status(result_key, s3_client)` and make org-api call it, so org-api's behaviour is unchanged.
- [ ] **Step 4: Run them, plus the existing org-api checklist tests:** `python -m pytest tests -k "checklist_report or decision" -v`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(decision-loop): scan emitters and shadow processing`.

### Task 5: SAM function, schedules, flag wiring

**Files:**
- Modify: `src/template.yaml`, `.github/workflows/deploy.yml`, `.github/workflows/deploy-prod.yml`
- Test: `tests/unit/test_decision_scan_template.py`, modelled on `tests/unit/test_check_gaps_template.py`

**Interfaces:**
- Parameter `SiteDecisionLoop` (AllowedValues `off`, `shadow`; Default `off`).
- Condition `ShouldRunDecisionLoop` = `HasDb` and not `off`.
- `DecisionScanFunction`:
  - `Condition: ShouldRunDecisionLoop`;
  - VPC, `PsycopgLayer`, DB credential env like `CheckGapsDbFunction`;
  - env `SITE_DECISION_LOOP`, `COMPANY_STATUS_ENFORCE`, `LAKE_BUCKET`;
  - IAM: S3 GetObject on `session_report_results/*` in the lake bucket, and ListBucket on the bucket conditioned on that prefix (so a missing key is a 404, not a 403); no SES, no invoke permissions;
  - Timeout 120, MemorySize 512.
- Schedules, all `State: !If [ShouldEnableSchedules, ENABLED, DISABLED]`:
  - `checklist_gap`: rate(15 minutes);
  - `todo_overdue`: cron 18:30 UTC (06:30/07:30 NZ);
  - `process`: rate(5 minutes).
- `OrgApiFunction` env: add `SITE_DECISION_LOOP: !Ref SiteDecisionLoop` unconditionally (Task 6 reads it).
- Workflows: add `"SiteDecisionLoop=${{ vars.TEST_SITE_DECISION_LOOP || 'off' }}"` in deploy.yml and the `PROD_` version in deploy-prod.yml, next to the `StatusSuggestions=` line.

- [ ] **Step 1: Write failing template tests:**
  - `test_parameter_defaults_off`;
  - `test_function_exists_only_under_condition`;
  - `test_three_schedules_with_inputs`;
  - `test_no_ses_or_invoke_permission`: the policy statements contain no `ses:` and no `lambda:InvokeFunction`;
  - `test_s3_scope_is_session_report_results_with_listbucket`;
  - `test_org_api_gets_the_flag`;
  - `test_workflows_pass_the_flag_with_off_default` (both files).
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Edit the template and the two workflows.** Memory: SAM keeps a previous parameter value, so a changed default does not reach a stack; the workflow variable is what sets it.
- [ ] **Step 4: Run the tests and `sam validate --lint`** (the CI cfn-lint pin). Expected: PASS.
- [ ] **Step 5: Commit** `feat(decision-loop): scan function, schedules and SITE_DECISION_LOOP flag (off by default)`.

### Task 6: Org-api shadow review routes

**Files:**
- Modify: `src/lambda_org_api.py`: two routes in `dispatch`, plus the route list in the module docstring
- Test: `tests/unit/test_org_api_decision_shadow.py` (routing and auth with fakes) and `tests/integration/test_org_api_decision_shadow_sql.py`

**Interfaces:**
- Inside `dispatch` (which receives `route` already stripped of `/api/org`), match `/decision-shadow` and `/decision-shadow/{card_id}/label` with a regex like `_PACK_ROUTE_RE`.
- `GET /api/org/decision-shadow?label=&limit=&offset=`. Platform admin only (`repositories.acl.is_cross_company(caller["global_role"])`), otherwise 403 `"platform_admin role required"`.
  - Returns `{"cards": [...], "summary": {"labelled": n, "needed": n, "not_needed": n, "wrong_evidence": n, "precision": float | None, "cards_per_recipient_day_p95": float | None}}`.
  - `precision = needed / (needed + not_needed)`, None when the denominator is 0.
  - `cards_per_recipient_day_p95`: over (recipient user_id, NZ calendar day of `created_at`, with Sunday counted with the following Monday) pairs, excluding `fault` cards; this is the spec's ≤3 gate.
  - Each card carries the company and site names for display.
- `POST /api/org/decision-shadow/{card_id}/label`, body `{"label": "needed" | "not_needed" | "wrong_evidence", "note": str}`. Platform admin only. An unknown label → 400. An unknown card → 404. Returns the card. Never changes `status`.
- Both return 404 when `SITE_DECISION_LOOP` is `off` in org-api's env (wired in Task 5).
- **Response shape (fixed by the ui page already on dev, ui#469; keep these names exactly):**
  - card: `card_id`, `company_name`, `site_name`, `rule_id`, `kind`, `purpose`, `urgency` (`injury` | `normal`), `question`, `consequence`, `created_at` (ISO 8601 UTC), `shadow_label`, `label_note`;
  - `options`: `[{id, label, recommended, reason_required?}]`;
  - `evidence`: `[{quote, at}]`;
  - `recipients`: `[{user_id, role, display_name}]` (`display_name` = first + last name, so the page shows names);
  - summary: `labelled`, `needed`, `not_needed`, `wrong_evidence`, `precision` (0..1 or null), `cards_per_recipient_day_p95` (number or null);
  - the label POST returns the card in the same shape. The `label` filter is one of the three values or absent; there is no `unlabelled` value.

- [ ] **Step 1: Write failing tests:**
  - `test_non_platform_admin_gets_403` (admin, gm, pm);
  - `test_flag_off_is_404`;
  - `test_label_validation`;
  - `test_label_unknown_card_404`;
  - `test_summary_precision_formula` (2 needed, 1 not_needed, 1 wrong_evidence → precision 0.667);
  - `test_cards_per_recipient_day_p95` (integration: one recipient with 4 cards on one NZ day and 1 on another → p95 ≥ 3.5; fault cards excluded);
  - `test_label_keeps_status_open` (integration).
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement** both handlers. The API layer passes only `label` and `note` (whitelist; memory: the ui api layer whitelists request bodies).
- [ ] **Step 4: Run them.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(decision-loop): org-api shadow card list and labels (platform admin)`.

### Task 7: Replay judge, invariants, mutation tests, nightly run

**Files:**
- Create: `tools/decision_replay.py`, `tests/integration/test_decision_replay.py`, `tests/integration/test_decision_mutations.py`, `.github/workflows/nightly-decision-loop.yml`

**Interfaces:**
- `decision_replay.replay(conn, company_id=None) -> list[str]`:
  - read `site_events` in `seq` order, skipping rows in `queued` / `running`;
  - per event, `today_nz = nz_time.to_nz(received_at).date()`;
  - recompute `dedupe_key` from the stored fields and compare it with the column (strip the `|dup|` suffix on duplicates);
  - for pre-dispatch outcomes (`late`, `unmatched`, `duplicate`, `blocked`, `too_deep`) compare the status one to one. `blocked` is recomputed from the current `companies.status`, so a company whose status changed since shows as a known false mismatch; label it as such in the output;
  - for dispatched events compare the set of (`card_key`, `kind`, `status`) with `decision_cards`, and check there is exactly one `done` event per `card_key`;
  - fault cards are compared through `run_events` / failed-run counts, not through the pure hub;
  - return a list of human-readable mismatches, empty when consistent.
- `decision_replay.invariants(conn) -> list[str]`, as SQL checks:
  - every card has an event of the same company and site;
  - no row in any of the four tables has a `site_id` whose site belongs to a different `company_id`;
  - at most one `open` card per `card_key`;
  - no ledger row of kind `defaulted` exists (none in P0);
  - every `merged` event is in `run_events`;
  - an event with ≥3 failed runs has exactly one fault card; no event has more than 5 failed runs;
  - `site_events.attempts` = the count of that event's failed runs.

- [ ] **Step 1: Write failing tests:**
  - `test_replay_of_a_scripted_day_is_clean`: seed 2 companies, 3 sites, items and reports, run the scans and `process` several times with time advanced; `replay()` and `invariants()` both return `[]`.
  - The scripted day must include: an external recorder on another company's site, a gap report dated more than 7 days back, and one event whose handler is forced to fail.
  - `lambda_decision_scan` imports hub functions by name (`from decision_hub import is_late, dedupe_key, ...`). Mutations patch `lambda_decision_scan.<name>`, and the patch is **undone before** `replay()` / `invariants()` run, so the judge never shares the mutant.
  - `test_mutations_are_all_caught`: parametrised over the planted faults below. Each applies its patch, reruns the scripted day, undoes the patch, and asserts `replay()` or `invariants()` reports at least one finding. The faults:
    - `wrong_company`: `emit_checklist_gaps` uses `checklist_reports.company_id` instead of the site's company (caught by the cross-company invariant);
    - `skip_dedupe`: `dedupe_key` returns a uuid (caught by the recomputed-key comparison);
    - `skip_late`: `is_late` always returns False (caught on the old gap report);
    - `duplicate_event`: emit each overdue event twice under fresh keys (caught by the recomputed-key comparison);
    - `skip_attempt_counter`: attempts never increment (caught by the attempts invariant).
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement** `tools/decision_replay.py`, and add the nightly workflow: cron 15:00 UTC, the same Postgres service as `test.yml`, running these two files plus `test_decision_scan.py`. Header note: "No answer counted twice" cannot be exercised until P1 (spec §12).
- [ ] **Step 4: Run them.** Expected: PASS, with every mutation caught. If one is not caught, strengthen the replay or invariants; never delete the mutation.
- [ ] **Step 5: Commit** `test(decision-loop): replay judge, invariants, mutation gate, nightly`.

### Task 8: fieldsight-ui shadow review page (repo fieldsight-ui, branch from `dev`)

**Files (fieldsight-ui):**
- Create: `scripts/api/decisions.js`, `scripts/pages/decision-review.js`, `tests/decision-review.test.js`
- Modify: `scripts/pages/_page-registry.js` (route `decision-review`; this file is a plain route map), and the role gate where other admin-only routes are gated (`scripts/auth/session-bridge.js` around line 105 and `scripts/app-shell.js` around line 811): visible only to `platform_admin`

**Interfaces:**
- Consumes Task 6's two routes.
- `window.FS.api.decisions`:
  - `listShadow({label, limit, offset}) -> Promise<{cards, summary}>`;
  - `label(cardId, label, note) -> Promise<card>`. The body is built from exactly `label` and `note`.
- The page shows:
  - the summary line: labelled / needed / not needed / wrong evidence / precision;
  - a filter by label;
  - one row per card: company, site, rule, question, options (the recommended one marked), evidence quote, recipients, created date in NZ time. Find the existing NZ date formatter in the repo before writing one (`scripts/api/dates.js` exports only `getDates`); reuse it if it exists;
  - three buttons and a note field.

- [ ] **Step 1: Write failing node tests:**
  - the api body whitelist (extra keys dropped);
  - the role gate hides the route for non-platform-admins;
  - the precision text renders `—` when null.
- [ ] **Step 2: Run** `node --test tests/decision-review.test.js`. Expected: FAIL.
- [ ] **Step 3: Implement** following an existing page such as `scripts/pages/quality.js` for structure.
- [ ] **Step 4: Run the full suite** `node --test tests/*.test.js`. Expected: 0 failures.
- [ ] **Step 5: Commit and open a PR to `dev`** (the frontend line merges `dev` itself; `main` is the owner's).

### Task 9: TEST verification and handover to the owner

- [ ] **Step 1:** Merge Tasks 1-7 to `develop` once CI is green (check the base is `develop`). Set repo var `TEST_SITE_DECISION_LOOP=shadow`, then confirm the TEST deploy run applied 0097 (migrate step output) and created `DecisionScanFunction`.
- [ ] **Step 2:** Invoke the scan on TEST with each input (`checklist_gap`, `todo_overdue`, `process`). Read the JSON result. Expected: no errors; `emitted` greater than 0 on at least one of the first two, given TEST data; and `cards` matching a SQL count of `decision_cards` for that run.
- [ ] **Step 3:** Run `tools/decision_replay.py` against TEST read-only. Expected: `replay()` and `invariants()` return `[]`.
- [ ] **Step 4:** Open the review page on TEST as platform admin. Label 3 cards, confirm the summary updates and that `status` stayed `open`.
- [ ] **Step 5:** Write the TEST result into the handoff note. Open a release PR to `main` for the owner (narrow `release/*` cherry-pick, migration listed in its body). Do not set `PROD_SITE_DECISION_LOOP`. The owner applies the prod migration and flag, merges ui `main`, and labels the 50 cards.
