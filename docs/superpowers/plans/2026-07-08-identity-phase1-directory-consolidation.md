# 身份系 Phase 1:身份目录收编(migration 0007)Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development。
> Spec:`docs/superpowers/specs/2026-07-07-identity-system-merge-analysis.md`(方案 A + §3 迁移路径 + §5 阶段 1)。
> 这是 backlog #1、Phase 4b 实时抽取的"硬前置",根治全库反复出现的 slug↔UUID↔name ad-hoc 匹配。

**Goal:** org DB 成为唯一身份目录:`sites.slug`(回填,唯一)+ `users.folder_name/kind`(收编 8 报告人含 4 非登录设备用户)+ `cognito_sub` 可空;把 ingest/item-writer/org-api 的 name 匹配换成 slug/folder_name 查表;历史 topics/chunks 的 NULL user_id 回填至 0。

**Architecture:** migration 0007(一次性,runner 按文件名整型排序、事务内单次应用)→ 扩展 seed(回填 slug + 收编 field_only 用户 + 补 login 用户 folder_name,双路径幂等)→ ingest/item-writer resolve_* 改查表 → org-api 吐 slug/folder_name → 部署后 migrate+seed+ingest 回填。

**Tech Stack:** Python 3.11/pytest;psycopg;SQL DDL。

## Global Constraints(侦察锁定)

- **migration runner**:`db/migrate.py` 按 `int(filename.split('_')[0])` 排序、事务内单次应用、按文件名记 schema_migrations——**0007 只跑一次,文件内无需 IF NOT EXISTS**(照 0002-0006 裸 DDL 风格)。
- **0007 列(spec §3 verbatim)**:
  - `sites`:`ADD COLUMN slug text`;回填(见下);`CREATE UNIQUE INDEX idx_sites_company_slug ON sites(company_id, slug)`。
  - `users`:`ALTER COLUMN cognito_sub DROP NOT NULL`;`ADD COLUMN folder_name text`;`ADD COLUMN kind text NOT NULL DEFAULT 'login'`;`CREATE UNIQUE INDEX idx_users_company_folder ON users(company_id, folder_name)`。
  - cognito_sub 保留 UNIQUE(NULL 不参与 PG 唯一性)。
- **slug 回填**:0007 里**无法读 S3 user_mapping**(DDL 层);slug 回填放 **seed**(seed 已读 user_mapping,按 name 命中 site 后 `UPDATE sites SET slug=%s WHERE id=%s`)。0007 只加列+索引;seed 填值。**注意唯一索引**:回填前 slug 全 NULL(多行 NULL 不冲突唯一索引——PG 允许);seed 幂等回填。
- **kind 枚举**:`'login'`(默认)| `'field_only'`(spec 用词,非 'device'/'report-only')。
- **收编规则(spec §3 line 207)**:mapping 有而 Cognito 没有的人 → 建 `kind='field_only'` 行(folder_name=name.replace(' ','_'),cognito_sub NULL,global_role=mapping role,company_id=FieldSight)+ 按 mapping sites[] 建 membership;Cognito 用户 → 补 folder_name。8 人:Jarley_Trainor/David_Barillaro=login;MPI1/MPI2/James_Lamb/Jack_Gibson=field_only。
- **upsert_user 双路径幂等**:现 `ON CONFLICT (cognito_sub)`;新增 folder_name 路径——`upsert_field_only_user(conn, company_id, folder_name, ...)` 用 `ON CONFLICT (company_id, folder_name) DO UPDATE`;login 用户补 folder_name 走现 sub 路径 UPDATE。**cognito_sub NULL 的行不能走 sub-conflict**(NULL 不冲突)——field_only 专用 folder_name 路径。
- **create_site 加 slug**:`create_site(..., slug=None)`;seed 回填改用 create 时带 slug 或后置 UPDATE(择一,幂等)。
- **ingest/item_writer 切查表(payoff)**:resolve_site → 先 `sites.get_company_site_by_slug`(新)——但 report 侧给的是**显示名**不是 slug;桥:report['site'] 显示名 → user_mapping 反查 slug?**保留 name→site 主路径**(get_company_site_by_name),**新增**:双 miss 时用 user 的 folder_name→user 行→其 membership site(比 primary_site 启发式硬)。resolve_user → **直接 `users.get_by_folder_name(conn, company_id, folder_name)`**(净新,替代 list+name join;folder_name 直接来自 user_folder)。收编后 field_only 用户也命中 → user_id 不再 NULL。
- **org-api 吐 slug/folder_name**:list_org_sites 的 _COLS 含 slug;users 相关吐 folder_name。启用 `?site=<slug>` 经 `get_company_site_by_slug` 解析到 uuid(programme/live-items/rollup 可选接受 slug 或 uuid——v1 加 slug→id 解析辅助,不破坏现 uuid 契约)。
- **历史回填(T5 运维)**:部署后 invoke migrate → seed(收编+slug)→ ingest `{"backfill":true}`(resolve_user 现命中 field_only → UPDATE 历史 NULL user_id;source_s3_key 幂等)。验收:`SELECT count(*) FROM report_chunks WHERE user_id IS NULL` 趋 0(BD Opportunity 等非站点报告仍跳过,合理)。
- **⚠️ 生产数据操作**:0007 前向不可逆;seed 重跑有已知 role 覆盖 quirk(重跑覆盖 org-api 改过的角色——spec §附记录,可接受);T5 在 test 栈 Aurora 执行,谨慎、可核对。
- 铁律:pytest 零回归(基线 211);单行 Edit 锚;绝不 `git add -A`;串行部署。

---

### Task 1:migration 0007 + repo 列/查表(TDD)

**Files:** Create `src/migrations/0007_identity_directory.sql`;Modify `src/repositories/sites.py`、`users.py`;Modify 相关单测。

- 0007.sql:上述 sites/users 的 ALTER + 两个 UNIQUE INDEX(裸 DDL,单次)。
- sites.py:`_COLS` 加 `slug`;`get_company_site_by_slug(conn, company_id, slug) -> dict|None`(镜像 get_company_site_by_name);`create_site` 增 `slug=None` 参数(INSERT 含 slug)。
- users.py:`_COLS` 加 `folder_name, kind`;`get_by_folder_name(conn, company_id, folder_name) -> dict|None`;`upsert_field_only_user(conn, company_id, folder_name, first_name, last_name, global_role) -> dict`(cognito_sub NULL,kind 'field_only',`ON CONFLICT (company_id, folder_name) DO UPDATE`);`set_folder_name(conn, cognito_sub, folder_name)`(login 用户补)。

- [ ] 测试先行(FakeConn 断言 SQL/参数;有集成测试则 skip DB):get_company_site_by_slug 查询含 company_id+slug;create_site 带 slug;get_by_folder_name;upsert_field_only_user 的 ON CONFLICT (company_id, folder_name) + cognito_sub NULL + kind field_only;set_folder_name UPDATE by sub。
- [ ] 实现;py_compile;全套 pytest 零回归。
- [ ] 提交 `feat(identity): migration 0007 (sites.slug, users.folder_name/kind, nullable sub) + repo lookups`。

### Task 2:seed 扩展——回填 slug + 收编 field_only + 补 folder_name(TDD)

**Files:** Modify `src/lambda_org_seed.py`;Modify 其测试(若有;否则新建 test_lambda_org_seed.py monkeypatch 风格)。

- slug_to_site 循环:命中/建 site 后 `sites` 回填 slug(create 带 slug 或后置 UPDATE by id,幂等)。
- Cognito 用户循环:upsert 后 `users.set_folder_name(sub, name.replace(' ','_'))`。
- **新增第二遍**:遍历 `mapping["mapping"]` 的 name,若该 name 不在 Cognito 用户集 → `users.upsert_field_only_user(company_id, folder_name=name.replace(' ','_'), first/last=split_name, global_role=mapping role)` → 按 `info["sites"]` slug→site_id `ensure_membership`。
- 幂等:全部走 ON CONFLICT/get-then-set;重跑不重复(role 覆盖 quirk 保留,注释)。

- [ ] 测试先行(monkeypatch repos + s3 user_mapping;FakeConn):slug 回填调用 / login 用户补 folder_name / field_only 用户按 name-not-in-cognito 收编 + membership / 重跑幂等(ON CONFLICT 路径)/ 返回摘要含收编数。
- [ ] 实现;全套零回归。
- [ ] 提交 `feat(identity): seed backfills sites.slug + enrolls field_only users + sets folder_name`。

### Task 3:ingest/item_writer resolve_* 切查表(TDD)

**Files:** Modify `src/lambda_ingest.py`(resolve_site fallback + resolve_user);Modify `tests/unit/test_lambda_ingest.py`。

- resolve_user:改为 `users.get_by_folder_name(conn, company_id, user_folder)`(user_folder 即 folder_name);miss→None(保留)。**收编后 field_only 命中 → 历史回填生效。**
- resolve_site:主路径 name 命中不变;fallback 改用 user 的 membership(get_by_folder_name→user→accessible_site_ids 取首个)或保留 primary_site slug 但经 `get_company_site_by_slug`(净新,替代 slug→name→name-match 三跳);双 miss 仍跳过。
- item_writer 无需改(复用 lambda_ingest.resolve_*)。

- [ ] 测试先行:resolve_user 走 get_by_folder_name / field_only folder 命中 / resolve_site fallback 经 slug 查表 / 双 miss 跳过不变。
- [ ] 实现;全套零回归。
- [ ] 提交 `feat(identity): ingest resolves user by folder_name + site by slug (retire name heuristic)`。

### Task 4:org-api 吐 slug/folder_name + ?site=slug 解析(TDD)

**Files:** Modify `src/lambda_org_api.py`;Modify `tests/unit/test_lambda_org_api.py`。

- list_org_sites 返回含 slug(sites._COLS 已含);caller/users 相关吐 folder_name。
- 加辅助:`?site=` 参数若非 UUID 形态则经 `sites.get_company_site_by_slug` 解析到 id(programme/live-items/rollup 的 site 解析统一走一个 helper `_resolve_site_param`——接受 uuid 或 slug;**不破坏现 uuid 契约**,只增容 slug)。ACL 仍按解析后的 id ∈ accessible。
- [ ] 测试先行:sites 吐 slug / _resolve_site_param 接受 uuid 原样 + slug 解析 + 未知→None→403 / programme|rollup 用 slug 也通。
- [ ] 实现;全套零回归。
- [ ] 提交 `feat(identity): org-api exposes slug/folder_name + accepts ?site= as slug-or-uuid`。

### Task 5:Fable 终审 → PR → 部署 → migrate+seed+回填(控制器)

- [ ] 整分支 diff → Fable 5 终审(镜头:0007 唯一索引与 NULL slug 回填顺序、upsert 双路径幂等与 cognito_sub NULL 不误撞 sub-conflict、resolve_* 查表正确性与收编后命中、_resolve_site_param 不破坏 uuid 契约与 ACL、跨公司隔离、seed 重跑 role quirk、历史回填幂等)。修→复审。
- [ ] PR(base develop)→ 用户合并 → 部署 success。
- [ ] **生产数据操作(test 栈 Aurora,谨慎)**:invoke migrate(0007 应用)→ 核对 `\d sites`/`\d users` 有新列 → invoke seed(slug 回填 + 收编)→ 核对 `SELECT slug FROM sites`(3 站有 slug)、`SELECT folder_name, kind FROM users`(8 人,4 login 4 field_only)→ invoke ingest `{"backfill":true}` → 核对 `SELECT count(*) FROM report_chunks WHERE user_id IS NULL`(趋 0,非站点报告除外)。
- [ ] 账本 + memory 更新身份系状态;UI `?site=` 恢复 = 后续小批。

## 自审
- spec §5 阶段1 全覆盖:0007(T1)、seed 收编+slug(T2)、ingest 切查表(T3)、org-api 吐 slug/folder(T4)、历史回填+验收(T5)。UI ?site= 恢复留后续(注明)。
- 接口一致:get_company_site_by_slug(T1)↔ seed 回填/ingest fallback(T2/T3)/_resolve_site_param(T4);get_by_folder_name/upsert_field_only_user(T1)↔ seed 收编(T2)/resolve_user(T3)。
- 预判:唯一索引 vs 多 NULL slug(PG 允许)、upsert 双路径 NULL sub 不误撞、生产迁移前向不可逆+seed role quirk、历史回填幂等(source_s3_key)、_resolve_site_param 向后兼容 uuid。
