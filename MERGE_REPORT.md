# Merge report — PR #829 × `origin/develop`

Worktree `C:\Users\camil\Dropbox\fs-829`, branch `merge/report-describes-the-day`
(head 3cea8a0) merged with `origin/develop` (337b776). No rebase, no push.

## What conflicted, and how it was resolved

`git merge origin/develop` reported conflicts in **three** files, not four:
`src/lambda_report_generator.py` auto-merged cleanly (develop's #840 regenerate-request
handling touches `generate_periodic_report` / `lambda_handler`; #829 touches
`_add_photos_to_document` and `render_sections_into` — disjoint regions, both present
in the merged file).

A decisive fact shaped every resolution: **this branch is built directly on #828's own
commit** (`cee5b59 The report reads the keys the model actually writes`). `git diff
3cea8a0 origin/develop -- <each conflicting file>` is therefore *purely* the reversal of
#829's own changes — develop's side of these three files carries nothing this branch
lacks. Taking this branch's side in every conflicting hunk loses no develop work, and
was verified file-by-file with that diff before resolving.

| File | The conflict | Resolution |
|---|---|---|
| `src/report_sections.py` | `_safety`: #829 builds `entries` (`title/status/note`), develop builds a 4-column `table` (`observation/risk/location/action`). Two hunks — the bare-string case and the dict case. | #829's `entries`. Develop's *reading* is preserved inside it (see below). `_issues` (entries) and `_photos` (`{name,key}`) auto-merged from #829; `_on_site` stays deleted; `_dates` and `_open_questions` are identical on both sides and come through unchanged. |
| `tests/unit/test_report_sections.py` | `test_safety_survives_when_there_is_something_to_say` and `test_safety_says_what_to_do_about_it` assert opposite shapes; develop also keeps `test_on_site_carries_the_numbers_not_the_filenames`, #829 replaces it with `test_the_recording_counts_are_not_a_section`, and the fixed section order differs by `"On Site"`. | Merged behaviour wins: entries shape, no On Site. Develop's *content* assertions (risk level, location, recommended action) are kept, restated against `items[0]["status"]` / `items[0]["note"]`. |
| `tests/unit/test_report_sections_robustness.py` | `test_a_bare_string_of_safety_is_one_observation_not_nine_letters` asserts `rows[…]["observation"]` on develop, `items[…]["title"]` on #829; plus #829's four new photo tests sit where develop's file ends. | #829's assertion; all four photo tests kept; all three of develop's Key Dates tests (#828) kept untouched — they were above the conflict and never at risk. |

## Which keys each merged section reads

Authority for "what the model actually emits" is the daily schema and instructions in
`src/lambda_report_generator.py:159-219,629-652` and the daily prompt in
`config/prompt_templates.json`.

| Section | kind | Source field | Keys read |
|---|---|---|---|
| Summary | `narrative` | `executive_summary` | the value itself (list **or** string; a list is joined into one body string) |
| Actions | `table` (`action,owner,due,priority,status`) | `topics[].action_items[]` | `action`, `responsible`, `deadline`, `priority`, `status`, `mention_count` |
| Open Questions (#819) | `list` | `topics[].open_questions[]` **and** `topics[].questions[]` | bare strings, or `question` on a dict |
| Decisions | `list` | `topics[].key_decisions[]` | bare strings, or `decision` on a dict |
| Key Dates (#828) | `table` (`when,what,who`) | `critical_dates_and_deadlines[]` | `date_mentioned` → `when` (legacy `date`, `deadline`), `context` → `what` (legacy `item`, `description`, `event`, `detail`), `who_mentioned` → `who` (legacy `responsible`); ordering via `due_dates.days_until` against `report_date`/`date`, unreadable dates last in stated order |
| Issues & Quality | `entries` | `quality_and_compliance[]` | `item` → `title`, `status` → `status`, `details` (fallback `detail`) → `note` |
| Safety (#828 keys, #829 shape) | `entries` | `safety_observations[]` | `observation` (fallback `detail`) → `title`; `risk_level` (fallback `severity`) → `status`; `location` **and** `recommended_action` (fallback `recommended`) joined with a middot → `note`. A bare string becomes one entry. |
| Photos (#829) | `photos` | `topics[].related_photos[]` | bare filenames, emitted as `{name, key}` with `key = users/{user_name}/pictures/{report_date}/{name}`; an entry already containing `/` is left alone; duplicates collapse |

Nothing #828 taught the renderer to read went unread after the merge: safety's
`risk_level`, `location` and `recommended_action` all still reach the reader, and Key
Dates keeps both its key set and its date ordering. `render_sections_into` in
`src/lambda_report_generator.py` renders `entries` (bold title, status beside it, note
underneath) and calls `_add_photos_to_document` for `photos`, with
`REPORT_MAX_EMBEDDED_PHOTOS` (default 40) as the ceiling.

## Tests

* `python -m pytest tests/unit/test_report_sections.py tests/unit/test_report_sections_robustness.py -q` → **61 passed** in 0.84s
* `python -m pytest tests/unit -q` → **5117 passed, 2 skipped**, 73 warnings, 136s

## Proof on real data

`report_sections.build` run over the deployed-code TEST report
`test_0903.json` (Ben_UCPK2, 2026-09-03), with the report's stored `sections`
discarded first so the merged builder does the work:

```
report_date=2026-09-03 user_name=Ben_UCPK2  -> 6 sections

== Summary | kind=narrative | items=1
   - Address falsework exclusion zone breach at Cook Brothers site by creating WhatsApp
     coordination group for vehicle movements and delineated zones Push tanking contractor
     to complete Zone 1 detailing by 29 August to hold council inspection on 30 August and
     enable backfill from 28 August Expedite PC certificate to avoid $5,000 bond extension …

== Actions | kind=table | items=23
   fields: ['action', 'owner', 'due', 'priority', 'status']
   - {"action": "Ensure all crew complete sign-in and daily briefing by 08:00", "owner": "Jesse", "due": "08:00", "priority": "medium", "status": "open", "mentions": 1}
   - {"action": "Send group email confirming Twizel bookings 5th-8th", "owner": "Ben", "due": "Tonight", "priority": "medium", "status": "open", "mentions": 1}

== Decisions | kind=list | items=12
   - Ben to keep chasing Corbin and Aaron for closeout documents
   - Backfill Zone 1 before Zone 2 approved subject to 7-day strength

== Key Dates | kind=table | items=11
   fields: ['when', 'what', 'who']
   - {"when": "3 July 2026 (revised from 17 March 2026)", "what": "Agreed practical completion date, DLP extended ~3 months, final bond release", "who": "Ben"}
   - {"when": "28 August / 30 August - 1 September", "what": "Backfill behind Zone 1 retaining wall, 7 days after blockfill", "who": "Ben"}

== Issues & Quality | kind=entries | items=10
   - {"title": "Electrical PS4 blocked by light pole PS4", "status": "concern", "note": "Need PS4 for light poles to NZS 3910 tender requirement; only path is letter of professional opinion from Cal Feedway, otherwise give MOE credit and omit poles"}
   - {"title": "Tanking / waterproofing QA Zone 1", "status": "in_progress", "note": "Prime + torch-on system, 30m of wall, critical detailing around step; contractor allowed 5 days to 29th for council inspection 30th; photos being sent to council contact"}

== Safety | kind=entries | items=5
   - {"title": "Entry into Cook Brothers falsework exclusion zone with diggers operating", "status": "high", "note": "Cook Brothers adjoining site / falsework zone · Start WhatsApp group with falsework, Cook Brothers to plan movements and delineate zones; toolbox focus on vehicle movements Mondays 09:30"}
   - {"title": "Crane-from-bottom operation with no visibility for operator and just-in-time trucks on uncontrolled access", "status": "medium", "note": "Upper crane pad / back route · Pre-document lifts, confirm purlin locations, coordinate delivery windows; allow slower cycle due to swinging and blind picks"}
```

**No section rendered empty.** Every section built from a field this report carries is
populated, and the Safety entries demonstrably carry #828's three keys. The old stored
`sections` in that file (written by the deployed code) were `Summary/On Site/Actions/
Decisions/Key Dates/Issues & Quality/Safety`; after the merge `On Site` is gone by
design and Issues & Quality and Safety are `entries`.

Two sections do not appear, and neither is a mis-read key:

* **Open Questions** — the daily report schema has no `open_questions`/`questions` on a
  topic (topic keys in this file: `topic_id, time_range, topic_title, category,
  participants, summary, key_decisions, action_items, safety_flags, related_photos`).
  The section exists for the meeting/extraction path, which does write them, and its
  unit tests cover both key names.
* **Photos** — every one of the 11 topics has `related_photos: []`. There is no photo
  filename in the report to turn into a key. The `{name,key}` construction is covered by
  four unit tests, including the already-a-path and duplicate cases.

## Dropped

One test was dropped, and replaced rather than deleted:

* `test_on_site_carries_the_numbers_not_the_filenames` (develop) asserts an `On Site`
  `kpi` section. #829 deletes that section outright (controller rule 1), so the test
  asserts behaviour the merge removes. `test_the_recording_counts_are_not_a_section`
  (this branch) takes its place and asserts the removal, including that no section of
  kind `kpi` survives.

Develop's two safety tests were not dropped: both survive with their content assertions
(`risk_level`, `location`, `recommended_action`) restated against the `entries` shape,
in `test_safety_says_what_to_do_about_it`.

## Concerns

* **Summary is still one narrative paragraph, not bullets.** The brief lists "Summary
  becomes bullets" among #829's presentation changes, but neither side implements it:
  `_summary` is byte-identical on `3cea8a0` and `origin/develop` (`kind: "narrative"`,
  a list joined with spaces into one body string), and #829's diff does not touch it.
  On the real report this produces exactly the run-on the brief describes — five model
  bullets with no terminal punctuation concatenated into one line (visible in the dump
  above). Making it a bullet list would be a design decision neither side made, so per
  rule 5 it is reported rather than invented. It is plausibly the paired
  `fieldsight-ui` PR's half ("Pairs with fieldsight-ui" in #829's body). **This is the
  one item in the brief the merge does not deliver.**
* Nothing else outstanding: the merge is committed with only the four files as
  content, tests are green, and no section renders empty on real data.
