# Plan — Aligning the Prototype With Backend Reality

## Context

The prototype was built (Sprints 0–2.0) before the backend's data shapes were
known. With `BACKEND-CONTEXT.md` now in hand, there is a clear mismatch
between what the UI assumes and what the API actually serves:

| Prototype assumes | Backend actually serves |
|---|---|
| `myTasks`, `teamTasks`, `urgent`, `activity`, `onSite` as parallel collections | A single `DailyReport[date, user]` containing `topics[]`, each with `action_items[]`, `safety_flags[]`, `key_decisions[]`, `related_photos[]` |
| Today as the primary surface | Daily-Report **Timeline by (date, user)** is the primary surface. Today is best modelled as a *derived* dashboard over the latest report |
| Static mock data, no auth | Cognito JWT, role-gated visibility, `/api/sites`, `/api/timeline`, `/api/transcripts`, `/api/audio-segments`, `/api/video-segments`, `/api/dates`, `/api/actions`, `/api/ask`, `/api/reports/history` |
| Tasks are first-class user-level entities | There is no user-level task list — action items live inside topics inside the daily report |
| No media model | Per-topic transcripts (with diarized speaker segments), audio segments, H264 video previews, photos |
| No calendar | Heat-map of dates with reports (`/api/dates`) |
| No Q&A | Ask Agent endpoint scoped to report or single topic |
| No archive | Daily/weekly/monthly history + regenerate |
| Single schema | Two report schemas: Daily Report (site walk) and Meeting Minutes (different field names: `owner` vs `responsible`, no `safety_flags`) |

The good news: **L1–L4 is reusable.** Tokens, atoms (Button/Input/Card/Badge/
Avatar), shell, drag divider, role/permission engine all stand. The router
already supports query params, so `/timeline?date=…&user=…` works without
router changes. The page-registry pattern accommodates new pages cleanly.

The lift is in **L5 composites + L6 pages + a new data layer**. The Today
page itself doesn't need to be thrown away — it can be re-pointed at adapters
that derive its sections from a `DailyReport`.

## Recommended approach: evolve, don't rewrite

A four-phase progression. Each phase ships independently and keeps the
preview HTMLs working.

### Phase A — Backend-shaped data layer (Sprint 2.1, ~½ day)

Goal: stop pretending. Get the in-memory mocks shaped like real API responses
so every subsequent feature is a drop-in for the real call.

- Create `scripts/api/` with one module per endpoint, each exporting an
  `async` function that resolves a backend-shaped object after a small delay:
  - `api/sites.js`         → `getSites()`, `getSiteUsers(site)`, `getUsers()`
  - `api/dates.js`         → `getDates({ months, site })`
  - `api/timeline.js`      → `getTimeline({ date, user })`
  - `api/transcripts.js`   → `getTranscripts({ date, user, start, end })`
  - `api/audio.js`         → `getAudioSegments({ … })`
  - `api/video.js`         → `getVideoSegments({ … })`
  - `api/actions.js`       → `getActions(date)`, `toggleAction({ … })`
  - `api/ask.js`           → `ask({ date, user, question, scope, topic_id })`
  - `api/reports.js`       → `getReportsHistory(limit)`, `regenerate({ … })`
- Move existing `scripts/mock-data.js` into `scripts/mock/` and add a
  **fixtures** file `mock/daily-report.fixture.js` that returns a
  realistic `DailyReport` (with `executive_summary[]` as array, en-dash
  `time_range`, populated `topics[]`, `safety_flags`, `action_items`,
  `related_photos`, `_report_metadata`).
- Single switch `window.FS.api.useMocks = true` so we can flip to real
  `fetch` later without rewriting call sites.

Critical files:
- new `scripts/api/index.js`
- new `scripts/mock/daily-report.fixture.js`
- new `scripts/mock/dates.fixture.js`
- new `scripts/mock/sites.fixture.js`
- existing `scripts/mock-data.js` kept as a thin shim that re-exports from
  fixtures during the transition

### Phase B — Timeline page (Sprint 2.2, the big one)

Goal: build the **missing primary surface** — `/timeline?date=…&user=…`.
This is what the backend exists to serve.

- New L5 composites in `scripts/composites/`:
  - `executive-summary-card.js` — bullet list, handles `string[]` not
    `string` (BUG-noted in backend doc)
  - `topic-card.js` — header (time_range, topic_title, category badge,
    participants), summary, expandable sections (decisions, action items,
    safety flags, photos)
  - `action-item-row.js` — checkbox + text + responsible + deadline + priority
    pill. Wraps `api/actions.toggleAction` and is keyed by
    `${topic_id}_${action_index}`. Optimistic update + revert-on-error.
  - `safety-flag-row.js` — risk-level badge (high=red, medium=amber,
    low=neutral) + observation + recommended_action
  - `category-badge.js` — `safety | progress | quality` (re-uses the
    existing Badge atom, just an opinionated wrapper)
  - `kpi-strip.js` — already exists; re-fed from report metadata
    (topic count, safety count, recordings_processed, total_words)
- New L6 page `scripts/pages/timeline.js`:
  - Middle column: header (date · user · site) → KpiStrip → ExecutiveSummary →
    list of TopicCard (collapsible), respecting BUG-19 NZDT date math
  - Right detail: TopicDetail panel (full topic + media tabs: Transcript,
    Audio, Video, Photos — each lazy-loaded)
  - Empty/no-report state: handle the three shapes — 404 JSON,
    `{ message, date }` 200 body, `{ available_users:[…] }` admin
    disambiguation
  - Content-type guard for the CloudFront SPA-fallback trap (BUG-20)
- Register at `/timeline` in `_page-registry.js`. Router already passes
  query params via `getCurrentRoute().params`.
- Add a "Timeline" item to the nav (under DAILY, between Today and
  Activity). Permission: `report:view`.

Critical files:
- new `scripts/pages/timeline.js`
- new `scripts/composites/{executive-summary-card, topic-card, action-item-row, safety-flag-row, category-badge}.js`
- edit `scripts/pages/_page-registry.js` for `/timeline` registration
- edit `scripts/fs-globals.js` NAV_ITEMS (add timeline)
- edit `styles/composites.css` for new composites
- edit `app-shell-preview.html` script loads + cache-buster bump

### Phase C — Topic-detail media (Sprint 2.3)

Goal: when a topic is selected in the right pane, show the recordings
behind it.

- Tabs in the RightDetail when route is `/timeline`:
  - **Transcript** — speaker_segments list, jump-to-time. Note that
    `spk_0/spk_1` are not stable across files (BUG 8.6), so colour by
    *position within current view*, not globally.
  - **Audio** — segment playlist; React state via `onplay/onpause` events,
    not `audioRef.current.paused` (BUG-21).
  - **Video** — only `is_preview:true` (H264). Use `offset_sec` for
    jump-to-start. `<video preload="metadata">`.
  - **Photos** — grid of thumbnails. For each `topic.related_photos`
    filename, build `users/{folder}/pictures/{date}/{file}` and call
    `api/media.presignedUrl`. `<img loading="lazy">`. Re-fetch on modal
    re-open (15-min URL expiry).
- New composites: `transcript-list.js`, `audio-playlist.js`,
  `video-player.js`, `photo-grid.js`.

### Phase D — Today as a derived view (Sprint 2.4)

Goal: keep the existing Today UI, but feed it from the latest `DailyReport`
for the current user instead of bespoke mocks.

- New adapter `scripts/api/today-adapter.js`:
  - `morningBrief.bullets`  ← `report.executive_summary`
  - `urgent`                ← topics where `category==='safety'` OR
    `safety_flags.length>0`, plus `safety_observations` with risk_level=high
  - `myTasks`               ← `topics[*].action_items` filtered to
    `responsible === currentUser.display_name`
  - `teamTasks`             ← rest of `action_items` (gated by role —
    workers only see myTasks)
  - `activity`              ← topics ordered desc by time_range, mapped to
    `{speaker, snippet, timeAgo, channel}`
  - `onSite`                ← `getSiteUsers(currentUser.primary_site)`
- Existing TaskCard/UrgentCard/ActivityCard/MorningBriefCard/OnSiteCard
  stay; only `today.js` swaps its data source.
- Now Sprint 2's previously-planned task check-off animation lands on
  *real* action items, with the toggle going through `api/actions`.

### Phase E and beyond (subsequent sprints, scope outline only)

- **E. Calendar + multi-day** — heat-mapped date picker on the
  Timeline page using `/api/dates`. Top-of-page widget that toggles to a
  full-month modal.
- **F. Reports archive** — `/reports` page: list weekly/monthly history,
  presigned-URL download for `.docx`, regenerate button (admin/pm only,
  workers forced to self).
- **G. Ask Agent** — chat strip per-topic and per-report; client-side
  history reconstruction since the endpoint is stateless.
- **H. Meeting Minutes** — separate composite set
  (`meeting-topic-card.js`, etc.) that reads the meeting schema (`owner`
  not `responsible`, no `safety_flags`). Side-by-side toggle when both a
  daily report and meeting minutes exist for the same date.
- **I. Real auth + fetch** — replace `auth-mock.js` with a Cognito
  `USER_PASSWORD_AUTH` flow, switch `window.FS.api.useMocks` to false,
  add token refresh, render the role-aware empty/403 state. Until this
  ships, the prototype stays preview-only.

## Concrete next step (what I'd do this week)

**Sprint 2.1 — backend-shaped data layer + fixture for Daily Report.**
Smallest viable step that unlocks everything else. ½–1 day.

Deliverables:
1. `scripts/api/` modules with mock implementations (no real fetch yet).
2. `scripts/mock/daily-report.fixture.js` — one fully populated `DailyReport`
   matching §5.1 of `BACKEND-CONTEXT.md` (3 topics, varied categories,
   action items, safety flags, photos, en-dash time ranges).
3. `scripts/mock/dates.fixture.js`, `sites.fixture.js`, `actions.fixture.js`.
4. Old `scripts/mock-data.js` becomes a shim built atop these fixtures via
   the today-adapter sketch (Phase D), so the existing Today page keeps
   rendering unchanged and we prove the adapter pattern works end-to-end
   for one surface.
5. `node --check` clean across all new files.
6. `app-shell-preview.html` cache-busters bumped.

After this sprint, **Phase B (Timeline page)** becomes a pure UI exercise
against a stable, backend-shaped contract.

## Verification

For each phase:
- `for f in scripts/**/*.js scripts/api/*.js scripts/mock/*.js scripts/composites/*.js scripts/pages/*.js; do node --check "$f"; done`
- `python3 -m http.server 8765` → open `app-shell-preview.html`
- Confirm Today still renders as before (Sprint 2.1 is non-breaking).
- For Sprint 2.2 onward: navigate to `#/timeline?date=2026-04-29&user=Jarley_Trainor`,
  open right detail, switch tabs.
- Role rotation: dev role switcher (`?dev=1`) cycles through admin / gm /
  pm / site_manager / worker; verify visibility of nav, user picker, and
  team-task vs my-task buckets at each level.
- Backend-doc traps to spot-check: NZDT date math (BUG-19), CloudFront
  HTML-404 (BUG-20), audio paused-ref (BUG-21), en-dash time_range,
  speaker label non-stability (BUG 8.6), 15-min presigned-URL expiry.

## Critical files reference

| Concern | File |
|---|---|
| Hash router (already supports query params) | `scripts/router.js` |
| Page registry (direct-match by path) | `scripts/pages/_page-registry.js` |
| Roles/permissions engine | `scripts/roles.js` |
| Nav items + token mirror | `scripts/fs-globals.js` |
| App shell + middle/right column wiring | `scripts/app-shell.js` |
| Existing today page (will become derived view) | `scripts/pages/today.js` |
| Existing composites to reuse | `scripts/composites/{task-card,urgent-card,activity-card,morning-brief-card,on-site-card,timeline,stat-card,kpi-strip}.js` |
| New (Phase A) data layer | `scripts/api/*.js`, `scripts/mock/*.fixture.js` |
| New (Phase B) timeline composites | `scripts/composites/{executive-summary-card,topic-card,action-item-row,safety-flag-row,category-badge}.js` |
| New (Phase B) timeline page | `scripts/pages/timeline.js` |
| Preview entry | `app-shell-preview.html`, `components-preview.html` |

## Sprint 3 — Polish backlog (post-Phase-I review)

Items raised during the post-Phase-I review pass. Phases A–I are merged
and shipped; this section captures everything that should land in
**Sprint 5 (Flows + polish)** per the CLAUDE.md roadmap, plus a few
loose ends from the build itself. Reorder / cull as needed before
starting the sprint.

### From end-to-end UI review

- **P-01 · Urgent now: surface risk + recommended action inline.**
  `urgent-card.js` already renders `item.body` (which carries
  `safety_flags[0].observation`), but the **risk-level badge** and
  **recommended_action** are only visible after opening the right
  detail. Reuse `safety-flag-row.js` (dense mode) or extend UrgentCard
  with a one-line action caption. ~30 min.
  Files: `scripts/composites/urgent-card.js`, `scripts/api/today-adapter.js`,
  `styles/composites.css`.

- **P-02 · Recent activity: clarify purpose or remove.**
  Today's `Recent activity` reads from the same topics as `/timeline`
  (todayAdapter just remaps `topics → {speaker, snippet, timeAgo,
  channel}` desc). Two viable directions, pick one before building:
    - **(A) Live feed**: only show topics from the last ~90 minutes
      plus latest PTT chatter, so it functions as a "what's happening
      now" stream that complements (not duplicates) /timeline.
    - **(B) Remove**: Today already has morning brief / urgent / my
      tasks / on site — drop the activity section to tighten the
      dashboard.
  Decision required before implementation.
  Files: `scripts/pages/today.js`, `scripts/api/today-adapter.js`.

- **P-03 · Left-nav collapsed: logo + chevron overlap.**
  When `isCollapsed`, `logoAreaStyle` lays out a 28 px logo + 28 px
  chevron with `space-between` inside a 64 px column (40 px usable
  after padding) → they overlap. Fix: hide the F mark when collapsed
  and centre the chevron.
  Files: `scripts/left-nav.js` (logoAreaStyle + render).

- **P-04 · Left-nav collapsed: NavItem icons drift right.**
  `NavItem` carries `borderLeft: 3px solid transparent` for the active
  indicator. When the row is collapsed and centred (`justifyContent:
  center`), the 3 px border eats space inside the flex, so every icon
  visually shifts right of true centre. Fix: move the active indicator
  to a `box-shadow inset` or absolutely-positioned ::before, so it
  doesn't consume layout width.
  Files: `scripts/left-nav.js` (itemStyle).

- **P-05 · Dev role switcher dropdown chrome.**
  The native `<select>` opens a UA-chrome dropdown that (a) doesn't
  match the dev panel's translucent dark blue and (b) renders the
  selected row in the same colour as the menu background, killing
  visibility. Two options:
    - Quick: add `color-scheme: dark` + `option { background: ... }`
      for partial cross-browser improvement.
    - Proper: replace with a custom dropdown (mirror the timeline
      view-toggle / date-picker month-nav pattern).
  Dev-only surface, lowest priority.
  Files: `scripts/dev-role-switcher.js`.

### Loose ends from the build

- **P-06 · Today's date is hard-coded.**
  `scripts/pages/today.js` uses `FIXTURE_DATE = '2026-04-29'` to anchor
  the prototype on the fixture report. Production needs an NZDT
  "today" computed via `FS.api.addDaysISO(...)` against the user's
  clock. Trivial replacement — guarded by the fact the fixture only
  has one date, so flipping requires either generating more fixtures
  or wiring real fetch.
  Files: `scripts/pages/today.js`.

- **P-07 · `window.FieldSight._todayCache` should be a Context.**
  Phase D shares the live Today snapshot from Middle to Right via a
  window slot. Works, but breaks under SSR / multiple instances and
  is invisible to React DevTools. Replace with a small React Context
  provider mounted by the Today page.
  Files: `scripts/pages/today.js`.

- **P-08 · Session gate not wired.**
  Phase I ships `LoginScreen` + `FS.session` + `_fetch.js` but the
  shell never decides between them. When `FS.api.useMocks=false`,
  AppShell should mount `LoginScreen` whenever
  `FS.session.isSignedIn() === false` and re-mount the main shell on
  `session.onChange`. Currently flipping `useMocks=false` would render
  the main shell with no auth header → 401 cascade. Add the gate
  before any real-auth attempt.
  Files: `scripts/app-shell.js`, `app-shell-preview.html` (or a new
  `app.html` for the live build).

- **P-09 · Meeting topic right-detail.**
  `TimelineRightDetail` only handles `selectedItem.kind === 'topic'`.
  Selecting a meeting topic emits `kind: 'meeting_topic'`, which
  falls through to the placeholder. Add a meeting topic detail panel
  (Overview / Decisions / Open questions; no media tabs since meeting
  audio/video isn't part of the daily report's recording bundle).
  Files: `scripts/pages/timeline.js`.

- **P-10 · Meeting action items are read-only by design.**
  `/api/actions/toggle` keys off the daily report's topic_id. Meeting
  action items live in a different schema and have no toggle endpoint.
  `MeetingTopicCard` currently renders them as plain rows — add a
  visible caption ("Read-only — meeting actions are tracked in
  minutes") so users don't expect to check them off.
  Files: `scripts/composites/meeting-topic-card.js`.

- **P-11 · Reduced-motion audit.**
  Sprint 2.4's task check-off animation respects
  `prefers-reduced-motion`. The Sprint 2.7 ask-pending dots animation
  also does. Sweep the rest of `composites.css` for any keyframes /
  transitions added in 2.5+ that don't have a reduced-motion fallback
  (date-picker dot pulses, login-screen focus rings).
  Files: `styles/composites.css`.

- **P-12 · 403 path coverage.**
  Pages currently treat `_accessDenied: true` responses as generic
  errors. Wire `AccessDenied` composite into `today.js`, `timeline.js`,
  and `reports.js` empty branches so the empathetic 403 actually
  renders when the API rejects a cross-user request (BACKEND-CONTEXT
  §8.4). The composite exists; just needs hookup.
  Files: `scripts/pages/{today,timeline,reports}.js`.

After P-01 through P-12, the prototype is ready for handoff to a real
auth + fetch flip (Phase I activation) without UI surprises.
