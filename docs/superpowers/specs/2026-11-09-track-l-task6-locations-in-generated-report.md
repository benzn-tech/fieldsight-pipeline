# Track L Task 6 (revised): a Locations section in the generated (template) report

Date: 2026-11-09. Supersedes the steps written under Task 6 of
`docs/superpowers/plans/2026-09-25-track-l-photos-and-topics-by-location.md`
(the `[at:]` prompt route). The owner said "push everything remaining" on 2026-11-09.

## Problem

The day view and the assembled day report already group the day's topics under the
places the inspector named (#1228, #1232). The generated report — the .docx written to a
company's template by `lambda_session_report` — has no place information except where a
photograph lands. It is also the report people actually open: the Reports page only shows
generated reports.

## Decision

Append ONE section, titled `Locations`, after the template's own sections. Same content
as the day report's Locations section (`report_sections._locations`): one heading per
place in visit order, the titles of the topics recorded there under it.

Rejected:
- **Regrouping the template's sections under place headings** (the original Task 6). It
  changes the customer's fixed layout, and a section such as "Safety" covers several
  places, so it would have to be split or duplicated.
- **Asking the model for an `[at:]` tag.** A prompt change needs a real-call A/B, and a
  prompt containing an instruction is not a model that follows it. The place is already
  known in code from the topic's time and the day's markers.

## Rules

1. **Source.** The topics are the report's own offer (`_offered_topics`: the topics in
   the report's window, with `title` and `time_range`). The markers are the request's
   `reportFacts.locations`, which org-api already attaches for this report's
   (company, folder, date). The grouping is `location_grouping.group_day` through the same
   code as the day report. Make `report_sections._locations` callable from outside
   (a public name), not a copy.
2. **Absent, not empty.** The section is absent when:
   - there are no markers;
   - `group_day` returns None;
   - no topic in the offer falls under a named place. A section with only
     "Before any location was announced" says nothing.
   It is never rendered with no entries.
3. **Never invent a place.** Groups without a place keep the day report's headings
   ("Before any location was announced" / "Location not stated" / "Time not recorded").
   Places are the markers' own labels.
4. **Walk reports are excluded.** When `artifact["walk"]` is set (multi-unit inspection),
   no Locations section. The walk report already lays out by unit, and walk `entered`
   marks are the multi-unit line's.
5. **No photographs in it.**
   - The section is appended AFTER `_place_photos` has run, so photo placement (all three
     tiers, including the "last heading" floor) is byte-for-byte what it is today.
   - The section never receives `photos_after` or `photo_streams`.
6. **Template untouched.** The template's sections, their order and their text are
   unchanged. The coverage note (`_coverage_note`) still comes where it does today.
   Locations comes after everything the template and the coverage note produce.
7. **Flag.**
   - Env var `REPORT_LOCATIONS_SECTION`, read at call time, `"true"` to enable.
   - TEST: true (`deploy.yml`).
   - Prod: `${{ vars.PROD_REPORT_LOCATIONS_SECTION || 'false' }}` (`deploy-prod.yml`).
     It changes what customers receive, so turning it on in prod is the owner's call.
   - Flag off means output identical to today.
8. **Fail open.** Any exception building the section is logged and the report is written
   without it.
9. **Observability.** One INFO line per report:
   `locations: n places, m topics placed, k unplaced` (or `locations: absent (<reason>)`).

## Review changes (Fable, 2026-11-09) — these override the text above where they differ

1. **psycopg chain (blocking).** `location_grouping` imports `repositories.location_markers`, which imports
   psycopg at module top; SessionReportFunction has no psycopg layer, so the lazy import inside
   `_locations`' `except` would make the section silently absent on every real run. Move the pure
   `locate` (+ its helpers/constants) into a pure module `src/location_locate.py`;
   `repositories.location_markers` re-exports it (no caller changes); `location_grouping` imports the
   pure module. Test: import `location_grouping` and `report_sections` with `sys.modules["psycopg"]`
   and `psycopg.rows` set to None. Confirm SessionReportFunction's CodeUri bundles the new module.
2. **Separator:** the day report's `_join` (` · `), unchanged, so both reports read the same.
3. **Shape conversion:** `_locations` returns `{"kind": "entries", "items": [{title, note}]}`. The worker
   converts to a prose section `{"title": "Locations", "paragraphs": ["**<title>** — <note>", ...],
   "line_refs": [[] ...]}` (bold via the existing `_add_rich_text` markdown), at the same level as the
   template's sections.
4. **Insertion point:** appended to `prose` after `report_facts.insert` and immediately before
   `generate_prose_document`. That renders Locations after the template's sections and before the
   Actions table and the closing coverage note. Rule 6's "after the coverage note" is replaced by this;
   no renderer change.
5. **Which reports:** day reports AND session reports (org-api attaches `reportFacts.locations` to both).
   A walk demoted to an ordinary report (template without a checklist) is ordinary and gets it: test
   the local `walk`, not `artifact["walk"]`. `requestedTopicRowIds` reports use their offer as is.
6. **Inputs:** call as `_locations(offer_topics, {"location_markers": facts_locations}, None)` with a
   public name (`report_sections.locations_section`); offer dicts have `title`/`time_range`, identity
   falls back to position — the same list goes to grouping and titles. `aliases=None`.
7. **Rule 2 case 3** (only `_NO_PLACE` rows → absent) lives in the WORKER, not in `_locations`, so the
   day report is unchanged.
8. **Flag plumbing:** `src/template.yaml` parameter `ReportLocationsSection` (AllowedValues true/false,
   copy `EmitEvidence`), env `REPORT_LOCATIONS_SECTION` on SessionReportFunction; `deploy.yml`
   `"ReportLocationsSection=${{ vars.TEST_REPORT_LOCATIONS_SECTION || 'true' }}"`; `deploy-prod.yml`
   `"ReportLocationsSection=${{ vars.PROD_REPORT_LOCATIONS_SECTION || 'false' }}"`. Remember SAM keeps
   previous parameter values — passing it explicitly on every deploy is what makes it take.
9. **INFO line:** `places` = rows titled by a marker label; `unplaced` = topics under `_NO_PLACE` rows.
10. **Mutation fixture** for "append before `_place_photos`" must include an unclaimed (tier-3) photo,
    or it cannot go red.

## Rendering

- Use the docx renderer's existing heading and paragraph path.
- One heading `Locations` at the template's section level.
- Under it, for each entry, one paragraph with the place in bold, then ` — ` and the
  topic titles joined with `; `. This is the same `note` text the day report uses.
- No new styles.

## Tests

Unit tests:
- No markers → no `Locations` heading in the rendered docx.
- A walk artifact with markers → no `Locations` heading.
- Markers under which only before-first topics fall → absent.
- Two places → one `Locations` heading with two entries, in visit order.
- The count of `<a:blip>` in the whole document and under each template heading is
  identical with the flag on and off.
- The template's headings, in order, are identical with the flag on and off.
- Flag off → document text identical to a run without markers.
- A raising `group_day` → the report is still written, with no Locations.

The mutation set:
- drop rule 2's third case;
- append before `_place_photos`.

Each mutation must turn a test red.

## Out of scope

- Email Where column (separate change).
- Location headings inside template sections.
- Any prompt change.
