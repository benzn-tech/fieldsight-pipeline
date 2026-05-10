# Accessibility — Manual Test Checklists

This document is the contract between the codebase and a human tester
running NVDA (Windows / Firefox) or VoiceOver (macOS / Safari) over
the FieldSight prototype. Code-level fixes from automated tooling
(axe-core via `?axe=1`) cover most WCAG 2.1 AA violations; the items
below need a real screen reader because they're about how the
*announcements* land in the user's ear, not what's in the DOM.

## Setup

1. Open `app-shell-preview.html?dev=1` (role switcher visible)
2. Switch to **NVDA** on Windows (Firefox preferred) or **VoiceOver**
   on macOS (Safari preferred)
3. Enable browser zoom 125% — most issues are invisible at 100%
4. Enable "Speak hovered text" on VoiceOver / "Speech viewer" on NVDA
   so you can scroll back to verify what was announced

If running an axe-core pass alongside, also append `?axe=1` —
violations log to the browser console under a collapsed `[axe]` group
on every route change.

---

## `/today` — 6 checks

| # | Action | Expected announcement |
|---|---|---|
| **1** | Tab into the page from the URL bar | Should land on the **Skip to main content** link first; pressing Enter should jump focus into the middle column body. |
| **2** | Arrow-down through the morning brief bullets | Each bullet announced as a separate list item, ordinal included ("item 2 of 5"). |
| **3** | Tab to the first **Urgent card** | Announced as button with the urgent item's title + risk level (e.g., "Loose scaffold board on level 2 of Block C, button, high risk"). |
| **4** | Press Enter on the urgent card | Right detail focus management — the right-detail heading should be announced ("Loose scaffold board on level 2 of Block C, heading"). NVDA may say "selection cleared" first; ignore. |
| **5** | Tab to a **TaskCard**, press Space | Action item check toggle should announce its new state — **"Marked complete"** via the live-region (`#fs-live-region` polite announce). Verify the announcement isn't doubled (would mean the label and live-region both fire). |
| **6** | Press `?` (no field focused) | Shortcut reference modal opens, focus jumps inside, ESC returns focus to body. Verify modal heading is read aloud. |

## `/programme` — 4 checks

| # | Action | Expected announcement |
|---|---|---|
| **1** | Tab into the Gantt timeline | First Gantt bar announced as `slider`, with `aria-valuemin / aria-valuemax / aria-valuenow` mapped to start day. Should read "task title, slider, value 12 of 60". |
| **2** | ArrowRight on a focused Gantt bar | Announces the new value-now (e.g., "13 of 60") and the bar visually moves 1 day. Shift+ArrowRight should change the *valuemax* (extend end) — re-announced. |
| **3** | Tab into the **Board** view (toggle from header) | Each kanban column announced as `list`, each card as `listitem`. Card label = task title + priority badge. |
| **4** | Open `ProgrammeTaskEditor` (Enter on a Gantt bar) | Modal: focus traps, `aria-modal=true`, header h2 announced. ESC closes + returns focus to the Gantt bar that triggered it. |

---

## Common pitfalls (file these as bugs if encountered)

- **Live-region announcements doubling**: if both the button label
  and the `#fs-live-region` text fire on the same action, the user
  hears the same thing twice. Either remove the live-region call or
  silence the button label via `aria-label="" tabIndex="-1"` after
  click. (Sprint 8.5.4 wired this; regressions possible.)
- **Drag-and-drop on Gantt bars** (Sprint 5) is keyboard-only via
  ArrowKeys; mouse-drag has no SR equivalent. That's by design — the
  ARIA-slider pattern is the canonical replacement.
- **Photo carousel** (Sprint 6.6.3): each thumbnail must be a button
  with `aria-label="Photo N of M, taken at HH:MM"`. If announcement
  is just "image" with no context, that's a regression.
- **Toast notifications**: `role="status"` + `aria-live="polite"` on
  the toast container (Sprint 8.5.4). Verify a single toast is
  announced once, not on every re-render.

---

## Reporting bugs

Tag any failure with the WCAG criterion. Common ones:

| Criterion | Common manifestations |
|---|---|
| 1.3.1 Info and Relationships | Heading levels skip, list/listitem missing |
| 1.4.3 Contrast (minimum) | Should be caught by axe-core; SR can't verify |
| 2.1.1 Keyboard | Anything mouse-only without keyboard alternative |
| 2.4.3 Focus Order | Tab order doesn't match visual / logical reading order |
| 2.4.6 Headings and Labels | Button names too generic ("Click here") |
| 3.3.2 Labels or Instructions | Form fields missing `<label>` association |
| 4.1.2 Name, Role, Value | Custom widgets (Gantt slider, kanban) missing ARIA |
| 4.1.3 Status Messages | Live-region issues — see "doubling" pitfall above |

File reports in this repo's GitHub issues with the route, browser+SR
combo, expected announcement, actual announcement, and a quick
audio capture if possible.
