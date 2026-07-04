# Phase 3 收尾 · 批次1:后端数据模型完善 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
> **REVISED 2026-07-04(Fable 复审,用户已批,见 spec §8b)**:并入归档 caller 语义、include_archived 发现、自我归档守卫、重邀 409、membership 复活、pending 过期 400、PATCH /sites、头像清除、asset-url 拒 pending。任务重排:新 Task 7 = PATCH /sites;原 T7/T8 → T8/T9。

**Goal:** 给 org 数据模型加归档软删除(含完整语义:发现/守卫/复活)+ 上传资产生命周期治理(pending 前缀 + 提交搬迁 + 替换删旧 + 过期兜底),消除频繁更新的孤儿并支持项目/人员归档。

**Architecture:** 新迁移 `0005_archive.sql` 给 `sites/users/memberships` 加 `archived_at`;仓储 list 默认过滤已归档(admin 可 `include_archived` 发现)+ archive/unarchive(级联 memberships);`lambda_org_api` 加归档端点与归档 caller 拦截 + 把上传改成「先签到 `org-assets/pending/`,提交时 CopyObject 到正式 key + 删 pending + 删旧」+ `PATCH /sites/{id}`;template 加 `s3:DeleteObject` + 新脚本下发 pending 的 1 天过期 lifecycle。

**Tech Stack:** Python 3.11 Lambda,psycopg3(无 ORM),SAM(src/template.yaml),pytest(unit 本地 / integration 走 CI pgvector 容器),boto3 S3。

**Spec:** `docs/superpowers/specs/2026-07-04-phase-3-completion-org-datamodel-ui.md` §4 + §8b。本批**不含 UI**(批次2另写)。

## Global Constraints

- 账号 `509194952652`/ap-southeast-2;prod 手工资源不碰;只部署 `fieldsight-test` 栈(develop→CI),绝不 `sam deploy` 碰 prod。
- Windows autocrlf=true 混合行尾:用**单行 Edit anchor**;**绝不** `git add -A`/`git add .`,只 add 明确路径。未跟踪的本地文件(`assets/`、`benchmark/`、`claw-code/`、`scripts/aws-*.sh`、`src/requirements.txt`、用户 roadmap 笔记)绝不 stage/删。
- Lambda runtime=python3.11;CI Python=3.11。本地 python 3.14 装了 psycopg+boto3,unit + handler 测试本地可跑;integration 测试需 `TEST_DATABASE_URL`(本地无则 skip,CI pgvector 容器跑)。
- **仓储永不 commit**——caller 拥有事务(`with get_connection() as conn:` 干净退出提交、异常回滚、块尾关闭)。同一 repo 函数内多条 SQL 属同一事务。
- ACL deny-by-default;角色 `admin|gm|pm|site_manager|worker`;写路径公司守卫(WHERE ... AND company_id=%s)。
- 迁移是纯 `.sql`,命名 `NNNN_name.sql`,`db/migrate.py` 按数字排序、每文件一个事务(多语句 OK,无参数走 simple query protocol)。
- `{{resolve:secretsmanager:...}}` 只与 Parameter 组合;cfn-lint E1051 连注释里的 resolve 字面量都抓——别在 YAML 注释写。
- S3 CopyObject 的 IAM = `s3:GetObject`(源)+`s3:PutObject`(目标),**非**独立 action;新增的只有 `s3:DeleteObject`。
- 跑测试:`python -m pytest tests/unit -v`(integration 无 TEST_DATABASE_URL 自动 skip);`cfn-lint src/template.yaml infra/db-template.yaml` 保持 exit 0(二进制可能在 `C:/Users/camil/AppData/Local/Python/pythoncore-3.14-64/Scripts/cfn-lint.exe`)。
- 提交:conventional(`feat(3b):`/`fix(3b):`),每个绿色 TDD 循环一提交。

---

### Task 1: 迁移 0005 — archived_at 软删列

**Files:**
- Create: `src/migrations/0005_archive.sql`
- Test: `tests/integration/test_archive.py`(新建)

**Interfaces:**
- Consumes: `db.migrate.apply_migrations`(已有),`tests/conftest.py` 的 `migrated_db_url`/`db` fixture。
- Produces: `sites`/`users`/`memberships` 各有 `archived_at timestamptz`(可空,默认 NULL)。后续 Task 2-7 依赖此列。

- [ ] **Step 1: 写失败的集成测试**

新建 `tests/integration/test_archive.py`:

```python
import pytest
from repositories import companies, users, sites, memberships

pytestmark = pytest.mark.integration


def _columns(conn, table):
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name=%s",
        (table,),
    ).fetchall()
    return {r[0] for r in rows}


def test_archived_at_columns_exist(db):
    for t in ("sites", "users", "memberships"):
        assert "archived_at" in _columns(db, t)
```

- [ ] **Step 2: 跑,确认(本地 skip / collection 干净)**

Run: `python -m pytest tests/integration/test_archive.py -v`(本地 SKIPPED)+ `python -m pytest tests/ --collect-only -q`(无 collection error)。CI 里真跑会因缺列 FAIL。

- [ ] **Step 3: 写迁移**

新建 `src/migrations/0005_archive.sql`:

```sql
-- Phase 3 batch 1: soft-delete (archive) support. NULL = active.
ALTER TABLE sites       ADD COLUMN archived_at timestamptz;
ALTER TABLE users       ADD COLUMN archived_at timestamptz;
ALTER TABLE memberships ADD COLUMN archived_at timestamptz;
```

- [ ] **Step 4: 确认 collection 干净 + unit 绿**

Run: `python -m pytest tests/ --collect-only -q` 然后 `python -m pytest tests/unit -v`(全 PASS,未变)。

- [ ] **Step 5: 提交**

```bash
git add src/migrations/0005_archive.sql tests/integration/test_archive.py
git commit -m "feat(3b): migration 0005 — archived_at soft-delete columns"
```

---

### Task 2: 仓储 list 过滤已归档(+ include_archived 发现)

**Files:**
- Modify: `src/repositories/users.py`、`src/repositories/sites.py`、`src/repositories/memberships.py`
- Test: `tests/integration/test_archive.py`(追加)

**Interfaces:**
- Consumes: Task 1 的 `archived_at` 列;现有 `_COLS`、`upsert_user`、`create_site`、`create_company`、`ensure_membership`。
- Produces(供 Task 3-7 与已有调用方):
  - `users._COLS` / `sites._COLS` 末尾含 `archived_at`(所有返回行多一字段,无害)。
  - `users.list_company_users(conn, company_id, include_archived=False)`、`sites.list_company_sites(conn, company_id, include_archived=False)`——默认只回未归档;`include_archived=True` 全量(admin 的「查看已归档」发现用)。
  - `sites.list_sites_by_ids`、`memberships.list_company_memberships`、`memberships.accessible_site_ids` 只返回未归档行(无 include 参数——ACL 路径永不含归档)。
  - `sites.get_company_site_by_name`、`sites.get_site`、`users.get_user_by_sub` **不过滤**(seed 幂等 / caller 自读 archived 仍可)。

- [ ] **Step 1: 写失败的集成测试**(追加到 `tests/integration/test_archive.py`)

```python
def test_lists_hide_archived_rows_and_include_flag(db):
    co = companies.create_company(db, "ArchCo")
    s_live = sites.create_site(db, co["id"], "Live Site")
    s_arch = sites.create_site(db, co["id"], "Arch Site")
    u_live = users.upsert_user(db, "sub-al", "al@x.nz", company_id=co["id"])
    u_arch = users.upsert_user(db, "sub-aa", "aa@x.nz", company_id=co["id"])
    memberships.ensure_membership(db, u_live["id"], s_live["id"], "worker")
    db.execute("UPDATE sites SET archived_at=now() WHERE id=%s", (s_arch["id"],))
    db.execute("UPDATE users SET archived_at=now() WHERE id=%s", (u_arch["id"],))

    assert [r["name"] for r in sites.list_company_sites(db, co["id"])] == ["Live Site"]
    assert {r["name"] for r in sites.list_company_sites(db, co["id"], include_archived=True)} == {"Live Site", "Arch Site"}
    assert [s["name"] for s in sites.list_sites_by_ids(db, [s_live["id"], s_arch["id"]])] == ["Live Site"]
    assert [u["cognito_sub"] for u in users.list_company_users(db, co["id"])] == ["sub-al"]
    assert {u["cognito_sub"] for u in users.list_company_users(db, co["id"], include_archived=True)} == {"sub-al", "sub-aa"}
    # get_* point lookups still see archived (seed idempotency / self-read)
    assert sites.get_company_site_by_name(db, co["id"], "Arch Site") is not None
    assert users.get_user_by_sub(db, "sub-aa") is not None


def test_accessible_site_ids_excludes_archived(db):
    co = companies.create_company(db, "AccArch")
    s1 = sites.create_site(db, co["id"], "S1")
    s2 = sites.create_site(db, co["id"], "S2")
    w = users.upsert_user(db, "sub-w", "w@x.nz", company_id=co["id"], global_role="worker")
    memberships.ensure_membership(db, w["id"], s1["id"], "worker")
    memberships.ensure_membership(db, w["id"], s2["id"], "worker")
    db.execute("UPDATE memberships SET archived_at=now() WHERE site_id=%s", (s2["id"],))
    assert memberships.accessible_site_ids(db, w["id"], "worker") == [s1["id"]]
    adm = users.upsert_user(db, "sub-adm", "adm@x.nz", company_id=co["id"], global_role="admin")
    db.execute("UPDATE sites SET archived_at=now() WHERE id=%s", (s2["id"],))
    assert set(memberships.accessible_site_ids(db, adm["id"], "admin")) == {s1["id"]}
```

- [ ] **Step 2: 跑,确认 collection 干净(本地 skip)**

Run: `python -m pytest tests/ --collect-only -q`(无错)。

- [ ] **Step 3: 改仓储**

`src/repositories/users.py` — `_COLS` 改为:

```python
_COLS = "id, cognito_sub, company_id, email, first_name, last_name, avatar_s3_key, global_role, created_at, archived_at"
```

`list_company_users` 整函数替换为:

```python
def list_company_users(conn, company_id, include_archived=False) -> list[dict]:
    guard = "" if include_archived else "AND archived_at IS NULL "
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM users WHERE company_id=%s {guard}ORDER BY created_at",
        (company_id,),
    ).fetchall()
```

`src/repositories/sites.py` — `_COLS` 改为:

```python
_COLS = "id, company_id, name, location, client, industry, icon_s3_key, created_at, archived_at"
```

`list_company_sites` 整函数替换为:

```python
def list_company_sites(conn, company_id, include_archived=False) -> list[dict]:
    guard = "" if include_archived else "AND archived_at IS NULL "
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM sites WHERE company_id=%s {guard}ORDER BY created_at",
        (company_id,),
    ).fetchall()
```

`list_sites_by_ids` 的 `WHERE id = ANY(%s)` 行改为:

```python
        f"SELECT {_COLS} FROM sites WHERE id = ANY(%s) AND archived_at IS NULL ORDER BY created_at",
```

`src/repositories/memberships.py` — `list_company_memberships` 的 WHERE 行(单行 anchor `"WHERE s.company_id = %s AND u.company_id = s.company_id "`)改为:

```python
        "WHERE s.company_id = %s AND u.company_id = s.company_id AND m.archived_at IS NULL "
```

`accessible_site_ids`:ALL 分支 `WHERE u.id = %s` 改为 `WHERE u.id = %s AND s.archived_at IS NULL`;MEMBERSHIPS 分支 `"SELECT site_id FROM memberships WHERE user_id=%s"` 改为 `"SELECT site_id FROM memberships WHERE user_id=%s AND archived_at IS NULL"`。

- [ ] **Step 4: 确认 collection + unit 绿**

Run: `python -m pytest tests/ --collect-only -q` 然后 `python -m pytest tests/unit -v`(全 PASS)。

- [ ] **Step 5: 提交**

```bash
git add src/repositories/users.py src/repositories/sites.py src/repositories/memberships.py tests/integration/test_archive.py
git commit -m "feat(3b): repo lists filter archived_at (+include_archived discovery); archived_at in _COLS"
```

---

### Task 3: 仓储 archive/unarchive、membership 复活、clear_avatar、update_site

**Files:**
- Modify: `src/repositories/sites.py`、`src/repositories/users.py`、`src/repositories/memberships.py`
- Test: `tests/integration/test_archive.py`(追加)

**Interfaces:**
- Consumes: Task 1/2 的列与 `_COLS`。
- Produces(供 Task 4-7):
  - `sites.archive_site(conn, site_id, company_id) -> dict | None`(公司守卫;级联归档该 site 的 memberships;已归档/越权/不存在→None)
  - `sites.unarchive_site(conn, site_id, company_id) -> dict | None`(只恢复 site 行,不级联)
  - `sites.set_site_icon(conn, site_id, icon_s3_key) -> dict`
  - `sites.update_site(conn, site_id, company_id, name=None, location=None, client=None, industry=None) -> dict | None`(COALESCE None=不改;公司守卫;已归档→None)
  - `users.archive_user(conn, cognito_sub, company_id) -> dict | None`(级联其 memberships)
  - `users.unarchive_user(conn, cognito_sub, company_id) -> dict | None`
  - `users.clear_avatar(conn, cognito_sub) -> dict | None`(avatar_s3_key 置 NULL)
  - `memberships.ensure_membership` ON CONFLICT 现在**同时复活**(`archived_at=NULL`)——重新加人=重新激活。

- [ ] **Step 1: 写失败的集成测试**(追加)

```python
def test_archive_site_cascades_memberships(db):
    co = companies.create_company(db, "CascadeCo")
    s = sites.create_site(db, co["id"], "Casc Site")
    u = users.upsert_user(db, "sub-cs", "cs@x.nz", company_id=co["id"])
    memberships.ensure_membership(db, u["id"], s["id"], "worker")
    row = sites.archive_site(db, s["id"], co["id"])
    assert row is not None and row["archived_at"] is not None
    assert sites.list_company_sites(db, co["id"]) == []
    assert memberships.accessible_site_ids(db, u["id"], "worker") == []
    other = companies.create_company(db, "OtherCo")
    assert sites.archive_site(db, s["id"], other["id"]) is None   # cross-company
    assert sites.archive_site(db, s["id"], co["id"]) is None      # double-archive
    assert sites.unarchive_site(db, s["id"], co["id"])["archived_at"] is None
    assert [x["name"] for x in sites.list_company_sites(db, co["id"])] == ["Casc Site"]
    assert memberships.accessible_site_ids(db, u["id"], "worker") == []  # membership stays archived
    # re-adding revives the archived membership (ON CONFLICT resets archived_at)
    m = memberships.ensure_membership(db, u["id"], s["id"], "site_manager")
    assert m["role"] == "site_manager"
    assert memberships.accessible_site_ids(db, u["id"], "worker") == [s["id"]]


def test_archive_user_cascades_and_guards(db):
    co = companies.create_company(db, "UArchCo")
    s = sites.create_site(db, co["id"], "S")
    u = users.upsert_user(db, "sub-ua", "ua@x.nz", company_id=co["id"])
    memberships.ensure_membership(db, u["id"], s["id"], "worker")
    assert users.archive_user(db, "sub-ua", co["id"])["archived_at"] is not None
    assert users.list_company_users(db, co["id"]) == []
    assert memberships.accessible_site_ids(db, u["id"], "worker") == []
    other = companies.create_company(db, "Other2")
    assert users.archive_user(db, "sub-ua", other["id"]) is None
    assert users.unarchive_user(db, "sub-ua", co["id"])["archived_at"] is None


def test_set_site_icon_and_update_site(db):
    co = companies.create_company(db, "IconCo")
    s = sites.create_site(db, co["id"], "Icon Site", location="Chch")
    row = sites.set_site_icon(db, s["id"], "org-assets/site-icons/" + str(s["id"]) + "/x.png")
    assert row["icon_s3_key"].endswith("x.png")
    row = sites.update_site(db, s["id"], co["id"], name="Renamed")
    assert row["name"] == "Renamed" and row["location"] == "Chch"  # None-preserving
    other = companies.create_company(db, "IconOther")
    assert sites.update_site(db, s["id"], other["id"], name="X") is None  # company guard
    db.execute("UPDATE sites SET archived_at=now() WHERE id=%s", (s["id"],))
    assert sites.update_site(db, s["id"], co["id"], name="Y") is None     # archived -> None


def test_clear_avatar(db):
    co = companies.create_company(db, "AvCo")
    users.upsert_user(db, "sub-av", "av@x.nz", company_id=co["id"])
    users.update_profile(db, "sub-av", avatar_s3_key="org-assets/avatars/sub-av/a.png")
    row = users.clear_avatar(db, "sub-av")
    assert row["avatar_s3_key"] is None
    assert users.clear_avatar(db, "sub-ghost") is None
```

- [ ] **Step 2: 跑,确认 collection 干净(本地 skip)**

Run: `python -m pytest tests/ --collect-only -q`(无 ImportError)。

- [ ] **Step 3: 实现**

追加到 `src/repositories/sites.py`:

```python
def archive_site(conn, site_id, company_id) -> dict | None:
    """Soft-delete a site (company-guarded) and cascade-archive its
    memberships. Returns None if not found / wrong company / already archived."""
    cur = conn.cursor(row_factory=dict_row)
    row = cur.execute(
        f"UPDATE sites SET archived_at=now() "
        f"WHERE id=%s AND company_id=%s AND archived_at IS NULL RETURNING {_COLS}",
        (site_id, company_id),
    ).fetchone()
    if row is None:
        return None
    cur.execute(
        "UPDATE memberships SET archived_at=now() "
        "WHERE site_id=%s AND archived_at IS NULL", (site_id,))
    return row


def unarchive_site(conn, site_id, company_id) -> dict | None:
    """Restore a site row only (memberships are NOT auto-restored — re-add
    people explicitly, which revives via ensure_membership)."""
    return conn.cursor(row_factory=dict_row).execute(
        f"UPDATE sites SET archived_at=NULL "
        f"WHERE id=%s AND company_id=%s AND archived_at IS NOT NULL RETURNING {_COLS}",
        (site_id, company_id),
    ).fetchone()


def set_site_icon(conn, site_id, icon_s3_key) -> dict:
    return conn.cursor(row_factory=dict_row).execute(
        f"UPDATE sites SET icon_s3_key=%s WHERE id=%s RETURNING {_COLS}",
        (icon_s3_key, site_id),
    ).fetchone()


def update_site(conn, site_id, company_id, name=None, location=None,
                client=None, industry=None) -> dict | None:
    """None = leave unchanged (same semantics as users.update_profile).
    Company-guarded; archived sites are not editable."""
    return conn.cursor(row_factory=dict_row).execute(
        f"UPDATE sites SET "
        f"  name=COALESCE(%(name)s, name), "
        f"  location=COALESCE(%(loc)s, location), "
        f"  client=COALESCE(%(client)s, client), "
        f"  industry=COALESCE(%(ind)s, industry) "
        f"WHERE id=%(sid)s AND company_id=%(cid)s AND archived_at IS NULL "
        f"RETURNING {_COLS}",
        {"sid": site_id, "cid": company_id, "name": name, "loc": location,
         "client": client, "ind": industry},
    ).fetchone()
```

追加到 `src/repositories/users.py`:

```python
def archive_user(conn, cognito_sub, company_id) -> dict | None:
    """Soft-delete a user (company-guarded) and cascade-archive their
    memberships. Cognito login is NOT touched (design). Returns None if not
    found / wrong company / already archived."""
    cur = conn.cursor(row_factory=dict_row)
    row = cur.execute(
        f"UPDATE users SET archived_at=now() "
        f"WHERE cognito_sub=%s AND company_id=%s AND archived_at IS NULL RETURNING {_COLS}",
        (cognito_sub, company_id),
    ).fetchone()
    if row is None:
        return None
    cur.execute(
        "UPDATE memberships SET archived_at=now() "
        "WHERE user_id=%s AND archived_at IS NULL", (row["id"],))
    return row


def unarchive_user(conn, cognito_sub, company_id) -> dict | None:
    return conn.cursor(row_factory=dict_row).execute(
        f"UPDATE users SET archived_at=NULL "
        f"WHERE cognito_sub=%s AND company_id=%s AND archived_at IS NOT NULL RETURNING {_COLS}",
        (cognito_sub, company_id),
    ).fetchone()


def clear_avatar(conn, cognito_sub) -> dict | None:
    """Explicit avatar removal (update_profile's COALESCE can't set NULL)."""
    return conn.cursor(row_factory=dict_row).execute(
        f"UPDATE users SET avatar_s3_key=NULL WHERE cognito_sub=%s RETURNING {_COLS}",
        (cognito_sub,),
    ).fetchone()
```

`src/repositories/memberships.py` 的 `ensure_membership`:把 `"ON CONFLICT (user_id, site_id) DO UPDATE SET role=EXCLUDED.role "` 那行改为:

```python
        "ON CONFLICT (user_id, site_id) DO UPDATE SET role=EXCLUDED.role, archived_at=NULL "
```

并把其 docstring 补一句(在 idempotent 说明后):`Re-adding an archived membership revives it (archived_at reset). NOTE: a seed re-run therefore revives archived memberships — folded into the documented seed re-run quirk.`

- [ ] **Step 4: 确认 collection + unit 绿**

Run: `python -m pytest tests/ --collect-only -q` 然后 `python -m pytest tests/unit -v`。

- [ ] **Step 5: 提交**

```bash
git add src/repositories/sites.py src/repositories/users.py src/repositories/memberships.py tests/integration/test_archive.py
git commit -m "feat(3b): repo archive/unarchive (cascade), membership revive, clear_avatar, update_site"
```

---

### Task 4: org-api 归档端点 + 归档 caller 语义 + include_archived + 重邀 409

**Files:**
- Modify: `src/lambda_org_api.py`
- Test: `tests/unit/test_lambda_org_api.py`(改 2 个现有 fake + 追加)

**Interfaces:**
- Consumes: Task 3 的 `archive_site/unarchive_site/archive_user/unarchive_user`;Task 2 的 `include_archived` kwarg;现有 `resolve_scope`、`ok`/`error`、`dispatch`。
- Produces:
  - dispatch:archived caller → 除 `GET /me` 外一律 403「account archived」。
  - `POST /api/org/sites/{id}/archive` · `/unarchive`、`POST /api/org/members/{sub}/archive` · `/unarchive`(admin/gm;归档成员禁 self)。
  - `GET /api/org/sites?include_archived=1`、`GET /api/org/members?include_archived=1`(仅 ALL-scope 生效)——`list_org_sites(conn, caller, event)`、`list_members(conn, caller, event)` 签名改变。
  - `create_member`:同公司 archived 用户 → 409。

- [ ] **Step 1: 先适配 2 个现有 fake(kwarg 兼容),再写失败的新测试**

现有 `test_list_sites_admin_gets_company_sites` 的 fake `lambda conn, cid: [...]` 会因新 kwarg 炸——改为 `lambda conn, cid, include_archived=False: [{"id": "s-1", "name": "Alpha"}]`。同理 `test_list_members_joins_memberships` 的 `users.list_company_users` fake 改为 `lambda conn, cid, include_archived=False: [...]`(原返回值不变)。

追加新测试:

```python
def test_archived_caller_blocked_except_get_me(wired):
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "archived_at": "2026-07-04"})
    wired.setattr(org.memberships, "accessible_site_ids", lambda *a: [])
    assert org.lambda_handler(make_event("GET", "/api/org/me"), None)["statusCode"] == 200
    assert org.lambda_handler(make_event("GET", "/api/org/sites"), None)["statusCode"] == 403
    assert org.lambda_handler(make_event("POST", "/api/org/sites", body={"name": "X"}), None)["statusCode"] == 403


def test_archive_site_admin_ok_and_404(wired):
    seen = {}
    wired.setattr(org.sites, "archive_site",
                  lambda conn, sid, cid: (seen.update(sid=sid, cid=cid)
                                          or {"id": sid, "archived_at": "2026-07-04"}))
    res = org.lambda_handler(make_event("POST", "/api/org/sites/s-1/archive"), None)
    assert res["statusCode"] == 200
    assert seen == {"sid": "s-1", "cid": "c-uuid-1"}
    wired.setattr(org.sites, "archive_site", lambda conn, sid, cid: None)
    assert org.lambda_handler(make_event("POST", "/api/org/sites/s-9/archive"), None)["statusCode"] == 404


def test_unarchive_site_routes(wired):
    wired.setattr(org.sites, "unarchive_site",
                  lambda conn, sid, cid: {"id": sid, "archived_at": None})
    res = org.lambda_handler(make_event("POST", "/api/org/sites/s-1/unarchive"), None)
    assert res["statusCode"] == 200 and body_of(res)["archived_at"] is None


def test_archive_site_worker_403(wired):
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "worker"})
    assert org.lambda_handler(make_event("POST", "/api/org/sites/s-1/archive"), None)["statusCode"] == 403


def test_archive_member_ok_but_never_self(wired):
    wired.setattr(org.users, "archive_user",
                  lambda conn, sub, cid: {"cognito_sub": sub, "archived_at": "x"})
    assert org.lambda_handler(make_event("POST", "/api/org/members/sub-2/archive"), None)["statusCode"] == 200
    # self-archive -> 400 (last-admin lockout guard)
    assert org.lambda_handler(make_event("POST", "/api/org/members/sub-1/archive"), None)["statusCode"] == 400
    # unarchive self is fine (row can't be reached anyway while archived, but no self-guard needed)
    wired.setattr(org.users, "unarchive_user", lambda conn, sub, cid: {"cognito_sub": sub, "archived_at": None})
    assert org.lambda_handler(make_event("POST", "/api/org/members/sub-2/unarchive"), None)["statusCode"] == 200


def test_include_archived_param_admin_only(wired):
    seen = {}

    def fake_list(conn, cid, include_archived=False):
        seen["inc"] = include_archived
        return []

    wired.setattr(org.sites, "list_company_sites", fake_list)
    ev = make_event("GET", "/api/org/sites")
    ev["queryStringParameters"] = {"include_archived": "1"}
    org.lambda_handler(ev, None)
    assert seen["inc"] is True
    # workers never get archived rows (membership path has no include flag)
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "worker"})
    wired.setattr(org.memberships, "accessible_site_ids", lambda *a: [])
    wired.setattr(org.sites, "list_sites_by_ids", lambda conn, ids: [])
    ev2 = make_event("GET", "/api/org/sites")
    ev2["queryStringParameters"] = {"include_archived": "1"}
    assert org.lambda_handler(ev2, None)["statusCode"] == 200  # ignored, not honored


def test_create_member_archived_same_company_409(member_wired):
    wired, fake = member_wired
    fake.exists = True

    def by_sub(conn, sub):
        if sub == "sub-1":
            return dict(CALLER)
        if sub == "sub-existing":
            return {**CALLER, "cognito_sub": "sub-existing", "archived_at": "2026-07-01"}
        return None

    wired.setattr(org.users, "get_user_by_sub", by_sub)
    res = org.lambda_handler(make_event("POST", "/api/org/members", body={
        "email": "back@x.nz"}), None)
    assert res["statusCode"] == 409
```

- [ ] **Step 2: 跑,确认失败**

Run: `python -m pytest tests/unit/test_lambda_org_api.py -v -k "archive or include_archived"`
Expected: FAIL(路由/守卫未加)。

- [ ] **Step 3: 实现**

`dispatch()` 里,`if not caller["company_id"]:` 块之后插入:

```python
    if caller.get("archived_at") is not None and not (route == "/me" and method == "GET"):
        return error("account archived", 403)
```

`/sites` 与 `/members` 的 GET 调用行改为传 event:`return list_org_sites(conn, caller, event)` / `return list_members(conn, caller, event)`。

`m = re.match(r"^/members/([^/]+)/role$", route)` 块之后、`/upload-url` 之前插入:

```python
    m_sa = re.match(r"^/sites/([^/]+)/(archive|unarchive)$", route)
    if m_sa and method == "POST":
        return archive_site_endpoint(conn, caller, m_sa.group(1), m_sa.group(2))
    m_ma = re.match(r"^/members/([^/]+)/(archive|unarchive)$", route)
    if m_ma and method == "POST":
        return archive_member_endpoint(conn, caller, m_ma.group(1), m_ma.group(2))
```

`list_org_sites` 整函数替换为:

```python
def list_org_sites(conn, caller, event):
    include_archived = ((event.get("queryStringParameters") or {})
                        .get("include_archived") == "1")
    if resolve_scope(caller["global_role"]) == "ALL":
        rows = sites.list_company_sites(conn, caller["company_id"],
                                        include_archived=include_archived)
    else:
        # membership scope never includes archived rows (param ignored)
        ids = memberships.accessible_site_ids(
            conn, caller["id"], caller["global_role"])
        rows = sites.list_sites_by_ids(conn, ids)
    return ok({"sites": rows})
```

`list_members` 的签名与首两行改为:

```python
def list_members(conn, caller, event):
    if resolve_scope(caller["global_role"]) != "ALL":
        return error("admin or gm role required", 403)
    include_archived = ((event.get("queryStringParameters") or {})
                        .get("include_archived") == "1")
    rows = users.list_company_users(conn, caller["company_id"],
                                    include_archived=include_archived)
```

(其余 memberships join 逻辑不变。)

`create_member` 里,紧跟 409 跨公司守卫之后插入:

```python
    if existing and existing["company_id"] == caller["company_id"] and existing.get("archived_at"):
        return error("user is archived — unarchive them instead", 409)
```

`create_member` 之后、assets 小节之前追加:

```python
# ----------------------------------------------------------
# archive / unarchive (admin/gm, company-guarded)
# ----------------------------------------------------------
def archive_site_endpoint(conn, caller, site_id, action):
    if resolve_scope(caller["global_role"]) != "ALL":
        return error("admin or gm role required", 403)
    fn = sites.archive_site if action == "archive" else sites.unarchive_site
    row = fn(conn, site_id, caller["company_id"])
    if row is None:
        return error("site not found in your company", 404)
    return ok(row)


def archive_member_endpoint(conn, caller, target_sub, action):
    if resolve_scope(caller["global_role"]) != "ALL":
        return error("admin or gm role required", 403)
    if action == "archive" and target_sub == caller["cognito_sub"]:
        return error("cannot archive yourself", 400)
    fn = users.archive_user if action == "archive" else users.unarchive_user
    row = fn(conn, target_sub, caller["company_id"])
    if row is None:
        return error("member not found in your company", 404)
    return ok(row)
```

- [ ] **Step 4: 跑测试**

Run: `python -m pytest tests/unit -v`(全 PASS)。

- [ ] **Step 5: 提交**

```bash
git add src/lambda_org_api.py tests/unit/test_lambda_org_api.py
git commit -m "feat(3b): archive endpoints + archived-caller semantics + include_archived + re-invite 409"
```

---

### Task 5: upload-url 改签 pending 前缀;asset-url 拒 pending 读

**Files:**
- Modify: `src/lambda_org_api.py`(`create_upload_url`、`get_asset_url`)
- Test: `tests/unit/test_lambda_org_api.py`(改现有 upload 测试 + 加 1)

**Interfaces:**
- Consumes: 现有 `create_upload_url`、`get_asset_url`、`ALLOWED_IMAGE_TYPES`、`s3()`。
- Produces:`POST /upload-url` 签发 key 到 `org-assets/pending/{caller_sub}/{uuid}.{ext}`(avatar 与 site_icon 同前缀,kind 只决定 role 检查);`GET /asset-url` 对 `org-assets/pending/` 前缀 400(UI 预览用本地 FileReader,不读 pending)。Task 6/7 的提交搬迁依赖 pending 前缀。

- [ ] **Step 1: 改现有测试 + 加 1 个新测试**

`test_upload_url_avatar` 的断言改为 `assert b["key"].startswith("org-assets/pending/sub-1/")`;`test_upload_url_site_icon_admin_gets_owner_scoped_key` 的断言改为 `assert body_of(res)["key"].startswith("org-assets/pending/sub-1/")`。(worker 403 与 content-type 测试不变。)

追加:

```python
def test_asset_url_rejects_pending_reads(presign_wired):
    res = org.lambda_handler(make_event(
        "GET", "/api/org/asset-url",
        params={"key": "org-assets/pending/sub-1/x.png"}), None)
    assert res["statusCode"] == 400
```

- [ ] **Step 2: 跑,确认失败**

Run: `python -m pytest tests/unit/test_lambda_org_api.py -v -k "upload_url or asset_url"`(改动的用例 FAIL)。

- [ ] **Step 3: 实现**

`create_upload_url` 的 kind 分支改为(删掉两个分支里各自的 `key = ...`,统一在后面赋值):

```python
    kind = body.get("kind")
    if kind == "avatar":
        pass
    elif kind == "site_icon":
        if caller["global_role"] not in ("admin", "gm"):
            return error("admin or gm role required", 403)
    else:
        return error("kind must be avatar or site_icon", 400)
    # Upload lands in a pending prefix; patch_me / site create+patch relocate
    # it to the permanent key on commit. Abandoned uploads are swept by the
    # 1-day S3 lifecycle rule on org-assets/pending/.
    key = f"{ORG_ASSETS_PREFIX}pending/{caller['cognito_sub']}/{uuid.uuid4().hex}.{ext}"
```

`get_asset_url` 的前缀守卫后加:

```python
    if key.startswith(f"{ORG_ASSETS_PREFIX}pending/"):
        return error("pending uploads are not readable — commit them first", 400)
```

- [ ] **Step 4: 跑测试**

Run: `python -m pytest tests/unit -v`(全 PASS)。

- [ ] **Step 5: 提交**

```bash
git add src/lambda_org_api.py tests/unit/test_lambda_org_api.py
git commit -m "feat(3b): upload-url signs to pending/; asset-url refuses pending reads"
```

---

### Task 6: 提交搬迁 + 删旧 + 过期 400 + 头像清除(patch_me / create_org_site)

**Files:**
- Modify: `src/lambda_org_api.py`
- Test: `tests/unit/test_lambda_org_api.py`(FakeS3 扩展 + 改/加测试)

**Interfaces:**
- Consumes: Task 5 的 pending key 约定;`s3()`;`sites.set_site_icon`、`users.clear_avatar`(Task 3);现有 `users.update_profile`、`sites.create_site`;`botocore.exceptions.ClientError`。
- Produces:
  - 助手 `_relocate_asset(pending_key, final_key) -> bool`(copy+del pending;源不存在→False)与 `_delete_asset(key)`。
  - `patch_me`:`avatar_s3_key` 为 string→必须是调用者 pending key,搬迁到 `org-assets/avatars/{sub}/{fname}`、删 pending、删旧、存正式 key;**显式 null**(键存在且值 null)→ 删旧 + `clear_avatar`;搬迁源缺失→400「upload expired」。
  - `create_org_site`:pending `icon_s3_key` → 建行后搬迁到 `org-assets/site-icons/{site_id}/{fname}` + `set_site_icon`。

- [ ] **Step 1: 扩展 FakeS3 + 改/加测试**

`tests/unit/test_lambda_org_api.py` 里把 `class FakeS3:` 整类替换为:

```python
class FakeS3:
    def __init__(self):
        self.copied = []
        self.deleted = []
        self.missing_source = False

    def generate_presigned_url(self, op, Params=None, ExpiresIn=0):
        self.last = {"op": op, "params": Params, "expires": ExpiresIn}
        return "https://s3.example/" + Params["Key"]

    def copy_object(self, Bucket=None, CopySource=None, Key=None):
        if self.missing_source:
            from botocore.exceptions import ClientError
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "CopyObject")
        self.copied.append((CopySource["Key"], Key))

    def delete_object(self, Bucket=None, Key=None):
        self.deleted.append(Key)
```

现有 `test_patch_me_avatar_must_be_caller_scoped`(`org-assets/avatars/sub-OTHER/x.png` → 400)与 `test_patch_me_rejects_foreign_avatar_key`(`reports/...` → 400)在新校验(必须 pending 前缀)下**仍 400,不改**。`test_patch_me_updates_profile_fields_only` 只发 first_name,不碰 S3,**不改**。

追加:

```python
def test_patch_me_relocates_pending_avatar_and_deletes_old(presign_wired):
    wired, fake = presign_wired
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "avatar_s3_key": "org-assets/avatars/sub-1/old.png"})
    captured = {}
    wired.setattr(org.users, "update_profile",
                  lambda conn, sub, **kw: (captured.update(kw) or {**CALLER, **kw}))
    pending = "org-assets/pending/sub-1/newhex.png"
    res = org.lambda_handler(make_event("PATCH", "/api/org/me",
                                        body={"avatar_s3_key": pending}), None)
    assert res["statusCode"] == 200
    assert captured["avatar_s3_key"] == "org-assets/avatars/sub-1/newhex.png"
    assert fake.copied == [(pending, "org-assets/avatars/sub-1/newhex.png")]
    assert pending in fake.deleted and "org-assets/avatars/sub-1/old.png" in fake.deleted


def test_patch_me_expired_pending_400(presign_wired):
    wired, fake = presign_wired
    fake.missing_source = True
    res = org.lambda_handler(make_event("PATCH", "/api/org/me",
        body={"avatar_s3_key": "org-assets/pending/sub-1/gone.png"}), None)
    assert res["statusCode"] == 400
    assert "expired" in body_of(res)["error"]


def test_patch_me_explicit_null_clears_avatar(presign_wired):
    wired, fake = presign_wired
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "avatar_s3_key": "org-assets/avatars/sub-1/old.png"})
    wired.setattr(org.users, "clear_avatar",
                  lambda conn, sub: {**CALLER, "avatar_s3_key": None})
    res = org.lambda_handler(make_event("PATCH", "/api/org/me",
                                        body={"avatar_s3_key": None}), None)
    assert res["statusCode"] == 200
    assert body_of(res)["avatar_s3_key"] is None
    assert "org-assets/avatars/sub-1/old.png" in fake.deleted
    assert fake.copied == []


def test_create_site_relocates_pending_icon(presign_wired):
    wired, fake = presign_wired
    wired.setattr(org.sites, "create_site",
                  lambda conn, cid, name, **kw: {"id": "s-new", "name": name})
    seticon = {}
    wired.setattr(org.sites, "set_site_icon",
                  lambda conn, sid, key: (seticon.update(sid=sid, key=key)
                                          or {"id": sid, "icon_s3_key": key}))
    pending = "org-assets/pending/sub-1/ic.png"
    res = org.lambda_handler(make_event("POST", "/api/org/sites",
        body={"name": "New", "icon_s3_key": pending}), None)
    assert res["statusCode"] == 201
    assert fake.copied == [(pending, "org-assets/site-icons/s-new/ic.png")]
    assert seticon == {"sid": "s-new", "key": "org-assets/site-icons/s-new/ic.png"}
    assert pending in fake.deleted
```

注意:`test_patch_me_relocates...` 与 `test_patch_me_explicit_null...` 需要 `presign_wired`(FakeS3)同时用 `wired` 的 caller override —— `presign_wired` 本身基于 `wired`,直接用其返回的 wired 对象 setattr 即可(如上)。

- [ ] **Step 2: 跑,确认新测试失败**

Run: `python -m pytest tests/unit/test_lambda_org_api.py -v -k "relocate or expired or clears"`(FAIL)。

- [ ] **Step 3: 实现**

文件顶部 import 区(`import boto3` 之后)加:

```python
from botocore.exceptions import ClientError
```

`ALLOWED_IMAGE_TYPES = {...}` 之后加助手:

```python
def _relocate_asset(pending_key, final_key):
    """Copy a committed upload from pending to its permanent key and delete
    the pending object. Returns False when the pending object no longer
    exists (lifecycle-expired or bogus key) — callers turn that into a 400.
    S3 calls go through the S3 gateway endpoint (in-VPC, no NAT)."""
    try:
        s3().copy_object(Bucket=S3_BUCKET,
                         CopySource={"Bucket": S3_BUCKET, "Key": pending_key},
                         Key=final_key)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return False
        raise
    s3().delete_object(Bucket=S3_BUCKET, Key=pending_key)
    return True


def _delete_asset(key):
    if key and key.startswith(ORG_ASSETS_PREFIX):
        s3().delete_object(Bucket=S3_BUCKET, Key=key)
```

`patch_me` 整函数替换为:

```python
def patch_me(conn, caller, body):
    if body is None:
        return error("malformed JSON body", 400)
    old_avatar = caller.get("avatar_s3_key")
    avatar = body.get("avatar_s3_key")
    clear = "avatar_s3_key" in body and avatar is None
    final_avatar = None
    if avatar is not None:
        pending_prefix = f"{ORG_ASSETS_PREFIX}pending/{caller['cognito_sub']}/"
        if not isinstance(avatar, str) or not avatar.startswith(pending_prefix):
            return error(f"avatar_s3_key must be your pending upload ({pending_prefix}…)", 400)
        fname = avatar.rsplit("/", 1)[-1]
        final_avatar = f"{ORG_ASSETS_PREFIX}avatars/{caller['cognito_sub']}/{fname}"
        # Relocate BEFORE the DB write. A DB failure after this leaves at most
        # one unreferenced object in avatars/ (rare; retry re-uploads) — same
        # pragmatic tradeoff as create_member's Cognito orphan.
        if not _relocate_asset(avatar, final_avatar):
            return error("upload expired or missing — please re-upload the image", 400)
    row = users.update_profile(
        conn, caller["cognito_sub"],
        first_name=body.get("first_name"),
        last_name=body.get("last_name"),
        avatar_s3_key=final_avatar,
    )
    if row is None:
        return error("user not found", 404)
    if clear:
        row = users.clear_avatar(conn, caller["cognito_sub"])
        if old_avatar:
            _delete_asset(old_avatar)
    elif final_avatar and old_avatar and old_avatar != final_avatar:
        _delete_asset(old_avatar)
    return ok(row)
```

`create_org_site` 里,从 `icon = body.get("icon_s3_key")` 到 `return ok(row, 201)` 那段替换为:

```python
    icon = body.get("icon_s3_key")
    if icon is not None:
        pending_prefix = f"{ORG_ASSETS_PREFIX}pending/{caller['cognito_sub']}/"
        if not isinstance(icon, str) or not icon.startswith(pending_prefix):
            return error(f"icon_s3_key must be your pending upload ({pending_prefix}…)", 400)
    row = sites.create_site(
        conn, caller["company_id"], name,
        location=body.get("location"), client=body.get("client"),
        industry=body.get("industry"), icon_s3_key=None,
    )
    if icon is not None:
        fname = icon.rsplit("/", 1)[-1]
        final_icon = f"{ORG_ASSETS_PREFIX}site-icons/{row['id']}/{fname}"
        if not _relocate_asset(icon, final_icon):
            return error("upload expired or missing — please re-upload the image", 400)
        row = sites.set_site_icon(conn, row["id"], final_icon)
    return ok(row, 201)
```

(注:图标搬迁失败返回 400 时,`with conn:` 因正常 return 会提交已建的 site 行——**可接受**:站点已建、图标丢了,重传即可;比回滚整个建站更符合直觉。此语义写进 docstring 不需要,注释即可。)

- [ ] **Step 4: 跑测试**

Run: `python -m pytest tests/unit -v`(全 PASS)。

- [ ] **Step 5: 提交**

```bash
git add src/lambda_org_api.py tests/unit/test_lambda_org_api.py
git commit -m "feat(3b): commit-relocate pending uploads, delete-old, expired->400, explicit avatar clear"
```

---

### Task 7: PATCH /api/org/sites/{id} — 站点信息与图标更新

**Files:**
- Modify: `src/lambda_org_api.py`
- Test: `tests/unit/test_lambda_org_api.py`(追加)

**Interfaces:**
- Consumes: Task 3 的 `sites.update_site`、`sites.set_site_icon`;Task 6 的 `_relocate_asset`/`_delete_asset`;现有守卫风格。
- Produces:`PATCH /api/org/sites/{id}`(admin/gm,公司守卫,归档站点 404):body `{name?, location?, client?, industry?, icon_s3_key?(pending)}`;换图标搬迁 + 删旧图标。

- [ ] **Step 1: 写失败的测试**(追加)

```python
def test_patch_site_updates_fields(wired):
    seen = {}
    wired.setattr(org.sites, "update_site",
                  lambda conn, sid, cid, **kw: (seen.update(sid=sid, cid=cid, **kw)
                                                or {"id": sid, "name": kw.get("name") or "Old",
                                                    "icon_s3_key": None}))
    res = org.lambda_handler(make_event("PATCH", "/api/org/sites/s-1",
                                        body={"name": "Renamed", "location": "Akl"}), None)
    assert res["statusCode"] == 200
    assert seen["sid"] == "s-1" and seen["cid"] == "c-uuid-1"
    assert seen["name"] == "Renamed" and seen["location"] == "Akl"


def test_patch_site_worker_403_and_missing_404(wired):
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "worker"})
    assert org.lambda_handler(make_event("PATCH", "/api/org/sites/s-1",
                                         body={"name": "X"}), None)["statusCode"] == 403
    wired.setattr(org.users, "get_user_by_sub", lambda conn, sub: dict(CALLER))
    wired.setattr(org.sites, "update_site", lambda conn, sid, cid, **kw: None)
    assert org.lambda_handler(make_event("PATCH", "/api/org/sites/s-9",
                                         body={"name": "X"}), None)["statusCode"] == 404


def test_patch_site_swaps_icon_and_deletes_old(presign_wired):
    wired, fake = presign_wired
    wired.setattr(org.sites, "update_site",
                  lambda conn, sid, cid, **kw: {"id": sid, "name": "S",
                                                "icon_s3_key": "org-assets/site-icons/s-1/old.png"})
    wired.setattr(org.sites, "set_site_icon",
                  lambda conn, sid, key: {"id": sid, "icon_s3_key": key})
    pending = "org-assets/pending/sub-1/new.png"
    res = org.lambda_handler(make_event("PATCH", "/api/org/sites/s-1",
                                        body={"icon_s3_key": pending}), None)
    assert res["statusCode"] == 200
    assert fake.copied == [(pending, "org-assets/site-icons/s-1/new.png")]
    assert pending in fake.deleted and "org-assets/site-icons/s-1/old.png" in fake.deleted
```

- [ ] **Step 2: 跑,确认失败**

Run: `python -m pytest tests/unit/test_lambda_org_api.py -v -k patch_site`(FAIL,404——路由不存在)。

- [ ] **Step 3: 实现**

`dispatch()` 里,`/sites` 的 exact-match 块之后(`m_sa` 归档匹配之前)插入——注意 `[^/]+$` 不会误吃 `/sites/{id}/archive`:

```python
    m_sp = re.match(r"^/sites/([^/]+)$", route)
    if m_sp and method == "PATCH":
        return patch_org_site(conn, caller, m_sp.group(1), parse_body(event))
```

`create_org_site` 之后追加:

```python
def patch_org_site(conn, caller, site_id, body):
    if caller["global_role"] not in ("admin", "gm"):
        return error("admin or gm role required", 403)
    if body is None:
        return error("malformed JSON body", 400)
    name = body.get("name")
    if name is not None:
        if not isinstance(name, str) or not name.strip():
            return error("name must be a non-empty string", 400)
        name = name.strip()
    icon = body.get("icon_s3_key")
    if icon is not None:
        pending_prefix = f"{ORG_ASSETS_PREFIX}pending/{caller['cognito_sub']}/"
        if not isinstance(icon, str) or not icon.startswith(pending_prefix):
            return error(f"icon_s3_key must be your pending upload ({pending_prefix}…)", 400)
    row = sites.update_site(
        conn, site_id, caller["company_id"],
        name=name, location=body.get("location"),
        client=body.get("client"), industry=body.get("industry"),
    )
    if row is None:
        return error("site not found in your company", 404)
    if icon is not None:
        old_icon = row.get("icon_s3_key")
        fname = icon.rsplit("/", 1)[-1]
        final_icon = f"{ORG_ASSETS_PREFIX}site-icons/{site_id}/{fname}"
        if not _relocate_asset(icon, final_icon):
            return error("upload expired or missing — please re-upload the image", 400)
        row = sites.set_site_icon(conn, site_id, final_icon)
        if old_icon and old_icon != final_icon:
            _delete_asset(old_icon)
    return ok(row)
```

同时把文件头 docstring 的 Routes 清单补上三行(单行 Edit,加在 asset-url 行后):

```
  PATCH /api/org/sites/{id}               → update site fields / swap icon (admin/gm)
  POST  /api/org/sites/{id}/(un)archive   → soft-delete / restore site (admin/gm)
  POST  /api/org/members/{sub}/(un)archive→ soft-delete / restore member (admin/gm, never self)
```

- [ ] **Step 4: 跑测试**

Run: `python -m pytest tests/unit -v`(全 PASS)。

- [ ] **Step 5: 提交**

```bash
git add src/lambda_org_api.py tests/unit/test_lambda_org_api.py
git commit -m "feat(3b): PATCH /sites/{id} — field updates + icon swap with old-icon cleanup"
```

---

### Task 8: IAM(DeleteObject)+ pending lifecycle 下发

**Files:**
- Modify: `src/template.yaml`(OrgApiFunction 的 S3 policy)
- Create: `scripts/wire-bucket-lifecycle.sh`
- Modify: `.github/workflows/deploy.yml`

**Interfaces:**
- Consumes: Task 6/7 的 delete/copy 调用;现有 OrgApiFunction IAM(`s3:PutObject`/`s3:GetObject` on `org-assets/*` 已覆盖 CopyObject)。
- Produces:OrgApiFunction 多 `s3:DeleteObject`(限 `org-assets/*`);`org-assets/pending/` 1 天过期 lifecycle;deploy.yml 幂等下发。

- [ ] **Step 1: template 加 DeleteObject**

`src/template.yaml` OrgApiFunction 的 S3 语句,在 `                - s3:GetObject` 行后加一行:

```yaml
                - s3:DeleteObject
```

(Resource 行不变,仍是 `org-assets/*`。)

- [ ] **Step 2: 建 lifecycle 脚本**

新建 `scripts/wire-bucket-lifecycle.sh`:

```bash
#!/usr/bin/env bash
# wire-bucket-lifecycle.sh BUCKET [REGION]
# Expire abandoned presigned uploads under org-assets/pending/ after 1 day
# (committed assets are relocated out of pending on save, so anything left is
# an abandoned upload). put-bucket-lifecycle-configuration REPLACES the whole
# config — abort if the bucket already has OTHER rules so we never clobber them.
set -euo pipefail
BUCKET="${1:?usage: wire-bucket-lifecycle.sh BUCKET [REGION]}"
REGION="${2:-ap-southeast-2}"

EXISTING="$(aws s3api get-bucket-lifecycle-configuration --bucket "$BUCKET" \
  --region "$REGION" --query 'Rules[?ID!=`org-assets-pending-expiry`].ID' \
  --output text 2>/dev/null || true)"
if [ -n "$EXISTING" ]; then
  echo "ERROR: bucket $BUCKET has other lifecycle rules ($EXISTING); refusing to replace. Merge manually." >&2
  exit 1
fi

aws s3api put-bucket-lifecycle-configuration --bucket "$BUCKET" --region "$REGION" \
  --lifecycle-configuration '{
    "Rules": [
      {
        "ID": "org-assets-pending-expiry",
        "Status": "Enabled",
        "Filter": { "Prefix": "org-assets/pending/" },
        "Expiration": { "Days": 1 }
      }
    ]
  }'
echo "Lifecycle applied to s3://$BUCKET"
aws s3api get-bucket-lifecycle-configuration --bucket "$BUCKET" --region "$REGION"
```

- [ ] **Step 3: deploy.yml 加步骤**

在 `Wire bucket CORS` 步骤之后加:

```yaml
      - name: Wire bucket lifecycle (TEST bucket, expire abandoned pending uploads)
        run: bash scripts/wire-bucket-lifecycle.sh fieldsight-data-test-509194952652 ${{ env.AWS_REGION }}
```

- [ ] **Step 4: 校验**

Run: `cfn-lint src/template.yaml infra/db-template.yaml`(exit 0);`bash -n scripts/wire-bucket-lifecycle.sh`(无输出);`python -m pytest tests/unit -q`(全 PASS)。

- [ ] **Step 5: 提交**

```bash
git add src/template.yaml scripts/wire-bucket-lifecycle.sh .github/workflows/deploy.yml
git commit -m "feat(3b): org-api s3:DeleteObject + pending-uploads 1-day lifecycle"
```

**部署前置(权限门,执行阶段给用户 `!` 自跑)**:deploy role 需 lifecycle 权限,否则 CI 该步失败:
```bash
aws iam put-role-policy --role-name github-actions-fieldsight-deploy \
  --policy-name test-bucket-lifecycle --policy-document '{"Version":"2012-10-17","Statement":[{"Sid":"TestBucketLifecycle","Effect":"Allow","Action":["s3:PutLifecycleConfiguration","s3:GetLifecycleConfiguration"],"Resource":"arn:aws:s3:::fieldsight-data-test-509194952652"}]}'
```

---

### Task 9: PR、部署、迁移、归档+上传冒烟

**Files:** 无代码;`.superpowers/sdd/progress.md` 记账。

**Interfaces:** Consumes 全部上述;Produces 线上验证 + 台账。

- [ ] **Step 1: 预检**

```bash
python -m pytest tests/unit -v            # 全绿
cfn-lint src/template.yaml infra/db-template.yaml
git log --oneline origin/develop..HEAD    # 只有本批提交
git status --short                         # 只有预期文件;用户未跟踪笔记不动
```

- [ ] **Step 2: 推分支 + 开 PR 到 develop**

```bash
git push -u origin feature/phase3b-backend-datamodel
gh pr create --base develop --title "Phase 3b: org data-model — archive + upload lifecycle" --body "…"
```
等 CI 绿(test.yml 跑 0005 迁移 + 全部集成测试;ci.yml lint)。红则修复循环。

- [ ] **Step 3: 用户操作**:lifecycle IAM 授权(上面命令 `!` 自跑)+ 合并 PR(权限门)。合并后 `gh run watch` 盯 deploy.yml。

- [ ] **Step 4: 迁移 0005 上库**

```bash
export AWS_CLI_FILE_ENCODING=UTF-8 PYTHONUTF8=1
aws lambda invoke --function-name fieldsight-test-migrate --payload '{}' \
  --cli-binary-format raw-in-base64-out /dev/stdout --region ap-southeast-2
```
Expected: `{"applied": ["0005_archive.sql"]}`(二跑 `[]`)。Data API 核对三表有 `archived_at`。

- [ ] **Step 5: 冒烟**(合成 admin claims 直接 invoke,payload 用 `file://$(cygpath -m …)`)

- 归档链:`POST /sites/{真实id}/archive` → 200 archived_at 非空;`GET /sites` 该站消失;`GET /sites?include_archived=1` 又出现;`/unarchive` 恢复。
- 上传链:`POST /upload-url {kind:avatar}` → key 在 `org-assets/pending/{sub}/`;PUT 测试图;`PATCH /me {avatar_s3_key:<pending>}` → 200 且返回 key 在 `avatars/`;`aws s3 ls` 确认 pending 删、avatars 有;`PATCH /me {"avatar_s3_key": null}` → 200 avatar 清空且 S3 对象删除。
- `PATCH /sites/{id} {name:"..."}` → 200 改名生效。
- lifecycle:`aws s3api get-bucket-lifecycle-configuration --bucket fieldsight-data-test-509194952652` 含 `org-assets-pending-expiry`。

- [ ] **Step 6: 记账**

`.superpowers/sdd/progress.md` 追加批次1完成行。批次2(UI)另起 writing-plans。

---

## 自审(已完成,含 Fable 复审并入项)

- Spec §4+§8b 覆盖:0005(T1)、list 过滤+发现(T2)、archive/复活/clear/update_site 仓储(T3)、端点+caller 语义+409(T4)、pending 签发+拒读(T5)、搬迁/删旧/过期/清除(T6)、PATCH sites(T7)、IAM+lifecycle(T8)、部署冒烟(T9)——§8b 的 1-10 全部落位。
- 签名一致:`list_company_users/list_company_sites(conn, cid, include_archived=False)` T2 定义、T4 调用一致;`update_site(conn, site_id, company_id, name/location/client/industry)` T3/T7 一致;`_relocate_asset -> bool` T6 定义、T6/T7 消费一致;`clear_avatar` T3/T6 一致;pending 前缀 T5 产出、T6/T7 消费一致。
- 已知取舍(注释在案):relocate 在 DB 写前(至多 1 个 avatars/ 孤儿,重传自愈);图标搬迁失败时 site 行保留(400 提示重传);unarchive 不级联(ensure_membership 复活弥补);seed 重跑复活归档 membership(并入已记录 quirk);`get_user_by_sub`/`get_company_site_by_name` 不过滤 archived(自读/幂等)。
