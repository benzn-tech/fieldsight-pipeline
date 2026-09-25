# Track L — The report reads level by level: topics and photographs grouped by where he was

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

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

- [ ] **Step 1:** Over the last 30 days per company: days with any topic; days with ≥1 marker; markers per day (p50/p90); distinct raw location strings and their counts (this is the normaliser's input); photos per day and the share that `locate()` places (non-null) — reuse `location_markers.locate` and `extract_base_time_from_filename` so the number is what the payload would show.
- [ ] **Step 2:** Print the top 50 raw location strings. Keep this list; Task 2's regexes are written against it, not imagined.
- [ ] **Step 3:** If fewer than 30 % of inspection days carry a marker, say so at the top of this plan and route the owner to the habit + App button (Task 9) before Tasks 3–7 are built. The code will still be built; the order of value changes.

---

### Task 2: A place normaliser

**Files:**
- Create: `src/place_normalise.py` (pure; no I/O)
- Test: `tests/unit/test_place_normalise.py`

**Interfaces:**
- `normalise(text) -> {"kind": "level"|"room"|"zone"|"block"|"other", "value": str, "label": str, "raw": str}`; `same_place(a, b) -> bool`; `place_key(text) -> str` (what groups are keyed on).

- [ ] **Step 1: Rules, from Task 1's list.** English and Chinese: `level|lvl|l|floor|storey` + ordinal/number (`one`, `1st`, `ground` = 0, `一楼`, `二层`); `room|rm|unit|apt` + code (`101`, `1.01`, `G02`); `zone|area|block|wing|stair|grid` + token; compass words as qualifiers (`east`, `north`). Output a canonical `label` ("Level 1", "Room 101", "Zone B east"). Anything unrecognised is `kind: other` with `value` = casefolded, whitespace-collapsed text (`content_hash.normalize`).
- [ ] **Step 2: Aliases.** `place_key` consults an optional alias dict (from `name_aliases` rows with `kind='other'` and a `place:` prefix, loaded by the caller) so a company can pin "the new block" → "Block C" without a code change. No new table.
- [ ] **Step 3: Tests** from real strings: `"Level one"`, `"L1"`, `"first floor"`, `"一楼"` → same key; `"Room 101"` vs `"Room 1.01"` → same; `"Level 1"` vs `"Level 1 east"` → **different** (a qualifier is a narrower place, not a spelling); `"the kitchen"` → `other`; an empty string raises.

---

### Task 3: Place topics, not only photos

**Files:**
- Create: `src/location_grouping.py` (pure)
- Test: `tests/unit/test_location_grouping.py`

**Interfaces:**
- `group_day(markers, topics, photos, *, aliases=None) -> list[Group]` where `Group = {"location": label|None, "kind", "order", "topic_ids": [...], "photos": [filename...], "unbound_photos": [...]}`, in first-visit order.
- `place_of(markers, hhmm) -> label|None` wrapping `location_markers.locate` + `place_normalise`.

- [ ] **Step 1: Topic placement.** A topic's place is `place_of(markers, start_of(time_range))`; a topic that starts before the first marker is `None`. A topic whose window spans two markers stays with the place it **started** in — he was there when he began talking about it; the next marker is a new place, not a split of this topic.
- [ ] **Step 2: Photo placement.** A photo bound to a topic (`related_photos`) follows its topic's place. A photo bound to no topic is placed by its own time with `place_of`, into the group's `unbound_photos`. A photo bound to two topics goes with the earlier topic and is not listed twice.
- [ ] **Step 3: Consecutive visits to the same place merge**; a return to a place after another place is a **new** group (Level 1 → Level 2 → Level 1 renders three groups, in that order). Say so in the docstring: the day is a walk, and "back on Level 1" is what he did.
- [ ] **Step 4: Tests** on a hand-built day: three markers, five topics (one before any marker, one spanning two), eight photos (two unbound, one double-bound). Assert order, membership, and that every photo appears exactly once across all groups.

---

### Task 4: The day payload carries the grouping

**Files:**
- Modify: `src/lambda_org_api.py` (`_photo_groups` → delegate to `location_grouping.group_day`; add `shape["location_groups"]`)
- Test: `tests/unit/test_org_api_location_groups.py`

- [ ] **Step 1:** Keep `photo_groups` shape byte-identical for existing clients (derive it from `group_day`), and add `location_groups` = the full `Group` list with `topic_ids`. Same `None` semantics, same cross-user gate, same never-raise wrapper.
- [ ] **Step 2:** Test the gate (site-clipped caller: neither key), the no-marker day (neither key), and a marker day (both keys, and `photo_groups` equals what the old code produced on the same fixture — replay, do not re-derive).

---

### Task 5: The assembled report gets a Locations section

**Files:**
- Modify: `src/report_sections.py` (`build` gains `_locations(topics, report, markers)`; `_photos` stays)
- Modify: `src/lambda_report_generator.py` / wherever `report_sections.build` is called for a day (pass markers; the nightly generator reads S3, so it takes markers from the day's extraction artifact `location_markers`, not Aurora)
- Test: `tests/unit/test_report_sections_locations.py`

- [ ] **Step 1:** `kind: "entries"` section titled *Locations*: one entry per group — heading = label (or "Before any location was announced"), then the group's topic titles, then its photo keys (same `{name, key}` shape `_photos` builds, so the docx renderer already knows it).
- [ ] **Step 2:** Section is dropped when there are no markers (the `build` rule: no empty headings).
- [ ] **Step 3:** Test: a report with markers renders the section between *Safety* and *Photos*; without markers the section list is unchanged (assert on the exact list of titles a fixture produced before this change).

---

### Task 6: The generated (template) report places sections under locations

**Files:**
- Modify: `src/report_template.py` (`_covers_block` offers the topic's place beside each ref), `src/lambda_session_report.py` (`_place_photos` and section assembly honour a `[at: Level 1]` line the same way `[covers: t0]` is honoured)
- Test: extend `tests/unit/test_session_report_covers*.py` (whatever #925 named them) with the location variant

- [ ] **Step 1:** In the offer block, each topic line gains its place when known: `- t0  Level 1  Ductwork above ceiling  (3 photographs)`. The instruction asks each section to end with `[covers: …]` **and**, when the covered topics share a place, `[at: <label>]` copied verbatim from the offer.
- [ ] **Step 2:** Worker: parse `[at: …]` with the same strip-and-verify shape as `_COVERS_RE`; a label not in the offer is dropped (never invented — #925's rule for refs). Sections carrying `[at:]` are emitted under a location heading in first-visit order; sections without one keep today's order after them. Photos follow their section exactly as today.
- [ ] **Step 3:** Count and log `sectionsPlacedByLocation` next to `photosPlaced`; a prompt containing the request is not a model that obeyed it.
- [ ] **Step 4:** Docx test in #925's style: count headings and `<a:blip>` under each location heading on a rendered document. Run the mutation set from that PR plus: `[at:]` label not in offer → dropped; two sections same label → one heading.
- [ ] **Step 5: A/B on TEST with `scripts/extraction_ab.py`'s discipline** (same config twice): does adding `[at:]` to the prompt change `photosPlaced`? It must not go down.

---

### Task 7: Email and Today view

**Files:**
- Modify: `src/lambda_session_finalize.py` (the confirmation email's topic table gains a *Where* column from `group_day`, blank when unknown)
- `fieldsight-ui` (separate repo): a "By location" toggle on the day view reading `location_groups`. Out of this repo's scope; note the payload contract in `BACKEND-CONTEXT.md`.

- [ ] **Step 1:** Email column; byte parity between email and Preview & copy on one fixture (the check that has caught four divergences).
- [ ] **Step 2:** Write the `location_groups` contract into the UI repo's backend-context doc.

---

### Task 8: Measure again

- [ ] Re-run Task 1 two weeks after TEST deploy. Report: share of inspection days with markers, share of photos placed, share of topics placed, number of `other`-kind places (the normaliser's miss rate). Paste under §Findings.

---

### Task 9: The habit, and the button (owner + App)

Not code in this repo, and the thing that decides whether Tasks 2–7 show anything:

- [ ] **Working rule for site managers:** say the place when you enter it — "Level two", "Room one-oh-one" — before the first observation there. One sentence; the extractor already listens for it.
- [ ] **GrandTime App:** a *Current location* quick button that records a marker (`{at, location}`) without speech — same shape, same table, arrives with the session. Spec it in the App repo referencing `day_location_markers`; the backend needs no change if the App writes it through the session's existing metadata channel.

---

## Findings

_(filled by Task 1 and Task 8)_

## Out of scope

A `locations` table with ids and a hierarchy (Track C; `place_key` becomes the join key when it lands); Procore locations import (paused); tags by trade/element (Track C); any change to photo→topic binding tolerances (measured and settled in PRs #762/#773/#775).
