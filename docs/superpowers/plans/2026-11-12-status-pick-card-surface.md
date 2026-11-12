# Status Pick Card Surface Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pending status pick cards become visible and decidable on the web Tasks page. A person chooses the item (audited status change) or answers "none of these".

**Architecture:**
- A shared candidate rule (`status_candidates.py`) used by the writer and by org-api.
- Three repository functions on `status_pick_cards`.
- Two org-api routes: `GET /status-pick-cards` and `POST /status-pick-cards/{id}`.
- In the UI, two API helpers, one composite, and a mount on the Tasks page.

**Tech Stack:** Python 3.11, psycopg 3, pytest, local pgserver for integration tests. Frontend is vanilla JS tested with node:test.

**Spec:** `docs/superpowers/specs/2026-11-12-status-pick-card-surface-design.md` (rev 2).

## Global Constraints

- The system never chooses: every live candidate is shown, including one that is the only candidate left.
- Every status write goes through `patch_action_item`, and the person is the actor.
- A GET never writes.
- Logs carry ids, `site_id` and counts only. Never log `quote`, `text` or `responsible`.
- No new S3 prefix, no IAM change, no flag, no migration.
- Prod is untouched. A missing 0099 table degrades through `tenancy_status.not_migrated`.
- Error codes:
  - a malformed or unknown card id returns 404;
  - a malformed body `stable_id` returns 400;
  - a card that is not pending returns 409 `card already decided`;
  - a candidate that is no longer open returns 409 `item no longer open`;
  - a `stable_id` that is not one of the card's candidates returns 400 `not a candidate`.
- Shown cards are capped at 50, and the cap is applied after filtering.
- Python tests run with `C:\Users\camil\fspy\Scripts\python.exe`, one integration pytest at a time, via `bash /c/Users/camil/fswork/run-tests.sh <file>`.
- UI work happens in a fieldsight-ui worktree at `C:\Users\camil\wt\fsui-pickcard`, branched from `origin/dev`. Tests are `node --test tests/`.

## Review Focus

1. **The item is closed by hand between page load and the click.** The person expects a 409 and the card gone, with no status write. Pinned in Task 3 by `test_choose_rechecks_under_lock`.
2. **Someone who can edit only one candidate presses "None of these" on a hand-crafted request.** Expect 403. Pinned in Task 3 by `test_none_needs_authority_over_every_candidate`.
3. **A candidate has been moved to another site.** Expect it to be absent from the card, and a choose on it to return 409. Pinned in Task 3 by `test_moved_site_candidate_dropped`.
4. **The card is decided in another tab.** Expect the UI to remove the card with "Already handled", not leave a dead card. Pinned in Task 4 by `decidePickCard maps a thrown 409 to conflict`.
5. **The site switcher changes.** Expect cards for the new site only. Pinned in Task 4 by `tasks page refetches pick cards on site change`.

---

### Task 1: Shared candidate rule

**Files:**
- Create: `src/status_candidates.py`
- Modify: `src/lambda_suggestion_writer.py:205-227` (delete the two helpers and import them instead)
- Test: `tests/unit/test_status_candidates.py`

**Interfaces:**
- Produces:
  - `canon_uuid(value) -> str | None`, which never raises;
  - `drop_reason(row: dict | None, site_id: str | None, suggested_status: str) -> str | None`, returning one of `"gone"`, `"moved_site"`, `"terminal"`, `"same"` or None;
  - `TERMINAL = frozenset({"done", "closed"})`. Move the writer's `_TERMINAL` here if it matches; read the writer first.

- [ ] **Step 1: Write the failing tests.**
  - `test_canon_uuid_forms`: an upper-case, braced or dashless uuid returns the lowercase canonical form; `None`, `"x"` and `123` return None.
  - `test_drop_reason_order`: None gives `"gone"`; a site mismatch gives `"moved_site"` even when the status is also terminal; `site_id=None` gives `"moved_site"`; a done item gives `"terminal"`; a status equal to `suggested_status` gives `"same"`; an open item on the same site gives None.
- [ ] **Step 2:** Run `python -m pytest tests/unit/test_status_candidates.py -q`. Expected: ImportError.
- [ ] **Step 3: Implement.** Move the bodies verbatim. In the writer, keep `_canon_uuid = status_candidates.canon_uuid` and `_candidate_drop_reason = status_candidates.drop_reason` as aliases so existing call sites and tests stay unchanged.
- [ ] **Step 4:** Run the new test, `tests/unit/test_lambda_suggestion_writer.py`, and `tests/integration/test_status_suggestions_writer.py` through run-tests.sh. Expected: all pass.
- [ ] **Step 5: Commit** `refactor: share the status candidate drop rule`.

### Task 2: Repository reads and CAS

**Files:**
- Modify: `src/repositories/status_pick_cards.py`
- Test:
  - `tests/unit/test_status_pick_cards_reads.py` (fake conn: SQL shape, empty input, not_migrated);
  - `tests/integration/test_status_pick_cards_repo.py`. Extend the existing file; do not re-test insert or bounds.

**Interfaces:**
- Produces:
  - `get_pick_card(conn, card_id) -> dict | None`. It returns None for a malformed uuid (no query), an unknown id, or a `not_migrated` error.
  - `pending_for_sites(conn, site_ids: list[str]) -> list[dict]`. It returns `[]` with no query for empty input, and `[]` on `not_migrated`. Otherwise it returns `state='pending'` rows, newest first (`created_at DESC, id DESC`).
  - `resolve_pick_card(conn, card_id, state: str, chosen_stable_id: str | None, user_id) -> dict | None`. This is the CAS `UPDATE … SET state, chosen_stable_id, resolved_by, resolved_at=now() WHERE id=%s AND state='pending' RETURNING _COLS`.
  - The two reads use `with conn.transaction():` and catch only `not_migrated(exc)`; any other error re-raises.

- [ ] **Step 1: Write the failing tests.**
  - Unit:
    - `test_get_malformed_id_no_query`;
    - `test_pending_empty_in_empty_out`;
    - `test_reads_degrade_when_not_migrated`, where the fake raises `psycopg.errors.UndefinedTable`;
    - `test_other_errors_raise`.
  - Integration:
    - `test_pending_for_sites_filters_site_and_state`, with cards on two sites, one already resolved;
    - `test_resolve_is_cas`: the first call returns a row, the second returns None, and the row keeps the first decider.
- [ ] **Step 2:** Run both. Expected: failures (AttributeError).
- [ ] **Step 3:** Implement the three functions.
- [ ] **Step 4:** Run both again. Expected: pass.
- [ ] **Step 5: Commit** `feat(pick-cards): repository reads and CAS resolve`.

### Task 3: org-api routes

**Files:**
- Modify: `src/lambda_org_api.py`. Add:
  - the routes after the status-suggestion route at ~1950, literal GET first;
  - the handlers next to `decide_status_suggestion` at ~9318;
  - the keyword parameters on `_action_item_edit_denial` at ~9286;
  - one line per route in the module docstring route list.
- Test:
  - `tests/unit/test_status_pick_card_endpoints.py`, modelled on `tests/unit/test_status_suggestion_endpoint.py`'s fakes;
  - `tests/integration/test_status_pick_card_decide.py`.

**Interfaces:**
- Consumes: Task 1 `canon_uuid`, `drop_reason`; Task 2's three functions.
- Produces (HTTP):
  - GET `/api/org/status-pick-cards[?site=]` returns `{"cards":[{id, site_id, site_name, status, quote, created_at, can_dismiss, candidates:[{stable_id, action_item_id, text, status, responsible, task_no, can_choose}]}]}`.
  - POST `/api/org/status-pick-cards/{id}` takes `{decision:"choose", stable_id}` or `{decision:"none"}` and returns `{id, state, status, action_item_id}`.
- Signature change: `_action_item_edit_denial(conn, caller, row, *, allowed_site_ids=None, site_roles=None)`. When an argument is None it is fetched exactly as today.

Decisions the spec pins (§4.1, §4.2), restated so the implementer does not re-derive them:
- **Reach.** `site_id in _allowed_site_ids`. `?site=` goes through `_resolve_site_param`.
- **Live set.** A candidate is live when `drop_reason(action_items.get_live_by_stable_id(conn, sid), canon_uuid(card["site_id"]), card["suggested_status"]) is None`. The order is the stored order.
- **`can_choose`.** `_action_item_edit_denial(…, allowed_site_ids=…, site_roles=…) is None`, with both sets hoisted once per request.
- **`can_dismiss`.** True when the caller is admin/gm of the card's company, or `pm`/`site_manager` of the site (use the same predicates as `_action_item_edit_denial`'s tiers), or every live candidate has `can_choose`. The `none` decision uses the same function.
- **`task_no`.** One `_task_numbers(conn, ids)` call per request.
- **Decide order.**
  1. Body validation (400).
  2. `get_pick_card`, then a reach check (404).
  3. Live set empty (404).
  4. Not pending (409).
  5. The choose or none branch.
- **choose.** It runs in one `with conn.transaction():` holding three steps:
  1. `get_live_by_stable_id(conn, sid, for_update=True)` and `drop_reason`, else raise 409 `item no longer open`;
  2. `patch_action_item`, raising unless it returns 200;
  3. `resolve_pick_card`, raising 409 `card already decided` on None.
  
  Reuse `_SuggestionNotApplied` or a sibling exception with the same shape.

- [ ] **Step 1: Write the failing unit tests.**
  - `test_get_hides_unreachable_sites_and_other_tenants`
  - `test_get_site_param_out_of_reach_403`
  - `test_moved_site_candidate_dropped`: also covers gone, terminal and same.
  - `test_card_with_no_live_candidate_omitted`
  - `test_one_live_candidate_still_a_card`
  - `test_can_choose_and_can_dismiss_flags`
  - `test_cap_applies_after_filtering`: 60 stale cards plus 1 live card; the live card is returned.
  - `test_choose_happy_path_patches_and_resolves`: asserts the PATCH body is `{"status": card.suggested_status}`, and the state is chosen with the right `chosen_stable_id`.
  - `test_choose_not_a_candidate_400`
  - `test_choose_rechecks_under_lock`: the FOR UPDATE re-read returns done; expect 409, no PATCH, no resolve.
  - `test_choose_cas_loss_rolls_back_409`
  - `test_none_by_site_role`
  - `test_none_by_editing_every_candidate`
  - `test_none_needs_authority_over_every_candidate`: 403.
  - `test_decide_unknown_or_malformed_card_404`
  - `test_decide_out_of_reach_404`
  - `test_decided_card_409`
  - `test_bad_body_400`: covers decision values and a malformed `stable_id`.
  - `test_logs_never_carry_quote_or_text`
- [ ] **Step 2:** Run the unit file. Expected: failures.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the unit file, `tests/unit/test_status_suggestion_endpoint.py`, and every unit test that calls `_action_item_edit_denial` (`git grep -l _action_item_edit_denial tests`). Expected: all pass.
- [ ] **Step 5: Integration test** `test_choose_cas_loss_leaves_no_audit_row`, run through run-tests.sh:
  - seed a site, an action item and a pick card;
  - resolve the card first;
  - call the choose handler;
  - expect 409, zero `content_edits` rows for the item, and the item status unchanged.
- [ ] **Step 6:** Run `-k "suggestion or pick or org_api"` across the unit tests. Expected: pass.
- [ ] **Step 7: Commit** `feat(org-api): read and decide status pick cards`.

### Task 4: Tasks-page card (fieldsight-ui)

**Files** (worktree `C:\Users\camil\wt\fsui-pickcard` from `origin/dev`):
- Modify: `scripts/api/actions.js` (add `listPickCards`, `decidePickCard`).
- Modify: `scripts/composites/action-item-row.js` (export `suggestionLabel` on `window.FieldSight`).
- Create: `scripts/composites/status-pick-card.js`.
- Modify: `scripts/pages/tasks.js` (mount above the list).
- Modify: the HTML that loads composites, so the new script loads before `tasks.js`. Find it with `git grep -l "action-item-row.js"`.
- Tests: `tests/status-pick-card.test.js`, `tests/pick-card-api.test.js`.

**Interfaces:**
- Consumes: Task 3's HTTP shapes.
- Produces:
  - `FS.api.listPickCards(siteId) -> Promise<{ok:true, cards} | {ok:false, reason, message?}>`;
  - `FS.api.decidePickCard(id, {decision, stable_id?}) -> Promise<{ok:true, state, status, action_item_id} | {ok:false, reason:'conflict'|'not_found'|'error', status?, message?}>`. It always resolves; the 409 comes from a thrown `err.status`.
  - `window.FieldSight.StatusPickCard`, a composite with props `{card, onDone(card, result)}`.

- [ ] **Step 1: Write the failing tests.**
  - `decidePickCard sends only decision and stable_id`
  - `decidePickCard maps a thrown 409 to conflict`
  - `decidePickCard maps _notFound to not_found`
  - `decidePickCard drops ticks on a successful choose`
  - `card renders quote as text not HTML`, using a quote that contains `<b>`
  - `candidate with can_choose false is disabled`
  - `None of these hidden without can_dismiss`
  - `Mark button disabled until a choice`
  - `conflict removes card with Already handled`
  - `tasks page refetches pick cards on site change`
  
  Follow `tests/status-suggestion-chip.test.js`'s window/FS fakes.
- [ ] **Step 2:** Run `node --test tests/`. Expected: the new tests fail.
- [ ] **Step 3: Implement.** Use the copy exactly:
  - "Which task was this?"
  - "Mark <label>", where the label comes from `suggestionLabel`
  - "None of these"
  - "not yours to change"
  - "Already handled"
  
  After a successful choose, find the row with `row.actionItemId === action_item_id` and call `patchRow(row.id, {status, status_suggestion: null})`.
- [ ] **Step 4:** Run `node --test tests/`. Expected: all pass.
- [ ] **Step 5: Commit** `feat(tasks): status pick card`. Push and open a PR to `dev`.

### Task 5: TEST end to end

- [ ] **Step 1:** Merge the backend PR to develop after CI is green; check the base is develop first. Wait for "Deploy FieldSight TEST (SAM)".
- [ ] **Step 2:** Merge the UI PR to `dev`. The TEST frontend is an Amplify build from `dev`.
- [ ] **Step 3: Seed a card on TEST.**
  - Use a scratchpad Python script that calls `status_pick_cards.insert_pick_card` through the Data API.
  - Site `d104ebbe-e294-4eb5-95bf-b153b623aa14`.
  - Candidates are 2 live open action items' stable_ids. Pick east PVC and unit 3 from A (`sid6fcef858…`) and read their stable_ids from `action_items`.
  - Status `done`, quote "That one's sorted.", run `manual-e2e-1`.
- [ ] **Step 4: Check in the browser** (Chrome, TEST frontend, Tasks page, logged in as the user):
  - the card shows both candidates;
  - choose one;
  - verify `content_edits` has a row with actor = the user, and the card state is `chosen`;
  - seed a second card and choose "None of these"; verify the state is `none` and no status change;
  - take light and dark screenshots.
- [ ] **Step 5:** Revert the chosen item's status by hand through the Tasks UI, so the twin-trap fixtures stay usable.
