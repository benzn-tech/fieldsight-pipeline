# 身份系合并全面分析(device-mgmt 前置分析)

> **性质:** 分析文档,不含代码改动。为 device-mgmt 批次与后续 Phase 4b/5 定身份统一方向。
> **日期:** 2026-07-07 · **输入:** `config/user_mapping.json` v2.1、`src/lambda_ingest.py` v1.0、
> `src/lambda_org_seed.py` v1.0、migrations 0002/0003/0004、
> `docs/superpowers/plans/2026-07-01-platform-evolution-roadmap.md`;
> ui 仓 `scripts/site-context.js`、`scripts/api/org.js`、`scripts/pages/sites.js`、`scripts/pages/team.js`。

---

## 0. 结论速览

- 系统当前有 **两套平行身份体系**(org 侧 Aurora UUID 系 vs 报告侧 S3 字符串系),之间只有
  **4 座基于字符串匹配的临时桥**,四大痛点全部源于此。
- **推荐方案 A:org 库收编为唯一身份目录 + 数据库归属账本(assignment ledger),S3 路径不动。**
  一次 migration 把 slug/folder_name/device 全部收编进 Aurora,`user_mapping.json` 降级为
  自动导出物,管线零改动;录制↔工地归属靠 `device_assignments`(带生效时间)+
  `recording_sessions`(时段级覆盖)分层解决。
- 痛点①(录制↔工地归属)推荐组合:**②的 assignments 作默认归属 + 采集入口轻量标签作覆盖 +
  展示层切分**;不做 S3 路径重构(解法 b 否决)。
- 四个阶段,每阶段独立可交付;阶段 1(身份目录收编)是 Phase 4b 实时抽取与 Phase 5 RAG
  的直接前置,建议紧接着做。

---

## 1. 现状:身份标识符全图谱

### 1.1 标识符清单(每个标识符存在于哪里)

| 标识符 | 例值 | 权威来源 | 还出现在(消费方) |
|---|---|---|---|
| **company.id** (uuid) | `d3b0…` | Aurora `companies` | `users.company_id`、`sites.company_id`、org API ACL |
| **user.id** (uuid) | `9f2a…` | Aurora `users` | `memberships.user_id`、`topics.user_id`(可 NULL)、`report_chunks.user_id`(可 NULL)、observations(0006) |
| **cognito_sub** | `4a8c…` | Cognito 池 `ap-southeast-2_q88pd6XXr` | `users.cognito_sub`(NOT NULL UNIQUE)、JWT、org API 身份、ui `_toPageMember.device_id`(误用作展示 id) |
| **email** | `benl.tech@…` | Cognito | `users.email`、org seed 的 admin 判定 |
| **first_name / last_name** | `Jarley` / `Trainor` | Aurora `users`(seed 时从 Cognito `name` 拆分) | `lambda_ingest.resolve_user` 的 name-join 匹配、ui `org.js folderName()` |
| **site.id** (uuid) | `77c1…` | Aurora `sites` | `memberships.site_id`、`topics/report_chunks/…site_id`、ui `_toPageSite().site_id` |
| **site 显示名** | `SB1108 Ellesmere College` | 双源!Aurora `sites.name` **和** `user_mapping.json sites[slug].name` | `daily_report.json` 的 `report['site']`(Claude 生成,可能不是真工地)、`lambda_ingest.resolve_site` 第一跳 |
| **site slug** | `sb1108-ellesmere` | `config/user_mapping.json` `sites{}` 键 | 报告 API `?site=`、ui `/timeline` 的 `loadTimelineSite()`/`getDates({site})`/`getSiteUsers(site)`、`mapping[].primary_site/sites[]` |
| **设备 ID** | `Benl1`…`Benl6` | RealPTT 设备账号(系统外) | **所有媒体/转录文件名前缀**(`Benl1_2026-03-20_12-18-34…`)、`user_mapping.json mapping{}` 键 |
| **用户文件夹名 / display_name** | `Jarley_Trainor`(空格→下划线) | S3 路径约定(隐式) | `users/{folder}/…`、`transcripts/{folder}/{date}/`、`reports/{date}/{folder}/`、`lambda_ingest._display_name` 反向还原、ui `org.js folderName()` 正向合成 |
| **mapping 人名** | `Jarley Trainor`、`MPI1` | `user_mapping.json mapping[].name` | org seed 的 role/membership 推导(lower-case name match)、ingest 的 fallback 桥 |
| **reassignment_log** | 手工数组(现仅 `_example`) | `user_mapping.json` | 无任何代码消费——纯人肉审计 |

### 1.2 两套体系的画像

```
┌─ org 侧(Aurora,Phase 2/3)────────────┐   ┌─ 报告侧(S3 + config,管线原生)──────────┐
│ companies.id / sites.id / users.id      │   │ 设备 ID   Benl1…Benl6(文件名前缀)        │
│   全 UUID,外键闭环                     │   │ 文件夹名  Jarley_Trainor(路径段)          │
│ users.cognito_sub ── 登录身份           │   │ site slug sb1108-ellesmere(API 查询键)    │
│ memberships ── 人↔工地多对多+每站角色  │   │ site 名   SB1108 Ellesmere College(报告体)│
│ 4 个登录用户(仅 Cognito 镜像)         │   │ 8 个报告文件夹人(含 MPI1/MPI2 等占位名)  │
└─────────────────────────────────────────┘   │ 静态映射  config/user_mapping.json          │
                                              └─────────────────────────────────────────────┘
```

### 1.3 现存的 4 座桥(全部是字符串匹配,全部单向)

| # | 桥 | 位置 | 机制 | 脆弱点 |
|---|---|---|---|---|
| B1 | Cognito → org users | `lambda_org_seed` | sub+email 镜像;role 靠 `name.lower()` 撞 mapping 人名 | 改名即断;重跑 seed 会覆盖 org API 改过的角色(已知坑) |
| B2 | 报告 → org site | `lambda_ingest.resolve_site` | `report['site']` 名匹配 → miss 再走 mapping `primary_site` slug → 名 → 再匹配;双 miss 整报告跳过 | `report['site']` 是 Claude 输出(实证:`BD Opportunity Brainstorm`);fallback 只会给 primary_site,**跨工地日永远归到主工地** |
| B3 | 文件夹 → org user | `lambda_ingest.resolve_user` | 下划线→空格,与 `first_name+last_name` 拼串比对;miss → NULL | 8 个文件夹人只有 4 个有 Cognito row,**MPI1/MPI2/James Lamb/Jack Gibson 恒 NULL**(James/Jack 若无登录账号);曾引发 Fable Critical(NULL-user scope 互删) |
| B4 | org member → 文件夹 | ui `org.js folderName()` | `first_name_last_name`,或 name 空格→下划线 | 与 B3 是同一脆弱假设的两个方向;org site UUID ↔ report slug **无桥**——`sites.js`/`team.js` 跳 timeline 被迫不带 `?site=`(代码内注释明确 parked) |

**核心事实:两套体系之间没有任何一张表存储对应关系——所有对应都在运行时靠"名字长得像"重建。**

---

## 2. 四痛点根因分析

### 痛点①:录制↔工地归属缺口

**现象:** S3 全部路径按 人+日期 组织(`users/{person}/video/{date}/`、`reports/{date}/{person}/`),
无 site 段;同一人同日跨工地录制无法区分归属。

**根因:** 归属信息只在**录制那一刻**客观存在(人身处哪个工地),但采集链
(RealPTT 设备 → orchestrator → S3)没有任何环节捕获它。事后所有环节
(VAD/转录/报告/ingest)拿到的只有 `设备+人+时间`,site 只能靠静态
`primary_site` 猜——这是**信息丢失**问题,不是路径格式问题。B2 的 fallback 行为
就是该根因的直接后果:跨工地日全部归到主工地,错得安静。

**放大器:** 一个被低估的资产——**每个媒体/转录文件名都以设备 ID 开头**
(`Benl1_2026-03-20_12-18-34…`,BUG-11 的元数据约定)。只要有"设备在某时段被
分配给谁、在哪个工地"的账本,`设备+文件名时间戳` 就能确定性解析出 人+工地,
不需要改采集端也不需要改路径。这是 §5 推荐组合的基石。

### 痛点②:设备转交无时间维度

**现象:** 设备换人 = 手工改 `user_mapping.json` + 手工追加 `reassignment_log` + 手工传 S3。

**根因:** mapping 是**当前状态快照**,没有生效区间。历史录音的归属靠"S3 文件夹名
在写入时被冻结"这个副作用维持;`reassignment_log` 无代码消费、格式无约束(现存两条
都是 `_example`)。已定方向(admin 产品 UI 操作、org 库为 source of truth、
变更后自动导出 user_mapping.json)正确,本文给出 schema 落地(§4 方案 A)。
带生效日期的 assignments 同时正是①的基座数据。

### 痛点③:登录身份 ≠ 报告身份

**现象:** org `users` 仅 4 个登录用户;S3 报告文件夹 8 人(James Lamb / Jack Gibson /
MPI1 / MPI2 是设备映射用户,非登录用户)。

**根因:** `users` 表把两个不同概念焊死成一个:**"能登录的账号"**(`cognito_sub NOT NULL`)
与 **"出现在数据里的人"**。seed 只从 Cognito 进人,于是不登录的现场人员没有 row →
B3 恒 miss → `topics.user_id` / `report_chunks.user_id` 大面积 NULL → 按人过滤、
按人聚合、Phase 5 的 per-user 引用全部失能。MPI1/MPI2 这类占位名说明现实中
"设备使用者"甚至可以不是自然人名——身份目录必须容纳"非登录人员"。

### 痛点④:身份桥是临时桥

**现象:** ingest 的 resolve_site/resolve_user 靠名字匹配、双 miss 跳过;ui 的
sites/team 页跳 Timeline 不敢带 `?site=`。

**根因:** 与③④同源于 §1.3 的结论——**对应关系无处持久化**。org site UUID 与
report slug 分属两个键空间,查询链(`?site=` → `loadTimelineSite()` →
`getDates({site})`)全部键在 slug 上,而 org API 只吐 UUID。桥的每一跳都是
启发式,所以只能"宁可跳过不可写错"(ingest)或"宁可不带参数不可带错参数"(ui)。
这不是两个 bug,是同一缺失的两个症状。

---

## 3. 统一方案(三案对比)

### 方案 A(推荐):org 库收编为唯一身份目录 + 归属账本,S3 路径不动

**思路:** 把报告侧的三类字符串标识符(slug、folder_name、设备 ID)全部**收编为
org 表的列/表**,让对应关系变成数据而非启发式;录制归属用 `device_assignments`
(默认)+ `recording_sessions`(覆盖)两层账本表达;`user_mapping.json` 变成
从 org 库**自动导出**的产物(管线读它的代码零改动)。

**Schema 变更(migration 0007 + 0008,草案):**

```sql
-- 0007a 站点收编:slug 入表
ALTER TABLE sites ADD COLUMN slug text;
UPDATE sites SET slug = …;                -- 从 user_mapping.json 一次性回填
CREATE UNIQUE INDEX idx_sites_company_slug ON sites (company_id, slug);

-- 0007b 人员收编:登录与身份解耦
ALTER TABLE users ALTER COLUMN cognito_sub DROP NOT NULL;   -- 非登录人员无 sub
ALTER TABLE users ADD COLUMN folder_name text;              -- 报告侧文件夹名,显式存储
ALTER TABLE users ADD COLUMN kind text NOT NULL DEFAULT 'login';  -- 'login' | 'field_only'
CREATE UNIQUE INDEX idx_users_company_folder ON users (company_id, folder_name);
-- 注意:cognito_sub 的 UNIQUE 约束保留(NULL 不参与唯一性,Postgres 语义天然兼容);
-- upsert_user 需按 (cognito_sub 非空) 与 (folder_name) 两条路径分别幂等。

-- 0008 设备与归属账本
CREATE TABLE devices (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id  uuid NOT NULL REFERENCES companies(id),
  device_code text NOT NULL,               -- 'Benl1'(文件名前缀,即 RealPTT 账号)
  label       text,
  created_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (company_id, device_code)
);

CREATE TABLE device_assignments (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  device_id     uuid NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
  user_id       uuid NOT NULL REFERENCES users(id),
  site_id       uuid REFERENCES sites(id),  -- 默认工地;NULL = 未定
  assigned_from timestamptz NOT NULL,
  assigned_to   timestamptz,                -- NULL = current(即已定草图)
  reason        text,
  created_by    uuid REFERENCES users(id),
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_assignments_device_window
  ON device_assignments (device_id, assigned_from, assigned_to);
-- 应用层保证同 device 时间窗不重叠(转交 = 关旧开新,一个事务)。

-- 时段级归属覆盖(痛点①的跨工地日兜底 + 未来采集端标签的落点)
CREATE TABLE recording_sessions (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  device_id  uuid NOT NULL REFERENCES devices(id),
  user_id    uuid REFERENCES users(id),
  site_id    uuid NOT NULL REFERENCES sites(id),
  starts_at  timestamptz NOT NULL,
  ends_at    timestamptz NOT NULL,
  source     text NOT NULL,                -- 'admin_override' | 'capture_tag' | 'inferred'
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_sessions_device_window ON recording_sessions (device_id, starts_at, ends_at);
```

**归属解析函数(唯一入口,替代 B2/B3):**

```
resolve_recording(device_code, ts):
  1. recording_sessions 命中 (device, ts ∈ [starts_at, ends_at)) → (user, site)   # 覆盖层
  2. device_assignments 命中 (device, ts ∈ [assigned_from, assigned_to)) → (user, site)  # 默认层
  3. miss → 跳过并入 skipped 报表(保留 ingest 现有"绝不发明工地"原则)
```

设备码与时间戳都直接来自文件名(BUG-11 已有 `transcript_utils.extract_*`),
**解析变成两次索引查询,零字符串启发式**。

**user_mapping.json 导出器:** org API 的 devices/assignments 写路径尾部挂一步
"render 当前状态 → 写 `config/user_mapping.json` 到 S3"(内容 = 各设备**当前**
assignment 的 name/role/primary_site/sites,`reassignment_log` = assignments 历史的
只读投影)。VAD/transcribe/API 等所有既有读方零改动——这正是已定方向②的落地。

**迁移路径(全程零停机):**
1. 跑 0007,从 `user_mapping.json` 回填 `sites.slug`;seed 扩展:mapping 里
   有而 Cognito 没有的人 → 建 `kind='field_only'` 行(填 folder_name);
   Cognito 用户 → 补 folder_name。
2. 跑 0008,从 mapping + reassignment_log 回填 devices 与初始 assignments
   (`assigned_from` 取该设备最早录音日,现存 6 台一次人工核对即可)。
3. ingest 的 resolve_site/resolve_user 切到查表(B2/B3 退役);
   历史 `topics.user_id` / `report_chunks.user_id` 为 NULL 的行**按
   source_s3_key 重解析后 UPDATE 回填——不动 embedding,零 re-embed 成本**。
4. org API 吐 `sites.slug` + `users.folder_name` → ui 删掉 `folderName()` 合成逻辑,
   sites/team 跳 Timeline 恢复 `?site=`(B4 退役)。

**对 Phase 4b(实时抽取)的影响:** 4b 的触发事件是 `transcripts/{user}/{date}/` 的
S3 写入,事件时刻就要定身份。方案 A 下:文件名 → device_code+ts →
`resolve_recording` 两次查询,确定、可缓存、不依赖报告体(4b 时甚至还没有
`report['site']` 可用——**A 是 4b 的硬前置**,否则 4b 只能复制 B2 的猜测逻辑)。

**对 Phase 5(RAG)的影响:** ACL 一条 SQL(`site_id = ANY(accessible)`)的前提是
chunks 的 site_id/user_id 正确且非 NULL。A 之后:user_id 覆盖率从 4/8 → 8/8,
per-user 过滤与引用可用;跨工地日不再整天归错主工地,site ACL 语义变真;
历史块只 UPDATE 标识列,向量不动。

### 方案 B:方案 A + S3 路径重构(加 site 段 + 历史迁移)

`users/{site_slug}/{person}/video/{date}/…`,13.5GB 历史对象全量搬迁。
**评估:** 路径里的 site 仍然要有人在写入时决定——**信息源问题一点没解决**,只是把
A 已经放进数据库的答案再抄一份进 key。代价:VAD/transcribe/API/前端全链路路径
约定改造(每一处都是 BUG-01/12/13 型回归的雷区)、S3 事件过滤前缀重配、迁移期
双前缀并存。收益:仅"S3 控制台裸眼可读"。**否决**(数据湖刚并桶完成,稳定压倒美观)。

### 方案 C:维持双体系,只把桥表化(最小改动)

只建一张 `identity_bridge`(slug↔site_id、folder↔user_id)供 ingest/ui 查。
**评估:** 修痛点④的表层,①②③原封不动;桥表本身又成了第三个需要人肉同步的
真相源。适合"两周内必须让 ui 带上 ?site="的应急,不适合作为方向。**不推荐**,
但其查询接口设计与 A 阶段 1 兼容——若急,可先做 A 的 0007a/0007b 子集,天然就是 C。

### 结论

| 维度 | A(收编+账本) | B(A+路径重构) | C(仅桥表化) |
|---|---|---|---|
| 痛点①②③④ | ✅✅✅✅ | 同 A | ❌❌❌✅(半) |
| 管线改动 | 仅 ingest 解析函数 | 全链路路径 | 仅 ingest 查询 |
| 历史数据 | UPDATE 标识列 | 13.5GB 对象搬迁 | 无 |
| 4b/5 就绪 | 直接前置满足 | 同 A(多绕路) | 不满足 |
| 风险 | 低(增列增表,读方不动) | 高(路径约定 = 雷区) | 低但欠债 |

**推荐 A。**

---

## 4. 痛点① 三候选解法逐一评估与推荐组合

**(a) 设备端录制时打工地标签** — 信息源头最干净,是唯一能原生解决"同日跨工地"
的方案。但 RealPTT 设备/固件不可控,现实入口只有 orchestrator 或未来自家采集 app;
且有"忘了选/选错/离线"的人因问题,**必须有 fallback,不能作唯一机制**。
→ **采纳轻量版,后置:** 凡采集入口能带 site 提示(app 选择器、上传元数据),
写一条 `recording_sessions(source='capture_tag')` 即可,落点已在方案 A 里预留。

**(b) S3 路径加 site 段 + 迁移历史** — 见 §3 方案 B 评估:路径是**结果**不是
**信息源**,写路径的人依然要先知道答案;改动面与回归风险全仓最高。→ **否决。**

**(c) 报告生成时按工地切分** — 报告从转录生成,转录不知道工地;让 Claude 从内容
猜归属已被实证反驳(`report['site'] == 'BD Opportunity Brainstorm'`)。但"归属已知
之后,把一天的报告**按工地分篇渲染**"是合理的展示层需求。→ **降级为展示层:**
不作归属源,在 ingest/报告生成读 `resolve_recording` 的结果分组。

**推荐组合(分层归属):**

```
默认层   device_assignments(site_id)       ← 解法②的账本,覆盖绝大多数单工地日
覆盖层   recording_sessions                 ← (a)轻量版 capture_tag + admin 手工改判
展示层   报告/Dashboard 按解析结果分组渲染   ← (c)降级形态
信息键   文件名的 device_code + 时间戳       ← 已存在,零采集端改动
```

跨工地日的实际工作流:默认全归 assignment 的 site → SM/admin 在 UI 上对某时段
改判(写 recording_sessions)→ 受影响报告按 `source_s3_key` 重跑 ingest(幂等,
Fable C1 修复后安全)。

---

## 5. 分阶段实施路线(每阶段独立可交付)

| 阶段 | 内容 | 交付判据 | 解决 |
|---|---|---|---|
| **1. 身份目录收编** | migration 0007(sites.slug、users.folder_name/kind、cognito_sub 可空)+ seed 扩展收编 8 人 + ingest B2/B3 切查表 + 历史 topics/chunks 的 user_id UPDATE 回填 + org API 吐 slug/folder_name + ui 恢复 `?site=` 跳转 | ingest 重跑 backfill:skipped 中不再有 identity miss;`SELECT count(*) FROM report_chunks WHERE user_id IS NULL` → 0;sites/team 页点人跳 Timeline 自动选中该工地 | ③④ 全部,①的查询基建 |
| **2. 设备账本 + 转交 UI** | migration 0008(devices/device_assignments)+ 回填 6 台 + org API 设备 CRUD/转交端点 + admin UI + user_mapping.json 导出器 | 在 UI 完成一次转交:org 库出现关旧开新的两条 assignment,S3 的 user_mapping.json 自动更新,管线新录音归新人 | ② 全部 |
| **3. 归属解析 + 改判** | `resolve_recording` 函数落地(sessions→assignments 两层)+ ingest 切换 + recording_sessions 表 + admin 时段改判 UI + 受影响报告一键重 ingest | 造一个跨工地日:默认归主工地 → 改判某时段 → 重跑后该时段 topics/chunks 归属新工地 | ① 主体 |
| **4. 采集端标签(条件成熟时)** | 采集入口(app/上传元数据)写 `capture_tag` sessions | 带标签的录音免改判直接归对 | ① 剩余(源头化) |

**排期建议:** 阶段 1 紧跟当前批次(它同时是 Phase 4b 的硬前置,且工作量最小、
回报最集中);阶段 2 即已立项的 device-mgmt 批次本体;阶段 3 可与 Phase 4b 并行
(4b 直接以 `resolve_recording` 为身份入口,避免复制临时桥);阶段 4 挂采集端
条件,不阻塞任何其他工作。

---

## 附:本分析核对过的代码事实

- `lambda_ingest.py:126-155` — resolve_site 双跳字符串匹配与"绝不发明工地"原则;resolve_user 的 name-join 与 NULL 容忍。
- `lambda_ingest.py:255-261` — 幂等键已改 `source_s3_key`(Fable C1/I1),阶段 3 的"改判后重跑"依赖此语义。
- `lambda_org_seed.py:54-64` — role 推导靠 `name.lower()` 撞 mapping;重跑覆盖 org API 角色(既有已知坑,阶段 2 后 seed 应退役为一次性工具)。
- `user_mapping.json` — 6 设备/8 人/3 工地;`reassignment_log` 仅 `_example` 且无消费方。
- migrations 0002/0003/0004 — `topics.user_id`/`report_chunks.user_id` 均可空;`report_chunks.embedding vector(1024)` 与标识列解耦(回填免 re-embed 的依据)。
- ui `sites.js:575-590`、`team.js:811-818` — 两处注释明确将 UUID↔slug 缺桥 parked 给 device-mgmt 批次;`org.js:27-32 folderName()` 是 B4 桥本体。
