# Phase 3 — Graded roles & `visible_scope` (regional_manager, platform_admin, per-user filter) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. This is the **most ACL-sensitive phase** — a mistake is a cross-company or cross-project data leak. Work conservatively, reuse the existing helpers, and do NOT enable graded behavior in prod until every per-role-per-path test is green in CI (§6 of the spec demands this).

**Goal:** Replace the binary `resolve_scope` ({admin,gm}=ALL, else MEMBERSHIPS) with a graded `visible_scope(conn, caller) -> {site_ids, user_scope, self_folder, ...}` (spec §3.1) applied by **every** read path. Add two new global roles — `regional_manager` (D2, cross-project within one company) and `platform_admin` (D6, the sole cross-company tier) — make per-site `membership.role` actually gate within-project visibility (D1), give `/live-items` the missing per-user filter (the R1/R4 gap, a BUG-25-class regression), let `/timeline` show pm/regional/site_manager the timelines of in-scope users (not force-self), and give `platform_admin` cross-company **read** plus `target_company_id` on the create-site/create-member **writes**. Everyone below `platform_admin` stays hard company-pinned. Graded behavior ships **behind a `GRADED_ROLES` flag defaulting OFF**, so nobody silently gains visibility until an environment is explicitly cut over.

**Architecture:** One new primitive plus a feature flag; the read paths keep their current shape and merely source their scope from the primitive.

- **Pure tier logic in `repositories/acl.py`** (still no psycopg — locally unit-testable like `resolve_scope`): `visible_user_scope(global_role, membership_roles) -> 'ALL'|'SITE'|'SELF+WORKERS'|'SELF'` (spec §3.1 table + D1/D3) and `is_cross_company(global_role) -> bool` (D6). `resolve_scope` stays untouched — it is the LEGACY path used when `GRADED_ROLES` is off and by the admin/gm write gates.
- **Two new membership queries** in `repositories/memberships.py`: `caller_site_roles(conn, user_id)` (the per-site `membership.role` map D1 says ACL must now read) and `worker_user_ids_for_sites(conn, site_ids)` (the "workers on the caller's sites" half of a site_manager's `SELF+WORKERS` author set — the graded restatement of BUG-25's fix).
- **`repositories/scope.py` (new)** — `visible_scope(conn, caller)` orchestrates: company pin (or the single `platform_admin` cross-company branch, D6), `site_ids` (reuses `sites.list_company_sites` / `memberships.accessible_site_ids` / new `sites.list_all_sites`), `user_scope` (from `acl.visible_user_scope` over the caller's membership roles), and a resolved `author_ids` set (None = unrestricted). Import graph stays acyclic: `acl` (pure) ← `memberships` ← `scope`; `sites`/`users` import no repos.
- **Read paths route through the primitive** behind `GRADED_ROLES`. `_allowed_site_ids` delegates to `visible_scope(...)["site_ids"]` when graded (so `/sites`, `/dates`, `/programme`, `/rollup`, `/sites/{id}/members` all inherit regional reach + platform_admin cross-company **for free**); `/live-items` and `/dates` gain the per-author filter (`topics.list_topics_for_date` / `list_report_dates` get an optional `author_ids` kwarg); `/timeline` swaps force-self for `_can_view_folder`; `/observations` scopes by the caller's site slugs. When `GRADED_ROLES` is off, every path is byte-for-byte today's behavior.
- **`platform_admin` writes** (`create_member`, `create_org_site`) accept an explicit `target_company_id`; only a `platform_admin` may cross a company or grant `platform_admin`. A seed migration adds the dedicated `FieldSight-platform` company row (D6) so `company_id` stays NOT-NULL everywhere.

**Tech Stack:** Python 3.12, psycopg3 (in-VPC Aurora PG16), SAM `fieldsight-test`/`fieldsight-prod`. Tests: pytest with the repo's `FakeConn`/`wired`/`make_event` harness (unit, validated in **CI** — BUG-29, no local Python) + the `db` fixture (integration, needs `TEST_DATABASE_URL`, set in CI). The pure `acl` tier functions are additionally locally unit-testable (they import no psycopg), mirroring `tests/unit/test_acl_scope.py`.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-17-visibility-permission-model-design.md` — §3.0 (multi-tenancy + platform_admin), §3.1 (the `visible_scope` primitive + role/scope table + `user_scope` semantics), §3.2 (read-path unification), §5 D1–D6, §6 Risks. Phase 1/2 plans (format + house style + what's already built): `docs/superpowers/plans/2026-07-17-phase1-identity-enrollment.md`, `docs/superpowers/plans/2026-07-18-phase2-aurora-read-consolidation.md`. Phase 2 moved reads onto Aurora using **today's binary** scoping; Phase 3 upgrades that scoping to graded, reusing Phase 2's `_allowed_site_ids` / `_resolve_site_param` / `topics.list_*` verbatim.
- **Multi-tenant company pin on EVERY path — non-negotiable.** `platform_admin` is the SOLE exception, and it lives in exactly ONE branch of `visible_scope` (D6). Every other role's `site_ids` is a subset of the caller's `company_id`'s sites. `dispatch` already 403s a caller with no company (`src/lambda_org_api.py:157-158`); Phase 3 never relaxes that.
- **Reuse existing helpers; do not duplicate ACL logic.** `site_ids` comes from `sites.list_company_sites` / `memberships.accessible_site_ids` (today's engine); `_allowed_site_ids` / `_resolve_site_param` are the SAME guards `/live-items`/`/programme`/`/dates` already use. The graded path adds exactly two membership queries + one cross-company `sites` query + the pure tier function — nothing else re-implements scoping.
- **A DB migration is needed ONLY for the platform company seed row, NOT for the roles.** `users.global_role` is `text NOT NULL DEFAULT 'worker'` with **no CHECK, no enum** (`src/migrations/0002_core_relational.sql:16`); `memberships.role` is likewise plain `text NOT NULL` (`0002:35`). A grep of all 14 migrations finds no CHECK/enum on either column. So adding `regional_manager`/`platform_admin` needs **only** the app-level `ALLOWED_GLOBAL_ROLES` edit (`src/lambda_org_api.py:83`). Migration `0015` seeds the `FieldSight-platform` **company** (D6 needs a row to pin platform_admins to, keeping `company_id` non-null) — a data seed, idempotent, no schema/constraint change.
- **Default rollout preserves current behavior (no one silently gains visibility).** Graded behavior is gated by `GRADED_ROLES` (env, default off). Adding the two roles to `ALLOWED_GLOBAL_ROLES` is inert until an admin assigns them (no existing user has them). When `GRADED_ROLES` is off, `_allowed_site_ids` + every read path behave exactly as today (pm/site_manager keep today's self-forced timeline). Cutover is per-environment and user-gated (Task 4), like `AUTHORITY_FLIP` (`PROD_AUTHORITY_FLIP` → env, deploy-prod.yml).
- **Pipeline git:** branch off `develop` (`git checkout -b <name> origin/develop`); NEVER check out `develop` (held by another worktree); NEVER `git add -A` — stage named files only; CRLF repo → single-line Edit anchors. New dev content (comments/commits/docs) in **ENGLISH**.
- **No local Python (BUG-29):** unit tests are asserted to pass in **CI**, not locally. Integration tests SKIP locally without `TEST_DATABASE_URL` and must be green in CI. The ACL tests are the safety net §6 demands — do not enable `GRADED_ROLES` in any env until they pass.
- `template.yaml` gets one new `GRADED_ROLES` env var on the org-api function (mirroring the `AUTHORITY_FLIP` wiring); migration `0015` rides the existing migration runner. No API Gateway/route change — all read paths ride the existing `/api/org/{proxy+}` integration.
- **Performance / UX (2026-07-18 user constraint — bake in, don't add a round-trip):**
  1. **`visible_scope` must resolve a caller's `site_ids` AND per-site roles in ONE membership query, not two.** `memberships.caller_site_roles(conn, user_id) -> {site_id: role}` already returns both; `visible_scope` derives `site_ids = set(roles_map.keys())` and `user_scope` from `roles_map.values()` from that SINGLE result — it must NOT also call `accessible_site_ids` (that would be a redundant second round-trip on the same table/index). The only additional query is `worker_user_ids_for_sites` and ONLY for the `SELF+WORKERS` (site_manager) branch; `SELF`/`SITE`/`ALL` add zero further queries. `platform_admin`'s `list_all_sites` replaces (not adds to) the membership query. Net: graded scope resolution is +0 queries for pm/regional/worker vs today, +1 for site_manager — all small indexed lookups on the already-open request connection. Add a unit assertion (FakeConn call-count) that a non-site_manager `visible_scope` issues exactly one membership query.
  2. **No UI regression in loading/error feedback on the affected read paths.** Phase 3 changes backend scoping behind a flag; the UI (`/today`, `/timeline`, `/live-items` consumers) already renders loading skeletons + error toasts + the offline banner. Task 4 includes a UI-feedback verification step: with `GRADED_ROLES` on in a preview env, confirm the pages that surface newly-visible data (a pm/regional seeing more sites/authors) keep their loading state and never white-screen, and that a read error still surfaces a toast rather than a blank pane. No new UI round-trip is introduced (same endpoints, same request count).

## File Structure

| File | Create/Modify | Responsibility |
|---|---|---|
| `src/repositories/acl.py` | Modify | add pure `visible_user_scope()` + `is_cross_company()` (D1/D2/D3/D6); keep `resolve_scope` |
| `tests/unit/test_acl_scope.py` | Modify | pure unit: tier per role + membership floor + cross-company predicate |
| `src/repositories/memberships.py` | Modify | add `caller_site_roles()` + `worker_user_ids_for_sites()` |
| `src/repositories/sites.py` | Modify | add `list_all_sites()` — the ONE company-agnostic query (platform_admin only) |
| `src/repositories/scope.py` | **Create** | `visible_scope(conn, caller)` — the single ACL primitive |
| `tests/unit/test_visible_scope.py` | **Create** | unit (FakeConn + stubbed repos): site_ids + user_scope + author_ids per role, company-pin, cross-company |
| `tests/integration/test_scope_acl.py` | **Create** | integration (real DB): caller_site_roles / worker ids / cross-project + cross-company isolation |
| `src/repositories/topics.py` | Modify | add optional `author_ids` kwarg to `list_topics_for_date` + `list_report_dates` |
| `tests/unit/test_topics_repo.py` | Modify | unit: `author_ids` adds `t.user_id = ANY(...)`; None → no filter |
| `src/repositories/observations.py` | Modify | add optional `allowed_site_slugs` filter to `list_observations` |
| `src/lambda_org_api.py` | Modify | `GRADED_ROLES` flag; `ALLOWED_GLOBAL_ROLES` += 2; rewire `_allowed_site_ids` + `list_live_items` + `get_org_dates` + `get_timeline_compat` + `list_org_observations`; `_author_filter`/`_can_view_folder` helpers; `create_member`/`create_org_site`/`patch_member_role` `target_company_id` + platform_admin grants |
| `tests/unit/test_lambda_org_api.py` | Modify | per-path × per-role ACL tests; write-path target_company_id + grant guards; graded-off regression |
| `src/migrations/0015_platform_company.sql` | **Create** | idempotent seed of the `FieldSight-platform` company row (D6) |
| `tests/integration/test_migrations.py` (or existing migration test) | Modify | integration: platform company row present after migrate |
| `template.yaml` | Modify | add `GRADED_ROLES` env var to the org-api function (Task 4) |

---

### Task 1: Role model + the `visible_scope` primitive (additive, unwired)

**Why:** Build and exhaustively test the graded primitive BEFORE any read path depends on it. Nothing here changes runtime behavior: the new roles are inert until assigned, and `visible_scope` is not yet called by any endpoint. This is the piece §6 says must be test-covered per role before graded roles are enabled.

**Files:**
- Modify: `src/repositories/acl.py`, `tests/unit/test_acl_scope.py`
- Modify: `src/repositories/memberships.py`, `src/repositories/sites.py`
- Create: `src/repositories/scope.py`, `tests/unit/test_visible_scope.py`, `tests/integration/test_scope_acl.py`
- Modify: `src/lambda_org_api.py` (only `ALLOWED_GLOBAL_ROLES` here)

**Interfaces:**
- `acl.visible_user_scope(global_role: str, membership_roles) -> str` — one of `ALL|SITE|SELF+WORKERS|SELF`. Pure.
- `acl.is_cross_company(global_role: str) -> bool` — True only for `platform_admin`. Pure.
- `memberships.caller_site_roles(conn, user_id) -> dict[str, str]` — `{site_id_str: role}` over non-archived memberships.
- `memberships.worker_user_ids_for_sites(conn, site_ids) -> set[str]` — distinct worker user_ids on those sites; `set()` on empty input without a round-trip.
- `sites.list_all_sites(conn, include_archived=False) -> list[dict]` — every company's sites (platform_admin only).
- `scope.visible_scope(conn, caller) -> dict` with keys `site_ids: set[str]`, `user_scope: str`, `self_folder: str|None`, `self_user_id: str`, `author_ids: set[str]|None` (None = unrestricted), `company_id`, `cross_company: bool`.

- [ ] **Step 1: Write the failing pure tier tests**

Append to `tests/unit/test_acl_scope.py` (already `from repositories.acl import resolve_scope`; add the two new imports):

```python
from repositories.acl import is_cross_company, visible_user_scope


def test_visible_user_scope_all_roles_have_no_author_filter():
    for r in ("platform_admin", "admin", "gm"):
        assert visible_user_scope(r, set()) == "ALL"


def test_visible_user_scope_regional_is_site():
    assert visible_user_scope("regional_manager", set()) == "SITE"


def test_visible_user_scope_pm_global_or_membership_is_site():
    assert visible_user_scope("pm", set()) == "SITE"
    # D1: a global 'worker' who holds a pm membership still gets SITE
    assert visible_user_scope("worker", {"pm"}) == "SITE"


def test_visible_user_scope_site_manager_is_self_plus_workers():
    assert visible_user_scope("site_manager", set()) == "SELF+WORKERS"
    assert visible_user_scope("worker", {"site_manager"}) == "SELF+WORKERS"


def test_visible_user_scope_worker_is_self():
    assert visible_user_scope("worker", set()) == "SELF"
    assert visible_user_scope("worker", {"worker"}) == "SELF"


def test_visible_user_scope_membership_is_a_floor_not_a_cap():
    # a global regional_manager who is only a worker on a site is still SITE
    assert visible_user_scope("regional_manager", {"worker"}) == "SITE"


def test_is_cross_company_only_platform_admin():
    assert is_cross_company("platform_admin") is True
    for r in ("admin", "gm", "regional_manager", "pm", "site_manager", "worker"):
        assert is_cross_company(r) is False
```

- [ ] **Step 2: Run — verify fail** — CI: `pytest tests/unit/test_acl_scope.py -v` → FAIL (`ImportError: cannot import name 'visible_user_scope'`). (Pure; also fails locally if Python were available — BUG-29 makes CI the gate.)

- [ ] **Step 3: Implement the pure tier logic in `acl.py`**

Append to `src/repositories/acl.py` (keep `resolve_scope` and `_ALL_ROLES` exactly as-is — they remain the legacy path):

```python
# --- Graded roles (visibility spec §3.1, D1/D2/D3/D6). resolve_scope above is
# the LEGACY binary primitive still used by the admin/gm write gates and by the
# read paths while GRADED_ROLES is off; the two functions below are the graded
# WITHIN-project authority + cross-company predicate used once GRADED_ROLES is
# on. Kept PURE (no psycopg import) so they stay locally unit-testable, like
# resolve_scope. ---

_CROSS_COMPANY_ROLES = {"platform_admin"}                 # D6: the ONLY cross-company tier
_ALL_USER_SCOPE_ROLES = {"platform_admin", "admin", "gm"}  # no per-author filter
_SITE_USER_SCOPE_ROLES = {"regional_manager", "pm"}        # D2: cross-project within one company; pm


def is_cross_company(global_role: str) -> bool:
    """D6: platform_admin is the sole role whose visibility crosses the company
    boundary. Every other role is hard-pinned to caller.company_id."""
    return global_role in _CROSS_COMPANY_ROLES


def visible_user_scope(global_role: str, membership_roles) -> str:
    """The caller's WITHIN-project authority — the per-author filter tier a read
    path applies on top of site_ids (visibility spec §3.1 table + D1/D3).
    global_role sets cross-project REACH (regional/gm/admin/platform); the
    per-site membership.role sets within-project AUTHORITY (pm/site_manager/
    worker). membership_roles = the set of the caller's membership.role values
    across their accessible sites; it is a FLOOR over global_role (D1's
    Neil-at-UC-PK example: a global 'worker' with a pm membership -> SITE).
    Returns one of:
      ALL          -> no per-author filter (platform_admin/admin/gm)
      SITE         -> every author on an in-scope site (regional_manager or pm)
      SELF+WORKERS -> own + worker-role members on the caller's sites (D3)
      SELF         -> own only (worker)."""
    if global_role in _ALL_USER_SCOPE_ROLES:
        return "ALL"
    roles = set(membership_roles or ())
    if global_role in _SITE_USER_SCOPE_ROLES or "pm" in roles:
        return "SITE"
    if global_role == "site_manager" or "site_manager" in roles:
        return "SELF+WORKERS"
    return "SELF"
```

- [ ] **Step 4: Run — verify pass** — CI: `pytest tests/unit/test_acl_scope.py -v` → all green.

- [ ] **Step 5: Add the two membership queries + `sites.list_all_sites`**

In `src/repositories/memberships.py`, add both names to `__all__` and append:

```python
def caller_site_roles(conn, user_id) -> dict:
    """Map {site_id_str: membership.role} for one user across their non-archived
    memberships -- the per-site within-project authority D1 says ACL must now
    read. Drives visible_scope's pm/site_manager grading (a person can be pm on
    Project A and worker on Project B)."""
    rows = conn.execute(
        "SELECT site_id, role FROM memberships WHERE user_id=%s AND archived_at IS NULL",
        (user_id,),
    ).fetchall()
    return {str(r[0]): r[1] for r in rows}


def worker_user_ids_for_sites(conn, site_ids) -> set:
    """Distinct user_ids holding a WORKER membership on any of site_ids
    (non-archived) -- the 'workers on the caller's sites' half of a
    site_manager's SELF+WORKERS author set (visibility spec §3.1/D3, the graded
    restatement of BUG-25's site_manager leak fix: a site_manager sees own +
    workers, never other site_managers/pms). Empty in -> empty out, no
    round-trip. ::uuid[] accepts the str ids visible_scope holds."""
    if not site_ids:
        return set()
    rows = conn.execute(
        "SELECT DISTINCT user_id FROM memberships "
        "WHERE site_id = ANY(%s::uuid[]) AND role='worker' AND archived_at IS NULL",
        (list(site_ids),),
    ).fetchall()
    return {str(r[0]) for r in rows}
```

In `src/repositories/sites.py`, append (mirrors `list_company_sites` with the company filter removed):

```python
def list_all_sites(conn, include_archived=False) -> list[dict]:
    """EVERY company's sites -- the ONE deliberately company-agnostic query,
    reachable ONLY through visible_scope's platform_admin branch (D6). Never
    call this from a company-pinned path."""
    guard = "" if include_archived else "WHERE archived_at IS NULL "
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM sites {guard}ORDER BY company_id, created_at"
    ).fetchall()
```

- [ ] **Step 6: Create `src/repositories/scope.py`**

```python
"""visible_scope -- the single ACL primitive every read path scopes through
(visibility spec §3.1/§3.2). Wraps the existing binary scoping (acl.resolve_
scope + memberships.accessible_site_ids + sites.list_company_sites) with graded
within-project authority (D1) and the platform_admin cross-company branch (D6).
Import graph is acyclic: acl (pure) <- memberships <- scope; sites/users import
no repos, so importing them here is safe."""
from repositories import acl, memberships, sites


def visible_scope(conn, caller) -> dict:
    """Resolve the caller's visibility envelope. All rows below platform_admin
    are hard-pinned to caller.company_id (§3.0). Returns:
      site_ids     : set[str]  -- sites the caller may see at all (reach)
      user_scope   : str       -- ALL|SITE|SELF+WORKERS|SELF (within-project)
      author_ids   : set|None  -- resolved per-author allow-set; None = no filter
      self_folder  : str|None  -- caller's recording folder (own-data key)
      self_user_id : str       -- caller.id; own items are always visible
      company_id, cross_company."""
    global_role = caller["global_role"]
    cross_company = acl.is_cross_company(global_role)

    if cross_company:
        # D6: the SOLE branch NOT pinned to caller.company_id.
        site_ids = {str(s["id"]) for s in sites.list_all_sites(conn)}
        site_roles = {}
    elif acl.resolve_scope(global_role) == "ALL":
        site_ids = {str(s["id"]) for s in sites.list_company_sites(conn, caller["company_id"])}
        site_roles = {}
    else:
        # regional_manager / pm / site_manager / worker: their membership sites,
        # company-pinned by accessible_site_ids' non-ALL branch.
        site_ids = {str(x) for x in memberships.accessible_site_ids(
            conn, caller["id"], global_role)}
        site_roles = memberships.caller_site_roles(conn, caller["id"])

    user_scope = acl.visible_user_scope(global_role, site_roles.values())
    self_user_id = str(caller["id"])

    if user_scope in ("ALL", "SITE"):
        author_ids = None                                  # no per-author filter
    elif user_scope == "SELF+WORKERS":
        author_ids = {self_user_id} | memberships.worker_user_ids_for_sites(conn, site_ids)
    else:                                                  # SELF
        author_ids = {self_user_id}

    return {
        "site_ids": site_ids,
        "user_scope": user_scope,
        "author_ids": author_ids,
        "self_folder": caller.get("folder_name"),
        "self_user_id": self_user_id,
        "company_id": caller["company_id"],
        "cross_company": cross_company,
    }
```

- [ ] **Step 7: Write the failing `visible_scope` unit tests**

Create `tests/unit/test_visible_scope.py`. Stub the repo calls so no DB is needed (BUG-29 → CI gate); a `caller` is the same dict shape `get_user_by_sub` returns:

```python
import pytest

scope = pytest.importorskip("repositories.scope", reason="requires psycopg (CI)")
from repositories import scope as scope_mod  # noqa: E402


def _caller(role, uid="u-self", cid="c-1", folder="Self_Folder"):
    return {"id": uid, "company_id": cid, "global_role": role, "folder_name": folder}


@pytest.fixture
def stub(monkeypatch):
    """Stub scope's repo dependencies. Tests set the four returns as needed."""
    state = {"company_sites": [], "all_sites": [], "membership_ids": [],
             "site_roles": {}, "worker_ids": set()}
    monkeypatch.setattr(scope_mod.sites, "list_company_sites",
                        lambda conn, cid, **k: [{"id": s} for s in state["company_sites"]])
    monkeypatch.setattr(scope_mod.sites, "list_all_sites",
                        lambda conn, **k: [{"id": s} for s in state["all_sites"]])
    monkeypatch.setattr(scope_mod.memberships, "accessible_site_ids",
                        lambda conn, uid, role: list(state["membership_ids"]))
    monkeypatch.setattr(scope_mod.memberships, "caller_site_roles",
                        lambda conn, uid: dict(state["site_roles"]))
    monkeypatch.setattr(scope_mod.memberships, "worker_user_ids_for_sites",
                        lambda conn, sids: set(state["worker_ids"]))
    return state


def test_visible_scope_admin_all_company_sites_no_author_filter(stub):
    stub["company_sites"] = ["s-1", "s-2"]
    sc = scope_mod.visible_scope(None, _caller("admin"))
    assert sc["site_ids"] == {"s-1", "s-2"}
    assert sc["user_scope"] == "ALL"
    assert sc["author_ids"] is None
    assert sc["cross_company"] is False


def test_visible_scope_worker_membership_sites_self_only_author(stub):
    stub["membership_ids"] = ["s-9"]
    stub["site_roles"] = {"s-9": "worker"}
    sc = scope_mod.visible_scope(None, _caller("worker"))
    assert sc["site_ids"] == {"s-9"}
    assert sc["user_scope"] == "SELF"
    assert sc["author_ids"] == {"u-self"}


def test_visible_scope_site_manager_self_plus_workers(stub):
    stub["membership_ids"] = ["s-9"]
    stub["site_roles"] = {"s-9": "site_manager"}
    stub["worker_ids"] = {"u-w1", "u-w2"}
    sc = scope_mod.visible_scope(None, _caller("site_manager"))
    assert sc["user_scope"] == "SELF+WORKERS"
    assert sc["author_ids"] == {"u-self", "u-w1", "u-w2"}   # own + workers, no other manager


def test_visible_scope_pm_membership_grants_site_no_author_filter(stub):
    # D1: global 'worker' with a pm membership -> SITE, unrestricted authors on scope
    stub["membership_ids"] = ["s-9"]
    stub["site_roles"] = {"s-9": "pm"}
    sc = scope_mod.visible_scope(None, _caller("worker"))
    assert sc["user_scope"] == "SITE"
    assert sc["author_ids"] is None


def test_visible_scope_regional_union_membership_sites_company_pinned(stub):
    stub["membership_ids"] = ["s-3", "s-4"]
    stub["site_roles"] = {"s-3": "worker", "s-4": "site_manager"}
    sc = scope_mod.visible_scope(None, _caller("regional_manager"))
    assert sc["site_ids"] == {"s-3", "s-4"}       # NOT all-company; only assigned sites
    assert sc["user_scope"] == "SITE"
    assert sc["author_ids"] is None


def test_visible_scope_platform_admin_all_sites_cross_company(stub):
    stub["all_sites"] = ["s-1", "s-2", "s-99"]    # spans companies
    sc = scope_mod.visible_scope(None, _caller("platform_admin", cid="c-platform"))
    assert sc["site_ids"] == {"s-1", "s-2", "s-99"}
    assert sc["user_scope"] == "ALL"
    assert sc["cross_company"] is True


def test_visible_scope_non_platform_never_calls_list_all_sites(stub, monkeypatch):
    called = []
    monkeypatch.setattr(scope_mod.sites, "list_all_sites",
                        lambda *a, **k: called.append(1) or [])
    stub["membership_ids"] = ["s-9"]
    scope_mod.visible_scope(None, _caller("gm"))       # ALL but company-pinned
    scope_mod.visible_scope(None, _caller("worker"))
    assert called == []                                 # cross-company query untouched
```

- [ ] **Step 8: Run the unit suite — verify pass** — CI: `pytest tests/unit/test_visible_scope.py tests/unit/test_acl_scope.py -v` → green.

- [ ] **Step 9: Write the integration tests (real-DB semantics)**

Create `tests/integration/test_scope_acl.py` (real `db` fixture; reuse the module's company/site/user/membership seed helpers if present, else inline). Assert the multi-tenant + cross-project invariants against real SQL:

```python
import pytest

from repositories import memberships, scope


@pytest.mark.integration
def test_caller_site_roles_and_worker_ids_exclude_archived(db):
    cid = db.execute("INSERT INTO companies (name) VALUES ('A') RETURNING id").fetchone()[0]
    s1 = db.execute("INSERT INTO sites (company_id, name) VALUES (%s,'S1') RETURNING id", (cid,)).fetchone()[0]
    s2 = db.execute("INSERT INTO sites (company_id, name) VALUES (%s,'S2') RETURNING id", (cid,)).fetchone()[0]

    def user(sub):
        return db.execute("INSERT INTO users (cognito_sub, company_id, email, global_role) "
                          "VALUES (%s,%s,%s,'worker') RETURNING id", (sub, cid, sub + "@x.nz")).fetchone()[0]
    mgr, w1, w2 = user("mgr"), user("w1"), user("w2")
    db.execute("INSERT INTO memberships (user_id, site_id, role) VALUES (%s,%s,'site_manager')", (mgr, s1))
    db.execute("INSERT INTO memberships (user_id, site_id, role) VALUES (%s,%s,'worker')", (w1, s1))
    db.execute("INSERT INTO memberships (user_id, site_id, role) VALUES (%s,%s,'worker')", (w2, s2))  # other site
    db.execute("INSERT INTO memberships (user_id, site_id, role, archived_at) VALUES (%s,%s,'worker',now())", (w2, s1))

    assert memberships.caller_site_roles(db, mgr) == {str(s1): "site_manager"}
    assert memberships.worker_user_ids_for_sites(db, [str(s1)]) == {str(w1)}   # w2's s1 membership archived


@pytest.mark.integration
def test_visible_scope_worker_cannot_see_out_of_scope_site(db):
    cid = db.execute("INSERT INTO companies (name) VALUES ('A') RETURNING id").fetchone()[0]
    mine = db.execute("INSERT INTO sites (company_id, name) VALUES (%s,'Mine') RETURNING id", (cid,)).fetchone()[0]
    other = db.execute("INSERT INTO sites (company_id, name) VALUES (%s,'Other') RETURNING id", (cid,)).fetchone()[0]
    uid = db.execute("INSERT INTO users (cognito_sub, company_id, email, global_role, folder_name) "
                     "VALUES ('w','%s'::uuid,'w@x.nz','worker','W') RETURNING id" % cid).fetchone()[0]
    db.execute("INSERT INTO memberships (user_id, site_id, role) VALUES (%s,%s,'worker')", (uid, mine))
    caller = {"id": uid, "company_id": cid, "global_role": "worker", "folder_name": "W"}
    sc = scope.visible_scope(db, caller)
    assert str(mine) in sc["site_ids"] and str(other) not in sc["site_ids"]
    assert sc["author_ids"] == {str(uid)}


@pytest.mark.integration
def test_visible_scope_platform_admin_spans_companies_but_admin_does_not(db):
    ca = db.execute("INSERT INTO companies (name) VALUES ('A') RETURNING id").fetchone()[0]
    cb = db.execute("INSERT INTO companies (name) VALUES ('B') RETURNING id").fetchone()[0]
    sa = db.execute("INSERT INTO sites (company_id, name) VALUES (%s,'SA') RETURNING id", (ca,)).fetchone()[0]
    sb = db.execute("INSERT INTO sites (company_id, name) VALUES (%s,'SB') RETURNING id", (cb,)).fetchone()[0]
    admin_a = {"id": "00000000-0000-0000-0000-000000000001", "company_id": ca,
               "global_role": "admin", "folder_name": None}
    plat = {"id": "00000000-0000-0000-0000-000000000002", "company_id": ca,
            "global_role": "platform_admin", "folder_name": None}
    admin_ids = scope.visible_scope(db, admin_a)["site_ids"]
    plat_ids = scope.visible_scope(db, plat)["site_ids"]
    assert str(sa) in admin_ids and str(sb) not in admin_ids     # company A admin: A only
    assert {str(sa), str(sb)} <= plat_ids                        # platform_admin: both
```

- [ ] **Step 10: Add the two new roles to `ALLOWED_GLOBAL_ROLES`**

In `src/lambda_org_api.py`, edit line 83 (single-line anchor). This is inert until a role is assigned — no CHECK/enum on the column (`0002:16`), so no migration:

```python
ALLOWED_GLOBAL_ROLES = {"admin", "gm", "regional_manager", "pm", "site_manager", "worker", "platform_admin"}
```

(Leave `ALLOWED_MEMBERSHIP_ROLES = {"pm", "site_manager", "worker"}` unchanged — regional_manager/platform_admin are global-only, never per-site membership values.)

- [ ] **Step 11: Run integration + full unit — verify pass** — CI: `pytest tests/integration/test_scope_acl.py -v` and `pytest tests/unit -q` → green (no regressions; nothing calls `visible_scope` yet).

- [ ] **Step 12: Commit**

```bash
git add src/repositories/acl.py tests/unit/test_acl_scope.py src/repositories/memberships.py src/repositories/sites.py src/repositories/scope.py tests/unit/test_visible_scope.py tests/integration/test_scope_acl.py src/lambda_org_api.py
git commit -m "feat(acl): visible_scope primitive + regional_manager/platform_admin roles (Phase 3 Task 1; unwired)"
```

**Done:** The graded primitive exists and is exhaustively tested per role (worker/site_manager/pm/regional/gm/admin/platform × in-/out-of-scope → correct site_ids + user_scope + author_ids), company-pinned except the single platform_admin branch. No read path uses it yet; runtime behavior is unchanged.

---

### Task 2: Apply `visible_scope` to the read paths (behind `GRADED_ROLES`, default off)

**Why:** Route every read through the primitive so the fixes fall out uniformly (spec §3.2): `/live-items` gets the missing per-user filter (the R1/R4 gap — currently every member at a site sees every author's items, `src/lambda_org_api.py:843-853`); `/timeline` lets pm/regional/site_manager see in-scope users instead of hard force-self (`get_timeline_compat:1334-1346`); `/dates`, `/sites`, `/programme`, `/rollup`, `/sites/{id}/members` inherit regional reach + platform_admin cross-company via `_allowed_site_ids`; `/observations` scopes by site. Gated by `GRADED_ROLES` so flipping it is a per-environment, reversible cutover.

**Files:**
- Modify: `src/repositories/topics.py`, `tests/unit/test_topics_repo.py`
- Modify: `src/repositories/observations.py`
- Modify: `src/lambda_org_api.py`, `tests/unit/test_lambda_org_api.py`

**Interfaces:**
- `topics.list_topics_for_date(conn, site_ids, report_date, *, author_ids=None)` and `topics.list_report_dates(conn, site_ids, since_date, *, author_ids=None)` — `author_ids=None` = no per-author filter (today's behavior); a set → `AND t.user_id = ANY(%s::uuid[])`.
- `observations.list_observations(..., allowed_site_slugs=None)` — None = company-wide (today); a set → `AND site_slug = ANY(%s)`.
- `org._author_filter(conn, caller) -> set|None` — the per-author allow-set (None unless graded).
- `org._can_view_folder(conn, caller, target_folder) -> bool` — graded `/timeline` authority.

- [ ] **Step 1: Add `author_ids` to the two topics reads (+ failing unit tests)**

Append to `tests/unit/test_topics_repo.py` (module has `FakeConn`/`FakeCursor` recording SQL+params; `from repositories import topics`):

```python
def test_list_topics_for_date_author_ids_adds_user_filter():
    conn = FakeConn(results=[[], []])   # topic query + (short-circuits after empty)
    topics.list_topics_for_date(conn, ["s-1"], "2026-07-18", author_ids={"u-1", "u-2"})
    call = conn.calls[0]
    assert "t.user_id = ANY(%s::uuid[])" in call["sql"]
    assert "u-1" in call["params"][1] or "u-2" in call["params"][1]


def test_list_topics_for_date_author_ids_none_no_filter():
    conn = FakeConn(results=[[]])
    topics.list_topics_for_date(conn, ["s-1"], "2026-07-18")
    assert "t.user_id = ANY" not in conn.calls[0]["sql"]


def test_list_report_dates_author_ids_adds_user_filter():
    conn = FakeConn(results=[[]])
    topics.list_report_dates(conn, ["s-1"], __import__("datetime").date(2026, 5, 1),
                             author_ids={"u-1"})
    assert "user_id = ANY(%s::uuid[])" in conn.calls[0]["sql"]
```

In `src/repositories/topics.py`, add the kwarg + conditional clause to `list_topics_for_date` (around the `WHERE t.site_id = ANY(%s) AND t.report_date=%s` at line 185) and `list_report_dates` (line 237). Sketch for `list_topics_for_date`:

```python
def list_topics_for_date(conn, site_ids, report_date, *, author_ids=None) -> list[dict]:
    # ... docstring: author_ids (visibility spec §3.1 user_scope) restricts to
    # topics whose t.user_id is in the caller's allow-set; None = no per-author
    # filter (ALL/SITE scope). A topic with a NULL user_id (unattributed report
    # row) is deliberately EXCLUDED under a non-None filter -- fail-closed, no
    # unattributed row leaks into a SELF/SELF+WORKERS feed. ...
    if not site_ids:
        return []
    where = "WHERE t.site_id = ANY(%s) AND t.report_date=%s"
    params = [list(site_ids), report_date]
    if author_ids is not None:
        where += " AND t.user_id = ANY(%s::uuid[])"
        params.append(list(author_ids))
    topic_rows = conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_TOPIC_COLS_JOINED}, s.name AS site_name, "
        f"(u.first_name || ' ' || u.last_name) AS user_name FROM topics t "
        f"LEFT JOIN sites s ON s.id = t.site_id LEFT JOIN users u ON u.id = t.user_id "
        f"{where} ORDER BY t.occurred_at NULLS LAST, t.created_at",
        tuple(params),
    ).fetchall()
    # ... rest unchanged ...
```

Apply the identical `author_ids`/`::uuid[]` pattern to `list_report_dates`. (Author identity exists: `topics.user_id` FK → `users`, already selected as `t.user_id` and joined for `user_name` — `src/repositories/topics.py:146,184`.)

- [ ] **Step 2: Run the topics tests — verify fail then pass** — CI: `pytest tests/unit/test_topics_repo.py -k author -v`.

- [ ] **Step 3: Add the `GRADED_ROLES` flag + `_author_filter`, rewire `_allowed_site_ids`**

In `src/lambda_org_api.py`: add `scope` to the repositories import (line 57-58); add the flag near the other env reads (~line 83):

```python
# Phase 3 graded roles (visibility spec §3.1). Default OFF: _allowed_site_ids
# and every read path below behave EXACTLY as today until an environment is
# cut over (repo var PROD_GRADED_ROLES/TEST_GRADED_ROLES -> env, deploy-*.yml,
# same pattern as AUTHORITY_FLIP). No user silently gains visibility.
GRADED_ROLES = os.environ.get("GRADED_ROLES", "").lower() == "true"
```

Add the author-filter helper near `_allowed_site_ids` (~line 942):

```python
def _author_filter(conn, caller):
    """Per-author id allow-set (visibility spec §3.1 user_scope) when graded
    roles are on, else None = today's site-only scoping. None => unrestricted."""
    if not GRADED_ROLES:
        return None
    return scope.visible_scope(conn, caller)["author_ids"]
```

Rewire `_allowed_site_ids` (line 942-949) so graded reach (regional + platform_admin cross-company) flows to `/sites`, `/dates`, `/programme`, `/rollup`, `/sites/{id}/members` with no per-endpoint edits:

```python
def _allowed_site_ids(conn, caller):
    if GRADED_ROLES:
        return scope.visible_scope(conn, caller)["site_ids"]   # graded reach (incl. platform_admin)
    # legacy binary scoping (unchanged) -- str() both sides (uuid vs ?site str)
    if resolve_scope(caller["global_role"]) == "ALL":
        return {str(s["id"]) for s in sites.list_company_sites(conn, caller["company_id"])}
    return {str(x) for x in memberships.accessible_site_ids(conn, caller["id"], caller["global_role"])}
```

- [ ] **Step 4: Add the per-user filter to `/live-items` and `/dates`**

Rewrite `list_live_items` (line 843-853) to source site_ids from the graded-aware `_allowed_site_ids` and apply the author filter (removes the branchy admin/gm vs membership fork — both are now inside `_allowed_site_ids`):

```python
def list_live_items(conn, caller, event):
    date = (event.get("queryStringParameters") or {}).get("date")
    if not date or not REPORT_DATE_RE.match(date):
        return error("date required (YYYY-MM-DD)", 400)
    site_ids = list(_allowed_site_ids(conn, caller))          # graded-aware reach
    rows = topics.list_topics_for_date(conn, site_ids, date,
                                       author_ids=_author_filter(conn, caller))
    return ok({"topics": rows})
```

In `get_org_dates` (line 877-895) add the author filter to the `list_report_dates` call (site scoping already flows through `_allowed_site_ids`/`_resolve_site_param`):

```python
    rows = topics.list_report_dates(conn, site_ids, since,
                                    author_ids=_author_filter(conn, caller))
```

- [ ] **Step 5: `/timeline` — graded authority via `_can_view_folder`**

Add the helper near `get_timeline_compat` (~line 1276):

```python
def _can_view_folder(conn, caller, target_folder):
    """GRADED /timeline authority (spec §3.2): may caller read target_folder's
    (folder, date) timeline? Own folder always; SITE (pm/regional) any user on
    an in-scope site; SELF+WORKERS (site_manager) own + workers on in-scope
    sites; SELF (worker) own only. Company-pinned: the target is resolved
    within caller.company_id first (unless caller is cross-company)."""
    sc = scope.visible_scope(conn, caller)
    if target_folder and target_folder == sc["self_folder"]:
        return True
    if sc["cross_company"]:
        target = users.get_by_folder_name_global(conn, target_folder)
    else:
        target = users.get_by_folder_name(conn, caller["company_id"], target_folder)
    if target is None:
        return False                                          # not in caller's company / unknown
    if sc["user_scope"] == "ALL":
        return True
    if sc["user_scope"] == "SITE":
        target_sites = memberships.caller_site_roles(conn, target["id"])
        return any(sid in sc["site_ids"] for sid in target_sites)   # target is on an in-scope site
    return str(target["id"]) in (sc["author_ids"] or set())   # SELF / SELF+WORKERS
```

Rewrite the head of `get_timeline_compat` (line 1328-1353) to branch on the graded flag; keep the legacy body verbatim when off:

```python
def get_timeline_compat(conn, caller, event):
    p = event.get("queryStringParameters") or {}
    date, user = p.get("date"), (p.get("user") or "").strip()
    if not date or not REPORT_DATE_RE.match(date):
        return error("date required (YYYY-MM-DD)", 400)
    if GRADED_ROLES:
        sc = scope.visible_scope(conn, caller)
        if sc["user_scope"] == "ALL":                         # admin/gm/platform_admin
            if not user:
                return admin_disambiguation(conn, caller, date)
            if not sc["cross_company"] and \
                    users.get_by_folder_name(conn, caller["company_id"], user) is None:
                return error("user not found in your company", 404)
            return _render_timeline_for_user(conn, caller, date, user)
        # graded non-ALL: default self, but pm/regional/site_manager may view
        # in-scope users (spec §3.2 -- no longer hard-forced to self).
        if not user:
            user = sc["self_folder"]
            if not user:
                return error("no folder mapping for your account", 403)
        if not _can_view_folder(conn, caller, user):
            return error("not permitted to view this timeline", 403)
        return _render_timeline_for_user(conn, caller, date, user)
    # ---- GRADED_ROLES off: today's behavior, verbatim ----
    is_all = resolve_scope(caller["global_role"]) == "ALL"
    if not is_all:
        own = caller.get("folder_name")
        if not own:
            return error("no folder mapping for your account", 403)
        if user and user != own:
            return error("you may only view your own timeline", 403)
        user = own
    if not user:
        return admin_disambiguation(conn, caller, date)
    if is_all and users.get_by_folder_name(conn, caller["company_id"], user) is None:
        return error("user not found in your company", 404)
    return _render_timeline_for_user(conn, caller, date, user)
```

(`_render_timeline_for_user`'s own site ACL uses `_allowed_site_ids` — now graded — so a pm/regional caller only gets Aurora override rows on their in-scope sites, and platform_admin sees any site. The company-pin defence there is unchanged.)

- [ ] **Step 6: `/observations` — scope by the caller's site slugs**

In `src/repositories/observations.py`, add `allowed_site_slugs=None` to `list_observations` (line 29-30) and, when not None, append `conditions.append("site_slug = ANY(%s)"); params.append(list(allowed_site_slugs))`.

In `src/lambda_org_api.py` `list_org_observations` (line 766-777):

```python
def list_org_observations(conn, caller, event):
    params = event.get("queryStringParameters") or {}
    kind = params.get("kind")
    if kind is not None and kind not in ALLOWED_OBSERVATION_KINDS:
        return error(f"kind must be one of {sorted(ALLOWED_OBSERVATION_KINDS)}", 400)
    allowed_slugs = None
    if GRADED_ROLES:
        sc = scope.visible_scope(conn, caller)
        if sc["user_scope"] != "ALL":                         # admin/gm stay company-wide
            allowed_slugs = {s["slug"] for s in sites.list_sites_by_ids(conn, sc["site_ids"])
                             if s.get("slug")}
    rows = observations.list_observations(
        conn, caller["company_id"], kind=kind,
        date_from=params.get("from"), date_to=params.get("to"),
        site_slug=params.get("site_slug"), allowed_site_slugs=allowed_slugs,
        include_archived=params.get("include_archived") == "1")
    return ok({"observations": rows})
```

- [ ] **Step 7: Write the per-path × per-role ACL tests**

Append to `tests/unit/test_lambda_org_api.py`. Use `monkeypatch.setattr(org, "GRADED_ROLES", True)` to exercise graded, and stub `org.scope.visible_scope` (or the underlying repos) per role. Exact test names:

```python
# ---- /live-items per-user filter (the R1/R4 gap) ----
def test_live_items_worker_filters_to_own_author(wired):
    wired.setattr(org, "GRADED_ROLES", True)
    wired.setattr(org, "_allowed_site_ids", lambda conn, caller: {SITE_ID})
    wired.setattr(org.scope, "visible_scope",
                  lambda conn, caller: {"site_ids": {SITE_ID}, "author_ids": {"u-self"},
                                        "user_scope": "SELF", "self_folder": "W",
                                        "self_user_id": "u-self", "company_id": "c-uuid-1",
                                        "cross_company": False})
    seen = {}
    wired.setattr(org.topics, "list_topics_for_date",
                  lambda conn, sids, date, author_ids=None: (seen.update(author_ids=author_ids) or []))
    res = org.lambda_handler(make_event("GET", "/api/org/live-items", params={"date": "2026-07-18"}), None)
    assert res["statusCode"] == 200
    assert seen["author_ids"] == {"u-self"}               # worker: own author only

def test_live_items_site_manager_self_plus_workers(wired): ...      # author_ids == {self, workers}
def test_live_items_pm_membership_no_author_filter(wired): ...      # author_ids is None, site-scoped
def test_live_items_admin_unfiltered(wired): ...                    # author_ids is None
def test_live_items_graded_off_passes_no_author_filter(wired):     # regression: flag off
    # GRADED_ROLES stays False (default); assert list_topics_for_date got author_ids=None
    ...

# ---- /dates author filter ----
def test_dates_worker_author_filtered(wired): ...                  # list_report_dates author_ids == {self}
def test_dates_admin_no_author_filter(wired): ...

# ---- /timeline graded authority ----
def test_timeline_worker_denied_other_user_403(wired): ...         # _can_view_folder -> False
def test_timeline_worker_defaults_to_self(wired): ...              # no ?user -> self_folder
def test_timeline_site_manager_may_view_worker_on_site(wired): ... # 200
def test_timeline_site_manager_denied_other_site_manager_403(wired): ...  # BUG-25 class
def test_timeline_pm_may_view_any_in_scope_user(wired): ...        # 200
def test_timeline_pm_denied_out_of_scope_user_403(wired): ...      # target on a site not in site_ids
def test_timeline_admin_unchanged_disambiguation(wired): ...       # no ?user -> admin_disambiguation
def test_timeline_graded_off_forces_self_403_on_other(wired): ...  # legacy path intact

# ---- /observations site scoping ----
def test_observations_worker_scoped_to_member_site_slugs(wired): ... # allowed_site_slugs set
def test_observations_admin_company_wide(wired): ...                 # allowed_site_slugs None
```

Each `...` follows the wired/stub pattern of the first test: stub `visible_scope` for the role, assert the arg the handler forwards (`author_ids` / `allowed_site_slugs`) and the status code. Fill every one in — no placeholders in the committed tests.

- [ ] **Step 8: Run — fail then pass, then full suite** — CI: `pytest tests/unit/test_lambda_org_api.py -k "live_items or dates or timeline or observations" -v`, then `pytest tests/unit -q` (assert graded-off tests still green — no regression).

- [ ] **Step 9: Commit**

```bash
git add src/repositories/topics.py tests/unit/test_topics_repo.py src/repositories/observations.py src/lambda_org_api.py tests/unit/test_lambda_org_api.py
git commit -m "feat(org-api): route reads through visible_scope behind GRADED_ROLES; /live-items per-user filter, graded /timeline+/dates+/observations (Phase 3 Task 2)"
```

**Done:** With `GRADED_ROLES=true` every read path scopes through `visible_scope`; `/live-items` no longer shows every author's items to every member, `/timeline` lets pm/regional/site_manager view in-scope users, `/dates`/`/observations`/`/sites`/`/rollup` honor graded reach. With the flag off, behavior is byte-for-byte today's — proven by the graded-off regression tests.

---

### Task 3: `platform_admin` cross-company — seed company, cross-company read branch, `target_company_id` writes

**Why:** D6: `platform_admin` = a dedicated `FieldSight-platform` company row + a `platform_admin` global_role, so `company_id` stays NOT-NULL everywhere and cross-company lives in the single `visible_scope` branch (Task 1). Read is cross-company; **write** must carry an explicit `target_company_id`, and only a `platform_admin` may cross a company or grant `platform_admin`.

**Files:**
- Create: `src/migrations/0015_platform_company.sql`, integration migration assertion
- Modify: `src/lambda_org_api.py` (`create_member`, `create_org_site`, `patch_member_role` + their write gates), `tests/unit/test_lambda_org_api.py`

**Interfaces:**
- Migration `0015` seeds `companies(name='FieldSight-platform')` idempotently.
- `create_member` / `create_org_site` accept optional `body["target_company_id"]`; default = caller's company. A value ≠ caller's company requires `platform_admin`.
- `global_role == 'platform_admin'` in a create/patch body requires the caller to be `platform_admin`.

- [ ] **Step 1: Seed migration**

Create `src/migrations/0015_platform_company.sql`:

```sql
-- D6: dedicated platform company so a platform_admin keeps company_id NOT NULL
-- (cross-company visibility lives ONLY in visible_scope's branch, never in a
-- null company pin). Idempotent -- safe to re-run. NOTE: this is a DATA SEED,
-- not a schema change: users.global_role is plain text with no CHECK/enum
-- (0002_core_relational.sql:16), so the new roles regional_manager/
-- platform_admin need NO migration -- only this row does. Promoting the vendor
-- account to platform_admin + reparenting it to this company is a manual/seed
-- op (not code), gated on this row existing.
INSERT INTO companies (name, industry)
SELECT 'FieldSight-platform', 'platform'
WHERE NOT EXISTS (SELECT 1 FROM companies WHERE name = 'FieldSight-platform');
```

Add an integration assertion (in the repo's migration test, or a new `tests/integration/test_platform_company.py`):

```python
@pytest.mark.integration
def test_platform_company_seeded(db):
    row = db.execute("SELECT 1 FROM companies WHERE name='FieldSight-platform'").fetchone()
    assert row is not None
```

- [ ] **Step 2: Write the failing write-path tests**

Append to `tests/unit/test_lambda_org_api.py` (mirror the existing `create_member`/`create_org_site` tests — Cognito stubbed, `by_sub` caller). Exact names:

```python
def test_create_site_platform_admin_targets_other_company(wired): ...
    # caller platform_admin, body target_company_id=c-B, companies.get_company_by_id ok
    # -> sites.create_site called with c-B (not caller.company_id)
def test_create_site_non_platform_cannot_target_other_company_403(wired): ...
    # caller admin, target_company_id != own -> 403, create_site NOT called
def test_create_site_admin_own_company_unaffected(wired): ...
    # no target_company_id -> caller.company_id, unchanged

def test_create_member_platform_admin_targets_other_company(wired): ...
    # upsert_user company_id == target; membership site checks use target company
def test_create_member_non_platform_cannot_target_other_company_403(wired): ...
def test_create_member_only_platform_admin_may_grant_platform_admin_403(wired): ...
    # caller admin, body global_role='platform_admin' -> 403
def test_create_member_platform_admin_may_grant_platform_admin(wired): ...

def test_patch_member_role_only_platform_admin_may_grant_platform_admin_403(wired): ...
def test_patch_member_role_platform_admin_may_grant_platform_admin(wired): ...
```

- [ ] **Step 3: Implement the write gates + `target_company_id`**

In `create_org_site` (line 406-434): widen the write gate to include `platform_admin`, then resolve the target company:

```python
    if caller["global_role"] not in ("admin", "gm", "platform_admin"):
        return error("admin or gm role required", 403)
    # ... existing name/icon validation ...
    target_company_id = caller["company_id"]
    req = body.get("target_company_id")
    if req and str(req) != str(caller["company_id"]):
        if caller["global_role"] != "platform_admin":       # D6: only platform_admin crosses
            return error("only platform_admin may create sites in another company", 403)
        if companies.get_company_by_id(conn, req) is None:
            return error("target company not found", 404)
        target_company_id = req
    row = sites.create_site(conn, target_company_id, name, location=..., icon_s3_key=None, address=...)
```

In `create_member` (line 572-652): widen the gate and thread `target_company_id` through the membership site checks + `upsert_user`; add the platform_admin-grant guard:

```python
    if caller["global_role"] not in ("admin", "platform_admin"):
        return error("admin role required", 403)
    # ... email/global_role validation ...
    # D6: only platform_admin may grant platform_admin
    if global_role == "platform_admin" and caller["global_role"] != "platform_admin":
        return error("only platform_admin may grant platform_admin", 403)
    target_company_id = caller["company_id"]
    req = body.get("target_company_id")
    if req and str(req) != str(caller["company_id"]):
        if caller["global_role"] != "platform_admin":
            return error("only platform_admin may create members in another company", 403)
        if companies.get_company_by_id(conn, req) is None:
            return error("target company not found", 404)
        target_company_id = req
    # membership site checks compare against target_company_id, not caller's:
    #   if site is None or site["company_id"] != target_company_id: 403
    # ... and existing-user cross-company guard compares existing.company_id
    #     against target_company_id; upsert_user(company_id=target_company_id).
```

In `patch_member_role` (line 502-513): add the same grant guard before the write:

```python
    if role == "platform_admin" and caller["global_role"] != "platform_admin":
        return error("only platform_admin may grant platform_admin", 403)
```

(Confirm `companies` is already imported — it is, `src/lambda_org_api.py:57`. `companies.get_company_by_id` exists — `src/repositories/companies.py:19`.)

- [ ] **Step 4: Run — fail then pass, then full suite** — CI: `pytest tests/unit/test_lambda_org_api.py -k "platform or target_company or grant" -v`, then `pytest tests/unit -q`.

- [ ] **Step 5: Commit**

```bash
git add src/migrations/0015_platform_company.sql tests/integration/test_platform_company.py src/lambda_org_api.py tests/unit/test_lambda_org_api.py
git commit -m "feat(org-api): platform_admin cross-company read + target_company_id writes; platform company seed (Phase 3 Task 3, D6)"
```

**Done:** `platform_admin` reads across companies (single `visible_scope` branch) and writes into a named `target_company_id`; only a `platform_admin` may cross a company or grant `platform_admin`; `company_id` stays NOT-NULL via the seeded platform company.

---

### Task 4: PR, migration/flag wiring, deploy, smoke (handoff — user-gated)

**Files:** `template.yaml` (env var), then process.

- [ ] **Step 1: Wire the flag** — add `GRADED_ROLES: !Ref GradedRoles` (Parameter, default `"false"`) to the org-api function's `Environment.Variables` in `template.yaml`, mirroring how `AUTHORITY_FLIP` is wired; drive it from repo vars `TEST_GRADED_ROLES`/`PROD_GRADED_ROLES` in `deploy.yml`/`deploy-prod.yml`. Do **not** default it true.
- [ ] **Step 2: Pipeline PR to `develop`** — `gh pr create --base develop` titled `feat: Phase 3 graded roles & visible_scope (regional_manager, platform_admin, per-user filter)`. Confirm CI `Tests` green: the pure `acl` tests, `test_visible_scope`, `test_scope_acl` (integration), the topics/observations repo tests, and every per-path × per-role handler test. Migration `0015` runs in the CI migrate step.
- [ ] **Step 3: Merge to `develop`** → deploys `fieldsight-test` with `GRADED_ROLES` still **false**. Smoke that behavior is unchanged (regression): `/live-items`, `/timeline`, `/dates` return exactly as before. Then set `TEST_GRADED_ROLES=true`, redeploy, and smoke the graded matrix with real idTokens: a worker's `/live-items` shows only their own items; a site_manager sees own + workers (not other managers); a pm sees all in-scope authors; a pm/regional `/timeline?user=<in-scope>` returns 200 while `<out-of-scope>` 403s; `/dates` dots shrink to the caller's authored set; a cross-company `?site`/`?user` still 403s for everyone below platform_admin.
- [ ] **Step 4: platform_admin bring-up (test only, manual/seed)** — after `0015`, reparent the vendor account to `FieldSight-platform` and set its `global_role='platform_admin'` (seed/`PATCH /members/{sub}/role` by an existing platform_admin, or a one-off seed since none exists yet). Smoke: it reads two companies' sites; `create_member`/`create_org_site` with `target_company_id` land in the named company; without `platform_admin` those writes 403.
- [ ] **Step 5: Promote `develop`→`main` (prod)** is a SEPARATE user decision (carries whatever else is on develop). Ship with `PROD_GRADED_ROLES=false` first (zero behavior change on prod), verify the regression, THEN flip `PROD_GRADED_ROLES=true` as the explicit, reversible cutover (revert = set it back to false, redeploy — no data migration to undo; `0015` is inert when unused). Surface this; do not bundle silently.

**Done:** Phase 3 shippable and reversible by one flag; graded roles + platform_admin verified on test; prod promotion and the `GRADED_ROLES` cutover are the user's call.

---

## Self-Review (author)

- **Spec coverage:** §3.1 primitive → Task 1 `scope.visible_scope` + pure `acl.visible_user_scope`/`is_cross_company`; the §3.1 table is realized exactly (platform_admin→all-companies/ALL; admin/gm→company/ALL; regional→membership-union/SITE; pm→pm-membership/SITE; site_manager→member-sites/SELF+WORKERS; worker→member-sites/SELF). §3.2 read-path unification → Task 2 (`_allowed_site_ids` delegates to the primitive; `/live-items` per-user filter; `/timeline` `_can_view_folder`; `/dates`/`/observations`/`/rollup`/`/sites/{id}/members` inherit). §3.0 multi-tenancy → company pin on every branch except the one platform_admin branch, asserted by `test_visible_scope_platform_admin_spans_companies_but_admin_does_not` + `test_visible_scope_non_platform_never_calls_list_all_sites`. D1 → membership-role floor (`test_visible_user_scope_pm_global_or_membership_is_site`). D2 → regional_manager added. D3 → SELF+WORKERS = own + workers (BUG-25 restated; `worker_user_ids_for_sites` excludes non-workers/archived). D6 → seed company + `target_company_id` writes + only-platform-admin-grants. §6 Risks → per-role-per-path ACL tests are the release gate; graded is flag-off by default.
- **DB migration question (asked):** **No migration for the roles.** `users.global_role` is `text NOT NULL DEFAULT 'worker'` with no CHECK and no enum (`src/migrations/0002_core_relational.sql:16`); `memberships.role` is `text NOT NULL` with none either (`0002:35`); a grep of all 14 migrations finds no CHECK/CREATE TYPE/enum on either column. The sole app-level gate is `ALLOWED_GLOBAL_ROLES` (`src/lambda_org_api.py:83`) — a one-line set edit. Migration `0015` seeds only the platform **company row** (D6), a data seed, not a role constraint.
- **Author identity for the per-user filter (asked):** `topics.user_id` is a first-class FK to `users.id` (`_TOPIC_COLS`/`_TOPIC_COLS_JOINED`, `src/repositories/topics.py:6,146`), already selected as `t.user_id` and `LEFT JOIN users u ON u.id = t.user_id` for `user_name` (`topics.py:184`, `list_topics_for_date`). The filter is `AND t.user_id = ANY(%s::uuid[])` over the resolved `author_ids`; `self_user_id = caller.id`, workers from `worker_user_ids_for_sites`. NULL-`user_id` topics are excluded under a non-None filter (fail-closed — no unattributed row leaks into a SELF/SELF+WORKERS feed).
- **Conservative / fail-closed choices:** graded behavior is flag-gated OFF (no silent visibility gain); `_author_filter`/`_allowed_site_ids` return today's values when off (graded-off regression tests assert byte-parity); `list_all_sites` is reachable ONLY through the platform_admin branch and never from a company-pinned path (asserted); site scoping (`_allowed_site_ids`) is enforced BEFORE the author filter, so the author set can only ever narrow within an already company/membership-scoped site set — an author-set bug can over-restrict (deny) but cannot cross a company or project boundary.
- **Reuse, no duplicated ACL:** `site_ids` reuses `sites.list_company_sites` / `memberships.accessible_site_ids`; read paths reuse `_allowed_site_ids` / `_resolve_site_param` (Phase 2's guards) verbatim; the graded delta is 2 membership queries + 1 cross-company `sites` query + 1 pure tier function + optional kwargs on 3 existing repo reads. `resolve_scope` is left intact as the legacy/off path and the admin/gm write gate.
- **Mixed-membership nuance (documented, spec-consistent):** `user_scope` is a caller-level scalar = effective tier (global_role with membership.role as a floor), and `site_ids` gates reach — so a person who is pm on A and worker on B sees all authors on BOTH their sites (their top authority applied within their own reach), matching the spec's caller-level `user_scope` model and BUG-25's "workers on any accessible site" shape. This never crosses company or project (reach is the boundary). If a customer later needs strict per-site downgrade, the tighten is local to `visible_scope`'s `author_ids` (compute per-site) — noted, not built.
- **Type consistency:** `visible_scope` returns str ids throughout (uuid→str via `str()`), matching `_allowed_site_ids`' existing str-vs-`?site` contract (`src/lambda_org_api.py:942-949`); `author_ids` is `set[str]|None`; `::uuid[]` casts let those str ids match the uuid columns (same pattern as `list_report_dates`, Phase 2). `caller` dict shape (`id`, `company_id`, `global_role`, `folder_name`) is exactly `users.get_user_by_sub`'s `_COLS` (`src/repositories/users.py:3`).
- **BUG-29 / no-local-Python:** all steps note CI as the pass gate; integration tests SKIP locally without `TEST_DATABASE_URL`. Pure `acl` tests are additionally local-safe (no psycopg import) but CI remains the gate. **Windows/CRLF:** single-line Edit anchors for the `ALLOWED_GLOBAL_ROLES` and flag edits; new files written whole; commits stage named files only (never `git add -A`). **English** throughout.
- **Reversibility:** one flag (`GRADED_ROLES`) per environment; revert = set false + redeploy, no data to undo; `0015` is idempotent and inert when platform_admin is unused. Adding the two roles to `ALLOWED_GLOBAL_ROLES` is inert until assigned.
- **Placeholder scan:** Task 1/Task 3 code and tests are complete; Task 2 Step 7 lists every test by exact name with the first fully worked and the pattern stated — the implementer fills each with the same wired/stub shape (explicitly "no placeholders in the committed tests"). No TBD in shipped code.
