# Multi-unit walk: site-wide answers are answered once, in General (D5, 2026-11-13)

Parent specs: `2026-11-03-multi-unit-inspection-design.md` (R1-R52) and
`2026-11-13-multi-unit-walk-defects.md` (D1-D4; D5 moved here after the Fable review).

## Problem (measured on TEST, ElevenLabs pre-roof walk, 2026-11-12)

The opening of the walk went through 13 site-wide items: silt fence, consent
documents, site instructions, engineer, PS4, location certificate, LBP card,
variations, timing, truss layout, location. The model put them in General, as the
shape sentence says for a finding that names no unit. Then:
- each unit's row for those items stayed "not checked", so 20 of the 59 final-check
  questions asked about things already answered on site;
- Unit 2's "everything else in this one is all good" filled the silt fence Yes in
  Unit 2, while General said it was down. The report contradicts itself.

The owner delegated the choice (2026-11-13: pick the long-term option).

## Options

- (a) Copy the General answer into every unit as that unit's own answer. Rejected:
  one silt-fence No becomes three Nos, three read-backs on site and three defects.
- (b) Leave it. Rejected: the questions are noise, and ALL contradicts General.
- (c) **Chosen.** A site-wide General answer is answered once, in General. Each
  unit's row for that item points to it and is neither not-checked nor failed in
  the unit. ALL never fills it. The final check does not ask it per unit.

## Ruling R53 (amends R-General: "a finding that names no unit goes to General,
## never guessed into a unit", and the all-good ruling)

A General finding is **site-wide** only when it was said **before he walked into the
first unit**. A finding that names no unit and is said inside a unit's stretch stays
General-only, exactly as today: it is not guessed into any unit, and it is not
linked.

How "before the first unit" is decided, in this order:
1. **Entered-mark places** (`walk_checklist._mark_places`, already used for
   all-good): every place the General row's evidence was said (`_Text.hits`) lies
   before the first mark's place. Both the report and the final check pass
   `entered`, so this works in both.
2. If the marks cannot be placed (`_mark_places` is None) but there are
   **stretches** (report path only): the evidence's line time is in a General
   stretch (unit None) that starts before the first unit stretch.
3. Otherwise: **not site-wide**. When placement is not known it is conservative and
   behaves as today.

Only rows with a valid item number (1..N), a valid answer (`checklist.ANSWERS`) and
evidence that passes can be site-wide.

## Design

### General rows keep their item and answer
- `_general` keeps `n` (int, or None when the cell is not a valid item number) and
  `answer` (normalized), alongside `comment` and `evidence`. Rows with no valid
  number stay General notes (today's behaviour) and are never linked.
- The same item twice in General: the first valid row wins, the later one is
  dropped `"answered twice"` (same as `checklist.rebuild`).
- A General `ALL` row is dropped with the reason `OUTSIDE` ("all good outside the
  unit"): all-good belongs to a unit.
- `walk_report.general_section` shows, for a numbered row, `"<item text>: <answer>
  -- <comment>"`. Un-numbered rows render as today.

### A new row state: `site_wide`
- In `build`, before each unit's all-good is applied (inside `_unit_section`,
  before `_apply_all_good`), every unit row that is still `not_checked` and whose
  item has a site-wide General answer becomes:
  `state "site_wide"`, `origin "general"`, `answer` = General's answer,
  `comment "Site-wide -- see General"`, `evidence` = General's evidence.
- A unit's own row always wins over General. It is never replaced.
- `_apply_all_good` already skips rows that are not `not_checked`, so ALL never fills
  a site-wide row.
- `render`: the row shows the answer and the comment above.
- `summary`: the cell is the answer followed by " (G)", e.g. "No (G)".
- `report.gaps` (in `checklist.rebuild`'s report and the overlay's `_recount`):
  site-wide rows are in neither `not_covered` nor `failed`. The General No is listed
  once, in General.

### On-site answers (walk_answers_overlay)
- A site-wide row has **no base** (as a not-checked row does), so an on-site answer
  on it is a **fill** (origin on_site), never "confirmed" or a conflict.
- The overlay keeps the site-wide row as the fallback. When a row has no base and
  no site answer, `_draw` redraws the site-wide row, not "not checked". This is a
  new field in the state tuple, beside base and site.
- A move whose source is a site-wide row is refused `NOT_A_NO`. The existing check
  `src["state"] != "answered"` already refuses it. A test pins it.
- A move INTO a unit whose row is site-wide replaces it, as it replaces a Yes or a
  not-checked row today.
- `_recount`: site-wide rows counted in neither list.

### Final check (check_done)
- `walk_questions`: a `site_wide` row asks nothing. Today any `answer == "No"` with
  origin not on_site becomes `confirm_no`; site_wide must be excluded explicitly.
- `_walk_result`: `failed` per unit leaves out site_wide rows. Any `not_covered`
  derived from state is already correct, since site_wide is not `not_checked`.
- The device contract (`docs/contracts/2026-11-03-multi-unit-final-check.md`)
  keeps its shape. The final check asks fewer questions, and a unit's `failed` no
  longer repeats a site-wide No. A sentence is added to the contract saying so.
- The phone route (`lambda_org_api`, walk final check read) reads the stored final
  check, so nothing changes there. Verify: no code there switches on `state` values
  it would not know.

## Not changing
- Units, stretches, walk keys, the database, the API shapes.
- Ordinary checks: General and site-wide exist only in walks.

## Verification
1. Unit tests:
   - site-wide before the first mark, linked in every unit without its own row;
   - a General row said inside Unit 2's stretch is NOT linked;
   - marks not placeable plus no stretches means not linked;
   - a unit's own N/A against a General No: the unit keeps N/A;
   - a General duplicate item: the first wins, "answered twice";
   - a General ALL is dropped OUTSIDE;
   - ALL in a unit does not fill a site-wide row;
   - summary shows "(G)"; gaps leave out site-wide rows;
   - overlay: an answer on a site-wide row is a fill; removing it restores the
     site-wide row; a move off a site-wide row is refused; a move into one replaces
     it;
   - final check: no question for site-wide rows; `failed` leaves them out.
2. Replay `walk_checklist.build` and `check_done` on the stored EL walk output (no
   model call needed: run1 `fc_out.json` and the TEST walk text): the question count
   drops from 59 to about 39, and Unit 2 shows the silt fence "No (G)", not Yes.
3. TEST: re-inject the EL walk after deploy. The same numbers live, and the 13
   answers still apply.

## Fable review 2026-11-13: approved with changes (these override the text above)

The selector was checked on the real walk. Mark places are Unit 1 @310, Unit 2 @574,
Unit 3 @638. All 10 numbered General rows hit at words 111-301 in each of 5 replays.
20 of the 59 questions are those 10 items in Units 1 and 3, so 59 -> 39 holds.
(Problem section: 10 General rows, not 13. Items 4/5/8 name a unit and are never
General.)

1. Build `_Text` and the first-mark place whenever `entered` is given and General has
   a numbered row, not only when there is an ALL row (the final check often has none).
2. Compute General first (kept, dropped, `{n: row}` of site-wide rows), then pass the
   map into `_unit_section`.
3. `checklist.rebuild` cannot exclude site-wide rows (it runs before the link). After
   linking, recount `answered`, `gaps.not_covered`, `gaps.failed` and
   `report["evidence"]` from the rows unconditionally, with one helper shared with
   the overlay's `_recount`.
4. Selector guards:
   - at least one hit AND every hit before the first mark (an empty hit list is not
     site-wide);
   - use a new `_first_mark_place` (the earliest named mark by time, its first hit).
     A later unfound mark must not disable the selector. `_mark_places` stays for
     all-good;
   - path 2 (stretches) applies only when a unit stretch exists;
   - the selector relies on the transcript being in time order. Both paths join
     oldest recording first (check_done 909-911, 967-979; the report's `turns`).
5. Overlay:
   - base is None for `site_wide` rows;
   - `_moved` replaces whenever `target_row["state"] != "answered"`, so a site-wide
     No target is replaced, not merged. The test uses a No target;
   - state tuple [base, site, fallback]. A move's source reset keeps the fallback, so
     a site-wide row filled on site and then moved away shows "No (G)" again;
   - the `_draw` fallback branch sets `report["evidence"][n]` like the others.
6. check_done:
   - `walk_questions` excludes site_wide from confirm_no;
   - `_walk_result` already excludes it (keys on `state == "answered"`); a test pins it;
   - phone `/open` with `source: "report"` (`open_from_result`) rebuilds rows from the
     stored gaps: a site-wide row is in neither list, lands as answered/"" and is not
     asked, which is correct. A test pins it.
7. Edge cases, each with a test: evidence said both before and inside a unit -> NOT
   site-wide; a site-wide remark after the first mark -> General-only, still asked per
   unit (accepted); an opening mis-marked as entering Unit 1 -> nothing linked
   (accepted).
8. `lambda_session_report` checklist trace: add a `site_wide` count beside `answered`,
   so a unit answered only by site-wide rows is not logged as `no_match`.
