# Execution guide — the event-graph tracks (A, B, L; C and Procore parked)

**Date:** 2026-09-25 · **Audience:** whoever picks up one of these tracks in a fresh session.
**Branch this guide ships on:** `feature/event-graph-tracks` (based on `develop`).

## Read first, in this order

1. `docs/superpowers/specs/2026-09-24-event-graph-and-jev-assessment.md` — what exists, what is missing, why the order is what it is. §7 lists the decisions the owner has already taken; do not reopen them.
2. The plan for your track (below).
3. `CLAUDE.md` — especially *Testing* ("run the SQL against a real database"), *A stubbed seam is a seam nobody tested*, and the BUG-36 VPC rule.

## The tracks

| Track | Plan | Depends on | Session shape |
|---|---|---|---|
| **A** Jev shadow eval | `plans/2026-09-24-track-a-jev-shadow-eval.md` | nothing; needs `OPENROUTER_API_KEY` | one session, ~1–2 weeks, read-only, offline scripts |
| **B** stable identity + decision records | `plans/2026-09-24-track-b-stable-identity-and-decision-records.md` | nothing | one session, ~2–4 weeks, migration + writer + endpoints; deploy to TEST |
| **L** topics and photos by location | `plans/2026-09-25-track-l-photos-and-topics-by-location.md` | nothing (pure read-time) | one session, ~1–2 weeks; Task 1 first, it may re-order the rest |
| C | edges, claim_type, locations table, tags, Procore blocks | A's findings + B landed | not started; do not begin until A's findings doc exists |
| Procore | `runbooks/2026-09-24-procore-sandbox-setup.md`, `scripts/procore_probe.py` | sandbox credentials | **paused by the owner 2026-09-25**; run the probe only when credentials arrive |

A, B and L can run in three sessions at once. They touch different files:
A is `scripts/jev_eval/*` + `src/systemone_client.py`; B is migrations, `lambda_item_writer`, repositories, `deleted_predicates`, `lambda_org_api` confirm/reject; L is `place_normalise`, `location_grouping`, `report_sections`, `lambda_session_report`, one function in `lambda_org_api`. The one shared file is `lambda_org_api.py`; B edits the suggestion endpoints (~line 6324–6460), L edits `_photo_groups` (~line 7185). Rebase onto `develop` before opening a PR and the merge is trivial.

## Branching and PRs (repo convention)

- Start from `develop`: `git fetch origin develop && git checkout -b feature/<track-short-name> origin/develop`. Bring the plans in by merging `feature/event-graph-tracks` or cherry-picking its doc commits; they are documentation only.
- PR to `develop`. `test.yml` runs the full suite including integration tests against a pgvector Postgres container; `deploy.yml` deploys the TEST stack on merge.
- **Migration numbers:** `develop` is at `0067` as of 2026-09-25. Take the next free number at merge time and expect to renumber once (it has happened twice this month).
- Prod (`main`) is not touched by any of these tracks until the owner says so. Track B's flags stay off in prod by default.

## Session-start prompt (copy, fill the track letter)

> Work on Track **X** of the event-graph programme in `benzn-tech/fieldsight-pipeline`.
> Read `docs/superpowers/plans/2026-09-25-execution-guide-event-graph-tracks.md`, then the
> assessment spec it names, then the Track X plan. Execute the plan task by task with
> `superpowers:subagent-driven-development`. Branch from `develop` as `feature/<name>`; PR to
> `develop`. Every task's tests must run for real (no `importorskip` skips hiding a missing
> dependency — CI installs numpy, python-docx, pglast). For any SQL, add an integration test
> under `tests/integration/`. Report each task's outcome in the plan file's checkboxes and
> commit the plan with the code. Do not reopen decisions listed in the assessment's §7.

## Rules that apply to all three

- **Measure before you change a prompt, and run the same config twice.** Track A is built on this; L's Task 6 and B's carry-forward floor are where it bites elsewhere.
- **No customer text in fixtures that get committed.** A's exports and L's Task 1 output are gitignored; commit counts, not rows.
- **Nothing in A writes.** Nothing in L writes to Aurora except what `lambda_org_api` already wrote. B writes, and every B write has a real-Postgres test.
- **A guard that logs nothing when disabled is indistinguishable from one that is broken** — the item-writer already says this; keep saying it.
- **Do not put a model identifier in commits, PR titles or code comments.**

## Hand-offs between tracks

- A → C: the findings doc (with its pre-registered decision rule) decides whether Jev replaces, augments, or is not adopted for each gate.
- B → A: once `decision_records` lands, A's Task 1 export switches to `decision_records.list_for_eval`; until then it reads the suggestion tables directly.
- B → Procore Block D: `stable_id` is the `origin_id`.
- L → C: `place_normalise.place_key` becomes the join key when the `locations` table exists; the read-time grouping does not change.

## What a track is done when

- A: `counts.json` committed; findings doc committed with the rule dated before the results; the hand-read disagreement sample; one recommendation per set.
- B: a ticked action item survives its session's final pass on TEST with the same `stable_id`; a question stays answered after re-extraction; `decision_records` contains rejected verdicts and human outcomes; no read path returns a superseded topic.
- L: Task 1 and Task 8 numbers in the plan; a TEST day with markers renders a *Locations* section in the assembled report, location headings in the generated docx, and `location_groups` in the day payload; a day without markers renders exactly as before.
