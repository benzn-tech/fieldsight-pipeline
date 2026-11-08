# Track L Task 7 Step 1: a Where column in the confirmation email (backend half, flag-gated)

Date: 2026-11-09. Plan: `docs/superpowers/plans/2026-09-25-track-l-photos-and-topics-by-location.md`, Task 7 Step 1
(deferred 2026-11-06; the owner said "push everything remaining" on 2026-11-09).

## Constraint that shapes everything

The table header is literal and must be identical on BOTH surfaces:
- the confirmation email (`lambda_session_finalize.build_confirmation_email`, text + HTML);
- the frontend's Preview & copy (`fieldsight-ui/scripts/composites/email-preview-modal.js`).

The rule comes from the owner, 2026-10-08. The UI is owned by the UI line. So this change:
- ships the backend half behind a flag that is **off in both TEST and prod**;
- hands the UI line a contract;
- flips the flag only when the UI half lands. Flipping it on TEST is my call; on prod it is
  the owner's.

## Behaviour (flag on)

1. **Source of places.** The finalize request may carry `locationMarkers`. These are the day's
   markers for (company, folder, date), as `[{at, location, at_s?}]`.
   - The writer that enqueues the `final` request is `lambda_item_writer`, which is in the VPC and
     already imports `repositories.location_markers`. It attaches them from
     `location_markers.for_day`. This is fail-open: on any error the key is absent.
   - Requests of other kinds (rolling, updated, the non-rendered kinds that re-summarise) carry no
     markers, so their email is unchanged.
2. **A row's place.** A row's `where` is `location_locate.locate(markers, row["at"])`. That is the
   same locator the day view uses, with the same 30-minute carry.
   - A row with no `at`, or with no place, has `where = None`.
   - The place is never invented.
3. **When the column appears.** Only when at least one row has a place.
   - Header text: `AGENDA ITEM / WHERE / ASSIGNED / DUE DATE`.
   - Cell text: the marker's label as spoken, whitespace-collapsed.
   - A row without a place shows an empty cell. That is not "—", which means unassigned, and not
     "N/A", which means not a task.
   - With no placed rows, the email is byte-identical to today's.
4. **Plain text and HTML agree.**
   - The pipe table gains a column: `| --- | --- | --- | --- |`.
   - The HTML table gains a `<th>` after AGENDA ITEM. AGENDA ITEM goes from 60% to 50% width.
5. **Flag.**
   - Env var `EMAIL_WHERE_COLUMN`, read at call time; `"true"` enables it.
   - `template.yaml` parameter `EmailWhereColumn`, on `SessionFinalizeFunction` only.
   - `deploy.yml`: `${{ vars.TEST_EMAIL_WHERE_COLUMN || 'false' }}`.
   - `deploy-prod.yml`: `${{ vars.PROD_EMAIL_WHERE_COLUMN || 'false' }}`.
   - The item-writer attaches markers regardless of the flag. The key is additive and ignored when
     the flag is off.
6. **Non-VPC worker.** `lambda_session_finalize` must not import psycopg. It imports only the pure
   locator (`src/location_locate.py`, created by the Task 6 branch). If that branch has not merged
   first, rebase on it.

## Review changes (Fable, 2026-11-09) — override the text above where they differ

1. **Rows need a time (was the blocking gap).** On the path prod runs, no row has `at`:
   - `lambda_item_writer._final_email_rows` and `_topic_rows` emit only `text/responsible/due`;
   - only brief action rows (SESSION_BRIEF, TEST) carry `at` ("HH:MM:SS").

   Fix: give each extraction-built row `at` = the start of its topic's `time_range` ("HH:MM"), in both
   builders. `locate` accepts HH:MM and HH:MM:SS. Sunk brief rows have no `at`, so on TEST a mixed table
   (some rows placed, some empty) is expected.
2. **Markers are attached at the item-writer's call site** (~line 1816, inside the `with conn` block,
   after `replace_for_session`, so the session's own markers are visible). Use
   `location_markers.for_day(conn, owner_company_id, user_folder, date)`, with the real company, never
   None. Attach them as `ctx["locationMarkers"]`, fail-open. Do NOT attach inside `_final_email_context`.
3. **Where the place is computed:** after `_clean_todos` (which keeps `at`), in `build_confirmation_email`,
   via a new keyword argument `location_markers=None`. The caller at ~1080 passes
   `artifact.get("locationMarkers")`.
4. **`src/location_locate.py`** carries `locate` + `_seconds` + `CARRY_MINUTES`. It is created on the Task 6
   branch; this branch rebases onto it.
5. **Carry boundary:** the rule is `> 30` minutes, so the "too old" test uses 31 minutes. Exactly 30 is
   still placed.
6. **Tests to add:**
   - a flag-on house-style test: the WHERE `<th>` is navy with white text, like the other three;
   - a new psycopg-blocked import fixture, modelled on `test_writer_needs_no_numpy`.

   The existing parity tests stay unchanged for flag off.
7. **Flag plumbing** follows `EnableSessionBrief` (template, SessionFinalizeFunction env, deploy.yml, deploy-prod.yml).
   `test_template_workflow_parameter_wiring` must pass.

## Contract for the UI line (Preview & copy)

- The column appears iff at least one row has a place.
- Header: `WHERE`, second column.
- Cell: the place label, or empty.
- The same `cell()` escaping applies.
- Places come from the same day markers. The UI reads them via the timeline's `location_groups`
  (rows whose `topic_ids` include the row's topic), or by a row `at` with the same 30-minute rule.
  The UI line picks one and states it.
- A row time known only to the minute (`HH:MM`) counts as the END of that minute (the locator's rule); the UI must use the same rule or the two surfaces can disagree on a marker said in that minute.
- Byte parity with the email's plain-text table is tested on one fixture on both sides.

## Tests

- Flag off: email text and HTML are byte-identical to today's, for a request with and without
  `locationMarkers`.
- Flag on, no placed rows: byte-identical.
- Flag on, two rows at two places plus one row before any marker: four-column header; cells in
  order; an empty cell for the unplaced row; the HTML has 4 `<th>`.
- `at` missing: empty cell.
- A marker older than 30 minutes before the row's `at`: empty cell. This is the carry rule.
- The item-writer attaches `locationMarkers` when `for_day` returns rows, and leaves the key absent
  when `for_day` raises (fail open).
- Importing `lambda_session_finalize` with psycopg blocked works.
- Every SQL path gets an integration test: `for_day` already has one. Confirm, or add one.

## Out of scope

- The UI half.
- Turning the flag on anywhere.
- Places for rolling, updated or re-summarised emails.
