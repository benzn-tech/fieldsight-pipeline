
---

## 7 · Sprint 9 — Backend integration with P1/P2 endpoints

> Committed 2026-05-06 — paired with `fieldsight-pipeline` ROADMAP.md P3 and `INTEGRATION_PLAN.md` stage C.
> Backend P0/P1/P2 features have just merged to `develop` (pipeline-side stage A); 9 endpoints are now live but unwired in this UI. This sprint wires them in three sub-PRs.

### Conventions (apply to every endpoint)

1. **One commit per endpoint.** Message: `feat(api): wire /api/<name> — Sprint 9.x`.
2. **One module per endpoint** in `scripts/api/`; do not edit `index.js`.
3. **Mock fixture first.** Add fixture data, then flip `useMocks=false` and verify both modes pass smoke.
4. **Schema first.** Update `BACKEND-CONTEXT.md` (§4.13–§4.21) before writing client code.
5. **No backend edits from this repo.** Anything the backend needs to change goes in `UI_BACKEND_REQUESTS.md`.
6. **One PR per Sprint sub-bullet** (9.1, 9.2, 9.3 are separate PRs).

### 9.1 — Read-only surfaces (small, ~1 week)

- [ ] **`GET /api/calendar-events`** → in `composites/date-picker.js`, render due-date red dots alongside existing recording dots.
  - Fixture: extend `dates.fixture.js` with `events: [{date, title, type}]`.
  - UX: tooltip on hover shows event title.
- [ ] **`GET /api/onepager`** → in `pages/reports.js`, add "Open one-pager" button next to each report card; opens HTML in new window.
  - Backend returns `{html_url}`; UI does `window.open(html_url, '_blank')`.
- [ ] **`GET /api/topics/priority` + `POST /api/topics/priority`** → in `composites/topic-card.js` header, add a priority pill (Low/Med/High) — readers see it; pm+ can click to override.
  - On override, optimistic update + POST; on failure, revert + toast.

### 9.2 — Write paths (medium, ~1–2 weeks; pairs with §4 Q-2)

- [ ] **`POST /api/reports/correction` + `GET /api/corrections`** → inline edit modal on report cards. Show `✏ Corrected` badge if a correction exists.
  - State machine: `idle → editing → saving → saved | error`.
  - Read corrections at report load; merge into rendered text.
- [ ] **`POST /api/analytics/events`** → wire existing `EventTracker` from mock to live endpoint. Batch up to 10 events; flush on tab unload via `navigator.sendBeacon`.

### 9.3 — Admin views (medium, ~1–2 weeks; pairs with §4 Q-4)

- [ ] **`GET /api/dashboard`** → `/sites` page swaps mock adapter for live data. Keep mock fixture for offline preview / CI.
- [ ] **`GET /api/search`** → search palette gains a "server" scope (alongside the existing client-side entity search). Debounce 300ms; show loading spinner.
- [ ] **`POST /api/ask` (global scope)** → cross-day Ask. Surface = the search palette's "Ask" tab. Result includes `citations: [{date, topic_id}]` linkable into the timeline.

### Deferred (waiting on backend)

- ⏸ **`GET/POST /api/digest`** — pipeline Lambda not yet built (commit marker says "Lambda TBD"). Hold until `UI_BACKEND_REQUESTS.md` confirms it's live.

### Sprint 9 sign-off

- [ ] All `useMocks=true` paths still green (offline preview unchanged).
- [ ] All `useMocks=false` paths green against dev CloudFront.
- [ ] `BACKEND-CONTEXT.md` §4.13–§4.21 reflects deployed schemas exactly.
- [ ] No new entries in `UI_BACKEND_REQUESTS.md` "Open requests" without a status update.
