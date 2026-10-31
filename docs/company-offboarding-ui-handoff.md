# Company offboarding: hand-off to fieldsight-ui

Backend branch: `feat/company-offboarding` (fieldsight-pipeline). Spec:
`docs/superpowers/specs/2026-11-01-company-offboarding-design.md` (§9 API and UI, §10 Rollout).
Everything below was checked against the code (`src/lambda_org_api.py`, `src/tenancy_status.py`,
`src/repositories/company_status.py`, `src/company_status_cognito.py`, `src/lambda_rag_search.py`,
`src/lambda_ask_agent.py`, `src/ask_orchestrator.py`, `src/lambda_ws_connect.py`). All routes are
under `/api/org`. All error bodies are `{"error": "<message>"}`.

## 1. What the backend does

A company has a `status`: `active`, `suspended` or `exited`. Suspended and exited block the same
things; they differ only in intent and billing (`exited` always stops billing; `suspended` only
with `stop_billing: true`). There is no delete. Data is always kept.

When a company is not `active`, and only when enforcement is on:

- its home users get 403 `company_inactive` on every org-api route except `GET /me`;
- their Cognito users are disabled and globally signed out by an async LakeOps task (runs on a
  10-minute schedule, so allow up to about 10 minutes);
- their WebSocket rows are deleted and new WebSocket connects are refused;
- writes that would add people or access to that company get 409 `company_inactive`;
- its sites disappear from other companies' users (external members), including from
  `GET /me.site_ids` / `site_roles`;
- no customer email or emailed report goes out for it.

`platform_admin` is exempt from every block and keeps full access to an inactive company.

### Enforcement flag

`COMPANY_STATUS_ENFORCE` (backend env). Off = shadow mode, the default for the first release:

- the status can still be changed, and `GET /companies` / `GET /status-events` show it;
- nothing is blocked: no 403, no 409, no `blocked` field on `GET /me`, no Cognito disable (work
  rows stay `pending`), no sockets closed (`socketsClosed: 0`);
- the backend only logs what it would have blocked.

The UI must work in both modes. Do not infer "blocked" from the company status; react only to the
responses in §3 and §5.

## 2. Platform admin › Companies

All endpoints in this section are `platform_admin` only (other roles get 403
`platform_admin role required`). An unknown or non-uuid company id gets 404 `company not found`.

### 2.1 Status badge

`GET /companies` now returns `status` per company. Before migration 0092 every company reads
`active`.

```json
{"companies": [
  {"id": "3f0c6c1e-...", "name": "Acme Builders", "status": "active"},
  {"id": "9a71d2b4-...", "name": "Old Client Ltd", "status": "exited"}
]}
```

Suggested badges: `active` (no badge or neutral), `suspended` (warning), `exited` (muted/grey).

### 2.2 Actions: Suspend / Mark exited / Reactivate

`POST /companies/{id}/status`

| Action | Body |
|---|---|
| Suspend | `{"status": "suspended", "reason": "...", "stop_billing": false}` |
| Mark exited | `{"status": "exited", "reason": "..."}` |
| Reactivate | `{"status": "active", "reason": "..."}` |

- `reason` is required: a non-blank string, trimmed, at most 1000 characters.
- `stop_billing` is optional, must be a boolean (`null` counts as false). It only matters for
  `suspended`; `exited` always sets `billingStoppedAt`; `active` clears it.
- Show the action buttons by current status: `active` → Suspend, Mark exited; `suspended` →
  Mark exited, Reactivate; `exited` → Reactivate (and Suspend is accepted too, but has no use).

Errors, in the order the backend checks them:

| Status | Body `error` | When |
|---|---|---|
| 403 | `platform_admin role required` | caller is not platform_admin |
| 404 | `company not found` | bad or unknown id |
| 403 | `cannot change your own company's status` | the target is the caller's own company. Hide or disable the actions on that row. |
| 400 | `body must be a JSON object` / `status must be one of: active, suspended, exited` / `reason is required` / `reason is longer than 1000 characters` / `stop_billing must be true or false` | bad body |
| 503 | `not migrated` | migration 0092 not applied on this environment |

Success is 200 (not 201). Response when the status changed:

```json
{
  "companyId": "9a71d2b4-...",
  "status": "suspended",
  "previousStatus": "active",
  "changed": true,
  "eventId": "c1d2e3f4-...",
  "affectedUsers": 14,
  "affectedDevices": 6,
  "cognito": {"disable": 14, "signout": 14, "enable": 0},
  "cognitoTrigger": "queued",
  "socketsClosed": 3,
  "statusChangedAt": "2026-11-01T03:12:45.123456+00:00",
  "billingStoppedAt": null,
  "exitedAt": null
}
```

- `affectedUsers`: home users of the company (archived included, users with a Cognito sub).
  It is the same count on every transition, including reactivation; on reactivation the number of
  people actually being re-enabled is `cognito.enable`.
- `affectedDevices`: devices whose last heartbeat account is one of those users.
- `cognito`: work rows created by this change. Deactivation from `active`: one `disable` and one
  `signout` per home user. `suspended` ↔ `exited`: all zero (the users were already handled).
  Reactivation: `enable` = users whose latest completed action was a disable.
- `cognitoTrigger`: `queued` (LakeOps will pick it up), `none` (nothing to do: zero rows), or
  `failed` (the status change IS saved, but the trigger for the Cognito task could not be written;
  rows stay pending until an operator re-runs the task). Show `failed` as a warning, not an error.
- `socketsClosed`: 0 when the flag is off, and always 0 on reactivation.

Response when the company already has that status (idempotent, nothing written). Note it is a
smaller object: `statusChangedAt`, `billingStoppedAt` and `exitedAt` are absent.

```json
{
  "companyId": "9a71d2b4-...",
  "status": "suspended",
  "previousStatus": "suspended",
  "changed": false,
  "eventId": null,
  "affectedUsers": 0,
  "affectedDevices": 0,
  "cognito": {"disable": 0, "signout": 0, "enable": 0},
  "cognitoTrigger": "none",
  "socketsClosed": 0
}
```

Show it as "Already suspended; nothing changed".

### 2.3 Confirmation dialog

There is **no dry-run or preview endpoint**. The user and device counts only come back in the
POST response, after the change is made. So the dialog cannot show the backend's counts before
confirming. What it can show before confirming:

- the member count from `GET /members?include_archived=1` (platform_admin gets every tenant;
  filter rows by `company_id`). That matches `affectedUsers` except for users without a Cognito
  sub. There is no device count available to the UI before confirming.
- the text: "All N people whose home company is X will be signed out and unable to sign in.
  Their data is kept. People from other companies lose access to X's projects."

After the POST, show the returned `affectedUsers` / `affectedDevices` / `cognito` in the success
message.

### 2.4 Audit view

`GET /companies/{id}/status-events` (allowed for the caller's own company too; read-only).
Events are newest first; each event's Cognito rows are sorted by `userSub`, then `action`.

```json
{
  "companyId": "9a71d2b4-...",
  "status": "active",
  "statusChangedAt": "2026-11-02T01:00:00.000000+00:00",
  "statusReason": "Contract renewed",
  "billingStoppedAt": null,
  "exitedAt": "2026-11-01T03:12:45.123456+00:00",
  "events": [
    {
      "id": "e2...",
      "fromStatus": "exited",
      "toStatus": "active",
      "reason": "Contract renewed",
      "actorUserId": "u1...",
      "actorSub": "a1b2c3...",
      "at": "2026-11-02T01:00:00.000000+00:00",
      "detail": {"stop_billing": false,
                 "previous": {"status_reason": "Contract ended",
                              "billing_stopped_at": "2026-11-01T03:12:45.123456+00:00",
                              "exited_at": "2026-11-01T03:12:45.123456+00:00"}},
      "cognito": [
        {"userSub": "sub-1", "action": "enable", "state": "done", "attempts": 1,
         "lastError": null, "updatedAt": "2026-11-02T01:10:03.000000+00:00"}
      ]
    },
    {
      "id": "e1...",
      "fromStatus": "active",
      "toStatus": "exited",
      "reason": "Contract ended",
      "actorUserId": "u1...",
      "actorSub": "a1b2c3...",
      "at": "2026-11-01T03:12:45.123456+00:00",
      "detail": {"stop_billing": false,
                 "previous": {"status_reason": null, "billing_stopped_at": null,
                              "exited_at": null}},
      "cognito": [
        {"userSub": "sub-1", "action": "disable", "state": "done", "attempts": 1,
         "lastError": null, "updatedAt": "2026-11-01T03:20:01.000000+00:00"},
        {"userSub": "sub-1", "action": "signout", "state": "done", "attempts": 1,
         "lastError": null, "updatedAt": "2026-11-01T03:20:01.000000+00:00"}
      ]
    }
  ]
}
```

- `actorUserId` is the platform_admin's users.id (may be null), `actorSub` their Cognito sub. The
  UI has to resolve names itself (e.g. from `GET /members`).
- `detail` is stored JSON and keeps **snake_case** keys (`stop_billing`, `previous.status_reason`,
  `previous.billing_stopped_at`, `previous.exited_at`): the values before this change.
- `exitedAt` is the first exit and is kept after reactivation.
- Cognito row `action`: `disable` | `signout` | `enable`. `state`: `pending` | `done` | `failed`.
  - `pending`: not run yet. With the flag off, rows stay pending (the task does nothing).
  - `done` with `lastError: "user not found in Cognito"`: nothing to do; treat as done.
  - `failed` with another `lastError` (e.g. `"NotAuthorizedException: ..."`): retried
    automatically up to 5 attempts.
  - `failed` with `lastError` starting with `superseded` is final and **not an error**: a later
    status change made this row moot. Values: `superseded by reactivation` (a disable/signout not
    yet run when the company was reactivated), `superseded by deactivation` (an enable not yet run
    when the company was deactivated again), `superseded by status change` (the executor found
    the company in the other state when it got to the row). Show these as "Skipped (superseded)".

Suggested summary per event: counts of done / pending / failed (excluding superseded) / skipped.

### 2.5 Export links

Unchanged endpoints, plus a new `records` field:

- `POST /companies/{id}/export` → 202 `{"exportId": "...", "companyId": "..."}`. Async (LakeOps
  picks it up on its 10-minute tick). Errors: 502 `could not start the export`.
- `GET /companies/{id}/export/{export_id}` → status. 400 `export id must be a uuid`; 404
  `export not found (or not started yet)`.

The structured records (`records/*.jsonl` + `records/README.txt`) are written first, then the
file export. While running:

```json
{"exportId": "e...", "companyId": "c...", "status": "running", "done": false,
 "partsWritten": 0, "objects": 0,
 "records": {"status": "running", "files": [],
             "tables": {"company": {"done": true, "rows": 1, "excluded": {}},
                        "topics": {"done": false, "rows": 5000,
                                   "excluded": {"deleted": 3, "superseded": 12}}}}}
```

When done:

```json
{"exportId": "e...", "companyId": "c...", "status": "done", "done": true,
 "parts": [{"key": "exports/c.../e.../part-0001.zip", "objects": 812, "bytes": 1048576000,
            "url": "https://...presigned..."}],
 "objects": 812,
 "excluded": {},
 "manifest": {"key": "exports/c.../e.../manifest.json", "url": "https://..."},
 "records": {"status": "done",
             "files": [{"key": "exports/c.../e.../records/company.jsonl", "url": "https://..."},
                       {"key": "exports/c.../e.../records/README.txt", "url": "https://..."}],
             "tables": {"project_documents": {"done": true, "rows": 0, "excluded": {},
                                              "note": "absent: ..."}}},
 "expiresIn": 900}
```

- `records` is `null` for exports made before this change.
- `records.files` is empty until `records.status` is `done`; `key` is the full S3 key (show the
  part after `records/` as the file name). `tables[*].note` appears only when there is one.
- Every URL expires after 900 s (`expiresIn`, present only in the done response). Re-fetch the
  status to get fresh links; do not store them.
- Long-lived (up to 7 days) handover links for the customer are made by the operator script
  `scripts/presign_company_export.py`, not by the UI. The UI may show a note saying so.

## 3. Blocked member experience

Only with the flag on. Responses that mean "your company is inactive" or "your account is
archived":

| Where | Response |
|---|---|
| any org-api route except `GET /me` | 403 `{"error": "company_inactive"}` |
| `GET /me` | 200, normal body plus `"blocked": {"reason": "company_inactive"}` |
| `GET /me`, archived user | 200, plus `"blocked": {"reason": "account_archived"}` |
| any other org-api route, archived user | 403 `{"error": "account archived"}` (pre-existing; **space, not underscore**, and independent of the flag) |
| WebSocket connect | refused (HTTP 403 on the handshake) |

`blocked` is present only when the gate refuses the caller (flag on). It is absent otherwise,
never `null`. Exact shape:

```json
{"id": "...", "company_id": "...", "email": "...", "site_ids": [], "scope": "...",
 "site_roles": {}, "effective_scope": null, "company_name": "Old Client Ltd",
 "blocked": {"reason": "company_inactive"}}
```

(`site_ids` / `site_roles` are already emptied of inactive-company sites.)

UI behaviour:

- One full-page notice, shown when `GET /me` has `blocked`, or when any org-api call returns 403
  with `error === "company_inactive"`. Suggested copy for `company_inactive`: "Your company's
  FieldSight access is paused. Your data is kept. Contact your administrator or FieldSight
  support." For `account_archived` / `account archived`: "Your account has been deactivated.
  Contact your company administrator." Offer only Sign out. Do not retry the request.
- Stop opening/reconnecting the WebSocket once blocked (the connect will keep being refused).
- **Ask:** the Ask endpoint does **not** return `company_inactive` to the UI. rag-search returns
  `{"chunks": [], "error": "company_inactive"}` internally, but ask-agent (both paths) turns it
  into an ordinary answer `"No relevant records found for this question."` with no `error` and
  no web offer. In practice the full-page notice from `/me` or the 403 on other routes comes first.
- **After Cognito disable** (up to ~10 min after the change): the user cannot sign in, and the
  token refresh fails (Cognito `NotAuthorizedException`, message "User is disabled.").
  A global sign-out also invalidates refresh tokens. The existing ID token stays valid until it
  expires (up to 60 min), during which the 403s above apply. The UI must treat a failed refresh as
  "signed out": go to the sign-in page, without an error loop. A disabled user who tries to sign
  in gets "User is disabled."; show it plainly (e.g. "This account is disabled. Contact your
  administrator.").

## 4. Team page: `login_status: "DISABLED"`

`GET /members`: no new field. `members[].login_status` has one new value, `"DISABLED"` (the
Cognito user is disabled; it wins over every other status). Other values are unchanged (Cognito
`UserStatus`, e.g. `CONFIRMED`, `FORCE_CHANGE_PASSWORD`, or `null` when unknown). It reflects
Cognito, not the flag.

```json
{"members": [{"id": "...", "cognito_sub": "...", "email": "a@b.co", "company_id": "...",
              "memberships": [{"site_id": "...", "role": "worker"}],
              "login_status": "DISABLED", "company_name": "Old Client Ltd"}]}
```

(`company_name` is present only for platform_admin.) Add `DISABLED: 'Sign-in disabled'` (or
similar) to `LOGIN_STATUS_TEXT` in `scripts/api/org.js`. No resend-invite button for it
(`memberAccessFacts` already sets `canResend = false` for unknown values; keep it that way).

## 5. Writes into an inactive company: 409

With the flag on, a non-platform_admin gets 409 `{"error": "company_inactive"}` when the write
would add people or access to an inactive company:

- create member, add external member, put member membership, member unarchive;
- site create, site patch, site unarchive;
- session writes on a session that lies on an inactive company's site (re-extraction /
  regenerate of the recorder's own session, unname speaker).

Suggested message: "This company's access is paused, so this change can't be made. Ask a
FieldSight administrator." Do not retry.

Related, not an error: `POST /sessions/{id}/report` and `POST /days/{date}/report` with
`deliver: "email"` on an inactive company's site are turned into downloads. The 202 response then
carries `"emailSuppressed": "company_inactive"`. Show "Email was not sent: the company's access is
paused. You can download the report." (Field absent otherwise.)

## 6. Open items / not built

- No preview / dry-run endpoint for the counts (§2.3).
- Ask gives no distinguishable error for a blocked caller (§3).
- `/api/ask/corroborate` and `/api/ask/voice` STT run in the DB-less legacy API and are not gated;
  usable until the ID token expires (≤ 60 min). Known gap in the spec.
- No UI built in this repo; nothing in fieldsight-ui changed yet.
- Records export: external members (other companies' people who worked on the company's sites)
  appear only as user ids; their names are not in the export.
- Long-lived handover links are operator-only (§2.5).
- Rollout: flag off on prod first (shadow). The first real prod exit is an owner action.
