# Phase 3 收尾 · 批次1:后端数据模型完善 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 org 数据模型加归档软删除 + 上传资产生命周期治理(pending 前缀 + 提交搬迁 + 替换删旧 + lifecycle 兜底),消除频繁更新的孤儿并支持项目/人员归档。

**Architecture:** 新迁移 `0005_archive.sql` 给 `sites/users/memberships` 加 `archived_at`;仓储 list 默认过滤已归档 + 新增 archive/unarchive(级联 memberships);`lambda_org_api` 加归档端点 + 把上传改成「先签到 `org-assets/pending/`,提交时 CopyObject 到正式 key + 删 pending + 删旧」;template 加 `s3:DeleteObject` + 新脚本下发 pending 的 1 天过期 lifecycle。

**Tech Stack:** Python 3.11 Lambda,psycopg3(无 ORM),SAM(src/template.yaml),pytest(unit 本地 / integration 走 CI pgvector 容器),boto3 S3。

**Spec:** `docs/superpowers/specs/2026-07-04-phase-3-completion-org-datamodel-ui.md` §4。本批**不含 UI**(批次2另写)。

## Global Constraints

- 账号 `509194952652`/ap-southeast-2;prod 手工资源不碰;只部署 `fieldsight-test` 栈(develop→CI),绝不 `sam deploy` 碰 prod。
- Windows autocrlf=true 混合行尾:用**单行 Edit anchor**;**绝不** `git add -A`/`git add .`,只 add 明确路径。未跟踪的本地文件(`assets/`、`benchmark/`、`claw-code/`、`scripts/aws-*.sh`、`src/requirements.txt`、用户 roadmap 笔记)绝不 stage/删。
- Lambda runtime=python3.11;CI Python=3.11。本地 python 3.14 装了 psycopg,unit + handler 测试本地可跑;integration 测试需 `TEST_DATABASE_URL`(本地无则 skip,CI pgvector 容器跑)。
- **仓储永不 commit**——caller 拥有事务(`with get_connection() as conn:` 干净退出提交、异常回滚、块尾关闭)。同一 repo 函数内多条 SQL 属同一事务。
- ACL deny-by-default;角色 `admin|gm|pm|site_manager|worker`;写路径公司守卫(WHERE ... AND company_id=%s)。
- 迁移是纯 `.sql`,命名 `NNNN_name.sql`,`db/migrate.py` 按数字排序、每个文件在一个事务内 apply(多语句 OK,无参数走 simple query protocol)。
- `{{resolve:secretsmanager:...}}` 只与 Parameter 组合不与 ImportValue;cfn-lint E1051 连注释里的 resolve 字面量都抓——**别在 YAML 注释写 resolve 字面量**。
- S3 CopyObject 的 IAM 由 `s3:GetObject`(源)+`s3:PutObject`(目标)授权,**不是**独立 action;只有 `s3:DeleteObject` 是新增。
- 跑测试:`python -m pytest tests/unit -v`(integration 无 TEST_DATABASE_URL 自动 skip);`cfn-lint src/template.yaml infra/db-template.yaml` 保持 exit 0。
- 提交:conventional(`feat(3b):`/`fix(3b):`),每个绿色 TDD 循环一提交。cfn-lint 二进制可能在 `C:/Users/camil/AppData/Local/Python/pythoncore-3.14-64/Scripts/cfn-lint.exe`。

---

### Task 1: 迁移 0005 — archived_at 软删列

**Files:**
- Create: `src/migrations/0005_archive.sql`
- Test: `tests/integration/test_archive.py`(新建)

**Interfaces:**
- Consumes: `db.migrate.apply_migrations`(已有),`tests/conftest.py` 的 `migrated_db_url`/`db` fixture。
- Produces: `sites`/`users`/`memberships` 各有 `archived_at timestamptz`(可空,默认 NULL)。后续 Task 2-4 依赖此列。

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

- [ ] **Step 2: 跑,确认因缺列失败(或本地 skip)**

Run: `python -m pytest tests/integration/test_archive.py -v`
Expected 本地:SKIPPED(无 TEST_DATABASE_URL)。用 `python -m pytest tests/ --collect-only -q` 确认无 collection error。CI 里真跑会因 `archived_at` 不存在而 FAIL。

- [ ] **Step 3: 写迁移**

新建 `src/migrations/0005_archive.sql`:

```sql
-- Phase 3 batch 1: soft-delete (archive) support. NULL = active.
ALTER TABLE sites       ADD COLUMN archived_at timestamptz;
ALTER TABLE users       ADD COLUMN archived_at timestamptz;
ALTER TABLE memberships ADD COLUMN archived_at timestamptz;
```

- [ ] **Step 4: 确认 collection 干净 + unit 绿**

Run: `python -m pytest tests/ --collect-only -q`(无错)然后 `python -m pytest tests/unit -v`(全 PASS,未变)。

- [ ] **Step 5: 提交**

```bash
git add src/migrations/0005_archive.sql tests/integration/test_archive.py
git commit -m "feat(3b): migration 0005 — archived_at soft-delete columns"
```

---

### Task 2: 仓储 list 过滤已归档 + archived_at 进 _COLS

**Files:**
- Modify: `src/repositories/users.py`、`src/repositories/sites.py`、`src/repositories/memberships.py`
- Test: `tests/integration/test_archive.py`(追加)

**Interfaces:**
- Consumes: Task 1 的 `archived_at` 列;现有 `_COLS`、`upsert_user`、`create_site`、`create_company`、`add_membership`/`ensure_membership`。
- Produces(供 Task 3/4 与已有调用方):
  - `users._COLS` / `sites._COLS` 末尾含 `archived_at`(所有返回行多一字段,无害)。
  - `users.list_company_users`、`sites.list_company_sites`、`sites.list_sites_by_ids`、`memberships.list_company_memberships`、`memberships.accessible_site_ids` 均只返回**未归档**行。
  - `sites.get_company_site_by_name`、`sites.get_site`、`users.get_user_by_sub` **不过滤**(seed 幂等 / caller 自读 archived 仍可)。

- [ ] **Step 1: 写失败的集成测试**(追加到 `tests/integration/test_archive.py`)

```python
def test_lists_hide_archived_rows(db):
    co = companies.create_company(db, "ArchCo")
    s_live = sites.create_site(db, co["id"], "Live Site")
    s_arch = sites.create_site(db, co["id"], "Arch Site")
    u_live = users.upsert_user(db, "sub-al", "al@x.nz", company_id=co["id"])
    u_arch = users.upsert_user(db, "sub-aa", "aa@x.nz", company_id=co["id"])
    memberships.ensure_membership(db, u_live["id"], s_live["id"], "worker")
    # archive one site, one user, one membership directly via SQL (repo archive fns come in Task 3)
    db.execute("UPDATE sites SET archived_at=now() WHERE id=%s", (s_arch["id"],))
    db.execute("UPDATE users SET archived_at=now() WHERE id=%s", (u_arch["id"],))

    site_names = [r["name"] for r in sites.list_company_sites(db, co["id"])]
    assert site_names == ["Live Site"]
    assert [s["name"] for s in sites.list_sites_by_ids(db, [s_live["id"], s_arch["id"]])] == ["Live Site"]
    assert [u["cognito_sub"] for u in users.list_company_users(db, co["id"])] == ["sub-al"]
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
    # admin ALL-scope excludes archived sites
    adm = users.upsert_user(db, "sub-adm", "adm@x.nz", company_id=co["id"], global_role="admin")
    db.execute("UPDATE sites SET archived_at=now() WHERE id=%s", (s2["id"],))
    assert set(memberships.accessible_site_ids(db, adm["id"], "admin")) == {s1["id"]}
```

- [ ] **Step 2: 跑,确认 collection 干净(本地 skip)**

Run: `python -m pytest tests/ --collect-only -q`(无错)。CI 会因过滤未实现而 FAIL。

- [ ] **Step 3: 改仓储**

`src/repositories/users.py` — `_COLS` 末尾加 `archived_at`,`list_company_users` 加过滤:

```python
_COLS = "id, cognito_sub, company_id, email, first_name, last_name, avatar_s3_key, global_role, created_at, archived_at"
```

`list_company_users` 的 SQL 改为(单行 anchor:`"SELECT {_COLS} FROM users WHERE company_id=%s ORDER BY created_at"` 那行):

```python
        f"SELECT {_COLS} FROM users WHERE company_id=%s AND archived_at IS NULL ORDER BY created_at",
```

`src/repositories/sites.py` — `_COLS` 末尾加 `archived_at`:

```python
_COLS = "id, company_id, name, location, client, industry, icon_s3_key, created_at, archived_at"
```

`list_company_sites` 改为:

```python
        f"SELECT {_COLS} FROM sites WHERE company_id=%s AND archived_at IS NULL ORDER BY created_at", (company_id,)
```

`list_sites_by_ids` 改为(`WHERE id = ANY(%s)` 那行):

```python
        f"SELECT {_COLS} FROM sites WHERE id = ANY(%s) AND archived_at IS NULL ORDER BY created_at",
```

`src/repositories/memberships.py` — `list_company_memberships` 的 `WHERE s.company_id = %s AND u.company_id = s.company_id ` 那行改为:

```python
        "WHERE s.company_id = %s AND u.company_id = s.company_id AND m.archived_at IS NULL "
```

`accessible_site_ids` 的两个分支:ALL 分支 `WHERE u.id = %s` 改为 `WHERE u.id = %s AND s.archived_at IS NULL`;MEMBERSHIPS 分支 `"SELECT site_id FROM memberships WHERE user_id=%s"` 改为 `"SELECT site_id FROM memberships WHERE user_id=%s AND archived_at IS NULL"`。

- [ ] **Step 4: 确认 collection + unit 绿**

Run: `python -m pytest tests/ --collect-only -q` 然后 `python -m pytest tests/unit -v`(全 PASS)。

- [ ] **Step 5: 提交**

```bash
git add src/repositories/users.py src/repositories/sites.py src/repositories/memberships.py tests/integration/test_archive.py
git commit -m "feat(3b): repo lists filter archived_at; archived_at in _COLS"
```

---

### Task 3: 仓储 archive/unarchive 函数(级联 memberships)

**Files:**
- Modify: `src/repositories/sites.py`、`src/repositories/users.py`(+ `memberships.py` 的 `__all__` 不涉及)
- Test: `tests/integration/test_archive.py`(追加)

**Interfaces:**
- Consumes: Task 1/2 的列与 `_COLS`。
- Produces(供 Task 4):
  - `sites.archive_site(conn, site_id, company_id) -> dict | None`(公司守卫;set archived_at=now();级联把该 site 的 memberships 归档;已归档/越权/不存在→None)
  - `sites.unarchive_site(conn, site_id, company_id) -> dict | None`(只恢复 site 行,不级联恢复 memberships)
  - `sites.set_site_icon(conn, site_id, icon_s3_key) -> dict`(供 Task 6 图标搬迁)
  - `users.archive_user(conn, cognito_sub, company_id) -> dict | None`(级联该用户 memberships 归档)
  - `users.unarchive_user(conn, cognito_sub, company_id) -> dict | None`(不级联)

- [ ] **Step 1: 写失败的集成测试**(追加)

```python
def test_archive_site_cascades_memberships(db):
    co = companies.create_company(db, "CascadeCo")
    s = sites.create_site(db, co["id"], "Casc Site")
    u = users.upsert_user(db, "sub-cs", "cs@x.nz", company_id=co["id"])
    memberships.ensure_membership(db, u["id"], s["id"], "worker")
    row = sites.archive_site(db, s["id"], co["id"])
    assert row is not None and row["archived_at"] is not None
    # site hidden + its membership archived
    assert sites.list_company_sites(db, co["id"]) == []
    assert memberships.accessible_site_ids(db, u["id"], "worker") == []
    # cross-company guard + double-archive -> None
    other = companies.create_company(db, "OtherCo")
    assert sites.archive_site(db, s["id"], other["id"]) is None
    assert sites.archive_site(db, s["id"], co["id"]) is None  # already archived
    # unarchive restores the site row (not memberships)
    assert sites.unarchive_site(db, s["id"], co["id"])["archived_at"] is None
    assert [x["name"] for x in sites.list_company_sites(db, co["id"])] == ["Casc Site"]
    assert memberships.accessible_site_ids(db, u["id"], "worker") == []  # membership stays archived


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


def test_set_site_icon(db):
    co = companies.create_company(db, "IconCo")
    s = sites.create_site(db, co["id"], "Icon Site")
    row = sites.set_site_icon(db, s["id"], "org-assets/site-icons/" + s["id"] + "/x.png")
    assert row["icon_s3_key"].endswith("x.png")
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
    """Restore a site row only (memberships are NOT auto-restored)."""
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
```

- [ ] **Step 4: 确认 collection + unit 绿**

Run: `python -m pytest tests/ --collect-only -q` 然后 `python -m pytest tests/unit -v`。

- [ ] **Step 5: 提交**

```bash
git add src/repositories/sites.py src/repositories/users.py tests/integration/test_archive.py
git commit -m "feat(3b): repo archive/unarchive (cascade memberships) + set_site_icon"
```

---

### Task 4: org-api 归档端点

**Files:**
- Modify: `src/lambda_org_api.py`
- Test: `tests/unit/test_lambda_org_api.py`(追加)

**Interfaces:**
- Consumes: Task 3 的 `sites.archive_site/unarchive_site`、`users.archive_user/unarchive_user`;现有 `resolve_scope`、`ok`/`error`、`dispatch`。
- Produces:
  - `POST /api/org/sites/{id}/archive` · `/unarchive`(admin/gm)→ 更新后的 site 行 / 404
  - `POST /api/org/members/{sub}/archive` · `/unarchive`(admin/gm)→ 更新后的 user 行 / 404

- [ ] **Step 1: 写失败的单测**(追加到 `tests/unit/test_lambda_org_api.py`,复用其 `make_event`/`FakeConn`/`CALLER`/`wired`/`body_of`)

```python
def test_archive_site_admin_ok(wired):
    seen = {}
    wired.setattr(org.sites, "archive_site",
                  lambda conn, sid, cid: (seen.update(sid=sid, cid=cid)
                                          or {"id": sid, "archived_at": "2026-07-04"}))
    res = org.lambda_handler(make_event("POST", "/api/org/sites/s-1/archive"), None)
    assert res["statusCode"] == 200
    assert seen == {"sid": "s-1", "cid": "c-uuid-1"}


def test_unarchive_site_routes_to_unarchive(wired):
    wired.setattr(org.sites, "unarchive_site",
                  lambda conn, sid, cid: {"id": sid, "archived_at": None})
    res = org.lambda_handler(make_event("POST", "/api/org/sites/s-1/unarchive"), None)
    assert res["statusCode"] == 200
    assert body_of(res)["archived_at"] is None


def test_archive_site_worker_403(wired):
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "worker"})
    res = org.lambda_handler(make_event("POST", "/api/org/sites/s-1/archive"), None)
    assert res["statusCode"] == 403


def test_archive_site_not_found_404(wired):
    wired.setattr(org.sites, "archive_site", lambda conn, sid, cid: None)
    res = org.lambda_handler(make_event("POST", "/api/org/sites/s-9/archive"), None)
    assert res["statusCode"] == 404


def test_archive_member_admin_ok(wired):
    seen = {}
    wired.setattr(org.users, "archive_user",
                  lambda conn, sub, cid: (seen.update(sub=sub, cid=cid)
                                          or {"cognito_sub": sub, "archived_at": "x"}))
    res = org.lambda_handler(make_event("POST", "/api/org/members/sub-2/archive"), None)
    assert res["statusCode"] == 200
    assert seen == {"sub": "sub-2", "cid": "c-uuid-1"}


def test_archive_member_gm_ok_worker_403(wired):
    # gm (ALL scope) allowed
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "gm"} if sub == "sub-1" else None)
    wired.setattr(org.users, "archive_user", lambda conn, sub, cid: {"cognito_sub": sub})
    assert org.lambda_handler(make_event("POST", "/api/org/members/sub-2/archive"), None)["statusCode"] == 200
```

- [ ] **Step 2: 跑,确认失败**

Run: `python -m pytest tests/unit/test_lambda_org_api.py -v -k "archive"`
Expected: FAIL（404，路由未加）。

- [ ] **Step 3: 实现**

在 `dispatch()` 里、`m = re.match(r"^/members/([^/]+)/role$", route)` 块之后、`if route == "/upload-url"...` 之前,插入:

```python
    m_sa = re.match(r"^/sites/([^/]+)/(archive|unarchive)$", route)
    if m_sa and method == "POST":
        return archive_site_endpoint(conn, caller, m_sa.group(1), m_sa.group(2))
    m_ma = re.match(r"^/members/([^/]+)/(archive|unarchive)$", route)
    if m_ma and method == "POST":
        return archive_member_endpoint(conn, caller, m_ma.group(1), m_ma.group(2))
```

在 `create_member` 之后、assets 小节之前,追加:

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
    fn = users.archive_user if action == "archive" else users.unarchive_user
    row = fn(conn, target_sub, caller["company_id"])
    if row is None:
        return error("member not found in your company", 404)
    return ok(row)
```

- [ ] **Step 4: 跑测试**

Run: `python -m pytest tests/unit -v`
Expected: 全 PASS。

- [ ] **Step 5: 提交**

```bash
git add src/lambda_org_api.py tests/unit/test_lambda_org_api.py
git commit -m "feat(3b): org api archive/unarchive endpoints for sites + members"
```

---

### Task 5: upload-url 改签 pending 前缀

**Files:**
- Modify: `src/lambda_org_api.py`(`create_upload_url`)
- Test: `tests/unit/test_lambda_org_api.py`(改现有 upload 测试)

**Interfaces:**
- Consumes: 现有 `create_upload_url`、`ALLOWED_IMAGE_TYPES`、`s3()`。
- Produces:`POST /upload-url` 现在签发 key 到 `org-assets/pending/{caller_sub}/{uuid}.{ext}`(avatar 与 site_icon 同前缀,kind 只决定 role 检查)。Task 6 的提交搬迁依赖此前缀。

- [ ] **Step 1: 改现有测试暴露新行为**

在 `tests/unit/test_lambda_org_api.py` 里,把 `test_upload_url_avatar` 的断言 `assert b["key"].startswith("org-assets/avatars/sub-1/")` 改为:

```python
    assert b["key"].startswith("org-assets/pending/sub-1/")
```

把 `test_upload_url_site_icon_admin_gets_owner_scoped_key` 的断言 `assert body_of(res)["key"].startswith("org-assets/site-icons/sub-1/")` 改为:

```python
    assert body_of(res)["key"].startswith("org-assets/pending/sub-1/")
```

(`test_upload_url_site_icon_worker_403` 与 `test_upload_url_rejects_content_type` 不变。)

- [ ] **Step 2: 跑,确认失败**

Run: `python -m pytest tests/unit/test_lambda_org_api.py -v -k "upload_url"`
Expected: 两个改动的用例 FAIL(现仍是 avatars/site-icons 前缀)。

- [ ] **Step 3: 实现**

在 `create_upload_url` 里,把 avatar / site_icon 两个分支的 key 赋值改为统一 pending 前缀:

```python
    kind = body.get("kind")
    if kind == "avatar":
        pass
    elif kind == "site_icon":
        if caller["global_role"] not in ("admin", "gm"):
            return error("admin or gm role required", 403)
    else:
        return error("kind must be avatar or site_icon", 400)
    # Upload to a pending prefix; patch_me / create_org_site relocate it to
    # the permanent key on commit (and sweep pending via S3 lifecycle).
    key = f"{ORG_ASSETS_PREFIX}pending/{caller['cognito_sub']}/{uuid.uuid4().hex}.{ext}"
```

(即:删掉原来两个分支里各自的 `key = ...` 行,保留 site_icon 的 role 检查,统一在最后赋 pending key。)

- [ ] **Step 4: 跑测试**

Run: `python -m pytest tests/unit -v`
Expected: 全 PASS。

- [ ] **Step 5: 提交**

```bash
git add src/lambda_org_api.py tests/unit/test_lambda_org_api.py
git commit -m "feat(3b): upload-url signs to org-assets/pending/ (relocated on commit)"
```

---

### Task 6: 提交搬迁 + 删旧(patch_me / create_org_site)

**Files:**
- Modify: `src/lambda_org_api.py`(`patch_me`、`create_org_site`,+ 两个 S3 助手)
- Test: `tests/unit/test_lambda_org_api.py`(改现有 + 加新)

**Interfaces:**
- Consumes: Task 5 的 pending key 约定;`s3()`(copy_object/delete_object);`sites.set_site_icon`(Task 3);现有 `users.update_profile`、`sites.create_site`。
- Produces:
  - `patch_me`:`avatar_s3_key` 必须是**调用者自己的 pending key**;CopyObject→`org-assets/avatars/{sub}/{fname}`、删 pending、删旧头像、存正式 key。
  - `create_org_site`:`icon_s3_key`(可选)必须是调用者 pending key;建 site 后 CopyObject→`org-assets/site-icons/{site_id}/{fname}`、删 pending、`set_site_icon` 存正式 key。
  - 助手 `_relocate_asset(pending_key, final_key)`、`_delete_asset(key)`。

- [ ] **Step 1: 改现有测试 + 写新测试**

现有 `test_patch_me_avatar_must_be_caller_scoped` 断言 400 仍成立(现在校验的是 pending 前缀,`org-assets/avatars/sub-OTHER/x.png` 仍非本人 pending → 400),**不改**。`test_patch_me_rejects_foreign_avatar_key`(`reports/...` → 400)**不改**。`test_patch_me_updates_profile_fields_only`(只发 first_name,无 avatar,不碰 S3)**不改**。

在 `presign_wired` 的 `FakeS3` 类里加 copy/delete 记录(找到 `class FakeS3:` 定义,替换为):

```python
class FakeS3:
    def __init__(self):
        self.copied = []
        self.deleted = []
    def generate_presigned_url(self, op, Params=None, ExpiresIn=0):
        self.last = {"op": op, "params": Params, "expires": ExpiresIn}
        return "https://s3.example/" + Params["Key"]
    def copy_object(self, Bucket=None, CopySource=None, Key=None):
        self.copied.append((CopySource["Key"], Key))
    def delete_object(self, Bucket=None, Key=None):
        self.deleted.append(Key)
```

追加新测试:

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
    # relocated pending -> avatars/, stored final key, deleted pending + old
    assert captured["avatar_s3_key"] == "org-assets/avatars/sub-1/newhex.png"
    assert fake.copied == [(pending, "org-assets/avatars/sub-1/newhex.png")]
    assert pending in fake.deleted and "org-assets/avatars/sub-1/old.png" in fake.deleted


def test_patch_me_rejects_non_pending_avatar(presign_wired):
    res = org.lambda_handler(make_event("PATCH", "/api/org/me",
        body={"avatar_s3_key": "org-assets/avatars/sub-1/x.png"}), None)
    assert res["statusCode"] == 400  # must be a pending key now


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

- [ ] **Step 2: 跑,确认新测试失败**

Run: `python -m pytest tests/unit/test_lambda_org_api.py -v -k "relocate or non_pending"`
Expected: FAIL(搬迁逻辑未实现;`copy_object` 未被调用)。

- [ ] **Step 3: 实现**

在 `ALLOWED_IMAGE_TYPES = {...}` 定义之后(assets 小节顶部)加两个助手:

```python
def _relocate_asset(pending_key, final_key):
    """Copy a committed upload from its pending key to the permanent key and
    delete the pending object. S3 API calls — go through the S3 gateway
    endpoint (in-VPC, no NAT)."""
    s3().copy_object(Bucket=S3_BUCKET,
                     CopySource={"Bucket": S3_BUCKET, "Key": pending_key},
                     Key=final_key)
    s3().delete_object(Bucket=S3_BUCKET, Key=pending_key)


def _delete_asset(key):
    if key and key.startswith(ORG_ASSETS_PREFIX):
        s3().delete_object(Bucket=S3_BUCKET, Key=key)
```

把 `patch_me` 整个函数体替换为:

```python
def patch_me(conn, caller, body):
    if body is None:
        return error("malformed JSON body", 400)
    avatar = body.get("avatar_s3_key")
    final_avatar = None
    if avatar is not None:
        pending_prefix = f"{ORG_ASSETS_PREFIX}pending/{caller['cognito_sub']}/"
        if not isinstance(avatar, str) or not avatar.startswith(pending_prefix):
            return error(f"avatar_s3_key must be your pending upload ({pending_prefix}…)", 400)
        fname = avatar.rsplit("/", 1)[-1]
        final_avatar = f"{ORG_ASSETS_PREFIX}avatars/{caller['cognito_sub']}/{fname}"
        # Relocate BEFORE the DB write. A DB failure after this leaves at most
        # one unreferenced object in avatars/ (rare; retry re-uploads) — the
        # same pragmatic tradeoff as create_member's Cognito orphan.
        _relocate_asset(avatar, final_avatar)
    old_avatar = caller.get("avatar_s3_key")
    row = users.update_profile(
        conn, caller["cognito_sub"],
        first_name=body.get("first_name"),
        last_name=body.get("last_name"),
        avatar_s3_key=final_avatar,
    )
    if row is None:
        return error("user not found", 404)
    if final_avatar and old_avatar and old_avatar != final_avatar:
        _delete_asset(old_avatar)
    return ok(row)
```

在 `create_org_site` 里,把 icon 校验与 create 那段(从 `icon = body.get("icon_s3_key")` 到 `return ok(row, 201)`)替换为:

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
        _relocate_asset(icon, final_icon)
        row = sites.set_site_icon(conn, row["id"], final_icon)
    return ok(row, 201)
```

- [ ] **Step 4: 跑测试**

Run: `python -m pytest tests/unit -v`
Expected: 全 PASS。

- [ ] **Step 5: 提交**

```bash
git add src/lambda_org_api.py tests/unit/test_lambda_org_api.py
git commit -m "feat(3b): commit-relocate pending uploads + delete-old avatar; site icon -> site_id key"
```

---

### Task 7: IAM(DeleteObject)+ pending lifecycle 下发

**Files:**
- Modify: `src/template.yaml`(OrgApiFunction 的 S3 policy)
- Create: `scripts/wire-bucket-lifecycle.sh`
- Modify: `.github/workflows/deploy.yml`(加 lifecycle 步骤)

**Interfaces:**
- Consumes: Task 6 的 delete/copy 调用;现有 OrgApiFunction IAM(`s3:PutObject`/`s3:GetObject` on `org-assets/*`——已覆盖 CopyObject)。
- Produces:OrgApiFunction 多 `s3:DeleteObject`(限 `org-assets/*`);`org-assets/pending/` 加 1 天过期 lifecycle;deploy.yml 幂等下发。

- [ ] **Step 1: template 加 DeleteObject**

在 `src/template.yaml` 的 OrgApiFunction policy 里,把 S3 语句的 Action 列表(现为 `s3:PutObject` / `s3:GetObject`)那段改为(anchor:`                - s3:GetObject` 后一行 `              Resource: !Sub arn:aws:s3:::${DataBucketName}/org-assets/*` 前):在 `- s3:GetObject` 下加一行 `- s3:DeleteObject`。即:

```yaml
            - Effect: Allow
              Action:
                - s3:PutObject
                - s3:GetObject
                - s3:DeleteObject
              Resource: !Sub arn:aws:s3:::${DataBucketName}/org-assets/*
```

- [ ] **Step 2: 建 lifecycle 脚本**

新建 `scripts/wire-bucket-lifecycle.sh`(仿 `wire-bucket-cors.sh`;put-bucket-lifecycle-configuration 会**整体替换** lifecycle 配置,所以先检查是否已有其它规则,有则中止以免误删):

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

在 `.github/workflows/deploy.yml` 的 `Wire bucket CORS` 步骤之后加一步:

```yaml
      - name: Wire bucket lifecycle (TEST bucket, expire abandoned pending uploads)
        run: bash scripts/wire-bucket-lifecycle.sh fieldsight-data-test-509194952652 ${{ env.AWS_REGION }}
```

- [ ] **Step 4: 校验**

Run: `cfn-lint src/template.yaml infra/db-template.yaml`(exit 0)。`bash -n scripts/wire-bucket-lifecycle.sh`(语法检查,无输出=OK)。`python -m pytest tests/unit -q`(全 PASS)。

- [ ] **Step 5: 提交**

```bash
git add src/template.yaml scripts/wire-bucket-lifecycle.sh .github/workflows/deploy.yml
git commit -m "feat(3b): org-api s3:DeleteObject + pending-uploads 1-day lifecycle"
```

**部署前置(权限门,执行阶段由用户 `!` 自跑)**:deploy role 需 `s3:PutLifecycleConfiguration`+`s3:GetLifecycleConfiguration`(限 test 桶),否则 CI 的 lifecycle 步骤失败。命令(仿 CORS 授权,已有 `test-bucket-cors` policy 先例):
```bash
aws iam put-role-policy --role-name github-actions-fieldsight-deploy \
  --policy-name test-bucket-lifecycle --policy-document '{"Version":"2012-10-17","Statement":[{"Sid":"TestBucketLifecycle","Effect":"Allow","Action":["s3:PutLifecycleConfiguration","s3:GetLifecycleConfiguration"],"Resource":"arn:aws:s3:::fieldsight-data-test-509194952652"}]}'
```

---

### Task 8: PR、部署、迁移、归档+上传冒烟

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
gh pr create --base develop --title "Phase 3b: org data-model (archive + upload lifecycle)" --body "…"
```
等 CI(test.yml 在 pgvector 容器跑 integration + 0005 迁移;ci.yml lint 双模板)。红则修复循环。

- [ ] **Step 3: 应用 lifecycle IAM 授权(用户)**,合并(用户,权限门)。合并后盯 deploy.yml(`gh run watch`)。

- [ ] **Step 4: 迁移 0005 上库**

部署完成后手动 invoke migrate:
```bash
export AWS_CLI_FILE_ENCODING=UTF-8 PYTHONUTF8=1
aws lambda invoke --function-name fieldsight-test-migrate --payload '{}' \
  --cli-binary-format raw-in-base64-out /dev/stdout --region ap-southeast-2
```
Expected: `{"applied": ["0005_archive.sql"]}`(二跑 `[]`)。Data API 核对三表有 `archived_at` 列。

- [ ] **Step 5: 归档 + 上传生命周期冒烟**

用合成 admin claims 直接 invoke org-api(参考批次3后端已用过的 `printf event → file:// payload → cygpath -m` 法):
- `POST /api/org/sites/{一个真实 site_id}/archive` → 200,`archived_at` 非空;再 `GET /api/org/sites` → 该 site 消失;`/unarchive` → 恢复。
- `POST /api/org/upload-url {kind:avatar,content_type:image/png}` → key 在 `org-assets/pending/{sub}/`;PUT 一张测试图到返回 url;`PATCH /me {avatar_s3_key: <pending>}` → 200,返回 `avatar_s3_key` 在 `org-assets/avatars/{sub}/`;`aws s3 ls` 确认 pending 已删、avatars 有对象。
- 确认 lifecycle:`aws s3api get-bucket-lifecycle-configuration --bucket fieldsight-data-test-509194952652` 有 `org-assets-pending-expiry`。

- [ ] **Step 6: 记账 + 收尾**

`.superpowers/sdd/progress.md` 追加批次1完成行(栈状态、迁移、冒烟结果、待办)。批次2(UI)另起 writing-plans。

---

## 自审(已完成)

- Spec §4 覆盖:4.1 归档(0005 迁移 T1、list 过滤 T2、archive 函数 T3、端点 T4)✅ · 4.2 上传生命周期(pending T5、搬迁+删旧 T6、lifecycle T7)✅ · 4.3 IAM(DeleteObject T7、lifecycle IAM T7 用户命令)✅ · 4.4 测试(各 Task 的 TDD + T8 冒烟)✅。
- 类型/签名一致:`archive_site(conn, site_id, company_id)`/`archive_user(conn, cognito_sub, company_id)` T3 定义、T4 调用一致;`set_site_icon(conn, site_id, icon_s3_key)` T3/T6 一致;`_relocate_asset(pending_key, final_key)`/`_delete_asset(key)` T6 内部一致;pending 前缀 `org-assets/pending/{sub}/` T5 产出、T6 消费一致。
- 占位符扫描:T2/T8 的 PR body "…" 为执行时自由填;代码步骤无 TBD。
- 已知取舍(代码注释在案):patch_me 在 DB 写前 relocate,DB 失败留至多一个 avatars/ 孤儿(罕见,重传自愈);unarchive 不级联恢复 memberships(显式重加);`get_company_site_by_name`/`get_user_by_sub` 不过滤 archived(seed 幂等 / caller 自读)。
