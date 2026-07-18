# Phase 1 — Identity Enrollment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every login account is linked to its report/recording folder
(`users.folder_name`), so a user sees their **own** Today/Timeline and their
device recordings attribute to them — the foundation the whole visibility model
(spec `2026-07-17-visibility-permission-model-design.md` §3.3, Rollout step 1)
rests on.

**Architecture:** `folder_name` (underscored `safe_name` of the display name) is
the identity bridge between a Cognito login (`users.cognito_sub`) and its S3
report/recording folder. The enrollment primitive `PATCH /api/org/members/{sub}/folder`
already exists (shipped this session, PR #74 → main, pending prod deploy). This
phase makes enrollment **automatic on invite** and **surfaced in the Team UI**,
and backfills existing logins — so enrollment is never a forgotten manual step.

**Tech Stack:** Python/psycopg3 (`lambda_org_api.py`, `repositories/users.py`),
no-build browser React (fieldsight-ui `scripts/pages/team*`), pytest (CI-only —
no local Python, BUG-29).

## Global Constraints
- `folder_name` is stored **underscored** — `safe_name(name)` =
  `re.sub(r'[<>:"/\\|?*\s]', '_', name)` (matches `lambda_orchestrator.safe_name`
  and the enroll route). NEVER store the space form.
- `folder_name` is **globally unique** (migration `0012`,
  `idx_users_folder_global WHERE folder_name IS NOT NULL`). Every writer guards
  the unique-index collision (409/skip), never lets it 500.
- Company-guarded: a member's `folder_name` may only be set within the caller's
  own `company_id`.
- Tests validate in **CI only** (no local Python). Mirror existing FakeConn/`wired`
  patterns; do not claim local pass.
- Windows/CRLF repos: short single-line Edit anchors; NEVER `git add -A`.

---

### Task 1: Auto-enroll `folder_name` on member creation (D4)

**Files:**
- Modify: `src/lambda_org_api.py` — `create_member` (~L488-556)
- Modify: `src/repositories/users.py` — `upsert_user` (accept optional `folder_name`) OR set it in a follow-up call
- Test: `tests/unit/test_lambda_org_api.py`

**Interfaces:**
- Consumes: `users.get_by_folder_name_global(conn, folder)` (collision check),
  the `safe_name`-equivalent regex (copy the one in `patch_member_folder`).
- Produces: after `create_member`, the new user's `folder_name` is set to
  `safe_name("{first} {last}")` when that name is non-empty AND not already taken.

- [ ] **Step 1: Write the failing test**

```python
def test_create_member_auto_enrolls_folder_name(wired):
    # admin invites "Neil Blunden" → folder_name auto-set to "Neil_Blunden"
    body = {"email": "neil@x.com", "first_name": "Neil", "last_name": "Blunden",
            "memberships": [{"site_id": "site-ucpk", "role": "site_manager"}]}
    res = org.lambda_handler(make_event("POST", "/api/org/members", body=body), None)
    assert res["statusCode"] == 201
    # folder_name captured on the users write (mirror how existing create_member tests
    # assert on the FakeConn-captured params / returned user row)
    assert _created_user_folder(wired) == "Neil_Blunden"

def test_create_member_skips_autoenroll_on_folder_collision(wired):
    # another user already owns "Neil_Blunden" → invite still succeeds, folder left unset (logged)
    _seed_existing_folder(wired, "Neil_Blunden", sub="other-sub")
    body = {"email": "neil2@x.com", "first_name": "Neil", "last_name": "Blunden",
            "memberships": []}
    res = org.lambda_handler(make_event("POST", "/api/org/members", body=body), None)
    assert res["statusCode"] == 201
    assert _created_user_folder(wired) is None   # collision → not set, no 500
```

- [ ] **Step 2: Run tests to verify they fail**

Run (CI): `pytest tests/unit/test_lambda_org_api.py -k auto_enroll -v` → FAIL
(auto-enroll not implemented). Locally unavailable (BUG-29) — rely on CI.

- [ ] **Step 3: Implement the minimal change**

In `create_member`, after `user = users.upsert_user(...)` and before/around the
membership loop, derive and set the folder (collision-guarded):

```python
# Auto-enroll the recording-folder identity (D4) so the login is linked to its
# report folder from day one. Underscored safe_name; skip on collision (the
# global unique index would otherwise 500) — folder can be set later via
# PATCH /members/{sub}/folder.
if not user.get("folder_name"):
    fn = re.sub(r'[<>:"/\\|?*\s]', '_', display_name.strip())
    if fn:
        clash = users.get_by_folder_name_global(conn, fn)
        if clash is None or clash["cognito_sub"] == sub:
            user = users.set_folder_name(conn, sub, fn) or user
        else:
            logger.info("create_member: folder_name %r taken, left unset for %s", fn, sub)
```

(`display_name` is already computed in `create_member`; `re` is imported;
`set_folder_name` returns the updated row or None — normalize to keep `user`.)

- [ ] **Step 4: Run tests to verify they pass** — CI: `-k auto_enroll` PASS.

- [ ] **Step 5: Commit**

```bash
git add src/lambda_org_api.py tests/unit/test_lambda_org_api.py
git commit -m "feat(org-api): auto-enroll folder_name on member creation (D4)"
```

---

### Task 2: Backfill enrollment for existing logins

**Files:**
- Create: `src/lambda_org_api.py` — extend the existing `PATCH /members/{sub}/folder`
  is per-user; add a **bulk** admin route `POST /api/org/members/enroll-backfill`
  that enrolls every un-enrolled login whose `safe_name("first last")` is free.
- Test: `tests/unit/test_lambda_org_api.py`

**Interfaces:**
- Consumes: `users` list for the company, `safe_name` regex, collision guard.
- Produces: `{ enrolled: [{sub, folder_name}], skipped: [{sub, reason}] }`.

- [ ] **Step 1: Write the failing test** — admin calls backfill; a member with
  NULL folder_name + free name gets enrolled; one with a colliding name is
  skipped with reason; a member already enrolled is untouched. (Mirror `wired`.)

- [ ] **Step 2: Run (CI) to verify fail.**

- [ ] **Step 3: Implement** the route + `backfill_enroll(conn, caller)` handler:
  admin-only; iterate company members with `folder_name IS NULL`; for each,
  `fn = safe_name("first last")`; if `fn` non-empty and free → `set_folder_name`;
  else skip with reason. Company-guarded throughout.

- [ ] **Step 4: Run (CI) to verify pass.**

- [ ] **Step 5: Commit** `feat(org-api): bulk folder_name backfill for existing logins`.

---

### Task 3: Team UI — show + edit member `folder_name`

**Files:**
- Modify: `scripts/pages/team*.js` (member row / detail) in fieldsight-ui
- Modify: `scripts/api/org.js` — add `setMemberFolder(sub, folder)` →
  `PATCH /members/{sub}/folder`
- Modify: `app-shell-preview.html` cache-buster for changed JS

**Interfaces:**
- Consumes: existing `getMembers()` (rows already carry `folder_name` via
  `_toPageMember`); `org.setMemberFolder`.
- Produces: an admin can view each member's recording-folder identity and set/fix
  it inline.

- [ ] **Step 1** Read the Team page member rendering + how role edit is wired
  (mirror that pattern for a folder_name field). Confirm `getMembers` surfaces
  `folder_name`.
- [ ] **Step 2** Add `setMemberFolder` to `scripts/api/org.js` (live PATCH + mock).
- [ ] **Step 3** Add a "Recording folder" field/inline-edit to the member detail
  (admin-gated, mirror the role editor); on save call `setMemberFolder`, refetch.
- [ ] **Step 4** `node --check` changed JS; bump `?v=N` for changed files.
- [ ] **Step 5** Commit `feat(team): show + edit member recording-folder identity`.

---

## Operational steps (not code — run after the code tasks merge)
- [ ] Deploy pipeline `develop`→`main` (already merged as PR #74; **approve the
  production gate** — run 29552180648) so the enroll endpoint + auto-enroll are
  live on prod.
- [ ] Enroll the current testers via the endpoint / backfill:
  `Ben_UCPK → Ben_UCPK`, and (once invited) `Neil Blunden → Neil_Blunden`,
  `James Alcock → James_Alcock`.
- [ ] Verify: Ben_UCPK's Today shows their own data (not empty-by-mismatch);
  an app/device recording tagged UC PK attributes to them.

## Self-review
- Coverage: spec §3.3 (enrollment) + D4 (auto-enroll) covered by Tasks 1-3 +
  operational. The per-user enroll endpoint (spec's primitive) already shipped.
- Types: `set_folder_name(conn, sub, folder)`, `get_by_folder_name_global(conn,
  folder)` used consistently with `repositories/users.py`.
- Not in this phase: Aurora read-consolidation (Phase 2), graded roles (Phase 3),
  tenant split (Phase 4), content-filter (Part B) — each its own plan.
