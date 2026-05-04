# PLAN_SPRINT8.md — FieldSight UI · Sprint 8: Production Readiness

> **Branch:** `plan/sprint-8`  
> **Prerequisite:** Sprint 7 fully shipped (team page, settings page, strategic dashboards, dark-mode polish, Cognito scaffold)  
> **Theme:** From prototype to production-capable — backend wiring, write flows, mobile-deep, accessibility, performance, and demo quality

---

## What Remains After Sprint 7

Sprint 7 closes the last planned feature pages (`/team`, `/settings`, strategic dashboards) and finishes dark-mode polish. After that merge, the codebase has **zero unimplemented nav slots** but carries a set of systemic gaps that a real production app needs:

| Category | Gap | Root Cause |
|---|---|---|
| Backend integration | All API calls use `useMocks=true`; real HTTP never exercised | Phase I scaffolded but not wired |
| Write flows | Entire app is read-only; completion/creation calls are mocked | No backend PATCH/POST endpoints exercised |
| Programme persistence | Edit/add/delete mutate in-memory; page reload resets | Same mock-only constraint |
| Excel import | Only CSV + MS Project XML supported | SheetJS deferred (license check) |
| Programme depth | No slack/float display, no over-allocation warning | Deferred post-Sprint 5 |
| Mobile | Sprint 6 did responsive foundations; no bottom-nav, no swipe, Gantt unscrollable on phone | Sprint 6 scoped to layout only |
| Accessibility | Semantic HTML + basic ARIA done; no screen-reader pass, focus management gaps in modals | Never formally audited |
| Global search | No search entry point in shell | Never planned |
| Error / offline | Loading spinners exist; no offline banner, no skeleton loaders, no reconnect UX | Empty states exist, not error paths |
| Performance | No pagination; evidence/activity/tasks pages fetch all items | Acceptable for prototype, breaks at scale |
| Fixture quality | Only 2026-04-29 has real content; 1 site with full data | Prototype fixture, insufficient for demos |
| Print / share | No `@media print`, no copy-link, no export CTA | Never planned |
| Onboarding / help | No first-run guide, no keyboard shortcut reference | Never planned |

---

## Sprint 8 Architecture Constraints (carry forward)

All Sprint 0 constraints remain in force:

- **No build step** — no npm, webpack, vite; browser Babel only
- **BEM naming** — `.fs-{block}__{element}--{modifier}`
- **Tokens only** — no hardcoded colors/spacing; always `var(--fs-*)`
- **Token sync** — edit `tokens.css` → also edit `fs-globals.js`
- **Component export** — `window.FieldSight.{Name}` IIFE pattern
- **Page register** — `window.FieldSight.PAGES['/route'] = { Middle, Right }`
- **Reduced motion** — every new animation needs `@media (prefers-reduced-motion: reduce)` guard
- **Cache busters** — bump `?v=N` on every changed file loaded by a preview HTML
- **`node --check`** — run on every modified JS before committing

New constraint for Sprint 8:
- **SheetJS CE (Community Edition)** is MIT-licensed and CDN-loadable; acceptable for XLSX import
- **No service worker** — offline cache via SW would require a build step; use network-state detection only
- **No WebSocket** — real-time via polling only until backend exposes a WS endpoint

---

## Sprint 8 Sub-Sprint Plan

### Sprint 8.0 — Backend Integration Foundation

**Goal:** `FS.api.useMocks = false` works end-to-end for all read operations.

**Why first:** Every subsequent sub-sprint that touches write flows or real data depends on a working HTTP layer.

#### 8.0.1 — `api/_fetch.js` hardening

File: `scripts/api/_fetch.js`

- Add exponential-retry (3 attempts, 1 s / 2 s / 4 s) for transient 5xx and network errors
- Add request-timeout (10 s default, overridable per-call)
- Add `X-Request-Id` header (UUID v4, browser-side) for server-side correlation
- Extend `isJsonResponse()` guard already present (BUG-20) to also catch `text/html` with status 200 (CloudFront cached-HTML trap)
- Export `FS.api.setBaseUrl(url)` helper so preview HTML can point at staging vs local backend

Pre-check: `grep -n "isJsonResponse" scripts/api/_fetch.js`  
Verify: Pass `useMocks=false` + a real base URL → network tab shows JSON responses; 503 triggers retry

#### 8.0.2 — Cognito JWT real flow

Files: `scripts/auth/cognito.js`, `scripts/auth/session.js`

- Replace scaffolded stub with real Cognito Hosted UI redirect flow
- `session.js` stores `id_token` + `access_token` + `refresh_token` in `sessionStorage`
- Auto-refresh: if token expiry < 60 s away, call Cognito `/token` with `refresh_token` before next API call
- `FS.api._fetch` reads `FS.session.getAccessToken()` and attaches `Authorization: Bearer …` header
- `LoginScreen` composite already wired to `SessionGate`; no changes needed to render layer
- Expose `FS.auth.logout()` → clears storage + redirects to Cognito logout endpoint

Pre-check: `grep -n "SessionGate\|cognito\|session" scripts/app-shell.js`  
Verify: Open app with `useMocks=false`; unauthenticated → redirects to Cognito; after login → returns with token in sessionStorage

#### 8.0.3 — Page provider error states

Files: `scripts/pages/*.js` (all 11 providers)

All page Context Providers currently only handle loading + data states. Add:

- `error` state slot in each provider (`useState({ loading, data, error })` pattern)
- On fetch failure, set `error` to `{ code, message, retryable }` 
- Middle column: render `<ErrorBanner>` component (new, see 8.7.1) instead of content when `error` is set
- Right detail: render `<ErrorBanner mini>` when detail fetch fails
- "Retry" button calls the provider's fetch function again
- Clear `error` on successful retry

Pre-check: `grep -n "useState\|loading\|error" scripts/pages/today.js`

#### 8.0.4 — `useMocks` toggle in preview HTML

File: `app-shell-preview.html`

- Add `?mocks=0` query param that sets `window.FS.api.useMocks = false` before page boot
- Add a dev-panel indicator (alongside existing role switcher) showing `MOCK` vs `LIVE` badge
- Document the `?baseUrl=https://…` param in a comment block at top of file

Pre-check: `grep -n "useMocks\|dev=1" app-shell-preview.html`

---

### Sprint 8.1 — Write Flows: Action Items, Safety, Quality

**Goal:** Users can complete actions, raise safety observations, and create quality items — with optimistic UI + revert on failure.

**Why here:** Read-only prototype is not useful for user-acceptance testing. Write flows validate interaction patterns.

#### 8.1.1 — Action item completion to real backend

Files: `scripts/api/actions.js`, `scripts/api/actions-bus.js`

- `toggleAction(id, done)` currently mutates in-memory fixture
- When `useMocks=false`: PATCH `/api/actions/{id}` with `{ done: true/false }`
- On success: publish to `actions-bus` (already handles cross-component sync)
- On failure: publish revert event to `actions-bus` → checkbox returns to prior state; show toast error
- Add `FS.api.actions.createAction(payload)` → POST `/api/actions` for new action items (used by 8.1.3)

Pre-check: `grep -n "toggleAction\|actions-bus" scripts/api/actions.js`

#### 8.1.2 — Safety observation creation

Files: `scripts/composites/safety-flag-row.js`, `scripts/pages/safety.js`, new `scripts/composites/safety-create-modal.js`

New composite: `SafetyCreateModal`

```
SafetyCreateModal props:
  siteId, onSuccess(newFlag), onCancel
Fields:
  observation (Textarea, required)
  risk_level  (Select: low | medium | high, required)
  recommended_action (Textarea, optional)
  location (Input, optional)
  photos (file input, multiple, accept image/*, max 5)
  
Submit: POST /api/safety-observations
  body: { site_id, observation, risk_level, recommended_action, location, photo_keys[] }
  photo_keys: from presigned PUT uploads via FS.api.media.presignedPut()
  
On success: close modal, call onSuccess(newFlag) → SafetyProvider prepends to list
```

CSS: `.fs-safety-create-modal` in `styles/composites.css` — reuse modal-overlay scaffold

`/safety` page:
- Add "+ Raise Observation" `IconButton` in middle column header (gated: `hse_manager` or `site_manager`)
- Clicking opens `SafetyCreateModal`

Pre-check: `grep -n "hse_manager\|canDo.*safety" scripts/pages/safety.js`

#### 8.1.3 — Quality item creation

Files: `scripts/pages/quality.js`, new `scripts/composites/quality-create-modal.js`

Mirrors 8.1.2 pattern. New composite: `QualityCreateModal`

```
Fields:
  observation (Textarea, required)
  category    (Select: quality | compliance | workmanship, required)
  follow_up_required (Checkbox, default true)
  deadline    (DatePicker inline, optional)
  location    (Input, optional)

Submit: POST /api/quality-items
On success: prepend to QualityProvider list
```

`/quality` page:
- "+ Log Item" button gated: `quality_manager` or `site_manager`

#### 8.1.4 — Toast notification system

New file: `scripts/composites/toast.js`  
New CSS: `.fs-toast` block in `styles/composites.css`

- `FS.toast.show({ message, tone, duration })` — global API, no React
- Renders fixed-position stack (bottom-right, 4-item max)
- Tones: `success` (green), `error` (danger red), `warning` (amber), `info`
- Auto-dismiss after `duration` ms (default 4000), pause on hover
- Reduced motion: skip slide-in animation, still dismiss after duration
- Used by: write-flow success/fail, retry events, import results

CSS tokens to use: `--fs-surface-overlay`, `--fs-shadow-md`, existing tone variables

---

### Sprint 8.2 — Programme Persistence + Excel Import

**Goal:** Programme changes survive page reload; XLSX import joins CSV/XML.

#### 8.2.1 — Programme real PATCH/POST/DELETE

File: `scripts/api/programme.js`

When `useMocks=false`:

- `updateTask(programmeId, taskId, patch)` → PATCH `/api/programmes/{programmeId}/tasks/{taskId}`
- `createTask(programmeId, payload)` → POST `/api/programmes/{programmeId}/tasks`
- `deleteTask(programmeId, taskId)` → DELETE `/api/programmes/{programmeId}/tasks/{taskId}`
- `importTasks(programmeId, tasks[])` → POST `/api/programmes/{programmeId}/tasks/bulk`
- On any write: cascade engine (`programme-schedule.js`) still runs client-side; result sent to backend as bulk patch

Optimistic pattern (same as actions):
1. Apply mutation to local state immediately
2. Fire API call
3. On failure: rollback + toast error

Pre-check: `grep -n "updateTask\|createTask\|deleteTask" scripts/api/programme.js`

#### 8.2.2 — Excel XLSX import

Files: `scripts/api/programme-import.js`, `scripts/composites/programme-import-modal.js`  
New CDN dep: SheetJS CE (`xlsx.full.min.js`, CDN UMD, MIT license)

Add to `app-shell-preview.html` (load before composites):
```html
<script src="https://cdn.sheetjs.com/xlsx-0.20.3/package/dist/xlsx.full.min.js"></script>
```

`programme-import.js`:
- Add `parseXLSX(file)` → reads `ArrayBuffer` via `XLSX.read()` → first sheet → row array
- Expect same column names as CSV spec (Name, Start, End, Assignee, Status, Progress, WBS_Parent)
- Fallback: if first row doesn't match, show column-mapper UI (see below)

`ProgrammeImportModal`:
- File drop zone now accepts `.csv`, `.xml`, `.xlsx`, `.xls`
- Detect filetype by extension + MIME; route to correct parser
- If XLSX column names don't match: show a column-mapper step (dropdown per required field)
- Validation + confirm steps unchanged

Pre-check: `grep -n "parseCSV\|parseMSProjectXML" scripts/api/programme-import.js`

---

### Sprint 8.3 — Programme Deep Features

**Goal:** Gantt becomes a real scheduling tool — slack, over-allocation, baseline.

#### 8.3.1 — Slack / float column

Files: `scripts/api/programme-schedule.js`, `scripts/composites/gantt-row.js`, `styles/composites.css`

`programme-schedule.js`:
- CPM already computes `early_start`, `early_finish`, `late_start`, `late_finish`
- Derive `total_float = late_start - early_start` (in days) per task
- Expose on each task object as `_float`

`GanttRow`:
- Add optional "Float" column (after Task Name, before timeline bars)
- Show `_float` as pill: `0d` = critical (danger tone), `1-3d` = amber, `>3d` = success tone
- Column toggled by a "Show float" checkbox in Gantt toolbar

CSS: `.fs-gantt-float-pill` — pill shape, tone colours from existing badge tokens

#### 8.3.2 — Over-allocation warning

Files: `scripts/api/programme-schedule.js`, `scripts/pages/programme.js`, new `scripts/composites/over-allocation-banner.js`

`programme-schedule.js`:
- Add `detectOverAllocations(tasks)` → for each (user, date) pair, count tasks in-flight
- Return `overAllocationMap: { [userId]: [date[]] }` where count > 1

`OverAllocationBanner`:
- Renders above Gantt if `overAllocationMap` is non-empty
- Lists affected users + date ranges: "Maria Chen is double-booked 3–5 May"
- Dismissible (per session); re-appears on next mutation that creates new conflicts
- Gated: visible only to `programme:manage` holders

CSS: `.fs-over-allocation-banner` — amber surface, warning icon, collapsible chevron

#### 8.3.3 — Baseline snapshot

Files: `scripts/api/programme.js`, `scripts/pages/programme.js`, `scripts/composites/gantt-row.js`

- "Save baseline" button in Gantt toolbar (gated `programme:manage`)
- Saves snapshot of all task `{ start, end, status }` to `localStorage` keyed by `programmeId`
- "Compare baseline" toggle: renders ghost bars behind current bars (opacity 0.3, dashed outline)
- Ghost bar style: `.fs-gantt-bar--baseline` — use `--fs-border-subtle` + dashes
- Tooltip on ghost bar: "Planned: {start} → {end}"

---

### Sprint 8.4 — Mobile Deep Responsive

**Goal:** Full usability on 375 px (iPhone SE) and 414 px (iPhone 14) screens. All 12 pages.

#### 8.4.1 — Bottom navigation bar (mobile)

Files: `styles/app-shell.css`, `scripts/app-shell.js`

At `max-width: 767px`:
- Left nav collapses completely (display: none)
- Bottom nav bar appears: fixed, full-width, 56 px tall (touch-safe)
- Shows top 5 nav items (Today, Timeline, Tasks, Safety, More…)
- "More…" opens a drawer with remaining nav items (slide-up sheet)
- Active item gets accent underline, icon + label below icon

CSS class: `.fs-bottom-nav` + `.fs-bottom-nav__item` + `.fs-bottom-nav__more-sheet`

Right-detail pane:
- At `max-width: 767px`: collapses to zero width by default
- Selecting a list item slides it in over the middle column (full-width, with ← Back button)
- Back button calls `FS.shell.closeDetail()` (new AppShell method)

#### 8.4.2 — Programme Gantt on mobile

File: `styles/composites.css`, `scripts/composites/gantt-strip.js`, `scripts/pages/programme.js`

- At `< 768px`: default to Board view (Gantt is inherently wide)
- Gantt accessible via explicit "Gantt" toggle button even on mobile
- Gantt in mobile: horizontal scroll enabled (`overflow-x: scroll`), timeline header sticky-top
- WBS tree column fixed at 160 px (truncated with ellipsis)
- Pinch-zoom on timeline area: scale `--fs-gantt-day-width` CSS var with `GestureEvent` / `PointerEvent` pair
- Drag disabled on mobile (too error-prone on touch); show edit modal on tap

#### 8.4.3 — Breakpoint audit — all 12 pages

Breakpoints to test: 375 px, 414 px, 768 px (tablet), 1024 px, 1440 px

Pages to audit (touch each file if CSS fix needed):

| Page | Common mobile issues |
|---|---|
| `/today` | KpiStrip wraps; MorningBrief collapses well (already tested) |
| `/timeline` | TopicCard sections stack correctly; transcript list line-length |
| `/sites` | SiteCard grid → single column |
| `/activity` | UserActivityCard 4-count strip → 2-column grid |
| `/tasks` | FilterChips wrap to 2 rows |
| `/evidence` | Tab labels truncate; PhotoGrid 2-col → 1-col |
| `/programme` | See 8.4.2 |
| `/safety` | SafetyFlagRow right-truncation |
| `/quality` | Same pattern as safety |
| `/reports` | Table → card list |
| `/team` | (Sprint 7 new page) grid → single column |
| `/settings` | (Sprint 7 new page) form layout |

For each fix: add breakpoint rule in `styles/composites.css` or `styles/app-shell.css`; no inline styles.

#### 8.4.4 — Swipe-to-go-back gesture (mobile)

File: `scripts/router.js`

- Track `touchstart` / `touchend` on `document`
- If swipe starts within 20 px of left edge, travels > 80 px right, and velocity > 0.3 px/ms → call `history.back()`
- Guard: only fires when Right detail panel is open (mobile full-screen mode)
- No animation needed on the page transition (browser default suffices)

---

### Sprint 8.5 — Accessibility Audit (WCAG 2.1 AA)

**Goal:** Pass WCAG 2.1 AA for all implemented pages.

#### 8.5.1 — Color contrast audit

File: `styles/tokens.css`

Run automated contrast check (axe-core or manual calc) against all `--fs-text-*` on `--fs-surface-*` combinations.

Known risk areas:
- `--fs-text-subtle` on `--fs-surface-base` (light mode): target ≥ 4.5:1
- Safety orange `#FF6B35` as text on white: likely fails; never use as body text — only as icon/border/accent (already the pattern; verify no violations crept in)
- Dark-mode surface-on-surface combinations (Sprint 7 added many)

For any failing pair: darken text token or lighten surface token; update both `tokens.css` and `fs-globals.js`.

#### 8.5.2 — Focus management: modals and drawers

Files: `scripts/composites/modal-overlay.js`, `scripts/composites/right-drawer.js`, `scripts/composites/programme-task-editor.js`, `scripts/composites/programme-import-modal.js`

Pattern to implement in all modal/drawer composites:
1. On open: move focus to first focusable element inside (`input`, `button`, `[href]`, `[tabindex="0"]`)
2. Focus trap: Tab/Shift+Tab cycle within the modal; never escape to background
3. ESC: already closes — ensure focus returns to the trigger element
4. `aria-modal="true"` on the container
5. Background content: `aria-hidden="true"` while modal is open

Pre-check: `grep -n "aria-modal\|aria-hidden\|focusTrap" scripts/composites/modal-overlay.js`

#### 8.5.3 — Skip navigation link

File: `scripts/app-shell.js`, `styles/app-shell.css`

- Add `<a href="#fs-main-content" class="fs-skip-nav">Skip to main content</a>` as first child of `<body>`
- Visible only on focus (`.fs-skip-nav:focus { clip: auto; position: static }`)
- Middle column's root element: `id="fs-main-content"` + `tabindex="-1"` (so programmatic focus works)

#### 8.5.4 — ARIA live regions for dynamic content

Files: affected composites

Regions needed:
- Filter chip count changes (`/tasks`, `/safety`, `/quality`): `aria-live="polite"` on count badge
- Toast notifications (8.1.4): `role="status"` + `aria-live="polite"` on toast container
- Action item toggle ("Marked complete"): `aria-live="polite"` announcement
- Page title on route change: `document.title` update + `aria-live` region announcing page name

#### 8.5.5 — Keyboard operability for Gantt drag

File: `scripts/composites/gantt-row.js`

Mouse-drag only is an accessibility failure. Add keyboard alternative:
- Gantt bar has `tabindex="0"` + `role="slider"` (horizontal date range)
- `ArrowLeft` / `ArrowRight`: move start date by 1 day (shift whole task)
- `Shift+ArrowLeft` / `Shift+ArrowRight`: extend/shrink end date by 1 day
- `Enter` / `Space`: open ProgrammeTaskEditor modal
- `aria-valuemin` / `aria-valuemax` / `aria-valuenow`: bind to task start in days-from-project-start
- Visual focus ring: `outline: 2px solid var(--fs-focus-ring)` (token already exists)

#### 8.5.6 — Screen reader pass

Manual checklist (run with NVDA on Firefox, VoiceOver on Safari):

- [ ] All images have meaningful `alt` (photos use filename as fallback alt)
- [ ] All icon-only buttons have `aria-label`
- [ ] Table headers in reports page use `<th scope="col">`
- [ ] Timeline collapsible topics use `aria-expanded`
- [ ] DatePicker calendar grid: `role="grid"`, `role="gridcell"`, `aria-selected`, `aria-label="date"`
- [ ] Programme Kanban columns: `role="list"`, cards `role="listitem"`
- [ ] Status badges have `aria-label` (not just visual color tone)

---

### Sprint 8.6 — Global Search

**Goal:** Cmd/Ctrl+K opens a search palette; searches across tasks, sites, users, safety flags.

#### 8.6.1 — Search palette composite

New file: `scripts/composites/search-palette.js`  
New CSS: `.fs-search-palette` in `styles/composites.css`

```
SearchPalette:
  - Full-screen modal overlay (reuse ModalOverlay)
  - Input at top (auto-focused on open)
  - Results list below: grouped by entity type (Tasks / Safety / Sites / Users)
  - Each result: icon + title + subtitle + keyboard navigation (up/down arrows)
  - Enter: navigates to the entity's canonical page with deep-link params
  - Empty state: "No results for 'X'" 
  - Recent searches: stored in sessionStorage, shown when input is empty
```

Search scope (client-side, searches local mock/fetched data):
- **Tasks**: search `action_items[].text` across loaded timeline data
- **Safety**: search `safety_flags[].observation` + `safety_observations[].observation`
- **Sites**: search `sites[].name` + `sites[].location`
- **Users**: search `users[].name` + `users[].role`
- **Reports**: search `reports[].title` + date

Keyboard shortcut: `Cmd+K` / `Ctrl+K` on `document` (added in `app-shell.js`)

#### 8.6.2 — Search button in app shell header

File: `scripts/app-shell.js`, `styles/app-shell.css`

- Add a search icon button to the top-right of the middle column header bar
- Mobile: search icon in bottom nav bar (replaces one of the 5 slots)
- Clicking opens `SearchPalette`

---

### Sprint 8.7 — Error States & Offline Polish

**Goal:** App degrades gracefully when network is unavailable or API returns errors.

#### 8.7.1 — ErrorBanner composite

New file: `scripts/composites/error-banner.js`  
New CSS: `.fs-error-banner` in `styles/composites.css`

```
ErrorBanner props:
  message (string)
  retryable (bool)
  onRetry (fn)
  mini (bool) — compact variant for right detail panel

Renders:
  - Warning icon + message + optional Retry button
  - Tone: danger surface (--fs-surface-danger-subtle)
  - mini variant: inline row, no full-width
```

Used by: all page providers (Sprint 8.0.3), right-detail panels.

#### 8.7.2 — Offline detection banner

File: `scripts/app-shell.js`, `styles/app-shell.css`

- Listen to `window.onfline` / `window.onoffline` events
- When offline: show sticky banner below app header: "You're offline — changes won't sync"
- Tone: amber surface (warning), auto-dismiss when `online` event fires
- CSS class: `.fs-offline-banner` — `position: sticky; top: 0; z-index: var(--fs-z-overlay)`

#### 8.7.3 — Skeleton loaders

New CSS: `.fs-skeleton` + `.fs-skeleton--text`, `.fs-skeleton--avatar`, `.fs-skeleton--card` in `styles/components.css`

Pattern:
```css
.fs-skeleton {
  background: linear-gradient(90deg, var(--fs-surface-subtle) 25%, var(--fs-surface-muted) 50%, var(--fs-surface-subtle) 75%);
  background-size: 200% 100%;
  animation: fs-skeleton-shimmer 1.5s infinite;
}
@media (prefers-reduced-motion: reduce) {
  .fs-skeleton { animation: none; background: var(--fs-surface-subtle); }
}
```

Apply skeleton loading to:
- `TodayProvider`: while loading, render 3 skeleton TaskCard rows + 1 skeleton UrgentCard
- `TimelineProvider`: skeleton TopicCard rows (3 of them)
- `SitesProvider`: skeleton SiteCard rows
- `ProgrammeProvider`: skeleton Gantt rows (5 rows, varying widths)

---

### Sprint 8.8 — Performance & Pagination

**Goal:** Pages remain fast with large datasets (100+ tasks, 30+ days of activity).

#### 8.8.1 — Tasks page pagination

Files: `scripts/api/tasks-aggregator.js`, `scripts/pages/tasks.js`

Current: `getActionsRange(from, to)` fetches all 14 days at once.

Add:
- `pageSize = 25` actions per page
- "Load more" button (same pattern as `/activity` already uses)
- Client-side: filter on already-loaded items first; fetch more only when needed
- Backend: accept `?limit=25&offset=N` when `useMocks=false`

#### 8.8.2 — Evidence page pagination

File: `scripts/pages/evidence.js`

Currently fetches all 7 days of photos on Photos tab activation.

Add:
- Default: load 3 days of data
- "Load more" fetches 3 more days
- Each additional load appends to `PhotoGrid` without remounting
- Audio/Video tabs: same pattern (3 days default)

#### 8.8.3 — Virtual list for long Gantt

File: `scripts/pages/programme.js`, `scripts/composites/gantt-row.js`

For programmes with > 50 tasks:
- Measure viewport height of Gantt scroll container
- Render only rows within `scrollTop ± 200 px` (overscan 200 px top + bottom)
- Use CSS transform (`translateY`) on a single wrapper `div` to position visible rows
- Total height placeholder: `div` with fixed height = `totalRows × rowHeight`
- `rowHeight = 44px` (matches existing Gantt row CSS)
- Board view unaffected (already card-grid, browser handles)

This avoids full virtual-list library (no build step); simple custom implementation ~80 lines.

#### 8.8.4 — Presigned URL token refresh

File: `scripts/api/media.js`

Presigned S3 URLs expire in 15 min. Current: no refresh.

Add:
- `presignedUrlCache: Map<key, { url, expiresAt }>` in module scope
- On each media request: if `expiresAt - now < 2 min`, re-fetch before returning
- `PhotoGrid` and `VideoPlayer` call `FS.api.media.getUrl(key)` instead of storing URL directly

---

### Sprint 8.9 — Fixture Expansion & Demo Quality  ✅ shipped

**Status (2026-05-04):** Done — fixtures expanded (`dates`, `daily-report`,
`programme`); `DemoTour` composite ships with `?demo=1` activation.

**Goal:** The mock dataset is rich enough for credible demos with any role, across multiple sites and dates.

#### 8.9.1 — Fixture data expansion

Files: `scripts/mock/*.fixture.js`

**`dates.fixture.js`** (currently: 1 date with data):
- Add 30 dates: 2026-04-01 through 2026-04-30
- Mix: 18 days with full reports, 8 days with meeting-minutes only, 4 days empty
- Heatmap intensity: varied `report_count` per date (1–4)

**`daily-report.fixture.js`** (currently: 1 report, 2026-04-29):
- Add 4 more full reports: 2026-04-28, 2026-04-25, 2026-04-24, 2026-04-23
- Vary: different users per report, different risk levels, different topic counts
- Include one "all clear" day (no safety flags, all actions done)
- Include one "high risk" day (3 high-risk flags, 1 urgent item)

**`sites.fixture.js`** (currently: 2 sites, 4 users):
- Add 1 more site (total 3)
- Add 4 more users (total 8): 2 workers, 1 foreman, 1 hse_manager
- Assign users across sites (not all on one site)

**`programme.fixture.js`** (currently: 1 programme, 14 tasks):
- Add overdue tasks (end date in the past, status not "done")
- Add blocked tasks (status "blocked", assigned to user with other conflicts)
- Add a completed sub-group to test Board "Done" column with real items

#### 8.9.2 — Demo tour mode

New file: `scripts/composites/demo-tour.js`  
New CSS: `.fs-demo-tour` in `styles/composites.css`

Activated by `?demo=1` URL param.

```
DemoTour:
  Steps array: [
    { route: '/today', highlight: '.fs-kpi-strip', text: "Your day at a glance" },
    { route: '/today', highlight: '.fs-urgent-card', text: "Urgent items surface automatically" },
    { route: '/timeline?date=2026-04-29&user=Jarley_Trainor', highlight: '.fs-topic-card', text: "Tap any topic to expand" },
    { route: '/programme', highlight: '.fs-gantt-row', text: "Drag tasks to reschedule" },
    { route: '/safety', highlight: '.fs-safety-flag-row', text: "Safety flags with risk levels" },
  ]
  
  Renders: bottom-center tooltip card with step counter + Prev/Next/Done buttons
  Highlight: box-shadow ring on `.highlight` element (CSS class `fs-demo-highlight`)
  Does not block interaction (no overlay)
```

---

### Sprint 8.10 — Print / Export / Share  ✅ shipped

**Status (2026-05-04):** Done — `@media print` rules in `app-shell.css`,
copy-link / share button in middle-column header, batch export of the
month's reports on `/reports`.

**Goal:** Reports and safety summaries can be printed or shared via link.

#### 8.10.1 — Print CSS

File: `styles/app-shell.css`

Add `@media print` rules:
- Hide: left nav, right detail, drag divider, dev switcher, bottom nav, toast stack
- Middle column: full page width, no max-width
- Remove all `box-shadow`, `border-radius` (printer-safe)
- Force light mode (even if dark-mode active): `color-scheme: light`
- Page break: `page-break-inside: avoid` on `TopicCard`, `SafetyFlagRow`, `StatCard`
- Header: print the page title + current date as a `::before` pseudo on `#fs-main-content`

Pages where print is most useful: `/timeline`, `/reports`, `/safety`, `/quality`

#### 8.10.2 — Copy-link CTA

File: `scripts/app-shell.js`, `styles/app-shell.css`

- Add a share icon button in the middle column header (next to search button from 8.6.2)
- Clicking calls `navigator.clipboard.writeText(window.location.href)`
- On success: show toast "Link copied" (success tone)
- On failure (clipboard denied): show toast with the URL text to copy manually
- Mobile: prefer `navigator.share()` if available (Web Share API)

#### 8.10.3 — Export trigger for Reports page

File: `scripts/pages/reports.js`

The Reports page already has a "Download" button that calls `FS.api.reports.getPresignedUrl()`.

Add:
- "Export all (this month)" button → iterates presigned URLs for the month → triggers download for each
- Batch export limit: 10 files max; show warning if more
- Loading state on button during fetch

---

### Sprint 8.11 — In-App Help & Onboarding  ✅ shipped

**Status (2026-05-04):** Done — `OnboardingOverlay` (3-step welcome,
role-aware copy, dismissible, resettable from `/settings`); `?` opens a
keyboard shortcut reference modal; `Tooltip` composite available for
contextual hints. Single-key navigation (T/S/P) wired in `AppShell`.

**Goal:** New users can orient themselves without external documentation.

#### 8.11.1 — First-run onboarding overlay

New file: `scripts/composites/onboarding-overlay.js`  
New CSS: `.fs-onboarding` in `styles/composites.css`

Triggered on first visit (detected via `localStorage.setItem('fs.onboarded', '1')`):

```
Overlay: centred card, max-width 480px
Step 1: "Welcome to FieldSight" — logo + one-line description
Step 2: Role-aware message (e.g. for site_manager: "You'll see your site's reports, tasks, and safety flags")
Step 3: "Start with Today →" button navigates to /today and sets onboarded flag

Skip button: sets flag immediately, dismisses
Reduced motion: no slide animations, just fade
```

Reset link in `/settings` page: "Reset onboarding" → clears flag.

#### 8.11.2 — Keyboard shortcut reference

File: `scripts/app-shell.js`

- Pressing `?` (when no input is focused) opens a `ModalOverlay` listing shortcuts
- Shortcuts to document:

| Key | Action |
|---|---|
| `Cmd/Ctrl+K` | Open search palette |
| `?` | This help modal |
| `Escape` | Close modal / drawer / detail panel |
| `ArrowLeft/Right` | Gantt task date shift (when bar focused) |
| `T` | Navigate to Today |
| `S` | Navigate to Safety |
| `P` | Navigate to Programme |

CSS: reuse existing `.fs-modal-overlay` + standard table styling

#### 8.11.3 — Contextual tooltips for complex features

File: new `scripts/composites/tooltip.js`  
New CSS: `.fs-tooltip` in `styles/composites.css`

Lightweight tooltip (not a full library):
```
Tooltip props: content (string), placement (top|bottom|right), delay (ms, default 600)
Trigger: wraps any element; shows on hover + focus
Dismiss: mouseout + blur
Positioning: CSS absolute relative to wrapper + `calc()` to center
Max-width: 240px, multiline OK
```

Apply tooltips to:
- Programme toolbar icons (Save Baseline, Show Float, Import)
- Evidence tab icons (when labels truncate on mobile)
- KpiStrip stat cards (explain what each metric means)
- Role switcher dev panel (?dev=1 mode)

---

## Sprint 8 Delivery Checklist

Before closing Sprint 8:

### Code quality
- [ ] `node --check` passes on every modified JS file
- [ ] No hardcoded colors or spacing values (tokens only)
- [ ] Every `@keyframes` block has a `prefers-reduced-motion` override
- [ ] `tokens.css` and `fs-globals.js` are in sync
- [ ] Cache busters bumped on every changed file in `app-shell-preview.html`
- [ ] `components-preview.html` updated with any new L5 composites

### Accessibility
- [ ] All new UI elements have keyboard operability
- [ ] All new icon buttons have `aria-label`
- [ ] New modal/drawer composites have focus trap + `aria-modal`
- [ ] Screen reader smoke-test (NVDA/VoiceOver) on `/today` and `/programme`

### Mobile
- [ ] All 12 pages usable at 375px (iPhone SE viewport)
- [ ] Bottom nav renders correctly; "More" sheet opens/closes
- [ ] Gantt defaults to Board on mobile
- [ ] No horizontal overflow on any page at 375px (check with DevTools)

### Performance
- [ ] `/tasks` page with 100+ mock actions does not block the main thread
- [ ] Virtual Gantt renders correctly with 50+ tasks fixture
- [ ] Evidence page lazy-loads tabs (network tab: no photo fetch until Photos tab clicked)

### Write flows
- [ ] Action item toggle works with `useMocks=false` (needs real backend or intercept mock)
- [ ] SafetyCreateModal validates required fields before POST
- [ ] QualityCreateModal validates required fields before POST
- [ ] Toast appears on success and failure for all write operations

### Demo quality
- [ ] `?demo=1` launches tour without crashing on any page
- [ ] Role switcher with `hse_manager` shows safety create button
- [ ] Role switcher with `worker` hides team tasks, safety create, and quality create
- [ ] 3 sites, 8 users, 30 dates of fixture data confirmed in mock layer

---

## File Impact Summary

| File | Sprint 8 sub-sprint | Type |
|---|---|---|
| `scripts/api/_fetch.js` | 8.0.1 | Modify |
| `scripts/auth/cognito.js` | 8.0.2 | Modify |
| `scripts/auth/session.js` | 8.0.2 | Modify |
| `scripts/pages/*.js` (all 11) | 8.0.3 | Modify |
| `app-shell-preview.html` | 8.0.4, 8.2.2 | Modify |
| `scripts/api/actions.js` | 8.1.1 | Modify |
| `scripts/composites/safety-create-modal.js` | 8.1.2 | **New** |
| `scripts/composites/quality-create-modal.js` | 8.1.3 | **New** |
| `scripts/composites/toast.js` | 8.1.4 | **New** |
| `scripts/api/programme.js` | 8.2.1 | Modify |
| `scripts/api/programme-import.js` | 8.2.2 | Modify |
| `scripts/composites/programme-import-modal.js` | 8.2.2 | Modify |
| `scripts/api/programme-schedule.js` | 8.3.1, 8.3.2 | Modify |
| `scripts/composites/gantt-row.js` | 8.3.1, 8.3.3, 8.5.5, 8.8.3 | Modify |
| `scripts/composites/over-allocation-banner.js` | 8.3.2 | **New** |
| `scripts/pages/programme.js` | 8.3.3, 8.4.2, 8.8.3 | Modify |
| `styles/app-shell.css` | 8.4.1, 8.5.3, 8.7.2, 8.10.1 | Modify |
| `scripts/app-shell.js` | 8.4.1, 8.5.3, 8.6.2, 8.7.2, 8.10.2, 8.11.2 | Modify |
| `scripts/router.js` | 8.4.4 | Modify |
| `styles/tokens.css` | 8.5.1 | Modify |
| `scripts/composites/modal-overlay.js` | 8.5.2 | Modify |
| `scripts/composites/right-drawer.js` | 8.5.2 | Modify |
| `scripts/composites/programme-task-editor.js` | 8.5.2 | Modify |
| `styles/components.css` | 8.5.3, 8.7.3 | Modify |
| `scripts/composites/search-palette.js` | 8.6.1 | **New** |
| `scripts/composites/error-banner.js` | 8.7.1 | **New** |
| `scripts/api/tasks-aggregator.js` | 8.8.1 | Modify |
| `scripts/pages/tasks.js` | 8.8.1 | Modify |
| `scripts/pages/evidence.js` | 8.8.2 | Modify |
| `scripts/api/media.js` | 8.8.4 | Modify |
| `scripts/mock/dates.fixture.js` | 8.9.1 | Modify |
| `scripts/mock/daily-report.fixture.js` | 8.9.1 | Modify |
| `scripts/mock/sites.fixture.js` | 8.9.1 | Modify |
| `scripts/mock/programme.fixture.js` | 8.9.1 | Modify |
| `scripts/composites/demo-tour.js` | 8.9.2 | **New** |
| `scripts/pages/reports.js` | 8.10.3 | Modify |
| `scripts/composites/onboarding-overlay.js` | 8.11.1 | **New** |
| `scripts/composites/tooltip.js` | 8.11.3 | **New** |
| `styles/composites.css` | All sub-sprints | Modify |
| `fs-globals.js` | 8.5.1 (if token changes) | Modify |

**New files: 9**  
**Modified files: 30+**  
**Deleted files: 0**

---

## Effort Estimate (prototype context — no tests, no CI)

| Sub-sprint | Complexity | Estimated sessions |
|---|---|---|
| 8.0 Backend foundation | High (real auth flow) | 3 |
| 8.1 Write flows | Medium | 2 |
| 8.2 Programme persistence + XLSX | Medium | 2 |
| 8.3 Programme deep features | Medium | 2 |
| 8.4 Mobile deep responsive | High (12 pages) | 3 |
| 8.5 Accessibility audit | High (manual + code) | 3 |
| 8.6 Global search | Medium | 2 |
| 8.7 Error / offline | Low–Medium | 1 |
| 8.8 Performance + pagination | Medium | 2 |
| 8.9 Fixture expansion + demo tour | Low | 1 |
| 8.10 Print / export / share | Low | 1 |
| 8.11 Help & onboarding | Low | 1 |
| **Total** | | **~23 sessions** |

*One "session" ≈ one focused patch cycle (spec → implement → verify → commit).*

---

*Generated: 2026-05-03 · Branch: `plan/sprint-8` · FieldSight UI prototype*
