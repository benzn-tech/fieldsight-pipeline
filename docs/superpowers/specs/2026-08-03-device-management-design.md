# Device Management — Design

**Date:** 2026-08-03
**Repos touched:** `fieldsight-pipeline` (backend), `GrandTime` (mobile). No frontend work.
**External surface:** one Notion database (the only UI), Teams webhook + SES email (alerts only).

## Problem

Twenty F2SP devices rotate between clients on roughly monthly cycles — about one hand-over
per working day at steady state. There is no way to answer:

- Which device is at which client right now?
- Which device was dispatched but has never been switched on?
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
- Three derived alerts: **due for return**, **dispatched but never seen**, **outdated app version**.
- Alert push to a Teams channel and to email.
- Logout hygiene on mobile (a hand-over consequence, not a device-management feature, but it
  ships in the same change because it is caused by the same account rotation).

**Explicitly out of scope**

- Offline/silent alerting for devices already at a client. Monthly rotation means a device is
  legitimately dark for a week while in transit; a threshold-based offline alert would be
  almost entirely false positives and would be muted within a fortnight.
- Maintenance, retirement, loan records, SIM cards, batteries. Excel does this better until
  there are far more than twenty devices.
- Any client-facing view of devices. The ledger is internal only.
- A dashboard page, PowerBI, or any visualisation layer.

## Decisions and rationale

| Decision | Rationale |
|---|---|
| `asset_tag` (a physical FS-01..FS-20 label) is the **authoritative** identity; `device_uuid` is advisory | The twenty devices are likely flashed from one factory image, so `ANDROID_ID` may be identical across all of them. This ROM already misreports `SENSOR_ORIENTATION`, so its identifiers cannot be trusted. |
| Both label-entry and back-office claiming are supported | Label entry covers the normal path with zero back-office work; claiming covers the case where whoever ships the device forgets to type the number. |
| Assignment granularity is the **client**, not the site | A client moving a device between their own sites must not produce a false mismatch. The site a device actually works at is derived from `recordings`, so registered-vs-actual stays meaningful. |
| Heartbeat rides existing requests; no background task | The device ROM's background-task reliability is unproven. Every org-api request is a heartbeat, and the one blind spot (app never opened) is exactly the "never seen" alert. |
| Notion, not a web page | Non-technical teammates need zero-setup access; a Notion database is also **writable per property**, so hand-entered assignment data survives every sync. A generated file or SQL query cannot hold hand-entered data. |
| Internal-only visibility | Collapses the permission model to a single question. This project's recurring failures are in graded ACLs (empty-list-means-no-filter, per-endpoint `platform_admin` teaching); none of that is needed here. |

## Architecture

### 1. Mobile — the device states its name

`DeviceIdentity`, a singleton resolved once at process start:

- **`asset_tag`** — entered by a human in Settings (`FS-07`). Persisted to **both**
  `SharedPreferences` and a file under `getExternalFilesDir()`. The double write survives
  "Clear data"; only uninstall or factory reset loses it.
- **`device_uuid`** — `Settings.Secure.ANDROID_ID`, rejected and replaced by a self-minted
  `UUID` if it is null, all-zero, or the well-known `9774d56d682e549c` emulator value.
  Persisted the same way as the tag.
- **`app_version`** — `BuildConfig.VERSION_NAME`.

A single OkHttp `Interceptor` attaches three headers to every org-api request:

```
X-Device-Tag: FS-07          (absent if not yet entered)
X-Device-Id:  <uuid>
X-App-Version: 1.4.2
```

Settings screen gains: an editable device-number field, and a read-only display of the first
six characters of `device_uuid` so a human can match a device against an unclaimed row.

**Logout hygiene.** Pending upload-queue entries are stamped with the `owner_sub` that created
them. After a different account logs in, those entries are neither uploaded nor listed. Logout
shows "N recordings not yet uploaded" as a warning but does **not** block — a hand-over often
happens without connectivity, and blocking would strand the person doing it.

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

There is deliberately **no status column and no assignment table**. Status and assignment live
in Notion as hand-edited properties; duplicating them in Aurora would create two sources of
truth for data that only ever originates from a human.

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

A request with no `X-Device-Tag` but a `X-Device-Id` creates/updates an **unclaimed** row keyed
by uuid (tag = `unclaimed:<uuid-prefix>`), which surfaces in Notion for manual claiming.

**Uuid collision detection.** If one `device_uuid` is reported under two different `asset_tag`s,
every row carrying that uuid is set `uuid_trusted = false`. Trust never auto-recovers; the tag
remains authoritative regardless, so the ledger stays correct even if all twenty devices report
an identical `ANDROID_ID`.

`recordings.device_id` is stamped at `create_recording_upload_url` time from the same header,
which is what makes "the site this device actually worked at" derivable.

**`lambda_device_report`** — a new EventBridge-scheduled Lambda (daily; the requirement is
hour-to-day latency, not real time). It:

1. Reads all `devices` rows plus, per device, the most recent `recordings` → `sites` join.
2. Reads the Notion database to obtain the hand-entered columns.
3. Computes the three alerts (all derived at query time; no state machine, no stored status).
4. Writes only the telemetry properties back to Notion, including `Last synced`.
5. If any alert fires, posts to the Teams webhook and sends an email.

All date comparisons use `Pacific/Auckland`, never UTC.

### 3. Notion — the only interface

One database. Left half is hand-edited and never touched by the sync; right half is
system-owned and overwritten on every run.

| Hand-edited | System-written |
|---|---|
| `Device` (FS-xx) · `Client` · `Dispatched` · `Due back` · `Returned` ☐ · `Notes` | `Status` · `Last seen` · `App version` · `Actual site` · `Last synced` |

`Dispatched` and `Due back` are dates; `Client` is free text matched against company names for
the mismatch check. A row whose `Device` does not correspond to any `devices` row still displays —
it simply shows an empty `Last seen`, which is the "never seen" case.

Alert derivation:

- **Due for return** — `Returned` unticked and `Due back` < today (NZ).
- **Dispatched, never seen** — `Client` set, `Returned` unticked, and `last_seen_at` is null or
  earlier than `Dispatched`, and `Dispatched` was more than the grace period ago
  (configurable, default 3 days).
- **Outdated version** — `app_version` below the highest version any device has ever reported.
  Zero configuration: publishing a new build makes the comparison update itself.
- **Site mismatch** — the derived actual site belongs to a company other than the registered
  `Client`. Shown as a flag on the row, not pushed as an alert.

`Last synced` is the failure fuse. A silent sync failure — expired token, renamed property,
deleted page — leaves a table that merely looks unchanged, which is the same class of defect as
BUG-41. Anyone can see a stale timestamp at a glance, and a Lambda failure also emails.

The Notion integration token follows the ElevenLabs key's existing path: GitHub secret → CFN
`NoEcho` parameter → Lambda environment variable. No new secret store.

## Delivery phases

**Phase 1 — backend only, no app release required.**
`devices` table, heartbeat upsert, `lambda_device_report`, Notion sync of telemetry columns.
Until the app sends headers no device rows exist, so every Notion row shows an empty `Last seen`.
That is not wasted work: the twenty rows are hand-created in Notion during this phase, and the
sync running against them proves out the entire Notion link — token, property names, permissions,
scheduling — before the app release lands. It also front-loads the alert most likely to be wrong,
"never seen", by making every row exercise it.

**Phase 2 — mobile release.**
`DeviceIdentity`, the interceptor, the Settings screen, logout hygiene. Real tags, uuids and
versions begin arriving and the ledger becomes device-centric.

**Phase 3 — alerts.**
The three derived alerts plus Teams and email push. Depends on Phase 2 for `last_seen_at` to
mean "device" rather than "account".

Phases 1 and 2 can be developed in parallel; only the ordering of release matters.

## Failure modes and handling

| Risk | Handling |
|---|---|
| All twenty devices report the same `ANDROID_ID` | `asset_tag` authoritative; collision detection clears `uuid_trusted` |
| New CFN resources (Lambda, EventBridge rule) without matching deploy-role IAM → CREATE_FAILED, whole stack rolls back, pipeline blocked | Explicit plan step: extend `github-actions-fieldsight-deploy` and verify with `simulate-principal-policy` before pushing. Do not read the template and guess. |
| Notion sync fails silently | `Last synced` property, plus email on Lambda failure |
| Due-date arithmetic in UTC is a day out | `Pacific/Auckland` throughout (BUG-37/19) |
| Heartbeat write amplification on the hot path | Conditional upsert; no row modified inside the throttle window |
| Client and site names leave AWS for Notion | Accepted (user decision, 2026-08-03). Only tags, client names, site names, account display names sync — never recording, transcript or report content. |
| Notion API rate limit (~3 req/s) | Twenty rows daily; not a factor |

## Semantics worth stating

`last_seen_at` means **last network contact**, not last power-on. A device recording all day on a
site with no signal and syncing at the office that evening shows the evening timestamp. This does
not affect any of the three chosen alerts, but the column must not be read as uptime.

A device that is switched on but never logged in issues no requests and therefore never appears —
which is precisely the "dispatched, never seen" alert, not a gap in coverage.

## Testing

- Repo `FakeConn`/`FakeCursor` doubles for the heartbeat upsert: first sighting inserts;
  repeat inside the window is a no-op; account change and version change both write through the
  window; missing tag produces an unclaimed row; duplicate uuid across tags clears `uuid_trusted`.
- `lambda_device_report` with a faked Notion client: each alert fires on its boundary condition
  and stays silent one day either side; a Notion write failure surfaces rather than being swallowed.
- Timezone: a due date of "today" in NZ must not fire while it is still yesterday in UTC.
- Mobile: `DeviceIdentity` survives Clear-data; a rejected `ANDROID_ID` falls back to a minted
  uuid; queued uploads from a previous `owner_sub` are neither listed nor sent after a switch.
