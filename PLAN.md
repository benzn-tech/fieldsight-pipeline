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

- **Sprint 4.10 · Today × Programme integration + Board Mine/All filter** ✅ done
  Connects the planning layer (Programme) to the execution layer
  (Today). Eight surgical pieces:

  1. `ProgrammeMiddleColumn` reads `?task=T-XXX` from the URL on
     mount and auto-selects the task (RightDrawer slides in
     automatically). Used by 4.10.6 click-throughs.
  2. New `FS.api.todayProgramme.getTodayProgrammeTasks` adapter
     module — finds programme tasks where today ∈ task.start..end
     AND caller ∈ task.assignees, returns rows shaped for the
     Today UI with Day-N-of-M + progress + critical flag.
  3. `TodayProvider` now fans the adapter in parallel with the
     daily-report load; result lives at `state.data.programmeTasks`.
     Empty-state still shows programme tasks if any (programme is
     not gated on a daily report existing).
  4. New `ProgrammeTaskCard` composite — Today-page variant of
     TaskCard for programme rows. WBS code prefix, accent left
     border (danger when critical), Day N of M, dominant progress
     bar. No checkbox (programme progress is multi-day).
  5. Today's My-tasks region splits into two visually-distinct
     subgroups: "From your programme · N" (above) and "From recent
     reports · N" (below). Same parent SectionLabel, so the user
     reads them as ONE list with provenance.
  6. Click any programme card on Today → navigates to
     `/programme?task=T-XXX&from=today`. Drawer auto-opens via 4.10.1.
  7. Timeline `← Back to Today` link restyled — now a full-width
     dark-navy CTA matching `fs-today__view-report-cta` so the
     round-trip looks symmetrical from both ends.
  8. `ProgrammeKanbanBoard` adds a Mine/All chip toggle (workers
     hidden — api already enforces the scope; site_manager / pm /
     admin / gm see it). Counts on each chip; column totals
     refresh against the filtered set.

  Fixture tweak: T-003 Foundation pour end-date extended from
  2026-04-30 → 2026-05-01 so the demo date carries both a wrap-up
  (T-003 95%) and a kickoff (T-004 12%) for Jarley — exactly the
  scenario described in the original product question.

  Smoke (node, mock mode):
    • Jarley (site_manager) → 2 today programme tasks
      (T-003 wrap-up + T-004 kickoff, both critical)
    • Sarah (worker) → 1 task (T-003 only — she joins T-005
      starting 2026-05-04)
  Cache busters: composites.css v=24, today.js v=12, timeline.js
  v=10, programme.js v=5, programme-kanban-board.js v=2,
  today-programme-adapter.js v=1 (new), programme-task-card.js
  v=1 (new).

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
- **Sprint 4.8 · Jira-style 4-column kanban replaces ProgrammeTodoList** ✅ done
  Programme's Board view (renamed from "TO-DO") is now a four-column
  status grid modelled on the Jira active-sprint screenshot the user
  shared. Columns: Not started · In progress · Blocked or Delayed
  (combined to keep the board 4-wide) · Done. Rows = WBS parent
  groups (Earthworks & Foundations / Structure / Envelope / Services /
  Fit-out). Cards distribute into the column matching their
  `status`. Per-card chrome: WBS code top-left, Critical badge
  top-right, task name (semibold), inline progress bar in the
  In-progress column only, assignee Avatar + first name, date
  range footer. Top-of-board status totals strip and per-row
  group header with collapsible chevron (collapsed-set shared
  with the Gantt view via `ProgrammeProvider.collapsed`). Empty
  cells render as 45° hatched panels for visual rhythm.
  Old `programme-todo-list.js` composite and its `.fs-prog-todo*`
  CSS deleted (no remaining references after the swap). Smoke
  test: distribution across the 14 fixture leaves = 10 Not started
  / 2 In progress / 0 Blocked / 2 Done; rows × columns matrix
  prints expected 5×4. Cache busters: composites.css v=22,
  programme.js v=3, programme-kanban-board.js v=1 (new).
- **Sprint 4.9 · Gantt drag (L1 move + L2 edge resize)** ✅ done
  Last sub-sprint of the post-review batch. Programme Gantt bars
  are now interactive — three drag modes:
    • **L1 move** — pointerdown on bar body → translate whole bar
    • **L2 resize-start** — pointerdown on first 8 px → only `start` moves
    • **L2 resize-end** — pointerdown on last 8 px → only `end` moves
  Drag is gated to leaf tasks (group rows + completed tasks aren't
  draggable). Snap is implicit: `Math.round(deltaPx / pixelsPerDay)`
  → days. Bounds clamp to `programme.start_date` / `end_date`;
  start ≤ end enforced. Optimistic preview updates the bar position
  in flight; commit happens on `pointerup` via the new
  `ProgrammeProvider.updateTask({task_id, start, end})` which
  mutates the in-memory leaves[] and re-publishes state. Cursor
  affordances (grab / grabbing / ew-resize) on the bar plus a
  body-level `fs-gantt-dragging` lock on selection during drag.
  Pointer capture so the drag survives the cursor leaving the bar.
  Cascade engine explicitly out of scope — Sprint 5.2 owns it.
  Cache busters: composites.css v=23, gantt-row.js v=2,
  programme.js v=4. Smoke math: +60px @ 24ppd → +3 days; -36px
  resize-end @ 24ppd → -1 day; snap rounds correctly across
  12/24/35/36 px boundaries.

  Sprint 4 follow-up batch complete (4.5–4.9). Total post-review
  cost: 5 sub-sprints, ~13 new files, 2 deleted, no nav-slot
  changes. PR #14 ready for review/merge.

## Sprint 5 — Programme operability (active)

Sprint 4 delivered Programme as a **read-only** Gantt + Board +
RightDrawer surface (4.4) with one interactive escape hatch —
drag-to-reschedule shipped in 4.9. Sprint 5 closes the rest of the
operability gap that 4.4 deliberately deferred: **task editing,
creation, deletion**, **importing** an external programme from
CSV / MS Project XML, and a **cascade engine** that keeps dependent
tasks + critical path consistent when anything changes. Detailed
plan in `/root/.claude/plans/ok-very-good-plan-sprint4-graceful-trinket.md`
(Sprint 5 section).

Branch strategy: stack all sub-sprints on
`claude/sprint5-00-programme-operability` and roll the PR title
forward (same convention as the Sprint 4 follow-up batch).

User decisions captured at sprint open:

| Question | Choice |
|---|---|
| Imports scope | CSV + MS Project XML (no Excel; .mpp not feasible client-side) |
| Cascade engine depth | Medium — chain-shift + CPM recompute (~80 LoC) |
| Reverse linking (action → programme progress) | Defer to Sprint 6+ — field-test 4.10 first |

Critical invariants across all sub-sprints:
- No build step (every new lib via UMD/CDN; everything else native).
- Mock-only persistence — all writes mutate `leaves[]` in
  `ProgrammeProvider`'s React state; reload still resets to fixture.
- No backend API changes — additive provider methods (`editTask`,
  `addTask`, `deleteTask`, `replaceTasks`) live alongside 4.9's
  `updateTask`.
- Reuse Sprint 4 patterns — RightDrawer for modal architecture,
  Input/Select/DatePicker for forms, `updateTask` reducer pattern
  for new mutations.

Execution order: `5.0 → 5.1 → 5.6 → 5.2 → 5.3 → 5.4 → 5.5 → 5.7`.
5.6 is pulled forward so all subsequent reducers (`addTask`,
`replaceTasks`) plug into the cascade pipeline at write time, not
bolted on later.

- **Sprint 5.0 · ModalOverlay primitive** ✅ done
  Centred modal composite for 5.1 task editor and 5.4 import flow.
  New `scripts/composites/modal-overlay.js` (~97 LoC) + appended
  `.fs-modal*` block in `styles/composites.css` (~85 LoC). Mirrors
  RightDrawer's backdrop + ESC + always-mounted-while-open pattern
  but layers above the drawer (z=50) via `--z-modal=500` so a modal
  opened from inside the drawer visually stacks on top. Supports
  sm/md/lg sizes, optional title with `aria-labelledby`,
  `closeOnBackdrop` toggle (default true; 5.1 editor passes false
  to protect unsaved input), and respects
  `prefers-reduced-motion`. Cache busters: composites.css v=25,
  modal-overlay.js v=1.

- **Sprint 5.1 · ProgrammeTaskEditor** ✅ done
  Modal form for editing a Programme leaf task. Mounted inside
  `ProgrammeRightDetail` and triggered by an Edit button next to
  the detail close (×). Commits via new
  `ProgrammeProvider.editTask({task_id, patch})` reducer — same
  in-memory mutation pattern as 4.9's `updateTask` drag handler.
  New `scripts/composites/programme-task-editor.js` (~306 LoC)
  covers name, status, progress %, start/end dates, assignees,
  tags, and a `depends_on` checkbox grid grouped by WBS parent.
  Pure `validatePatch(patch, taskId)` factored out and exposed at
  `window.FieldSight._programmeEditor.validatePatch` for node
  tests: name non-empty, start ≤ end, depends_on excludes own id
  (1-step cycle; full DAG check lives in 5.6), progress_pct in
  [0, 100]. On success recomputes `duration_days = diffDays(start,
  end) + 1`. Native `Input type="date"` rather than `DatePicker`
  here — the existing DatePicker queries report-day fixture data
  which is unrelated to programme task scheduling. Cache busters:
  composites.css v=26, programme.js v=6,
  programme-task-editor.js v=1.

- **Sprint 5.6 · Cascade engine (medium depth)** ✅ done
  Pure module `scripts/api/programme-schedule.js` (~216 LoC)
  exporting `cascadeFromTask(leaves, task_id, deltaDays)` and
  `computeCriticalPath(leaves, programmeStartISO)`. Both run a
  Kahn's-algorithm cycle check up front; on cycle they
  `console.warn` and return safe values (input unchanged for
  cascade, `[]` for CPM) — never deadlock. Cascade is a
  chain-shift on the transitive dependents of the trigger task.
  CPM is a standard forward + backward pass returning task_ids
  with zero slack. Date helpers (`addDaysISO`, `diffDaysISO`)
  inlined so the module stays node-importable without booting
  `FS.api`. `programme.js` now routes both `updateTask` (drag)
  and `editTask` (form) through a single `applyTaskMutation`
  helper that: applies the patch, computes end-delta, cascades,
  recomputes critical_path. Initial mount also recomputes the
  critical path from the loaded fixture rather than using the
  stored `critical_path` array — so the fixture's value is now a
  hint, not the authority. Note: the engine-computed CP for the
  current fixture is `T-001 → T-002 → T-003 → T-004 → T-006 →
  T-007 → T-013 → T-014` (92 days) which is genuinely longer
  than the fixture's hand-coded chain through T-009 (86 days);
  the math wins. Cache busters: programme.js v=7,
  programme-schedule.js v=1.

- **Sprint 5.2 · Add task** ✅ done
  Shipped on `claude/sprint5-00-programme-operability`.
  ProgrammeTaskEditor gains a `mode='create'` branch — WBS-group
  selector, blank defaults on open, required start/end validation,
  onSubmit emits `{parentId, name, status, …}` instead of a patch.
  New `ProgrammeProvider.addTask` reducer mints `task_id` as
  `T-<NNN>` from `max(numeric suffix) + 1` (scans suffixes, not
  array length, so deletes never cause id reuse), derives WBS as
  `{parent.N}.(maxSuffix+1)`, appends the leaf, and recomputes the
  critical path through the same `applyTaskMutation` helper used by
  5.6. New `+ Add task` primary button in the programme header
  toolbar opens the create-mode editor. Cache busters:
  programme-task-editor.js v=2, programme.js v=8.

- **Sprint 5.3 · Delete task** ✅ done
  Shipped on `claude/sprint5-00-programme-operability`. New
  `ProgrammeProvider.deleteTask(taskId)` reducer removes the leaf
  from `leaves[]`, **scrubs the deleted id from every other leaf's
  `depends_on[]`** so dangling references can't cause cascade
  infinite-loops in the 5.6 engine, and recomputes the critical
  path. ProgrammeTaskEditor gains an `onDelete` prop and a Delete
  button in the footer (edit mode only). First click shows a ghost
  button with danger colour; second click changes to a full
  `variant='danger'` "Confirm delete?" button; confirming calls
  `onDelete(task_id)` + `onClose`. Cancel resets the confirm state.
  ProgrammeRightDetail wires `onDelete` → `ctx.deleteTask` +
  `setEdit(false)` + drawer close so the panel closes immediately.
  Cache busters: programme-task-editor.js v=3, programme.js v=9.

- **Sprint 5.4 · CSV import** ✅ done
  Shipped on `claude/sprint5-00-programme-operability`. New
  `scripts/api/programme-import.js` (plain script, no Babel) —
  BOM + CRLF-tolerant parser with RFC-4180-ish tokeniser, validates
  required columns (task_id, wbs, name, start, end, status),
  parses pipe-separated lists for `depends_on` + `assignees`,
  splits output into `{ parents, leaves, errors, warnings }`.
  New `scripts/composites/programme-import-modal.js` — three-phase
  ModalOverlay wrapper: (1) drop-zone (drag-drop + click-to-browse,
  .csv only); (2) preview — validation report (errors block confirm;
  warnings advisory), WBS-ordered task table (max 20 rows shown,
  overflow count), replace-note; (3) confirm calls new
  `ProgrammeProvider.replaceTasks(parents, leaves)` which does a
  full in-memory snapshot swap and recomputes CPM from scratch.
  "Import…" secondary button added to Programme toolbar next to
  "+ Add task". Cache busters: composites.css v=27,
  programme-import.js v=1 (new), programme-import-modal.js v=1
  (new), programme.js v=10.

- **Sprint 5.5 · MS Project XML import** ✅ done
  Shipped on `claude/sprint5-00-programme-operability`. New
  `parseMSProjectXML(text)` in `scripts/api/programme-import.js`
  does a namespace-agnostic `DOMParser` walk of
  `<Project>/<Tasks>/<Task>`, mapping `<UID>` → `T-NNN` task_id,
  `<WBS>` → wbs, `<Name>`, `<Start>`/`<Finish>` (YYYY-MM-DD prefix
  only), `<PercentComplete>` → progress_pct + derived status,
  `<Summary>` / `<OutlineLevel>=1` → group rows.
  `<PredecessorLink>` resolves FS relationships only (Type=1);
  non-FS relationships and lag are warned once. Parent ids
  resolved by WBS ancestor walk so output keeps the FS fixture's
  two-level group/leaf shape. Calendars, resource assignments,
  non-FS links, and lag warned (once) and ignored. Returns the
  same `{ parents, leaves, errors, warnings }` contract as
  `parseCSV`. The Sprint 5.4 modal now accepts `.csv` and `.xml`
  with extension validation in DropZone, dispatches to the right
  parser in `handleFile`, and updates pick-phase title, aria-label,
  hint text, and column guide for both formats. Cache busters:
  composites.css v=28, programme-import.js v=2,
  programme-import-modal.js v=2.

- **Sprint 5.7 · Wire-up + cache-buster sweep** ✅ done
  Wraps the sprint. Per-sub-sprint cache busters were already
  bumped at write time (programme.js v=5 → v=10 across 5.1/5.6/
  5.2/5.3/5.4; composites.css v=24 → v=28 across 5.0/5.1/5.4/
  5.5; programme-task-editor.js v=1 → v=3 across 5.1/5.2/5.3;
  programme-import.js v=1 → v=2 across 5.4/5.5;
  programme-import-modal.js v=1 → v=2 across 5.4/5.5; new
  modules modal-overlay.js + programme-schedule.js shipped at
  v=1) so no sweep-bump was needed in this commit. Reduced-motion
  audit: Sprint 5 introduced **zero new `@keyframes`** —
  ModalOverlay's slide-in is pure CSS transition (opacity +
  transform), zeroed by tokens.css §16's global belt and reset
  by an explicit suspenders block at composites.css §Sprint-5.0
  (`transition: none; transform: translate(-50%, -50%)` under
  `prefers-reduced-motion: reduce`). The animation registry
  comment block at the top of composites.css therefore needs
  no new entry. `node --check` clean across all 86 JS files
  under `scripts/`.

- **Sprint 5.7.1 · Post-deploy follow-up batch** ✅ done
  Four issues caught in the first round of human testing on the
  branch:

  1. *Modal clipped by RightDrawer.* Clicking Edit inside the
     drawer rendered the modal off-centre and partially hidden —
     the drawer creates its own stacking context (it has
     `transform` + `overflow: hidden`), so the centred modal got
     positioned relative to the drawer rather than the viewport
     and was clipped. Fix: `ModalOverlay` now uses
     `ReactDOM.createPortal` to mount at `document.body`, lifting
     the entire backdrop + panel out of any parent stacking
     context. Falls back to in-tree rendering when ReactDOM is
     unavailable (node smoke harness).

  2. *Permission gate on Add task (5.2).* "+ Add task" now only
     renders when `window.FS.can(caller, 'programme:manage')` —
     i.e. project_manager, construction_manager (and above via
     hierarchy) and admin. Site managers and below see the
     programme but no longer the create button.

  3. *Permission gate on Delete (5.3) + visibility.* Same gate
     as 5.2 applied to the editor's `onDelete` prop; the trash
     control disappears completely for non-write roles. The
     button was always there for write roles — it sits in the
     bottom-LEFT of the editor footer — but was previously
     hidden by issue #1's clipping; the portal fix makes it
     visible.

  4. *Date input locale + Assignees/Tags UX.* Added `lang="en-NZ"`
     to the two `<Input type="date">` controls in the editor so
     Chrome/Edge stop showing the placeholder/format in the OS's
     display language (was rendering "yyyy/mm/日" on Chinese
     locale). Replaced the comma-separated string Inputs for
     Assignees and Tags with a new `ChipInput` composite — chips
     render selected values with × to remove, the input
     autocompletes via `<datalist>` from a pool collected from
     all leaves' existing assignees/tags, and Enter or comma
     commits a chip. Free input still allowed (datalist is a
     hint, not a constraint).

  Edit was deliberately left ungated: site_managers can still
  edit existing tasks (consistent with their `programme:view`
  scope), but cannot add or delete. Cache busters: composites.css
  v=28 → v=29, modal-overlay.js v=1 → v=2, programme-task-editor.js
  v=3 → v=4, programme.js v=10 → v=11.

- **Sprint 5.7.2 · Gate Edit too** ✅ done
  Followed up on 5.7.1 by extending the `programme:manage` gate
  to the Edit button itself in `ProgrammeRightDetail`. The three
  write actions (Edit / Add / Delete) now share one symmetric
  permission contract — site_managers and below see the
  programme + can drag-reschedule (drag is gated separately
  inside `gantt-row.js`) but cannot enter the editor at all.
  Cache buster: programme.js v=11 → v=12.

### Deferred to Sprint 6+

| Item | Why deferred |
|---|---|
| Excel `.xlsx` import (SheetJS) | Commercial license caveat; CSV covers the common path |
| MS Project `.mpp` binary | No pure-JS parser exists. Either backend conversion or accept .xml only |
| Reverse linking (action done → programme progress nudge) | Field-test 4.10 first — UX is not yet validated |
| Deep cascade (slack analysis + over-allocation warnings) | Medium covers ~80% of value; deep needs domain rules per org |
| Persistent edits (write-through to backend) | `PATCH /api/programmes/...` doesn't exist yet — currently mock-only |
| Resource pool conflict detection | Needs a separate sub-sprint after deep cascade lands |

## Sprint 6 — Compliance pair (`/safety` + `/quality`) ✅ done

Sprint 5 closed Programme operability (PR #15 merged). Sprint 6 turns
on the next two highest-value nav slots — `/safety` and `/quality` —
which are reserved in `fs-globals.js:339-364` but currently render
the app-shell placeholder. They share fixture surface
(`safety_observations` + `safety_flags` per topic; `quality_and_compliance`
per report; `category: 'safety' | 'quality' | 'progress'` on every
topic), share an audience (site_managers, project_managers, HSE +
Quality specialists), and share a UX shape (cross-day rollup). Built
together so the L5 composites pull double duty rather than two
parallel stacks.

Note on roadmap: CLAUDE.md schedules Sprint 6 as "Mobile + dark mode."
We're swapping that to **compliance pair** per the Sprint 5+ user
decision — the read-only aggregation work fits more naturally on top
of the Sprint 5 patterns (cross-day fan-out, page provider, KPI strip),
while dark mode pairs better with `/settings` in Sprint 7.

### Scope decisions (locked)

| Question | Choice |
|---|---|
| Sprint 6 scope | **Compliance pair only** (Safety + Quality) — Team + Settings deferred |
| Range view | **7-day default + date-picker** on both pages |
| /team scope (Sprint 7) | Read-only directory grouped by site, role badges |
| /settings scope (Sprint 7) | Full prefs (notifications + default landing + density + theme) |

### Constraints carried forward

- No build step (UMD/CDN only).
- Mock-only persistence — both pages are pure read in this sprint.
- No new backend endpoints; aggregator fans out existing
  `getTimeline(date)` over a date range, mirrors `tasks-aggregator.js`.
- No new L4 atoms — every list/form element comes from Sprint 1
  primitives + Sprint 4–5 composites.

### Sub-sprints

- **Sprint 6.0 · Compliance aggregator + fixture audit** ✅ done

  Shipped: new `scripts/api/compliance-aggregator.js` (~290 LoC).
  `getSafetyRange` + `getQualityRange` parallel exports. Internal
  `fanoutDates` mirrors `tasks-aggregator.js:57-87` exactly. Worker
  scope clamping via shared `resolveUser` (intentional parity with
  tasks aggregator). _AUDIT block at top documents four gaps:
  fixture sparsity (only 2026-04-29 has real content), no `status`
  field on safety_observations, hse_manager fan-out parity, and
  field-shape verification. Smoke-tested in node: 5 safety rows
  (2 obs + 3 topic flags, 2 high-risk) + 3 quality rows (2 QC items
  + 1 quality topic, 1 follow-up) — matches fixture totals.

  New pure module `scripts/api/compliance-aggregator.js` (~120 LoC).
  Two parallel call paths: `getSafetyRange({from, to})` and
  `getQualityRange({from, to})`. Both share one underlying
  `fanoutDates()` helper (mirrors `tasks-aggregator.js` exactly:
  `Promise.all` over the date range + flatten). Worker scope clamping
  delegated to `timeline.js` upstream.

  Audit deliverable: confirm every report fixture has
  `safety_observations`, `safety_flags`, `quality_and_compliance`,
  and `category`. Document any gaps in an `_AUDIT` block at the top
  of the aggregator. **No fixture changes** — audit-only.

  Smoke test (node): import the aggregator, feed 2026-04-29 fixture,
  assert each return shape, assert flag counts match
  `dates.fixture.js`'s totals.

- **Sprint 6.1 · `/safety` middle column** ✅ done

  Shipped: new `scripts/pages/safety.js` Provider + Middle (~360 LoC
  for the middle-column slice of the file). Range toolbar with three
  chips (Today / Last 7 days / Pick date), DatePicker dropdown for
  single-day mode. KPI strip: Total flags · High risk (danger
  tone) · Sites affected · Open / closed (warning when any open).
  Date-grouped list with dense `SafetyFlagRow` per item. Header
  always visible so the toolbar stays reachable during loading
  / empty / error states. CSS additions to `composites.css` (~165
  LoC) under `.fs-safety` namespace.

  New `scripts/pages/safety.js` (~280 LoC) + page registry entry.
  Provider holds `{ status, range: 'today'|'week', date|fromTo,
  byDate, totals, selectedFlag }`. Default range: last 7 days that
  have reports (queried via `FS.api.dates.getDates({months: 1})`,
  take top 7).

  Middle column structure:
  - Toolbar: title + range chips (`Today` / `Last 7 days` / date-picker
    chip) — same pattern as `/programme` view-toggle.
  - KPI strip: total flags · high-risk count · sites affected ·
    open vs closed.
  - Body: grouped by date desc, each group is a date header +
    N `safety-flag-row` items (existing composite). Click → set
    `selectedFlag`.
  - Empty / loading / error states reuse the standard pattern.

  New CSS: `.fs-safety-*` block in `composites.css` (~80 LoC).

- **Sprint 6.2 · `/safety` right detail** ✅ done

  Shipped: full inspection panel replaces the 6.1 placeholder
  (~200 LoC added to `safety.js`). Header carries observation as
  title + risk-tone Badge + status Badge. Field rows adapt to the
  source shape — observation rows expose location + raised-by;
  topic_flag rows skip those nulls. Lazy-fetches related action_items
  from the source topic via `FS.api.timeline.getTimeline` and
  surfaces them as click-through chips with text + responsible +
  priority (only for topic_flag source — observation source has no
  topic to lift actions from). 'Open source report' button →
  `/timeline?date=…&user=…`. Close (X) clears `selectedFlag`.
  CSS additions to `composites.css` (~90 LoC) under
  `.fs-safety-detail` namespace. `safety.js` cache buster bumped
  v=1 → v=2.

  Right column renders the selected flag with full context: risk
  badge, observation, recommended action, location, who raised,
  source-report link → `/timeline?date=…&user=…&topic=…`. If
  `linked_action_items` exist, surface them as click-through chips
  (mirrors Programme right-detail's linked actions block from 4.4).
  No new composites — `Card` + `Badge` + `Button`.

- **Sprint 6.3 · `/quality` middle column** ✅ done

  Shipped together with 6.4 (single commit, single file —
  `scripts/pages/quality.js` ~545 LoC). Mirrors safety middle column
  with three intentional deltas: status comes from the fixture, not
  synthesised (statusTone() maps completed/pass → success, concern →
  warning, fail/blocked → danger, observed → info); KPI swaps to
  Total · Follow-up · Sites · Completed; rows render as
  title + details + status badge stack instead of `SafetyFlagRow`
  (quality items don't carry a risk_level so the coloured-border
  treatment doesn't apply).

  Mirrors 6.1 file-for-file. New `scripts/pages/quality.js`
  (~270 LoC). Provider has the same shape but `byDate` flattens
  `quality_and_compliance` items + topics where
  `category === 'quality'`. KPI strip swaps: total items ·
  failing/follow-up · sites affected · resolved this week. List
  rows reuse generic `Card` (quality items shape simpler than
  safety — no new composite needed).

  CSS: `.fs-quality-*` block in `composites.css` (~70 LoC).

- **Sprint 6.4 · `/quality` right detail** ✅ done

  Shipped together with 6.3 in a single commit (combining was
  cleaner — same file, same context, no review value in splitting).
  Same lazy-fetch pattern as 6.2 for related action_items. Skips
  the lookup for report-level qc_items (topic_id = -1). 'Open source
  report' button mirrors safety. CSS additions to `composites.css`
  (~250 LoC) under `.fs-quality` / `.fs-quality-detail`.

  Mirrors 6.2: selected quality item with details, status,
  follow-up flag, source-report link, linked actions.

- **Sprint 6.5 · Wire-up + cache-buster sweep + role walkthrough** ✅ done

  - Cache busters: only the touched files were bumped — `safety.js`
    v=1 → v=2 inside Sprint 6.2; everything else carries fresh v=1
    from first introduction. Existing pages untouched in this sprint
    (their cache busters intentionally not bumped).
  - Reduced-motion audit: `git diff origin/main..HEAD -- styles/`
    shows **0 new `@keyframes`**. Clean by construction — both
    pages reuse existing transition variables (`--duration-fast`,
    `--easing-out`).
  - `node --check` sweep: 92 JS files (was 89 before sprint), all
    pass.
  - Browser walkthrough at five roles **deferred to user** — no
    headless browser in this environment. Expected behaviour
    documented in §Sprint 6 / Scope decisions and codified by the
    nav permission gates (`canSeeNav('safety', user)`,
    `canSeeNav('quality', user)`). Worker/foreman: gated out at
    nav. site_manager: nav visible, list will show their accessible
    site only (clamped via aggregator's resolveUser). hse_manager,
    quality_manager, admin: full visibility.
  - Sprint 6 entries above flipped to ✅ done.

### Sprint 6.6 polish round (post browser-verification feedback)

User ran the merged Sprint 6 in the browser and reported four
issues. All four addressed in a single PR-additive batch:

- **Sprint 6.6.1 · DatePicker `inline` mode** ✅ done

  Bug: clicking the < or > arrows on the 7-day strip in /safety +
  /quality date-picker committed a single-day selection instead of
  sliding the visible window. Fix: extend DatePicker with an
  `inline` prop. When set: skip the strip entirely, render the
  month grid + month-nav header inline (not modal), arrows nav
  months without committing, cell click commits. Timeline page
  unchanged — keeps its original strip + modal flow.

- **Sprint 6.6.2 · /safety KPI Closed/Open swap** ✅ done

  Reorder of the open/closed StatCard. Closed reads first
  (desirable end-state), open second. Tone still keys on
  `totals.open` so colour semantics unchanged.

- **Sprint 6.6.3 · Inline photo carousel** ✅ done

  Eliminates the round-trip to /timeline that users hit when they
  want to see "what does this flag actually look like." Three
  pieces: (a) compliance-aggregator surfaces `related_photos` on
  topic_flag + topic_quality rows; (b) PhotoGrid gains
  `variant='carousel'` (flex + scroll-snap, default 2 cells
  visible at right-panel widths, 4:3 aspect-ratio thumbs);
  (c) safety + quality right panels render <PhotoGrid
  variant='carousel'> below field rows when present. Report-level
  rows (observation, qc_item) skip — no specific topic to lift
  photos from.

- **Sprint 6.6.4 · Deep-link to topic + focus mode + flash** ✅ done

  "Open source report" now appends `&topic=N`, timeline reads it
  and force-opens just the target topic (others auto-collapse =
  focus mode), scrolls the topic into view, runs a 3-pulse accent
  flash. TopicCard gained `highlight` and made `defaultOpen`
  reactive to prop changes (was: read once at mount); also wrapped
  in a transparent <div> for ref / scrollIntoView since L4 Card
  doesn't forwardRef. New @keyframes `fs-topic-card-flash`
  (1800ms, ease-out, accent-100 pulse). prefers-reduced-motion
  fallback: animation:none + steady accent background — affordance
  preserved.

  Sprint 6 audit note: Sprint 6.0–6.5 was 0 new @keyframes; 6.6.4
  adds 1. Acceptable — explicit user-requested UX with motion
  fallback in place.

### Sprint 6.7 polish round (post-6.6 verification feedback)

After the 6.6 round shipped, user identified two more refinements:

- **Sprint 6.7.1 · Action checkbox sync (middle ↔ right)** ✅ done

  Bug: same action_item rendered twice on /timeline (middle
  TopicCard + right OverviewTab) didn't sync — toggling one didn't
  strike-through the other. Each ActionItemRow held its own local
  React state seeded from a parent state slot that never crossed
  the middle/right boundary.

  Fix: tiny pub/sub bus. New `scripts/api/actions-bus.js` (~30
  LoC) exposes `window.FS.actionsBus.{ emit, subscribe }`. No
  React context, no AppShell prop drilling. ActionItemRow:
  (a) syncs local state when `initialChecked` prop changes
  (skipped while pendingRef set, so no clobber of in-flight
  optimistic updates), (b) subscribes to bus, on matching key
  syncs to server-truth payload, (c) emits on toggleAction
  success. timeline.js MiddleColumn + RightDetail both subscribe
  to mirror events into their own state slots so subsequent
  re-mounts see fresh data.

- **Sprint 6.7.2 · Precision spotlight — flag-level highlight** ✅ done

  Refines 6.6.4: when /safety opens a `topic_flag` row, the
  spotlight now lands on the exact flag inside that topic's
  `safety_flags[]` (not the whole topic card). Solves "topic has
  3 flags, which one was I looking at?" ambiguity.

  Five-piece change: SafetyFlagRow gains `highlight` prop with
  the same scrollIntoView + flash treatment; TopicCard gains
  `flagHighlight` (number index) prop that drills into one
  SafetyFlagRow; CSS adds `.fs-safety-flag-row--flash` selector
  to the existing 6.6.4 keyframes (no new keyframes); timeline.js
  parses `&flag=<idx>` from URL; safety.js extracts flag idx from
  `sel.id` via `/_flag_(\d+)$/` regex (verified against all 6 row
  id shapes — only topic_flag matches). Topic-quality +
  observation + qc_item keep 6.6.4 / no-anchor behaviour.

### Critical files

| Path | Role | Status |
|---|---|---|
| `scripts/api/compliance-aggregator.js` | Cross-day fan-out for safety + quality | NEW (6.0) |
| `scripts/pages/safety.js` | Provider + Middle + Right | NEW (6.1, 6.2) |
| `scripts/pages/quality.js` | Provider + Middle + Right | NEW (6.3, 6.4) |
| `app-shell-preview.html` | Script tag registrations + cache-buster bumps | MODIFIED each sub-sprint |
| `styles/composites.css` | New `.fs-safety-*` and `.fs-quality-*` blocks | MODIFIED |
| `scripts/api/tasks-aggregator.js` | Reference pattern | READ-ONLY |
| `scripts/composites/{category-badge,safety-flag-row,kpi-strip,stat-card,card,badge,date-picker}.js` | All reused as-is | READ-ONLY |
| `scripts/api/{timeline,dates}.js` | Underlying fetchers | READ-ONLY |

### Deferred to Sprint 7+

| Item | Why deferred |
|---|---|
| `/team` (read-only directory) | Narrower audience (gm + director only via `user:manage`) |
| `/settings` (full prefs + theme + density) | Pairs naturally with dark-mode work |
| Safety/quality write actions (raise flag, mark resolved) | No backend `POST /api/safety` exists; mock-only-mutation lesson from Sprint 5 |
| Trend/heatmap views (flags-per-site over time) | Needs >7 day fixture data; revisit when backend lands |
| Vocabulary-aware tagging (Q-2 backend) | Already in §Q-2; not blocking Sprint 6 |

## Sprint 7 — Team + Settings + Dark mode 🟡 active

### Context

Sprint 6 (compliance pair) is merged via PR #16 with two polish
rounds (6.6 + 6.7). The remaining nav slots from CLAUDE.md's
original Sprint 6 charter — `/team` and `/settings` plus the dark
mode polish — are next up. Per user decision Sprint 7 deliberately
**does not include mobile / responsive** work; that's a
cross-cutting effort touching every page and earns its own Sprint 8.

Why these three together: they're tightly coupled by a single
piece of new infrastructure — the `/settings` page is where the
dark-mode toggle lives, and dark mode itself needs the toggle UI
to be useful. `/team` is largely independent but ships in the same
sprint because it's small (data fixture and patterns all exist)
and it closes the last open nav slot from Sprint 6's nav charter.

### User decisions captured

| Question | Choice |
|---|---|
| Sprint 7 scope | `/team` + `/settings` + dark mode (mobile → Sprint 8) |
| `/settings` v1 widgets | Default landing override + Theme toggle |
| `/team` right detail | Profile + cross-page link buttons |
| Theme toggle UI lives in | `/settings` page |
| Dark mode polish strategy | Wire toggle + one-shot audit across all pages |

### Strategy

**6 sub-sprints, single branch** following the Sprint 6 cadence.
The order is: theme infrastructure first (unblocks 7.3 + 7.4),
then `/team` (independent), then `/settings` (consumes theme
infra), then dark-mode audit across existing pages, then wrap-up.

Critical invariants:
- **No build step** (UMD/CDN only).
- **localStorage only for prefs** — no backend prefs API yet.
  `defaultLanding` override + theme persist client-side; documented
  as Sprint 8+ migration target when a real `/api/user/prefs`
  lands.
- **No new L4 atoms** — every input/control comes from Sprint 1
  primitives + Sprint 4–6 composites.
- **Reuse, don't rebuild** — the existing dev `setTheme()` in
  `tokens-reference.js:495-515` is the model for the new theme
  module; `/sites` page is the architectural template for
  `/team`; `/safety` is the template for `/settings` (range
  toolbar pattern → settings sections).

### 6 sub-sprints

#### 7.0 — Theme infrastructure + app-shell wiring

New `scripts/theme.js` (~80 LoC). Public API:

```js
window.FS.theme = {
  init()              // call once at app boot
  set(mode)           // 'light' | 'dark' | 'auto'
  get()               // → resolved mode (auto → resolved current)
  getStored()         // → stored preference verbatim
}
```

Behaviour:
- `init()` reads `localStorage.fs.settings.theme` (default `'auto'`),
  applies `data-theme` attribute on `<html>`. In auto mode,
  consults `prefers-color-scheme` and listens for changes via
  `matchMedia(...).addEventListener('change')`.
- `set(mode)` writes localStorage, applies `data-theme`,
  re-evaluates auto if relevant.
- AppShell (`scripts/app-shell.js`) calls `FS.theme.init()` once
  after mount.
- Add `theme` to existing `STORAGE_KEYS` in `app-shell.js` for
  consistency.

Reference: `scripts/tokens-reference.js:495-515` already does this
for the demo page — extract + generalise. No need to remove the
demo wiring; the new module supersedes it everywhere else.

#### 7.1 — `/team` middle column

New `scripts/pages/team.js` (~280 LoC). Provider state:
`{ status, usersBySite, totals }`. Fetches via existing
`FS.api.sites.getUsers()` (no aggregation needed — single fetch).

Middle column structure:
- Header: title "Team" + meta line (N users · M sites)
- KPI strip: total users · active sites · roles represented
  (count of distinct roles)
- Body: grouped by `primary_site` (descending by user count),
  each group is a site header + N user rows. Each row:
  `Avatar` (initials variant, deterministic colour) + name +
  role badge + secondary sites pill (if `user.sites.length > 1`)

Permission gate (defense-in-depth): Provider checks
`FS.can(caller, 'user:manage')` and surfaces AccessDenied if
false. Nav already gates this, but a direct URL hit could land
unauthorised users on the page.

CSS: append `.fs-team-*` block to `composites.css` (~70 LoC,
mirrors `.fs-safety` group structure).

#### 7.2 — `/team` right detail + `/tasks?user=` extension

Right column shows the selected user's profile:
- Header: large Avatar + name + role badge + scope pill
  (e.g., "Site Manager · Ellesmere College")
- Field rows: Primary site · All sites · Device ID
- Footer: two action buttons —
  - "View their reports" → `/timeline?date=<today>&user=<folder>`
    (timeline page already reads `params.user`,
    `scripts/pages/timeline.js:241`)
  - "View their tasks" → `/tasks?user=<folder>`

Tasks-side wiring (~15 LoC in `scripts/pages/tasks.js`):
- Add `readRouteParams()` helper (mirror timeline.js:37-40)
- TasksProvider reads `params.user` and passes it to
  `FS.api.tasks.getActionsResolvedRange({user})`
- `resolveUser` in `tasks-aggregator.js:46-55` already accepts
  explicit user — no aggregator change

#### 7.3 — `/settings` page ✅ done

New `scripts/pages/settings.js` (~230 LoC). Provider holds prefs
state, syncs to localStorage on change.

Middle column has two sections:

1. **Theme** — Light / Dark / Auto radio group.
   - Calls `FS.theme.set(mode)` on change → instant visual
     feedback
   - "Auto" option captioned with current resolved mode
     (e.g., "Auto · matches your system, currently dark")

2. **Default landing** — Dropdown of nav items the current user
   can see (uses existing `FS.getVisibleNavItems(user)`).
   - First option: "Use my role's default
     (`<role.defaultLanding>`)" → unsets the override
   - Persists to `localStorage.fs.settings.defaultLanding`
   - Bootstrap block in `app-shell-preview.html` extended: on
     initial navigation (when route is empty), prefers the stored
     override over `FS.getDefaultLanding(user)`. ~5 LoC.

Right column: static "Your preferences" summary card showing
theme preference, resolved theme, and effective landing page.

CSS: `.fs-settings-*` + `.fs-settings-summary` appended to
`composites.css` (~160 LoC, sections layout + form rows +
summary card + reduced-motion block).

`app-shell-preview.html` updated: `settings.js?v=1` registered,
`composites.css?v=31`, `app-shell.css?v=4` cache-busted.

#### 7.4 — Dark mode polish audit (per-page) ✅ done

Audit method (grep-based, no headless browser):
1. Grepped `composites.css` + `components.css` + `app-shell.css`
   for `#[0-9a-f]{3,6}`, `rgb(`, `rgba(`.
2. Grepped inline `style=` with colour values in
   `scripts/composites/` and `scripts/pages/` — no hits.
3. For each find: replaced with `var(--*)` token, or categorised
   and documented below.

**Fixed:**
- `composites.css` `.fs-modal__backdrop` — `rgba(15, 23, 42, 0.5)`
  → `var(--surface-overlay)` (uses the dark-aware token).
- `app-shell.css` `.fs-right-drawer__backdrop` — `rgba(15, 23, 42, 0.4)`
  → `var(--surface-overlay)`.
- `app-shell.css` `.fs-dev-switcher select option` — `#1a1a1a` / `#fff`
  → `var(--surface-tooltip)` / `var(--text-primary)`.

**Intentional / kept as-is:**
- `color: #fff` on solid badge + button variants — white text on
  coloured backgrounds; correct in both modes.
- `background: #000` on `.fs-video-player__media` — video
  letterbox; intentional.
- `rgba(0, 0, 0, ...)` shadows and overlays (gantt bar progress,
  kanban card hover, dragging) — alpha-transparent, theme-neutral.
- `rgba(255, 255, 255, 0.06)` sidebar borders in `app-shell.css`
  — sidebar background is always dark navy (`--surface-sidebar`),
  so translucent white borders are correct in both themes.
- `rgba(255, 107, 53, ...)` accent tints — accent colour is fixed
  across themes; no change needed.

**Skipped / deferred to Sprint 8:**
- `scripts/composites/transcript-list.js` speaker palette
  (`#1E40AF/#DBEAFE`, `#15803D/#DCFCE7`, etc.) — no design-system
  token set for arbitrary speaker slots; needs Sprint 8 token
  additions (`--color-speaker-{1..6}-{fg,bg}`).

#### 7.5 — Wrap-up ✅ done

- Cache busters bumped: `composites.css?v=31`, `app-shell.css?v=4`,
  `settings.js?v=1` (new file).
- `node --check` sweep across all ~95 JS files — zero errors.
- Reduced-motion audit: two new `@media (prefers-reduced-motion)`
  blocks added in `composites.css` (one for `.fs-settings__radio-row`
  + `.fs-settings__select` transitions).
- No new keyframe animations introduced in Sprint 7.3–7.5.
- Browser walkthrough deferred to user — note role expectations:
  - **worker** — no /team in nav, /settings accessible, theme
    toggle works.
  - **site_manager** — same as worker.
  - **gm** / **admin** — /team visible in nav, /settings shows
    all nav items in the landing dropdown.

### Recommended execution order

`7.0 → 7.1 → 7.2 → 7.3 → 7.4 → 7.5`

7.0 must come first (7.3 + 7.4 depend on the theme module). 7.1
and 7.2 can swap places without consequence but conventionally
"middle then right" matches Sprint 6 cadence. 7.4 must come last
of the build phase (audits the work of 7.1–7.3 plus all earlier
sprints).

### Critical files

| Path | Role | New / Modified |
|---|---|---|
| `scripts/theme.js` | Theme apply + persist + auto-mode listener | NEW (7.0) |
| `scripts/app-shell.js` | Add `FS.theme.init()` call + `STORAGE_KEYS.theme` + defaultLanding override hook | MODIFIED (7.0, 7.3) |
| `scripts/pages/team.js` | Provider + Middle + Right | NEW (7.1, 7.2) |
| `scripts/pages/settings.js` | Provider + Middle + Right | NEW (7.3) |
| `scripts/pages/tasks.js` | Read `?user=` from URL | MODIFIED (7.2) |
| `app-shell-preview.html` | Script registrations + cache-buster bumps | MODIFIED each sub-sprint |
| `styles/composites.css` | New `.fs-team-*` and `.fs-settings-*` blocks + dark-mode fixes | MODIFIED |
| `styles/components.css` | Possible dark-mode fixes for L4 atoms | MODIFIED (7.4) |
| `styles/app-shell.css` | Possible dark-mode fixes | MODIFIED (7.4) |
| `scripts/mock/sites.fixture.js` | Existing 8-user fixture | READ-ONLY |
| `scripts/api/sites.js` | `getUsers()` / `getSiteUsers()` | READ-ONLY |
| `scripts/components/avatar.js` | Used as-is for /team + /settings header | READ-ONLY |
| `scripts/composites/{kpi-strip,stat-card,card,badge}.js` | Reused | READ-ONLY |
| `scripts/roles.js` + `scripts/fs-globals.js` | `getVisibleNavItems`, `getDefaultLanding`, `can` | READ-ONLY |
| `styles/tokens.css:564-618` | Dark token set; verify completeness during 7.4 | READ-ONLY |
| `scripts/tokens-reference.js:495-515` | Reference implementation for theme persist | READ-ONLY |
| `PLAN.md` | Status entries flipped to ✅ as each sub-sprint lands | MODIFIED |

### Verification

Per sub-sprint, before commit:
1. `for f in $(find scripts -name '*.js'); do node --check "$f" || exit 1; done` — must remain clean.
2. Smoke test for 7.0: in node, run `theme.js` against a stubbed
   `localStorage` + `matchMedia`, verify init / set / auto behaviour.
3. Cache busters bumped only on touched files.

After 7.5 lands:
4. Browser walkthrough at 4 roles — verify nav gating and
   per-role behaviour. Log any dark-mode regression as a Sprint
   8 follow-up.
5. No regressions on the 11 existing pages from Sprints 4–6.

### Branch + PR strategy

Single branch `claude/sprint7-team-settings-dark`, all sub-sprints
stacked. PR title rolls forward at each push. Final PR rename
happens at 7.5 close. Same convention as Sprint 5 + Sprint 6.

### Deferred to Sprint 8+

| Item | Why deferred |
|---|---|
| Mobile / responsive | Cross-cutting work touching every page; deserves its own sprint |
| User profile editing (email, phone) | AuthMock doesn't have these fields; no `PATCH /api/users/me` endpoint |
| Notification preferences (real) | No toast/snack composite + no `/api/notifications` endpoint |
| Density toggle (Comfortable / Compact) | Requires building density CSS variables + reworking spacing tokens; sizable own sprint |
| `/team` write actions (invite user, deactivate) | Read-only by user decision; needs `POST /api/users` + audit |
| `/settings` backend persistence | All prefs land in localStorage; migrate when `/api/user/prefs` exists |
| Per-user defaultLanding stored server-side | Currently localStorage; Sprint 8 if backend prefs ship |
| Sprint 7 audit for any token additions | If 7.4 finds gaps that need new tokens, those are Sprint 8 micro-sprints |

### Range estimate

| Bracket | Sub-sprints | What ships |
|---|---|---|
| Minimum | 7.0, 7.1, 7.3 | Theme + /team middle only + /settings. 3 commits. Punts /team right detail and dark audit. |
| **Nominal (chosen)** | 7.0–7.5 | Full /team + /settings + dark audit. 6 commits. |
| Maximum | + 7.6 density toggle + 7.7 notification stub | Adds half-baked features that don't fully work without backend. Not chosen. |

Estimated total: **~5 working days** at sub-sprint-per-day pace.
7.4's audit is the wild card — could be 1 day if existing pages
are clean, 2 days if there's significant hardcoded-colour debt.

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
