# Decision card inbox (P1 web route): GET/POST /api/org/decision-cards

Status: rev 2, 2026-11-13 (rev 1 2026-11-12; Fable review folded in, see §9). Parent spec:
`2026-11-05-site-decision-loop-design.md` (rev 6), §5.3, §6, §7, §10, §12 P1.
Depends on: the read-time deletion fix on branch `fix/decision-cards-deletion-and-copy` (commit 0317715c and
successors; its PR must be merged to develop before this is built).
Contract source: the 前端优化v2 session's draft (2026-11-10 23:28Z) plus the backend's five notes, all accepted by
that session (digest folded to `{count,title,href}`; `source_label` derived from `rule_id`; `sub_label` null until
J4; `evidence[].audio` optional; POST `applied` = null). The owner had not confirmed the mockup when this was
written; the route ships behind a flag that is off by default, so a mockup change costs a route change.

## 1. What and why
P1 of the decision loop sends cards to people through a web inbox (§12). This spec is the backend half: one read
route returning the caller's own open cards, capped and ordered on the server, and one write route recording an
answer. Out of scope: email, timers/escalation, the follow-up actions an answer implies (mark done, set a date),
and the safety-gap "copy the company admin" (§7 known gaps).

## 2. Flag, parameters, environments
- Env var `DECISION_INBOX` on **OrgApiFunction only**: `off` (default) | `companies`. SAM parameter
  `DecisionInbox`, Type String, AllowedValues `['off','companies']`, Default `off`. (`internal` is NOT used:
  on this function `ACTION_PACKS=internal` already means platform_admin-only, `action_pack_view.route_allowed`.)
- Env var `DECISION_INBOX_COMPANIES`: SAM parameter `DecisionInboxCompanies`, Type String, Default `''`,
  AllowedPattern `^$|^[0-9a-fA-F-]+(,[0-9a-fA-F-]+)*$` (comma-separated company UUIDs, no spaces).
- Workflows: `DecisionInbox=${{ vars.TEST_DECISION_INBOX || 'off' }}` / `PROD_…` in deploy.yml / deploy-prod.yml.
  `DecisionInboxCompanies` is appended **only when the variable is set** (the conditional `_PARAM` pattern used for
  `ACTION_PACKS_PARAM`, because `--parameter-overrides` rejects a bare `Key=`). Never change an existing parameter
  default (SAM keeps previous values).
- Template pin test for both parameters, like `tests/unit/test_decision_scan_template.py` does for
  `SiteDecisionLoop` (AllowedValues `['off','shadow']`).
- No IAM change: org-api already reads the deletion mirror (S3_BUCKET) and Aurora.
- Both routes return **404** unless `DECISION_INBOX=companies` **and** `SITE_DECISION_LOOP=shadow` **and** the
  CARD's company is in `DECISION_INBOX_COMPANIES` (GET: cards of other companies are simply not returned; if the
  caller can see no allow-listed card source at all the response is still 200 with empty lists — 404 only for the
  two flag checks, so the web can distinguish "off" from "nothing for you").
- TEST: `TEST_DECISION_INBOX=companies`, `TEST_DECISION_INBOX_COMPANIES` = Test Project's company id. Prod stays
  `off` until the owner's P0 gate passes; that switch is the owner's.

## 3. Migration 0103 (additive)
`0103_decision_card_answers.sql` (number confirmed free across all remote branches at write time; re-check before
commit and take the next free number if another session took it):
```
ALTER TABLE decision_cards
  ADD COLUMN IF NOT EXISTS answered_by uuid REFERENCES users(id),
  ADD COLUMN IF NOT EXISTS answered_option text,
  ADD COLUMN IF NOT EXISTS answered_at timestamptz,
  ADD COLUMN IF NOT EXISTS answer_reason text;
```
Nullable, no default, no backfill, no index (lookups are by card_id PK). Status values are unchanged (`answered`
already exists in §5.3). Why a migration: options that are not decisions (remind, new date, reassign) must not
write a ledger row (§5), yet who answered what must be recorded.

## 4. GET /api/org/decision-cards
Response `200 {cards: [Card], digest: Digest|null, more: int}`.

**Card universe.** `decision_cards` rows with `status='open'`, `company_id IN (allow-list)`,
`rule_id NOT IN ('hub.fault', 'todo.overdue_backlog')` and `kind NOT IN ('fault','rule')` (fault cards stay on the
shadow page only), whose `recipients` contain the caller's user id. Query shape: filter on
`idx_decision_cards_site_status` by `company_id` + `status='open'`, then `recipients @> %s::jsonb`
(`[{"user_id": "<caller>"}]`). Bounded: one cards query, one membership query per distinct (company, site), one
sources pass (the shared check), one users query.

**Still a recipient.** For each card, the caller's stored recipient role must still hold:
- `pm` / `site_manager`: the caller appears with that role in
  `memberships.members_for_site(conn, card.company_id, card.site_id)` (this includes external members of the
  site, per project-owned tenancy, and excludes archived memberships/users). External members of an allow-listed
  company's site see that site's cards.
- `company_admin`: `caller.company_id = card.company_id` and `caller.global_role IN ('admin','gm',
  'regional_manager')` (`decision_hub.COMPANY_ADMIN_GLOBAL_ROLES`; regional_manager counts for every site, as in
  the hub — both change together if that changes).
- any other stored role (e.g. `ops`): not served.
`recipients()` stores one role per user (first in ROLE_ORDER); a pm who is also company admin, removed from the
site, loses a card that was also routed to admins. Accepted for P1.

**Inactive companies.** Card rows go through `tenancy_status.inactive_site_ids` / `drop_inactive` evaluated for
the CARD's company. While `CompanyStatusEnforce` is off these return None and cards are served, like every other
read.

**Digest.** Built from the caller's open `todo.overdue_backlog` cards (same universe/recipient rules otherwise).
For each such card's site, the count is computed LIVE: the stale subset of `action_items.overdue_open(conn,
today_nz)` for that (company, site), stale decided by `decision_hub.is_late` under `RULES["todo.overdue"]` on the
per-item event exactly as `lambda_decision_scan._emit_overdue_item` builds it (share that builder; do not restate
the arithmetic). So the number follows today's backlog even on a day the scan has not run yet, and drops to 0 when
the backlog clears (parent §6.5: refreshing is P1). One `overdue_open` call per request. A site with 0 is left out.
`digest = {count: sum, title, href: "#/tasks?filter=overdue-old"}` where title is
`"1 overdue to-do more than 3 days old"` / `"N overdue to-dos more than 3 days old"`, plus, when more than one site
contributes, `" on <A>, <B>"` (site names sorted, at most 3, then `" and N more sites"`). `null` when no site
contributes. The digest is not a card, does not count toward the cap and cannot be answered. (`href` to confirm
with 前端优化v2.)

**Cap and order.** Order by: urgency `injury` first; `deadline_at` ascending, nulls last; `created_at` ascending;
`card_id`. Return the first 3; if more than 3 injury cards exist, all injury cards are returned. `more` = ordered
open cards beyond those returned. This is a page size over the open set, not a per-day delivery ledger (nothing
writes `delivered_refs` yet); answering a card lets the next one in.

**Quiet hours.** A pull inbox shows cards as soon as they exist; parent §6.6 quiet hours apply to push/email.

**Card shape.**
```
{card_id, kind, site_id, site_name, created_at (ISO UTC),
 source_label, sub_label: null,
 question, item: {stable_id, title} | null,
 options: [{option_id, label, recommended, reason_required?}],
 evidence: [{speaker_name: null, said_at: ISO|null, quote, audio: null}]}
```
- `kind`: as stored (P0 rules write `choose` and `fact`).
- `source_label` (one table in code, unit-tested): `todo.overdue` → `"Overdue since <d MMM>"`, date = payload
  `deadline` parsed with `date.fromisoformat(v[:10])` (as `decision_rules._parse_date`) + 1 day; absent or
  malformed → `"Overdue"`. `checklist.gap` → `"Missing from the inspection"`. Anything else → `"From the site"`.
- `item`: `todo.overdue` → `{stable_id: subject_ref, title: payload text}`; `null` for other rules, and `null` when
  the card's source is removed (a pointer to a deleted item is useless to the UI).
- `options`: stored `id` → `option_id`; `reason_required` passed through when present. A card with no option is
  skipped (logged).
- `evidence`: stored spans `{quote, at}` → `{speaker_name: null, said_at: at, quote, audio: null}`; `at` is null for
  every P0 span; at most 2.
- **Deletion and redaction.** The check is `_removed_source_cards(conn, rows) -> (removed, due)` from the fix
  branch (S3 mirror + Aurora tombstones + walk part sessions + action-item visibility, fail closed). The rewording
  now inline in `_decision_card_views` (question via `decision_rules.removed_overdue_question(due)`, person-free
  remind label `_GENERIC_REMIND`, evidence `[]`) is extracted into ONE pure helper
  `decision_card_copy.redact(row, removed: bool, due) -> (question, options, evidence)` that both the shadow view
  and the inbox call. The inbox never has its own copy. Exact served strings are those of that helper.

## 5. POST /api/org/decision-cards/{card_id}/answer
Body `{option_id: str, reason?: str}`. Every 400/404 is decided **before** any write (an `error()` return is a
clean exit and the request transaction commits).
- 404: either flag check fails; card not found, not in the allow-list, in the excluded universe (fault/rule/digest),
  or the caller is not a current recipient (same visibility as GET; existence is not leaked).
- 400: `option_id` not one of the card's options; `reason` (after strip) missing when the option has
  `reason_required`; `reason` over 500 chars.
- **Which answers are decisions.** Ledger rows are written only for options that settle the subject:
  `todo.overdue`: `done` → `decided`, `not_needed` → `declined`; `checklist.gap`: `accept` → `decided`.
  `remind`, `new_date`, `reassign` and any unknown option write **no** ledger row. Reason: the hub treats a
  `decided`/`declined` row on (company, site, subject_ref) as final (`lambda_decision_scan._handle` →
  `decision_ledger.decided`) and never cards that subject again; a reminder is not a decision and must not silence
  the item. A `done` answer silences the item's future overdue cards even though, in this rev, nothing marks the
  to-do done (applied is null; the follow-up action is later P1 work) — that is the person's statement and is
  recorded as such.
- **First answer wins**, one transaction:
  `UPDATE decision_cards SET status='answered', answered_by=%s, answered_option=%s, answered_at=now(),
  answer_reason=%s WHERE card_id=%s AND company_id=%s AND site_id=%s AND status='open' RETURNING …`
  - 1 row: if the option is a decision, insert one `decision_ledger` row: company_id, site_id, card_id, subject_ref
    from the card; `decision` = option_id; `kind` per the table above; `decided_by` = caller; `authority_basis` =
    the recipient role the caller matched; `evidence` = the card's evidence as served (post-redaction) plus, when
    a reason was given, a final element `{"kind": "reason", "text": <reason>}`; `source='card'`. The ledger is
    append-only and permanent (retention deferred, parent §10). Return `200 {status:"answered", applied:null}`.
  - 0 rows: re-read the card (same tenant filter). If `status='answered'` and `answered_by` = caller and
    `answered_option` = option_id → `200 {status:"answered", applied:null}` (retry-safe). Otherwise
    `409 {status:"stale"}` — covers another person's answer AND the scan's concurrent `supersede` (a gap closing
    between GET and POST; the common case on TEST).
- Logged at INFO: card_id, option_id, caller id (no question text).

## 6. Effects on P0 machinery
- **Hub.** Unchanged. A decision answer silences the subject via `decision_ledger.decided`; a non-decision answer
  frees the card key (dedupe applies only while a card with the key is open), so the next day's `todo.overdue`
  event may raise a fresh card while the item is within its first three overdue days.
- **Replay judge** (`tools/decision_replay.py`): relax the "P0 has only open / superseded" status check to allow
  `answered`, and relax the per-key "older card not superseded" check (`decision_replay.py:363`) to accept
  `superseded` OR `answered` for an older card on a key (the partial unique index and the "more than one open
  card" check still guarantee one open card per key); keep `_decided` (subject-based) as is; add invariants: an `answered` card has non-null
  `answered_by/answered_option/answered_at`; it has at most one `source='card'` ledger row, and exactly one iff its
  option is a decision; an `open` card has none and null answer columns. Add mutations the judge must catch:
  "ledger row written but card left open", "decision option answered with no ledger row", "ledger row written for
  a remind answer".
- **Shadow route** unchanged except it calls the shared redaction helper; answered cards stay listed and
  labellable; labels never touch status.
- **Gate metric.** p95 cards per recipient counts created cards; unaffected.

## 7. Known P1 gaps (stated so the owner's gate is not surprised)
- Safety-gap accept: parent §6.2 copies the company admin; once any recipient answers, the card leaves every inbox
  and nothing is emailed. The ledger row (with reason) is the record until the email piece of P1.
- Timeouts and pm → admin escalation: not here (P1 timers piece).
- `done` / `new_date` do not change the to-do (applied = null).

## 8. Tests
Unit (pure): source_label table incl. malformed deadline; digest title (1, N, multi-site, >3 sites); ordering incl.
injury overflow; option mapping; decision-option table; reason validation; redact helper (shared output for both
views). Integration (local Postgres, real SQL): flag off ×2 → 404; company not allow-listed → not served;
recipient vs removed-from-site vs external member vs other company vs company_admin roles; fault/rule/digest
cards excluded; inactive company with CompanyStatusEnforce on; cap and `more`; digest count is live (more stale
items today than at card creation → today's number; backlog cleared → digest null); deletion check shared with the shadow route; POST: decision answer writes ledger,
remind writes none and the next day's event can card again, `done` silences later events; idempotent retry; 409
after another's answer and after supersede; 400s write nothing; two-connection test (A holds the UPDATE, B blocks
then gets 409; with A rolled back B gets 200); replay judge 0 findings after answers and each new mutation caught;
migration applies twice cleanly; template pin test.

## 9. Fable review (2026-11-13) folded in
B1 answer semantics vs `_handle` → §5 decision table, §6 judge rewrite. B2 fault cards → excluded (§4/§5).
S3 shared helper + `item: null` → §4. S4 scope by card company, external members, role names → §4. S5 parameter
wiring → §2. S6 `internal` collision → `companies`. S7 live digest count → §4. S8 safety copy → §7. S9 inactive
predicate → §4. S10 tenant-scoped UPDATE, 409 causes, validate-before-write → §5 (retry check now on the card's own
columns, no ledger index needed). S11 two-connection test → §8. NITs 12-21 → §4/§5 (at→said_at, `more`, quiet
hours, regional_manager, query shape, reason element, deadline parse, href to confirm, one-role note, AllowedValues).
Rev 2 re-review: APPROVE_WITH_CHANGES; R1 judge per-key check relaxed for `answered` → §6; R2 live digest count
(option b) → §4/§8.
