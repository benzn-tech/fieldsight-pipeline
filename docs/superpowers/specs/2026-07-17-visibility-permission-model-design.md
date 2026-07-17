# Visibility & Permission Model — Unified Design (2026-07-17)

**Status:** Design / for review. Pairs with
`2026-07-17-content-filter-privacy-system-design.md` (the two interlock at the
site_manager's authority and the layered review gate).

**Scope:** fieldsight-pipeline (backend ACL) + fieldsight-ui (read paths, site
selector). This is the durable fix for the ACL leaks and role gaps surfaced
during 2026-07-17 customer testing — replacing one-off patches with one
coherent model.

---

## 1. Problem

### 1.1 Two backends, inconsistent ACL (root cause)
The app straddles **two** backends that answer the same questions differently:
- **Legacy** `lambda_fieldsight_api.py` — DynamoDB `fieldsight-users` + S3
  `config/user_mapping.json` for identity/ACL. Serves `/api/timeline` (fallback),
  `/api/dates`, `/api/site-users`, `/api/users`.
- **Current** `lambda_org_api.py` + Aurora (`users`/`memberships`) — the
  dashboard-first source of truth. Serves `/api/org/timeline`, `/live-items`,
  `/sites`, `/members`, etc.

Views that should agree read from different backends, so ACL is enforced in one
and not the other. Concrete symptoms observed:
- **Today access-denied** — a real site_manager (`Ben_UCPK`) hard-banned from
  their **own** Today because the login isn't linked to its report folder
  (`folder_name` unenrolled); the aurora shim 403s (`user != own`), the UI falls
  back to the legacy path, which also 403s (`can_access_user_data`).
- **Timeline dates leak** — the date-strip dots come from legacy `/dates`, which
  returns a **site's** report-dates without checking the caller is a member of
  that `?site=` (`get_dates` → `get_accessible_users(caller, site_filter=site)`
  is not membership-gated). A new site_manager sees *"activity exists"* metadata
  for projects they don't belong to. (Content is safe — clicking shows "No
  report" — so it is a **metadata** leak, not a content breach.)
- **Timeline default site out of scope** — UI defaults `site` to a global
  default (`sb1108-ellesmere`), not the caller's accessible site.
- **Sites → USERS ON SITE empty / "Access denied to this site"** — that panel
  reads legacy `/site-users`, which doesn't know Aurora-only sites (e.g. UC PK),
  while Team reads Aurora `/members`. The membership is correct; only that panel
  reads the wrong source.

### 1.2 Role model is 2-tier and largely inert
- `resolve_scope` (`repositories/acl.py:1-7`) is **binary**: `{admin, gm}` → `ALL`
  (whole company); everyone else → `MEMBERSHIPS` (their membership sites).
  `pm`, `site_manager`, `worker` are **scope-identical**.
- `memberships.role` (`pm`/`site_manager`/`worker` per site) is **written and
  displayed but never read** by any ACL decision (`memberships.py:26-28`).
  "Give Neil PM" currently changes nothing.
- **No cross-project tier** between site-scoped and company-wide. `regional_manager`
  does not exist in `ALLOWED_GLOBAL_ROLES` (`lambda_org_api.py:77`).
- `/live-items` (`lambda_org_api.py:747-757`) filters by **site only, no
  per-user** rule — every member at a site sees every author's items (a BUG-25-class
  regression that was fixed in the legacy path but not ported to Aurora).
- `/observations` (`repositories/observations.py`) is filtered by `company_id`
  only — cross-project.

### 1.3 Identity bridge missing
Attribution and "own data" both hinge on `users.folder_name` matching the S3
report/recording folder. Until 2026-07-17 **no product route could set
`folder_name`** — only the manual-invoke seed. The
`PATCH /api/org/members/{sub}/folder` endpoint (shipped this session) closes that,
but enrollment must become a first-class, always-applied step.

---

## 2. Goals / non-goals

**Goals**
1. **Single source of truth = Aurora org-api** for all read/ACL paths
   (Today/Timeline/dates/site-users/live-items/observations).
2. **Enrolled identity**: every login linked to its report folder (`folder_name`).
3. **Graded roles that actually gate**: worker < site_manager < pm <
   regional_manager < gm/admin, with `membership.role` honored per site.
4. **One ACL primitive** applied uniformly to every read path — no per-endpoint
   bespoke rule that can drift or leak.
5. **Layered visibility**: site-level content is immediate (timeliness);
   company/regional aggregation only sees **reviewed/published** data (privacy —
   see the companion spec).
6. **Multi-tenant invariant preserved**: a caller never sees another company's
   data, at any tier.

**Non-goals**
- Rewriting report *generation* (the legacy pipeline still generates reports;
  only the **read/ACL** surface moves to Aurora).
- The content-filter/redaction mechanics (companion spec).

---

## 3. Design

### 3.1 One ACL primitive: `visible_scope(conn, caller)`
A single function returns the caller's visibility envelope, used by **every**
read path:

```
visible_scope(conn, caller) -> {
  site_ids:      set[site_id],     # sites the caller may see at all
  user_scope:    'ALL' | 'SITE' | 'SELF',
  self_folder:   folder_name | None,
}
```

Resolution (replaces the binary `resolve_scope`):

| global_role       | site_ids                                  | user_scope | meaning |
|-------------------|-------------------------------------------|------------|---------|
| `admin` / `gm`    | every non-archived company site           | `ALL`      | whole company |
| `regional_manager`| union of assigned sites (memberships)     | `SITE`     | cross-project within their region |
| `pm`              | sites where they hold a `pm` membership   | `SITE`     | all members at those projects |
| `site_manager`    | sites where they are a member             | `SELF+WORKERS` | own + workers at their sites |
| `worker`          | sites where they are a member             | `SELF`     | own only |

- `user_scope` decides the **per-user** filter that read paths apply on top of
  `site_ids`:
  - `ALL` → no per-user filter.
  - `SITE` → any author whose folder is attributed to an in-scope site.
  - `SELF+WORKERS` → own folder + folders of `worker`-role members on the caller's sites.
  - `SELF` → own folder only.
- **`membership.role` is now read.** `pm` scope comes from holding a `pm`
  *membership* (per-site), independent of `global_role`; this lets one person be
  a pm on Project A and a worker on Project B. `global_role` sets the ceiling;
  `membership.role` sets per-site authority. (Open decision D1: whether pm is
  driven by `global_role`, `membership.role`, or the max of the two.)

### 3.2 Read-path unification
Every read endpoint calls `visible_scope` and applies `(site_ids, user filter)`
identically. Specific fixes fall out automatically:

- **`/timeline`** — non-`ALL` no longer hard-forced to self; a pm/regional/site_manager
  sees the timelines of users in `user_scope`. (Removes the "can only view your own
  timeline" over-restriction while keeping cross-project isolation.)
- **`/dates`** — computed from Aurora over `site_ids ∩ ?site` (reject a `?site`
  not in `site_ids`), scoped to `user_scope`. Kills the metadata-dots leak.
- **`/live-items`** — add the `user_scope` per-user filter (currently missing).
- **site-users** — new `GET /api/org/sites/{id}/members` reading Aurora
  memberships (admin/pm/site_manager of that site); UI stops calling legacy
  `/site-users`.
- **`/observations`** — scope by `site_ids` (currently company-only).
- **UI site selector** — options and default come from `GET /api/org/sites`
  (already `site_ids`-scoped); default = caller's primary/first accessible site,
  never a global constant.

### 3.3 Identity enrollment (folder_name)
- `PATCH /api/org/members/{sub}/folder` (shipped) is the enrollment primitive.
- **Make it part of onboarding**: when an admin invites a member
  (`create_member`) with first/last name, offer/auto-derive `folder_name =
  safe_name("First Last")` (Open decision D2: auto vs explicit, given the
  field_only-collision caveat). Add a Team-page UI field so enrollment isn't an
  API-only action.
- Attribution (`resolve_site`, recordings G5b) and "own data" both consume the
  enrolled `folder_name`; one enrollment fixes both read (own Today) and write
  (recording attribution).

### 3.4 Layered visibility (ties to companion spec)
- **Site/self tier**: immediate. A site_manager sees their site's items as soon
  as extraction lands (timeliness preserved — no draft gate at site level).
- **Company/regional tier**: aggregation, portfolio, insights, cross-project
  RAG read only **published** (site_manager-reviewed) data, and always exclude
  redacted items. `regional_manager`/`gm`/`admin` roll-ups are built on the
  published set. (Mechanics: companion spec §Review-gate.)

---

## 4. Rollout (incremental, kill-switchable)
1. **Enroll** `folder_name` for all existing logins (backfill from Aurora
   memberships / user_mapping); make invite auto-enroll. *Immediately fixes the
   Today ban and recording attribution.*
2. **Repoint reads** Today/Timeline/dates/site-users to Aurora; retire the legacy
   read fallbacks behind a flag. *Fixes the dates + site-users leaks.*
3. **Graded roles**: introduce `visible_scope`, honor `membership.role`, add
   `regional_manager`. Migrate existing users (default mapping preserves current
   behavior: everyone non-admin stays site-scoped until explicitly promoted).
4. Each step is independently shippable and reversible; the multi-tenant
   `company_id` guard is never relaxed.

---

## 5. Open decisions (for your review)
- **D1** — pm scope from `global_role` vs per-site `membership.role` vs max.
- **D2** — `regional_manager`: new `global_role` value (cleanest) vs reuse `gm`
  with a site-subset (less clean). Recommend **new value**.
- **D3** — site_manager sees **self + workers** (legacy BUG-25 rule) vs **self
  only** (stricter). Recommend **self + workers**, with the companion spec's
  redaction protecting privacy.
- **D4** — invite auto-derives `folder_name` (one-step onboarding, needs the
  field_only-collision guard) vs explicit enroll step. Recommend **auto + guard**.
- **D5** — legacy read-path retirement: hard cut vs keep as flagged fallback for
  one release. Recommend **flagged fallback**, then remove.

---

## 6. Risks
- **Widening pm/regional visibility is a real ACL change** — every change is a
  potential cross-project/cross-company leak. Every read path must go through
  `visible_scope`; add ACL tests per path (worker/site_manager/pm/regional/gm ×
  in-scope/out-of-scope) before enabling graded roles.
- **Legacy retirement** must not drop report *generation* — only reads move.
- **Enrollment collisions** (field_only vs login on the unique `folder_name`
  index, migration 0012) — the enroll route's 409 guard handles it; the
  auto-enroll path needs the same guard.
