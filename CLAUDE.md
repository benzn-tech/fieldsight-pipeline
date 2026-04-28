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
├── README.md                           (placeholder)
├── tokens-reference.html               L1 token doc with live demos
├── components-preview.html             L4 component showcase
├── app-shell-preview.html              L3 + L6 full-app preview
├── styles/
│   ├── tokens.css                      L1 — CSS custom properties (single source of truth)
│   ├── components.css                  L4 — `.fs-{name}` BEM
│   └── app-shell.css                   L3 — shell + utility + popover styling
└── scripts/
    ├── fs-globals.js                   L1 mirror to JS — tokens + roles + nav items + canSeeNav
    ├── router.js                       hash routing
    ├── auth-mock.js                    mock current-user
    ├── roles.js                        7 hierarchy + 3 specialist roles, perms, canDo
    ├── mock-data.js                    fixture data for Today (myTasks/teamTasks/urgent/activity/onSite + WEATHER)
    ├── drag-divider.js                 middle-column resize
    ├── left-nav.js                     L3 — sidebar with sections/subgroups
    ├── app-shell.js                    L3 — shell, MiddleColumn, RightDetail, WeatherIndicator+Popover
    ├── dev-role-switcher.js            dev-only role switcher (?dev=1)
    ├── components/                     L4 — button.js, input.js, card.js, badge.js, avatar.js
    └── pages/
        ├── _page-registry.js           route → { Middle, Right }
        └── today.js                    L6 Today page
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
| **0** | Foundation — L1 tokens + L2 visual language + tokens-reference.html | ✅ done |
| **1** | Core components — L4 atoms + L3 AppShell + Today lo-fi (1.5 hotfix → 1.5.5 nav restructure → 1.6 lo-fi → 1.6 hotfix) | ✅ done |
| **2** | Today page hi-fi — L5 composites (TaskCard / StatCard / Timeline) + L7 task check-off animation | 🟡 next |
| **3** | Secondary core pages — Tasks page, Safety page, supporting composites | ⏳ pending |
| **4** | Remaining core pages — Sites, Programme, Evidence, Reports + Weather integration UI | ⏳ pending |
| **5** | Flows + polish — core user flows, micro-interactions, empty/error/loading states | ⏳ pending |
| **6** | Mobile + dark mode — responsive design, dark-mode variants | ⏳ pending |

### Sprint 2 sub-sprints (planned)

- **2.0 — L5 component extraction**: pull TaskCard / StatCard / Timeline /
  UrgentCard / ActivityCard / MorningBriefCard / KpiStrip out of `today.js`
  into `scripts/composites/`; new `styles/composites.css`; show in
  `components-preview.html`.
- **2.1 — Today hi-fi composition**: KpiStrip at top, time-aware greeting,
  Brief truly collapses, polished spacing/hierarchy.
- **2.2 — Task check-off animation**: checkbox on TaskCard, border pulse +
  line-through + fade-out, respects `prefers-reduced-motion`.
- **2.3 — Wire deferred stubs + role variants**: Reassign popover, Mark
  complete triggers animation, Related item nav, worker-role hides
  `teamTasks` via `window.FS.canDo`.

## Current State

- **Branch**: `claude/review-project-PO2L5`
- **Open PR**: [#3](https://github.com/benzn-tech/fieldsight-ui/pull/3) —
  Sprint 1.6 hotfix (task grouping, weather popover, detail panel
  refinements). `mergeable_state: clean`, no CI configured.
- **Next**: Sprint 2.0 (L5 component extraction) when user gives the go.

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
