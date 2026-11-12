# Decision Card Inbox Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** GET/POST `/api/org/decision-cards` — a per-person P1 inbox over `decision_cards`, behind `DECISION_INBOX=companies`, on TEST only.

**Architecture:** Pure logic (labels, digest title, ordering, option table, validation) in a new `src/decision_inbox.py`; one shared redaction helper `src/decision_card_copy.py` used by the shadow view and the inbox; SQL in `src/repositories/decision_cards.py` / `decision_ledger.py`; routes in `src/lambda_org_api.py`. Additive migration 0103 records who answered. Replay judge learns `answered`.

**Tech Stack:** Python 3.11, psycopg 3, Aurora PostgreSQL, SAM, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-11-12-decision-card-inbox-design.md` (rev 2).

## Global Constraints
- Base: origin/develop AFTER the PR of branch `fix/decision-cards-deletion-and-copy` (commits e0454362, 432b64f5, 0317715c+) is merged. Worktree under `C:/Users/camil/wt/`.
- Python `C:\Users\camil\fspy\Scripts\python.exe`; tests only via `bash /c/Users/camil/fswork/run-tests.sh <path> -q`, one pytest at a time. Any SQL path is tested in `tests/integration` (fake conns never execute SQL).
- Flag values exactly: `DecisionInbox` AllowedValues `['off','companies']` default `off`; `DecisionInboxCompanies` default `''`, AllowedPattern `^$|^[0-9a-fA-F-]+(,[0-9a-fA-F-]+)*$`. Env on OrgApiFunction only.
- 404 only for the flag checks (`DECISION_INBOX!='companies'` or `SITE_DECISION_LOOP!='shadow'`) and for an invisible card on POST.
- Cap 3; injury cards never cut; field `more` (not `remaining_today`).
- Decision options → ledger: `todo.overdue` `done`→`decided`, `not_needed`→`declined`; `checklist.gap` `accept`→`decided`. Everything else: no ledger row.
- Reason ≤500 chars after strip; ledger evidence reason element `{"kind": "reason", "text": ...}`.
- Excluded from the inbox: `rule_id IN ('hub.fault','todo.overdue_backlog')`, `kind IN ('fault','rule')` (digest rows feed only the digest).
- No model calls, no email, no prod flag changes. Never `git stash`, never `git add -A`; commit messages English with NO AI attribution lines.

## Review Focus
1. A user removed from a site after the card was created must stop seeing it on GET and get 404 on POST → Task 5/6 tests.
2. The same user double-clicking (two POSTs, same option) gets 200 twice and exactly one ledger row → Task 6.
3. A gap card superseded by the scan between GET and POST → 409, no ledger row → Task 6.
4. A deleted recording behind a card: inbox shows no quote, no person name, `item: null` — identical copy to the shadow page → Task 2/5.
5. Backlog cleared today → digest `null` though the backlog card is still open → Task 5.

---

### Task 1: Migration 0103, parameters and pin test

**Files:**
- Create: `src/migrations/0103_decision_card_answers.sql`
- Modify: `src/template.yaml` (Parameters next to `SiteDecisionLoop`; OrgApiFunction env), `.github/workflows/deploy.yml`, `.github/workflows/deploy-prod.yml`
- Create: `docs/superpowers/specs/2026-11-12-decision-card-inbox-design.md` (copy of the spec)
- Test: `tests/unit/test_decision_inbox_template.py`, `tests/integration/test_migration_0103.py`

**Interfaces:** Produces columns `decision_cards.answered_by uuid`, `answered_option text`, `answered_at timestamptz`, `answer_reason text`; env `DECISION_INBOX`, `DECISION_INBOX_COMPANIES` on org-api.

- [ ] Step 1: Re-check 0103 is free on every remote ref (`git log --all --diff-filter=A --name-only -- 'src/migrations/0103*'` empty); if taken, use the next free number everywhere in this plan.
- [ ] Step 2: Write failing tests: `test_decision_inbox_params_pinned` (AllowedValues/Default/AllowedPattern exactly as Global Constraints; env only on OrgApiFunction); `test_workflows_pass_decision_inbox` (deploy.yml uses `vars.TEST_DECISION_INBOX || 'off'`, deploy-prod.yml `vars.PROD_DECISION_INBOX || 'off'`, companies appended only when set, same shape as `ACTION_PACKS_PARAM`); integration `test_0103_applies_twice_and_columns_nullable`.
- [ ] Step 3: Run them — FAIL.
- [ ] Step 4: Implement (SQL from spec §3 verbatim).
- [ ] Step 5: Run the three tests — PASS. Also run the existing template guard tests (`tests/unit -q -k template`).
- [ ] Step 6: Commit.

### Task 2: Shared redaction helper

**Files:**
- Create: `src/decision_card_copy.py`
- Modify: `src/lambda_org_api.py` (`_decision_card_views` ~1789, `_GENERIC_REMIND` ~1673 moves into the new module)
- Test: `tests/unit/test_decision_card_copy.py`

**Interfaces:** Produces `redact(row: dict, removed: bool, due: date | None) -> tuple[str, list[dict], list[dict]]` returning `(question, options, evidence)` with options/evidence in the STORED shape (`id`, `quote`, `at`), so each view maps them. Consumes `decision_rules.removed_overdue_question`, `REMIND_INSPECTOR`, and the owner wording constant.

- [ ] Step 1: Failing tests: `test_redact_live_card_unchanged`; `test_redact_removed_gap_generic_label_no_evidence`; `test_redact_removed_overdue_question_has_no_text`; `test_redact_non_remind_labels_untouched`.
- [ ] Step 2: Run — FAIL. Step 3: implement by moving the inline logic; `_decision_card_views` calls `redact`. Step 4: tests PASS, and the existing decision-shadow unit + integration tests pass unchanged (response byte-identical). Step 5: commit.

### Task 3: Pure inbox logic

**Files:** Create `src/decision_inbox.py`; Test `tests/unit/test_decision_inbox.py`

**Interfaces (Produces):**
- `DECISION_OPTIONS: dict[tuple[str, str], str]` = `{("todo.overdue","done"):"decided", ("todo.overdue","not_needed"):"declined", ("checklist.gap","accept"):"decided"}`
- `source_label(rule_id: str, payload: dict | None) -> str` (spec §4 table; `"Overdue since 13 Nov"` style via `decision_hub.format_date` if it yields `d MMM`, else `"%-d %b"` equivalent portable on Windows)
- `digest_title(per_site: list[tuple[str, int]]) -> str | None`
- `order_and_cap(cards: list[dict], cap: int = 3) -> tuple[list[dict], int]` → `(shown, more)`
- `card_view(row: dict, question: str, options: list, evidence: list, site_name: str, removed: bool) -> dict` (spec §4 card shape, `id`→`option_id`, `at`→`said_at`, evidence ≤2, `item` per spec)
- `validate_answer(row: dict, option_id: str, reason: str | None) -> str | None` (error message or None)
- `is_excluded(row: dict) -> bool`

- [ ] Step 1: Failing tests with exact values: `source_label("todo.overdue", {"deadline":"2026-11-10"}) == "Overdue since 11 Nov"`; malformed/absent → `"Overdue"`; gap → `"Missing from the inspection"`; other → `"From the site"`. `digest_title([("A",1)]) == "1 overdue to-do more than 3 days old"`; `[("B",2),("A",3)]` → `"5 overdue to-dos more than 3 days old on A, B"`; 5 sites → names of first 3 sorted + `" and 2 more sites"`; all zero/empty → None. Ordering: injury first, deadline asc nulls last, created_at, card_id; 4 injury cards → all 4 shown, `more` = rest. `validate_answer`: unknown option, missing reason when `reason_required`, 501 chars → messages; 500 chars ok. `card_view`: removed overdue → `item is None`; option without `id` dropped; card with no options → caller skips (test the helper returns options `[]`).
- [ ] Steps 2-5: run FAIL, implement, run PASS, commit.

### Task 4: Repository reads and writes

**Files:** Modify `src/repositories/decision_cards.py`, `src/repositories/decision_ledger.py`; Test `tests/integration/test_decision_inbox_repo.py`

**Interfaces (Produces):**
- `decision_cards.inbox_rows(conn, company_ids: list[str], user_id: str) -> list[dict]` — open cards of those companies whose recipients contain the user (query shape spec §4), with `site_name`, event `payload`; includes backlog rows (caller splits).
- `decision_cards.answer(conn, card_id, company_id, site_id, user_id, option_id, reason) -> dict | None` — the guarded UPDATE of spec §5, RETURNING the row; None on 0 rows.
- `decision_cards.get_for_answer(conn, card_id) -> dict | None` — row incl. answer columns, recipients, options, event payload.
- `decision_ledger.append(conn, row)` exists — reuse; do not add a second writer.

- [ ] Step 1: Failing integration tests: inbox_rows returns only the user's open cards in listed companies (other company, other user, superseded, answered excluded); answer() first call returns row and sets the 4 columns; second call returns None; answer() with wrong company/site returns None.
- [ ] Steps 2-5: FAIL, implement, PASS, commit.

### Task 5: GET route

**Files:** Modify `src/lambda_org_api.py` (new `decision_cards_route(conn, caller, event, method, route)` dispatched beside `decision_shadow_route` ~1932); Test `tests/integration/test_decision_inbox_get.py`, unit handler tests in `tests/unit/test_org_api_decision_inbox.py`

**Interfaces:** Consumes Tasks 2-4, `_removed_source_cards`, `memberships.members_for_site`, `decision_hub.COMPANY_ADMIN_GLOBAL_ROLES`, `tenancy_status.inactive_site_ids` / `drop_inactive`, `action_items.overdue_open`, and the per-item event builder from `lambda_decision_scan._emit_overdue_item` (extract `overdue_item_event(item, now) -> Event` there if it is not separable, and use it in both places) with `decision_hub.is_late(ev, RULES["todo.overdue"], today_nz)`.

- [ ] Step 1: Failing tests: flag off (each of the two) → 404; on → 200 `{cards, digest, more}`; removed-from-site → card gone; external member of the allow-listed company's site → sees it; company_admin roles admin/gm/regional_manager → see admin-routed card, `pm` global role does not; company not allow-listed → empty; fault card excluded; CompanyStatusEnforce on + inactive company → empty; cap 3 + `more`; digest live: seed 2 stale items at card creation then 5 → count 5; close all → `digest` null; deleted session behind a gap card → no evidence, generic label (same strings as shadow route for the same row); query count bounded (follow existing query-count tests if present, else assert ≤ 8 statements for a page of 10 cards across 2 sites).
- [ ] Steps 2-5: FAIL, implement, PASS, commit.

### Task 6: POST answer route

**Files:** Modify `src/lambda_org_api.py`; Test `tests/integration/test_decision_inbox_answer.py`

**Interfaces:** Consumes Tasks 3-5. Response bodies exactly `{"status":"answered","applied":null}` / `{"status":"stale"}` (409).

- [ ] Step 1: Failing tests: `done` → 200, card answered, one ledger row (`decided`, decided_by, authority_basis = matched role, evidence as served, source `card`); `not_needed` → `declined`; gap `accept` without reason → 400 and nothing written; with reason → ledger evidence ends with `{"kind":"reason","text":...}`, `answer_reason` set; `remind` → 200, no ledger row, and a next-day `todo.overdue` event for the same item processes into a NEW card (run the scan's process step); `done` → later event is `done/nothing`, no card; same user retry → 200, still one ledger row; other user after answer → 409; superseded between GET and POST → 409, no ledger; invisible card / not a recipient / digest / fault → 404; two connections: A holds UPDATE uncommitted, B's POST blocks, A commits → B 409; A rolls back → B 200.
- [ ] Steps 2-5: FAIL, implement (all validation before the UPDATE), PASS, commit.

### Task 7: Replay judge and invariants

**Files:** Modify `tools/decision_replay.py` (status set ~351-353, per-key check ~360-365, invariants, mutation list); Test the existing judge tests file (grep `decision_replay` under tests/)

- [ ] Step 1: Failing tests: after Task 6 answers on seeded data the judge reports 0 findings; mutations caught: "ledger row written but card left open", "decision option answered with no ledger row", "ledger row written for a remind answer"; older `answered` card followed by a new open card on the same key → 0 findings.
- [ ] Steps 2-5: FAIL, implement per spec §6, PASS, commit.
- [ ] Step 6: Full `run-tests.sh tests/unit -q` then `run-tests.sh tests/integration -q`; record counts.

### Task 8 (controller): ship to TEST
- [ ] PR to develop; CI green; confirm base is develop; merge.
- [ ] Set `TEST_DECISION_INBOX=companies`, `TEST_DECISION_INBOX_COMPANIES=<Test Project company id>`; re-run deploy.yml; confirm org-api env.
- [ ] Verify on TEST via org-api invoke as an owner-alias test user that is a recipient (read-only GET; one POST on a TEST card only). Run the Data API judge shim (rebuild if gone) → 0 findings.
- [ ] Message 前端优化v2 (if live) with the live contract: `more` (not `remaining_today`), `reason` in POST, `reason_required`, `item: null` when removed, `href` to confirm.
