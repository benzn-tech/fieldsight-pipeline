# Track L — The report reads level by level: topics and photographs grouped by where he was

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

## Audit 2026-11-05 — what other lines shipped since this plan was written

Read against `origin/develop` @ `d14c5698`. Columns: what exists, what this plan still has to add, and which steps are now obsolete or change shape.

| Area | Already done (where) | Still missing | Plan step now obsolete / changed |
|---|---|---|---|
| Markers captured + stored | `clean_location_markers` + `time_location_markers` give `at_s` to the second (`lambda_extract_session`); per-session merge so a later session no longer wipes the day (`location_markers.replace_for_session`, b14eaf3c); "inspection of <place>" now emits a marker (63472bc8) | — | — |
| Day view photo grouping | `_photo_groups` (`lambda_org_api.py` ~12978) groups photos by `location_markers.locate`, to the second (#1030, #1032); `None` vs `[]`; cross-user gate | **topics** are not grouped; no `location_groups` key; places are keyed by raw string, so "Level one" and "Level 1" are two groups | Task 4 unchanged in intent, but `_photo_groups` now keys on `locate` (seconds); the replay test must use that, not the 2026-09-25 minute rule |
| Photo → topic binding by place | `photo_binding._stays/_location_owner`: a photo belongs to the topic under way when its place was announced (owner 2026-10-01). Runs in `photo_rebind` on every day, **not** gated | Stay key is lowercase raw text: "level one" then "Level 1" are two stays | Task 3 Step 2 ("photo follows its topic's place") is now what binding already does; group_day should reuse `photo_binding.place_at`, not a second locator |
| Inside one place: which item a photo shows | `_first_words_owner` (d8bb5605) | Needs quote `at_s` → only with **EMIT_EVIDENCE=true; OFF on prod**. On prod, inside one stay the photo goes to the topic under way when the place was named | Not this track's to fix (prod flag = owner). Note it; Task 8 counts must say whether TEST had it on |
| Template (generated) report | `_place_photos(places=…)` places a topic's photos on the line naming their place (`photo_binding.names_place`, owner 2026-10-01); prompt already asks "two floors walked → one line each" (`report_template.py` ~213); markers reach the worker as `reportFacts.locations` (b14eaf3c) | No location **headings**; no `[at:]`; no `sectionsPlacedByLocation` | Task 6 Steps 1–3 shrink: the per-line place match exists. What is left is headings in visit order. Re-decide after Task 1 |
| Multi-unit walk (other line, spec 2026-11-03) | `units.normalize_unit` ("Unit two"/"U2"/"unit-2" → "Unit 2"; unit/lot/room/house/apt…); `walk_report` gives one section per entered unit (8fab5762) | Levels/floors/zones/Chinese not covered by `units` | Task 2 must **reuse** `units.normalize_unit` for `kind: room/unit`, not write a second unit regex. For a walk check, Task 6 is already done by the walk line; Track L must not re-section a walk report |
| Number words in places | `photo_binding._place_words` (zero/ground…ten; "ground floor" = level 0) | ordinals ("first floor"), "L1", 一楼/二层, room 1.01 | Task 2 builds on it; `names_place` and `_stays` should switch to `place_key` once it exists (one rule, not three) |
| Nightly assembled report (`report_sections`) | — | *Locations* section | Task 5 unchanged |
| Confirmation email | — | *Where* column | Task 7 unchanged |
| Measurement | — | Task 1 | — |

Net: Tasks 1, 2, 5, 7, 8, 9 unchanged; Task 3 becomes topic placement + one shared locator; Task 4 adds `location_groups` and normalises the key; Task 6 is reduced to headings and is skipped for walk reports.

---

**Goal:** When a site manager walks Level 1, then Level 2, then Room 101, and says so as he goes, the day's report groups his topics **and** their photographs under those places, in the order he visited them. He stops assembling that by hand.

**Architecture:** Nothing new is captured and nothing new is stored. The extractor already emits `location_markers` (`lambda_extract_session.clean_location_markers`, `{at: "HH:MM", location, quote}`), the writer already persists them per day (`day_location_markers`, replaced wholesale on every pass), and org-api already groups a day's photos by them (`_photo_groups`, `shape["photo_groups"]`). What is missing is (1) a normaliser so "Level one", "L1" and "first floor" are one place, (2) the same placement applied to **topics**, not only photos, (3) a *Locations* section in both report renderers, and (4) a measurement of how often he actually says where he is — because if he does not, the product fix is a habit and a button, not code. Everything is computed at read/render time from `markers × time`, so it survives re-extraction and does not wait for Track B.

**Tech Stack:** Python 3.11, pure modules + `report_sections` + `lambda_session_report` (docx) + `lambda_org_api` day payload. No migration.

**Spec:** `docs/superpowers/specs/2026-09-24-event-graph-and-jev-assessment.md` §3 (Location is a dimension, not a tag) and the owner's 2026-09-25 rule: **location comes from what the inspector says, not GPS and not EXIF.**

## Global Constraints

- **Never invent a place.** A photo or topic taken before he said where he was goes under `location: null` ("before any location was announced"), exactly as `_photo_groups` does today. That group renders; it is not hidden.
- **`None` and `[]` stay different.** No markers on the day → the section is absent and the flat lists render as before. Markers present but nothing fell in one → an empty group is allowed.
- **Additive payloads.** `photo_filenames` and `topics[].related_photos` are unchanged. New keys only.
- **A photograph appears once** per document (PR #925's rule; prod has 13.7% of photos bound to more than one topic). Under a location grouping the photo sits under its location; within the location, under its topic; a photo bound to two topics goes under the earlier one.
- **The cross-user clip gate applies unchanged** (`lambda_org_api.py:7030-7078`): a site-clipped caller gets no photo groups and no location section, for the reason written there — a location plus a minute is a fact about where the target was.
- **Measure before and after** (Task 1 and Task 8); a prompt or habit change is what moves the number if code alone does not.
- **No GPS, no EXIF.** `recordings.gps_track` exists but is not a floor; phone photos carry no room. Stated so nobody "improves" the source.

---

### Task 1: Measure how often he says where he is

**Files:**
- Create: `scripts/measure_location_markers.py` (RDS Data API, read-only, `--database fieldsight_test|fieldsight`)
- Output: numbers pasted into this plan's §Findings and into the assessment spec

- [x] **Step 1:** (done 2026-11-05, see §Findings) Over the last 30 days per company: days with any topic; days with ≥1 marker; markers per day (p50/p90); distinct raw location strings and their counts (this is the normaliser's input); photos per day and the share that `locate()` places (non-null) — reuse `location_markers.locate` and `extract_base_time_from_filename` so the number is what the payload would show.
- [x] **Step 2:** (done; 20 prod + 12 TEST strings, kept locally, shapes in §Findings) Print the top 50 raw location strings. Keep this list; Task 2's regexes are written against it, not imagined.
- [x] **Step 3:** (customer days 0 %, internal 41 % → routed to owner 2026-11-05, see §Findings) If fewer than 30 % of inspection days carry a marker, say so at the top of this plan and route the owner to the habit + App button (Task 9) before Tasks 3–7 are built. The code will still be built; the order of value changes.

---

### Task 2: A place normaliser

**Files:**
- Create: `src/place_normalise.py` (pure; no I/O)
- Test: `tests/unit/test_place_normalise.py`

**Interfaces:**
- `normalise(text) -> {"kind": "level"|"room"|"zone"|"block"|"other", "value": str, "label": str, "raw": str}`; `same_place(a, b) -> bool`; `place_key(text) -> str` (what groups are keyed on).

- [x] **Step 1: Rules, from Task 1's list.** (done: `src/place_normalise.py`; reuses `units.normalize_unit`; level+zone in either order = one key; a phrase naming two places, or a measurement, is `other`) English and Chinese: `level|lvl|l|floor|storey` + ordinal/number (`one`, `1st`, `ground` = 0, `一楼`, `二层`); `room|rm|unit|apt` + code (`101`, `1.01`, `G02`); `zone|area|block|wing|stair|grid` + token; compass words as qualifiers (`east`, `north`). Output a canonical `label` ("Level 1", "Room 101", "Zone B east"). Anything unrecognised is `kind: other` with `value` = casefolded, whitespace-collapsed text (`content_hash.normalize`).
- [x] **Step 2: Aliases.** (done: `place_key(text, aliases=None)`, caller-built dict, one hop) `place_key` consults an optional alias dict (from `name_aliases` rows with `kind='other'` and a `place:` prefix, loaded by the caller) so a company can pin "the new block" → "Block C" without a code change. No new table.
- [x] **Step 3: Tests** (done: 159 incl. walk units; customer strings replaced by invented examples of the same shapes) from real strings: `"Level one"`, `"L1"`, `"first floor"`, `"一楼"` → same key; `"Room 101"` vs `"Room 1.01"` → same; `"Level 1"` vs `"Level 1 east"` → **different** (a qualifier is a narrower place, not a spelling); `"the kitchen"` → `other`; an empty string raises.

---

### Task 3: Place topics, not only photos

**Files:**
- Create: `src/location_grouping.py` (pure)
- Test: `tests/unit/test_location_grouping.py`

**Interfaces:**
- `group_day(markers, topics, photos, *, aliases=None) -> list[Group]` where `Group = {"location": label|None, "kind", "order", "topic_ids": [...], "photos": [filename...], "unbound_photos": [...]}`, in first-visit order.
- `place_of(markers, hhmm) -> label|None` wrapping `location_markers.locate` + `place_normalise`.

- [x] **Step 1: Topic placement.** (done: `src/location_grouping.py`) A topic's place is `place_of(markers, start_of(time_range))`; a topic that starts before the first marker is `None`. A topic whose window spans two markers stays with the place it **started** in — he was there when he began talking about it; the next marker is a new place, not a split of this topic.
- [x] **Step 2: Photo placement.** (done; bound photo follows its earliest topic) A photo bound to a topic (`related_photos`) follows its topic's place. A photo bound to no topic is placed by its own time with `place_of`, into the group's `unbound_photos`. A photo bound to two topics goes with the earlier topic and is not listed twice.
- [x] **Step 3: Consecutive visits to the same place merge**; a return to a place after another place is a **new** group (Level 1 → Level 2 → Level 1 renders three groups, in that order). Say so in the docstring: the day is a walk, and "back on Level 1" is what he did.
- [x] **Step 4: Tests** (done: 18; also `reason` on null groups: before_first / expired / no_time, empty groups never emitted, all-unusable markers → None) on a hand-built day: three markers, five topics (one before any marker, one spanning two), eight photos (two unbound, one double-bound). Assert order, membership, and that every photo appears exactly once across all groups.

---

### Task 4: The day payload carries the grouping

**Files:**
- Modify: `src/lambda_org_api.py` (`_photo_groups` → delegate to `location_grouping.group_day`; add `shape["location_groups"]`)
- Test: `tests/unit/test_org_api_location_groups.py`

- [x] **Step 1:** (done: `photo_groups` kept on its own code path — ruling — markers read once by `_day_markers`; `location_groups` added, topic ids = `topics[].topic_id`) Keep `photo_groups` shape byte-identical for existing clients (derive it from `group_day`), and add `location_groups` = the full `Group` list with `topic_ids`. Same `None` semantics, same cross-user gate, same never-raise wrapper.
- [x] **Step 2:** (done: `tests/unit/test_org_api_location_groups.py`, 8 tests) Test the gate (site-clipped caller: neither key), the no-marker day (neither key), and a marker day (both keys, and `photo_groups` equals what the old code produced on the same fixture — replay, do not re-derive).

---

### Task 5: The assembled report gets a Locations section

**Files:**
- Modify: `src/report_sections.py` (`build` gains `_locations(topics, report, markers)`; `_photos` stays)
- Modify: `src/lambda_report_generator.py` / wherever `report_sections.build` is called for a day (pass markers; the nightly generator reads S3, so it takes markers from the day's extraction artifact `location_markers`, not Aurora)
- Test: `tests/unit/test_report_sections_locations.py`

- [x] **Step 1:** (done, org-api day view; null headings by reason; entries only with a topic title; Photos keeps every photo because no renderer draws `entry.photos` yet — ruling) `kind: "entries"` section titled *Locations*: one entry per group — heading = label (or "Before any location was announced"), then the group's topic titles, then its photo keys (same `{name, key}` shape `_photos` builds, so the docx renderer already knows it).
- [x] **Step 2:** (done) Section is dropped when there are no markers (the `build` rule: no empty headings).
- [x] **Step 3:** (done; **nightly generator NOT wired**: it reads only `transcripts/`, has no DB; needs the day's `extractions/` markers merged and passed as `{**report, "location_markers": m}` at `build`) Test: a report with markers renders the section between *Safety* and *Photos*; without markers the section list is unchanged (assert on the exact list of titles a fixture produced before this change).

---

### Task 6: The generated (template) report gains a Locations section

> **Revised 2026-11-09** (owner: "push everything remaining"). Spec:
> `docs/superpowers/specs/2026-11-09-track-l-task6-locations-in-generated-report.md` (Fable-reviewed; its
> "Review changes" section overrides). The original `[at:]` prompt route and the regrouping of the
> customer's template sections are rejected there. Flag `REPORT_LOCATIONS_SECTION`: TEST on, prod off
> (owner's switch).

**Files:**
- Create: `src/location_locate.py` (pure `locate` + helpers, moved from `repositories/location_markers.py`, which re-exports)
- Modify: `src/location_grouping.py` (import the pure module), `src/report_sections.py` (public `locations_section`), `src/lambda_session_report.py` (append the section), `src/template.yaml`, `.github/workflows/deploy.yml`, `.github/workflows/deploy-prod.yml`
- Test: `tests/unit/test_session_report_locations.py` (new), `tests/unit/test_location_grouping_no_psycopg.py` (new)

- [x] **Step 1: Break the psycopg chain.** Move `locate` to `src/location_locate.py`; re-export from `repositories.location_markers`; `location_grouping` imports the pure module. Test with `psycopg` blocked in `sys.modules`. (done: `CARRY_MINUTES`, `_seconds`, `locate` moved; re-exported; subprocess test blocks psycopg and CALLS `locations_section` -- red before the move with `import of psycopg.rows halted`)
- [x] **Step 2: Public builder.** `report_sections.locations_section(topics, markers)` = today's `_locations` (day report output unchanged; `_locations` stays as a thin caller). (done: `locations_section(topics, markers, photos=None)` + `NO_PLACE_TITLES`; day-report tests pass unchanged)
- [x] **Step 3: Worker.** Behind `REPORT_LOCATIONS_SECTION=true`, not for a (non-demoted) walk: build from the offer topics + `reportFacts.locations`; absent when None or when no row has a marker label; convert entries to a prose section `**Place** — titles`; append after `report_facts.insert`, before `generate_prose_document`; fail open; one INFO line. (done: `lambda_session_report._locations_section`; titles joined with the day report's ` · `)
- [x] **Step 4: Flag plumbing.** template.yaml parameter + env; deploy.yml TEST default true; deploy-prod.yml PROD default false. (done: `ReportLocationsSection` parameter + SessionReportFunction env; CodeUri is `src/`, so `location_locate.py` ships)
- [x] **Step 5: Tests** per the spec's list (docx headings, `<a:blip>` counts and template heading order equal with flag on/off, walk excluded, before-first-only absent, raising grouping → report still written) and the two mutations (with a tier-3 photo in the fixture). (done: `tests/unit/test_session_report_locations.py`; both mutations turned tests red, then reverted)

---

### Task 7: Email and Today view

**Files:**
- Modify: `src/lambda_session_finalize.py` (the confirmation email's topic table gains a *Where* column from `group_day`, blank when unknown)
- `fieldsight-ui` (separate repo): a "By location" toggle on the day view reading `location_groups`. Out of this repo's scope; note the payload contract in `BACKEND-CONTEXT.md`.

- [ ] **Step 1:** **Deferred 2026-11-06.** The email table's three-column header (AGENDA ITEM / ASSIGNED / DUE DATE) is literal on BOTH the email and the frontend's Preview & copy (owner, 2026-10-08). A *Where* column must land in both at once, so it waits for the UI line. Email column; byte parity between email and Preview & copy on one fixture (the check that has caught four divergences).
- [x] **Step 2:** (contract below, handed to the UI line, which owns fieldsight-ui) Write the `location_groups` contract into the UI repo's backend-context doc.

**`location_groups` contract (org-api `/timeline` rendered day, PR #1228):**
- Present only when the day has usable location markers AND the caller may see the whole day. A cross-user or site-clipped caller never gets it. Absent ≠ `[]`.
- `photo_groups` (older key) may be present while `location_groups` is absent; do not infer one from the other.
- Shape: `[{location: str|null, kind: "level"|"room"|"zone"|"block"|"other"|null, key: str|null, order: int, reason: null|"before_first"|"expired"|"no_time", topic_ids: [int], photos: [filename], unbound_photos: [filename]}]`, in visit order. A return to a place is a new group.
- `topic_ids` are the same response's `topics[].topic_id` (positions, valid only within that response).
- `photos` = photos bound to those topics; `unbound_photos` = photos bound to no topic, placed by their own time. Every photo appears once across all groups.
- Null-group headings the report uses: before_first "Before any location was announced", expired "Location not stated", no_time "Time not recorded".
- The day report's `sections` also carry a "Locations" `entries` section (title = place, note = topic titles); photos stay in the Photos section.
- Locations shows only places with topics: a place where only photos were taken does not split it, so it can show fewer groups than the payload's `location_groups`.

---

### Task 8: Measure again

- [ ] Re-run Task 1 two weeks after TEST deploy. Report: share of inspection days with markers, share of photos placed, share of topics placed, number of `other`-kind places (the normaliser's miss rate). Paste under §Findings.

---

### Task 9: The habit, and the button (owner + App)

Not code in this repo, and the thing that decides whether Tasks 2–7 show anything:

- [ ] **Working rule for site managers:** say the place when you enter it — "Level two", "Room one-oh-one" — before the first observation there. One sentence; the extractor already listens for it.
- [ ] ~~**GrandTime App:** a *Current location* quick button that records a marker (`{at, location}`) without speech~~ — **not built (owner, 2026-11-06: habit first).** A button that writes a place without speech needs typed text or GPS, and both break owner rules (no typing in capture; place comes only from speech). If the habit alone does not move the Task 8 re-measure (~2026-11-20), the fallback is a *reminder-only* button: press → the device prompts "say where you are" → the inspector speaks → the existing extractor records the marker. No backend change either way.

**Ruling 2026-11-06 (made under the owner's standing "pick the long-term option" rule; owner may overturn) on `_assemble_session_report` / `_assemble_day_report` `topics[].related_photos`:** no change. Rows are already site-clipped, `related_photos` are filenames of photos bound to topics the caller may see, and an exported report is meant to carry its photos. The only cross-site case is a mis-bound photo — that is the photo-binding defect, not this path.

---

## Findings

### TEST verification — 2026-11-06 (deploy run 42026544929, develop 0d8217b7)

Read-only invoke of `fieldsight-test-org-api` `/timeline` as the owner's alias user Ben_Lin_test2:
- 2026-10-27 (7 markers, 14 photos): 200; `photo_filenames`, `photo_groups` and `location_groups` present; 4 groups in visit order (before_first 9 topics / "Inside" `other` 5 / Level 5 2 / expired 4); all 14 photos appear once; sections `Summary, Actions, Closed today, Open Questions, Decisions, Locations, Photos`, with Locations headed "Before any location was announced / Inside / Level 5 / Location not stated".
- 2026-10-24 (no markers, 9 photos): 200; only `photo_filenames`; sections unchanged (no Locations).

Task 8 (measure again) is due about 2026-11-20.

### Task 1 — 2026-11-05, last 30 days, `scripts/measure_location_markers.py` (read-only)

C1 = the platform's own company (internal/owner testing); C2 = a customer company. The label map and the raw strings stay in a local file and are not committed.

| DB | Co. | topic-days | photo-days | marker-days | % of topic-days | % of photo-days | markers/day p50/p90 | topics placed | photos (on marker-days) | photos placed |
|---|---|---|---|---|---|---|---|---|---|---|
| prod | C1 internal | 17 | 6 | 7 | 41.2% | 50.0% | 3 / 4 | 23.1% | 46 (38) | 76.1% |
| prod | C2 customer | 3 | 2 | 0 | **0%** | **0%** | — | 0% | 8 (0) | 0% |
| prod | all | 20 | 8 | 7 | 35.0% | 37.5% | 3 / 4 | 21.1% | 54 (38) | 64.8% |
| TEST | C1 internal | 11 | 7 | 6 | 54.5% | 71.4% | 2 / 4 | 30.4% | 88 (59) | 12.5% |
| TEST | C2 customer | 2 | 2 | 1 | 50.0% | 50.0% | 2 / 2 | 25.0% | 9 (6) | 0% |

- `location_markers.locate` (day view) and `photo_binding.place_at` (binding, template report) agreed on every photo (0 disagreements on prod and TEST).
- No marker row sat on a day without live topics.
- **The sample is tiny** (20 topic-days on prod in 30 days, 2 companies). The only customer days on prod (3) carry **no marker**. Every marker on prod comes from internal testing.
- Photos on marker-days: prod 35 of 38 placed. TEST only 11 of 65: 49 were taken more than `CARRY_MINUTES` (30) after the last marker, 5 before the first. On TEST he stops re-announcing the place while still photographing; the place expires, as designed.
- Distinct raw strings: 20 on prod, 12 on TEST. Shapes seen (described, not quoted; customer speech stays local): a level as a word or a digit; a level plus a narrower qualifier (an area, a grid line, a slab); **a level and a zone together, in either order**; ordinal floors, ground and basement; stairs, cores and blocks; and things that are not places in a building (a site or farm name, a city landmark, a bare "inside"-type word).
- **Multi-unit walk marks are not in `day_location_markers`.** "Now in Unit 2" lands in the extraction's `inspections[].entered` (`lambda_extract_session._clean_entered`, read by `units`/`walk_report`), and only reaches the day's markers if the model also emits a `location_marker` for it. A walk day can therefore be marker-less to Track L while its walk report has per-unit sections. Interface question for the multi-unit walk line (owner of `inspections[].entered`): should each `entered` mark also be written as a day marker, or should Track L read `entered` as a second source? Track L does not write it.

**Read against the 30 % rule (Step 3):** internal testing is above 30 %; the **one real customer is at 0 % (n=3)**. On real use the fix is the habit and the App button (Task 9) first. Tasks 2–7 are still built (owner decides the order), but they show nothing to a customer until markers exist on customer days.

Normaliser implications for Task 2: a level-and-zone compound must key the same whichever is said first; a qualifier after a level is a narrower place; vague words and site names are `other`, never a level.

## Out of scope

A `locations` table with ids and a hierarchy (Track C; `place_key` becomes the join key when it lands); Procore locations import (paused); tags by trade/element (Track C); any change to photo→topic binding tolerances (measured and settled in PRs #762/#773/#775).
