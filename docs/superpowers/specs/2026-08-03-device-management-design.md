# Device Management — Design

**Date:** 2026-08-03
**Repos touched:** `fieldsight-pipeline` (backend), `GrandTime` (mobile). No frontend work.
**External surface:** one Notion database (the only UI), Teams webhook + SES email (alerts only).

## Problem

Twenty F2SP devices rotate between clients on roughly monthly cycles — about one hand-over
per working day at steady state. There is no way to answer:

- Which device is at which client right now?
- Which device was handed over but has never been switched on?
- Which device was in use and has gone quiet?
- Which devices are still on an old app build?
- Which devices are overdue for return?

The backend has **no device concept at all**: no table, no column, no identifier.
`recordings.client_uuid` is the mobile Room row id (upload idempotency only), and the mobile
app collects no hardware identifier. The current unit of accounting is the **account**
(`folder_name` / Cognito), not the device — and because a device is re-logged-in to a
different account at every hand-over, account and device diverge every month.

## Scope

**In scope**

- Device identity: each device reports a stable tag + uuid + app version on every request.
- A `devices` ledger in Aurora, fed by a throttled heartbeat on the org-api hot path.
- A Notion database as the single human-facing surface: system writes telemetry columns,
  humans hand-edit the assignment columns.
- Four derived alerts: **handed over but never activated**, **activated then gone quiet**,
  **due for return**, **outdated app version**.
- Alert push to a Teams channel and to email.
- Logout hygiene on mobile (a hand-over consequence, not a device-management feature, but it
  ships in the same change because it is caused by the same account rotation).

**Explicitly out of scope**

- Maintenance, retirement, loan records, SIM cards, batteries. Excel does this better until
  there are far more than twenty devices.
- Any client-facing view of devices. The ledger is internal only.
- A dashboard page, PowerBI, or any visualisation layer.
- A lifecycle state machine with manually-recorded transitions. See "Deriving state without
  bookkeeping" below — none is needed.

## Decisions and rationale

| Decision | Rationale |
|---|---|
| `asset_tag` (a physical FS-01..FS-20 label) is the **authoritative** identity; `device_uuid` is advisory | The twenty devices are likely flashed from one factory image, so `ANDROID_ID` may be identical across all of them. This ROM already misreports `SENSOR_ORIENTATION`, so its identifiers cannot be trusted. |
| Both label-entry and back-office claiming are supported | Label entry covers the normal path with zero back-office work; claiming covers the case where a device reports before anyone has typed its number. |
| Assignment granularity is the **client**, not the site | A client moving a device between their own sites must not produce a false mismatch. The site a device actually works at is derived from `recordings`, so registered-vs-actual stays meaningful. |
| Heartbeat rides existing requests; no background task | The device ROM's background-task reliability is unproven. Every org-api request is a heartbeat, and the one blind spot (app never opened) is exactly the "never activated" alert. |
| Notion, not a web page | Non-technical teammates need zero-setup access; a Notion database is also **writable per property**, so hand-entered assignment data survives every sync. A generated file or SQL query cannot hold hand-entered data. |
| Internal-only visibility | Collapses the permission model to a single question. This project's recurring failures are in graded ACLs (empty-list-means-no-filter, per-endpoint `platform_admin` teaching); none of that is needed here. |
| Two Lambdas, not one | An in-VPC function can reach Aurora but not the internet (no NAT gateway); an outbound function cannot reach Aurora. This is the project's standing rule (BUG-36). |

## Deriving state without bookkeeping

Devices are **hand-delivered and hand-collected**, which removes the in-transit unknown that
would otherwise force a manual lifecycle state machine. Every phase boundary is already
observable:

| Phase | Test | Source |
|---|---|---|
| Handed over, not yet activated | `last_seen_at` is null or earlier than `Dispatched` | hand-entered date + automatic heartbeat |
| **In use** | `last_seen_at >= Dispatched` and `Returned` unticked | **fully derived** |
| Returned | `Returned` ticked | one tick on collection |

"Activation" is simply the first heartbeat after the hand-over date, so nobody has to record it.
Both tests use only the `last_seen_at` scalar — no per-day heartbeat history is retained or
needed.

This is what makes the **"activated then gone quiet"** alert viable: it is evaluated *only*
inside the in-use window, so the legitimately dark phases (not yet activated, already collected,
sitting in stock) fall outside it by construction and cannot produce false positives.

**Manual effort per rotation is one date on hand-over and one tick on collection.** Everything
else defaults or derives. `asset_tag` is entered once per device for its lifetime — it persists
across logout and account changes, so it is a one-off setup of twenty devices, not a per-rotation
step. Only a factory reset requires re-entry.

## Architecture

### 1. Mobile — the device states its name

`DeviceIdentity`, a singleton resolved once at process start:

- **`asset_tag`** — entered by a human in Settings (`FS-07`), once per device lifetime.
  Persisted to **both** `SharedPreferences` and a file under `getExternalFilesDir()`. The double
  write survives "Clear data"; only uninstall or factory reset loses it.
- **`device_uuid`** — `Settings.Secure.ANDROID_ID`, rejected and replaced by a self-minted
  `UUID` if it is null, all-zero, or the well-known `9774d56d682e549c` emulator value.
  Persisted the same way as the tag.
- **`app_version`** — `BuildConfig.VERSION_NAME`.

Three headers ride every org-api request:

```
X-Device-Tag: FS-07          (omitted entirely if not yet entered)
X-Device-Id:  <uuid>
X-App-Version: 1.4.2
```

**Attached at the request builders, beside `Authorization` — not via an OkHttp interceptor.**
This design originally said "a single interceptor"; reading the code showed that would leak.
`RecordingsApiClient` derives its S3 upload client from the org-api one
(`UPLOAD_HTTP = OK_HTTP.newBuilder()`), so an interceptor on the shared builder is inherited by
the client that PUTs media to **S3 presigned URLs** — needless risk against a signed request, and
it hands device identity to a service that has no use for it. Attaching where `Authorization`
already goes (`RealHttp.postJson`, `RealSitesHttp.getJson`, never `putFile`) cannot reach S3 by
construction.

Settings screen gains: an editable device-number field, and a read-only display of the first
six characters of `device_uuid` so a human can match a device against an unclaimed row.

**Logout hygiene — and a live leak this uncovered.** Pending upload-queue entries must be stamped
with the `owner_sub` that recorded them, at capture time. After a different account logs in, those
entries are neither uploaded nor listed. Logout shows "N recordings not yet uploaded" as a warning
but does **not** block — a hand-over often happens without connectivity, and blocking would strand
the person doing it.

This was written as if it were a small addition. It is not. Reading the mobile code on 2026-08-04
showed the opposite behaviour is currently wired in:

- All three `CaptureRecord(...)` sites in `capture/CaptureManager.kt` omit `authorSub`, so every
  row is created `NULL`.
- The only thing that ever sets it is `CaptureRecordDao.backfillAuthorSub(sub)` —
  `UPDATE capture_records SET authorSub = :sub WHERE authorSub IS NULL` — called from
  `CognitoAuthManager.onLoggedIn`, which claims **every** unowned row for whoever logs in.
- `listByUploadStatus` does not filter by author at all.

So a hand-over stamps client A's still-pending recordings with client B's sub and uploads them
under B's account. Monthly rotation *is* that flow, so this is a live cross-tenant leak rather
than a hypothetical one, and it is the **first** task of Phase 2 — a fix, not a feature.

### 2. Backend — the ledger

**Migration** adds:

```sql
create table devices (
  id                uuid primary key,
  asset_tag         text unique not null,
  device_uuid       text,
  uuid_trusted      boolean not null default true,
  app_version       text,
  last_seen_at      timestamptz,
  last_account_sub  text,
  created_at        timestamptz not null default now()
);
create index on devices (last_seen_at nulls first);

alter table recordings add column device_id uuid references devices(id);
create index on recordings (device_id, created_at desc);
```

There is deliberately **no status column and no assignment table**. Status is derived (see
above); assignment originates from a human and lives in Notion. Duplicating either in Aurora
would create two sources of truth.

**Heartbeat.** The org-api entry point performs one conditional upsert per request:

```sql
insert into devices (id, asset_tag, device_uuid, app_version, last_account_sub, last_seen_at)
values (...)
on conflict (asset_tag) do update set
  last_seen_at     = excluded.last_seen_at,
  app_version      = excluded.app_version,
  last_account_sub = excluded.last_account_sub,
  device_uuid      = excluded.device_uuid
where devices.last_seen_at < now() - interval '1 hour'
   or devices.last_account_sub is distinct from excluded.last_account_sub
   or devices.app_version    is distinct from excluded.app_version;
```

When the `where` fails, Postgres modifies no row: no WAL, no bloat, cost is one statement on an
already-open connection. The two `is distinct from` clauses mean **account switches and version
upgrades are never throttled** — an account switch is the central event of a hand-over and must
be recorded the moment it happens. Ordinary repeat heartbeats are suppressed for an hour.

A short session is not missed: the throttle suppresses only *repeat* writes, so a device's first
request after power-on always writes. A fifteen-minute session records a `last_seen_at` at most a
few minutes earlier than the true last activity, which is invisible at day granularity.

A request carrying `X-Device-Id` but no `X-Device-Tag` creates or updates an **unclaimed** row
keyed by uuid (`asset_tag = 'unclaimed:<uuid-prefix>'`), which surfaces in Notion for manual
claiming.

**Uuid collision detection.** If one `device_uuid` is reported under two different `asset_tag`s,
every row carrying that uuid is set `uuid_trusted = false`. Trust never auto-recovers; the tag
remains authoritative regardless, so the ledger stays correct even if all twenty devices report
an identical `ANDROID_ID`.

`recordings.device_id` is stamped at `create_recording_upload_url` time from the same header,
which is what makes "the site this device actually worked at" derivable.

**Two Lambdas, because one cannot do both halves.** There is no NAT gateway, so an in-VPC
function cannot reach `api.notion.com` or the Teams webhook, and a non-VPC function cannot reach
Aurora. This mirrors the existing RAG split (`AskAgentFunction` outside the VPC invoking the
in-VPC `RagSearchFunction`):

```
fieldsight-{stage}-device-report    non-VPC, EventBridge daily
      │  synchronous Invoke
      ▼
fieldsight-{stage}-device-ledger    in-VPC, reads devices + recordings/sites, returns JSON
      │
      ▼
back in device-report: derive state, compute alerts, write Notion, push Teams + email
```

The RAG-style direct invoke is preferred over the `FinalizeSweepFunction` style S3 hand-off
because S3 event triggers are wired manually outside the template (BUG-33), which would add a
new prefix, a manual wiring step and extra IAM for no benefit.

All date comparisons use `Pacific/Auckland`, never UTC. Schedule frequency is daily; the stated
tolerance is hours to days.

### 3. Notion — the only interface

One database. Alerts push to Teams and email only when something fires; the table itself is
always current, so silence is not the only evidence that the job ran.

| Hand-edited | System-written |
|---|---|
| `Device` (FS-xx) · `Dispatched` · `Returned` ☐ · `Notes` | `Client`* · `Due back`* · `Activated`* · `Status` · `Last seen` · `App version` · `Actual site` · `Last synced` |

\* **Fill-if-empty columns.** These three are written by the system **only when blank**, and once
they hold any value — whether typed by a human or previously written by the system — they are
never overwritten. This is what keeps per-rotation typing down to one date:

- `Due back` defaults to `Dispatched + 30 days`; type a value only for a non-standard term.
- `Client` is back-filled on activation from the company owning the first site the device
  uploaded to. Type it only to override.
- `Activated` is written once, on the first sync that observes `last_seen_at >= Dispatched`.
  It is also a useful business figure: how many days after hand-over the client actually started.

`Dispatched` and `Due back` are dates. A Notion row whose `Device` matches no `devices` row still
displays; it simply shows an empty `Last seen`, which is the never-activated case.

**Alert derivation** — all computed at run time; nothing is stored:

- **Handed over, never activated** — `Dispatched` set, `Returned` unticked, `last_seen_at` null
  or earlier than `Dispatched`, and `Dispatched` more than the grace period ago (default 3 days).
- **Activated then gone quiet** — in the in-use window (`last_seen_at >= Dispatched`, `Returned`
  unticked) and no heartbeat for more than `QUIET_ALERT_WORKING_DAYS` NZ working days.
  **Default 7, set by environment variable, deliberately not hard-coded** — the right threshold
  depends on how often devices at signal-poor sites actually sync, which is unknown until the
  ledger has run for a month. Tune from observed data rather than guessing now.
- **Due for return** — `Returned` unticked and `Due back` < today (NZ).
- **Outdated version** — `app_version` below the highest version any device has ever reported.
  Zero configuration: publishing a new build makes the comparison update itself.
- **Site mismatch** — the derived actual site belongs to a company other than `Client`. Shown as
  a row flag, not pushed as an alert.

`Last synced` is the failure fuse. A silent sync failure — expired token, renamed property,
deleted page — leaves a table that merely looks unchanged, which is the same class of defect as
BUG-41. Anyone can see a stale timestamp at a glance, and a Lambda failure also emails.

The Notion integration token follows the ElevenLabs key's existing path: GitHub secret → CFN
`NoEcho` parameter → Lambda environment variable. No new secret store.

**The database exists** (created 2026-08-04), pre-populated with twenty `FS-01`..`FS-20` rows all
at `在库`. It lives in the **company** workspace `Preformance` (`ben.lin@preformance.co.nz`), not
a personal one — the ledger is a company asset and must survive any one person's account:

- Database: `https://app.notion.com/p/943da8c294734365b6c7294c2055c45d`
- Data source id for the API: `1c4d069f-3019-4210-8363-ce4c370aa433`

Property names are exactly as tabled above; `Status` is a select over
`未激活 / 使用中 / 在库 / 失联 / 未认领`. Every property carries its rule as a Notion description,
so the hand-edited/system-written split is visible in the UI rather than only in this document.

A first copy was built in a personal workspace and abandoned the same day. Moving it was
impossible — Notion cannot move pages **across** workspaces, only within one — so it was rebuilt
and the ids above replace the originals. This cost nothing because the table was still empty and
no code referenced the ids yet; the same move after Phase 3 would have meant changing code, a
GitHub secret and a deploy. **Confirm the workspace before wiring an id into anything.**

Phase 3 still needs an integration token: create one in Notion, **connect the database to it**
(the page's `•••` → Connections — creating the integration alone is not enough and the API
returns a misleading 404 if this is skipped), then add it as GitHub secret `NOTION_TOKEN` and pass
it through `deploy.yml` as the `NotionToken` parameter. Until then `device-report` stays inert.

## Delivery phases

**Phase 1 is SHIPPED to TEST** (2026-08-04, PR #220). Verified on the live stack: migration
`0030` applied; a request carrying device headers records a heartbeat *even when it 403s* on an
unprovisioned account; a repeat request inside the window writes nothing while a version change
writes through immediately; `device-report` returns `{"status": "disabled"}` and its schedule rule
is `DISABLED`. Not deployed to prod — nothing here is useful there until the app sends headers.

**Phase 1 — backend only, no app release required.**
`devices` table, heartbeat upsert, the two Lambdas, Notion sync of telemetry columns.
Until the app sends headers no device rows exist, so every Notion row shows an empty `Last seen`.
That is not wasted work: the twenty rows are hand-created in Notion during this phase, and the
sync running against them proves out the entire Notion link — token, property names, permissions,
scheduling — before the app release lands.

**Phase 2 — mobile release.**
`DeviceIdentity`, the interceptor, the Settings screen, logout hygiene. Real tags, uuids and
versions begin arriving and the ledger becomes device-centric.

**Phase 3 — alerts.**
The four derived alerts plus Teams and email push. Depends on Phase 2 both for `last_seen_at` to
mean "device" rather than "account", and for a month of real data with which to set
`QUIET_ALERT_WORKING_DAYS`.

Phases 1 and 2 can be developed in parallel; only release ordering matters.

## Failure modes and handling

| Risk | Handling |
|---|---|
| All twenty devices report the same `ANDROID_ID` | `asset_tag` authoritative; collision detection clears `uuid_trusted` |
| Notion sync fails silently | `Last synced` property, plus email on Lambda failure |
| Due-date arithmetic in UTC is a day out | `Pacific/Auckland` throughout (BUG-37/19) |
| Heartbeat write amplification on the hot path | Conditional upsert; no row modified inside the throttle window |
| Quiet-alert threshold set too tight → false positives → alert gets muted → worse than no alert | Threshold is an environment variable and is left unset until a month of real gap data exists |
| System overwrites a hand-typed Notion value | Fill-if-empty rule, enforced in one place and unit-tested |
| Client and site names leave AWS for Notion | Accepted (user decision, 2026-08-03). Only tags, client names, site names and account display names sync — never recording, transcript or report content. |
| Notion API rate limit (~3 req/s) | Twenty rows daily; not a factor |

### Deploy-role IAM — verified, not assumed

CloudFormation applies a change set as one transaction under
`github-actions-fieldsight-deploy`. An `AccessDenied` on any single resource rolls back the
**entire** update, leaving the stack in `UPDATE_ROLLBACK_COMPLETE` — or `UPDATE_ROLLBACK_FAILED`,
which needs a manual `continue-update-rollback`. Until it is cleared every subsequent push fails,
and on prod recovery costs another trip through the approval gate. An API Gateway access-log
group caused exactly this previously.

The role was inspected on 2026-08-03. It grants by **name prefix**, not by enumeration:

```
lambda:*             → function:fieldsight-*
logs:CreateLogGroup  → log-group:/aws/lambda/fieldsight-*  (also /ecs/, /aws/apigateway/)
events:PutRule       → rule/*            (unrestricted)
iam:CreateRole, iam:PassRole → role/fieldsight-*
```

`StageConfig` prefixes are `fieldsight-prod` / `fieldsight-test`, and SAM derives execution-role
names from the stack name, which also begins `fieldsight-`. **Both new Lambdas, their schedule
rule, their log groups and their execution roles therefore fall inside existing grants.**

This reduces to one invariant rather than a per-resource audit: **every new resource's physical
name must begin with `fieldsight-`, and log groups must sit under one of the three granted
paths.** A single `simulate-principal-policy` check against the concrete ARNs remains a
pre-flight step, but is expected to pass.

## Semantics worth stating

`last_seen_at` means **last network contact**, not last power-on. A device recording all day on a
site with no signal and syncing at the office that evening shows the evening timestamp. This
matters for the quiet-alert threshold, which is why that threshold is data-driven rather than
guessed; it does not affect the other three alerts.

A device that is switched on but never logged in issues no requests and therefore never appears —
which is precisely the "never activated" alert, not a gap in coverage.

Per-device daily activity history is **not** retained in `devices` (`last_seen_at` is a scalar and
is overwritten), but once `recordings.device_id` is stamped the per-device, per-day upload record
is reconstructible from `recordings` indefinitely. Any future refinement that needs history — for
example replacing the fixed quiet threshold with a per-device baseline — is therefore still open
without retaining anything extra now.

## Testing

- Heartbeat upsert, via the repo's `FakeConn`/`FakeCursor` doubles: first sighting inserts;
  repeat inside the window is a no-op; account change and version change both write through the
  window; missing tag produces an unclaimed row; duplicate uuid across tags clears `uuid_trusted`.
- Two-hop invocation: `device-report` handles a `device-ledger` failure by surfacing it (email)
  rather than writing a partial or empty Notion table.
- Alert derivation with a faked Notion client: each of the four alerts fires on its boundary
  condition and stays silent one day either side; the quiet alert does **not** fire for a device
  that is not yet activated or already returned.
- Fill-if-empty: a hand-typed `Client`, `Due back` or `Activated` survives repeated syncs
  unchanged; a blank one is filled exactly once.
- Timezone: a due date of "today" in NZ must not fire while it is still yesterday in UTC.
- Mobile: `DeviceIdentity` survives Clear-data; a rejected `ANDROID_ID` falls back to a minted
  uuid; queued uploads from a previous `owner_sub` are neither listed nor sent after a switch.
