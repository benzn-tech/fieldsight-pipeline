# 批次 B:报告侧写后端(Observations,Aurora)Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development,逐任务执行,步骤 `- [ ]`。
> Spec(用户已批):`docs/superpowers/specs/2026-07-06-observations-write-backend-design.md`。存储 = Aurora org 库(dashboard-first item store 第一批公民)。

**Goal:** Safety/Quality 的"新增观察"真正可用:提交 → Aurora → 刷新持久 → 与报告提取条目合并显示(Manual 徽章),KPI 并入。

**Architecture:** 后端 = migration 0006 + `repositories/observations.py` + `lambda_org_api.py` 4 路由(`/api/org/{proxy+}` 代理,**零网关改动**);前端 = org.js 4 函数 + 两个 create-modal 接线(站点来自 A2 的 `FS.siteContext`)+ compliance-aggregator 读合并。两仓**严格串行**:后端先上线冒烟,前端才动。

**Tech Stack:** 后端 Python/psycopg/pytest(**有真实测试套件,走 TDD**);前端无构建 React(node --check + Chrome)。

## Global Constraints(含"我的情况"预判)

- **并发部署铁律**(PR #10/#11 教训):pipeline PR 合并后**等 CloudFormation UPDATE_COMPLETE** 再做任何后续(migrate 调用/冒烟);绝不与其他 pipeline PR 并行合并。
- **id 生成**:pgcrypto 已启用(0001),`gen_random_uuid()` 可用——但**仍在 SQL DEFAULT 用它、Python 不手造 uuid**(与 users/sites 表一致)。
- **NZ 日期惯例**:`report_date` 默认值沿用代码库既有 `datetime.utcnow() + timedelta(hours=13)`(+13 固定,不区分 NZST——与 lambda_fieldsight_api 一致,一致性优先,勿"顺手修正")。
- **身份桥**:`site_slug`/`author_*` 是报告侧身份文本快照;**不做 FK 校验**(报告站点在 S3 user_mapping,不在 org 库)——只验非空;UI 从 getSites 取值,脏数据风险可控(spec 已记取舍)。
- **权限**:POST/GET = 任何已登录成员(worker 可上报;archived caller 由既有 dispatch 守卫拦);PATCH = author 本人或 admin/gm;archive = admin/gm。公司隔离一律 `caller["company_id"]`。
- **CORS/路由**:org lambda `ok()` 已带 GET,POST,PATCH,OPTIONS 头;`{proxy+}` 代理一切——**无网关/template.yaml 改动**(除非新增 IAM,本批不需要)。
- 后端测试模式照抄 `tests/unit/test_lambda_org_api.py`(importorskip psycopg、FakeConn、monkeypatch repositories、make_event)。
- 前端:mock 模式行为逐字节不变(modal 的 mock 提交路径保留;org.js mock 分支返回本地对象);**聚合器读合并对 Insights/战略页是故意生效的**(手动条目=真实事件,应进全局视图——与 A2 铁律不冲突,那条铁律管的是 site 过滤不管数据源)。
- **orgWrites=true 真落库**:最终 Chrome 验证我会创建 1 条冒烟观察并随手 archive(留档已归档,无污染)。
- 绝不 `git add -A`;pipeline 仓 CRLF 单行 anchor。

---

## 后端(fieldsight-pipeline,branch `feature/observations`,base develop)

### Task 1: migration 0006 + repositories/observations.py

**Files:** Create `src/migrations/0006_observations.sql`;Create `src/repositories/observations.py`。

**Interfaces (Produces,后续任务依赖,名字必须一致):**
- `observations.create_observation(conn, company_id, kind, site_slug, author_sub, author_name, observation, risk_level=None, recommended_action=None, report_date=None)` → dict(整行)
- `observations.list_observations(conn, company_id, kind=None, date_from=None, date_to=None, site_slug=None, include_archived=False)` → list[dict]
- `observations.get_observation(conn, company_id, obs_id)` → dict|None
- `observations.set_status(conn, company_id, obs_id, status)` → dict|None
- `observations.set_archived(conn, company_id, obs_id, archived)` → dict|None

- [ ] Step 1:写 `src/migrations/0006_observations.sql`(照 spec 的 DDL,风格对齐 0002:小写类型、REFERENCES companies(id)、两个索引;`report_date date NOT NULL`)。
- [ ] Step 2:写 `src/repositories/observations.py`——照抄 `repositories/sites.py` 的风格(psycopg 参数化、dict_row 或既有 row 转 dict 模式——**先读 sites.py 确认**取行方式)。所有查询 WHERE 带 `company_id = %s`;list 按 `report_date DESC, created_at DESC` 排序;`set_status` 顺带 `updated_at = now()`。
- [ ] Step 3:`python -m py_compile src/repositories/observations.py`;提交 `feat(observations): migration 0006 + repository module`。

### Task 2: lambda_org_api.py 4 路由(TDD)

**Files:** Modify `src/lambda_org_api.py`;Modify `tests/unit/test_lambda_org_api.py`。

**Interfaces:**
- Consumes:Task 1 的 repository 函数(签名如上)。
- Produces(HTTP 契约,前端依赖):
  - `POST /api/org/observations` body `{kind, site_slug, observation, risk_level?, recommended_action?, report_date?}` → 201 `{observation: {...整行}}`;400:kind 不在 {safety,quality} / observation 空 / site_slug 空 / risk_level 给了但不在 {low,medium,high}。
  - `GET /api/org/observations?kind=&from=&to=&site_slug=&include_archived=` → 200 `{observations: [...]}`。
  - `PATCH /api/org/observations/{id}` body `{status}` → 200 整行;400 status 非法;403 非 author 且非 admin/gm;404 不存在/跨公司。
  - `POST /api/org/observations/{id}/archive` → 200;403 非 admin/gm;404 同上。

- [ ] Step 1(**测试先行**):在 test_lambda_org_api.py 追加测试(照既有 make_event/wired fixture 模式,monkeypatch `org.observations.*`):
  ```python
  def test_create_observation_ok(wired):            # worker 角色也 201,author 取 caller
  def test_create_observation_bad_kind_400(wired):
  def test_create_observation_missing_text_400(wired):
  def test_list_observations_filters(wired):        # 断言 kind/from/to/site_slug 透传给 repo
  def test_patch_status_author_ok(wired):
  def test_patch_status_other_worker_403(wired):    # 非 author 的 worker 拒
  def test_patch_status_admin_ok(wired):            # admin 改别人的 → 200
  def test_patch_status_bad_value_400(wired):
  def test_archive_requires_admin_or_gm(wired):     # worker 403 / gm 200
  def test_observation_cross_company_404(wired):    # repo 返回 None → 404
  ```
  每个测试体完整可运行(monkeypatch 返回构造 dict;404 用例让 repo 返回 None)。
- [ ] Step 2:跑 `python -m pytest tests/unit/test_lambda_org_api.py -k observation -v` → 预期 FAIL(路由不存在)。
- [ ] Step 3:实现——`from repositories import observations`;dispatch() 里 404 之前加:
  ```python
  if route == "/observations":
      if method == "POST": return create_observation_endpoint(conn, caller, parse_body(event))
      if method == "GET":  return list_observations_endpoint(conn, caller, event)
  m_ob = re.match(r"^/observations/([^/]+)$", route)
  if m_ob and method == "PATCH":
      return patch_observation_endpoint(conn, caller, m_ob.group(1), parse_body(event))
  m_oba = re.match(r"^/observations/([^/]+)/archive$", route)
  if m_oba and method == "POST":
      return archive_observation_endpoint(conn, caller, m_oba.group(1))
  ```
  端点函数:校验(kind/observation/site_slug/risk_level/status 白名单常量 `ALLOWED_OBSERVATION_KINDS = {"safety","quality"}` 等)、`report_date` 缺省 `(datetime.utcnow() + timedelta(hours=13)).date().isoformat()`、PATCH 权限 `row["author_sub"] == caller["cognito_sub"] or caller["global_role"] in ("admin","gm")`(先 get_observation 拿行,None→404)、archive 权限 `("admin","gm")`。文件头路由注释表同步补 4 行。
- [ ] Step 4:`pytest -k observation -v` 全 PASS;全套 `pytest tests/unit/test_lambda_org_api.py -v` 无回归。
- [ ] Step 5:提交 `feat(observations): org API routes (create/list/patch-status/archive) + tests`。

### Task 3: PR → 部署 → migrate → live 冒烟(控制器亲自执行)

- [ ] Step 1:push + PR(base develop),PR 文案含 spec 链接。**用户合并**(权限门)。
- [ ] Step 2:**等 deploy run completed/success**(gh run watch;并发铁律:期间不合任何其他 pipeline PR)。
- [ ] Step 3:调用 migrate lambda 应用 0006(照 Phase 3b 的调用方式;Data API 查 `information_schema.tables` 确认 observations 表存在)。
- [ ] Step 4:live 冒烟(直接 invoke OrgApiFunction,伪造 authorizer claims,照 Phase 3 冒烟脚本模式):create(安全类,site_slug=sb1108-ellesmere)→ list 过滤命中 → patch status closed → 非 author worker patch 403 → archive → list 默认不含/include_archived 含。记录到账本。

---

## 前端(fieldsight-ui,branch `feature/observations-ui`,base dev;**Task 3 冒烟通过后才开工**)

### Task 4: org.js 4 函数

**Files:** Modify `scripts/api/org.js`;bump `?v=`。

**Interfaces (Produces):**
- `org.createObservation(body)` → POST(orgWrite 门控;mock:回显 body+id+created_at 的本地对象,**并 push 进模块级 `_mockObservations` 数组**供 mock 读取)
- `org.getObservations(opts)` → GET `{kind,from,to,site_slug}`(orgLive 门控;mock:过滤 `_mockObservations` 返回)
- `org.updateObservation(id, {status})` → PATCH;`org.archiveObservation(id)` → POST archive(mock 同步改/移 `_mockObservations`)

- [ ] Step 1:照 org.js 既有函数风格实现 4 个 + `_mockObservations = []` 模块级(mock 会话内持久,刷新即空——mock 语义够用);导出。
- [ ] Step 2:`node --check`;bump;提交 `feat(observations-ui): org.js observation api (mock-backed)`。

### Task 5: 两个 create-modal 接线

**Files:** Modify `scripts/composites/safety-create-modal.js`、`scripts/composites/quality-create-modal.js`;Modify `scripts/pages/safety.js`、`scripts/pages/quality.js`(siteId 传参处);bump 各 `?v=`。

**Interfaces:** Consumes T4 + `FS.siteContext.get()` + `FS.api.sites.getSites()`。
- 现状预判:modal 现有 real 路径 gate `!useMocks && !writeMocks` 发 POST 到**不存在的**报告网关端点(死路径);mock 路径本地添加。页面传 `siteId: state.user || fixtures.sites[0].site_id`(admin 时是 fixture 值——live 下是错的)。

- [ ] Step 1(先读两个 modal 全文):live 提交路径(`!useMocks` 且 `FS.api.org` 可用)改调 `org.createObservation({kind:'safety'|'quality', site_slug, observation, risk_level, recommended_action})`——字段名从 modal 现有表单字段映射(读文件对齐;quality 的字段若叫 issue/note 也映射到 observation/recommended_action,报告里注明)。成功 → 现有 onSuccess 回调链(toast+插行)保持,但插的行带 `source:'manual'`。失败 → toast error(含后端 error message)。mock 路径逐字节不变。死 presignedPut 引用不动(照旧被守卫短路)。
- [ ] Step 2:site 来源:页面传 `siteId: (window.FS.siteContext && window.FS.siteContext.get()) || null`(live);modal 内:`siteId` 为空且 live 时渲染**必选**站点 `<select>`(options 来自 `getSites()`,一次性 effect),未选禁用提交。mock 时保持原 siteId 逻辑。
- [ ] Step 3:`node --check` 四文件;bump;提交 `feat(observations-ui): create modals write to org observations (siteContext-aware)`。

### Task 6: compliance-aggregator 读合并 + Manual 徽章 + 状态操作

**Files:** Modify `scripts/api/compliance-aggregator.js`;Modify `scripts/composites/safety-flag-row.js`(或行渲染所在组件——**先读**定位徽章/状态位);Modify 详情面板文件(safety.js/quality.js 右栏);bump 各 `?v=`。

**Interfaces:** Consumes T4 `org.getObservations`。
- [ ] Step 1(**先读** getSafetyRange/getQualityRange 的行构造代码,拿到精确行形状——observation/risk_level/date/user_name/site/closed 等字段名):在两函数返回前追加:
  ```js
  try {
    var manual = await window.FS.api.org.getObservations({ kind: 'safety'|'quality', from: from, to: to, site_slug: opts.site || undefined });
    rows = rows.concat((manual.observations || []).map(toRowShape /* source:'manual', author 显示名, status→closed 布尔 */));
  } catch (e) { console.warn('[compliance] manual observations unavailable', e); /* 报告行照常返回,不炸页 */ }
  ```
  KPI 计数逻辑若在行数组之上自动派生则零改动(读码确认)。**注意**:此追加对 Insights/战略页同样生效——故意的(spec),注释写明。
- [ ] Step 2:行组件对 `row.source === 'manual'` 渲染小 Badge 'Manual'(照团队页 Archived 徽章用法)。
- [ ] Step 3:详情面板:manual 行显示作者 + "Mark closed/reopen" 按钮(可见性:author 本人或 admin/gm——author_sub 对比 `FS.session.user.sub`),调 `org.updateObservation` → 成功后触发页面 refetch(bump retry)。
- [ ] Step 4:`node --check`;bump;提交 `feat(observations-ui): merge manual observations into safety/quality reads + Manual badge + status action`。

### Task 7: Fable 终审 + PR + 部署 + Chrome 验证

- [ ] Step 1:ui 分支全量 node --check;整分支 diff → **Fable 5** 终审(镜头:①modal mock 路径逐字节不变;②读合并的 try/catch 不炸页 + Insights 故意生效的注释;③site_slug 来源闭环(siteContext→modal→POST→GET 过滤);④author 权限对比用 sub 不用名字;⑤_mockObservations 不泄漏到 live)。
- [ ] Step 2:修 → PR → 用户合并 → Amplify → Chrome 验证:选 SB1108 → Raise Observation(填一条冒烟数据)→ 提交 → Safety 列表出现带 Manual 徽章的行 + KPI+1 → 刷新持久 → Mark closed → 状态翻转 → archive(CLI)收尾留档。
- [ ] Step 3:账本 + memory 更新(批次 B 完成;Templates 上传另立;报告侧 Safety/Quality 写闭环达成)。

---

## 自审

- Spec 全覆盖:表(T1)、4 路由+权限矩阵(T2)、UI 三层(T4/5/6)、验证(T3/T7)。
- 预判落位:并发部署(T3 Step 2)、pgcrypto(已核查,沿用 DEFAULT)、NZ +13(T2 Step 3)、身份桥无 FK(Constraints)、死 presignedPut(T5)、fixture siteId 错值(T5 Step 2)、Insights 故意生效(T6 注释)、orgWrites 真落库(T7 冒烟数据 archive 收尾)。
- 接口一致性:repository 五函数名贯穿 T1/T2;org.js 四函数名贯穿 T4/5/6;`source:'manual'` 贯穿 T5/6。
- TDD:后端 T2 测试先行(有真实 pytest);前端无套件按惯例 node --check + Chrome。
- 串行门:T3 冒烟过 → 才开 T4;两仓 PR 各自独立合并,pipeline 侧绝不并行。
