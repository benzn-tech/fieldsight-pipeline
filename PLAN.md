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
| **8** | Backend integration foundation, write flows, programme deep features, mobile bottom-nav, a11y, search, error/offline, performance, fixture expansion, demo tour, print/share, onboarding | 🟡 | branch `claude/sprint8` |

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

No active "Sprint 9" plan committed — these are the threads most
likely to fund the next sprint. The user picks; this section is a
menu, not a decision.

| Candidate | One-liner | Size |
|---|---|---|
| **Mobile app — today's to-do-list focus** | Purpose-built phone surface, narrower scope than the web shell. Replaces 8.4.3 mobile audit. | Cross-codebase; new repo or sibling tree |
| **Sprint 8 a11y finishing** | Run axe-core (8.5.1) + manual SR pass (8.5.6); fix anything that fails. | Small-medium; one-off |
| **Q-1 Tasks audit aggregation** | Cross-day action tracking + per-action history drawer + completion KPI. | Sprint-sized + backend |
| **Q-2 Editable reports + vocab** | PATCH `/api/reports`, edit-in-place UI, vocab admin surface. | Two sprints + backend |
| **Q-3 Photo lifecycle** | Delete + upload, with audit + permission gates. | One sprint + backend |
| **Q-4 Global Ask** | Cross-day AI query surface; could pair with the Sprint 8.6 search palette. | One sprint + backend |
| **Sprint 8 PR + merge** | Open PR for `claude/sprint8` and merge into main. | Hours |
