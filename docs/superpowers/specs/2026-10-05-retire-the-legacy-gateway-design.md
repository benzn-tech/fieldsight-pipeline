# Sub-project 1 — retire the legacy gateway's data routes (design, 2026-10-05, rev 2)

Status: **design, revised after a fact-check against code and deployed config
(2026-10-05).** Part of `2026-10-05-tenancy-roadmap-design.md` (decisions D1–D6).
Spans `fieldsight-pipeline`, `fieldsight-ui`, and two pieces of prod infrastructure.

## Why

`lambda_fieldsight_api` (`fieldsight-prod-api`, gateway `ys94qy2tk0` `/api/{proxy+}`)
serves tenant data with **no company concept**. Measured worse than first described:

- `/api/actions` GET and `/api/actions/toggle` POST check **nothing about the caller**:
  any signed-in user of any company reads or writes any date's ticks.
- `/api/users` returns the **whole** frozen mapping (device, name, role, sites) to any
  signed-in user; `/api/sites` and `/api/site-users` read the same frozen file.
- `/timeline`, `/transcripts`, `/audio-segments`, `/video-segments`, presign,
  `/reports/history`: admin/gm read any folder (`can_access_user_data` → True, presign
  skips owner checks).

#1026 closed the lake-wide summary. This sub-project takes tenant data out of the
gateway, because it cannot be patched into correctness: it has no database (D2).

## A second, forgotten gateway

`khfj3p1fkb` → lambda **`fieldsight-api`** (last modified 2026-07-13) is still deployed
with the same Cognito authorizer, the same company-blind code, no `RAG_SEARCH_FUNCTION`,
and an old `fieldsight-ask-agent` that falls through to the company-blind S3 path. Zero
invocations in 60 days — but it answers any valid token. It is retired here too.

## Every route on the live gateway, and its fate

Traffic: prod, 14 days to 2026-10-05 (Logs Insights on `/aws/lambda/fieldsight-prod-api`).

| route | calls | source | ACL today | fate |
|---|---|---|---|---|
| `/api/actions` GET | 2559 | DynamoDB `fieldsight-audit` `ACTIONS#{date}` | **none** | UI stops reading the overlay (see Actions); then **410** |
| `/api/actions/toggle` POST | 1 | same | **none** | UI stops writing it; then **410** |
| `/api/actions` POST | — | *no handler* — the dispatcher runs `get_actions` for any method → 400 | — | UI `createAction` is dead today: delete it |
| `/api/timeline` | 777 | S3 | admin/gm any folder | web: `legacyReadFallback=false`; then **410** |
| `/api/dates` | 1 | S3 | admin/gm unrestricted | web: same flag (already gated); then **410** |
| `/api/sites` | 149 | frozen mapping | mapping | web → org `/sites` via an adapter; then **410** |
| `/api/users` | 26 | frozen mapping | **none** | web → org (see Users); then **410** |
| `/api/site-users` | 19 | frozen mapping | mapping roster | web → org `/sites/{id}/members`; then **410** |
| `/api/sites`, `/api/users` POST/PATCH | — | *no handler* (method ignored, list returned) | — | UI `createSite`/`createUser`/`updateUserRole` against these are dead: delete or repoint to org |
| `/api/transcripts` `/audio-segments` `/video-segments` `/media/presigned-url` `/reports/history` `/recording-stats` | ≈0 | S3 | admin/gm unrestricted | **410** (web already uses org) |
| `/api/reports/generate` | — | — | — | **already 410** (2026-09-15) |
| `/api/search` `/api/ask` `/api/ask/corroborate` | 78 | proxy → ask-agent → rag-search with `caller_sub` | in-VPC, by company | **keep**, made fail-closed (below) |
| `/api/ask/voice` | 10 | device → STT → RAG with `caller_sub` | same | **keep** (D6) |
| `/api/health` | 26 | — | — | keep |

After this sub-project the gateway holds **no tenant data**: it is an authenticated
proxy to Ask/search, whose decisions are made in-VPC against the directory.

## The proxies must fail closed

`lambda_ask_agent` takes the RAG path only when **both** `caller_sub` is present and
`RAG_SEARCH_FUNCTION` is set; otherwise it falls to a company-blind S3 path
(`load_report` / `load_transcripts` by a named user). Prod has the variable; the risk is
an empty `caller_sub`. Two guards:

1. Gateway: `/api/search`, `/api/ask*` refuse (401) when the authorizer yields no `sub`.
2. ask-agent: with no `caller_sub` it **refuses** rather than falling to the S3 path.
   The S3 path stays reachable only for an internal invocation that carries an explicit,
   non-user marker (if any internal caller needs it — step 1 finds out); otherwise it is
   removed in step 5.

## Actions: the overlay, and what happens to the ticks

The web deliberately treats done-ness as the **union** of two stores: Aurora
`action_items.status == 'done'` OR the legacy `ACTIONS#` overlay's `checked`
(`actions.js`: "~119 check-offs … DONE-NESS IS THE UNION OF BOTH STORES"). Readers of the
overlay: `compliance-aggregator.js`, `tasks-aggregator.js`, `user-activity-aggregator.js`,
`action-item-row.js`, `today-adapter.js`. They change **in the same release** as the 410,
or every one of them errors.

The legacy table (`fieldsight-audit`, prod only — TEST uses `fieldsight-test-audit`),
full scan 2026-10-05: 356 items = **151 `ACTIONS#`** + 193 `AUDIT#` (append-only tick
history) + 12 `SITE#…#DATE#…` (written by the report generator; not read by the gateway).
Of the 151 `ACTIONS#` rows, **136 are checked**. Of those:

- **17 are not action items** — the action part is `flag_N`, `obs_N` or `quality`
  (findings / observations). They cannot map to `action_items`.
- **26 rows (15 checked) carry no folder** — `TOPIC#n#ACTION#n` form. Every write since
  2026-07-21 is folder-less; the folder-bearing `USER#…` form came from an unmerged branch.
- 2 groups share identical text within a day.

**Ruling (D-A1): backfill only what maps unambiguously; preserve the rest as a record.**
- A checked row with a folder, an action part that is an action index, and exactly one
  `action_items` row whose normalised `text` matches within that (date, folder) — joined
  `action_items.topic_id → topics(report_date, user_id) → users.folder_name` — sets that
  item's `status` to `done`, unless it is already done.
- `checked_by` is a display name and `action_items.updated_by` is a Cognito sub; names are
  not unique, so `updated_by` is **left null**, and the write goes through the repository
  with a `content_edits` audit row (the `patch_action_item` path), noting the legacy
  `checked_by` and `checked_at`.
- Everything else (non-action rows, folder-less rows, ambiguous text) is **written to a
  frozen, company-scoped record** (`docs/` or an operator S3 object, never shown to
  customers) and listed in the run's report. Nothing is guessed.
- Cost if wrong: a handful of historic ticks on findings and folder-less days are no
  longer shown as ticked in the UI; the record keeps them.

**Write authority narrows** from "anyone" to org-api's `PATCH /action-items/{id}` rule
(admin/gm/platform_admin, a pm/site_manager of the task's site, or the assignee). Some
ticks a worker could set before will now 403. That is the fix, not a regression. The
UI's legacy toggle for id-less (report-sourced) items is removed with the overlay.

## Sites and users: an adapter, and no fake data

- `getSites` → org `GET /sites` (every role, `visible_scope` reach). **The id space
  changes**: legacy returns the mapping slug as `site_id`, org returns a UUID (with
  `slug`). Consumers (`today.js`, `compliance-aggregator.js`, `programme.js`,
  `search-palette.js`, the quality/safety create modals) get an adapter that returns both,
  and each is moved to the UUID; `today.js` already keeps both maps.
- `getUsers` → admin/gm/platform_admin: org `GET /members`. **Other roles** (`/members`
  is 403 for them): the union of `GET /sites/{id}/members` over the caller's sites. Note
  this is reach-gated, not per-author graded — a worker sees the site's members where the
  legacy roster showed self only. Ruling (D-A2): acceptable — names of people on your own
  project are not cross-tenant data — cost if wrong: a worker sees co-workers' names.
- `getSiteUsers` → org `GET /sites/{id}/members`.
- **No silent mock fallback.** Today `tasks-aggregator.js`, `evidence.js`, `today.js`
  catch a failed `getUsers` and substitute fixture data in live mode. In live mode a
  failure renders as an error/empty state, never as fake people.

## Order — instrument, harden, move, close, remove

1. **Instrument** (pipeline): one structured line per request **after** auth — route,
   method, role, `caller_sub` present (bool), User-Agent family. No tenant content. Ship,
   observe one working week. No route closes while an unexplained caller remains.
2. **Harden the proxies** (pipeline): the two fail-closed guards. Independent of 1.
3. **Move the web** (ui): sites/users adapter; overlay readers switch to
   `action_items.status`; legacy toggle and dead writers removed; `timeline`/`dates`
   already gated — set `FS_LEGACY_READ_FALLBACK=false` on Amplify `main` at release.
4. **Backfill** (pipeline operator task, `photo_collapse` pattern: hand-invoked, dry run
   unless `apply` is exactly `true`): dry run on prod is a read — mine; `apply=true` on
   prod is a write — the owner's.
5. **Close** the data routes with **410** naming the org replacement, in the same window
   as 3 reaching prod.
6. **Retire the forgotten gateway** `khfj3p1fkb` / `fieldsight-api` / old
   `fieldsight-ask-agent`: disable first (reversible), delete after a quiet week —
   infrastructure outside the SAM stack; **owner's action**.
7. **Remove dead code** one release after 5 is quiet: data handlers, the DynamoDB users and
   audit reads, the mapping reads, the ask-agent S3 path if step 1 found no internal caller.

## Tests that must go red without the change

- Gateway: each closed route → 410 naming the replacement, for admin, gm, worker; no
  `s3_client`/`dynamodb` call on a 410 path; proxies refuse an empty `sub`.
- ask-agent: no `caller_sub` never reaches `load_report`/`load_transcripts`.
- UI: `getSites`/`getUsers`/`getSiteUsers` go through `orgRequest`; a failed call in live
  mode yields no fixture data; no module reads the `ACTIONS#` overlay; the adapter maps a
  legacy slug and a UUID to the same site.
- Backfill: dry run writes nothing; apply writes only unambiguous matches, via the
  repository with an audit row and `updated_by` null; a second apply writes nothing;
  non-action, folder-less and ambiguous rows land in the record, not in `action_items`;
  matching is by text, so a reordered list maps each tick to the same task.

## Owner touch-points (everything else is mine)

- Approve each prod deploy in Actions.
- The prod `apply=true` of the backfill (after reading the dry run).
- Disabling, then deleting, the forgotten gateway and its lambdas.

## Out of scope

Frozen mapping's other readers (sub-project 2), project-owned data (3), lake keys (4),
Cognito pool split (S1), connection pooling (S2).
