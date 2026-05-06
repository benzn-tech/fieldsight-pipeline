
---

## 4.13 Calendar events (P1)

`GET /api/calendar-events?from=YYYY-MM-DD&to=YYYY-MM-DD&site={site_id?}`

Returns due-date markers (action item due dates, scheduled inspections, etc.) for the date-picker red-dot overlay.

```json
{
  "events": [
    {
      "date": "2026-04-29",
      "type": "action_due" | "inspection" | "milestone",
      "title": "Block C scaffold inspection",
      "topic_id": "0",
      "user": "Jarley_Trainor",
      "priority": "high" | "med" | "low"
    }
  ]
}
```

**Empty:** `{ "events": [] }`. **Auth:** site_manager+ for the queried site; admin/pm/gm for all sites.

---

## 4.14 One-pager HTML (P1)

`GET /api/onepager?date=YYYY-MM-DD&user={display_name}`

Returns a CloudFront-served HTML one-pager for embedding or new-window open. Backend renders Jinja2 from existing `daily_report.json`; output cached at `reports/{date}/{user}/daily_onepager.html`.

```json
{
  "html_url": "https://d1xxxx.cloudfront.net/reports/2026-04-29/Jarley_Trainor/daily_onepager.html",
  "generated_at": "2026-04-29T18:32:11Z"
}
```

**404:** `{ "_notFound": true }` when no report exists for that date+user.

---

## 4.15 Topic priority (P1)

```
GET  /api/topics/priority?date=YYYY-MM-DD&user={display_name}
POST /api/topics/priority
```

Per-topic priority overrides (separate from the priority Claude assigns in the report). Allows pm+ to manually elevate / suppress topics.

GET response:
```json
{
  "overrides": [
    { "topic_id": "0", "priority": "high", "set_by": "Ben", "set_at": "2026-04-29T07:14:00Z" }
  ]
}
```

POST body:
```json
{ "date": "2026-04-29", "user": "Jarley_Trainor", "topic_id": "0", "priority": "high" | "med" | "low" | null }
```
`null` priority clears the override. Auth: pm+ write, all roles read.

---

## 4.16 Report corrections (P2)

```
POST /api/reports/correction      (write)
GET  /api/corrections?date=&user= (read all corrections for a report)
```

Inline edits over the AI-generated narrative. Original report is never mutated; corrections layered at render time.

POST body:
```json
{
  "date": "2026-04-29",
  "user": "Jarley_Trainor",
  "topic_id": "0",
  "field": "summary" | "key_decisions" | "action_items[2].text",
  "value": "Updated summary text."
}
```

GET response:
```json
{
  "corrections": [
    { "id": "corr_abc123", "topic_id": "0", "field": "summary", "value": "...",
      "corrected_by": "Ben", "corrected_at": "2026-04-29T19:00:00Z" }
  ]
}
```

UI: show `✏ Corrected` badge when `corrections[]` is non-empty for a topic. Auth: pm+ write, all roles read.

---

## 4.17 Analytics events (P2)

`POST /api/analytics/events`

Lightweight client telemetry (page views, button clicks, search queries). Backend writes to DynamoDB analytics table.

Body:
```json
{
  "events": [
    { "name": "page.view", "ts": "2026-04-29T07:00:00Z", "props": {"path": "/today"} },
    { "name": "report.opened", "ts": "...", "props": {"date": "2026-04-29", "user": "Jarley_Trainor"} }
  ]
}
```

Up to 10 events per call. Use `navigator.sendBeacon` on tab unload for reliability. Response: `204 No Content`. Auth: all signed-in roles.

---

## 4.18 Dashboard (P2)

`GET /api/dashboard?range=7d|30d|90d`

Aggregated KPIs for the `/sites` page (replacement for the current mock). Returns site-level rollups the role can see.

```json
{
  "range": "7d",
  "sites": [
    {
      "site_id": "site_001",
      "name": "Roskill",
      "users": 4,
      "recordings": 18,
      "topics": 47,
      "actions_open": 12,
      "actions_closed": 30,
      "safety_flags": 3,
      "quality_flags": 1,
      "last_activity": "2026-04-29T16:42:00Z"
    }
  ]
}
```

Auth: site_manager sees own site only; pm/gm/admin sees all accessible sites.

---

## 4.19 Server-side search (P2)

`GET /api/search?q=&from=&to=&scope=topics|actions|reports&site=`

Full-text-ish scan over DynamoDB report data. UI presents this alongside the existing client-side entity search; user picks scope.

```json
{
  "results": [
    {
      "type": "topic",
      "date": "2026-04-22",
      "user": "Jarley_Trainor",
      "topic_id": "3",
      "title": "Concrete pour Block C",
      "snippet": "...the pour was delayed by 40 min due to truck arrival...",
      "score": 0.81
    }
  ],
  "total": 12
}
```

Auth: results filtered to caller's accessible users + sites. Empty: `{ "results": [], "total": 0 }`.

---

## 4.20 Programme — task graph (Stage D — pending Stage D backend build)

> **Status as of 2026-05-06:** Backend not yet implemented. UI runs on `programme.fixture.js`. This section is the contract the backend will be built against.

### 4.20.1 GET programme metadata

`GET /api/programmes/{programme_id}`

```json
{
  "programme_id": "prog_roskill_main",
  "name": "Roskill main programme",
  "site_id": "site_001",
  "baseline_set_at": "2026-01-15",
  "task_count": 142,
  "updated_at": "2026-04-29T16:00:00Z"
}
```

### 4.20.2 GET tasks

`GET /api/programmes/{programme_id}/tasks?from=YYYY-MM-DD&to=YYYY-MM-DD`

```json
{
  "tasks": [
    {
      "task_id": "T_0042",
      "parent_id": "T_0040",
      "name": "Concrete pour Block C",
      "start": "2026-04-22",
      "end": "2026-04-22",
      "duration_days": 1,
      "assignees": ["Jack Gibson"],
      "depends_on": ["T_0038", "T_0041"],
      "is_group": false,
      "progress_pct": 100,
      "baseline_start": "2026-04-20",
      "baseline_end": "2026-04-20",
      "float_days": 2
    }
  ]
}
```

Field source of truth: `scripts/fixtures/programme.fixture.js`. Auth: site_manager+ read.

### 4.20.3 POST task

`POST /api/programmes/{programme_id}/tasks`

Body matches the task object above (without `task_id` — server generates). Returns the created task. Auth: pm+ write.

### 4.20.4 PATCH task

`PATCH /api/programmes/{programme_id}/tasks/{task_id}`

Partial update. Body keys: any subset of `name`, `start`, `end`, `duration_days`, `assignees`, `depends_on`, `progress_pct`, `parent_id`. Returns the updated task. Auth: pm+ write.

### 4.20.5 DELETE task

`DELETE /api/programmes/{programme_id}/tasks/{task_id}`

Soft-delete (sets `deleted=true`, retains for audit). Cascade deletes children unless `?cascade=false`. Returns `{ "deleted": ["T_0042", "T_0043"] }`. Auth: pm+ write.

### 4.20.6 POST bulk import

`POST /api/programmes/{programme_id}/tasks/bulk`

```json
{
  "mode": "replace" | "append" | "merge",
  "tasks": [ { "task_id": "T_001", "name": "...", "start": "...", "...": "..." } ]
}
```

Browser parses XLSX/CSV/XML via SheetJS into `tasks[]` before POST. Backend validates schema and writes; does NOT parse spreadsheets. Response:
```json
{ "imported": 142, "errors": [{ "row": 23, "message": "missing start" }] }
```

Auth: pm+ write.

---

## 4.21 Meetings list (Stage E — pending backend build)

> **Status:** Backend not yet built. Today the UI fetches meeting JSONs directly via the presigner. This endpoint will let the UI list available meetings without a presigner round-trip.

`GET /api/meetings?from=YYYY-MM-DD&to=YYYY-MM-DD&site=`

```json
{
  "meetings": [
    {
      "date": "2026-04-22",
      "title": "Weekly site walk",
      "key": "meeting_minutes/2026-04-22/weekly_site_walk.json",
      "attendees": ["Ben", "Sam", "Jack Gibson"],
      "duration_minutes": 47
    }
  ]
}
```

Auth: site_manager+ for the queried site. Empty: `{ "meetings": [] }`.
