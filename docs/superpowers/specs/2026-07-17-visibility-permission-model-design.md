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

### 3.0 Multi-tenancy model (the core the whole model rests on) — DECIDED
The company concept **already exists** in Aurora (migration `0002`): `companies`,
`users.company_id`, `sites.company_id` (NOT NULL), `memberships`. The legacy
DynamoDB store does **not** have this — another reason to consolidate on Aurora.

- **Each customer is its own `company`** (SouthBase = one company, MOE = another,
  …). A company's users see **only their own company's** sites/data. This is the
  hard multi-tenant boundary — enforced under **every** role below, never relaxed.
- **`platform_admin` (NEW tier)** = the FieldSight *vendor/ops* account that may
  cross companies (this is the user's *"测试端 admin"* exception). It is the
  **only** role that sees more than one company. Standard `admin`/`gm` are
  company-scoped — a SouthBase admin sees only SouthBase.
- **Every account carries a `company_id`.** The org-api already 403s a caller
  with no company; `platform_admin` is the sole exception (company_id may be a
  vendor/"FieldSight-platform" company or null-with-platform-flag — see D6).
- **Setup migration (new):** today all test sites (SB1108, UC PK, MPI, …) live
  under **one** company "FieldSight", so tenancy isn't actually exercised. To go
  live multi-tenant we split customers into separate `companies` and reparent
  their sites + users. Sequenced in §4.

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

Resolution (replaces the binary `resolve_scope`). **All rows below `platform_admin`
are additionally hard-scoped to the caller's `company_id`** (the multi-tenant
invariant, §3.0):

| global_role            | company scope        | site_ids                                | user_scope     | meaning |
|------------------------|----------------------|-----------------------------------------|----------------|---------|
| `platform_admin` (NEW) | **all companies**    | every site in every company             | `ALL`          | FieldSight vendor/ops/test — cross-company |
| `admin` / `gm`         | own company only     | every non-archived site in their company| `ALL`          | whole (their) company |
| `regional_manager` (NEW)| own company only    | union of assigned sites (memberships)   | `SITE`         | cross-project within their company |
| `pm`                   | own company only     | sites where they hold a `pm` **membership** | `SITE`     | all members at those projects |
| `site_manager`         | own company only     | sites where they are a member           | `SELF+WORKERS` | own + workers at their sites |
| `worker`               | own company only     | sites where they are a member           | `SELF`         | own only |

- `user_scope` decides the **per-user** filter that read paths apply on top of
  `site_ids`:
  - `ALL` → no per-user filter.
  - `SITE` → any author whose folder is attributed to an in-scope site.
  - `SELF+WORKERS` → own folder + folders of `worker`-role members on the caller's sites.
  - `SELF` → own folder only.
- **`membership.role` is now read.** `pm` scope comes from holding a `pm`
  *membership* (per-site), independent of `global_role`; this lets one person be
  a pm on Project A and a worker on Project B. **(D1 DECIDED)** `global_role`
  governs cross-project reach (regional/gm/admin/platform); per-site
  `membership.role` governs within-project authority (pm/site_manager/worker).

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
4. **Tenant split + platform_admin** (only when going truly multi-customer):
   create per-customer `companies`, reparent their sites+users, add the
   `platform_admin` role + platform company (D6). Until then the single
   "FieldSight" company keeps working unchanged.
5. Each step is independently shippable and reversible; the multi-tenant
   `company_id` guard is never relaxed.

---

## 5. Decisions (RESOLVED 2026-07-17)
- **Company structure — DECIDED:** true multi-tenancy — each customer is its own
  `company`; add a `platform_admin` cross-company tier for the FieldSight
  vendor/ops account. (§3.0)
- **D1 — DECIDED:** `global_role` governs **cross-project** reach
  (regional/gm/admin/platform); per-site **`membership.role`** governs
  **within-project** authority (pm/site_manager/worker). So a `pm` membership at
  UC PK lets Neil see James there, regardless of his global_role.
- **D2 — DECIDED:** `regional_manager` is a **new `global_role` value** (added to
  `ALLOWED_GLOBAL_ROLES`), sitting between pm and gm.
- **D3 — DECIDED:** site_manager sees **self + workers** on their sites.
- **D4 — DECIDED:** invite **auto-derives `folder_name`** (`safe_name("First
  Last")`) with the unique-index collision guard (409/skip).
- **D5 — DECIDED:** legacy read paths retired behind a **flagged fallback** for
  one release, then removed.
- **D6 — OPEN:** how `platform_admin` is represented — a dedicated
  "FieldSight-platform" company row + a `platform_admin` global_role, vs a
  `platform_admin` role with `company_id = NULL` + an explicit platform flag.
  Recommend **dedicated platform company + `platform_admin` role** (keeps the
  non-null `company_id` invariant intact everywhere else). One remaining call.

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

---
---

# 【中文翻译】可见性与权限模型 —— 统一设计(2026-07-17)

**状态:** 设计 / 待审。与 `2026-07-17-content-filter-privacy-system-design.md` 配套(两者在 site_manager 权限与分层审阅门处咬合)。

**范围:** fieldsight-pipeline(后端 ACL)+ fieldsight-ui(读路径、站点选择器)。这是对 2026-07-17 客户测试暴露的 ACL 泄漏与角色缺口的**根本性修复**——用一套连贯模型替代零散打补丁。

---

## 1. 问题

### 1.1 双后端、ACL 不一致(根因)
应用同时压在**两个**后端上,对同一问题给出不同答案:
- **遗留** `lambda_fieldsight_api.py` —— 身份/ACL 来自 DynamoDB `fieldsight-users` + S3 `config/user_mapping.json`。服务 `/api/timeline`(回落)、`/api/dates`、`/api/site-users`、`/api/users`。
- **现行** `lambda_org_api.py` + Aurora(`users`/`memberships`)—— 看板优先的事实源。服务 `/api/org/timeline`、`/live-items`、`/sites`、`/members` 等。

本应一致的视图读了不同后端,于是 ACL 在一个后端执行、另一个没执行。实测症状:
- **Today 被拒** —— 真实 site_manager(`Ben_UCPK`)被拦在**自己**的 Today 外,因为登录没链到报告文件夹(`folder_name` 未入册);aurora shim 403(`user != own`),UI 回落遗留路径,遗留也 403(`can_access_user_data`)。
- **Timeline 日期泄漏** —— 日期条圆点来自遗留 `/dates`,它拿到 `?site=` 就返回**该站点**的报告日期,**不校验调用者是否该站成员**(`get_dates` → `get_accessible_users(caller, site_filter=site)` 无成员门)。新 site_manager 能看到不属于自己的项目"有活动"这一元数据。(内容安全——点开显示"无报告"——所以是**元数据**泄漏,非内容泄漏。)
- **Timeline 默认站点越界** —— UI 把 `site` 默认成全局常量(`sb1108-ellesmere`),而非调用者可访问的站点。
- **Sites → USERS ON SITE 空 / "Access denied to this site"** —— 该面板读遗留 `/site-users`,它不认识 Aurora 独有站点(如 UC PK);而 Team 读 Aurora `/members`。成员关系是对的,只是这个面板读错了源。

### 1.2 角色模型只有两档且基本失效
- `resolve_scope`(`repositories/acl.py:1-7`)是**二值**:`{admin, gm}` → `ALL`(全公司);其余 → `MEMBERSHIPS`(自己的成员站点)。`pm`、`site_manager`、`worker` **scope 等价**。
- `memberships.role`(每站 `pm`/`site_manager`/`worker`)**存了、显示了,但任何 ACL 判定都不读**(`memberships.py:26-28`)。"给 Neil PM"目前零效果。
- **无跨项目中间层**。`regional_manager` 不在 `ALLOWED_GLOBAL_ROLES`(`lambda_org_api.py:77`)里。
- `/live-items`(`lambda_org_api.py:747-757`)**只按站点、不按人**过滤——同站任何成员看到全部作者的项(BUG-25 类回归,遗留修过、Aurora 没搬)。
- `/observations`(`repositories/observations.py`)只按 `company_id` 过滤——跨项目。

### 1.3 身份桥缺失
归属与"看自己"都依赖 `users.folder_name` 匹配 S3 报告/录音文件夹。2026-07-17 前**没有任何产品路由能设 `folder_name`**——只有手动 seed。本 session 上线的 `PATCH /api/org/members/{sub}/folder` 端点补上了这一环,但入册必须成为一等、始终执行的步骤。

---

## 2. 目标 / 非目标

**目标**
1. **单一事实源 = Aurora org-api**:所有读/ACL 路径(Today/Timeline/dates/site-users/live-items/observations)。
2. **入册身份**:每个登录链到自己的报告文件夹(`folder_name`)。
3. **角色分层真正生效**:worker < site_manager < pm < regional_manager < gm/admin,且每站尊重 `membership.role`。
4. **一个 ACL 原语**统一作用于每条读路径——不留会漂移/泄漏的逐端点特例。
5. **分层可见性**:本站内容即时(时效性);公司/区域聚合只见**已审阅/已发布**数据(隐私——见配套 spec)。
6. **保多租户不变量**:任何层级下,调用者绝不看到别的公司的数据。

**非目标**
- 重写报告*生成*(遗留管线仍生成报告;只把**读/ACL**面搬到 Aurora)。
- 内容 filter/redaction 机制(配套 spec)。

---

## 3. 设计

### 3.0 多租户模型(整个模型的内核)—— 已定
公司概念在 Aurora 里**本就存在**(migration `0002`):`companies`、`users.company_id`、`sites.company_id`(NOT NULL)、`memberships`。遗留 DynamoDB **没有**这套——又一个该弃遗留、统一到 Aurora 的理由。

- **每个客户是自己的 `company`**(SouthBase 一个公司、MOE 另一个…)。一个公司的用户**只看本公司**的站点/数据。这是硬多租户边界,下面**每个角色**都执行、绝不放松。
- **`platform_admin`(新层)** = FieldSight *厂商/运维* 账户,可跨公司(即你说的*"测试端 admin"*例外)。它是**唯一**能看多个公司的角色。标准 `admin`/`gm` 按公司收窄——SouthBase admin 只看 SouthBase。
- **每个账号都带 `company_id`。** org-api 现在就对无公司调用 403;`platform_admin` 是唯一例外(company_id 可为厂商/"FieldSight-platform" 公司,或 null+平台标记——见 D6)。
- **配置迁移(新增):** 目前所有测试站点(SB1108、UC PK、MPI…)都在**一个**公司 "FieldSight" 下,租户没真正启用。要上线多租户,须把客户拆成独立 `companies`,把其站点+用户重新挂靠。步骤见 §4。

### 3.1 一个 ACL 原语:`visible_scope(conn, caller)`
单一函数返回调用者的可见范围,供**每条**读路径使用:

```
visible_scope(conn, caller) -> {
  site_ids:      set[site_id],     # 可见的站点集合
  user_scope:    'ALL' | 'SITE' | 'SELF',
  self_folder:   folder_name | None,
}
```

解析(取代二值 `resolve_scope`)。**`platform_admin` 以下所有行都额外硬绑到调用者的 `company_id`**(多租户不变量,§3.0):

| global_role            | 公司范围        | site_ids                        | user_scope     | 含义 |
|------------------------|-----------------|---------------------------------|----------------|------|
| `platform_admin`(新)  | **所有公司**    | 每个公司的每个站点              | `ALL`          | FieldSight 厂商/运维/测试——跨公司 |
| `admin` / `gm`         | 仅本公司        | 本公司所有未归档站点            | `ALL`          | (本)全公司 |
| `regional_manager`(新)| 仅本公司        | 分配站点并集(memberships)      | `SITE`         | 本公司内跨项目 |
| `pm`                   | 仅本公司        | 持 `pm` **membership** 的站点   | `SITE`         | 这些项目的全员 |
| `site_manager`         | 仅本公司        | 作为成员的站点                  | `SELF+WORKERS` | 自己 + 本站 workers |
| `worker`               | 仅本公司        | 作为成员的站点                  | `SELF`         | 只自己 |

- `user_scope` 决定在 `site_ids` 之上的**逐用户**过滤:`ALL`=不过滤;`SITE`=作者文件夹归属于范围内站点;`SELF+WORKERS`=自己 + 本站 `worker` 角色成员;`SELF`=只自己。
- **`membership.role` 现在被读取。** pm 范围来自持有 `pm` *membership*(每站),与 `global_role` 无关;于是一人可在 A 项目是 pm、B 项目是 worker。**(D1 已定)** `global_role` 管跨项目层级(regional/gm/admin/platform);每站 `membership.role` 管项目内权限(pm/site_manager/worker)。

### 3.2 读路径统一
每个读端点都调 `visible_scope` 并统一施加 `(site_ids, 用户过滤)`。各修复自然落地:
- **`/timeline`** —— 非 `ALL` 不再硬锁自己;pm/regional/site_manager 可看 `user_scope` 内用户的时间线(去掉"只能看自己"的过度限制,同时保跨项目隔离)。
- **`/dates`** —— 由 Aurora 在 `site_ids ∩ ?site` 上计算(不在 `site_ids` 的 `?site` 直接拒),按 `user_scope` 收窄。**消除圆点元数据泄漏。**
- **`/live-items`** —— 补上缺失的 `user_scope` 逐用户过滤。
- **site-users** —— 新增 `GET /api/org/sites/{id}/members` 读 Aurora 成员(该站 admin/pm/site_manager);UI 停用遗留 `/site-users`。
- **`/observations`** —— 按 `site_ids` 收窄(现为只按公司)。
- **UI 站点选择器** —— 选项与默认来自 `GET /api/org/sites`(已按 `site_ids` 收窄);默认=调用者主站/首个可访问站,绝不用全局常量。

### 3.3 身份入册(folder_name)
- `PATCH /api/org/members/{sub}/folder`(已上线)是入册原语。
- **纳入 onboarding**:admin 邀请成员(`create_member`)带名/姓时,自动派生 `folder_name = safe_name("First Last")`(开放决策 D2:自动 vs 显式,注意 field_only 冲突);Team 页加字段,使入册不只是 API 动作。
- 归属(`resolve_site`、录音 G5b)与"看自己"都消费入册后的 `folder_name`;一次入册同时修好读(自己的 Today)与写(录音归属)。

### 3.4 分层可见性(与配套 spec 咬合)
- **本站/自己层**:即时。site_manager 抽取一落地就看到本站项(保时效——本站无草稿门)。
- **公司/区域层**:聚合、portfolio、insights、跨项目 RAG 只读**已发布**(site_manager 审阅过)数据,且始终排除 redacted 项。`regional_manager`/`gm`/`admin` 的 roll-up 建立在已发布集之上。(机制:配套 spec §审阅门。)

---

## 4. 上线(增量、可回退)
1. **入册** 所有现有登录的 `folder_name`(从 Aurora 成员/user_mapping 回填);邀请自动入册。*立即修好 Today 封禁与录音归属。*
2. **改读** Today/Timeline/dates/site-users 到 Aurora;遗留读回落挂开关退役。*修好 dates + site-users 泄漏。*
3. **分层角色**:引入 `visible_scope`,尊重 `membership.role`,加 `regional_manager`。迁移既有用户(默认映射保持现状:非 admin 一律站点范围,直到显式提升)。
4. **租户拆分 + platform_admin**(仅当真正做多客户时):建每客户 `companies`,把其站点+用户重新挂靠,加 `platform_admin` 角色 + 平台公司(D6)。在此之前,单一 "FieldSight" 公司照常工作、不变。
5. 每步独立可发、可逆;多租户 `company_id` 守卫从不放松。

---

## 5. 决策(2026-07-17 已定)
- **公司结构 —— 已定:** 真多租户——每个客户是自己的 `company`;加 `platform_admin` 跨公司层给 FieldSight 厂商/运维账户。(§3.0)
- **D1 —— 已定:** `global_role` 管**跨项目**层级(regional/gm/admin/platform);每站 **`membership.role`** 管**项目内**权限(pm/site_manager/worker)。于是在 UC PK 给 Neil 一个 pm membership,他就能看到 James,与其 global_role 无关。
- **D2 —— 已定:** `regional_manager` 是**新的 `global_role` 值**(加进 `ALLOWED_GLOBAL_ROLES`),位于 pm 与 gm 之间。
- **D3 —— 已定:** site_manager 看**自己 + 本站 workers**。
- **D4 —— 已定:** 邀请**自动派生 `folder_name`**(`safe_name("First Last")`),带唯一索引冲突守卫(409/跳过)。
- **D5 —— 已定:** 遗留读路径挂**开关回落**一版,再删。
- **D6 —— 待定:** `platform_admin` 怎么表示——专设一个 "FieldSight-platform" 公司行 + `platform_admin` 角色,vs `platform_admin` 角色 + `company_id = NULL` + 显式平台标记。推荐**专设平台公司 + `platform_admin` 角色**(别处 `company_id` 非空不变量不破)。剩这一个待拍。

---

## 6. 风险
- **放宽 pm/regional 可见性是真 ACL 改动** —— 每处改动都是潜在跨项目/跨公司泄漏。每条读路径必须走 `visible_scope`;启用分层角色前为每条路径加 ACL 测试(worker/site_manager/pm/regional/gm × 范围内/外)。
- **遗留退役**不得掉了报告*生成*——只搬读。
- **入册冲突**(field_only vs 登录在唯一 `folder_name` 索引上,migration 0012)—— 入册路由的 409 守卫已处理;自动入册路径需同样的守卫。
