# Checklist gaps: tell the inspector what was not done

Status: design (2026-10-16), Fable-reviewed (approve with changes, applied). Sub-project 3 of the "project assets / template
digitisation / inspection gaps" batch; this is step B (after the fact). Step A
(the device asks "three items left, cover them?" when the check ends) is a
later spec and reuses what this one stores.

## Why

A spoken check ("starting the pre-pour check") already becomes its filled
checklist on its own (`checklist_reports.py`, `checklist.py`). Every item the
recording did not address is written as `Not covered in the recording`, and an
item answered `No` carries its comment, responsible and due. But that only
lives inside a Word file. The inspector learns what is missing only by opening
the document and reading 40 rows.

Owner, 2026-10-15: do not just fill the template after the analysis -- tell the
user which items were not done. Owner chose (2026-10-16) to list two kinds,
shown apart because they lead to different actions:

- **Not covered** -- the recording never addressed the item. Action: go back and
  check it (a missed inspection step).
- **Failed** -- the item was answered `No`. Action: rectify (with its
  responsible and due when spoken).

`Yes` and `N/A` are not gaps. An answer the code dropped (no evidence in the
transcript, see `checklist.rebuild`) is not covered: the form shows it blank,
so the gap list must agree with the form.

## What changes

### 1. Backend: the gaps are computed where the form is built

`checklist.rebuild` already decides, per item, answered or blank. It adds to
its `report` dict:

```json
"gaps": {
  "not_covered": [{"n": 3, "text": "Formwork braced and plumb"}],
  "failed": [{"n": 7, "text": "Edge protection installed",
              "comment": "rail missing on the north side",
              "responsible": "Mike", "due": "Fri 17 Oct 2026"}]
}
```

- `n` is the 1-based item number in the customer's order, for ordering only:
  the Word form prints no item number (`DOC_COLUMNS`), so people match by
  wording. `text` is the customer's own wording (the same string the form
  prints). `text`, not `item`: `report["dropped"]` already uses `item` for the
  number, and the two must not be mixed.
- Built from the same `answered` map as the rebuilt table rows.
- No new cap: `MAX_ITEMS` (120) is enforced when a template is saved
  (`validate_items`), so `rebuild` never sees more.
- An "answered twice" drop keeps the first row, so that item is covered and
  correctly absent from `not_covered`.
- Known limits (pre-existing, not fixed here): (a) a checklist the model
  retitled stays as the model's raw table in the Word file while `apply` adds a
  blank form, so the gaps read "all not covered" beside it; (b) two checklist
  sections with the same title keep one report (`reports` is keyed on title).
- The omitted-by-model path (`checklist.apply`, a checklist the model left out)
  goes through `rebuild` too, so it gets every item as not covered for free.

These reports already reach the worker's result JSON as `meta["checklists"]`
(spread into the `done` result in `lambda_session_report.process_request`). No
new storage, no migration, no new S3 key.

### 2. org-api: the checklist report payload carries the gaps

`_checklist_report_status` already reads the result JSON for `status`. Its
return becomes `(status, err, gaps)`; the gaps are summed over that report's
checklist sections in a separate helper wrapped so that any failure (old or
malformed `checklists`) yields `gaps = None` and never changes `status` or
raises. `_checklist_report_payload` adds:

```json
"gaps": {"notCovered": [{"n": 3, "text": "...", "checklist": "Pre-pour"}],
         "failed": [{"n": 7, "text": "...", "comment": "...", "responsible": "...",
                     "due": "...", "checklist": "Pre-pour"}]}
```

- `gaps` is `null` when the status is not `done`, or unless at least one
  checklist in the result carries a `gaps` key. `checklists` can be `{}` for a
  matched template (`is_checklist` matches on `kind` only; `checklist_sections`
  needs items) and an old result has no `gaps` -- both are null, never "All
  items covered". A section without `gaps` is skipped, never counted as zero.
  Absent data must not read as "all done".
- `checklist` is the section title, so a template with two checklist sections
  stays readable.
- Both routes (`/days/{date}/checklist-reports`, `/checklist-reports/recent`)
  use the same payload function, so both get it. No new S3 read: the result
  JSON is already read per row; org-api already has GetObject on
  `session_report_results/*`.
- A day report generated from the dialog with a checklist template also writes
  `checklists[...].gaps` into its result but has no `checklist_reports` row, so
  nothing surfaces it. Intended for now; `day_report_status` is the later hook.

### 3. Frontend (fieldsight-ui): show the gaps where the report already shows

- **Timeline day card** (`ChecklistReportsCard`, `scripts/pages/timeline.js`):
  each done report row shows a summary after its name -- "3 not covered ·
  2 failed" -- or "All items covered" when both lists are empty and `gaps` is
  not null. Clicking the summary expands two short lists: *Not covered* (item
  number + text) and *Failed* (item, comment, responsible, due when present).
- The summary words live in `scripts/api/checklist-reports.js` as a new
  exported `gapWords(r)` (returns `''` when `gaps` is null); `words()` stays
  unchanged and the card and the bell each append `gapWords`. New CSS classes
  for the expansion, defined for both themes.
- **Notification bell** (`notification-bell.js` via `FS.checklistReports`):
  the line for a done report adds the same one-line summary. No expansion in
  the bell; clicking goes to the day as it does now.
- English UI copy (the app is English). Light and dark themes must both read
  (measure in the browser, per the theme-contrast lesson).
- The API layer (`scripts/api/org.js`) passes the response through untouched;
  nothing to whitelist (the trap is on request bodies, not responses -- still
  verify in the browser that `gaps` arrives).

## Not in this spec

- **Voice prompt at the end of a check (step A).** It needs the gaps before the
  report is written (a quick pass at "check finished"), and the voice agent's
  3-4 s first-token limit. Separate spec.
- **Stop email.** The stop email is sent at final extraction; the checklist
  report is generated after it, so the gaps are not known yet. Adding them
  would mean holding the email. Not now.
- **Covering a gap later.** Saying the same check again makes a second report;
  merging two windows into one form is a separate design.

## Error handling

- Result JSON unreadable or missing -> status `pending` as today, `gaps` null.
- A checklist report from before this change -> `gaps` null, UI silent.
- A result with checklists but every list empty -> "All items covered".
- Re-extraction over a longer window replaces the row's `result_key`
  (`checklist_reports._record`), so the row is `pending` (no gaps, no Download)
  until the new result lands. Expected, not a bug; the old result JSON stays in
  S3 unread.

## Testing

- Unit (`tests/unit/`, next to `test_a_checklist_is_answered_only_from_the_recording.py`):
  - rebuild with a mix of Yes / No / N/A / dropped (bad evidence) / unanswered
    -> `not_covered` = unanswered + dropped, `failed` = the No rows with their
    fields, Yes and N/A absent; item numbers and wording match the table rows.
  - the omitted-by-model checklist -> every item in `not_covered`.
- Unit for org-api payload, running the real status and summing code through a
  fake `s3()` client (not a stubbed status function): done with gaps -> summed
  lists with section titles; done without a gaps key -> null; `checklists: {}`
  -> null; malformed `checklists` -> null with status still `done`; NoSuchKey
  -> pending, null. Update the 2-tuple stub in
  `tests/unit/test_a_spoken_check_makes_its_own_report.py`.
- Revert check: remove the gaps code and confirm the new tests go red.
- Frontend: a node test importing `gapWords` from `checklist-reports.js`
  (counts, "All items covered", null -> '').
- On TEST: record (or replay) a short pre-pour check that skips items and says
  one is not done; confirm the day card shows the right two lists against the
  Word file, matching items by wording.

## Rollout and other sessions

- Additive only: a new key in a result JSON and a new field in a response. Old
  frontends ignore it; the new frontend tolerates its absence. No flag needed.
- Backend: PR to `develop` -> TEST. Frontend: PR to `dev` -> dev Amplify.
- Prod goes with the next owner-approved release; the order does not matter
  (each side tolerates the other's absence).
- `lambda_org_api.py` and `timeline.js` are touched by other sessions: branch
  from fresh `origin/develop` / `origin/dev`, keep the diff to the functions
  named above, rebase right before merge.
