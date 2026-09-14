# Reports over any stretch of the day

**Date:** 2026-09-15
**Status:** design, awaiting owner review
**Supersedes:** `Dropbox/AI/spec-a-report-over-a-window-of-the-day-2026-09-14.md`
(draft by another session; failed review, findings in its §8). That file is not
in the repo. Its §8 findings are adopted here and its §8.10 order is the
starting point of §9 below. Where the two disagree, this document is newer and
records why.
**Sibling constraint:** `Dropbox/AI/spec-a-topic-knows-when-it-happened-2026-09-13.md`
§9 — `topics.occurred_at` is NULL on every row and must not be written until
`lambda_ingest`'s reader is fixed. Nothing here writes or reads it.

---

## 1. What the owner asked for

In their words, across 2026-09-13/14:

- "我选择 9:00–11:30 的内容/topics 生成一份符合 personal meeting template 的报告"
  — a report over a chosen part of a day, beyond the fixed daily/weekly/monthly.
- The template is pluggable: a company picks or reshapes it in the Library
  (company A keeps today's sections, B drops Health & Safety, C strips anything
  about cost).
- Nothing useful may be silently limited or hidden by the structure.
- Whatever splits the day into selectable stretches must **not** slow the
  confirmation email; it may run in the background after content is on the site,
  "until it is done".
- A worker must be able to generate a report of their own day.

## 2. Decisions already made

| # | Decision | By | When |
|---|---|---|---|
| D1 | A day is shown as **recording blocks** (split on silence between recorded segments) containing **topics**; free dragging of a time range stays available | owner | 2026-09-14 |
| D2 | Block gap threshold **600 s** (10 min); long-block threshold **5400 s** (90 min) | owner | 2026-09-14 |
| D3 | Both thresholds are **system configuration**, not customer-facing | owner | 2026-09-14 |
| D4 | They are **separate** from `session_scope.SESSION_GAP_MINUTES = 15`, which is not touched | owner | 2026-09-15 |
| D5 | Blocks are computed **in the background**, never on the finalize/email path | owner | 2026-09-15 |
| D6 | Template storage and versioning are designed here but are a **separate later phase** (reconciles "hold the schema form" of 2026-09-13 with the 2026-09-14 request) | owner | 2026-09-15 |
| D7 | Workers may generate reports of **their own** day | owner | 2026-09-15 |

## 3. What exists today (verified 2026-09-14/15 against `origin/develop` 06a9837 and `fieldsight-ui` `origin/dev`)

### 3.1 Per-meeting reports

- `POST /api/org/sessions/{id}/report/preview`, `POST .../report`,
  `GET .../report/status`. Assembly is `_assemble_session_report`: rows from
  `_day_report_rows(conn, caller, folder, date)` (shared since #836, handles
  multi-device joiners), filtered to one session via `session_scope.session_ref`
  on `source_s3_key`, minus active redactions and `work_class = 'non_work'`,
  intersected with the caller's chosen `topicRowIds` (`_selected_topic_row_ids`,
  capped at 200).
- Frontend (`feat/choose-what-the-report-covers`, 108de34, on `dev`, **not on
  `main`**): inside ONE meeting's review modal, "Cover only HH:MM–HH:MM" ticks the
  topics whose `time_range` overlaps the window. Overlap, not "starts inside";
  unplaceable topics are never picked by a window; end-before-start is
  unplaceable. Client-side only.

### 3.2 The session-report worker produces no text

`lambda_session_report` (non-VPC, `Timeout: 300`) reshapes the already-extracted
topics into Word via `lambda_meeting_minutes.generate_word_document`. It makes
**no model call** and carries **no model key**. Its IAM can read
`session_report_requests/*`, `users/*/pictures/*`, `redactions/*` — **not
`transcripts/*`**.

`templateId` is written into the request artifact by org-api and read by
nothing. Executed, not inferred: the worker's `_content_to_minutes` was run with
three artifacts differing only in `templateId` (two ids and absent) → one
distinct output; `templateId` occurs 0 times in the worker source.

Everything in the worker and the status route keys on `sessionId`: `_doc_key`,
`_session_was_deleted` (returns False when there is no id — a day artifact
would skip the deletion check entirely), and the status route's result key and
removal check (predecessor §8.2, re-verified).

### 3.3 Topics carry a clock as text

`topics.time_range` is free text in the device's wall clock (`"08:19 – 08:48"`),
no date. Prod, 452 rows: **370 parseable, 80 empty, 2 corrupted** (control bytes
where the dash was). `topics.occurred_at`: **NULL on all 452**. There is no
`session_id` column; the session comes from parsing `source_s3_key`.

### 3.4 The Library is browser-local

`fieldsight-ui/scripts/api/template-store.js` stores templates in
`localStorage` (`fs_templates_v1`). Shape:
`Template {id, scope: org|personal, report_type, active, owner_user_id, title,
description, versions[]}`, `Version {id, schema, created_at,
created_by_user_id, change_note}`, `Schema {sections: [{title, kind, fields,
prompt_hint}]}`, `kind ∈ {narrative, list, table, kpi, photos}`. Versions are
append-only (`updateSchema` appends; restore appends a copy). Version ids are
`ver-<timestamp>-<random>`. **The backend has no template storage** — a
template exists in one browser; a report's `templateId` names nothing the
server can read.

### 3.5 `SESSION_GAP_MINUTES = 15`

Originally meant to assemble sessions upstream from chunk gaps
(`2026-07-23-session-continuity-design.md`). **That assembly was never built**
(`SESSION_MAX_MINUTES` does not exist in `src/`); sessions are now minted on
the device. Three live uses remain:

| Use | Where | Prod | Observed |
|---|---|---|---|
| Infer a session ended when no End arrived | `lambda_finalize_claim` (`INFER_IDLE_CLOSE=true`) | on | 22 of 48 finished sessions in 30 days; 14 of those were recordings left running ≥ a day, 6 were zero-length |
| Multi-device group settles after members go quiet | `session_group.list_due` (`ENABLE_GROUP_MERGE=true`) | on | 1 group ever |
| Adjacent sessions shown as one picker block | `GET /sessions` → `gap_minutes`, `timeline.js` reads `block` | on | every picker render |

A device-sent End is grace 0 and never waits on it (26 sessions, median 1.8 min
from last chunk to processed).

### 3.6 Permissions

Backend: the session report routes have no role gate; `_resolve_org_media_folder`
under `GRADED_ROLES` pins a worker to their own folder and returns 403 "no
folder mapping for your account" when `folder_name` is NULL.
Frontend: `fs-globals.js` gives `worker` `report:view:self` and no
`report:create`; `GenerateReportButton` requires `FS.can(caller,
P('report','create'))`. Roles in `HIERARCHY_ROLES` inherit every lower role's
permissions, and an unscoped requirement matches a grant of any scope — so
project managers already pass through site_manager's grant.

## 4. Measurements behind the design

All on one account (Ben_UCPK2). Validate on others before tuning D2.

### 4.1 Silence blocks versus topics

Segments are transcript objects; start = filename base time + VAD offset
(`transcript_utils.compute_segment_base_time`), end = start + (`to` − `off`).

| Day | Gap > 10 min | Topics |
|---|---|---|
| 09-10 (worn all day, one 17.8 h session) | 08:19–15:17 (418 min), 15:36–16:34, 20:10–20:14, 22:40–22:41 | 7, each a distinct discussion |
| 09-02 (intermittent) | 10:55–11:35, 17:14–17:47, 17:57–18:15 | 6; the first **four** are one meeting's agenda items; 17:14–17:28 has audio but no topic |
| 09-03 (worn all day) | 07:02–15:09 (487 min), 15:28–16:37, 17:01–17:46 | — |

Neither signal alone works: gaps are meetings on intermittent days and useless
on all-day days; topics are meetings on all-day days and over-split on
intermittent ones, and drop untopic'd audio. Hence D1's two levels and the
90-minute rule. At 1–2 min thresholds 09-10 yields 36–48 selectable edges, half
of them blocks under five minutes.

### 4.2 Prototype of scope + template + one call

`scratchpad/slice_and_report.py`, 09-10 09:00–11:30, `meta/muse-spark-1.3-contributor`:

- 229 transcript objects in the day → **70** overlapping the window.
- Template with six sections → all six headings present, in order, none added;
  `spk_N` printed 0 times. 67.7 k prompt tokens, **$0.008**.
- Same slice with `excluded_subjects: commercial` → `$` 2→0, `price` 3→0,
  `cost` 4→0, `claim` 1→0. The remaining `subcontractor`/`procurement` hits
  were read sentence by sentence: work quality and a staffing move, not money.
  Keyword counts over-report; "Price" was also a company name.
- A second run overwrote the first run's fingerprint file while the first run's
  text was being rendered — the PDF footer would have named the wrong template.
  → §5.6's atomicity rule.

## 5. Design

### 5.1 Scope

The request carries one of:

```
{ "scope": "session", "sessionId": "...", "topicRowIds": [...]? }            today
{ "scope": "day",     "date": "...", "user": "<folder>",
  "window": { "from": "HH:MM", "to": "HH:MM" }?, "topicRowIds": [...]? }      new
```

`window` absent = whole day. Both `window` and `topicRowIds` only **narrow**:
the server assembles the day's rows exactly as the session route does (ACL,
joiners, redactions, non_work, deleted sessions), then intersects. A client
cannot widen scope by naming ids or times.

Clock: `from`/`to` are the device's wall clock, the same clock `time_range` and
transcript filenames carry. No timezone conversion anywhere (sibling §9.2).
A window crossing midnight is refused (400) — the data cannot represent it.

The artifact records `sessionIds[]` actually in scope (predecessor §8.2) so the
worker can check every one against the deletion mirror.

### 5.2 Assembly

One core, two thin routes (predecessor §8.6): `_assemble_report(conn, caller,
folder, date, session_id=None)` built on `_day_report_rows` and
`build_day_sessions` — which already applies the exclusions and orders sessions
by authoritative start. Within a session, topics order by **parsed** `time_range`
start, unparseable last (predecessor §8.4 — never the lexical `ORDER BY
time_range`, never `created_at`).

Day routes: `POST /api/org/days/{date}/report/preview`,
`POST /api/org/days/{date}/report`, `GET /api/org/days/{date}/report/status`.
Day title is `"{date} · {sites}"`, editable; site names are the set, not
`rows[0]` (predecessor §8.3). The 200-id cap stays for hand-picked subsets; an
untouched day sends no ids. The 400's reason is surfaced (predecessor §8.8).

Keys stay under the existing prefixes with a `day/` segment —
`session_report_requests/{folder}/{date}/day/{requestId}.json`, likewise
results and `.docx` — so no result-side IAM or S3 trigger change
(predecessor §8.5).

### 5.3 Generation

The worker gains a text-producing step. Inputs, in order of authority:

1. **Transcripts in scope.** Objects under `transcripts/{folder}/{date}/`
   whose segment overlaps the window (overlap, as in §4.2), excluding objects of
   deleted sessions.
2. **Exclusions clipped out of the transcript.** Redaction and `non_work` are
   topic-level; raw transcripts contain everything. Turns falling inside an
   excluded topic's `time_range` are removed **before** the prompt is built.
   An excluded topic with no parseable `time_range` cannot be clipped, so its
   whole session's transcript is dropped from the input and the result says so
   (`droppedSessions: [{id, reason: "unplaceable_exclusion"}]`). Fails closed.
   Without this, a redacted conversation reaches the report through the
   transcript even though its topic is hidden — the frozen-copy leak class.
3. **Structured items** from Aurora for sections that are data, not prose.

Section kinds (Library schema, §3.4):

| kind | Source | Written by model |
|---|---|---|
| `narrative`, `list` | clipped transcript | yes — `title` + `prompt_hint` rendered into the prompt |
| `table` | action_items / findings in scope | no |
| `kpi` | recordings, duration, photo counts in scope | no |
| `photos` | topic photos in scope, within the existing budget | no |

The model never re-types a table or a number the database already holds.

Prompt: one fixed skeleton (versioned in code as `PROMPT_SKELETON_VERSION`),
into which the template injects only its sections and, when present, a
`Leave out` block. Every template ends with a catch-all section
("Anything else" — whatever matters later and fitted no heading); **excluded
subjects apply to it too**, or excluded content reappears unlabelled. The
skeleton says: plain sentences; `spk_N` labels are not identities and are never
printed; a section with nothing behind it says so rather than padding; spoken
figures marked provisional where they were; report the work, not remarks about
people. After rendering, the prompt must contain no `{`/`<` placeholder — muse
copies placeholders verbatim.

Until §6 ships there is no stored template, so generation uses a **built-in
default template** held in code with its own `content_hash`, making the
fingerprint meaningful from day one. `templateId` stays accepted and is
recorded, but only resolves once §6 exists.

Worker infrastructure (each item is a known trap in this repo):

- IAM: `s3:GetObject` on `transcripts/*`, and `transcripts/*` added to the
  `ListBucket` `s3:prefix` condition — without the latter a missing key answers
  AccessDenied, not NoSuchKey.
- Model env: the same block `ReportGeneratorFunction`, `MeetingMinutesFunction`
  and `AskAgentFunction` carry (`QWEN_API_KEY: !If [UsesSeparateChatVendor, …]`,
  `QWEN_BASE_URL`, `QWEN_MODEL`, `QWEN_MODEL_NONTHINKING`). Non-VPC, so outbound
  is allowed.
- `max_tokens` generous; muse spends reasoning tokens from the same budget and
  returns HTTP 200 with empty content when it runs out. Empty content is a
  recorded failure, never a blank document.
- `Timeout: 300` must be measured against a whole worn day (~195 k prompt
  tokens in the 09-10 one-shot) before the day scope is enabled.
- A grep for the call path must show the step is reached; a test that stubs the
  model must also assert the stub was called.

### 5.4 Recording blocks, in the background

A new in-VPC function, triggered by an EventBridge rule on `transcripts/`
Object Created in the ingest bucket — the same template-declared pattern as
`SessionActivityFunction`, not the hand-wired `scripts/wire-s3-events.sh`.
It is **not** added to `SessionActivityFunction`: that function feeds the
finalize path (D5).

On trigger, for the object's `(folder, date)`:

1. **Throttle**: skip if this day was computed less than 90 s ago. Every
   transcript landing would otherwise consume account Lambda concurrency shared
   with finalize — the one real coupling to email latency.
2. List `transcripts/{folder}/{date}/`, derive segment start/end from names
   (`transcript_utils`), merge where the gap ≤ `REPORT_BLOCK_GAP_SECONDS`.
3. Resolve the folder to a user with `users.get_by_folder_name_global`
   (`folder_name` is globally unique, migration 0012); an unresolvable folder
   is logged and skipped, never guessed. Upsert
   `day_recording_blocks(user_id, report_date, blocks jsonb, gap_seconds,
   long_block_seconds, source_object_count, computed_at)`. The row records the
   thresholds it was computed with, and `computed_at` is what step 1 reads.

No model call; one LIST and arithmetic. A day still being recorded is simply
recomputed as transcripts arrive — "until it is done".

Infrastructure for this function:

- IAM: `s3:ListBucket` on the ingest bucket with `s3:prefix` `transcripts/*`.
  It reads object names only, so no `GetObject`.
- A new Lambda and a new EventBridge rule are new CloudFormation resources. The
  deploy role `github-actions-fieldsight-deploy` must be checked with
  `simulate-principal-policy` for every action they need **before** the PR
  merges; a missing permission fails the whole stack with CREATE_FAILED and rolls
  it back.
- Migration for `day_recording_blocks` merges to `main` only when this step is
  ready; migrations on `main` run against the prod database.

`GET /api/org/sessions` gains `recording_blocks` (absent when no row yet) with
each block's `from`, `to`, `minutes`, `selectable_as_whole` (= minutes ≤
long-block threshold) and the topic row ids inside it. The UI renders blocks
when present and today's sessions/topics otherwise; it never waits.
`gap_minutes` (the 15-minute session grouping) is left as is.

Picker behaviour (D1):

- Block ≤ 90 min: selectable as a whole → `window = [block.from, block.to]`,
  which also covers untopic'd audio inside it.
- Block > 90 min: topics are the unit.
- Free drag always available. A range that cuts a topic prompts "include the
  whole topic?" — never a silent expansion.

### 5.5 Thresholds as system configuration

| Env | Template parameter | Default | Read by |
|---|---|---|---|
| `REPORT_BLOCK_GAP_SECONDS` | `ReportBlockGapSeconds` | `'600'` | block function only |
| `REPORT_LONG_BLOCK_SECONDS` | `ReportLongBlockSeconds` | `'5400'` | block function only |

Wired in all three places like `GroupMaxSpanSeconds` / `EvidenceWindowSec`:
`Type: String` parameter; `ENV: !Ref Param` on the one reader;
`"Param=${{ vars.PROD_… || '600' }}"` in `deploy-prod.yml` and `TEST_…` in
`deploy.yml`. One reader only — org-api serves the values stored on the row, so
two functions can never disagree (the `GROUP_MERGE_CAP` warning in
`test_template_workflow_parameter_wiring.py`). That test file gains the same
three assertions for these two. Seconds in storage, minutes in the UI.

For contrast, `DAILY_TRANSCRIPT_LIMIT` is read in code but absent from the
template and workflows; the deployed report-generator carries no such variable,
so it runs its code default and cannot be changed without a code deploy.

### 5.6 Fingerprint

Written in the result JSON and the document footer, **in the same write as the
result, keyed by `requestId`** — never a separate file that a later run can
overwrite (§4.2):

```json
"generated_from": {
  "scope": { "kind": "day", "folder": "...", "date": "...",
             "window": { "from": "09:00", "to": "11:30" },
             "sessionIds": ["..."], "topicRowIds": null,
             "transcriptObjects": 70, "droppedSessions": [] },
  "blocks": { "gap_seconds": 600, "long_block_seconds": 5400 },
  "template": { "id": "builtin-default", "version": 1, "content_hash": "sha256:..." },
  "prompt_skeleton_version": 1,
  "model": "meta/muse-spark-1.3-contributor",
  "generated_at": "...", "usage": { ... }
}
```

### 5.7 Permissions

- Server: unchanged rule. Day routes resolve the folder through
  `_resolve_org_media_folder`; a worker gets their own day only; joiner and
  cross-user clipping as #836 defines.
- UI: add `P('report','create',SCOPES.SELF)` to `worker` in `fs-globals.js`.
  Higher roles already inherit wider grants, so nobody else changes.
- A worker without `folder_name` gets the server's 403; the UI must say "your
  account has no recording folder" rather than "report did not start".

## 6. Later phase — template storage and versioning (D6)

Designed now, not built until the schema-constraint testing has a result.

- **Storage**: Aurora `report_templates(id, company_id, scope, owner_user_id,
  report_type, title, active, created_at)` and `report_template_versions(id,
  template_id, version int, schema jsonb, content_hash, created_by, change_note,
  created_at)`. Versions are insert-only; there is no UPDATE path. Restore
  inserts a copy. Soft delete on the template keeps every version.
- **Identity**: `template_id` names; `version` is assigned by the server, per
  template, monotonic — never typed by a user; `content_hash` proves.
  A report points at `(template_id, version)` and records the hash; the version
  it points at can never change.
- **Hash**: sha256 of canonical JSON (sorted keys, compact separators, UTF-8).
  Not `content_hash.py`: that normaliser casefolds and collapses whitespace for
  matching safety items, and a heading's capitalisation is visible in a report.
- **Schema additions**: `excluded_subjects: [{label, covers}]` (removed
  everywhere, including the catch-all) and a required catch-all section. Removing
  a section and excluding a subject are **different operations** and the Library
  UI must make the user choose: a removed H&S section still lets safety content
  appear under other headings; an excluded subject does not.
- **Migration**: the frontend's existing API shape (`PATCH
  /api/templates/{id}/schema`, `GET …/versions`, `POST …/versions/{vid}/restore`)
  becomes real; templates held in `localStorage` are uploaded once, per browser,
  with a notice.

## 7. What could go wrong

| Risk | Mitigation |
|---|---|
| Redacted or non-work speech reaches a day report through raw transcripts | §5.3 clipping before prompt build; fail closed on unplaceable exclusions; test with a redacted topic inside the window |
| A session deleted after enqueue is rendered or mailed | artifact carries `sessionIds`; worker checks every one (predecessor §8.2/§8.7) |
| Block function starves finalize of concurrency | 90 s per-day throttle; separate function; alarm on its Throttles metric (throttled calls log nothing) |
| New resources roll the whole stack back | deploy-role permissions verified with `simulate-principal-policy` before merge |
| A threshold "configured" but inert | three-place wiring + guard test; verify the deployed function's env after deploy |
| Whole worn day exceeds worker timeout | measure before enabling day scope; window scope is the default entry |
| Model returns empty content with HTTP 200 | treated as failure, logged with reasoning token count |
| Template placeholder copied into a customer report | post-render assertion that no `{`/`<` placeholder remains |
| Photo budget (4 per topic, 12 MB) runs out across a day | return `photosDropped` and state it in the document |
| Excluded-subject text still appears | review by sentence, not keyword; add the §4.2 exclusion case as a regression fixture |

## 8. Out of scope

- Changing `SESSION_GAP_MINUTES`, idle-close inference or group merge (D4).
- Why some sessions end without an End: 14 of the 22 were recordings left
  running ≥ a day and 6 were zero-length, so it is not a user-facing email-delay
  problem on current data.
- Site-wide or multi-person windows.
- Timezones; `occurred_at`.
- The `/reports` page's daily/weekly/monthly Generate panel (legacy gateway).
- The daily report's Summary being bullets joined into one run-on paragraph —
  a separate defect with its own fix.
- Procore integration (paused by the owner).

## 9. Order

Starts from predecessor §8.10.

1. ~~Joiners find and report on their meeting; shared `_day_report_rows`~~ — done, #836.
2. Worker and status learn `scope` / `sessionIds`; keys under `day/`; deletion
   check over every id. **Must land before any day generate route.**
3. `_assemble_report` core; day preview/generate/status routes with title,
   sites and ordering rules.
4. Frontend `scope`; "All day" opens the modal; `worker` gets `report:create:self`.
5. Generation step: transcripts IAM, model env, built-in template, clipping,
   fingerprint, empty-content handling; timeout measured on a worn day.
6. Recording blocks: migration, function, EventBridge rule, the two parameters
   and their guard test, `recording_blocks` in `GET /sessions`, two-level picker.
7. (Later phase) Template storage and versioning, §6.

Steps 2–4 change no output a customer sees until 4 ships. Step 5 is the first
that produces new text and should go to TEST behind its own flag.

## 10. Open questions

- How a live user's org role is mapped to the frontend role string. If a worker
  is not mapped to exactly `worker`, the §5.7 UI change does nothing. Check
  before step 4.
- Whether `Timeout: 300` suffices for a whole worn day (step 5).
- The 10/90 thresholds were measured on one account over three days. Recheck on
  another user and another device type before treating them as settled.
- When a day counts as "done" for blocks: currently never — each new transcript
  recomputes. Acceptable while computation is one LIST; revisit if it is not.
