# Status pick cards: read and decide endpoints, Tasks-page card

Date: 2026-11-12. Revision 2, after the Fable review: every Critical and Important finding is adopted, as are the Minors M1–M10. Line: status suggestions / cross-session reconcile.

Builds on:
- `docs/superpowers/specs/2026-11-09-cross-session-status-reconcile-design.md` (§6 pick cards, §12 owner decisions);
- migration 0099 `status_pick_cards`.

## 1. Problem

The reconcile worker writes a pick card when one spoken status statement ("that one's done") could refer to 2-4 open items. Owner rule §12: **when uncertain, show a pick card; the system never chooses.** Today nothing reads the table, so on TEST the cards exist only in the DB and an uncertain statement just disappears.

## 2. Goal

Anyone who can reach the card's site sees the pending card on the web Tasks page, with every live candidate. A person who may edit a candidate can choose it: the status changes through the normal audited path and the card closes as `chosen`. A person with authority over the whole question can answer "none of these": nothing changes and the card closes as `none`.

## 3. Non-goals

- **The one inbox for all system cards** (decision-loop hub). This card is an interim surface on the Tasks page. The endpoints below stay when the inbox absorbs it; only the rendering moves.
- **Email.** Email carries no actions, because link scanners click them.
- **Mobile app, voice yes/no.**
- **Escalation and timeouts.** Parent §12.3 routes pick cards to the assigner once a hierarchy exists. Until then, the `none` rule in §4.2 is the **interim** authority rule. It is deliberately stricter than "anyone on the project", because a `none` is permanent: `insert_pick_card` is ON CONFLICT DO NOTHING in any state, so the question is never asked again.
- **Audio playback from the card.**
- **Prod.** Reconcile runs only on TEST, so no card is ever written on prod. 0099 may be absent there, which is guarded (§5.4).

## 4. Design

### 4.0 Shared candidate rule

Move `_canon_uuid` and `_candidate_drop_reason` (`gone` / `moved_site` / `terminal` / `same`) out of `lambda_suggestion_writer.py` into a new `src/status_candidates.py`, as `canon_uuid(value)` and `drop_reason(row, site_id, suggested_status)`. The writer imports them, with no behaviour change. org-api uses the same functions. A candidate is **live** when `drop_reason(get_live_by_stable_id(sid), canon(card.site_id), card.suggested_status) is None`.

### 4.1 Read: `GET /api/org/status-pick-cards[?site=<id-or-slug>]`

- **Reach.** Reach is `card.site_id in _allowed_site_ids(conn, caller)` and nothing else. The per-item company pin is `_action_item_edit_denial`'s job. `?site=` goes through `_resolve_site_param`, so it gets the existing 400/403/404 and slug behaviour. Without `?site=`, every reachable site is included.
- **Reading pending cards.** `pending_for_sites(conn, site_ids)` reads all pending cards for those sites, newest first. Empty in gives empty out. The partial index makes this cheap, and cards are rare.
- **Candidates.** The handler resolves each card's candidates and keeps only the live ones, in stored order. A card with no live candidate is omitted. A GET never writes. The list is capped at **50 cards shown**, after filtering.
- **Who sees which candidates.** Every live candidate is shown to everyone with reach. Hiding one would narrow the choice for the person, which §12.2 forbids. Each candidate carries `can_choose`, which is whether the caller passes `_action_item_edit_denial` on it.
- **Query count.** Hoist `_allowed_site_ids` and `memberships.caller_site_roles` once per request. `_action_item_edit_denial` gains optional `allowed_site_ids=None, site_roles=None` keyword parameters. They default to fetching, so the existing callers are untouched.
- **Response.**

```json
{"cards": [{"id": "…", "site_id": "…", "site_name": "…", "status": "done",
  "quote": "…", "created_at": "<iso>", "can_dismiss": true,
  "candidates": [{"stable_id": "…", "action_item_id": "…", "text": "…", "status": "open",
                  "responsible": "…", "task_no": 12, "can_choose": true}]}]}
```

  - `task_no` comes in one batch call (`_task_numbers`).
  - `site_name` comes from the site rows already read for reach.
  - `can_dismiss` follows the `none` rule in §4.2.

### 4.2 Decide: `POST /api/org/status-pick-cards/{id}`

The body is `{"decision": "choose", "stable_id": "<uuid>"}` or `{"decision": "none"}`.

The handler takes these steps in order:

1. **Validate the body.**
   - `decision` must be `choose` or `none`, else 400.
   - For `choose`, `stable_id` must canonicalise with `canon_uuid`, else 400.
2. **Read the card.** `get_pick_card(conn, id)` returns None for a malformed or unknown id, or when the table is missing. Answer 404 when it is None, or when `card.site_id` is not in `_allowed_site_ids`. This is ACL-first, as in `decide_status_suggestion`.
3. **Compute the live set**, exactly as GET does. If it is empty, answer 404: the card does not exist for this caller, matching GET hiding it.
4. **Check state.** If `card.state != 'pending'`, answer 409 `card already decided`.
5. **choose.**
   - The body `stable_id` must be in the live set, else 400 `not a candidate`.
   - Then, in ONE `with conn.transaction():`:
     - re-read the item with `get_live_by_stable_id(sid, for_update=True)` and re-apply `drop_reason`; if it is no longer live, raise 409 `item no longer open`;
     - call `patch_action_item(conn, caller, item.id, {"status": card.suggested_status})`. It runs the edit ACL and writes the content_edits audit row with the person as actor. A non-200 response is raised and rolls back;
     - call `resolve_pick_card(conn, id, 'chosen', sid, caller.id)`. This is the CAS `WHERE id AND state='pending'`. If it returns None, raise 409 `card already decided`, which rolls back the PATCH.
   - Use the same raise-to-roll-back exception shape as `_SuggestionNotApplied`.
6. **none.**
   - The caller needs one of two things:
     - admin/gm of the site's company, or `pm`/`site_manager` of the site. That is the same tier test as `_action_item_edit_denial` minus the assignee tier;
     - or edit rights on **every** live candidate.
   - Otherwise answer 403.
   - Then `resolve_pick_card(conn, id, 'none', None, caller.id)`. If it returns None, answer 409.
7. **Respond** with `{"id", "state", "status": card.suggested_status, "action_item_id"}`. `action_item_id` is null for `none`.

Additions to `repositories/status_pick_cards.py`:
- `get_pick_card(conn, id)`;
- `pending_for_sites(conn, site_ids)`;
- `resolve_pick_card(conn, id, state, chosen_stable_id, user_id)`.

The reads run in a savepoint and degrade to None or [] when `tenancy_status.not_migrated(exc)`. Any other error is raised.

Dispatch: add the routes after the status-suggestion route (`lambda_org_api.py` ~1950), with the literal GET `/status-pick-cards` before the `^/status-pick-cards/([^/]+)$` POST.

### 4.3 The Tasks-page card (fieldsight-ui, branch from `dev`)

**API helpers** (`scripts/api/actions.js`):
- `listPickCards(siteId)`.
- `decidePickCard(id, body)`, which builds the body from the explicit fields `decision` and `stable_id`, because the API layer whitelists fields.
- Error handling follows `orgRequest`, which THROWS on 409 with `err.status`:
  - a 409 maps to `{ok:false, reason:'conflict', status:409, message}`;
  - the `_notFound` envelope maps to `{ok:false, reason:'not_found'}`;
  - anything else maps to `{ok:false, reason:'error', message}`.
- On a successful choose, call `dropTicksFor(action_item_id)`, as `decideStatusSuggestion` does.

**Composite** `StatusPickCard` (`scripts/composites/status-pick-card.js`):
- Contents:
  - the heading "Which task was this?";
  - the quote in quotes, rendered as text;
  - the status label;
  - a radio list of candidates showing `#task_no`, text and responsible;
  - candidates with `can_choose:false` are disabled, with the note "not yours to change";
  - a "Mark <status>" button, enabled once an enabled choice is selected;
  - a "None of these" button, shown only when `can_dismiss`.
- Results:
  - `conflict` or `not_found` removes the card and briefly shows "Already handled";
  - any other error stays inline.
- The status label: export `suggestionLabel` onto `window.FieldSight` from `action-item-row.js` and reuse it. Do not copy the map.

**Tasks page** (`scripts/pages/tasks.js`):
- The card list renders above the task list for `FS.siteContext.get()`, passed as `?site=`, and refetches on `siteContext.onChange`.
- After a successful choose, find the loaded row with `row.actionItemId === action_item_id`. If it is found, call `patchRow(row.id, {status, status_suggestion: null})`. Otherwise just remove the card.

Check light and dark in a real browser. The page is desktop-only.

### 4.4 Security and tenancy

- No new S3 prefix and no IAM change. The routes go through the existing `/api/org/{proxy+}`.
- Every status write goes through `patch_action_item`.
- Logs carry ids, `site_id` and counts only. Never log `quote`, `text` or `responsible`; `responsible` is a person's name.

## 5. Failure modes and edge inputs

1. **Two people decide at once.** The CAS fails the second with 409, and their PATCH rolls back.
2. **An item is changed by hand after the card was written.**
   - If it was closed or set to the same status, GET drops it, and a choose re-checks under `FOR UPDATE` and answers 409.
   - If it was moved to another site, it is dropped as `moved_site`.
3. **Mixed edit rights.** Everyone sees all live candidates. Only `can_choose` ones are selectable, and `patch_action_item` is the server-side gate.
4. **0099 is absent (prod).** GET returns `{"cards": []}` and decide returns 404, through `not_migrated`.
5. **Malformed ids.**
   - A malformed card id answers 404, matching the sibling `get_suggestion`.
   - A malformed `stable_id` in the body answers 400.
6. **Fifty stale cards.** The cap applies after filtering, so they never hide a newer live card.

## 6. Testing

**Unit tests (org-api):**
- reach: another tenant's site is invisible, out-of-reach `?site` answers 403, and decide answers 404;
- live filtering: gone, moved_site, terminal, same;
- `can_choose` and `can_dismiss`;
- choose:
  - the happy path;
  - a stable_id that is not a candidate;
  - an item closed between GET and decide (the FOR UPDATE re-check);
  - a CAS loss rolling back;
- none:
  - authority by site role;
  - authority by every-candidate edit rights;
  - a 403 when the caller can edit only one candidate;
- missing table;
- malformed ids;
- the cap after filtering.

**Writer regression:** the existing `test_lambda_suggestion_writer.py` passes unchanged after the move to `status_candidates`.

**Integration (local pgserver, real 0099)** for `pending_for_sites` (including empty in) and `resolve_pick_card`'s CAS. Also a decide inside an outer transaction where the CAS loses: it must leave no `content_edits` row. The fake conn never executes SQL, so this has to be an integration test.

**UI unit tests:** render, the disabled candidate, choose, none, the 409 and 404 removal, and the API-layer body fields.

**TEST end to end:**
- insert a card on the test site with real stable_ids through the repository;
- open the Tasks page and choose one, then verify the audit row and the card state;
- repeat with `none`.

## 7. Rollout

1. Backend PR to develop, then TEST.
2. UI PR to `dev`, then the TEST frontend.
3. Nothing on prod. Prod waits for R1 and reconcile's prod flag, both of which are the owner's.
