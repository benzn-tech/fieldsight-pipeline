# Plan — Aligning the Prototype With Backend Reality

## Design alternatives held for revisit

Quick recall: `grep -n "Design alternatives" PLAN.md`. Each entry below is
a candidate that was *not* chosen during a sub-sprint but is still
considered viable enough to come back to if the chosen direction proves
wrong.

### Activity page (`/activity`) — direction not chosen Sprint 4.6

The Sprint 4.6 redesign settled on **direction C — user activity stream**
(group events by user, "what each person did this week"). The two
alternatives below were rejected for now, but kept on file:

- **A — Kill `/activity` entirely.** It's coverable by `/timeline` (single
  day, structured) + `/tasks` (cross-day action tracker). Simplest path:
  remove the route + nav slot, delete the page + composite. Recall if
  user-activity-stream proves redundant in usage testing.
- **B — Repositioned as "raw on-site stream".** Show *unstructured* field
  signals before AI processing: PTT audio chunks just uploaded, photos
  mid-classification, voice notes, safety flags raised in real time —
  i.e., the raw event stream that feeds the daily-report pipeline. This
  is conceptually stronger but needs backend endpoints we don't have
  (per BACKEND-CONTEXT §10 the device→S3 path is one-way; there's no
  `/api/feed/raw` endpoint). Worth revisiting once backend exposes that
  stream.

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

### Phase A — Backend-shaped data layer (Sprint 2.1, ~½ day) ✅ done

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

### Phase B — Timeline page (Sprint 2.2, the big one) ✅ done

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

### Phase C — Topic-detail media (Sprint 2.3) ✅ done

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

### Phase D — Today as a derived view (Sprint 2.4) ✅ done

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

### Phases E – I (all shipped) ✅ done

- **E. Calendar + multi-day** — ✅ heat-mapped date picker on the
  Timeline page using `/api/dates`. Top-of-page widget toggles a
  full-month modal. (`scripts/composites/date-picker.js`,
  Sprint 2.5.)
- **F. Reports archive** — ✅ `/reports` page lists weekly/monthly
  history, presigned-URL download for `.docx`, regenerate button
  gated behind `report:create`. (`scripts/pages/reports.js`,
  Sprint 2.6.)
- **G. Ask Agent** — ✅ stateless chat strip per-topic and per-report;
  client-side history reconstruction. (`scripts/composites/ask-chat.js`,
  Sprint 2.7.)
- **H. Meeting Minutes** — ✅ separate composite set
  (`meeting-topic-card.js`, `meetings.js`, `meeting-minutes.fixture.js`)
  reading the §5.4 schema (`owner` not `responsible`, no `safety_flags`,
  decisions as objects). Daily / Meeting toggle when both exist for
  the date. (Sprint 2.8.)
- **I. Real auth + fetch** — ✅ scaffolding complete: `auth/cognito.js`
  wraps `USER_PASSWORD_AUTH`, `auth/session.js` manages tokens with
  auto-refresh, `api/_fetch.js` handles 401 retry + BUG-20 +
  `_accessDenied`, every api module dispatches on `useMocks`,
  `LoginScreen` + `AccessDenied` composites land. Prototype still ships
  with `useMocks=true` — flipping false activates the real flow.
  (Sprint 2.9.)

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

- **P-01 · Urgent now: surface risk + recommended action inline.** ✅ done
  Shipped on `claude/today-polish-p01-p02`. `today-adapter.js` now
  exposes `riskLevel` + `recommendedAction` on every urgent item;
  `urgent-card.js` renders the risk pill (high/medium/low) in the
  header and the recommended action under the observation. Risk-level
  left edge supplements the badge for safety items.

- **P-02 · Recent activity: removed.** ✅ done — chose (B).
  Decision: drop the section. The data was a remap of the same topics
  on `/timeline` (which is the canonical surface), so keeping both
  diluted Today's job as a quick dashboard. Today is now: brief →
  urgent → my/team tasks → on site. `todayAdapter` still emits
  `activity` so the field is available if a future "live feed"
  iteration wants it.

- **P-03 · Left-nav collapsed: logo + chevron overlap.** ✅ done
  Shipped on `claude/nav-collapse-p03-p04`. `logoAreaStyle` switches
  to `justifyContent: center` with zero horizontal padding when
  collapsed, and the F mark is no longer rendered — only the chevron
  stays, centred in the 64 px column.

- **P-04 · Left-nav collapsed: NavItem icons drift right.** ✅ done
  Shipped on `claude/nav-collapse-p03-p04`. The active stripe in
  expanded mode still uses `borderLeft` (text-row layout, 3 px is
  fine). In collapsed mode `borderLeft` is dropped and the stripe is
  drawn via `box-shadow: inset 3px 0 0 ...` — non-layout, so icons
  centre on the row's true mid-point.

- **P-05 · Dev role switcher dropdown chrome.** ✅ done
  Shipped on `claude/polish-p05-p06`. Replaced the native `<select>`
  with a custom popover dropdown so the menu inherits the dev panel's
  translucent dark blue (rgba(20,28,45,0.98)) and the active option
  is unambiguous: accent-orange text, accent-100 background, an
  accent left stripe, and a leading ✓. Optgroup labels (Hierarchy /
  Specialists) match the original `<optgroup>` shape. Closes on
  outside click + Escape.

### Loose ends from the build

- **P-06 · Today's date is hard-coded.** ✅ done
  Shipped on `claude/polish-p05-p06`. New `FS.api.todayNZDT()` helper
  returns `YYYY-MM-DD` in `Pacific/Auckland` via `Intl.DateTimeFormat`
  (DST-correct), with a manual UTC+13 fallback for engines lacking
  the time-zone tables. `today.js` calls it on mount; if the live
  date has no report it falls back to the most recent date in
  `/api/dates` and renders a "Latest available · DD MMM YYYY" banner
  so the UI never silently anchors on a stale fixture. Effective
  date is now threaded into the TaskCard's check-off persistence
  call so toggles land on the right (date, topic_id, action_index)
  key.

- **P-07 · `window.FieldSight._todayCache` should be a Context.** ✅ done
  Shipped on `claude/internals-p07-p11`. New `TodayContext` lives in
  `today.js`; new `TodayProvider` owns the Today state via
  `useTodayState` and exposes it via context. The page registry
  gained an optional `Provider` slot — `AppShell` reads the slot and
  wraps Middle + Right in it (or `React.Fragment` for pages without
  state-sharing needs). Provider is a `Context.Provider` underneath,
  so it adds no DOM and the existing flex layout of LeftNav / Middle
  / Right is unchanged. `window.FieldSight._todayCache` writes and
  reads are gone. Visible in React DevTools, scoped per-mount, and
  the pattern generalises — any future page that wants to share
  state between Middle and Right just exports a `Provider`.

- **P-08 · Session gate not wired.** ✅ done
  Shipped on `claude/auth-gates-p08-p12`. New `SessionGate` wrapper
  in `scripts/app-shell.js` decides between `LoginScreen` and
  `AppShell` based on `FS.api.useMocks` + `FS.session.isSignedIn()`.
  Subscribes to `session.onChange` so a successful sign-in (or a
  refresh failure that clears the session) swaps screens in place.
  In mock mode it short-circuits to `AppShell` directly — no auth
  needed. `mountAppShell` now mounts `SessionGate` as the root.

- **P-09 · Meeting topic right-detail.** ✅ done
  Shipped on `claude/meeting-detail-p09-p10`. `TimelineRightDetail`
  now dispatches by `kind`: daily topics keep their full tab set
  (Overview · Transcript · Audio · Video · Photos · Ask) while
  meeting topics get a focused two-tab view (Overview · Ask). The
  meeting Overview renders the §5.4 schema verbatim — decisions
  are object cards (text + rationale + decided-by), action items
  use `owner` (not `responsible`) with the read-only caption from
  P-10, and `open_questions` are rendered as a bulleted section.
  Header carries the same time / category / participants chrome
  plus a meeting-specific status pill (decided / deferred /
  in-discussion / blocked).

- **P-10 · Meeting action items are read-only by design.** ✅ done
  Shipped on `claude/meeting-detail-p09-p10`. Caption "Read-only —
  meeting actions are tracked in the minutes, not the daily-action
  audit log." sits below the action list in both the
  `MeetingTopicCard` body and the right detail's Meeting Overview.
  Renders as a faint italic note with a left stripe so it's clearly
  a system explanation, not an action item itself.

- **P-11 · Reduced-motion audit.** ✅ done
  Shipped on `claude/internals-p07-p11`. Sweep findings:
    * `tokens.css §16` zeroes `animation-duration` / `iteration-count`
      / `transition-duration` globally under reduced-motion (belt).
    * Three named keyframes total in the codebase, each with its own
      explicit override (suspenders):
        - `fs-task-checkoff` (P-04 / 2.4)  → `animation: none; opacity: 0`
        - `fs-ask-pending`   (Phase G / 2.7) → `animation: none; opacity: 0.6`
        - `fs-btn-spin`      (pre-existing) → animation slowed to 1.6 s
    * Sprint 2.5+ added no further keyframes (DatePicker, Reports,
      Meeting Minutes, Login, AccessDenied, Sprint 3 polish — all use
      simple hover / focus transitions covered by the global rule).
  Documented as an "Animation registry" comment block at the top of
  `composites.css` so any new keyframe is checked against the same
  belt-and-suspenders rule.

- **P-12 · 403 path coverage.** ✅ done
  Shipped on `claude/auth-gates-p08-p12`. Each page-level fetch in
  `today.js`, `timeline.js`, and `reports.js` now inspects the
  response for `_accessDenied: true` and routes to a new
  `state.status === 'access_denied'` branch that renders the
  `AccessDenied` composite. Special handling in `timeline.js`:
  meeting-minutes 403 (fetched via the generic media presigner)
  is downgraded to "no meeting" rather than blocking the daily
  report — the two surfaces are independently authorised.

After P-01 through P-12, the prototype is ready for handoff to a real
auth + fetch flip (Phase I activation) without UI surprises.

## Sprint 4 — Core operational pages + Programme MVP

Five sub-sprints, one PR each. See the Sprint 4 plan file
(`/root/.claude/plans/`) for detailed scope and risk mitigations.

- **Sprint 4.0 · Sites page (`/sites`)** ✅ done
  Shipped on `claude/sprint4-00-sites`. New `SiteCard` composite +
  `/sites` page with `SitesProvider` (Context shared between Middle
  and Right via the Sprint 3 P-07 page-Provider slot). Middle = list
  of sites with KPI mini-strip (users · reports · latest); right =
  selected site detail (recent reports + users on site, both
  click-through to `/timeline?date=…&user=…`). Worker role gates
  the list to `caller.primary_site` only.

- **Sprint 4.1 · Activity feed (`/activity`)** ✅ done
  Shipped on `claude/sprint4-00-sites` (stacked on 4.0). New
  `ActivityFeedRow` composite (multi-day variant of `ActivityCard` —
  drops "Xm ago" relative time in favour of HH:MM clock time so date
  grouping happens upstream). New `/activity` page with
  `ActivityProvider` running the same Sprint 3 P-07 page-Provider
  pattern. Default range = 5 most recent days with reports; "Load
  more" extends by 5 each click. Date headers (Today / Yesterday /
  Wed 28 Apr) group rows desc. Selected event opens a right-pane
  preview (counts strip + summary + "Open in timeline" CTA). Worker
  rule honoured client-side.
- **Sprint 4.2 · Tasks page (`/tasks`)** ✅ done — Q-1 commitment via fan-out
  Shipped on `claude/sprint4-00-sites` (stacked on 4.1). New
  `getActionsRange({from,to})` helper on `FS.api.actions` (additive,
  doesn't change `getActions(date)` shape), new
  `FS.api.tasks.getActionsResolvedRange` aggregator that joins
  timeline action source with audit overlay into a flat row contract.
  New `TasksFilterChips` composite (All / Mine / Open / Overdue /
  Done with counts) and `/tasks` page with `TasksProvider` (P-07
  pattern). Default range = trailing 14 days; default filter = Mine
  (or All for admin/gm). Right detail wires "Mark complete" through
  the existing `toggleAction` + optimistic-removal flow used by
  TaskCard. Heuristic deadline parser handles "Today HH:MM",
  "Tomorrow HH:MM", "DD MMM" — unparseable deadlines never count
  as overdue. Page header carries an explicit perf caveat:
  "Aggregated client-side — slow at scale until backend ships
  /api/actions/all".
- **Sprint 4.3 · Evidence library (`/evidence`)** ✅ done
  Shipped on `claude/sprint4-00-sites` (stacked on 4.2). New
  `EvidenceTabs` composite (Photos / Audio / Video / Transcripts
  with optional counts) and `/evidence` page with `EvidenceProvider`
  (P-07 page-Provider pattern). Default range = trailing 7 days
  with reports; "Load more" extends by 7. Tab activation drives
  rendering — Photos tab uses an aggregated count + central fetch
  (extracts `topic.related_photos` from per-day timelines and feeds
  one `PhotoGrid` per day); Audio / Video / Transcripts tabs render
  one Phase C composite per day, each fetching its own slice
  (parallel internal fetches, no central coordination needed).
  Right detail = read-only summary card (active tab name + range +
  found-count + tab blurb). Worker rule honoured (forced-self
  client-side).
- **Sprint 4.4 · Programme MVP (`/programme`)** ✅ done — biggest sub-sprint
  Shipped on `claude/sprint4-00-sites` (stacked on 4.3). New
  `FS.api.programme` module (`getProgramme` + `getProgrammeTasksForRange`,
  mock-only branch reads from `programme.fixture.js`; backend branch
  is a stub with documented endpoint contracts), new
  `programme.fixture.js` (1 programme · 5 WBS groups · 14 leaf tasks
  · 8 critical-path tasks · 4 linked report actions), and four new
  composites: `GanttStrip`, `TaskTreeCell`, `GanttRow`, and
  `ProgrammeTodoList`. New `/programme` page with `ProgrammeProvider`
  (P-07 page-Provider pattern) supporting Gantt/TO-DO view toggle,
  Day/Week/Month tier toggle, sticky WBS tree on the left, scrollable
  timeline on the right, today-marker overlay, group expand/collapse,
  critical-path highlighting (border), status-coloured bars, progress
  fill. TO-DO mode renders Jira-style buckets (This week / Next week /
  Later) with clickable rows. Right detail = full task fields + lazy
  linked-actions fetch (one `getTimeline` per unique linked date),
  click-through to `/timeline?date=…&user=…`. Worker rule applied
  inside the api module. Sprint 5 picks up imports / native edit /
  cascade engine.

  Sprint 4 complete. 5/14 nav slots → 9/14. Programme follow-ups
  (imports + native edit + cascade) tracked for Sprint 5; Compliance/
  admin (Safety/Quality/Team/Settings) tracked for Sprint 6;
  Strategic dashboards (Portfolio/Regional/Executive) tracked for
  Sprint 7.

## Sprint 4 follow-up sub-sprints (post-review feedback)

Five follow-up sub-sprints opened on top of 4.4 in response to the
post-walkthrough feedback. All stack on the same `claude/sprint4-00-sites`
branch (PR #14) until the user merges.

- **Sprint 4.5 · Today back-nav + Tasks subtitle clarity** ✅ done
  Two tiny UX fixes from the review:
  (1) `/today`'s "View daily report" CTA now appends `&from=today`
      to the timeline URL; `TimelineMiddleColumn`'s header detects
      that flag and renders a `← Back to Today` link above the
      title (no more digging for the left-nav).
  (2) `/tasks` subtitle replaced — verbose perf caveat lifted into
      a hover-tooltip on a small ⓘ icon. New subtitle: "Action items
      assigned across reports — yours, your team's, by status."
  Touched `today.js`, `timeline.js`, `tasks.js`, plus a small CSS
  block. Cache busters: today.js v=11, timeline.js v=9, tasks.js
  v=2, composites.css v=20.
- **Sprint 4.6 · Activity → user activity stream** ✅ done (direction C)
  Old chronological topic feed (4.1) replaced. New aggregator
  `FS.api.userActivity.getUserActivityRange({from,to})` joins
  per-user timelines + audit overlays into a per-user view that
  attributes events to whoever's named in
  `topic.participants` / `action.responsible` / `report.user_name`,
  regardless of which report each datum came from. Counts: Topics ·
  Actions · Photos · Safety. New `UserActivityCard` composite
  (avatar + name + 4-count strip + top-3 event preview). Right
  pane = full chronological event timeline grouped by date with
  border-coded kind colour and "Open in /timeline" click-through
  (carries `&from=activity` to surface a back-nav next sub-sprint).
  Worker scope = caller-only; site_manager / pm = primary_site
  scope; admin / gm = full visibility. Fixture verification:
  admin sees 8 users, 4 with non-zero counts; site_manager (Jarley)
  sees 4 users (sb1108-ellesmere); worker (Sarah Chen) sees only
  herself. Old `scripts/composites/activity-feed-row.js` deleted
  (no remaining references). Cache busters: composites.css v=21,
  pages/activity.js v=2, user-activity-aggregator.js v=1 (new),
  user-activity-card.js v=1 (new).
- **Sprint 4.7 · Programme full-width layout + slide-in drawer** ✅ done
  AppShell now respects a `layout: 'full-width'` flag on page registry
  entries. Programme declares it; other pages keep the 3-pane shell.
  Effects on `/programme`:
    • Middle column ignores the resize-handle width and flexes to
      fill the entire content area (~1300+ px on a typical desktop)
    • Static RightDetail pane is suppressed
    • Drag handle hidden (no neighbouring column to resize against)
    • New `RightDrawer` composite slides in from the right edge
      (440px wide, max 90vw) whenever a task is selected
    • Backdrop dims everything except the drawer; click closes
    • ESC key closes (only listens while open, so other pages
      unaffected)
  No changes to other pages — Today / Timeline / Sites / Activity /
  Tasks / Reports / Evidence keep the original 3-pane behaviour.
  Cache busters: app-shell.css v=3, app-shell.js v=9, programme.js
  v=2, right-drawer.js v=1 (new).
- **Sprint 4.8 · Jira-style 4-column kanban replaces ProgrammeTodoList** ⏳ pending
- **Sprint 4.9 · Gantt drag (L1 move + L2 edge resize)** ⏳ pending

## Sprint 4+ — Open product questions

Surfaced during the second-pass review of merged main. These aren't
bugs — they're product / scope decisions that need a yes/no before
any sprint commits to them. Each one is ~1 sprint of UI plus a
matching backend change (the prototype has zero backend hooks for
any of them).

- **Q-1 · Tasks page / cross-day audit aggregation.**
  `/api/actions` is keyed by date and writes to an immutable audit
  log (`fieldsight-audit` DynamoDB). Today the UI only surfaces
  per-action `Checked by …` captions inside one report; there's no
  "my open actions across the week" view, no "audit history of one
  action over time" drill-down, no completion-rate dashboard.
  - Surfaces likely needed: `/tasks` page (already a nav slot — no
    page yet), filtered by responsible/owner; per-action history
    drawer; weekly completion KPI on Today.
  - Backend: add `GET /api/actions/all?from=&to=&user=` aggregator,
    or have the UI fan out N `getActions(date)` calls.

- **Q-2 · Editable reports + vocabulary system.**
  BACKEND-CONTEXT §10 explicitly: reports are read-only, only
  checkboxes mutate. Two related needs raised in review:
  - Manual correction of inaccurate AI output (typo, mis-attributed
    speaker, wrong category). Needs `PATCH /api/reports/:date/:user`
    with audit + diff viewer + edit-in-place UI.
  - Custom vocabulary for project-specific terms ("SB1108",
    "MPI") so transcripts and reports get the spellings right.
    Needs a vocab admin surface + `POST /api/vocab` + Claude
    prompt injection.
  Both are sprint-sized features each. Decision needed before
  designing.

- **Q-3 · Photo lifecycle (delete + UI upload).**
  §10 explicitly: no UI upload (RealPTT devices push), no delete
  endpoint.
  - Delete: `DELETE /api/media/<key>` with permission gate (worker
    can't delete others'), soft-delete with retention window,
    audit. UI: trash icon in PhotoGrid + lightbox.
  - Upload: `POST /api/media/upload` with multipart / chunked
    upload (videos can be 200–300 MB). UI: drag-drop in topic
    detail. Changes the data-flow assumption (today: device-only
    inputs).

- **Q-4 · Global / cross-day Ask.**
  `/api/ask` is scoped to one (date, user) per call. Per-topic
  ("transcript" scope) and per-report ("both" scope) live in the
  current Timeline page. Reviewer asked about cross-day questions
  ("when was scaffold remediation last raised?"). Options:
  - Backend: new `POST /api/ask/global` that queries across a
    date range or all available reports.
  - Frontend-only: fan out N `/api/ask` calls and aggregate.
    Cheap to ship, expensive at runtime.
  - UI surface: a top-bar global search input, or a new `/search`
    nav item that opens a chat-style scope picker.
