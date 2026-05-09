# FieldSight UI — Sprint 11 Planning

_Generated 2026-05-09 after Sprint 10 (`claude/sprint10-prep`) merged A + B.0–B.6._

---

## TL;DR

Sprint 10 is **fully complete** — 3-panel → 2-panel migration (Track A) and the
entire Library/Template UI prototype (B.0–B.6) landed in three commits on
`claude/sprint10-prep`. This document audits every unresolved item from `PLAN.md`
§2 (deferred), §4 (open questions), and §6 (next-phase backlog), then proposes a
Sprint 11 plan and a forward roadmap.

---

## 1 · Sprint 10 Completion Audit

| Sub-sprint | Status | Commit |
|---|---|---|
| **A** — 3-panel → 2-panel (`/today`, `/activity`, `/settings`, `/evidence`) | ✅ | `c91e06d` |
| **B.0** — Stores + perm + scope model (`template-store.js`, roles) | ✅ | `921ff75` |
| **B.1** — `/library` route + page scaffold + nav entry | ✅ | `921ff75` |
| **B.2** — Upload modal + fixture-stubbed ADE schema | ✅ | `921ff75` |
| **B.3** — Skip-edit primary path + Test render panel | ✅ | `921ff75` |
| **B.4** — `SchemaEditor` (rename / reorder / delete) | ✅ | `0c76b45` |
| **B.5** — `VersionHistoryPanel` + author attribution | ✅ | `0c76b45` |
| **B.6** — Output-format selector in `/reports` + DemoTour step + components-preview | ✅ | `0c76b45` |

All decision points Q-S10-1 through Q-S10-11 are locked. No open items
remain in Sprint 10.

---

## 2 · Pending Items Triage

Items are drawn from `PLAN.md` §2 (deferred), §4 (open product questions),
and §6 (backlog). Each is classified by its **blocker** to reveal what's
actionable now vs. later.

### 2.1 Frontend-unblocked (actionable in Sprint 11)

| # | Item | Origin | Effort |
|---|---|---|---|
| **F-1** | WCAG colour-contrast audit (8.5.1) — axe-core pass + fix CSS/aria issues | §2 deferred | Small |
| **F-2** | Screen-reader smoke-test guide (8.5.6) — NVDA/VoiceOver checklist for `/today` + `/programme` | §2 deferred | Small |
| **F-3** | Excel `.xlsx` column-mapper UI — fallback modal for non-standard XLSX headers in programme import | §2 deferred | Small |
| **F-4** | Q-1 Tasks cross-day audit — fan-out `getActions(date)` across a date range; weekly completion KPI on `/today`; per-action history drawer in `/tasks` | §4 Q-1 | Medium–Large |

### 2.2 Backend-coordination required (Sprint 11 B-side or Sprint 12)

| # | Item | Dependency | Sprint target |
|---|---|---|---|
| **B-1** | Library / Template ADE backend integration (L-3) | Backend: multipart upload → ADE proxy → S3 + DynamoDB → API | Sprint 12 (backend-only swap, no UI rewrite) |
| **B-2** | Backend per-site timeline endpoint (`GET /api/timeline?site_id=`) | Backend exposes endpoint; one aggregator swap, no page rewrite | Sprint 12 |
| **B-3** | Backend wiring — Sprint 9 subcontractor_id + tags[] | Backend: mirror UI fixture fields into real DB schema; expose `/api/insights` rollup endpoints | Sprint 12 |
| **B-4** | Q-2 Editable reports (phase 1) — PATCH `/api/reports` + inline-edit UI + diff viewer | Backend: PATCH endpoint + immutable version log | Sprint 12 |
| **B-5** | Q-2 Vocabulary admin surface (phase 2) — project-specific term registry; fold into Insights tag system | Depends on phase 1 landing | Sprint 13 |
| **B-6** | Subcontractor management CRUD | Backend: new `subcontractor_admin:manage` permission + CRUD endpoints | Sprint 13 |
| **B-7** | Q-3 Photo lifecycle — soft-delete + permission gate + UI upload (multipart/chunked for 200–300 MB video) | Backend: permission gate + soft-delete + upload path; device-only assumption changes | Sprint 13 |
| **B-8** | Q-4 Global / cross-day Ask | Backend: `POST /api/ask/global` or frontend fan-out + aggregate | Sprint 13 |
| **B-9** | BI embed (Looker / Metabase / Power BI on `/insights`) | Auth federation (Cognito SSO); justified only when cross-month/cross-portfolio analytics land | Sprint 14+ |

### 2.3 Permanently deferred (no action planned)

| Item | Reason |
|---|---|
| **8.4.3 Mobile breakpoint deep audit** | Superseded by planned purpose-built mobile app (new repo). Re-open only if mobile app is dropped. |
| **MS Project `.mpp` binary import** | No pure-JS parser exists; would require a backend conversion service. Accept `.xml` only for v1. |
| **Resource-pool conflict detection** | Domain-rule heavy; revisit after over-allocation banner gets real-world feedback. |
| **Reverse linking: action-done → programme-progress nudge** | Field-test programme UX first; UX not validated. |

---

## 3 · Sprint 11 Plan

**Theme: A11y hardening + Excel column-mapper + Tasks cross-day audit (Q-1)**

Three parallel tracks. Tracks A and B are small enough to run concurrently in
the first half; Track C is the main sprint body.

### Track A — A11y hardening (≈ 1 day)

Resolves F-1 and F-2. Pure CSS/aria work, no new components.

| # | Sub-sprint | What ships |
|---|---|---|
| **A.1** | axe-core audit pass | Load axe-core via CDN in `app-shell-preview.html?dev=1`; run against `/today`, `/programme`, `/insights`, `/library`. Capture all WCAG 2.1 AA violations. Document findings as inline comments in the audit checklist below. |
| **A.2** | Contrast fixes | Fix every contrast violation in `components.css` / `composites.css` / `tokens.css`. Likely candidates: placeholder text, disabled states, muted captions in dark mode. |
| **A.3** | SR smoke-test checklist | Ship a `ACCESSIBILITY.md` doc: manual NVDA/VoiceOver step-by-step for `/today` (6 checks) and `/programme` (4 checks). Includes expected announcements per interaction. Deferred to a human tester; doc makes the tester's job concrete. |

### Track B — Excel column-mapper UI (≈ 1 day)

Resolves F-3. Extends the existing `programme-import-modal.js`.

| # | Sub-sprint | What ships |
|---|---|---|
| **B.1** | Column-mapping modal | When XLSX headers don't match the expected schema (`task_name`, `start_date`, `end_date`, `assignee`, `status`), present a mapping step: dropdown per expected field → select which imported column maps to it. Unmatched fields silently skip. |
| **B.2** | Wire into import flow | `programme-import-modal.js` detects header mismatch → opens `ColumnMapperStep`. On confirm, remaps the parsed rows before the existing validation chain. |

### Track C — Tasks cross-day audit / Q-1 (≈ 3 days)

Resolves F-4. This is the sprint's main feature: surfacing cross-day action
completion data that already exists in the fixture layer but is never
aggregated across dates.

| # | Sub-sprint | What ships |
|---|---|---|
| **C.1** | `tasks-aggregator` cross-day extension | New `getActionsRange(from, to, user)` on `tasks-aggregator.js`: fans out `getActions(date)` calls across each day in the range; flattens + deduplicates (by `action_id + date`); admin/gm path does the `(date × all-users)` cross-product (same pattern as `compliance-aggregator.fanoutDates`). |
| **C.2** | Weekly completion KPI on `/today` | Extend `TodayProvider` with a `weekActions` load: call `getActionsRange(weekStart, today)`. Display a mini KPI tile in the Today middle column — "X / Y actions resolved this week" with a small sparkline (reuse `spark-line.js`). Hidden when `weekActions` is empty. |
| **C.3** | Per-action history drawer in `/tasks` | In `TaskCard` / action rows: clicking a completed action opens the `RightDrawer` with a "History" tab showing every date the action appeared + who resolved it + timestamp. Data from the fan-out result set, filtered by `action_id`. |
| **C.4** | Mock API surface | Define the mock shape for `GET /api/actions/all?from=&to=&user=` in `api/` (returns the same flat array `getActionsRange` produces). No real call yet; fixture-only. Documents the endpoint spec for backend handoff. |

### Sprint 11 decision points

| # | Question | Default |
|---|---|---|
| Q-S11-1 | Date range for weekly KPI on Today — rolling 7 days or calendar week Mon–today? | Calendar week Mon–today (matches `/timeline` week framing) |
| Q-S11-2 | Column-mapper: require all 5 fields to be mapped, or allow partial? | Allow partial; unmapped fields get `null` / default |
| Q-S11-3 | Tasks history drawer: show only this user's resolutions, or all users' (admin view)? | Role-aware: admin/gm sees all; site user sees own only |
| Q-S11-4 | axe-core: ship as a permanent dev tool in `?dev=1` mode, or one-off audit? | One-off audit; don't add permanent CDN dep to dev mode |

### Sprint 11 estimated timeline

```
Day 1    Track A.1 + A.2 (axe-core audit + contrast fixes)
Day 2    Track B.1 + B.2 (column-mapper UI) + Track A.3 (SR checklist doc)
Day 3    Track C.1 (tasks-aggregator cross-day extension)
Day 4    Track C.2 + C.4 (weekly KPI on Today + mock API shape)
Day 5    Track C.3 (per-action history drawer) + wrap-up (cache busters, components-preview)
```

---

## 4 · Forward Roadmap (Sprint 12–14+)

### Sprint 12 — Backend integration wave 1

_Assumes backend team is ready to expose new endpoints._

| Track | Theme | Items |
|---|---|---|
| **12-A** | Library ADE backend swap-in | B-1: Replace `template-store.js` localStorage with real `/api/templates` calls; no UI changes needed |
| **12-B** | Sprint 9 backend wiring | B-3: `subcontractor_id` + `tags[]` into real DB; `/api/insights/safety` + `/api/insights/quality` rollup |
| **12-C** | Per-site timeline endpoint | B-2: Swap `strategic-aggregator.js` to per-site fetch when backend exposes it |
| **12-D** | Q-2 Editable reports phase 1 | B-4: Inline edit UI for daily report fields + PATCH shape + diff viewer |

### Sprint 13 — Backend integration wave 2 + UX expansion

| Track | Theme | Items |
|---|---|---|
| **13-A** | Q-2 Vocabulary admin | B-5: Project-specific term registry; Insights tag system folds into it |
| **13-B** | Subcontractor CRUD | B-6: New surface gated behind `subcontractor_admin:manage`; reuses existing modal patterns |
| **13-C** | Q-3 Photo lifecycle | B-7: Soft-delete + upload; chunked multipart for video |
| **13-D** | Q-4 Global Ask | B-8: Cross-day AI query surface; pairs with Sprint 8.6 search palette |

### Sprint 14+ — Strategic / long-tail

| Item | Trigger |
|---|---|
| **BI embed** (`/insights`) | When cross-month / cross-portfolio analytics asks land and native charts can't satisfy them |
| **Mobile app** | When mobile-app codebase decision is made; entirely separate repo / tech stack |
| **MS Project `.mpp` import** | When a backend conversion service is available |
| **Resource-pool conflict detection** | After over-allocation banner gets real-world usage data |

---

## 5 · Items Requiring a Product Decision Before Sprint 11 Starts

These are the open questions from `PLAN.md` §4 that **block Sprint 12+**
scope. They don't block Sprint 11 (which is all frontend), but need an
answer before the Sprint 12 kick-off spec is written.

| Question | What to decide | Who |
|---|---|---|
| **Q-1 scope** | Fan-out `getActions(date)` N calls client-side, or wait for a real `GET /api/actions/all` aggregator endpoint? Sprint 11 ships the fan-out mock; confirm backend will expose the real endpoint before Sprint 12. | Product + Backend |
| **Q-2 scope** | Phase 1 editable reports (PATCH single field) or full vocab system first? Which report fields are editable? Who can edit (author only, or PM+)? | Product |
| **Q-3 photo delete** | Soft-delete only (recoverable by admin), or hard-delete after N days? | Product + Legal |
| **Q-3 photo upload** | Accept only images (current device assumption), or extend to video (200–300 MB chunked)? | Product + Backend |
| **Q-4 Ask scope** | Cross-day (fan-out existing `/api/ask` per date), or new `POST /api/ask/global` with server-side aggregation? | Backend |
