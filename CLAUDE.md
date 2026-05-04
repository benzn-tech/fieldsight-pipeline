# FieldSight UI — Claude Memory

## What this is

FieldSight is a field-management app for construction sites (Procore/Aconex
lineage, NZ context). This repo is the **UI prototype**: pure HTML + CSS +
browser-side React (Babel transpiled in-browser, no build step). Open the
preview HTMLs directly or via `python3 -m http.server`.

The prototype's job is to validate visual language, component shape, and
interaction patterns before any backend wiring.

## Architecture · Layer Model

The codebase is organised into 7 layers, lower layers know nothing about
higher ones:

| Layer | Name | Lives in | Status |
|---|---|---|---|
| **L1** | Design tokens | `styles/tokens.css` (CSS custom properties) + `fs-globals.js` (JS mirror) | ✅ Sprint 0 |
| **L2** | Visual language | Color palette, typography, spacing decisions — embodied in L1 | ✅ Sprint 0 |
| **L3** | App shell | `scripts/app-shell.js` + `styles/app-shell.css` (3-pane layout, drag divider, role-based nav) | ✅ Sprint 1 |
| **L4** | Base components | `scripts/components/` + `styles/components.css` — Button, Input, Card, Badge, Avatar | ✅ Sprint 1 |
| **L5** | Composite components | `scripts/composites/` — TaskCard, StatCard, Timeline, MorningBriefCard, etc. | 🟡 Sprint 2 |
| **L6** | Pages | `scripts/pages/` — Today registered; Tasks/Safety/Sites/etc. coming | 🟡 Sprint 1 partial / Sprint 3+ |
| **L7** | Interactions | Inline within components/pages — task check-off animation, micro-interactions | 🟡 Sprint 2 + Sprint 5 |

## File Structure

```
.
├── CLAUDE.md                           ← this file
├── PLAN.md                             single-source action ledger (completed/pending/traps/questions)
├── README.md                           (placeholder)
├── tokens-reference.html               L1 token doc with live demos
├── components-preview.html             L4 + L5 component showcase
├── app-shell-preview.html              L3 + L6 full-app preview (also `?dev=1`, `?demo=1`, `?mocks=0`)
├── styles/
│   ├── tokens.css                      L1 — CSS custom properties (single source of truth)
│   ├── components.css                  L4 — `.fs-{name}` BEM
│   └── app-shell.css                   L3 — shell + utility + popover + bottom-nav + print
└── scripts/
    ├── fs-globals.js                   L1 mirror to JS — tokens + roles + nav + canSeeNav
    ├── theme.js                        Sprint 7 — Light / Dark / Auto persistence
    ├── density.js                      Sprint 7.6 — Comfortable / Compact persistence
    ├── router.js                       hash routing + Sprint 8.4.4 swipe-back
    ├── auth-mock.js                    mock current-user
    ├── auth/                           Sprint 8.0 — Cognito + session
    ├── roles.js                        7 hierarchy + 3 specialist roles, perms, canDo
    ├── api/                            backend-shaped data layer (Sprint 2 onwards)
    ├── mock/                           fixtures: sites · daily-report · dates · programme · media · …
    ├── drag-divider.js                 middle-column resize
    ├── left-nav.js                     L3 — sidebar with sections/subgroups
    ├── app-shell.js                    L3 — shell, MiddleColumn, RightDetail, BottomNav, Weather, offline banner
    ├── dev-role-switcher.js            dev-only role switcher (?dev=1) + MOCK/LIVE badge
    ├── components/                     L4 — button, input, card, badge, avatar
    ├── composites/                     L5 — task-card, urgent-card, kpi-strip, topic-card, gantt-row,
    │                                       safety-flag-row, action-item-row, modal-overlay, right-drawer,
    │                                       date-picker, photo-grid, evidence-tabs, programme-task-editor,
    │                                       programme-import-modal, programme-kanban-board, demo-tour,
    │                                       error-banner, over-allocation-banner, tooltip, toast,
    │                                       safety-create-modal, quality-create-modal, search-palette,
    │                                       onboarding-overlay, …
    └── pages/
        ├── _page-registry.js           route → { Provider, Middle, Right }
        └── today / timeline / tasks / sites / programme / safety / quality / reports / evidence /
            activity / team / settings
```

## Conventions

- **BEM**: `.fs-{block}__{element}--{modifier}` (e.g. `.fs-card__header`,
  `.fs-task-row--mine`).
- **Tokens only**: never hardcode color/spacing/font; use CSS custom
  properties from `tokens.css`. JS code reads from `window.FS.tokens`.
- **Token sync**: `tokens.css` and `fs-globals.js` are mirrored manually.
  When you edit one, edit the other.
- **Component export**: each component file IIFEs and attaches to
  `window.FieldSight.{Name}` (e.g. `window.FieldSight.Card`).
- **Pages register**: `window.FieldSight.PAGES['/route'] = { Middle, Right }`.
  AppShell looks up via `window.FieldSight.getPageForRoute(route)`.
- **Babel in-browser**: `<script type="text/babel">` is fine; JSX optional.
  Most files use `React.createElement` directly to avoid Babel parse cost.
- **Reduced motion**: respected globally via `@media (prefers-reduced-motion:
  reduce)` in `tokens.css` (~line 627). Any new animation must check too.
- **Cache busters**: bump `?v=N` query strings in preview HTMLs when shipping
  changes, so `file://` and dev servers pick up the new version.
- **No build step**: don't introduce npm/webpack/vite. The whole point of the
  prototype is to stay editable in any text editor.

## Commands

```bash
# Local preview (any of the 3)
python3 -m http.server 8765
# then open http://localhost:8765/app-shell-preview.html

# Syntax-check JS
node --check scripts/path/to/file.js

# All-in-one syntax check
for f in scripts/**/*.js; do node --check "$f"; done
```

No tests, no linter, no formatter configured. JS is plain ES2017+ (browsers
supported are evergreen).

## Design System Quick Reference

- **Primary navy** `#102A43` (Procore/Aconex lineage), **safety orange**
  `#FF6B35` accent (hi-vis construction norm).
- **Status colors split intentionally**: `blocked = magenta` (functional
  "halt") vs `overdue = red` (temporal urgency) — never reuse one for the
  other.
- **Touch targets**: 44 / 48 / 56 px (field default 48 — gloved-hand safe).
- **Typography**: Inter (sans), JetBrains Mono (code/technical IDs).
  `.type-stat` has `font-variant-numeric: tabular-nums` for KPI alignment.
- **Dark mode**: blue-tinted near-black surfaces; defined in `tokens.css`
  under `[data-theme="dark"]`. Sprint 6 polishes.

## Sprint Roadmap

| Sprint | Theme | Status |
|---|---|---|
| **0** | L1 tokens + L2 visual language + `tokens-reference.html` | ✅ done |
| **1** | L4 atoms + L3 AppShell + Today lo-fi (1.5–1.6 hotfixes) | ✅ done |
| **2** | Backend-shaped data layer (Phase A–I); Today derived from real `DailyReport`; Ask agent | ✅ done |
| **3** | Polish backlog after Phase-I review (P-01 … P-12) | ✅ done |
| **4** | Core operational pages — Sites, Programme MVP, Tasks aggregator, Reports, Evidence, Activity, Weather UI | ✅ done |
| **5** | Programme operability — drag/edit, kanban, CSV/MS-Project XML import, role gates | ✅ done (PR #15) |
| **6** | Compliance pair — `/safety` + `/quality` + deep-link spotlight + photo carousel | ✅ done (PR #16) |
| **7** | `/team` + `/settings` + dark-mode polish (theme + density + default-landing prefs) | ✅ done (PR #17) |
| **8** | Backend integration foundation, write flows, programme deep features, mobile bottom-nav, a11y, search, error/offline, performance, fixture expansion, demo tour, print/share, onboarding | 🟡 on `claude/sprint8` |

Detailed completed/pending/next-phase tracking lives in **`PLAN.md`**.

## Current State

- **Active branch**: `claude/sprint8` (8.0 → 8.11 shipped + audit follow-up)
- **Open PRs**: none — Sprint 8 ready to PR when user calls it
- **Next**: see `PLAN.md` §6 Next phase candidates

## Known traps & guardrails

Mirrors `PLAN.md` §3. Each is a real bug that shipped and got fixed;
re-introducing one is the most common way to break the prototype.

### Date math

- **BUG-19 NZDT**: never `new Date('YYYY-MM-DD')` (parses as UTC,
  drifts a day in NZ). Use `FS.api.todayNZDT()` /
  `FS.api.addDaysISO()` / `FS.api.folderName()`.

### Network

- **BUG-20 CloudFront SPA fallback**: a 200 with `text/html` body is
  the SPA shell, not JSON. `_fetch.js:isJsonResponse()` guards it;
  never bypass.
- **BUG-21 audio paused-ref**: don't read `audioRef.current.paused`
  — track play state in React state.

### Theming

- **JS-mirrored hex tokens bypass `[data-theme]`**. `t.surface.X` /
  `t.border.X` / `t.text.X` from `fs-globals.js` are baked
  light-mode hex. In React `style={{ ... }}` use string literals:
  `style={{ background: 'var(--surface-panel)' }}` — never
  `t.surface.panel`.
- **NavIcon SVG `var()` resolution**: `svg.setAttribute('stroke',
  'var(...)')` does **not** resolve. Use `svg.style.stroke = color`.
- **Status colour tokens are not theme-flipped** (`--color-{success,
  info, warning, danger}-{50,100}`). On dark mode their light-pastel
  backgrounds with global white text are unreadable. Pin
  foreground via `[data-theme="dark"] .fs-X { color:
  var(--color-neutral-900) }`.

### Selection / focus

- **`:focus` paints on mouse click**; produces "double-border" with
  `--selected`. Use `:focus-visible` for inset outlines.
- **`.fs-card--clickable:focus-visible` halo + `--selected`** also
  stack. Suppress halo when also selected.
- **Unified selection token**: `--surface-selected` (theme-aware) is
  the canonical "selected row bg". Don't reach for
  `--color-accent-50` directly — it reads as salmon on dark.

### Persistence / mocks

- **Don't ship UI write actions before the matching backend exists**
  (Sprint 5 lesson). Mocks lie; integration bites. Sprint 8 gates
  writes on `useMocks` and ships real PATCH/POST/DELETE shapes.

### Token / cache hygiene

- **Token sync**: `tokens.css` and `fs-globals.js` are mirrored
  manually. Edit one → edit the other.
- **Cache busters**: bump `?v=N` in preview HTMLs whenever a loaded
  `.js` / `.css` changes.

### Showcase

- **`components-preview.html` lag**: every new L5 composite must be
  registered there with at least a smoke render or trigger button.
  Easy to forget; check before claiming a sprint complete.

### Animation

- **Reduced motion is non-negotiable**. Every `@keyframes` needs a
  `@media (prefers-reduced-motion: reduce)` override — field workers
  with vestibular disorders are a real audience.

## Working with this Project

- The user issues **specs in markdown** for each sub-sprint — patch-by-patch
  with grep-based pre-checks and a manual verification checklist. Follow
  that format when proposing new specs.
- **Ask before making architectural changes** (build tooling, framework,
  major restructure). The "no build step" constraint is intentional.
- **Don't auto-bump cache busters** unless changes touch the loaded file.
- When delivering, run `node --check` on every modified JS, `grep` the spec
  pre-checks, and confirm script load order in `app-shell-preview.html`.
- Real browser verification isn't always possible from this environment;
  state explicitly when it's done vs deferred to the user.
