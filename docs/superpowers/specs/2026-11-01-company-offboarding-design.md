# Company offboarding (exit without deletion) — design

Date: 2026-11-01 (rev 2, after Fable builder review). Status: for owner review. Scope:
fieldsight-pipeline (backend, built here); fieldsight-ui changes are described, not built.

Decisions are labelled: **owner** (stated by the owner), **owner-level, main agent** (taken by the
main agent under the owner's standing instruction to choose the long-term reasonable option; the
owner can overturn them), **my call** (design detail).

## 1. Intent

The roadmap item "per-company delete" was mis-named. Owner ruling (2026-10-26/27): when a
company leaves FieldSight, **nothing is deleted**. Its people can no longer sign in, its devices
can no longer upload, billing stops, and everything it captured is kept and can be exported. If
the company comes back, it is switched back on and resumes where it left off.

Today the only tool is per-member archive (`POST /api/org/members/{sub}/archive`), one person at a
time, which does not touch the Cognito login and is enforced only by org-api (§3).

## 2. Goals, non-goals, anti-goals

Goals
- One platform-admin action takes a whole company out of service; one brings it back.
- After the action no member of that company can read or write customer data through any
  DB-backed API, within one request; the remaining non-DB paths are bounded (§5.1).
- Devices signed in as that company's users cannot obtain upload URLs or open sessions.
- No customer email goes to an inactive company.
- Complete export (files + structured records) at exit time and on request later.
- Every state change and every Cognito side effect is audited.

Non-goals
- Deleting data, S3 objects, Cognito users or Aurora rows (a future legal "erase on request"
  feature must not share this path).
- Automated billing. There is no billing system in the code (no plan/seat/subscription fields);
  this design records the billing stop date for invoicing to read.
- Self-service exit by the customer's own admin (v1 is platform_admin only — my call).

Anti-goals
- Any data loss, including in-flight uploads at the moment of exit.
- Any OTHER company losing access to its own data.
- Silent partial exits: a path that keeps working for an exited company without anyone seeing it.

## 3. What exists today (recon, develop @ 2026-11-01)

- `companies` (0002 + 0049): `id, name, industry, created_at, voiceprint_consent_basis`. No status.
- `users.archived_at` / `memberships.archived_at` (0005). `users.archive_user` archives the user
  and all their memberships; `unarchive_user` restores the user only, not the memberships
  (src/repositories/users.py ~275–300). Cognito login is not touched.
- `archived_at` is enforced only by org-api's caller guard (src/lambda_org_api.py ~632). rag-search,
  ws-connect, voice-resolve, `lambda_check_gaps_db.py:91` and `checklist_reports.py:147` resolve a
  user with `users.get_user_by_sub` and do not check it.
- The legacy `ApiFunction` (src/lambda_fieldsight_api.py:108–118, `get_caller_identity`) has **no
  DB and no VPC**: it reads authorizer claims only. Ask/search reach Aurora through rag-search
  (caller_sub → rag-search). `/api/ask/corroborate` and `/api/ask/voice` STT never touch a DB.
- ask-agent's web offer (src/lambda_ask_agent.py ~1498–1515) only skips the web path when the
  retrieval result carries `error`; an empty result without `error` would go to the web.
- Cognito pool `ap-southeast-2_q88pd6XXr`, client `fieldsight-web-client`: refresh token 30 days,
  ID/access token default 60 minutes, revocation enabled. The API Gateway Cognito authorizer
  checks signature and expiry only, so an ID token in hand keeps passing for up to 60 minutes after
  a disable. Org-api's IAM has `AdminCreateUser`, `AdminGetUser`, `ListUsers` (template ~2949–2953)
  and nothing that disables or signs out.
- Devices sign in as a user (Cognito or QR login). Uploads: `POST /recordings/upload-url`
  (presigned PUT, `PRESIGNED_URL_EXPIRY`), then S3 events. `/device/status` and the heartbeat run
  before the caller guard by design.
- Customer email senders: `lambda_session_finalize` (stop/summary email, ~1104–1109, reached via
  `lambda_finalize_claim` ~346–361 which resolves recipient/folder/site), `lambda_session_report.
  _send_email` (~620), `lambda_extraction_backlog` external-member notices (~518–535). The report
  generator sends no email. `lambda_device_report` mails the operator, not customers.
- Report generator per-company summaries: `_group_by_company` (~1765–1788) groups folders by home
  company and already excludes `has_external` folders.
- Deploys run `sam deploy` **before** the migration step in both workflows (deploy.yml ~279 vs
  ~410), so new code runs for a while against the old schema. Highest migration: 0091.
- Export: `POST /api/org/companies/{id}/export` → `company_export.py` (S3 files of the company's
  `lake_objects` only, tombstones honoured, fails closed). No Aurora records. org-api presigned
  links are 900 s.
- Migrations do not GRANT (single DB role).

## 4. States and data model

`companies.status` text NOT NULL DEFAULT `'active'`, CHECK in (`active`, `suspended`, `exited`),
plus `status_changed_at timestamptz`, `status_reason text`, `billing_stopped_at timestamptz NULL`,
`exited_at timestamptz NULL` (first exit; kept on reactivation). Migration **0092**, additive.

| State | Logins/API | Uploads | Customer email | Data | Late uploads processed |
|---|---|---|---|---|---|
| `active` | yes | yes | yes | kept | — |
| `suspended` | blocked | blocked | **none** | kept | yes (transcribed/extracted) |
| `exited` | blocked | blocked | **none** | kept | yes |

`suspended` and `exited` enforce the same blocks (my call); they differ in intent and billing
(`exited` sets `billing_stopped_at`; `suspended` only when the operator asks). A suspended
company's late uploads are still transcribed and extracted, but no email or report goes out
(**owner-level, main agent**). There is no `deleted` state.

Audit: `company_status_events (id, company_id, from_status, to_status, reason, actor_user_id,
actor_sub, at, detail jsonb)`, append-only, written in the same transaction as the status change.
Cognito side effects are audited per user in `company_status_cognito (event_id, user_sub, action
'disable'|'signout'|'enable', state 'pending'|'done'|'failed', attempts, last_error, updated_at)`
(my call: a row per user per action, so retries are idempotent and visible).

## 5. Enforcement

### 5.1 Application gate (authoritative)

`tenancy_status.caller_blocked(conn, caller) -> reason | None` returns `company_inactive` when the
caller's home company is not `active`, `account_archived` when the caller is archived, else None.
**platform_admin is exempt** (**owner-level, main agent**).

Schema-safe read: `get_user_by_sub`'s hot SELECT is **not** changed. The gate reads
`companies.status` in its own query inside a savepoint (pattern: `_stamp_last_seen`,
src/lambda_org_api.py:5045) and treats a missing column (the window between `sam deploy` and the
migration) as `active`. One read per request.

Called at **caller resolution** (not at lookups of other users) in: org-api dispatch (replacing the
archived-only check; `GET /me` stays allowed so the UI can show the notice), rag-search (all
resolve sites), ws-connect, voice-resolve, `lambda_check_gaps_db.py:91`, `checklist_reports.py:147`.
A blocked rag-search returns `{"chunks": [], "error": "company_inactive"}` so ask-agent's web offer
is skipped (it only skips on `error`) and the UI gets a distinguishable error.

Known gap (**owner-level, main agent**): `/api/ask/corroborate` and `/api/ask/voice` STT run in the
DB-less legacy `ApiFunction` and stay usable for at most the ID-token lifetime (≤60 min) after the
exit; they reach no customer data, only model spend. Accepted for v1 and logged; follow-up is an
in-VPC identity hop for those routes.

Whitelist test: a test enumerates every call of `get_user_by_sub` in `src/` and classifies each as
caller resolution (must call the gate) or target lookup (must not); an unclassified call fails.

### 5.2 One choke point for live-company filtering

Everything that lists or reaches sites/memberships goes through one helper that drops rows whose
**site company** is not `active` for non-platform-admins: the memberships helpers,
`scope.visible_scope` (src/repositories/scope.py:22), `sites.list_company_sites` /
`list_all_sites`, ws broadcast recipients, `_capture_site`, `get_me`. Own-folder reads follow the
same rule: a person from another company loses access to their own recordings made on an inactive
company's site (filtered by the recording's site company), restored on reactivation
(**owner-level, main agent**).

### 5.3 Cognito

The status change and its audit row commit first. Then an async LakeOps-style task disables and
globally signs out every user whose **home** company is the company, writing one
`company_status_cognito` row per user per action; failures are retried idempotently and stay
visible (my call). Reactivation enables the **union of users disabled by all events since the
company was last `active`**, so a user disabled for an unrelated reason before that is not
re-enabled. Users are never deleted, so no re-invite is needed. IAM: add
`cognito-idp:AdminDisableUser`, `AdminEnableUser`, `AdminUserGlobalSignOut` to the function that
runs the task (next to the template ~2949–2953 block).

Team page: the "has signed in" badge must use Cognito `Enabled`, not `UserStatus` (a disabled user
keeps `CONFIRMED`) (src/lambda_org_api.py ~6390).

### 5.4 Devices and sockets

Uploads need a signed-in, unblocked caller, so §5.1 stops new upload URLs and session opens.
`/device/status` and the heartbeat stay open (hardware recovery; no customer data). Presigned PUTs
issued before the exit land and are processed (no in-flight loss). Device queues drain on
reactivation. WebSocket: at exit, delete the company's `ws_connections` rows so no broadcast
reaches them; sockets become inert (my call); ws-connect refuses new connections via the gate.

### 5.5 Email

Gate customer email on the **site company** of the item being mailed (falls back to the
recipient's home company for site-less items):
- `lambda_finalize_claim` (~346–361): an inactive company's claim skips the email path (stop email
  suppressed — **owner-level, main agent**); finalize work itself still runs;
- `lambda_session_report._send_email` (~620);
- `lambda_extraction_backlog` external-member notices (~518–535): held, not sent and not expired
  while the company is inactive (my call).
Report-generator skips are keyed by the site's company (per-company summaries via
`_group_by_company`; per-user daily reports by the report's site company), not the person's home
company.

### 5.6 Writes into an inactive company

Refuse with 409 `company_inactive`: `create_member`, `add_external_member`, `ensure_membership`
revive, member `unarchive`, site create/patch — anything that adds people or access to an inactive
company (my call).

## 6. Cross-company effects

- **B's people external on A's sites, A exits.** Under project-owned tenancy those sites and what
  was captured there are A's. B's people lose access (including own-folder reads of recordings
  made there, §5.2) until A comes back; their memberships are kept, hidden. B's own data is
  untouched.
- **A's people external on B's sites, A exits.** Their logins are disabled; everything they
  recorded on B's sites stays B's and fully visible to B.
- **platform_admin** keeps full access to an inactive company and cannot change its own company's
  status (the endpoint refuses the caller's own company — **owner-level, main agent**).

## 7. Export

At exit the operator runs the existing file export plus a new **structured records export**
written under the same `exports/{company_id}/{export_id}/` prefix as `records/*.jsonl` (one file per
table: company, sites, users without secrets, memberships, sessions, topics, action_items with
status history, decisions, findings, observations, compliance resolutions, programme rows, project
document index) and a `README.txt` (my call). Selection is a whitelist by the company's ownership,
re-checked per row, never another company's rows.

Handover links: an **operator-side script** presigns the export parts with the operator's own
long-lived profile (up to 7 days) for sending to the customer; org-api keeps its 900 s links and
the Lambda role never issues long links (**owner-level, main agent**). Prerequisite (owner; the prod
bucket is hand-managed): `AbortIncompleteMultipartUpload` lifecycle rule on `exports/`.

## 8. Comeback

`POST /api/org/companies/{id}/status {"status":"active","reason":...}` (platform_admin): status +
audit row commit; the async task enables the union of users disabled since the company was last
active (§5.3); nothing else changes. Individually archived members stay archived. Devices resume on
next sign-in. Fix: `users.unarchive_user` restores the memberships closed at the same `archived_at`
as the user.

## 9. API and UI

Backend (org-api, platform_admin only, never the caller's own company):
- `POST /api/org/companies/{id}/status` `{status, reason, stop_billing?}` → new row + side-effect
  summary; idempotent.
- `GET /api/org/companies/{id}/status-events` → audit incl. per-user Cognito rows.
- `GET /api/org/companies` includes `status`.
UI (described): Platform admin › Companies status badge and Suspend / Mark exited / Reactivate with
required reason and a confirmation naming the affected user and device counts; export links. A
blocked member sees one full-page notice driven by `company_inactive` / `GET /me`.

## 10. Rollout

Flag `COMPANY_STATUS_ENFORCE`: off = shadow (log `COMPANY_GATE_WOULD_BLOCK`, block nothing). Order:
code + migration 0092 to TEST (flag off) → throwaway company on TEST with owner-alias users only
(TEST sends real email) → flag on TEST → prod release (flag off; a few days of shadow logs showing no
active company is ever blocked) → owner turns it on. The first real prod exit is an owner action.

## 11. Test plan

- Unit: gate reasons × states × archived; platform_admin exempt; missing column → active; status
  endpoint auth (platform_admin only, refuses own company), idempotency, audit in same transaction;
  Cognito task per-user rows, retry idempotency, reactivation union; unarchive restores memberships;
  rag-search blocked shape `{"chunks": [], "error": "company_inactive"}`; whitelist test (§5.1).
- Integration (local Postgres): choke point hides inactive-site rows from external members and
  own-folder reads; the other company's own sites stay visible; records export returns zero foreign
  rows with cross-memberships seeded; writes into an inactive company refused.
- TEST end-to-end: exit → sign-in fails; an existing session gets `company_inactive` on the next
  request; no upload URL; an in-flight presigned PUT lands and is ingested; no stop email;
  reactivate → access returns, memberships intact.

## 12. Owner questions

- Q1. Who receives the export at exit? (This design: owner sends operator-presigned links by hand.)
- Q2. Should `suspended` stop billing by default, or only `exited`? (This design: only `exited`.)
