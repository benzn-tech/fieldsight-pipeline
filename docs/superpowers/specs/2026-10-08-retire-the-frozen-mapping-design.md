# Sub-project 2 — retire the frozen mapping (design, 2026-10-08)

Status: owner said "开始" (2026-10-08), covered by the tenancy-roadmap autonomy grant. This
is roadmap decision **D5**: no frozen side-copies of the directory, and org-api publishes what
non-VPC components need. Master: `2026-10-05-tenancy-roadmap-design.md`.

## Where we are (inventory 2026-10-08, origin/develop)

`config/user_mapping.json` was hand-edited in August. **Nothing writes it.** Prod holds 8
devices (6 `site_manager`, 2 `worker`) and 5 sites. It has four live readers, all
**non-VPC**, and each falls back to `{}` silently when the file is missing:

| reader | reads | drives |
|---|---|---|
| report-generator (`load_user_mapping*`, `get_user_site_mapping`) | name, primary_site, sites, role; the `sites` block | the site/role stamped on daily reports, the role-aware prompt, weekly/monthly grouping by site, the weather site, and the device→name "Name Reference" prompt block |
| meeting-minutes (`load_user_mapping`) | device → name | speaker names in the minutes and the attendee list |
| extract-session (`load_sites`) | `sites[*].name` | fuzzy match of a spoken site name, recorded only (`declared_site.matched_site`) |
| orchestrator (`load_user_mapping`) | device → name | the S3 folder for realptt downloads. Missing file = media silently misfiled under the device id. |

Dead: `lambda_ingest.load_mapping` (no callers). Seven `CONFIG_KEY` env vars and three
exact-key read grants have no code behind them.

The orchestrator still runs on prod every 15 min plus nightly
(`fieldsight-prod-OrchestratorFunction{Sweep,Nightly}Event`, ENABLED). It has downloaded
nothing for 45+ days (memory: about 1830 invocations, zero downloads).

## Decisions

**M1. org-api publishes one machine-owned directory object**, `config/directory.json`. It
follows `config/site-coords.json` exactly: one writer, merge-and-skip-if-unchanged, never
raises, a daily republish task `{"task": "republish_directory"}` scheduled before the
05:00 reports, and an IAM grant on that single key.

Shape, keyed by identity rather than by name:
```
{"version": 1, "published_at": ISO,
 "people": {"<folder_name>": {"name": "First Last", "role": "<global_role>",
                              "company_id": "...", "primary_site": "<slug>|null",
                              "sites": ["<slug>", ...]}},
 "sites":  {"<slug>": {"name": ..., "location": ..., "client": ..., "company_id": ...}}}
```
- `primary_site` is the user's single open-site membership when there is exactly one, otherwise null.
- Archived users and sites are left out.
- Tenancy note: the file is read only by in-account pipeline lambdas, never served to a browser.

**M2. Readers switch to the directory, keyed by folder, with the old file only as a
transition fallback.** One small shared module, `directory.py` (`load()`, cached per
container), reads `config/directory.json` first. If that file is absent it reads
`config/user_mapping.json`, adapted to the same shape and logged once at WARNING. A
missing directory **and** a missing mapping is no longer silent `{}`: it logs ERROR.
- report-generator: role / primary_site / sites / site info come from `people[folder]` and `sites[slug]`. The device→name "Name Reference" block is dropped, because today's transcripts carry speaker labels and folder-named recorders, not device ids. The role vocabulary is now `global_role` (`site_manager`, `pm`, `worker`, `gm`, `admin`), and the prompt's role branches must accept it.
- meeting-minutes: the speaker name comes from `people[folder]["name"]`. Device ids are not translated; a legacy device id passes through unchanged.
- extract-session: the site-name fuzzy match uses `sites[*].name`, scoped to the speaker's company where `company_id` is known.

**M3. The orchestrator stops being a hazard.**
- Code: if the device → person mapping is unavailable, it refuses to file media and raises; it never writes under a device-id folder. It keeps reading `user_mapping.json`, because no directory source has device ids.
- Owner decision, recommended: disable the two prod orchestrator schedules (realptt has produced nothing for 45+ days). This is reversible.

**M4. Remove the dead.** Delete `lambda_ingest.load_mapping`. Drop `CONFIG_KEY` env vars and
exact-key grants that have no reader. Fix stale docstrings that still say item-writer bridges
through the mapping.

**M5. Retirement of the file itself.** It happens after one release with the directory live
on prod and **zero** `directory: falling back to user_mapping.json` WARNINGs for a week. The
fallback code is then removed and the owner deletes the object. The orchestrator is the only
remaining reader, so this waits on M3.

## Tests that must go red without the change
- The publisher emits people and sites from the DB (integration, real SQL), skips the write when unchanged, and never raises.
- The republish task writes the file.
- `directory.load()` prefers directory.json, adapts user_mapping, and logs ERROR when both are missing.
- report-generator: a daily report for `Deandre__Alberts` gets his role and site from the directory, not from a name match.
- meeting-minutes: the speaker name comes from the folder.
- The orchestrator refuses to file under a device folder.

## Final review notes (2026-10-08)

Shape change from review: `sites` is keyed by site **id** (slugs are unique per company only); each entry carries `slug`, and people's `primary_site`/`sites` hold ids. Legacy fallback uses `legacy:<slug>` ids. `primary_site`: one live membership, else the site with most topics by that person in 30 days (tie: latest), else null. `{"task": "republish_directory", "dry_run": true}` returns the built document without writing.

Accepted, not fixed:
- F7: publish runs before commit, so a failed commit or a concurrent request can leave the object briefly ahead of or behind the database; the daily republish repairs it. No S3 timeout is configured on the shared client (same as site coordinates).
- F9: weekly/monthly runs now make one LLM call per site that has members (previously unplaced people collapsed into one default bucket). Count `reports_by_site` after the first publish and watch the 900 s ceiling.
- F10: the role vocabulary change is intended (gm/admin/regional_manager now reach the prompt). `AccessDenied` on `directory.json` reads as absent and falls back with a WARNING; the "zero fallback WARNINGs for a week" gate is the backstop. A WARNING right after first deploy, before the first publish, is expected.
- Two sites sharing a slug across companies still share the slug-keyed `site-coords.json`, and a weekly site report whose slug was already written in the same run falls back to the site id for its path.
