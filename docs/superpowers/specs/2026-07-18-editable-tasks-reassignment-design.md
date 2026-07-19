# Editable Tasks & Reassignment — Design (2026-07-18)

**Status:** Design / for review. Builds directly on
`2026-07-17-visibility-permission-model-design.md` (the ACL primitives it
reuses) and `2026-07-18-phase2-aurora-read-consolidation.md` (the
`/api/org/sites/{id}/members` roster it validates reassignment against).

**Scope:** fieldsight-pipeline (a new org-api write route + one migration +
one repo) + fieldsight-ui (task-card / task-detail editors). This adds the
FIRST mutation of an `action_items` row's *content* — today the only writes
touching a task are the DynamoDB check-off overlay and the extraction insert.

---

## 1. Problem

On the Today / Timeline task cards a user (typically the site_manager) wants to
**edit** a task and **reassign** it to another project member. Concretely:

- Change **Priority** (High → Medium).
- Change **Assignee** — hand a task to another member of the same project
  (e.g. someone goes on holiday; reassign to "Neo" and it appears on *Neo's*
  Today, disappears from the original owner's).
- Change **Status** (open / in progress / blocked / done).
- Change **Due date**.

**Currently NONE of this is editable.** The evidence:

### 1.1 No update route for an action_item exists
The org-api dispatch (`src/lambda_org_api.py:156-256`) has routes for `/me`,
`/sites`, `/members`, `/observations` (incl. `PATCH /observations/{id}`,
:215-217), `/live-items` (GET only, :222-224), `/timeline`, `/programme`, …
**There is no `PATCH` for an action item** — no `/action-items/{id}`, no
`/actions/{id}`. The legacy report gateway (`src/lambda_fieldsight_api.py`)
only exposes `POST /api/actions/toggle` and `GET /api/actions`
(:1089-1090) — a check/uncheck boolean, not a field editor.

### 1.2 The `action_items` row is written once and never updated
`action_items` (`src/migrations/0003_dashboard_readmodel.sql:14-24`, +
`deadline_text` from `0011_authority_flip.sql:14`) has columns
`text / responsible / deadline / deadline_text / priority / status
(DEFAULT 'open') / site_id / topic_id`. It is INSERT-only: `upsert_topic`
inserts each item (`src/repositories/topics.py:49-55`) and nothing ever
`UPDATE`s it. There are **no `updated_at` / `updated_by` audit columns** and
**no `company_id`** (tenancy is reached via `site_id → sites.company_id`).

### 1.3 "Status" is a client-side illusion over a check-off overlay
The displayed status is **not** `action_items.status` (which stays `'open'`
forever). It is derived from the DynamoDB check-off boolean:
- `toggle_action` (`src/lambda_fieldsight_api.py:609-649`) writes the
  check state to the **DynamoDB** audit table keyed by
  `ACTIONS#{date}` / `TOPIC#{topic_id}#ACTION#{action_index}` — a *positional*
  key, never the durable `action_items.id`, and never the Aurora column.
- The UI turns that boolean into a label:
  `deriveStatus(checked)` → `Done`/`Open` (`scripts/api/today-adapter.js:91-94`),
  called per item at `today-adapter.js:375-377`.

So there are already **two disjoint notions of "done"**: the inert Aurora
`status` column and the DynamoDB overlay that actually drives the UI. Making
status editable without reconciling these would create a *third*.

### 1.4 "My tasks" is matched by a display-name STRING, not an id
Reassignment only "works" if it actually moves the task between people's
Today views. Today scopes mine-vs-team purely by string equality
(`scripts/api/today-adapter.js:425`):

```js
if (currentUserName && task.assignee === currentUserName) { myTasks.push(task); }
else { teamTasks.push(task); }
```

where `task.assignee = a.responsible || '—'` (`today-adapter.js:383`) and, on
the live path, `currentUserName = caller.name` (`scripts/pages/today.js:212`).
`responsible` is **free text** captured by extraction — there is no
`responsible_user_id` FK. Therefore reassignment == setting
`action_items.responsible` to the **exact display name** the new assignee's
session resolves to (`caller.name`, i.e. `"First Last"`).

### 1.5 The read serialization drops the durable id + status
The Today/Timeline compat shim renders topics through `render_report_shape`
(`src/lambda_org_api.py:1301-1344`). Its `action_items` projection
(:1327-1329) emits only `{action, responsible, deadline, priority}` — it
**drops `id` and `status`** and re-keys topics *positionally*
(`"topic_id": i`, :1320). So a card built from `/timeline` has no durable
handle to PATCH. (The `/live-items` child query *does* select `id` + `status`
— `topics.py:207-213` — but Today consumes the report shape, not live-items.)

---

## 2. Goals / non-goals

**Goals**
1. A single **`PATCH`** route that edits any of `priority / responsible /
   status / deadline` on one action item, addressed by its durable
   `action_items.id`.
2. **Reassignment that actually moves the task**: `responsible` is set to a
   validated member of the task's site, matching the mine-vs-team key so the
   item leaves the old owner's Today and lands on the new one's.
3. **One authoritative status.** Make `action_items.status` the source of
   truth (open/in_progress/blocked/done); fold the check-off into it. No third
   notion of "done".
4. **Reuse the existing ACL verbatim** (site-authority via `_allowed_site_ids`
   / `memberships` / `resolve_scope`) — no new ACL primitive, no
   cross-company / cross-project reach.
5. **Minimal audit**: record who last changed a task and when.

**Non-goals**
- **Notifications** (email/push when reassigned) — out of scope; visibility
  only, as the visibility spec §3 scopes it.
- Editing the task **text** / creating tasks from the card (createAction
  already exists as a separate flow).
- Graded-roles rollout: this works with `GRADED_ROLES` **off** (today's
  scoping) and stays correct when it flips on (§3.2).
- Any `template.yaml` / API-Gateway change — the route rides the existing
  `/api/org/{proxy+}` integration.

---

## 3. Design

### 3.1 The route: `PATCH /api/org/action-items/{id}`
A partial update mirroring `patch_observation_status`
(`src/lambda_org_api.py:879-891`) — validate body, fetch the (tenant-guarded)
row, authorize, write, return the updated row. Body carries any subset of:

```
PATCH /api/org/action-items/{action_item_id}
{ "priority":  "low" | "medium" | "high",
  "status":    "open" | "in_progress" | "blocked" | "done",
  "deadline":  "YYYY-MM-DD" | null,
  "responsible": "First Last" }          # must be a member of the task's site
```

Every field is optional; only supplied keys are written. An empty patch is a
400. The response is the updated row (incl. `updated_at` / `updated_by`), same
"return the row" contract as observations.

### 3.2 Permission model — reuse, don't invent (D1)
A task belongs to a `site_id` (`action_items.site_id`). Authority is gated in
two layers, both from EXISTING helpers:

1. **Reach** (multi-tenant + project scope): the task's `site_id` must be in
   `_allowed_site_ids(conn, caller)` (`src/lambda_org_api.py:1011-1020`) — the
   same guard `/live-items`, `/programme`, `/dates`, `/sites/{id}/members`
   already use. This alone blocks cross-company and out-of-project edits.
   Defence-in-depth: the repo fetch joins `sites.company_id` and the handler
   rejects a row whose company ≠ `caller["company_id"]` (404), so a bug in the
   reach set can't cross tenants.
2. **Edit authority** within that site — allow if ANY of:
   - `resolve_scope(caller["global_role"]) == "ALL"` — admin/gm
     (`src/repositories/acl.py:6-7`); or
   - the caller holds a `pm` or `site_manager` **membership on this site** —
     `memberships.caller_site_roles(conn, caller["id"]).get(site_id) in
     {"pm","site_manager"}` (`src/repositories/memberships.py:78-87`); or
   - the caller **is the current assignee** — `row["responsible"] ==
     display_name(caller)` (their own task).

This is exactly the shape of `patch_observation_status`'s "author OR admin/gm"
check (:888), widened to "site pm/site_manager OR admin/gm OR assignee". It
reads `membership.role` (which the visibility spec §1.2 notes is otherwise
inert), so it is correct whether `GRADED_ROLES` is on or off — `caller_site_roles`
does not depend on that flag.

### 3.3 Reassignment mechanics (D2)
- **Validate the target is a site member.** Fetch the roster with the Phase-2
  repo `memberships.members_for_site(conn, company_id, site_id)`
  (`src/repositories/memberships.py:57-75`) and require the requested
  `responsible` to equal one member's display name
  (`(first_name + " " + last_name).strip()`), the SAME string the UI member
  picker surfaces as `name` (`scripts/api/org.js:43,48` via `_toPageMember`).
  A non-member target is a 400 — no free-typing a name onto a task.
- **Store the display name in `responsible`.** Because mine-vs-team is a
  display-name string match (§1.4), writing `responsible = "First Last"` is
  what makes the task appear on that member's Today (their session's
  `caller.name`) and vanish from the previous owner's. No FK, no schema change
  for identity — the roster validation is what keeps it pointing at a real
  login.
- **Caveat (documented, accepted):** if a site member has no login, or their
  login's `caller.name` differs from `"First Last"`, the task won't surface on
  a Today view — but it is never lost (still visible on the site's
  Timeline/live-items, and to admins/pm). This is a visibility limitation, not
  a data one, consistent with the spec's "visibility only, notifications out".

### 3.4 Status reconciliation — one source of truth (D3)
Make **`action_items.status` authoritative** (`open` / `in_progress` /
`blocked` / `done`) and retire the DynamoDB overlay as the meaning of "done":

- The status editor writes `action_items.status` through this PATCH.
- The **check-off button** becomes a status shortcut: check → `status='done'`,
  uncheck → `status='open'`, routed through the SAME PATCH (by
  `action_items.id`) instead of `POST /api/actions/toggle`.
- `deriveStatus` (`today-adapter.js:91`) is changed to read the item's
  `status` column first, mapping the enum to the existing badge tones
  (`done`→success, `in_progress`→info, `blocked`→magenta, `open`→info). The
  DynamoDB check-off is kept ONLY as a read-time fallback for legacy days
  whose column was never written (so no historical check-off visibly reverts).

This requires the read serialization to carry `id` + `status`: extend
`render_report_shape` (:1327-1329) to emit `"id": str(a["id"])` and
`"status": a["status"]` (both already selected upstream in
`list_topics_for_source_prefix`, `topics.py:302-308`). The card then threads
`a.id` as the durable PATCH handle.

**Why not keep two systems?** The overlay is positional (`topic_id`,
`action_index`) and per-`user_folder`; the moment `responsible` changes, the
positional key still points at the old owner's folder — the overlay and the
row would disagree about who owns the "done". Collapsing "done" onto the row
removes that entire class of drift.

### 3.5 Priority / due (D5)
- **Priority** validated against `{"low","medium","high"}` (a real enum; the
  UI already title-cases via `priorityLabel`, `today-adapter.js:83`).
  Extraction wrote free text here historically; the PATCH only *accepts* the
  enum, it does not retro-normalize existing rows.
- **Deadline** validated as `YYYY-MM-DD` (reuse `REPORT_DATE_RE`,
  `src/lambda_org_api.py:820`) or `null` to clear. On write, set both
  `deadline` (the date column) and `deadline_text` (the free-text mirror the
  Timeline reads) to the same value, so the two never disagree after an edit.

### 3.6 Concurrency / audit (D6)
Minimal, mirroring `observations.set_status` (`updated_at=now()`,
`src/repositories/observations.py:81-89`): add `updated_at timestamptz` +
`updated_by text` (the caller's `cognito_sub`) to `action_items` (migration
0016) and set them on every PATCH. **Last-write-wins**, no optimistic
version/lock — task-edit contention at a single site is negligible and a
version column would be over-engineering at this scale. The `updated_at`/
`updated_by` pair is the audit trail ("who last touched this task"); it
supersedes the need to reuse the legacy DynamoDB audit log for edits.

---

## 4. Data model change

```sql
-- 0016: editable action items — minimal last-writer audit (mirrors
-- observations.updated_at). No company_id (reached via site_id -> sites),
-- no version column (last-write-wins, §3.6).
ALTER TABLE action_items ADD COLUMN updated_at timestamptz;
ALTER TABLE action_items ADD COLUMN updated_by text;
```

New repo `src/repositories/action_items.py` (mirrors `observations.py`):
- `get_action_item(conn, id) -> dict|None` — the row joined to
  `sites.company_id` (tenant guard); `None` on missing / malformed uuid.
- `update_action_item_fields(conn, id, fields, updated_by) -> dict|None` —
  whitelisted partial `UPDATE ... SET <cols>, updated_at=now(),
  updated_by=%s RETURNING <cols>`.

---

## 5. Reassignment flow (end to end)

1. Site_manager opens a task's detail; the assignee control fetches the site
   roster via `FS.api.org.getSiteMembers(siteId)` (Phase 2,
   `scripts/api/org.js:284`).
2. They pick "Neo"; the UI `PATCH /api/org/action-items/{id}` with
   `{responsible: "Neo Tan"}`.
3. Backend: reach check (`site_id ∈ _allowed_site_ids`), edit authority
   (admin/gm OR pm/site_manager on that site OR current assignee),
   member-validation (`"Neo Tan"` ∈ `members_for_site`), then write.
4. Next Today load: `responsible == "Neo Tan"`. On the previous owner's
   session `task.assignee !== caller.name` → the item drops to teamTasks (or
   disappears for a worker). On Neo's session `task.assignee === caller.name`
   → it appears in *myTasks*. The reassignment has moved the task.

---

## 6. Decisions (recommended)

- **D1 — Who may edit/reassign — RECOMMEND:** admin/gm (company) + the
  **pm/site_manager of the task's site** + the **current assignee** (own task).
  Reuse `_allowed_site_ids` (reach) + `caller_site_roles` (per-site role) +
  `resolve_scope` (admin/gm). No new ACL. (§3.2)
- **D2 — Reassignment target — RECOMMEND:** must be a member of the task's
  site (`members_for_site`); store the member's display name in `responsible`
  (matches the mine-vs-team string key). No `responsible_user_id` FK this
  round. (§3.3)
- **D3 — Status vs check-off — RECOMMEND:** `action_items.status` becomes
  authoritative (open/in_progress/blocked/done); the check-off is folded into
  it (check→done, uncheck→open) via the same PATCH; `deriveStatus` reads the
  column with the DynamoDB overlay kept only as a legacy read fallback. Avoids
  a third source of truth. (§3.4)
- **D4 — Addressing — RECOMMEND:** PATCH by durable `action_items.id`, not the
  positional `(topic_id, action_index)`. Surface `id` (+ `status`) through
  `render_report_shape`; the card threads it. (§3.4/§1.5)
- **D5 — Priority/due — RECOMMEND:** priority enum `{low,medium,high}`;
  deadline `YYYY-MM-DD`|null (reuse `REPORT_DATE_RE`), writing `deadline` and
  `deadline_text` together. (§3.5)
- **D6 — Audit/concurrency — RECOMMEND:** add `updated_at`/`updated_by`
  (migration 0016), last-write-wins, no version column. (§3.6)
- **D7 — Route shape — RECOMMEND:** `PATCH /api/org/action-items/{id}`, partial
  body, rides the existing proxy integration (no `template.yaml` change). (§3.1)

---

## 7. Risks

- **Reassignment silently not surfacing** if `responsible` ("First Last") ≠ the
  target's session `caller.name`. Mitigation: member-validation guarantees the
  string is a real site member's canonical name; the display-name convention
  is the same on both write (roster) and read (`caller.name`). A future
  hardening is a `responsible_user_id` FK (out of scope, noted D2).
- **Status double-write during transition.** While the check-off still
  dual-writes DynamoDB, a day can have both a column status and an overlay
  boolean. Mitigation: `deriveStatus` prefers the column; the overlay is
  read-only fallback. Retire the DynamoDB write after one release.
- **Widening write authority is a real ACL surface.** Every PATCH must pass the
  reach gate before the row is touched; add ACL tests (admin / site pm /
  site_manager / assignee / outsider × in-scope/out-of-scope) exactly as the
  visibility spec §6 requires for read paths.
- **Positional history.** Any code still keying a task by `(topic_id,
  action_index)` (the check-off overlay, `actions.js:62`) is untouched by an
  edit; only the durable-id PATCH path is authoritative. Keep the overlay
  key-space intact until it is retired, to avoid orphaning historical
  check-offs.

---

## 8. 中文摘要

需求:Today/Timeline 任务卡要能改 **优先级 / 负责人(改派)/ 状态 / 截止日期**,
目前全不可改。现状证据:org-api 无任何 action_item 的 PATCH 路由
(`lambda_org_api.py:156-256`,只有 observations 有 PATCH);`action_items`
行只在抽取时 INSERT、从不 UPDATE(`topics.py:49-55`);显示的"状态"其实来自
DynamoDB 打勾覆盖层(`lambda_fieldsight_api.py:609-649` + 前端
`today-adapter.js:91` deriveStatus),`action_items.status` 列恒为 'open';
"我的任务"是按**显示名字符串**匹配(`today-adapter.js:425`
`task.assignee === currentUserName`,后者=`caller.name`),`responsible`
是自由文本、无 FK。

设计:新增 `PATCH /api/org/action-items/{id}`(仿 `patch_observation_status`),
复用现有 ACL——站点可达用 `_allowed_site_ids`、编辑权限用 `caller_site_roles`
(该站 pm/site_manager)+ `resolve_scope`(admin/gm)+ 当前负责人;改派目标必须是
本站成员(`members_for_site`),写入其显示名到 `responsible`(才能真正移动到对方
Today)。状态改为以 `action_items.status` 列为准(open/in_progress/blocked/done),
打勾并入其中(勾→done),deriveStatus 改读列、DynamoDB 覆盖层仅作旧数据回落。
另需把 `render_report_shape` 补出 `id`+`status`(:1327),并加迁移 0016
(`updated_at`/`updated_by`)做最小审计。通知不在范围内。
