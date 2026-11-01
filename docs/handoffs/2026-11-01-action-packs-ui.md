# Action packs: hand-off to fieldsight-ui

Backend branch: `feat/action-packs` (fieldsight-pipeline). Spec:
`docs/superpowers/specs/2026-11-01-background-action-agent-design.md` (§5 apply, §10 UI
contract, §14 rev 2, which wins where it disagrees). Every shape below was read from the code:
`src/lambda_org_api.py` (`action_packs_route` and the functions it calls),
`src/action_pack_view.py` (`shape_pack`, `shape_list_row`, `applicable_candidate`,
`validate_*`, `effective_fields`, `item_view`, `render_markdown`),
`src/repositories/action_packs.py` and `src/repositories/topics.py`.

All routes are under `/api/org`. Every error body is `{"error": "<message>"}`, sometimes with
extra keys named below. The examples use made-up ids.

## 0. What an action pack is

A person types an instruction ("check the 10-10 pour and tidy the open items about it"). The
backend runs it in the background for 1 to 5 minutes and stores a **pack**:

- a **reading**: how the run understood the instruction;
- a **conclusion**: findings, each citing refs;
- **actions**: proposed changes to Tasks (close, update, create) and drafts (RFI, email,
  note), each citing refs and carrying a verifier label;
- **sources**: one row per cited ref, with its excerpt.

Nothing changes in Tasks until a person applies an action, one at a time.

## 1. Feature flag

`ACTION_PACKS` on the backend: `off` | `internal` | `on`.

- `off` (the default, and anything unknown): **every** `/action-packs` route answers 404
  `{"error": "not found"}`, exactly like an unknown route.
- `internal`: only `platform_admin` callers reach the routes; everyone else gets 404.
- `on`: everyone with access.

The UI has no flag endpoint. Recommended approach: hide every entry point unless a probe
`GET /action-packs?state=running` returns 200. A 404 means "feature not available to this
user". Cache the probe for the session.

## 2. Datetimes, ids, ordering

- Ids are UUID strings.
- Datetimes are serialised with Python `str()`, **not** strict ISO: for example
  `"2026-11-01 02:14:07.512331+00:00"` (a space, not `T`). `new Date(s.replace(" ", "T"))`
  parses it. Dates (`source.date`) are `"YYYY-MM-DD"`.
- Values inside `target_snapshot` and `proposed` are JSON written by the worker, so they are
  ISO strings already (`"deadline": "2026-11-05"`).
- `actions` come in display order (`seq`). `sources` come ordered by `ref` **as text**
  (`R1, R10, R2, ...`). Look sources up by `ref`; do not rely on their order.

## 3. Endpoints

| Method + path | Body / query | Success |
|---|---|---|
| POST `/action-packs` | `{instruction, site_id?}` | 202 `{id, state: "queued"}` |
| GET `/action-packs` | `?state=&site_id=&archived=` | 200 `{packs: [...]}` |
| GET `/action-packs/{id}` | | 200 the pack |
| GET `/action-packs/{id}/sources/{ref}/snapshot` | | 200 `{ref, url, fetched_at, text, truncated}` |
| GET `/action-packs/{id}/export` | `?format=md` | 200 `{format, filename, content_type, content}` |
| POST `/action-packs/{id}/cancel` | `{}` | 200 `{id, state: "cancelled"}` |
| POST `/action-packs/{id}/archive` | `{archived: bool}` | 200 `{id, archived}` |
| POST `/action-packs/{id}/actions/{aid}/dismiss` | `{}` | 200 `{id, state: "dismissed"}` |
| POST `/action-packs/{id}/actions/{aid}/apply` | `{}` or `{fields: {...}}` | 200 `{id, state: "applied", applied_result}` |

A malformed pack id or action id, a wrong method, or a pack this caller may not read is
always 404 `{"error": "not found"}`. The backend never says whether a pack exists.

**Who can read a pack:** its creator (while the site is still in their scope), admin/GM of the
site's company, and the site's pm/site_manager. Any reader can archive, dismiss and try to
apply. Only the creator or a company admin/GM can cancel. Apply then checks the applier's own
rights on the task (§3.9).

### 3.1 Create: POST `/action-packs`

```json
{"instruction": "Close the open items about the 10-10 pour that the pour record settles, and draft an RFI for anything still unclear.",
 "site_id": "6f1c2a8e-3b7d-4c55-9a51-1f0e2b7c9d10"}
```

202:

```json
{"id": "0b9e4d2c-77a1-4f3e-bb0c-5d8f6a1e2c33", "state": "queued"}
```

Rules:

- Only `instruction` and `site_id` are accepted. Any other key gives 400
  `{"error": "unknown fields", "fields": ["..."]}`.
- `instruction`: required, 1 to 2000 characters after trimming. Otherwise 400
  `"instruction is required"` or `"instruction must be at most 2000 characters"`.
- `site_id`:
  - **Required when the caller can see more than one site**: 400 `"site_id is required"`.
  - Defaults to the caller's only site when they see exactly one.
  - Unknown, malformed or not visible to the caller: 404 `"site not found"`.
  - A pack covers **one** site in v1. Every entry point must resolve the site client-side.
  - A non-string `site_id` gives 400 `"site_id must be a string"`.
- 409 `{"error": "you already have a pack in progress"}`: the caller already has a pack that
  is `queued` or `running`. Only one is allowed per person. Show a link to the running pack
  (list with `?state=running`, plus `?state=queued`).
- 409 `{"error": "company_inactive"}`: the site's company is suspended or exited. This only
  fires while the backend's company-status enforcement is on.
- 503 `{"error": "the pack could not be started"}`: the backend could not hand the pack to
  the worker. The pack is already marked `failed`, so the one-active slot is free. Retry is
  fine.

### 3.2 List: GET `/action-packs`

Query parameters, all optional:

- `state`: one of `queued | running | ready | failed | cancelled`. Anything else gives 400
  `"unknown state"`.
- `site_id`: a UUID. A malformed value gives 400 `"site_id must be a UUID"`.
- `archived`: `false` (default: unarchived only) | `true` (archived only) | `all`. Anything
  else gives 400 `"archived must be true, false or all"`.

Newest first. **At most 50 rows; there is no paging parameter in v1.**

```json
{"packs": [
  {"id": "0b9e4d2c-77a1-4f3e-bb0c-5d8f6a1e2c33",
   "title": "10-10 pour follow-up",
   "instruction": "Close the open items about the 10-10 pour ...",
   "state": "ready",
   "failure": null,
   "site_id": "6f1c2a8e-3b7d-4c55-9a51-1f0e2b7c9d10",
   "site_name": "Riccarton Rd Apartments",
   "created_at": "2026-11-01 02:14:07.512331+00:00",
   "archived": false,
   "action_counts": {"proposed": 3, "applied": 1, "dismissed": 0, "stale": 1, "failed": 0}}
]}
```

- `title` is null until the run has planned (queued packs, early running packs). Show the
  instruction instead.
- `action_counts` always has all five action states.
- Counts and titles in the list are not filtered by the source re-check (§3.3, "removed
  sources"). The GET one view can show fewer actions than the counts say.

### 3.3 Get one: GET `/action-packs/{id}`

```json
{"id": "0b9e4d2c-77a1-4f3e-bb0c-5d8f6a1e2c33",
 "site_id": "6f1c2a8e-3b7d-4c55-9a51-1f0e2b7c9d10",
 "site_name": "Riccarton Rd Apartments",
 "title": "10-10 pour follow-up",
 "instruction": "Close the open items about the 10-10 pour ...",
 "reading": "Review the Level 3 slab pour on 10 Oct 2026: which open items it settles, and what is still unclear.",
 "state": "ready",
 "failure": null,
 "conclusion": [
   {"text": "The pour record confirms the Level 3 slab was poured and tested on 10 Oct.", "refs": ["R2", "D1"], "confidence": "supported"},
   {"text": "No curing standard was found in the project library; consider uploading NZS 3109.", "refs": [], "confidence": "uncertain"}
 ],
 "as_of": "2026-11-01 02:16:41.003120+00:00",
 "created_at": "2026-11-01 02:14:07.512331+00:00",
 "updated_at": "2026-11-01 02:17:55.881002+00:00",
 "archived": false,
 "archived_at": null,
 "is_creator": true,
 "actions": [
   {"id": "5a7f1e0d-2c3b-4d8e-9f10-112233445566",
    "seq": 1,
    "type": "close_item",
    "verifier": "supported",
    "state": "proposed",
    "refs": ["R2", "R5"],
    "rationale": "The pour record shows the cylinders were taken, which this item asked for.",
    "proposed": {"status": "done"},
    "target_stable_id": "9c8b7a65-4321-4fed-cba9-876543210fed",
    "target_task_no": 69,
    "target_snapshot": {"status": "open", "deadline": "2026-10-12", "responsible": "Sam Patel",
                        "priority": "high", "text": "Take test cylinders at the L3 pour",
                        "updated_at": "2026-10-09T21:03:11+00:00"},
    "applicable": true,
    "applied_at": null,
    "applied_result": null,
    "created_item_stable_id": null},
   {"id": "6b8a2f1e-3d4c-4e9f-a021-223344556677",
    "seq": 2,
    "type": "create_item",
    "verifier": "weak",
    "state": "proposed",
    "refs": ["R7"],
    "rationale": "The engineer asked for the cure log, and no open item covers it.",
    "proposed": {"text": "Send the L3 slab cure log to the engineer", "responsible": "Sam Patel", "deadline": "2026-11-04"},
    "target_stable_id": null,
    "target_task_no": null,
    "target_snapshot": null,
    "applicable": true,
    "applied_at": null,
    "applied_result": null,
    "created_item_stable_id": null},
   {"id": "7c9b3a2f-4e5d-4fa0-b132-334455667788",
    "seq": 3,
    "type": "draft_rfi",
    "verifier": "supported",
    "state": "proposed",
    "refs": ["R7", "D1"],
    "rationale": "The engineer's question about the curing period is still open.",
    "proposed": {"subject": "L3 slab curing period", "body": "..."},
    "target_stable_id": null, "target_task_no": null, "target_snapshot": null,
    "applicable": false, "applied_at": null, "applied_result": null,
    "created_item_stable_id": null}
 ],
 "sources": [
   {"ref": "D1", "kind": "document", "provenance": "documents", "removed": false,
    "document_id": "a1b2c3d4-0000-4000-8000-000000000001", "revision": 3,
    "page_from": 12, "page_to": 12, "title": "Specification · 4.3 Curing",
    "date": null, "site_id": "6f1c2a8e-3b7d-4c55-9a51-1f0e2b7c9d10",
    "excerpt": "...", "topic_id": null, "chunk_id": null, "stable_id": null, "who": null,
    "url": null, "fetched_at": null, "snapshot_s3_key": null},
   {"ref": "R2", "kind": "topic", "provenance": "records", "removed": false,
    "topic_id": "d4e5f6a7-0000-4000-8000-000000000002", "title": "L3 slab pour",
    "date": "2026-10-10", "who": "Sam Patel", "excerpt": "...", "site_id": "6f1c2a8e-...",
    "chunk_id": null, "document_id": null, "revision": null, "page_from": null,
    "page_to": null, "stable_id": null, "url": null, "fetched_at": null,
    "snapshot_s3_key": null},
   {"ref": "R5", "kind": "item", "provenance": "records", "removed": true},
   {"ref": "W1", "kind": "web_source", "provenance": "web", "removed": false,
    "url": "https://example.org/curing-guide", "title": "Curing concrete",
    "fetched_at": "2026-11-01 02:16:02+00:00",
    "snapshot_s3_key": "action_packs/<company>/<pack>/web/W1.txt", "excerpt": "...",
    "...": "other keys null"}
 ],
 "run_trace": {"rounds": 2, "tokens": 81234, "token_cap": 400000, "truncated": false,
               "refs_gathered": 41, "refs_cited": 6, "actions": 3, "ms": {"total": 104233}},
 "models": {"planner": "deepseek/deepseek-v4.1-flash", "writer": "deepseek/deepseek-v4.1-flash",
            "verifier": "google/gemini-3.8-flash", "writer_served": "deepseek/deepseek-v4.1-flash"}}
```

Pack fields:

- `state`: `queued` → `running` → `ready` | `failed` | `cancelled`.
- `failure`: set only when `state` is `failed`. It is a reason code: `budget | model | tools |
  verifier | internal | company_inactive`. Suggested copy: "The run could not finish
  (‹code›). Try again." A run that dies silently is marked `failed/internal` after 20 minutes
  (§4).
- `reading` and `title` may be present **while `running`**: the worker writes them as soon as
  it has planned. Show the reading as soon as it is non-null.
- `conclusion`, `actions`, `sources` and `as_of` are empty or null until `ready`. A `failed`
  pack has no actions; it may have a counts-only `run_trace`.
- `is_creator`: whether the viewer created the pack. Use it to show Cancel to the creator; a
  company admin/GM may also cancel, and gets 403 otherwise.
- `run_trace` and `models`: diagnostics, counts and model ids only. Optional "Run details"
  disclosure; the UI does not need them otherwise. A very large trace comes back as
  `{"dropped": "size"}`. `run_trace.web_disabled` > 0 means the stage has web answers
  switched off (`ENABLE_WEB_ANSWER`), so no web sources were searched.

Conclusion lines: `{text, refs, confidence}`, where `confidence` is `supported | uncertain`.
An `uncertain` line may have **zero refs**. That is deliberate: "no standard found in your
project library" cites nothing. Lines the verifier rejected are not stored at all.

Action fields:

- `type`: `close_item | update_item | create_item` (apply types) or `draft_rfi |
  draft_email | note` (drafts: Copy and Export only, never Apply).
- `proposed`, per type:
  - `close_item` `{status: "done" | "closed"}`;
  - `update_item`: any of `{status, deadline, responsible, priority}`;
  - `create_item` `{text, responsible?, deadline?, priority?}`;
  - `draft_rfi` `{subject, body}`;
  - `draft_email` `{subject, body, to?}`;
  - `note` `{text}`.
- `verifier`: `supported | weak | unsupported`.
  - `weak`: "uncertain" badge; never part of Apply-all (§6).
  - `unsupported`: kept for audit. **Never applicable** (apply returns 409). Recommended:
    collapse these under "Not supported by the evidence".
- `state`: `proposed | applied | dismissed | stale | failed`. `failed` means a previous
  apply hit a server error; it is retryable (show "Retry"). `applied` and `dismissed` are
  final. `stale` can only be dismissed.
- `target_task_no`: the target task's `#number` (§8). Null when the target is gone, not
  numbered yet, or **not visible to this reader**.
- `target_snapshot`: what the run read on the target (`status, deadline, responsible,
  priority, text, updated_at`). It is null when the reader cannot see the live item. Null on
  a close/update therefore means "you cannot see this task", which is different from "the
  task is gone". Show "Task not visible to you" and no Apply.
- `applicable` (S15): whether **this viewer** may apply this action now. It is true only
  when all of these hold:
  - an apply type;
  - verifier is not `unsupported`;
  - state is `proposed` or `failed`;
  - the pack is `ready` and not archived;
  - the viewer passes the same check as `PATCH /action-items/{id}`. For close/update that
    is admin/GM, this site's pm/site_manager, or the task's current assignee. For create it
    is admin/GM or this site's pm/site_manager.

  Show the Apply button only when `applicable` is true. The backend still checks every
  apply, so a stale page can still get a 403 or 409.
- `applied_result` (after apply, dismiss or stale):
  - close/update applied: `{action_item_id, content_edit_ids: [...]}`;
  - already in the proposed state (someone did it by hand): `{noop: true, action_item_id}`.
    Show "Already done";
  - create applied: `{action_item_id, task_no, content_edit_ids}`. Link to the new task;
  - stale: `{reason: "changed" | "target_gone" | "target_moved"}`;
  - failed: `{error: "internal"}`.
- `created_item_stable_id`: the new task's `stable_id` after a create.

Sources: §5.

**Removed sources (S13).** A source whose record has since been deleted, redacted or hidden
from this reader comes back as `{ref, kind, provenance, removed: true}` with nothing else.
Render "source removed" with no excerpt. A conclusion line or action that cites **only**
removed sources is left out of the response entirely, so different readers can see
different numbers of actions on the same pack.

### 3.4 Web snapshot: GET `/action-packs/{id}/sources/{ref}/snapshot`

For a `web_source` with a non-null `snapshot_s3_key`. 200:

```json
{"ref": "W1", "url": "https://example.org/curing-guide",
 "fetched_at": "2026-11-01 02:16:02+00:00",
 "text": "Curing concrete\n...", "truncated": false}
```

- `text` is the plain page text the run stored when it read the page, at most 200 kB.
  `truncated: true` means the stored text was longer than that.
- 404 when the ref does not exist, is not a web source, has no snapshot, is removed, or the
  object is missing. A 500 here is a backend fault, not "no snapshot".
- `ref` must look like a ref (letters then digits, e.g. `W1`); anything else is 404.

Render it in a plain-text viewer (`white-space: pre-wrap`), with a header "Snapshot of ‹url›
taken ‹fetched_at›" and a link to the live page. Never render it as HTML.

### 3.5 Export: GET `/action-packs/{id}/export?format=md`

```json
{"format": "md", "filename": "action-pack-0b9e4d2c-77a1-4f3e-bb0c-5d8f6a1e2c33.md",
 "content_type": "text/markdown",
 "content": "# 10-10 pour follow-up\n\nState: ready · Site: Riccarton Rd Apartments · ..."}
```

- The same view as GET one, as Markdown: Reading, Conclusion, Actions and Sources.
  Removed sources stay removed; `uncertain` and `weak` are marked "_(uncertain)_".
- `format` defaults to `md`. **`format=docx` returns 400 `"format must be md"` in v1.**
  Hide the .docx option, or build .docx client-side from the Markdown.
- Use it for Copy and Download on drafts. A single draft can also be copied straight from
  `proposed.subject` / `proposed.body` / `proposed.text`.

### 3.6 Cancel: POST `/action-packs/{id}/cancel`

Body `{}`. 200 `{"id": "...", "state": "cancelled"}`.

- 403 `"only the creator or a company admin can cancel this pack"`.
- 409 `{"error": "pack is already finished", "state": "ready"}` when the pack is already
  `ready`, `failed` or `cancelled`. Refresh and show the final state.

The run stops at its next checkpoint. Anything it had gathered is discarded.

### 3.7 Archive: POST `/action-packs/{id}/archive`

`{"archived": true}` or `{"archived": false}` (unarchive). 200
`{"id": "...", "archived": true}`.

- 400 `"archived must be true or false"` or `"unknown fields"`.
- Any reader may archive. Archived packs drop out of the default list (`archived=false`).
- An archived pack's actions cannot be applied or dismissed (409 `"pack is archived"`), and
  `applicable` is false on all of them.

### 3.8 Dismiss: POST `/action-packs/{id}/actions/{aid}/dismiss`

Body must be empty: `{}` (or no body). Any key gives 400 `"unknown fields"`. 200
`{"id": "<aid>", "state": "dismissed"}`.

- 409 `"pack is archived"`.
- 409 `{"error": "action is applied", "state": "applied"}` (or `dismissed`): the action is
  already final.
- 404 `"action not found"` (including an action hidden by removed sources).

Dismissing a `stale` action keeps its stored stale record.

### 3.9 Apply: POST `/action-packs/{id}/actions/{aid}/apply`

Body: `{}` to apply the proposal as shown, or `{"fields": {...}}` to apply an edited
version. The edit is recorded as the applier's own edit. Editable fields per type:

| type | `fields` keys |
|---|---|
| `close_item` | `status` (`done` or `closed` only) |
| `update_item` | `deadline` (`YYYY-MM-DD` or null), `responsible` (a site member's display name), `priority` (`low` / `medium` / `high`), `status` |
| `create_item` | `text` (1 to 2000 chars), `responsible`, `deadline`, `priority` |

`text` is never editable on an existing task. Edited fields are merged over the proposal.

**200 applied:**

```json
{"id": "5a7f1e0d-2c3b-4d8e-9f10-112233445566", "state": "applied",
 "applied_result": {"action_item_id": "e1f2a3b4-0000-4000-8000-000000000003",
                    "content_edit_ids": ["f0e1d2c3-0000-4000-8000-000000000004"]}}
```

The other `applied_result` shapes are in §3.3: noop, create with `task_no`.

**Idempotent replay.** Applying an action that is already `applied` returns **200** with the
stored `applied_result` and writes nothing. A double-click is safe; treat a 200 on a re-click
as success.

**409 stale: the human wins.** A person changed the task since the run read it: a field the
action touches changed, or any edit was recorded after `as_of`. Or the task is gone, or it
moved to another site. Nothing is written, the action becomes `stale`, and the body is:

```json
{"error": "action is stale", "state": "stale",
 "live":     {"status": "in_progress", "deadline": "2026-11-08", "responsible": "Ana Lee",
              "priority": "high", "text": "Take test cylinders at the L3 pour",
              "updated_at": "2026-10-31 22:40:12.004411+00:00"},
 "snapshot": {"status": "open", "deadline": "2026-10-12", "responsible": "Sam Patel",
              "priority": "high", "text": "Take test cylinders at the L3 pour",
              "updated_at": "2026-10-09T21:03:11+00:00"}}
```

- Branch on `body.state === "stale"`, not on the status code: other 409s exist.
- `live` and `snapshot` are both **null** when the caller cannot see the target task, or
  when the target is gone (`live` null). In that case show "This task changed or is no
  longer visible to you" with no comparison.
- Applying a stale action again returns the same 409 (live values re-read).

**Other responses** (the action is unchanged unless stated):

| Status | Body | Meaning |
|---|---|---|
| 404 | `not found` | pack not readable or not there |
| 404 | `action not found` | unknown action, or hidden by removed sources |
| 404 | `action item not found` | the task is in another company |
| 409 | `pack is archived` | |
| 409 | `pack is not ready`, `{state}` | still running, or failed/cancelled |
| 409 | `this action has no apply`, `{type}` | a draft |
| 409 | `an unsupported action cannot be applied` | verifier `unsupported` |
| 409 | `action is dismissed`, `{state}` | final |
| 409 | `the action proposes a field apply cannot write`, `{code: "unsupported_field", fields}` | malformed proposal |
| 409 | `a close must set done or closed`, `{code: "unsupported_field", fields: ["status"]}` | |
| 409 | `the action has nothing to apply`, `{code: "empty_proposal"}` | |
| 409 | `the action names another site`, `{code: "site_mismatch"}` | create only |
| 409 | `status transition not allowed`, `{from, to}` | e.g. reopening is never proposed |
| 409 | `company_inactive` | site company suspended/exited (only while enforcement is on) |
| 403 | `access denied to this task's site` / `admin/gm, this site's pm/site_manager, or the assignee only` | the applier's rights on the task |
| 403 | `admin/gm or this site's pm/site_manager only` | create |
| 400 | `unknown fields` `{fields}` / `fields must be an object` / `field not editable` `{fields}` / `status must be one of ['closed', 'done']` | bad body |
| 400 | `text is required` / `text must be at most 2000 characters` | create |
| 400 | PATCH's validators: `priority must be one of ...`, `status must be one of ...`, `deadline must be YYYY-MM-DD or null`, `responsible must be a non-empty display name`, `assignee must be a member of this site` | bad edited value |
| 500 | `{error: "apply failed", code: "internal"}` | rolled back; the action is now `failed` and can be retried |

After any apply or dismiss, re-GET the pack (or patch the one action locally from the
response) and refresh Tasks.

## 4. Polling

- After create, open the pack page and poll `GET /action-packs/{id}` every **3–5 s** while
  `state` is `queued` or `running`. Stop on `ready`, `failed` or `cancelled`.
- Show `reading` (and `title`) as soon as they are non-null, even while running. "I read
  this as: ‹reading›" lets the person cancel early if the run misunderstood them.
- A run normally takes 1 to 5 minutes. The GET runs a sweep that marks a pack `failed`
  (`internal`) once its worker has been silent for 20 minutes, so polling always ends.
- Back off (for example to 15 s) when the tab is hidden (`document.visibilityState`), and
  poll once on return.
- The list page can poll `GET /action-packs?state=running` at a slower rate (15–30 s) for a
  "1 pack running" indicator.

## 5. Ref chips

Every conclusion line and every action shows its `refs` as chips. Route a chip by **looking
the ref up in `sources[]`** and branching on its `provenance` and `kind`. The letter prefix is
a convention, not a contract:

| Source | Chip opens |
|---|---|
| `removed: true` | nothing; a greyed "source removed" chip |
| `provenance: "records"` (`kind` `topic`, `chunk`, `item`) | the **same evidence card Ask uses**, fed from the source row: `title`, `date`, `who`, `excerpt`, plus `topic_id` / `chunk_id` (and `stable_id` for an item) for its "open in day view" link |
| `provenance: "documents"` | the **document page**: `document_id`, `revision`, `page_from`–`page_to`; `title` is "‹doc title› · ‹heading›" |
| `kind: "web_source"` with `snapshot_s3_key` | the **snapshot viewer** (§3.4), with the live `url` as a secondary link |
| `kind: "web_source"` without `snapshot_s3_key` | the `url` only. The page was not stored, so show "not stored" |
| `kind: "web_answer"` | the `excerpt` in a popover. There is no page and no snapshot |

The excerpt is the exact text the run read. Show it as quoted text, never as HTML.

**"Web source, not your project's documents".** When **every** ref of a conclusion line or
action resolves to a `provenance: "web"` source, label that element "Web source, not your
project's documents". The backend stores no flag for this; derive it from the sources. An
element that mixes web and project refs gets no label.

## 6. Verifier badges and Apply-all

- `verifier: "weak"`: show an **"uncertain"** badge. It can still be applied one at a time.
- `confidence: "uncertain"` on a conclusion line: the same "uncertain" badge.
- `verifier: "unsupported"`: never applicable (see §3.3).

**Apply-all** ("Apply all supported") is a **UI loop**. There is no bulk endpoint in v1.

1. Take `actions.filter(a => a.verifier === "supported" && a.applicable)`, in `seq` order.
   `applicable` already excludes drafts, unsupported, final states, unready and archived
   packs.
2. Call the single apply for each one, **sequentially**: one request at a time, and never
   with `fields`.
3. Do not stop on a failure. A stale or refused action must not block the others.
4. Report per action: applied, already done (`noop`), stale (offer the stale dialog),
   refused (status and message), or failed (retryable).
5. Re-GET the pack at the end.

Weak actions are never in Apply-all. Show the count, for example "2 uncertain actions not
included".

## 7. Stale dialog

Shown on a 409 with `state: "stale"`, or when the person opens an action whose `state` is
already `stale`. Applying it re-reads the live values.

- Title: "This task changed since the pack was made".
- A two-column table: **Now** (`live`) against **When the pack read it** (`snapshot`), with
  rows for status, deadline, responsible, priority, and text, plus `updated_at` as "last
  changed". Highlight the rows that differ, and the fields the action wanted to change.
- Buttons: "Open task" (link by `target_task_no` / the Tasks page) and "Dismiss action".
  There is no "apply anyway": v1 never overwrites a person's change. A new pack can propose
  again.
- When `live` and `snapshot` are null: "This task changed or is no longer visible to you",
  with Dismiss only.

## 8. `#task_no` in Tasks

Every action item now has a per-site number, shown as `#69`. It lasts across re-extraction
and is never reused. People can name it in an instruction ("close #69").

Where `task_no` appears:

| Response | Where |
|---|---|
| `GET /timeline` (report shape) | `topics[].action_items[].task_no` |
| `GET /live-items` | `topics[].action_items[].task_no` |
| `PATCH /action-items/{id}` | top-level `task_no` |
| apply create result | `applied_result.task_no` |
| pack actions | `target_task_no` (close/update targets) |

- `task_no` **can be null**: items written before the numbering migration until the owner
  runs the backfill, items written between deploy and migrate, or a lookup failure. Render
  nothing for null; never "#null" or "#0".
- Numbers are per **site**. Two sites both have a `#12`. Where Tasks mixes sites, show the
  site next to it.

## 9. "Agent" source badge (Tasks and day view)

Tasks created by applying a pack hang off a synthetic topic, one per pack, whose `source` is
`"agent"`. That topic is dated the NZ day of the first apply and belongs to the first
applier.

**The rule: `source === "agent"` shows an "Agent" badge. Never show "nightly" for it.**

- The backend never sends a "nightly" string. The live/nightly label is the UI's own
  derivation from `is_live`, and for an agent topic `is_live` stays **false**. A UI that maps
  `is_live === false` to "nightly" must check `source` first.
- Where `source` is carried:
  - `GET /timeline` report shape: on each topic (`topics[].source`) **and** on each item
    (`topics[].action_items[].source`), so the flattened Tasks list keeps it;
  - `GET /live-items`: on the **topic only** (`topics[].source`, next to `is_live`). The
    items there carry `task_no` but not `source`, so take the badge from the parent topic
    when flattening;
  - `PATCH /action-items/{id}` response: top-level `source` (the parent topic's).
- Other topics carry whatever `source` they had before. Only `"agent"` is new.
- An agent topic shows on the **first applier's own day view** for the apply date. Other
  site members see the task in Tasks and `/live-items`, not on their own day view.
- **History**: a created task's history (`GET /content/action_items/{id}/history`) has a row
  with `field: "created"` whose `after_text` is a **JSON string**
  `{"pack_id": "...", "action_id": "..."}`. Parse it and show "Added from action pack
  ‹title›", fetching the title with `GET /action-packs/{pack_id}`. If that is 404 (the viewer
  is not a pack reader, or the flag is off), show "Added from an action pack". Closes and
  updates made by apply show as ordinary edits by the applier.

**Known UI gap** (found in backend review; not re-checked in fieldsight-ui):
`compliance-aggregator.js` skips topics with `is_live === false`, so agent-created items are
missing from the safety and quality views. Fix it with the same rule: include
`source === "agent"`.

## 10. Entry points

All three create a pack with POST `/action-packs`, then open the pack page and poll. Each
needs a `site_id` (required when the user sees more than one site).

1. **Ask: "Do this in the background".** Turns the current question into an instruction. Use
   the question text, prefixed by the Ask scope's dates when there are any. Use the Ask
   scope's site; if Ask spans several sites, ask the user to pick one.
2. **Day view: "Review this day".** The run sees only the instruction, the site name and
   today's date; it does not know which day the user was looking at. **Put the date in the
   instruction**, for example `Review 10 Oct 2026 on this site: what was decided, which open
   items it settles, what still needs doing.` Use the day view's site.
3. **Tasks: "Tidy open items".** For example `Tidy the open items on this site: close what the
   records show is done, update owners and deadlines the records settle, and flag
   duplicates.` Use the Tasks page's site filter, and require one site.

Prefill the instruction in an editable box. The person can edit it before sending
(1 to 2000 characters).

A "Packs" list (GET `/action-packs`), filterable by site and state with an "Archived" tab,
is the home for all of them.

## 11. Checklist for the UI PR

- [ ] Entry points hidden unless the flag probe returns 200.
- [ ] Create handles 400 (site required), 404, 409 (in progress, and `company_inactive`),
      and 503.
- [ ] Poll 3–5 s while queued/running; reading shown early; backoff when hidden.
- [ ] Ref chips routed by source `provenance`/`kind`; removed sources shown greyed.
- [ ] Web-only elements labelled; snapshot viewer renders text, not HTML.
- [ ] `weak` and `uncertain` badges; unsupported collapsed.
- [ ] Apply shown only when `applicable`; Apply-all = supported and applicable, sequential,
      per-action report.
- [ ] Stale dialog with live against snapshot; nulls handled.
- [ ] Replay 200 treated as success; `failed` offers Retry.
- [ ] `#task_no` in Tasks, null-safe; site shown where sites are mixed.
- [ ] Agent badge from `source === "agent"` on timeline, live-items (topic) and the PATCH
      response; never "nightly".
- [ ] `compliance-aggregator.js` includes agent topics.
- [ ] History "Added from action pack ‹title›" from the `created` row.
- [ ] Export: md only; no docx option.
