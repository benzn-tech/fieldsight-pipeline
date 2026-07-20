# Deadline Normalization & Clean Calendar Fields — Design

**Date:** 2026-07-21
**Repos:** `fieldsight-pipeline` (backend), `fieldsight-ui` (frontend)
**Status:** Approved design, ready for implementation plan

## Problem

Action-item deadlines reach the UI as free text (`"Tomorrow 08:00"`, `"By Friday"`,
`"EOD"`, `"ASAP"`), not as clean calendar dates. The new inline edit feature
(`fs.DateField`) assumes an ISO `YYYY-MM-DD` value; feeding it free text crashes the
whole app. We need every `create date` / `due` the UI touches to be a clean calendar
field, plus a backfill so existing stored rows match the new contract.

### Why it happens (verified, not assumed)

The ISO filter already exists — the problem is elsewhere:

- **A strict ISO gate is already in place.** `src/lambda_ingest.py:105`
  `_ISO_DATE_RE = ^\d{4}-\d{2}-\d{2}$`; `_map_action_items()`
  (`src/lambda_ingest.py:207-229`) drops any non-ISO value to `NULL` in the typed
  `deadline` column while preserving the raw string in `deadline_text`.
  `src/lambda_item_writer.py:268` imports and reuses this same function — one filter,
  two paths.
- **Upstream prompts actively manufacture dirty values.**
  `src/lambda_report_generator.py:183` instructs the model to emit
  `'Tomorrow 08:00'`, `'EOD'`; `src/lambda_meeting_minutes.py:113` mixes ISO and
  `ASAP` in one field's examples; `src/lambda_extract_session.py:153` leaves
  `deadline` unconstrained. None of the three tell the model today's date, so the
  model can only produce relative words.
- **The read path prefers the dirty value.** `src/lambda_org_api.py:1709` serves
  `deadline_text or str(deadline)` — free text wins over a perfectly good typed date.
  Confirmed by `tests/unit/test_lambda_org_api.py:2309-2317`: a row with both
  `deadline_text="Tomorrow 8am"` and `deadline="2026-07-15"` renders as the text.
- **The KPI silently undercounts.** `src/repositories/rollup.py:103`
  `overdue_actions` filters on `deadline IS NOT NULL`, so every non-ISO deadline is
  invisible to the overdue count.
- **The UI crash is a truthy-Invalid-Date bug.** `scripts/pages/tasks.js:890` feeds
  raw `row.deadline` into `DateField`; `scripts/composites/date-picker.js:66`
  `parseISO` returns an **Invalid Date object (truthy)**, so the `|| new Date()`
  fallback at `:303` never fires → `:225` `d.toISOString()` throws
  `RangeError: Invalid time value`. There is no error boundary in `scripts/`, so the
  whole React tree unmounts (blank app). Read-only render is safe because it goes
  through `resolveDeadline`.

### Corrections to the original framing

- `fieldsight-pipeline` `develop` is **75 commits behind** `main`, leads by only 2
  docs-only commits — no date work lives there. The date work is on `main`.
- `fieldsight-ui` has **no `develop` branch**; the integration branch is `dev`, where
  the `DateField` edit feature landed.
- `"approx."` was **not found** in any date context in fixtures or source. Attested
  dirty values: `Tomorrow 8am`, `By Friday`, `EOD`, `EOW`, `ASAP`, `Next week`.
  Treat `approx.` as an unmodeled class — a dry-run over real production data
  precedes any backfill assumption.

## Design decisions (approved)

1. **Backfill aggressiveness: aggressive.** Anchor on `topics.report_date` (a
   `date NOT NULL` column, `src/migrations/0003_dashboard_readmodel.sql:6`).
   Deterministic relative words resolve by rule; fuzzy values also get a date
   (`ASAP`/`EOD` → report_date; `approx. <month>` → last day of that month;
   `Next week` → report_date + 7). Unclassifiable → `NULL`.
2. **`deadline_text` column: keep, stop reading and stop writing to the read path.**
   Not dropped. Because it is retained, every computed value is traceable and the
   backfill is re-runnable — aggressive inference is therefore not an irreversible bet.

**Known consequence to flag to stakeholders:** resolving `ASAP → report_date` flips
historical ASAP items to overdue, so `overdue_actions` (`rollup.py:103`) will jump.
Semantically correct, but a visible KPI shift — announce before running the backfill.

## Components

### A. `src/deadline_normalizer.py` — pure normalization function

Single responsibility, the system's **only** date-parsing point.

```
normalize_deadline(text: str | None, report_date: date) -> date | None
```

Table-driven, zero I/O, zero network, never raises. Three layers:

1. **ISO passthrough** — `^\d{4}-\d{2}-\d{2}$` → parse and return.
2. **Deterministic relative** — `Today`, `Tomorrow`, `By <weekday>`, `EOW`,
   `Next week`, weekday names. Resolved against `report_date`.
3. **Aggressive inference** — `ASAP`/`EOD` → `report_date`;
   `approx. <month>` / bare `<month>` → last day of that month (year inferred
   forward from `report_date`); other bounded guesses as the rule table grows.

Anything unmatched → `None`. Case-insensitive, tolerant of trailing time-of-day
(`"Tomorrow 08:00"` strips the `08:00`).

Fully unit-tested against a fixture table of every attested dirty value plus `None`
and already-ISO inputs.

### B. Wire into extraction (one edit, both paths)

`src/lambda_ingest.py:207` `_map_action_items()`: replace the inline `_ISO_DATE_RE`
check with a call to `deadline_normalizer.normalize_deadline(raw, report_date)`.
Because `src/lambda_item_writer.py:268` reuses this function, the real-time
extraction path and the nightly report path both get the new behavior from one edit.

`deadline_text` continues to be written verbatim (write-through preserved for
traceability; only the *read* path stops consuming it — see D).

### C. Constrain the prompts to ISO-only

Three prompts — `src/lambda_report_generator.py:183`,
`src/lambda_meeting_minutes.py:113`, `src/lambda_extract_session.py:153`:

- **Inject the report date** into the prompt context.
- Require `deadline` to be `YYYY-MM-DD` or `null` — remove the `'Tomorrow 08:00'` /
  `EOD` / `ASAP` example values that currently *teach* dirty output.

Layer B remains as the safety net; we do not assume the model complies 100%.

### D. Flip the read path + fix PATCH

- `src/lambda_org_api.py:1709`: `deadline_text or str(deadline)` → serve the typed
  `deadline` only (`YYYY-MM-DD` or `null`). The UI now only ever receives a clean
  calendar field or null.
- `src/lambda_org_api.py:1126-1127`: the PATCH endpoint currently overwrites
  `deadline_text` with the ISO value, destroying the original wording. Change it to
  write the `deadline` column only, leaving `deadline_text` untouched.

The PATCH fix is what makes the backfill able to reuse the non-VPC pattern (E)
without a separate in-VPC DB writer.

Update `tests/unit/test_lambda_org_api.py:2309-2317` to assert the typed date now
wins.

### E. `src/backfill_deadlines.py` — existing-data backfill

Mirrors the house pattern in `src/backfill_site_coords.py`:

- Pure planner `plan_deadline_backfill(rows, ...)` — fully unit-tested, no I/O,
  calls `deadline_normalizer.normalize_deadline` per row.
- Thin `run_backfill(...)` with I/O edges injected
  (`fetch_rows_fn` / `persist_fn`).
- **Deployed non-VPC**, persisting via `PATCH /api/org/action-items/{id}` (org-api),
  avoiding the BUG-36 in-VPC-vs-outbound trap. Enabled by the D PATCH fix.

Scope: rows where `deadline IS NULL AND deadline_text IS NOT NULL`. Anchor each row
on its `topics.report_date`.

**Dry-run first (mandatory):** produce a report — per-rule hit counts, count of rows
that stay `NULL`, and the top-N unclassified raw strings by frequency — for human
review before any write. This is also where we learn whether `approx.` actually
exists in production data and how large each dirty class is.

### F. UI hardening (frontend, ship first, independent of backend)

Even after D cleans the API, the truthy-Invalid-Date white-screen must not be
guarded only by upstream cleanliness. In `fieldsight-ui`:

- `scripts/composites/date-picker.js:66` `parseISO` → return `null` when
  `isNaN(d.getTime())`, so the existing `|| new Date()` fallbacks work.
- `scripts/composites/date-field.js` — validate `^\d{4}-\d{2}-\d{2}$` before
  assigning `value` / `anchorDate`; fall back to today (or empty) otherwise.

This localizes any future bad value to the widget instead of unmounting the app.

## Sequencing

1. **F** (UI hardening) — smallest change, kills the white-screen immediately,
   independent of backend deploy.
2. **A + B** — normalizer + extraction wiring; new writes land clean.
3. **C** — prompts stop manufacturing dirty values at the source.
4. **D** — flip read path + PATCH fix; UI receives clean fields.
5. **E** — dry-run, human review, then backfill existing rows.

## Testing

- **A:** unit table over every attested dirty value + ISO + `None`, asserting exact
  resolved dates against a fixed `report_date`.
- **B:** `_map_action_items` unit test — dirty in, ISO-or-null out; `deadline_text`
  still written.
- **C:** prompt-snapshot / contract tests asserting report date present and no dirty
  example strings remain.
- **D:** update `test_lambda_org_api.py:2309-2317` (typed date wins); PATCH test
  asserts `deadline_text` untouched.
- **E:** planner unit tests (pure); dry-run report reviewed before the write step.
- **F:** `parseISO` returns `null` on invalid; `DateField` never passes non-ISO to
  `DatePicker` (guards the `RangeError` path).

## Out of scope

- Dropping the `deadline_text` column (deferred to a later iteration once the backfill
  is proven stable).
- LLM second-pass inference for unclassifiable values (aggressive rule table is the
  agreed ceiling; unmatched stays `NULL` for human edit).
- Time-of-day precision — deadlines are calendar dates only.
