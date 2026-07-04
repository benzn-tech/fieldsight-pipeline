# Phase 3 收尾设计:Org 数据模型完善 + UI 接线(Design Spec)

> Brainstorming 产出(2026-07-04)。下一步交 writing-plans 拆成实施计划。
> 前置:Phase 3 org 写后端已上线 TEST 并冒烟通过(`fieldsight-test-org-api` `/api/org/{me,sites,members,role,upload-url,asset-url}`,种子已回填 company+4 用户+4 站点+2 memberships)。本 spec 覆盖其**收尾**:数据模型的冗余/归档治理 + 前端接线。
> 账号铁律:一切在 `509194952652`/ap-southeast-2;prod 手工资源不碰。

---

## 1. 目标

让 fieldsight-ui 的**组织管理闭环**(建项目 / 加成员 / 改角色 / 改资料 / 传头像图标)跑在真实 org 后端上,并把后端数据模型补齐到能长期运营:**上传资产不攒冗余、支持归档软删除**。

## 2. 范围与切分

一份设计,拆成**两个实施批次**(有依赖顺序):

- **批次 1(后端,fieldsight-pipeline)**:数据模型完善 —— 归档软删列 + 上传生命周期改造(pending 前缀 + 提交搬迁 + 替换删旧 + lifecycle 兜底)+ 归档端点 + IAM 增量。**先做**,因为 UI 依赖这些端点/语义。
- **批次 2(UI,fieldsight-ui)**:接线 —— 双基址配置 + org 数据层 + 登录身份 + team/sites/settings 页面 + admin 聚合改真实源 + presign 上传 + 归档过滤。

**明确不在范围**(已固化到 memory `fieldsight-recording-site-attribution-gap`):录制内容↔工地归属(S3 报告按人+日期无 site 维度)是 pipeline/数据模型层的独立议题,做多工地聚合前另开设计。UI 上会呈现"成员在多工地",但底层报告数据分不清工地——已知不一致,本批不解决。

## 3. 已敲定的设计决策(brainstorming)

1. **org 域读也切真实**:/team 成员、/sites 列表、/settings 资料都从真实 org API 读(报告类读 timeline/safety/evidence 仍走 prod 网关不变)。
2. **folder_name 由 UI 从 name 派生**:`name` 空格→下划线(`"Jarley Trainor"→"Jarley_Trainor"`),与现有 fixture/report 命名约定一致,零后端改动。
3. **presign 上传这批做**;但 `PATCH /me` 只能改自己 → **头像是自助的**,admin 加成员时不能替别人设头像。真实上传路径 **2 条**:①settings 自己头像 ②sites 站点图标(admin/gm)。
4. **/team = 公司级总览 + 可选工地过滤器**(global_role 公司级;membership.role 工地级,两层分开显示)。
5. **org-assets 两层冗余治理**:替换即删旧 + pending 前缀 + lifecycle 扫弃单。
6. **归档 = 软删除只隐藏**:`archived_at` 让其从 org 列表消失;**Cognito 登录不动**(简单可逆;人员离场禁登录是将来选项)。

---

## 4. 批次 1:后端数据模型完善(fieldsight-pipeline)

### 4.1 归档软删除

**迁移 `0005_archive.sql`**:`sites`、`users`、`memberships` 各加 `archived_at timestamptz`(可空,默认 NULL)。

**仓储**(`repositories/*.py`):
- 所有 list 查询默认加 `AND archived_at IS NULL`(`list_company_users`、`list_company_sites`、`list_sites_by_ids`、`list_company_memberships`、`accessible_site_ids` 的 memberships 分支)。
- 新增 `archive_site(conn, site_id)` / `unarchive_site`、`archive_user(conn, cognito_sub, company_id)` / `unarchive`(set `archived_at=now()` / `NULL`,公司守卫)。归档 user 时**级联归档其 memberships**(同事务);归档 site 时级联归档该 site 的 memberships。

**org-api 端点**(admin/gm,公司守卫):
- `POST /api/org/sites/{id}/archive` · `/unarchive`
- `POST /api/org/members/{sub}/archive` · `/unarchive`
- 归档不删 org-assets、不动 Cognito、不删报告数据(只读保留可追溯)。

### 4.2 上传生命周期改造(治冗余)

**结构(key 布局)**:
```
上传中:  org-assets/pending/{cognito_sub}/{uuid}.{ext}
正式头像: org-assets/avatars/{cognito_sub}/{uuid}.{ext}
正式图标: org-assets/site-icons/{site_id}/{uuid}.{ext}     ← 提交后才知道 site_id,此刻定 key
```

**流程**:
1. **`POST /upload-url`** 改为签发到 `org-assets/pending/{caller_sub}/{uuid}.{ext}`(content_type 白名单不变:jpeg/png/webp)。返回 `{url, key}`,key 是 pending key。
2. UI PUT 图片到 pending。
3. **提交时**后端搬迁:
   - `PATCH /me` 收到 `avatar_s3_key`(必须是**调用者自己**的 pending key,校验前缀 `org-assets/pending/{caller_sub}/`)→ `CopyObject` 到 `org-assets/avatars/{caller_sub}/{uuid}.{ext}` → `DeleteObject` pending → 若旧 `avatar_s3_key` 存在且在 `org-assets/` 下则 `DeleteObject` 旧的 → 存正式 key。
   - `POST /sites` 收到 pending `icon_s3_key` → 建 site 拿到 `site_id` → `CopyObject` 到 `org-assets/site-icons/{site_id}/{uuid}.{ext}` → 删 pending → 存正式 key。
4. **`org-assets/pending/` 加 S3 lifecycle 规则:1 天过期**(扫掉用户选了图又取消的弃单)。
5. **`GET /asset-url`** 读正式 key(前缀守卫 `org-assets/`,拒 pending 直读或允许自己的 pending 做预览——见开放项)。

**净效果**:每人至多一个当前头像对象、每 site 至多一个当前图标、弃单 1 天蒸发 → **零长期孤儿**。

### 4.3 IAM 增量(org-api 角色,全部限 `org-assets/*` 或本池)

- `s3:CopyObject`(其实是 GetObject+PutObject 组合)、`s3:DeleteObject`,资源限 `arn:aws:s3:::fieldsight-data-test-509194952652/org-assets/*`。
- 归档端点无新 AWS 权限(纯 DB)。
- S3 lifecycle 规则:走 `wire-bucket-lifecycle.sh`(仿 `wire-bucket-cors.sh`)在 deploy.yml 幂等下发;deploy role 需 `s3:PutLifecycleConfiguration`+`s3:GetLifecycleConfiguration`(限 test 桶)——**权限门,给用户命令自跑**。

### 4.4 后端测试

- 仓储集成测试(pgvector 容器):归档过滤、级联归档、unarchive。
- handler 单测(mock conn + mock s3 client):upload-url 签 pending key;patch_me 搬迁(copy+del pending+del old)+ 拒他人 pending;create_org_site 图标搬迁到 site_id key;archive/unarchive 端点 ACL + 公司守卫。
- 全绿 + cfn-lint 双模板。

---

## 5. 批次 2:UI 接线(fieldsight-ui)

### 5.1 配置层(双基址 + org 写开关)
- `amplify.yml` env.js 生成加 `FS_ORG_BASEURL`(指 test 网关 `https://wdsgobb7b0.execute-api.ap-southeast-2.amazonaws.com/prod/api`)、`FS_ORGWRITES`(布尔)。`env.example.js` 同步。
- `api/index.js` 的 `window.FS.api` 增 `orgBaseUrl` + `orgWrites`(读自 `window.FS_ENV`)。
- `app-shell-preview.html` 加 `?orgbaseurl=` / `?orgwrites=` 覆盖钩子(仿现有 `?mocks` / `?writemocks`)。

### 5.2 数据层——新增 org 请求通道
- `api/_fetch.js` 加 `orgRequest(path, opts)` 薄包装:解析 `orgBaseUrl`,复用同一套认证(**裸 idToken——prod 池 token 经 dual-pool authorizer 在 org 网关一样过**)。不动现有 `request()`。X-Request-Id 的同源守卫天然排除跨源 org 网关(和现有 prod 跨源读同款,已验证可行)。
- 新建 `api/org.js`:`getMe / updateProfile / getOrgSites / createOrgSite / getMembers / createMember / updateMemberRole / archiveMember / archiveSite / uploadUrl / assetUrl`。
  - **门控**:org 读 gate on `!useMocks`(live→真实,否则 fixtures);org 写 gate on `!useMocks && orgWrites`(新开关,**不碰** `writeMocks`——programme/safety-create 等仍 mock)。
  - **folder_name 派生**:`getMembers` 返回的每个成员补 `folder_name = name.replace(/ /g, '_')`(供 admin 聚合用)。

### 5.3 身份 / 会话
- `login-screen.js` 的 `hydrateUser` 从 `getSites()` 改成 **`GET /api/org/me`**:填 sub、email、global_role、site_ids、first/last name(补上目前缺的 sub/email)。`session-bridge.js` 照旧镜像到 `AuthMock.currentUser`,映射加 sub/email。

### 5.4 页面接线
- **/team**(`pages/team.js`):成员列表 → `org.getMembers`;加成员 → `org.createMember`;改角色 → `org.updateMemberRole`;归档成员 → `org.archiveMember`。公司级总览 + 工地过滤器(按 membership.site_id 过滤)。加成员弹窗**去掉头像选择器**(不能替别人设,决策 3)。
- **/sites**(`pages/sites.js`):列表 → `org.getOrgSites`;建项目 → `org.createOrgSite`(图标走 presign);归档 → `org.archiveSite`。
- **/settings**(`pages/settings.js`):`deriveProfile` → `org.getMe`;`saveProfile` 名字 → `org.updateProfile`(PATCH /me);头像走 presign。
- **admin 聚合**(`api/compliance-aggregator.js`、`api/tasks-aggregator.js`、`pages/evidence.js` 三处):用户源从 `fixtures.sites.users` 换成 `org.getMembers`,`folder_name` 用派生字段。**注意**:报告数据工地归属的已知不一致(见范围),聚合仍按 folder_name 扇出,与现状一致。
- **归档 UI**:列表默认隐藏 `archived_at != null`;加"查看已归档"开关可恢复(`unarchive`)。

### 5.5 presign 上传接线
- `api/media.js` 加 `presignedPut(kind, content_type)` → `org.uploadUrl`(POST /upload-url);`uploadImage(file, kind)` 助手:取签名 → PUT file 到 url → 返回 **pending key**。显示走 `org.assetUrl(key)`(GET /asset-url)。
- 2 处调用点接真实:
  - settings 头像:`FileReader` data-URI → `uploadImage(file,'avatar')` 拿 pending key → `PATCH /me { avatar_s3_key: pendingKey }`(后端搬迁)。显示用 `assetUrl(正式key)`。
  - sites 站点图标:`uploadImage(file,'site_icon')` 拿 pending key → `POST /sites { icon_s3_key: pendingKey }`(后端搬迁到 site_id key)。
- mock 模式保持现有 data-URI 行为(gate on `orgWrites`)。

### 5.6 收尾
- 无构建步骤;每个改动 `node --check`;改到的 `.js`/`.css` bump `?v=N`;`components-preview.html` 若涉及组件补注册。
- **Chrome 全流程验证**(双身份):admin 登录建项目/加成员/改角色/归档/传站点图标;自己登录改资料+传头像;刷新持久;顺带端到端验证 dual-pool authorizer(真 idToken 过 org 网关,目前只做过直接 invoke)。

---

## 6. 数据流(关键路径)

**加成员**:UI(admin)→ `POST /api/org/members {email,global_role,memberships}` → Cognito admin-create(发邮件邀请)+ upsert_user + ensure_membership → 201 → /team 刷新显示。

**改头像**:UI → `POST /upload-url{kind:avatar}` → pending key → PUT S3 → `PATCH /me{avatar_s3_key:pending}` → 后端 copy→avatars/ + del pending + del 旧 → 存 key → UI `GET /asset-url` 显示。

**归档项目**:UI(admin)→ `POST /api/org/sites/{id}/archive` → set archived_at + 级联 memberships → 列表隐藏。

## 7. 测试 / 验证策略
- 后端:仓储集成测试(pgvector 容器)+ handler 单测(mock conn/s3)。
- UI:`node --check` + Chrome 双身份全流程(真浏览器,live 契约验证——mock 形状≠真实契约是历史教训)。
- 两批各自 PR → CI(pytest+cfn-lint / Amplify build)→ 逐任务审查 + 全分支终审(沿用 Phase 3 后端已证明有效的子代理流水线)。

## 8. 开放项 / 风险
- `GET /asset-url` 目前无租户隔离(任何认证用户可 presign 任意 `org-assets/` key;uuid 不可猜 + 组织内共享 = 暂可接受)。是否顺带给 asset-url 加"key 属主校验"?**倾向本批不加**(会牵扯 site-icon 的跨用户可见),记为后续。
- 归档 user 不禁 Cognito → 被归档的人仍能登录(登录后 `/me` 会 403"未在 org 库"吗?不会——users 行还在只是 archived;需决定 `/me` 对 archived caller 的行为:**建议 archived caller 仍能读自己 /me 但 /members 列表不含他**)。
- pending 搬迁的 CopyObject 在 in-VPC Lambda 需经 S3 gateway endpoint(已就绪)——无 BUG-36 风险。
- lifecycle 规则下发需 deploy role 加 `s3:PutLifecycleConfiguration`(权限门,给用户命令)。

## 9. 与既有约定的一致性
- 仓储永不 commit(caller 拥有事务);`with get_connection() as conn:` 提交/回滚。
- ACL deny-by-default;角色 admin|gm|pm|site_manager|worker;公司守卫贯穿写路径。
- Windows autocrlf:单行 Edit anchor;绝不 `git add -A`。
- UI 无构建步骤、tokens-only、cache buster、BEM——沿用 fieldsight-ui CLAUDE.md 约定。
