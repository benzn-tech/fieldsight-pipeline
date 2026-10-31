# Company offboarding (exit without deletion) — design

Date: 2026-11-01. Status: draft for owner review. Scope: fieldsight-pipeline (backend, built here);
fieldsight-ui changes are described, not built.

## 1. Intent

The roadmap item "per-company delete" was mis-named. The owner's ruling (2026-10-26/27): when a
company leaves FieldSight, **nothing is deleted**. The company's people can no longer sign in,
its devices can no longer upload, billing stops, and everything it captured is kept and can be
exported. If the company comes back, it is switched back on and resumes where it left off.

Today the only tool is per-member archive (`POST /api/org/members/{sub}/archive`), done one
person at a time, which does not touch the Cognito login and is only enforced by org-api (see
§3). There is no company-level switch at all.

## 2. Goals, non-goals, anti-goals

Goals
- One platform-admin action takes a whole company out of service, and one brings it back.
- After the action, no member of that company can read or write anything through any API
  (org-api, legacy prod-api Ask/search, rag-search, voice WebSocket, voice-resolve), within a
  bounded, stated time.
- Devices signed in as that company's users stop being able to obtain upload URLs or open sessions.
- No scheduled job sends that company email or spends model tokens on it.
- A complete export (files + structured records) can be produced at exit time and on request later.
- Every state change is audited (who, when, why).

Non-goals
- Deleting data, S3 objects, Cognito users, or Aurora rows. (A future, separate "erase on
  request" feature may exist for legal reasons; it is out of scope and must not share this path.)
- Automated billing. There is no billing system in the code today (no plan/seat/subscription
  fields); billing is handled outside the product. This design records the billing stop date so
  invoicing can read it, nothing more.
- Self-service exit by the customer's own admin (v1 is platform_admin only — my call; see §10).

Anti-goals (a design that does any of these has failed, even if it "works")
- Any data loss, including in-flight uploads at the moment of exit.
- Any OTHER company losing access to its own data. In particular: a host company whose site had
  this company's people as external members keeps everything those people recorded on its site.
- Silent partial exits: a path that keeps working for an exited company without anyone seeing it.

## 3. What exists today (recon, develop @ 2026-11-01)

- `companies` (0002 + 0049): `id, name, industry, created_at, voiceprint_consent_basis`. No
  status column.
- `users.archived_at` / `memberships.archived_at` (0005). `users.archive_user` archives the user
  and all their memberships; `unarchive_user` restores the user only, **not the memberships**
  (src/repositories/users.py ~275–300). Docstring: "Cognito login is NOT touched (design)".
- Enforcement of `archived_at` exists only in org-api's caller guard
  (src/lambda_org_api.py ~632: `account archived` 403, except `GET /me`). **rag-search,
  ws-connect, voice-resolve and the legacy prod-api resolve the caller with
  `users.get_user_by_sub` and do not check `archived_at`** — an archived member can still use
  Ask/search/voice today. (Pre-existing gap; this design closes it for companies and members alike.)
- Cognito pool `ap-southeast-2_q88pd6XXr`, one app client `fieldsight-web-client`: refresh
  token 30 days, access/ID token default 60 minutes, token revocation enabled. The API Gateway
  Cognito authorizer validates the ID token's signature and expiry; it does not consult
  revocation. So disabling a login cuts new sign-ins and refreshes at once, but an ID token
  already in hand keeps passing the authorizer for up to 60 minutes. **Only an application-level
  check closes that window.**
- Devices (GrandTime F2SP) sign in as a user (Cognito, or QR login `POST /auth/qr/create` +
  public `/auth/qr/redeem`). Uploads go through `POST /recordings/upload-url` (presigned PUT,
  `PRESIGNED_URL_EXPIRY`), then S3 events drive ingest. `/device/status` and the heartbeat run
  **before** the caller guard by design (an unprovisioned device must still be heard).
- Export: `POST /api/org/companies/{id}/export` (platform_admin) → `company_export.py`: streams the
  company's `lake_objects` (S3 files only) into multipart ZIP parts + manifest under
  `exports/{company_id}/{export_id}/`, honours tombstones, fails closed. It does **not** include
  Aurora records (action items, decisions, observations, compliance resolutions, sites, members).
- Scheduled jobs that act per user/company: report generator (daily 16:00Z, 16:30Z, weekly,
  monthly), DeviceReport (16:00Z, sends email via SES), org-api 15:30Z/15:35Z tasks,
  ExtractionBacklog, FinalizeSweep, RecordingSegments, LakeOps hourly/10-min, NonWorkExpiry,
  FloorRecompute, VoiceReaper. Most only act on new data, so they go quiet by themselves once
  uploads stop; the ones that send email or run on a calendar regardless are DeviceReport, the
  weekly/monthly reports and the morning brief path.
- Tenancy (0085/0086, on prod): capture is attributed to the **site's** company. External
  members (`memberships.external`) are people of company B on a site of company A.

## 4. States

New column `companies.status` (text, NOT NULL, default `active`), check constraint over:

| State | Meaning | Logins | Devices/uploads | Scheduled email/model work | Data | Export |
|---|---|---|---|---|---|---|
| `active` | normal | yes | yes | yes | kept | platform_admin |
| `suspended` | temporary stop (e.g. unpaid) | **blocked** | **blocked** | **off** | kept | platform_admin |
| `exited` | the company has left | **blocked** | **blocked** | **off** | kept | platform_admin |
| (back to) `active` | reactivated | yes | yes | yes | kept, unchanged | — |

`suspended` and `exited` block exactly the same things (my call: one enforcement path, two
labels). The difference is intent and billing: `suspended` is expected to come back soon and
keeps the billing clock as the owner decides; `exited` sets `billing_stopped_at`. Both are fully
reversible. There is deliberately no `deleted` state.

Companion columns (my call): `status_changed_at timestamptz`, `status_reason text`,
`billing_stopped_at timestamptz NULL`, `exited_at timestamptz NULL` (first time it left; kept on
reactivation for history).

Audit: new table `company_status_events (id, company_id, from_status, to_status, reason,
actor_user_id, actor_sub, at, detail jsonb)`. Append-only; written in the same transaction as the
status change. `detail` records what the side effects did (Cognito users disabled N, failed [...]).

## 5. What "blocked" means, path by path

1. **Application gate (the authoritative one).** A single helper
   `tenancy_status.caller_blocked(conn, caller) -> reason|None` returns a reason when the caller's
   home company is not `active` **or** the caller is archived. Called right after caller
   resolution in: org-api dispatch (replacing the archived-only check), rag-search (all four
   resolve sites), ws-connect, voice-resolve, legacy prod-api (`lambda_fieldsight_api`), and any
   other lambda that resolves a Cognito sub to a user. Response 403 with
   `{"error": "company_inactive"}` (or `account_archived`) so the UI can show the right screen.
   `GET /me` stays allowed so the UI can render "your company's access has ended" instead of a
   broken app. This closes the 60-minute token window and the pre-existing archived-member gap.
   A unit test enumerates every handler that calls `get_user_by_sub` and fails if one does not
   call the gate (a whitelist test, per the tenancy lesson "blacklists miss rows").
2. **Cognito (defence in depth, and the only thing that stops refreshes).** On exit/suspend:
   `AdminDisableUser` + `AdminUserGlobalSignOut` for every user whose home company is the
   company. On reactivate: `AdminEnableUser` for exactly the users this action disabled (read
   from the audit `detail`), so a user disabled for some other reason earlier is not silently
   re-enabled (my call). Users are never deleted, so passwords, MFA and subs survive and the
   comeback needs no re-invite.
3. **Devices.** Uploads already require a signed-in caller, so (1)+(2) stop new upload URLs and
   session opens. `/device/status` and the heartbeat stay open (they run before the caller guard
   by design and carry no customer data) so we can still see where the company's devices are —
   which matters for getting hardware back. Presigned PUTs issued before the exit stay valid
   until `PRESIGNED_URL_EXPIRY`; those uploads are **accepted and processed** (anti-goal: no
   loss of in-flight data). Recordings still queued on a device stay on the device; on
   reactivation they upload normally. The device ledger gets a note per device (manual; the
   ledger is label-based today).
4. **Scheduled work.** The directory (`directory.py`) gains each company's status; the report
   generator, DeviceReport, morning brief and any other emailing job skip folders/users whose
   company is not `active`. Jobs that only react to new uploads need no change. Late uploads
   from (3) are still ingested and extracted (the data is kept), but no email or report is sent
   for an inactive company (my call — avoid emailing people whose access has ended).
5. **Billing.** `billing_stopped_at` is set on `exited` (and optionally on `suspended`, by the
   operator's choice in the request). Invoicing reads it; nothing else.

## 6. Cross-company effects (the anti-goal)

- **Company B's people who are external members on company A's sites, when A exits.** Under
  project-owned tenancy the site and everything captured on it belong to A. B's people lose
  access to A's sites (they are A's data, and A is out of service). B's own data — B's sites,
  B's people's site-less sessions, B's home-company views — is untouched. My call: their
  `memberships` rows on A's sites are left as they are (not archived), so a reactivation of A
  restores their access with no extra work; the gate hides A's sites from them while A is
  inactive (site-list and site-ACL queries add `companies.status = 'active'` on the site's
  company). Owner question Q1 below.
- **Company A's people who are external members on company B's sites, when A exits.** Their
  logins are disabled, so they lose access to B's sites too. Everything they recorded on B's
  sites belongs to B and stays fully visible to B (no change to B's reads). Nothing of B's moves.
- **Platform admin** keeps full read access to an inactive company (support, export).

## 7. Export

At exit the operator starts an export (the existing endpoint) and this design adds a
**structured records export** alongside the file export, written into the same
`exports/{company_id}/{export_id}/` prefix as `records/*.jsonl` (my call: JSON Lines, one file per
table, UTF-8, ISO timestamps, UUIDs as strings; plus a small `README.txt` describing each file):
company, sites, users (no Cognito secrets), memberships, sessions, topics, action_items (with
status history from content_edits), decisions, findings, safety/quality observations, compliance
resolutions, programme rows, project documents index, and the lake manifest the file export
already writes. Selection follows the same rule as the file export: rows owned by the company by
the site-company rule, never another company's rows (whitelist by `company_id`, re-checked per
row). Export stays platform_admin-only in v1; the customer receives it as presigned links the
operator sends (15-minute URLs today — v1 adds a longer-lived option of up to 7 days for the
handover, my call). Prerequisite already noted in `company_export.py`: the prod bucket needs the
`AbortIncompleteMultipartUpload` lifecycle rule on `exports/` (owner, hand-managed).

## 8. Comeback

`POST /api/org/companies/{id}/status {"status":"active","reason":...}` (platform_admin):
1. sets status `active` (audit row);
2. re-enables exactly the Cognito users disabled by the matching exit/suspend events;
3. nothing else — memberships, sites, archived members, voiceprints, settings are as they were.
Members who were archived individually before the exit stay archived. Devices resume with the
next sign-in (QR or password) and drain their local queues. The UI shows the company's history
including the gap.

Fix along the way: `users.unarchive_user` should restore the memberships its archive closed
(currently it leaves them archived). Scoped to memberships archived at the same `archived_at`
as the user (so a membership removed deliberately earlier stays removed).

## 9. API and UI

Backend (org-api, platform_admin only):
- `POST /api/org/companies/{id}/status` body `{status: active|suspended|exited, reason: str,
  stop_billing?: bool}` → 200 with the new row and the side-effect summary; idempotent (same
  status again = no-op 200).
- `GET /api/org/companies/{id}/status-events` → audit list.
- `GET /api/org/companies` includes `status`.
UI (described only): Platform admin › Companies gets a status badge, a "Suspend / Mark as
exited / Reactivate" action with a required reason and a confirmation that names the number of
users and devices affected, and links to start an export. A member of an inactive company who
opens the app sees a single full-page notice ("Your company's FieldSight access has ended.
Contact <owner contact> to restore it.") driven by the `company_inactive` 403 / `GET /me`.

## 10. My calls (owner can overturn any of these)

1. `suspended` and `exited` enforce the same blocks; they differ only in intent and billing.
2. v1 is platform_admin-only; no customer self-service exit.
3. Users are disabled in Cognito, never deleted; re-enable only those this action disabled.
4. Heartbeat/`/device/status` keep working for inactive companies (hardware recovery).
5. In-flight uploads are accepted and processed; no reports/emails for inactive companies.
6. External members' memberships on the exited company's sites are kept (hidden, not archived).
7. Structured records export as JSON Lines alongside the existing ZIP file export; up to 7-day
   links for the handover.
8. Fix `unarchive_user` to restore the memberships its archive closed.
9. The application gate also enforces per-member `archived_at` in every lambda (closes the
   pre-existing gap).

## 11. Migration

One migration (next free number on develop): add the companies columns with defaults (`active`,
all existing rows active — no backfill needed beyond the default), add `company_status_events`,
grant org-api read/write, other lambdas read on `companies.status`. Additive only; safe to apply
before code.

## 12. Rollout

Flag `COMPANY_STATUS_ENFORCE` (off → on), per stage like other flags (repo var + deploy-time
parameter). With the flag off the gate only logs `COMPANY_GATE_WOULD_BLOCK` (shadow), so we can
see on TEST and prod that no active company is ever blocked before turning it on. Order: TEST
migration + code (flag off) → exercise on TEST with a throwaway company (owner alias users only;
TEST sends real email) → flag on TEST → prod release (flag off, shadow logs for a few days) → owner
turns the flag on. The first real exit on prod is an owner action.

## 13. Test plan

- Unit: gate returns the right reason for active/suspended/exited × archived/not; whitelist test
  that every sub-resolving handler calls the gate; status endpoint authorization (only
  platform_admin; gm/admin of the company get 403); idempotency; audit row written in the same
  transaction; reactivation re-enables only the recorded users; unarchive restores memberships.
- Integration (local Postgres): site-list/ACL queries hide inactive companies' sites from
  external members and keep the other company's own sites visible; records export selects only
  the company's rows (seed two companies with cross-membership and assert zero foreign rows).
- TEST end-to-end: throwaway company with two owner-alias users and one external membership in
  both directions; exit → sign-in fails, an existing session gets `company_inactive` within one
  request, a device token cannot get an upload URL, an in-flight presigned PUT still lands and is
  ingested, no nightly email is sent; reactivate → everything returns, memberships intact.

## 14. Owner questions

- Q1. When a company exits, should other companies' people who were external members on its
  sites keep **read-only** access to what they themselves recorded there? (This design: no —
  under project-owned tenancy it is the exiting company's data; it comes back if the company does.)
- Q2. Who receives the export at exit, and should the product email it, or does the owner send
  the links by hand? (This design: owner sends by hand; no automated email in v1.)
- Q3. Should `suspended` stop billing too, or only `exited`? (This design: operator chooses per
  action; default only on `exited`.)
