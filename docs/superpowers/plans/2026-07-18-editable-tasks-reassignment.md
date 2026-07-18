# Editable Tasks & Reassignment (priority / assignee / status / due) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a site_manager (or admin/gm/pm, or the current assignee) EDIT a task's **priority**, **assignee (reassign)**, **status**, and **due date** from the Today/Timeline card. This adds the first content-mutation of an `action_items` row: a new `PATCH /api/org/action-items/{id}` on the in-VPC Aurora org-api, gated by the EXISTING site-authority ACL, with reassignment validated against the task's site roster; plus the UI editors wired to it; plus collapsing the two-source "done" (inert `action_items.status` column vs the DynamoDB check-off overlay) onto the column as the single source of truth.

**Architecture:** One write route on `lambda_org_api`, shaped exactly like `patch_observation_status` (`src/lambda_org_api.py:879-891`):
- `PATCH /api/org/action-items/{id}` — partial update of `priority`/`status`/`deadline`/`responsible`. ACL: the task's `site_id` must be in `_allowed_site_ids(conn, caller)` (reach — same guard as `/live-items`/`/programme`/`/dates`), AND the caller must be admin/gm (`resolve_scope == "ALL"`) OR hold a `pm`/`site_manager` membership on that site (`memberships.caller_site_roles`) OR be the current assignee. Reassignment (`responsible`) is validated against `memberships.members_for_site` (the task's site roster). Backed by a new repo `action_items.py` (`get_action_item` + `update_action_item_fields`) and migration `0016` (`updated_at`/`updated_by`).
- The Today/Timeline read shim `render_report_shape` (`src/lambda_org_api.py:1301-1344`) is extended to surface each item's durable `id` + `status` (both already selected upstream) so the card has a stable PATCH handle and can show the authoritative status.

Then the UI adds inline editors on the task detail (priority select, assignee picker sourced from `FS.api.org.getSiteMembers`, status select, due date), wired through a new `FS.api.actions.updateAction(id, patch)`; the check-off button is folded into the status column (`checked → status:'done'`); and `deriveStatus` reads the column with the DynamoDB overlay kept only as a legacy read fallback.

**Tech Stack:** Pipeline — Python 3.12, psycopg3 (in-VPC Aurora PG16), SAM `fieldsight-test`/`fieldsight-prod`. Tests: pytest with the repo's FakeConn/FakeCursor (unit, validated in **CI**) + the `db` fixture (integration, needs `TEST_DATABASE_URL`). `uv run pytest` is the gate. UI — no-build single-file React (`window.FS.api.*`), gate = `node --check` + grep pre-checks + `?v=N` cache-buster bumps.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-18-editable-tasks-reassignment-design.md` — §3.1 (route), §3.2 (permission model), §3.3 (reassignment), §3.4 (status reconciliation), §3.5 (priority/due), §3.6 (audit), D1–D7. Reuses `2026-07-18-phase2-aurora-read-consolidation.md`'s `members_for_site`/`/sites/{id}/members`.
- **Reuse the EXISTING ACL — do NOT invent new ACL.** Site-authority via `_allowed_site_ids(conn, caller)` (`src/lambda_org_api.py:1011`), per-site role via `memberships.caller_site_roles(conn, user_id)` (`src/repositories/memberships.py:78`), admin/gm via `resolve_scope` (`src/repositories/acl.py:6`). No `visible_scope`/Phase-3 dependency: `caller_site_roles` is flag-independent, so the gate is correct whether `GRADED_ROLES` is on or off.
- **Reassignment target MUST be a site member.** Validate `responsible` against `memberships.members_for_site(conn, company_id, site_id)`. No silent cross-company/cross-project: the reach gate (`site_id ∈ _allowed_site_ids`) plus the repo's company-pinned join block it; the handler also 404s a row whose joined `company_id != caller["company_id"]` (defence-in-depth).
- **`uv run pytest` is the gate.** Unit tests use the existing FakeConn/FakeCursor + `wired`/`make_event` harness; integration tests SKIP without `TEST_DATABASE_URL` and must be green in CI.
- **Pipeline git:** branch off `develop` (`git checkout -b <name> origin/develop`); NEVER check out `develop`; NEVER `git add -A` — stage named files only; CRLF repo → single-line Edit anchors. New dev content (comments/commits/docs) in ENGLISH.
- **UI git/hygiene:** `node --check` every modified `.js`; bump `?v=N` in the preview HTMLs for any changed loaded file; BUG-19 (never `new Date('YYYY-MM-DD')` — use `FS.api.addDaysISO`/`todayNZDT`), BUG-20 (`text/html` 200 is the SPA shell; `_fetch.js` guards). No build step.
- One migration (`0016`), one new repo module, one new route on the existing `/api/org/{proxy+}` integration — **no `template.yaml`/SAM change**. Live/prod smoke is user-gated (Task 4), not run by the implementer.

## File Structure

| File | Create/Modify | Responsibility |
|---|---|---|
| `src/migrations/0016_action_item_audit.sql` | Create | add `updated_at`/`updated_by` to `action_items` |
| `src/repositories/action_items.py` | Create | `get_action_item` (row + `sites.company_id`) + `update_action_item_fields` (whitelisted partial UPDATE) |
| `tests/unit/test_action_items_repo.py` | Create | unit: UPDATE SQL shape, whitelist, empty-fields short-circuit (FakeConn) |
| `tests/integration/test_action_items_repository.py` | Create | integration: real UPDATE + company-join fetch + cross-company None |
| `src/lambda_org_api.py` | Modify | `patch_action_item` handler + dispatch route + header doc line; expose `id`+`status` in `render_report_shape` |
| `tests/unit/test_lambda_org_api.py` | Modify | unit: PATCH ACL (admin/pm/site_manager/assignee/outsider), member-validation, field validation, id/status surfaced |
| `fieldsight-ui/scripts/api/actions.js` | Modify | add `updateAction(id, patch)` → `PATCH /api/org/action-items/{id}` |
| `fieldsight-ui/scripts/api/today-adapter.js` | Modify | thread `actionItemId` + `siteId`; `deriveStatus` reads the `status` column (overlay fallback) |
| `fieldsight-ui/scripts/composites/task-card.js` | Modify | check-off routes to `updateAction(status)` when the item carries an id (fallback to legacy toggle) |
| `fieldsight-ui/scripts/pages/today.js` (+ timeline task-detail) | Modify | task-detail editors: priority/status/due selects + assignee picker from `org.getSiteMembers` |
| `fieldsight-ui/app-shell-preview.html` (+ other preview HTMLs loading these) | Modify | bump `?v=N` cache-busters |

---

### Task 1: Backend — `PATCH /api/org/action-items/{id}` (priority/status/deadline/responsible) with site-authority ACL + member-validated reassignment

**Why:** No update route for an action item exists (`src/lambda_org_api.py:156-256` has none; legacy only toggles a DynamoDB boolean, `src/lambda_fieldsight_api.py:609-649`). This is the whole feature's write path. It also surfaces `id`+`status` in the read shim so the card can address the row and show the real status.

**Files:**
- Create: `src/migrations/0016_action_item_audit.sql`, `src/repositories/action_items.py`, `tests/unit/test_action_items_repo.py`, `tests/integration/test_action_items_repository.py`
- Modify: `src/lambda_org_api.py`, `tests/unit/test_lambda_org_api.py`

**Interfaces:**
- Produces (repo): `action_items.get_action_item(conn, id) -> dict|None` — the row plus `sites.company_id` (tenant guard); `None` on missing / malformed uuid (mirrors `observations.get_observation`).
- Produces (repo): `action_items.update_action_item_fields(conn, id, fields, updated_by) -> dict|None` — whitelisted partial `UPDATE`, `updated_at=now()`, RETURNING the row.
- Produces (handler): `PATCH /api/org/action-items/{id}` → the updated item row (200) / 400 / 403 / 404.
- Consumes: `_allowed_site_ids`, `resolve_scope`, `memberships.caller_site_roles`, `memberships.members_for_site`, `REPORT_DATE_RE`.

- [ ] **Step 1: Write the migration**

Create `src/migrations/0016_action_item_audit.sql` (last migration is `0015_platform_company.sql`; simple `ALTER`, same style as `0013_site_address.sql`):

```sql
-- 0016: editable action items (editable-tasks-reassignment spec §3.6) —
-- minimal last-writer audit, mirroring observations.updated_at. No
-- company_id (reached via action_items.site_id -> sites.company_id) and no
-- version column (last-write-wins). Both nullable: existing rows predate
-- any edit and correctly read NULL until first PATCHed.
ALTER TABLE action_items ADD COLUMN updated_at timestamptz;
ALTER TABLE action_items ADD COLUMN updated_by text;
```

- [ ] **Step 2: Write the failing repo unit tests**

Create `tests/unit/test_action_items_repo.py` (copy the `FakeConn`/`FakeCursor` recording harness from `tests/unit/test_topics_repo.py`; `from repositories import action_items`):

```python
def test_update_action_item_fields_builds_whitelisted_set_and_audit():
    conn = FakeConn(results=[[{"id": "a-1", "status": "done"}]])
    out = action_items.update_action_item_fields(
        conn, "a-1", {"status": "done", "priority": "high"}, "sub-9")
    assert out == {"id": "a-1", "status": "done"}
    call = conn.calls[0]
    assert "UPDATE action_items SET" in call["sql"]
    assert "status=%s" in call["sql"] and "priority=%s" in call["sql"]
    assert "updated_at=now()" in call["sql"] and "updated_by=%s" in call["sql"]
    assert "WHERE id=%s" in call["sql"]
    # values in column order, then updated_by, then the id
    assert call["params"] == ["done", "high", "sub-9", "a-1"]


def test_update_action_item_fields_ignores_non_whitelisted_keys():
    conn = FakeConn(results=[[{"id": "a-1"}]])
    action_items.update_action_item_fields(
        conn, "a-1", {"status": "open", "site_id": "hack", "text": "hax"}, "sub-9")
    sql = conn.calls[0]["sql"]
    assert "site_id=%s" not in sql and "text=%s" not in sql   # not editable


def test_update_action_item_fields_empty_short_circuits():
    conn = FakeConn(results=[])
    assert action_items.update_action_item_fields(conn, "a-1", {}, "sub-9") is None
    assert conn.calls == []                                    # no round-trip
```

- [ ] **Step 3: Run — verify fail** — `uv run pytest tests/unit/test_action_items_repo.py -v` → FAIL (`ModuleNotFoundError`/`AttributeError`, module/functions absent).

- [ ] **Step 4: Implement `src/repositories/action_items.py`**

```python
import psycopg
from psycopg.rows import dict_row

# Whitelist of columns this route may set. text/site_id/topic_id/created_at
# are intentionally NOT here — editing a task must never re-home it to another
# site (ACL bypass) or rewrite its body.
_EDITABLE = ("priority", "status", "deadline", "deadline_text", "responsible")
_RET = ("id, topic_id, site_id, text, responsible, deadline, deadline_text, "
        "priority, status, created_at, updated_at, updated_by")


def get_action_item(conn, action_item_id) -> dict | None:
    """One action item joined to its site's company_id (the tenant guard the
    handler checks against caller.company_id). Returns None if not found or
    action_item_id is not a valid UUID -- malformed id == missing (404
    semantics), same posture as observations.get_observation."""
    try:
        return conn.cursor(row_factory=dict_row).execute(
            f"SELECT {_RET.replace('id,', 'a.id,').replace(', ', ', a.')}, "
            f"s.company_id "
            f"FROM action_items a JOIN sites s ON s.id = a.site_id WHERE a.id=%s",
            (action_item_id,),
        ).fetchone()
    except psycopg.Error:
        conn.rollback()
        return None


def update_action_item_fields(conn, action_item_id, fields, updated_by) -> dict | None:
    """Whitelisted partial update + last-writer audit (spec §3.6). Only keys in
    _EDITABLE are written; updated_at=now(), updated_by=caller sub. Empty
    editable set -> None without a round-trip. None on missing / malformed
    uuid (mirrors observations.set_status)."""
    cols = [c for c in _EDITABLE if c in fields]
    if not cols:
        return None
    set_sql = ", ".join(f"{c}=%s" for c in cols) + ", updated_at=now(), updated_by=%s"
    params = [fields[c] for c in cols] + [updated_by, action_item_id]
    try:
        return conn.cursor(row_factory=dict_row).execute(
            f"UPDATE action_items SET {set_sql} WHERE id=%s RETURNING {_RET}",
            params,
        ).fetchone()
    except psycopg.Error:
        conn.rollback()
        return None
```

(If the `_RET.replace(...)` aliasing reads awkwardly to the implementer, inline the aliased column list literally — the requirement is only that `get_action_item` returns the item columns plus `s.company_id`.)

- [ ] **Step 5: Run repo unit tests — verify pass** — `uv run pytest tests/unit/test_action_items_repo.py -v` → 3 passed.

- [ ] **Step 6: Write + run the failing repo integration test**

Create `tests/integration/test_action_items_repository.py` (uses the `db` fixture; reuse the module's company/site/topic seed helpers where present):

```python
import datetime as _dt
import pytest
from repositories import action_items


@pytest.mark.integration
def test_get_and_update_action_item_roundtrip(db):
    cid = db.execute("INSERT INTO companies (name) VALUES ('A') RETURNING id").fetchone()[0]
    sid = db.execute("INSERT INTO sites (company_id, name) VALUES (%s,'S') RETURNING id", (cid,)).fetchone()[0]
    tid = db.execute("INSERT INTO topics (site_id, report_date, title) VALUES (%s,%s,'t') RETURNING id",
                     (sid, _dt.date(2026, 7, 18))).fetchone()[0]
    aid = db.execute("INSERT INTO action_items (topic_id, site_id, text) VALUES (%s,%s,'do X') RETURNING id",
                     (tid, sid)).fetchone()[0]

    row = action_items.get_action_item(db, str(aid))
    assert str(row["company_id"]) == str(cid)          # tenant guard column present
    assert row["status"] == "open"                     # default

    updated = action_items.update_action_item_fields(
        db, str(aid), {"status": "done", "priority": "high",
                       "responsible": "Neo Tan", "deadline": _dt.date(2026, 7, 20)}, "sub-9")
    assert updated["status"] == "done" and updated["priority"] == "high"
    assert updated["responsible"] == "Neo Tan"
    assert updated["updated_by"] == "sub-9" and updated["updated_at"] is not None


@pytest.mark.integration
def test_get_action_item_malformed_uuid_is_none(db):
    assert action_items.get_action_item(db, "not-a-uuid") is None
```

Run: `uv run pytest tests/integration/test_action_items_repository.py -v` (SKIPs without `TEST_DATABASE_URL`; green in CI).

- [ ] **Step 7: Surface `id` + `status` in the read shim (with a unit test)**

Extend `render_report_shape`'s `action_items` projection (`src/lambda_org_api.py:1327-1329`). Single-line Edit anchor — replace the `action_items` list-comp value so each item also carries its durable id + authoritative status (both already selected in `list_topics_for_source_prefix`, `topics.py:302-308`):

```python
            "action_items": [{"id": str(a["id"]), "action": a["text"],
                              "responsible": a["responsible"],
                              "deadline": a["deadline_text"] or (str(a["deadline"]) if a["deadline"] else None),
                              "priority": a["priority"], "status": a["status"]} for a in t["action_items"]],
```

Add to `tests/unit/test_lambda_org_api.py` (the module has render_report_shape coverage; assert the two new keys):

```python
def test_render_report_shape_exposes_action_item_id_and_status():
    rows = [{"id": "t-1", "site_name": "S", "user_name": "U", "title": "T",
             "time_range": None, "category": "progress", "participants": None,
             "summary": "s", "findings": [], "safety_observations": [], "photos": [],
             "action_items": [{"id": "a-1", "text": "do X", "responsible": "Neo Tan",
                               "deadline": None, "deadline_text": "Tomorrow",
                               "priority": "high", "status": "done"}]}]
    out = org.render_report_shape(rows, {}, "2026-07-18", "Neo_Tan")
    item = out["topics"][0]["action_items"][0]
    assert item["id"] == "a-1" and item["status"] == "done"
    assert item["action"] == "do X" and item["responsible"] == "Neo Tan"
```

- [ ] **Step 8: Write the failing handler unit tests**

Append to `tests/unit/test_lambda_org_api.py` (harness: `make_event`, `wired`, `body_of`, `CALLER` (admin), `SITE_ID`/`OTHER_SITE_ID`). `AITEM` is a helper row; stub `get_action_item`/`update_action_item_fields`/`caller_site_roles`/`members_for_site`:

```python
AITEM = {"id": "a-1", "site_id": SITE_ID, "company_id": "c-uuid-1",
         "responsible": "Ada Owner", "status": "open", "priority": "low"}


def _wire_item(wired, item=AITEM, roles=None, members=None):
    wired.setattr(org.action_items, "get_action_item", lambda conn, i: dict(item))
    wired.setattr(org, "_allowed_site_ids", lambda conn, caller: {item["site_id"]})
    wired.setattr(org.memberships, "caller_site_roles", lambda conn, uid: roles or {})
    wired.setattr(org.memberships, "members_for_site",
                  lambda conn, cid, sid: members or [{"first_name": "Neo", "last_name": "Tan"}])
    seen = {}
    wired.setattr(org.action_items, "update_action_item_fields",
                  lambda conn, i, fields, by: (seen.update(fields=fields, by=by) or {**item, **fields}))
    return seen


def test_patch_action_item_admin_updates_priority(wired):
    seen = _wire_item(wired)                                   # CALLER is admin (resolve_scope ALL)
    res = org.lambda_handler(make_event("PATCH", "/api/org/action-items/a-1",
                                        body={"priority": "medium"}), None)
    assert res["statusCode"] == 200
    assert seen["fields"] == {"priority": "medium"} and seen["by"] == CALLER["cognito_sub"]


def test_patch_action_item_site_manager_of_site_may_edit(wired):
    wired.setattr(org.users, "get_user_by_sub", lambda conn, sub: {**CALLER, "global_role": "worker"})
    seen = _wire_item(wired, roles={SITE_ID: "site_manager"})  # membership authority, not admin
    res = org.lambda_handler(make_event("PATCH", "/api/org/action-items/a-1",
                                        body={"status": "blocked"}), None)
    assert res["statusCode"] == 200 and seen["fields"] == {"status": "blocked"}


def test_patch_action_item_current_assignee_may_edit_own(wired):
    caller = {**CALLER, "global_role": "worker", "first_name": "Ada", "last_name": "Owner"}
    wired.setattr(org.users, "get_user_by_sub", lambda conn, sub: caller)
    seen = _wire_item(wired, roles={})                         # no site role, but IS the assignee
    res = org.lambda_handler(make_event("PATCH", "/api/org/action-items/a-1",
                                        body={"status": "done"}), None)
    assert res["statusCode"] == 200 and seen["fields"] == {"status": "done"}


def test_patch_action_item_outsider_worker_denied_403(wired):
    wired.setattr(org.users, "get_user_by_sub", lambda conn, sub: {**CALLER, "global_role": "worker",
                                                                   "first_name": "X", "last_name": "Y"})
    _wire_item(wired, roles={SITE_ID: "worker"})               # worker on the site, not the assignee
    res = org.lambda_handler(make_event("PATCH", "/api/org/action-items/a-1",
                                        body={"status": "done"}), None)
    assert res["statusCode"] == 403


def test_patch_action_item_site_out_of_reach_403(wired):
    wired.setattr(org.action_items, "get_action_item",
                  lambda conn, i: {**AITEM, "site_id": OTHER_SITE_ID})
    wired.setattr(org, "_allowed_site_ids", lambda conn, caller: {SITE_ID})   # not OTHER_SITE_ID
    called = []
    wired.setattr(org.action_items, "update_action_item_fields", lambda *a, **k: called.append(1))
    res = org.lambda_handler(make_event("PATCH", "/api/org/action-items/a-1",
                                        body={"priority": "low"}), None)
    assert res["statusCode"] == 403 and called == []          # never written


def test_patch_action_item_cross_company_row_404(wired):
    wired.setattr(org.action_items, "get_action_item",
                  lambda conn, i: {**AITEM, "company_id": "OTHER-CO"})
    res = org.lambda_handler(make_event("PATCH", "/api/org/action-items/a-1",
                                        body={"priority": "low"}), None)
    assert res["statusCode"] == 404


def test_patch_action_item_reassign_to_site_member_ok(wired):
    seen = _wire_item(wired, members=[{"first_name": "Neo", "last_name": "Tan"}])
    res = org.lambda_handler(make_event("PATCH", "/api/org/action-items/a-1",
                                        body={"responsible": "Neo Tan"}), None)
    assert res["statusCode"] == 200 and seen["fields"] == {"responsible": "Neo Tan"}


def test_patch_action_item_reassign_to_non_member_400(wired):
    _wire_item(wired, members=[{"first_name": "Neo", "last_name": "Tan"}])
    res = org.lambda_handler(make_event("PATCH", "/api/org/action-items/a-1",
                                        body={"responsible": "Someone Else"}), None)
    assert res["statusCode"] == 400


def test_patch_action_item_bad_status_400(wired):
    _wire_item(wired)
    res = org.lambda_handler(make_event("PATCH", "/api/org/action-items/a-1",
                                        body={"status": "finished"}), None)
    assert res["statusCode"] == 400


def test_patch_action_item_empty_body_400(wired):
    _wire_item(wired)
    res = org.lambda_handler(make_event("PATCH", "/api/org/action-items/a-1", body={}), None)
    assert res["statusCode"] == 400
```

- [ ] **Step 9: Run — verify fail** — `uv run pytest tests/unit/test_lambda_org_api.py -k action_item -v` → FAIL (404 not found; route+handler absent).

- [ ] **Step 10: Implement the handler + route**

In `src/lambda_org_api.py`:

(a) Add the enums near the other `ALLOWED_*` sets (~line 88):
```python
ALLOWED_ACTION_STATUS = {"open", "in_progress", "blocked", "done"}
ALLOWED_ACTION_PRIORITY = {"low", "medium", "high"}
```

(b) Import the new repo in the `from repositories import (...)` tuple (line 57): add `action_items`.

(c) Add the route in `dispatch`, next to the observations PATCH (after :217). GET-free, `$`-anchored so it can't collide:
```python
    m_ai = re.match(r"^/action-items/([^/]+)$", route)
    if m_ai and method == "PATCH":
        return patch_action_item(conn, caller, m_ai.group(1), parse_body(event))
```

(d) Add the handler beside `patch_observation_status` (after :891). Reuses `_allowed_site_ids` (reach), `resolve_scope`/`caller_site_roles` (authority), `members_for_site` (reassignment target), `REPORT_DATE_RE` (deadline):
```python
def _display_name(caller):
    return " ".join(p for p in (caller.get("first_name"), caller.get("last_name")) if p).strip()


def patch_action_item(conn, caller, action_item_id, body):
    """Edit priority/status/deadline/responsible on one action item (spec §3).
    ACL mirrors patch_observation_status widened to site authority: the task's
    site must be in the caller's reach, and the caller must be admin/gm, a
    pm/site_manager of THAT site, or the current assignee. Reassignment target
    must be a member of the task's site. Addressed by durable action_items.id."""
    if body is None:
        return error("malformed JSON body", 400)
    row = action_items.get_action_item(conn, action_item_id)
    if row is None or str(row["company_id"]) != str(caller["company_id"]):
        return error("action item not found", 404)            # incl. cross-company
    site_id = str(row["site_id"])
    if site_id not in _allowed_site_ids(conn, caller):
        return error("access denied to this task's site", 403)  # reach gate
    site_role = memberships.caller_site_roles(conn, caller["id"]).get(site_id)
    is_admin = resolve_scope(caller["global_role"]) == "ALL"
    is_site_authority = site_role in ("pm", "site_manager")
    is_assignee = bool(row["responsible"]) and row["responsible"] == _display_name(caller)
    if not (is_admin or is_site_authority or is_assignee):
        return error("admin/gm, this site's pm/site_manager, or the assignee only", 403)

    fields = {}
    if "priority" in body:
        if body["priority"] not in ALLOWED_ACTION_PRIORITY:
            return error(f"priority must be one of {sorted(ALLOWED_ACTION_PRIORITY)}", 400)
        fields["priority"] = body["priority"]
    if "status" in body:
        if body["status"] not in ALLOWED_ACTION_STATUS:
            return error(f"status must be one of {sorted(ALLOWED_ACTION_STATUS)}", 400)
        fields["status"] = body["status"]
    if "deadline" in body:
        dl = body["deadline"]
        if dl is not None and not (isinstance(dl, str) and REPORT_DATE_RE.match(dl)):
            return error("deadline must be YYYY-MM-DD or null", 400)
        fields["deadline"] = dl                               # write both so the
        fields["deadline_text"] = dl                          # date + free-text mirror agree (§3.5)
    if "responsible" in body:
        target = body["responsible"]
        if not isinstance(target, str) or not target.strip():
            return error("responsible must be a non-empty display name", 400)
        target = target.strip()
        member_names = {" ".join(p for p in (m.get("first_name"), m.get("last_name")) if p).strip()
                        for m in memberships.members_for_site(conn, caller["company_id"], site_id)}
        if target not in member_names:
            return error("assignee must be a member of this site", 400)
        fields["responsible"] = target
    if not fields:
        return error("no editable fields provided", 400)

    updated = action_items.update_action_item_fields(conn, action_item_id, fields, caller["cognito_sub"])
    if updated is None:
        return error("action item not found", 404)
    return ok(updated)
```

(e) Add `PATCH /api/org/action-items/{id}` to the route docstring block (~line 27, next to the observations PATCH line).

- [ ] **Step 11: Run — verify pass, then full unit suite** — `uv run pytest tests/unit/test_lambda_org_api.py -k "action_item or render_report_shape" -v` → all green. Then `uv run pytest tests/unit -q` → no regressions.

- [ ] **Step 12: Commit**

```bash
git add src/migrations/0016_action_item_audit.sql src/repositories/action_items.py tests/unit/test_action_items_repo.py tests/integration/test_action_items_repository.py src/lambda_org_api.py tests/unit/test_lambda_org_api.py
git commit -m "feat(org-api): PATCH /api/org/action-items/{id} — edit priority/status/deadline + member-validated reassignment (site-authority ACL)"
```

**Done:** An action item's priority/status/deadline/assignee can be edited through the Aurora org-api, gated by site-authority ACL, reassignment validated against the site roster; the read shim exposes the durable id + status.

---

### Task 2: UI — task-detail editors (priority / status / due / assignee picker) wired to `updateAction`

**Why:** The card renders these fields read-only (`scripts/composites/task-card.js`); nothing writes them. Add the editors on the task detail and the `updateAction` client, sourcing the assignee list from the Phase-2 roster.

**Files:**
- Modify: `fieldsight-ui/scripts/api/actions.js` (add `updateAction`)
- Modify: `fieldsight-ui/scripts/api/today-adapter.js` (thread `actionItemId` + `siteId`)
- Modify: `fieldsight-ui/scripts/pages/today.js` (+ Timeline task-detail) — editors
- Modify: preview HTML cache-busters

**Interfaces:**
- `window.FS.api.actions.updateAction(actionItemId, patch) -> Promise<item>` — `PATCH /api/org/action-items/{id}` via `orgRequest` (`scripts/api/_fetch.js:235`, supports `{method,body}`); mock returns the merged patch.
- Every derived task item gains `actionItemId` (durable `a.id` from the read shim) and `siteId` (the org site id for the assignee picker).

- [ ] **Step 1: Add `updateAction` to `actions.js`**

In `fieldsight-ui/scripts/api/actions.js`, add beside `createAction` (after :195) and to the exports object (:244). Uses `orgRequest` (org backend) since this is an Aurora write, not a report-gateway one:

```javascript
  /* Editable tasks (spec §3.1) — PATCH one action item's editable fields
     (priority/status/deadline/responsible) by its durable action_items.id.
     Aurora org write, so it rides orgRequest (not the report gateway).
     Mock merges the patch so the detail editors demo without a backend. */
  async function updateAction(actionItemId, patch) {
    if (!window.FS.api.useMocks && !window.FS.api.writeMocks) {
      return window.FS.api.orgRequest('/action-items/' + encodeURIComponent(actionItemId), {
        method: 'PATCH',
        body:   patch || {},
      });
    }
    await window.FS.api.delay(60);
    return Object.assign({ id: actionItemId }, patch || {});
  }
```

Add `updateAction: updateAction,` to `window.FS.api.actions = {...}`.

- [ ] **Step 2: Thread `actionItemId` + `siteId` through the adapter**

In `fieldsight-ui/scripts/api/today-adapter.js`, in the task object built in the `action_items.forEach` (around :378-424), add two keys (single-line additions):

```javascript
          actionItemId: a.id || null,   /* durable action_items.id (read shim §1.5) — PATCH handle */
          siteId:       (ctx.siteIdByName && ctx.siteIdByName[siteName]) || ctx.siteId || null,
```

(`siteIdByName`/`siteId` are provided by today.js the same way `siteSlugByName` already is; on a miss the assignee picker degrades to disabled, never crashes.)

- [ ] **Step 3: `deriveStatus` reads the authoritative column (overlay fallback)**

Replace `deriveStatus` (`today-adapter.js:91-94`) so the `action_items.status` column drives the badge, with the DynamoDB check-off kept ONLY as a legacy fallback for rows the column never got (spec §3.4):

```javascript
  /* Status is now the authoritative action_items.status column (spec §3.4).
     The DynamoDB check-off boolean is a legacy fallback only: used when the
     item carries no column status (pre-migration days), so a historical
     check-off never visibly reverts. */
  var STATUS_TONE = { done: 'success', in_progress: 'info', blocked: 'magenta', open: 'info' };
  function deriveStatus(columnStatus, checked) {
    var s = columnStatus || (checked ? 'done' : 'open');
    var label = s === 'in_progress' ? 'In progress' : s.charAt(0).toUpperCase() + s.slice(1);
    return { status: label, statusTone: STATUS_TONE[s] || 'info' };
  }
```

Update its call site (:375-377) to pass the column first:
```javascript
        var status = deriveStatus(a.status, checked);
```

- [ ] **Step 4: Add the editors to the task detail**

In the Today (and Timeline) task-detail panel — where `['Due', task.dueTime]` and the status badge already render — add controls, each firing `FS.api.actions.updateAction(task.actionItemId, {...})` and refreshing on success:
- **Priority**: a `<select>` (Low/Medium/High) → `{priority}`.
- **Status**: a `<select>` (Open/In progress/Blocked/Done) → `{status}`.
- **Due**: a date input (BUG-19: format via `FS.api` helpers, never `new Date(str)`) → `{deadline}` as `YYYY-MM-DD` or `null` to clear.
- **Assignee**: a picker populated by `await FS.api.org.getSiteMembers(task.siteId)` → `users[].name`; choosing one → `{responsible: name}`. Disabled (read-only text) when `task.siteId` is falsy or the roster is empty.

Gate visibility of the editors with the existing `FS.canDo` role check so a plain worker viewing a team task sees them read-only (the backend is the real gate; this is UX). On a `_accessDenied`/error response show the existing toast and revert the control.

- [ ] **Step 5: Syntax-check + grep verification**

```bash
node --check fieldsight-ui/scripts/api/actions.js
node --check fieldsight-ui/scripts/api/today-adapter.js
node --check fieldsight-ui/scripts/pages/today.js
grep -n "updateAction" fieldsight-ui/scripts/api/actions.js            # defined + exported
grep -n "orgRequest('/action-items" fieldsight-ui/scripts/api/actions.js
grep -n "getSiteMembers" fieldsight-ui/scripts/pages/today.js          # assignee picker sources the roster
```

- [ ] **Step 6: Bump cache-busters + manual verification (state done vs deferred)**

Bump `?v=N` for `api/actions.js`, `api/today-adapter.js`, `pages/today.js` (+ timeline) in `app-shell-preview.html` and any other preview HTML loading them. With `useMocks=0`, `orgBaseUrl` set: as a site_manager, change a task's priority/status/due and reassign it to another site member; confirm the PATCH fires and the card reflects the new values. Reassign to yourself on a second account → the task appears on your Today. Real-browser check may be deferred to the user; state which.

- [ ] **Step 7: Commit**

```bash
git add fieldsight-ui/scripts/api/actions.js fieldsight-ui/scripts/api/today-adapter.js fieldsight-ui/scripts/pages/today.js fieldsight-ui/app-shell-preview.html
git commit -m "feat(ui): editable task detail (priority/status/due/assignee) wired to PATCH /action-items; deriveStatus reads status column"
```

**Done:** The task detail edits priority/status/due and reassigns to a validated site member; status now reflects the authoritative column.

---

### Task 3: Reconcile the status / check-off model onto the column

**Why:** The round check-off button still writes the DynamoDB overlay (`task-card.js:113` → `toggleAction` → `POST /api/actions/toggle`, `lambda_fieldsight_api.py:609`), which is a second source of "done" the reassignment key can't follow (spec §1.3/§3.4). Fold check-off into the `status` column so there is ONE source of truth.

**Files:**
- Modify: `fieldsight-ui/scripts/composites/task-card.js` (route check-off to `updateAction` when an id is present)
- Modify: preview HTML cache-busters

**Interfaces:**
- Check-off, when the task carries `actionItemId`, calls
  `FS.api.actions.updateAction(id, {status: checked ? 'done' : 'open'})`; falls
  back to the legacy `toggleAction` only for items with no `actionItemId`
  (older days whose read shim predates Task 1). Same optimistic
  animation/rollback contract.

- [ ] **Step 1: Route the check-off through the column**

In `fieldsight-ui/scripts/composites/task-card.js`, in `startCheckOff` (:101-129), branch on `task.actionItemId`. When present, PATCH the status column instead of the DynamoDB toggle; keep the exact same `setCheckingOff(false)` rollback on failure:

```javascript
      var api = window.FS && window.FS.api && window.FS.api.actions;
      if (!api) return;

      /* Editable-tasks reconciliation (spec §3.4): a check-off is now a
         status transition on the authoritative action_items.status column,
         keyed by the durable id. Legacy items with no actionItemId (read
         shim predates the id surfacing) still use the DynamoDB toggle. */
      var persist = task.actionItemId
        ? api.updateAction(task.actionItemId, { status: 'done' })
        : api.toggleAction({
            date: props.date, topic_id: task.topic_id, action_index: task.actionIndex,
            checked: true, action_text: task.title, user_folder: task.folder,
          });
      persist.catch(function (err) {
        console.error('[TaskCard] check-off failed', err);
        setCheckingOff(false);
      });
```

- [ ] **Step 2: Syntax-check + grep**

```bash
node --check fieldsight-ui/scripts/composites/task-card.js
grep -n "updateAction(task.actionItemId" fieldsight-ui/scripts/composites/task-card.js
grep -n "toggleAction" fieldsight-ui/scripts/composites/task-card.js   # EXPECT: still present (legacy fallback only)
```

- [ ] **Step 3: Bump cache-busters + manual verification**

Bump `?v=N` for `composites/task-card.js`. With `useMocks=0`: check off a task on a current-shim day → its status becomes Done via the column (survives reload without the overlay); an older day with no `actionItemId` still checks off via the legacy toggle. Defer real-browser confirmation to the user if needed; state which.

- [ ] **Step 4: Commit**

```bash
git add fieldsight-ui/scripts/composites/task-card.js fieldsight-ui/app-shell-preview.html
git commit -m "feat(ui): task check-off writes authoritative status column via PATCH (legacy DynamoDB toggle only for id-less items)"
```

**Done:** Checking a task off writes `action_items.status='done'` on the durable row — one source of truth; the DynamoDB overlay is fallback-only and retirable.

---

### Task 4: PR, merge, deploy, live smoke (handoff — user-gated)

**Files:** none (process). Pipeline and UI are separate repos/PRs.

- [ ] **Step 1: Pipeline PR to `develop`** — `gh pr create --base develop` titled `feat(org-api): editable action items + reassignment (PATCH /api/org/action-items/{id})`. Confirm CI green: `uv run pytest` (new unit + integration). Migration `0016` runs via the existing migrations mechanism; no `template.yaml` change.
- [ ] **Step 2: Merge to `develop`** → deploys `fieldsight-test`. Data-API smoke with a real idToken: `PATCH /api/org/action-items/{id}` with `{priority:"medium"}` returns the updated row; `{responsible:"<a site member>"}` succeeds and `{responsible:"<non-member>"}` 400s; an action item on a site the caller can't reach 403s; a plain worker (not assignee) 403s; an admin succeeds.
- [ ] **Step 3: UI PR** on the FieldSight UI repo (its own branch), Amplify preview with `useMocks=0`, `orgBaseUrl` set: edit priority/status/due, reassign to another member and confirm it moves between two accounts' Today; check-off writes the column and survives reload.
- [ ] **Step 4: Promote `develop`→`main` (prod)** is a SEPARATE user decision (carries whatever else is on develop). Surface it; don't bundle silently. After prod deploy, retiring the legacy DynamoDB check-off write (drop the `toggleAction` fallback once no id-less days remain in the active window) is a follow-up, user-gated.

**Done:** Editable tasks + reassignment live on test, verified; prod promotion + legacy check-off retirement are the user's call.

---

## Self-Review (author)

- **Spec coverage:** §3.1 route → Task 1 handler + dispatch (`test_patch_action_item_admin_updates_priority`). §3.2 permission model → Task 1 ACL (`_allowed_site_ids` reach + `caller_site_roles`/`resolve_scope`/assignee authority; `test_patch_action_item_site_manager_of_site_may_edit`, `_current_assignee_may_edit_own`, `_outsider_worker_denied_403`, `_site_out_of_reach_403`, `_cross_company_row_404`). §3.3 reassignment → `members_for_site` validation (`test_patch_action_item_reassign_to_site_member_ok`, `_to_non_member_400`) + `responsible`=display name so mine-vs-team (`today-adapter.js:425`) moves the task (Task 2 picker sources `getSiteMembers` names). §3.4 status reconciliation → id+status surfaced in `render_report_shape` (`test_render_report_shape_exposes_action_item_id_and_status`), `deriveStatus` reads the column (Task 2 Step 3), check-off folded onto it (Task 3). §3.5 priority/due → enum + `REPORT_DATE_RE` validation (`test_patch_action_item_bad_status_400`, deadline mirror write). §3.6 audit → migration 0016 + `updated_at=now()`/`updated_by` (`test_update_action_item_fields_builds_whitelisted_set_and_audit`, integration `updated_by`/`updated_at` asserts). D1–D7 all land in Tasks 1–3.
- **Reuses existing ACL, not Phase 3:** reach = `_allowed_site_ids` (the SAME helper `/live-items`/`/programme`/`/dates`/`/sites/{id}/members` use); authority = `caller_site_roles` + `resolve_scope`; reassignment roster = `members_for_site`. No `visible_scope`/graded dependency — `caller_site_roles` is flag-independent, so the gate is identical whether `GRADED_ROLES` is on or off. No new ACL primitive invented.
- **Multi-tenant safety:** `get_action_item` returns the joined `sites.company_id`; the handler 404s a row whose company ≠ caller's BEFORE any reach/authority work; the reach gate then blocks cross-project; `members_for_site` is itself company-pinned on both join sides. Three independent guards, none relaxed.
- **Type/addressing consistency:** `get_action_item(conn, id)->dict|None` and `update_action_item_fields(conn, id, fields, updated_by)->dict|None` identical across repo impl / unit / integration / handler; the durable `action_items.id` is the PATCH handle end-to-end (surfaced by `render_report_shape`, threaded as `actionItemId` in the adapter, used by the card + detail). The whitelist (`_EDITABLE`) makes `site_id`/`text`/`topic_id` un-writable — no re-homing a task to bypass ACL (`test_update_action_item_fields_ignores_non_whitelisted_keys`).
- **Routing safety:** `^/action-items/([^/]+)$` (PATCH) is `$`-anchored and method-guarded — cannot collide with `/observations/{id}` (different prefix) or any `/sites/...` route. Rides the existing `/api/org/{proxy+}` integration → no `template.yaml`/SAM change.
- **Reassignment actually moves the task:** proven by the read-side finding — mine-vs-team is `task.assignee === currentUserName` with `assignee=responsible` and `currentUserName=caller.name` (`today-adapter.js:425`, `today.js:212`); the picker writes a validated member's `"First Last"` (`org.js:43,48`), the same string a session resolves to. Documented caveat (member without a matching login) is visibility-only, not data loss (spec §3.3/§7).
- **`uv run pytest` gate:** every backend step names it; integration tests SKIP without `TEST_DATABASE_URL` and are green in CI. **UI no-build:** gates are `node --check` + grep + cache-buster bumps; BUG-19 called out on the due-date input. **Reversibility:** the route is additive; the UI editors are behind `FS.canDo`; the check-off keeps the legacy `toggleAction` fallback so Task 3 is safe to ship before the DynamoDB write is retired.
- **Placeholder scan:** none — every code step carries full code and exact test names; no TBD/"handle edge cases". Line numbers read from the current tree; the `render_report_shape` and `caller_site_roles`/`members_for_site`/`_allowed_site_ids` anchors are from `develop`.
