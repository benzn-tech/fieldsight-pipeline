# FieldSight UI — Plan

Single source of truth for what's done, what's pending, and what trips
us up. Skim by section: each one stands alone.

Per-sprint specs and detailed sub-task notes live in **commit history
+ merged PRs** — not here. This file is the action-list / decision
ledger.

---

## 1 · Completed work

| Sprint | Theme | Status | Where |
|---|---|---|---|
| **0** | L1 design tokens + L2 visual language + `tokens-reference.html` | ✅ | merged |
| **1** | L4 atoms + L3 AppShell + Today lo-fi (1.5–1.6 hotfixes, nav restructure) | ✅ | merged |
| **2** | Backend-shaped data layer (Phase A–I) — Today is now derived from real `DailyReport`; check-off animation; Ask agent | ✅ | merged |
| **3** | Polish backlog after Phase-I review (P-01 … P-12) | ✅ | merged |
| **4** | Core operational pages — Sites, Programme MVP, Tasks aggregator, Reports, Evidence, Activity, Weather UI | ✅ | merged |
| **5** | Programme operability — drag/edit, kanban, CSV/MS-Project XML import, role gates | ✅ | PR [#15](https://github.com/benzn-tech/fieldsight-ui/pull/15) |
| **6** | Compliance pair — `/safety` + `/quality` cross-day rollups, deep-link spotlight, photo carousel | ✅ | PR [#16](https://github.com/benzn-tech/fieldsight-ui/pull/16) |
| **7** | `/team` + `/settings` + dark-mode polish (theme + density + default-landing prefs) | ✅ | PR [#17](https://github.com/benzn-tech/fieldsight-ui/pull/17) |
| **8** | Backend integration foundation, write flows, programme deep features, mobile bottom-nav, a11y, search, error/offline, performance, fixture expansion, demo tour, print/share, onboarding | ✅ | PR [#18](https://github.com/benzn-tech/fieldsight-ui/pull/18) |
| **9** | Insights dashboard (PM-facing safety/quality analytics) + PM-scoped Team page + Strategic dashboards (Portfolio / Regional / Executive) | ✅ | PR [#19](https://github.com/benzn-tech/fieldsight-ui/pull/19) |
| **10** | Library / Template UI (B.0–B.6) + 3-panel → 2-panel migration (A) + follow-up polish | 🟡 | branch `claude/sprint10-prep` (awaiting review) |
| **11** | A11y hardening (axe-core gate + contrast + SR checklist) + XLSX column-mapper partial mapping + Tasks cross-day audit (Q-1) | 🟡 | branch `claude/sprint11` (stacked on sprint10-prep) |

**Sprint 8 sub-sprint coverage** (audit on `claude/sprint8`):

- ✅ 8.0 fetch hardening + Cognito + page error states + `useMocks` toggle
- ✅ 8.1 action PATCH + safety/quality create modals + toast
- ✅ 8.2 programme PATCH/POST/DELETE + XLSX import (SheetJS CDN)
- ✅ 8.3 float pill + over-allocation banner + baseline snapshot
- ✅ 8.4.1/2/4 bottom nav + mobile-Gantt-default-Board + swipe-back
- ✅ 8.5.2/3/4/5 focus mgmt + skip-nav + aria-live (route + action toggle) + Gantt keyboard
- ✅ 8.6 search palette + ⌘K / Ctrl+K
- ✅ 8.7 error banner + offline banner + skeleton loaders
- ✅ 8.8 tasks/evidence pagination + virtual Gantt (>50 rows) + presigned-URL cache
- ✅ 8.9 fixtures: 3 sites · 8 users · 30 days April 2026 + DemoTour `?demo=1`
- ✅ 8.10 print CSS + copy-link/share + batch export
- ✅ 8.11 onboarding overlay + `?` shortcut modal + Tooltip composite
- ✅ Sprint 8 follow-ups (browser walkthrough fixes):
  - Round 1 (`fbec744`): action-toggle aria-live announcer; components-preview.html registers all 9 new L5 composites
  - Round 2 (`4b43615`): BottomNav portal wrap (kills Programme leak in desktop sidebar); admin fan-out across all users for `/tasks` `/safety` `/quality` `/evidence`; modal `siteId` fallback to first fixture site so admin's "+ Raise Observation" / "+ Log Item" buttons actually open; dark-mode tint overrides for badge subtle / safety+quality range chips / activity-card counters
- 📋 **Pending** — see §2 below

**Sprint 9 sub-sprint coverage** (in flight on `claude/sprint9-insights-strategic`):

Three tracks landed, then a cross-cutting redesign pass after browser
review. Recommended sequence executed: Track A → B → C → 9.5.

- ✅ **Track A · Insights dashboard** (PM-facing analytics) — `ae7cf29`
  - A.0 fixture extension: `subcontractor_id` + closed 12-tag vocabulary on safety/quality records
  - A.1 `/insights` route + permission + provider + KPI strip
  - A.2 chart composites: `bar-stack.js` + `spark-line.js` + `trend-pill.js` (vanilla SVG, no CDN dep)
  - A.3 top-5 subcontractors panel + top-5 tags panel + 14-day trendline
  - A.4 drill-down filter (click sub or tag → filter rows) + right-detail profile
- ✅ **Track B · PM Team scope** — `499f550`
  - B.1 `P('user','manage',SCOPES.PROJECT)` on project_manager (shipped in Track A commit as prerequisite)
  - B.2 `getCallerManagedSites()` + `userOnSites()` filter in `/team`
  - B.3 PM-only `ReassignModal` right-detail action with site override map
- ✅ **Track C · Strategic dashboards** — `415798d`
  - C.0 spec lock — 3 separate pages (Q-S9-4 default)
  - C.1 `strategic-aggregator.js` — `(date × user)` fan-out grouped by site/region/org
  - C.2 `health-score.js` + `rollup-table.js` composites
  - C.3 `/portfolio` (CM, last 30d)
  - C.4 `/regional` (GM, last 90d)
  - C.5 `/executive` (Director, last 90d, org banner + region rollup)
- ✅ **A.5 + C.6 wrap-up** — `6615a84`
  - 5 new composites registered in `components-preview.html`
  - DemoTour `?demo=1` adds `/insights` step
- ✅ **9.5 · Dashboard redesign pass**
  - 9.5.1 layout swap (`a2504c4`) → all 4 dashboards full-width 2-panel (matches `/programme`); drill-down moves to RightDrawer
  - 9.5.2 font-size pass (`a2504c4`): `.fs-stat-card__value` 28→22, page titles 22→18, drawer names 18→15, executive banner 24→20
  - 9.5.3 three new chart composites (`bd8d4ed`) — `word-cloud.js` / `column-chart.js` / `heatmap-grid.js` (vanilla SVG/HTML, no CDN)
  - 9.5.4 first-pass 12-color tag palette (`bd8d4ed`) — explicit `color` field per `TAG_VOCAB` entry
  - 9.5.5 per-page redesigns (`4f33ace`): `/insights` 2×2 chart grid + WordCloud + HeatmapGrid; `/portfolio` `/regional` `/executive` add ColumnChart for health-grade distribution
  - 9.5.6 theme-aware chart palette + monochrome WordCloud (`e567d18`) — `--fs-chart-{tone}` + `--fs-tag-{slug}` tokens, deep light / soft dark
  - 9.5.7 categorical 12-hue palette (`9accf09`) — paired red+orange, FAILED user review (unreadable)
  - 9.5.8 SEMANTIC palette (this commit): SAFETY tags → red (danger-700/300), QUALITY tags → blue (info-700/300). Convention codified in CLAUDE.md.


Sprint 9 decision points (Q-S9-1 … Q-S9-7) are tracked in §4. Strong
defaults documented; locked at Track-A start.

**Sprint 10 sub-sprint coverage** (in flight on `claude/sprint10-prep`):

- ✅ **Candidate A · 3-panel → 2-panel migration** — `c91e06d`
  - `/today` `/activity` `/settings` `/evidence` each gain `layout: 'full-width'`; existing Right component moves to RightDrawer; cache busters bumped.
- ✅ **B.0 Stores + perm + scope model** — `921ff75`
  - `templates.fixture.js`: 3 org + 1 personal template; ADE schema shape
  - `template-store.js`: LocalStorage API (list / get / create / updateSchema / activate / delete / listVersions / restore / usageStats); async ADE simulation via `onExtracted` listeners
  - `roles.js` + `fs-globals.js`: `TEMPLATE` resource; `template:manage:self` (all) / `template:view:org` (all) / `template:manage:org` (gm+)
- ✅ **B.1 `/library` route + page scaffold** — `921ff75`
  - `library.js`: LibraryProvider, LibraryMiddle (tabs Org/Personal/All, template list, upload button), LibraryRight
  - `left-nav.js`: 'library' (book-open icon) in WORKSPACE trailingItems
- ✅ **B.2 Upload modal + fixture-stubbed schema** — `921ff75`
  - `template-upload-modal.js`: drag-drop, .pdf/.docx/.md/image, 50 MB cap, idle → uploading → extracting → done phases
- ✅ **B.3 Skip-edit primary path + render preview** — `921ff75`
  - Side-by-side "Your file" vs "Extracted schema" grid + Test render panel + "✓ Use this template" CTA
- ✅ **B.4 Simple editor (rename / reorder / delete)** — this commit
  - `SchemaEditor` in `library.js`; Preview / Edit / History sub-nav in LibraryRight; Edit tab: rename input, ↑↓ reorder, × delete; save → new version via `updateSchema`; exported as `window.FieldSight.SchemaEditor`
- ✅ **B.5 Version history + author attribution** — this commit
  - `VersionHistoryPanel` in `library.js`; History tab: newest-first list, click → diff vs current (= / − / +), "Restore as new version"; exported as `window.FieldSight.VersionHistoryPanel`
- ✅ **B.6 Output-format selector + DemoTour + wrap-up** — this commit
  - `TemplateFormatSelector` in `reports.js`: personal first (active ✓), then org; `template_id` included in regenerate payload
  - DemoTour step 7: `/library` → highlight `.fs-library__list`
  - `SchemaEditor` + `VersionHistoryPanel` registered in `components-preview.html` with fixture-data demos
  - composites.css v50; demo-tour.js v3; library.js v2; reports.js v5
- ✅ **Sprint 10 follow-up · /library polish** — `e4fc8df`
  - Test render: `max-height: 480px` + `overflow-y: auto`; "↗ Full preview" button → ModalOverlay full-screen view
  - Tab order changed to All / Organisation / Personal (label only; scope key `'org'` preserved for backwards-compat); default tab `'all'`
  - Favourites pin shelf above template list (Heidi pattern): per-user `localStorage['fs.lib.favourites']`, max 6, ☆/★ button per row, × unpin on tile, empty-state hint
  - SchemaEditor drag-and-drop + 1-level nesting: ⋮⋮ handle, three drop zones (before / into / after), accent-line indicators, delete-promotes-children safety; replaces previous ↑↓ button reorder
  - Heidi-style document view + chatbot recorded as Sprint 11+ candidate in §6 backlog (deterministic schema-driven render kept as canonical path)

**Sprint 11 sub-sprint coverage** (in flight on `claude/sprint11`):

- ✅ **Track A · A11y hardening** — `4634761`
  - A.1 axe-core via CDN gated on `?axe=1`; runs on every hashchange; logs WCAG 2.1 AA violations to console (Q-S11-4: one-off, not permanent)
  - A.2 contrast fixes — `--text-disabled` + `--text-placeholder` bumped both themes (light: 2.85→4.83:1, dark: 4.05→8.5:1); `fs-globals.js` JS mirror synced
  - A.3 new `ACCESSIBILITY.md` — NVDA/VoiceOver checklists for `/today` (6 steps) + `/programme` (4 steps) + common pitfalls + WCAG criterion legend
- ✅ **Track B · XLSX column-mapper partial mapping** — `e47a061`
  - B.1 partial-mapping per Q-S11-2 default: removed disabled gate; warning becomes informational note; button label shows skip count
  - B.2 mapping flow already wired in 8.2.2; this commit just unblocks the UX
- ✅ **Track C · Tasks cross-day audit (Q-1)** — `5d5562e`
  - C.1 `tasks.getCrossDayAudit({from,to,user})` flat-array variant of `actions.getActionsRange`
  - C.2 `WeeklyCompletionKpi` on `/today` (Mon→today per Q-S11-1 default; SparkLine; hidden on empty)
  - C.3 `ActionHistoryPanel` on `/tasks` right-detail — every check event for the same `topic_action_key` over 90 days; Q-S11-3 role-aware visibility (admin sees all, users see own)
  - C.4 mock API spec inlined as docblock on `getCrossDayAudit` for Sprint 12 backend handoff

---

## 2 · Pending / deferred

Items that were touched but consciously left for later, plus
not-yet-started carry-overs.

### Deferred until further notice

| Item | Reason deferred |
|---|---|
| **8.4.3 — Mobile breakpoint deep audit at 375 / 414 px** | Superseded by user's plan to build a **purpose-built mobile app** focused only on today's to-do-list. Current 767 px breakpoint makes all 12 pages technically usable; finer per-page tuning is wasted polish if the canonical phone surface will be a different codebase. Re-open if mobile-app idea is dropped. |
| **8.5.1 — WCAG colour-contrast audit** | Needs an axe-core run (or manual contrast calc) on every text/surface pair introduced in Sprint 7's dark-mode work. Code can't self-verify. |
| **8.5.6 — Screen-reader smoke test** | Manual NVDA / VoiceOver pass on `/today` and `/programme`. Requires a real reader runtime. |
| Excel `.xlsx` parsing edge cases (column-mapper UI) | XLSX import shipped (8.2.2); column-mapper fallback for non-standard headers not yet built. |
| Reverse linking: action-done → programme-progress nudge | Field-test 4.10 first — UX not yet validated. |
| MS Project `.mpp` binary import | No pure-JS parser exists; either backend conversion service or accept `.xml` only. |
| Resource-pool conflict detection (beyond per-user over-allocation) | Domain-rule heavy; revisit after over-allocation banner gets real-world feedback. |
| **BI tool embed** (Looker / Metabase / Power BI on `/insights`) | Sprint 9 Track A ships native vanilla-SVG charts; embedded BI is overkill for the ~15-cell rollup the PM dashboard renders. Reconsider when cross-month / cross-portfolio "regression of weather-vs-safety"–style asks land. |
| **Q-2 vocabulary fold-in for Sprint 9 tag system** | Sprint 9 ships a hard-coded 12-tag vocab. When Q-2 admin-editable vocab system materialises, Insights can swap to a fetched list. Two-sprint stretch; not in Sprint 9. |
| **Backend per-site timeline endpoint** (`GET /api/timeline?site_id=`) | Sprint 9 Track C aggregator uses `(date × user)` cross-product then groups by `r.site` (option C.1.a). Migrate to per-site fetch when backend exposes; one-aggregator swap, no page rewrite. |
| **Subcontractor management surface** (CRUD UI for the new subcontractor directory) | Out of Sprint 9 scope; would gate behind a new `subcontractor_admin:manage` permission. |
| **Library / Template — backend ADE integration (L-3)** | LandingAI ADE chosen at Sprint 10 kickoff (single-API extraction with chunk → section mapping; org-level account; ~50 calls/month cap). Backend-only work: receive multipart upload → proxy to ADE → store original in S3 + parsed schema in DynamoDB → expose to frontend via the API surface defined in §6 candidate B. UI ships in Sprint 10 against fixture-stubbed schemas that mirror ADE shape, so this swap-in is a backend-only change. |

---

## 3 · Issues encountered & guardrails

Recurring traps caught in Sprint 0–8. Each one was a real bug that
shipped and got fixed; capturing them here so they don't get
re-introduced. **CLAUDE.md mirrors this section** — treat the two as
synchronised.

### Date math

- **BUG-19 NZDT**: never `new Date('YYYY-MM-DD')` (parsed as UTC,
  drifts a day in NZ). Always go through `FS.api.todayNZDT()` /
  `FS.api.addDaysISO()` / `FS.api.folderName()`.

### Network

- **BUG-20 CloudFront SPA fallback**: a 200 with `text/html` body is
  the SPA shell, not your JSON. `_fetch.js:isJsonResponse()` guards
  this; never bypass it.
- **BUG-21 audio paused-ref**: don't read `audioRef.current.paused`
  for play state — track it in React state.

### Theming

- **JS-mirrored hex tokens bypass `[data-theme]`**: `t.surface.X` /
  `t.border.X` / `t.text.X` from `fs-globals.js` are baked
  light-mode hex. Inlining them via React `style={{ background:
  t.surface.panel }}` defeats dark mode entirely. Use string
  literals: `style={{ background: 'var(--surface-panel)' }}`.
- **NavIcon SVG `var()` resolution**: `svg.setAttribute('stroke',
  'var(--text-disabled)')` does **not** resolve the CSS var (SVG
  presentation attrs aren't styled). Use `svg.style.stroke = color`
  instead. Already fixed in `left-nav.js`.
- **Status colour tokens are intentionally not theme-flipped**
  (`--color-success-100` / `--color-info-50` etc. are brand-semantic).
  In dark mode their light-pastel backgrounds + global white text =
  unreadable. Two patterns to use:
  - **Light pastel bg** (Gantt bar, kanban card): pin foreground to
    `var(--color-neutral-900)` via `[data-theme="dark"]` override.
    See `composites.css §PROG-DARK`.
  - **Light pastel chip / badge / counter on a dark panel** (badge
    subtle, range chip --active, activity-card count): swap the bg
    to a translucent `rgba(...)` tint and bump text to
    `var(--color-{tone}-200/300)`. See `composites.css §DARK-BADGES`.
- **SAFETY = red, QUALITY = blue is canonical** across the app.
  All safety-domain chart fills + tag colours pull from
  `--color-danger-700/300` (via `--fs-tag-{safety-slug}` and
  `--fs-chart-danger`). All quality-domain ones pull from
  `--color-info-700/300`. **Never pair red with deep-orange in the
  same chart** — Sprint 9.5.7 tried 12 categorical hues and
  failed user review (unreadable at narrow widths). "Other"
  categories — subcontractors, projects, regions, programme tasks
  — are free to vary their palette since they aren't tied to
  safety/quality semantics. Mirrored to CLAUDE.md.

### Selection / focus

- **`:focus` paints on mouse click** in some browsers — produces a
  "double-border" effect when stacked with a `--selected` border.
  Use `:focus-visible` (keyboard-only) for inset outlines.
- **`.fs-card--clickable:focus-visible` halo + `--selected` border**
  also stacked. Suppressed with `.fs-card--clickable.fs-card--selected:focus-visible
  { box-shadow: none }` in `components.css`.
- **Unified selection token**: `--surface-selected` (theme-aware) is
  the canonical "selected row bg". All pages with selectable rows
  use it. Don't reach for raw `--color-accent-50` again — it reads
  as salmon on dark surfaces.

### Persistence / mocks

- **Mock-only-mutation lesson** (Sprint 5): don't ship UI write
  actions before the matching backend endpoint exists. The mock
  appears to work; reality bites at integration. Sprint 8 addressed
  this for actions/safety/quality/programme by gating writes on
  `useMocks` and shipping real PATCH/POST/DELETE shapes alongside.

### Token / cache hygiene

- **Token sync**: `tokens.css` (CSS custom props) and
  `fs-globals.js` (JS mirror) are mirrored manually. When you edit
  one, edit the other — and grep both before claiming "token-only".
- **Cache busters**: bump `?v=N` query strings in preview HTMLs
  whenever a loaded `.js` / `.css` file changes. `file://` and dev
  servers won't pick up changes otherwise.

### Mobile-only floating UI clusters

- **React.Fragment of `position: fixed` siblings leaks into desktop
  layout** (Sprint 8 follow-up 2). `BottomNav` was a Fragment of
  backdrop + sheet + nav. The `<nav>` was hidden via `display: none`,
  but the sheet's `transform: translateY(100%)` + `overflow-y: auto`
  rendered visibly under specific viewport / parent-container
  conditions. **Wrap any mobile-only floating cluster in a single
  portal `<div>`** with `display: none` on desktop and
  `display: contents` in the mobile media query. One toggle, no gaps.

### Admin permission flow

- **`getTimeline(date, user=null)` for admin returns the
  `available_users` disambiguation envelope, NOT data.** Aggregator
  pages that fanned out per-date with `user=null` got 0 rows because
  the loop skipped `available_users` responses. Sprint 8 follow-up 2
  added explicit admin fan-out: when `caller.isAdmin / role==='admin'
  / role==='gm'` AND no explicit user, build a `(date × all-users)`
  cross-product from `fixtures.sites.users`. Pattern lives in
  `compliance-aggregator.fanoutDates`, `tasks-aggregator
  .getActionsResolvedRange`, and `evidence.js`'s photos load — copy
  it for any new aggregator page.
- **Modal `siteId` defaults to `''` for admin** since admin has no
  primary site. Always fall back to the first site from
  `fixtures.sites.sites[0].site_id` so the modal mounts with a real
  context.

### Showcase

- **`components-preview.html` lag**: every new L5 composite must be
  registered there with at least a smoke render. Sprint 8 caught
  this — 9 composites had shipped without registration. Add a
  `Section` block when introducing a composite, even if it's a
  trigger-button stub for interactive ones (modals, palettes).

### Animation

- **Reduced motion is non-negotiable**: every `@keyframes` needs a
  `@media (prefers-reduced-motion: reduce)` override. Skeleton
  shimmer, topic-card flash, task check-off — all gated. Field
  workers with vestibular disorders are a real audience.

---

## 4 · Open product questions

Surfaced during second-pass reviews; not bugs, but yes/no decisions
needed before any sprint commits to them. Each is roughly one sprint
of UI + a matching backend change.

- **Q-1 — Tasks page / cross-day audit aggregation.** `/api/actions`
  is keyed by date and writes an immutable audit log. UI today only
  surfaces per-action `Checked by …` captions inside one report.
  Surfaces likely needed: weekly-completion KPI on Today, per-action
  history drawer. Backend: `GET /api/actions/all?from=&to=&user=`
  aggregator, or fan out N `getActions(date)` calls.
- **Q-2 — Editable reports + vocabulary system.** Reports are
  read-only per BACKEND-CONTEXT §10. Two related needs: manual
  correction of AI mistakes (PATCH + diff viewer + inline edit), and
  custom vocabulary for project-specific terms ("SB1108", "MPI") so
  transcripts get the spellings right.
- **Q-3 — Photo lifecycle (delete + UI upload).** §10 explicitly
  blocks both. Delete needs permission gate + soft-delete + audit;
  upload needs multipart/chunked path (videos can be 200–300 MB) and
  changes the device-only data-flow assumption.
- **Q-4 — Global / cross-day Ask.** `/api/ask` is scoped to one
  (date, user). Reviewer asked about cross-day questions. Options:
  new `POST /api/ask/global`, or frontend fan-out + aggregate. UI
  surface: a top-bar global search input or a new `/search` chat
  scope picker. (Sprint 8.6 added a search palette for entity
  lookup; cross-day Ask is the bigger AI variant.)

### Sprint 9 decision points (lock at Track-A start)

These are "if I get them wrong, I rewrite half the sprint" forks.
Strong defaults are documented; user can override before A.0 lands.

| # | Question | Default |
|---|---|---|
| **Q-S9-1** | Tag taxonomy — closed 12-list now, or pull from Q-2 vocab system later? | Closed 12-list now; Q-2 fold-in is Sprint 10+ |
| **Q-S9-2** | Subcontractor source — UI-only fixture extension, or wait for backend schema? | UI-only fixture extension; document spec in BACKEND-CONTEXT for later sync |
| **Q-S9-3** | PM `/team` scope — `managed_sites[]` intersection, or new `project_id` link? | `managed_sites[]` on PM user (simplest path, no new entity) |
| **Q-S9-4** | Strategic dashboards — 3 separate pages, or 1 page + role-aware scope? | 3 separate pages (matches existing nav slots; clearer audit trail) |
| **Q-S9-5** | Chart approach — vanilla SVG, or Chart.js UMD? | Vanilla SVG (no build step, theme-token native, ~30 LoC per chart shape) |
| **Q-S9-6** | BI embed (Looker / Metabase) on `/insights` | Out of Sprint 9; document the future hook |
| **Q-S9-7** | `/insights` permission — new `insights:view`, or reuse `report:view`? | New `insights:view` (decouples audience from report consumers) |

---

## 5 · Design alternatives held for revisit

Recorded so we don't re-derive them from scratch if the chosen
direction proves wrong.

### Activity page (`/activity`) — direction not chosen Sprint 4.6

The Sprint 4.6 redesign settled on **direction C — user activity
stream** (group events by user, "what each person did this week").
The two alternatives below were rejected for now, but kept on file:

- **A — Kill `/activity` entirely.** Coverable by `/timeline` (single
  day, structured) + `/tasks` (cross-day action tracker). Recall if
  user-activity-stream proves redundant in usage testing.
- **B — Repositioned as "raw on-site stream".** Show *unstructured*
  field signals before AI processing: PTT audio chunks just
  uploaded, photos mid-classification, voice notes, safety flags
  raised in real time. Conceptually stronger but needs a backend
  endpoint we don't have yet (BACKEND-CONTEXT §10 — device→S3 path
  is one-way, no `/api/feed/raw`).

---

## 6 · Next phase candidates

Sprint 9 merged via PR #19 (Insights + PM Team scope + Strategic
dashboards + 9.5 redesign pass). Sprint 10 candidates below — primary
contenders at the top, longer-tail items below.

### Primary candidates (front-of-queue for Sprint 10)

#### A · 3-panel → 2-panel migration (4 pages)

Sprint 9.5 proved the full-width 2-panel pattern (matches `/programme`)
works well for canvas-heavy pages. The same migration is high-ROI on
4 more pages where the static right-detail rail is wasted space:

| Page | Why 2-panel wins |
|---|---|
| **`/today`** | Today is a feed (brief → urgent → tasks → on-site), not list-detail. Right pane shows placeholder most of the time. |
| **`/activity`** | User activity stream is read-mostly; per-user drill is a secondary path → RightDrawer fits better. |
| **`/settings`** | Form, not list-detail. Current right "summary" panel just echoes selected values. |
| **`/evidence`** | Photo grid lives in middle; lightbox already covers drill-down. 70%-wide grid renders much better than the cramped 4-col right-pane variant. |

**Why keep 3-panel** on `/timeline /tasks /safety /quality /reports
/sites /team` — all have frequent list↔detail switching where keeping
the list visible is the value-prop.

**Size:** Half-day. Same Sprint 9.5 commit pattern: add `layout:
'full-width'` to each page registry, keep existing `Right` component
(it'll render inside RightDrawer), tighten font sizes to match. ~20
lines × 4 pages.

#### B · Library / Template — UI prototype

Per-company report templates. Each construction firm has its own daily
report / weekly progress / incident report format. They upload their
template; future generated reports fit their format. Heidi-Health-style
clinical-note pattern, applied to construction reporting.

**Architecture (4 layers):**

1. **L-1 Source upload** — drag-drop `.docx` / `.md` / `.pdf` /
   scanned image, original file stored S3-side.
2. **L-2 Parsed schema** — `{ sections: [{ title, kind, fields,
   prompt_hint }] }` where `kind ∈ { narrative, list, table, kpi,
   photos }`. Schema shape mirrors LandingAI ADE chunk output so
   the L-3 swap-in is a backend-only change.
3. **L-3 Mapping + extraction** — file → schema via **LandingAI ADE**
   API (chosen Sprint 10 kickoff). Backend-only integration; key
   never leaves server. *Sprint 10 ships against fixture-stubbed
   schemas; real ADE wiring tracked in §2 as a backend follow-up.*
4. **L-4 Rendered output** — render canonical DailyReport into the
   chosen template (preserve logo, footer, page layout). PDF/DOCX
   export.

**Two-tier library scope** (decided Sprint 10 kickoff):

- **Org library** — admin-uploaded templates, visible to every user
  in the org. Org pays the ADE cost. Has an org-wide "active default"
  per report-type.
- **Personal library** — user-uploaded templates, visible only to
  the uploader. Personal active default overrides org default for
  that user. Lets a PM keep their own preferred summary format
  without polluting the org-wide list.

**Skip-edit UX (cognitive-load mitigation — users may NOT want to
edit schema):**

The flow is designed so that 90% of users never touch the editor.
Three-tier fallback:
1. **Primary path** — after upload + ADE parse, show a side-by-side
   "your file" vs "what we extracted" preview, then a **Test render**
   panel that fills the schema with the latest daily-report data.
   If it reads right, single button "✓ Use this template" saves
   directly. Done.
2. **Simple editor** (secondary path) — only three actions: rename
   section, reorder, delete. No split / merge / nested hierarchy
   editing in v1 (those are the actions that confuse non-technical
   users most).
3. **Start blank fallback** — when ADE fails or returns empty, "Start
   blank" creates an empty template with 4 generic sections (Summary
   / Decisions / Actions / Safety) the user can rename. Lets the
   feature work even when AI extraction breaks.

### Sprint 10 sub-sprint plan (Candidate B = 7 sub-sprints)

| # | Sub-sprint | What ships | Size |
|---|---|---|---|
| **B.0** | Stores + perm + scope model | LocalStorage layers for `org_library` + `personal_library`; `template:manage:org` for admin/gm/director; everyone gets `template:manage:self` for personal lib. Template fixture shape: `{ id, scope: 'org'|'personal', report_type, active, versions[] }`. Mock 3 org templates + 1 personal per role. | 0.5 day |
| **B.1** | `/library` route + page scaffold | Tabs (Org / Personal / All — All is read-only union). 3-panel layout (sidebar nav | template list | template detail). "Active default" badge per report-type. Permission-gated upload button. | 1 day |
| **B.2** | Upload modal + fixture-stubbed schema | Drag-drop modal with file-type + size validation. Async progress UX (toast + library row marked "Extracting…" during the simulated ADE wait). Stubbed schema response matches ADE shape so L-3 lands without UI rework. | 1 day |
| **B.3** | Skip-edit primary path + render preview | Side-by-side "source vs extracted schema" view + Test-render panel that fills mock schema with latest DailyReport data. "✓ Use this template" CTA saves + activates in one click. | 1 day |
| **B.4** ✅ | Simple editor (rename / reorder / delete) | `SchemaEditor` in `library.js` — Preview / Edit / History sub-nav in LibraryRight; Edit tab: rename (text input), reorder (↑↓), delete (×) per section; save creates new version via `updateSchema`; change note input; cancel returns to Preview. Exported as `window.FieldSight.SchemaEditor`. | 0.5 day |
| **B.5** ✅ | Version history + author attribution | `VersionHistoryPanel` in `library.js` — History tab in LibraryRight; versions displayed newest-first; click to expand diff vs. current schema (= same, − removed, + added later); "Restore as new version" via `restore(id, vid)`; read-only for non-managers. Exported as `window.FieldSight.VersionHistoryPanel`. | 1 day |
| **B.6** ✅ | Output-format selector in `/reports` + DemoTour + components-preview wrap-up | `TemplateFormatSelector` in `reports.js` — personal templates first (active flagged ✓), then org; default = active template; selected `template_id` included in regenerate payload. DemoTour step 7: `/library` highlighting `.fs-library__list`. `SchemaEditor` + `VersionHistoryPanel` registered in `components-preview.html` with fixture data demos. | 0.5 day |

**Total UI work**: ~5.5 days. Half a sprint, since most of B.* is
declarative rendering + localStorage state.

### Frontend API surface (mock-only in Sprint 10; backend lands later)

Sprint 10 stubs all of these against localStorage. Backend wiring
is the Sprint 11+ follow-up tracked in §2.

```
GET    /api/templates?scope=org|personal|all      list, optionally filtered
POST   /api/templates                              upload (multipart) → schema parse
GET    /api/templates/{id}                        full template + active version
PATCH  /api/templates/{id}/schema                 edit (creates new version)
POST   /api/templates/{id}/activate               set as default for {scope, report_type}
DELETE /api/templates/{id}                        soft-delete; versions preserved
GET    /api/templates/{id}/versions               list versions (id, author, date)
GET    /api/templates/{id}/versions/{vid}         specific version snapshot
POST   /api/templates/{id}/versions/{vid}/restore restore (creates new version from old)
GET    /api/templates/usage?from=&to=             ADE call count for cost cap UI
```

**Sprint 10 decision points (all locked at kickoff):**

| # | Question | Locked default |
|---|---|---|
| Q-S10-1 | Per-report-type templates? | Yes — `template.report_type ∈ {daily, weekly, monthly, incident}` |
| Q-S10-2 | Version retention? | Every edit = new version, immutable history, for reproducibility |
| Q-S10-3 | Cross-org sharing? | No for v1 (IP + privacy) |
| Q-S10-4 | Inline AI instructions in template? | No for v1; v2 feature |
| Q-S10-5 | ADE API key + cost ownership? | Org-level account; FieldSight-managed key; cost billed to org |
| Q-S10-6 | Cost cap | 50 ADE calls per org per month (one-off-style use, not steady-state); over-cap shows warning, not auto-charge |
| Q-S10-7 | Sync vs async UI? | Async + toast notifications; user can leave page during extraction |
| Q-S10-8 | ADE failure / extraction empty? | Three-tier mitigation (skip-edit primary path, simple editor secondary, "Start blank" fallback). Users may not edit schema; UX must tolerate that. |
| Q-S10-9 | File types? | Full ADE support: `.pdf`, `.docx`, `.md`, scanned images |
| Q-S10-10 | Re-run ADE on edit? | No — once user confirms / edits, schema is truth. Re-upload file = new template version. |
| Q-S10-11 | Personal lib vs org lib precedence? | Both visible side-by-side in `/library`. Personal active default overrides org default for that user; otherwise org default wins. Tracking who uploaded each version is mandatory (`created_by_user_id`). |

### Backlog (post-primary)

| Candidate | One-liner | Size |
|---|---|---|
| **Mobile app — today's to-do-list focus** | Purpose-built phone surface, narrower scope than the web shell. Replaces 8.4.3 mobile audit. | Cross-codebase; new repo or sibling tree |
| **Sprint 8 a11y finishing** | Run axe-core (8.5.1) + manual SR pass (8.5.6); fix anything that fails. | Small-medium; one-off |
| **Backend wiring for Sprint 9 schema** | Mirror the UI-side `subcontractor_id` + `tags[]` fields (Q-S9-2 default) into the real backend; expose `/api/insights/safety` + `/api/insights/quality` rollup endpoints. | Sprint-sized backend work |
| **Q-1 Tasks audit aggregation** | Cross-day action tracking + per-action history drawer + completion KPI. | Sprint-sized + backend |
| **Q-2 Editable reports + vocab** | PATCH `/api/reports`, edit-in-place UI, vocab admin surface. Once landed, Sprint 9's hard-coded 12-tag list folds into Q-2. | Two sprints + backend |
| **Q-3 Photo lifecycle** | Delete + upload, with audit + permission gates. | One sprint + backend |
| **Q-4 Global Ask** | Cross-day AI query surface; could pair with the Sprint 8.6 search palette. | One sprint + backend |
| **BI embed for `/insights`** | Looker / Metabase iframe with Cognito SSO, once cross-month / cross-portfolio analytics needs justify the cost. Hook stays in `/insights` provider state. | One sprint + auth federation |
| **Library / Template — Heidi-style document view + chatbot** | Add a "Document view" tab to `SchemaEditor` that renders the schema as a markdown-style document with `[placeholder]` markers + parenthetical instructions (Heidi pattern). Saves write back to schema, not free-form prompt. Optional chatbot ("What would you like to change?") can rewrite via a small LLM call — costly per-render so keep gate-able. Considered + deferred at Sprint 10 follow-up: schema-driven render kept as the canonical path because (a) deterministic, (b) cheap, (c) audit-friendly for compliance reports. Document view bridges UX gap for non-technical users without committing to LLM-per-render. | One sprint + small LLM cost (chatbot only) |
