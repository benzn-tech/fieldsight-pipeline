# Dashboard 批次:4b Live 徽章 + 4c leg-1 聚合 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development。
> 用户指定"4b/4c dashboard UI"。4b=Live 徽章(后端 is_live 已发,纯 UI);4c=leg-1 确定性聚合(后端净新 + strategic dashboards 接线)。leg-2 例外规则/leg-3 LLM 叙事留 4c 完整批。

**Goal:** ①运营数据带 Live 徽章展示(镜像现有 Manual 徽章);②Sprint 9 的 Portfolio/Executive strategic dashboards 接真实每站点聚合(open safety/quality/actions/topics 计数 + 红黄绿状态),替代 mock。

**两组 PR:** pipeline(4c rollup 后端)先行;ui(4b Live 徽章 + 4c strategic 接线)随后。

**Tech Stack:** org-api Python(in-VPC,新 GROUP BY SQL);无构建浏览器 React;pytest。

## Global Constraints

- **ACL 铁律**:rollup 端点复用 list_live_items 的 accessible_site_ids 模式(admin/gm 全公司站点;余者仅 memberships);`WHERE site_id = ANY(%s)`;跨公司零泄漏;空 site_ids→空结果。
- **4c leg-1 只做确定性 SQL**:无 LLM、无叙事、无物化。例外状态(红黄绿)由纯规则派生(高危未关闭 safety→红;任何未关闭 exception→黄;无→绿)。leg-2/3 出界。
- **observations 表按 site_slug(text)非 site_id(uuid)**:rollup 合并手动 observations 需 slug↔site_id 桥(v1:rollup 主体走 site_id 的 topics/action_items/safety_observations;manual observations 若纳入,按 site_slug 分组后由 UI 侧或端点侧用 org sites 的 name/slug 映射——v1 可**先只聚合 report-extracted 的 safety_observations/action_items/topics**,manual observations 纳入留注释,避免身份桥复杂度)。
- **UI 铁律**:无构建;tokens.css/fs-globals.js 双镜像;改 js/css 必 bump `?v=N`(app-shell-preview + components-preview 两处);BEM;theme var(--token);`:focus-visible`;reduced-motion;聚合器铁律(site 过滤显式 opts.site,不读 FS.siteContext);node --check;绝不 `git add -A`;单行 Edit 锚。
- **4b Live 徽章镜像 Manual**:compliance-aggregator 的 manual 合并(.concat + try/catch 韧性,失败不拖垮主行)+ safety-flag-row 的 Badge 分支——Live 用 tone 'info',source:'live'。UI 文案英文(NZ 受众)。
- **4c 接线不新建页面**:strategic-aggregator 的 getProjectRollup 在 live 模式调 rollup 端点取计数,merge 站点元数据(name/region/client/value 仍来自 org sites/fixtures),保持 RollupTable 现有 row keys 不变(zero UI 结构改动)。
- 端点返回 JSON-safe(uuid→str,default=str——4d C1 教训)。

---

### Task 1(pipeline): rollup repo 计数 + GET /api/org/rollup/portfolio(TDD)

**Files:** Create `src/repositories/rollup.py`;Modify `src/lambda_org_api.py`(dispatch + handler);Modify `tests/unit/test_lambda_org_api.py`。

**Interfaces (Produces):**
- `rollup.portfolio_counts(conn, site_ids) -> dict[site_id_str -> counts]`:一组 GROUP BY 查询(每对一表)——
  - `safety_observations`:`site_id, count(*) FILTER (WHERE status='open') AS open_safety, count(*) FILTER (WHERE status='open' AND risk_level='high') AS open_high_safety GROUP BY site_id`(WHERE site_id=ANY)。
  - `action_items`:`open_actions=count FILTER status='open'`、`total_actions=count(*)`、`overdue=count FILTER (status='open' AND deadline < CURRENT_DATE)` GROUP BY site_id。
  - `topics`:`topics_count=count(*)`、`participants=count(DISTINCT user_id)` GROUP BY site_id(可加 report_date 范围参数;v1 全量或近30天——取 `WHERE report_date >= CURRENT_DATE - 30`)。
  - 合并成 `{str(site_id): {open_safety, open_high_safety, open_actions, total_actions, overdue_actions, topics_count, participants}}`;缺失站点补零。
  - str() 所有 site_id 键(uuid vs str 教训)。
- org-api:`GET /api/org/rollup/portfolio` → `list_portfolio_rollup(conn, caller, event)`:accessible_site_ids → rollup.portfolio_counts → 每站点加 `status`(open_high_safety>0→'red';(open_safety>0 or open_actions>0 or 未关闭quality)→'yellow';else→'green')→ `ok({"sites": [{site_id, ...counts, status}]})`。
- dispatch:`if route == "/rollup/portfolio" and method == "GET"`(注意 route 匹配含斜杠子路径——确认现有 dispatch 的 route 正则允许 `/rollup/portfolio`;若 if-ladder 精确匹配则直接加该 route 字符串分支)。

- [ ] 测试先行(FakeConn 返回 canned GROUP BY 行;caller admin/worker):admin 全站点 vs worker memberships 收窄 / 空 site_ids→空 sites / 计数正确聚合(每站点合并三查询)/ status 派生(高危→red、有 open→yellow、全零→green)/ 缺数据站点补零 / site_id 全 str。
- [ ] 实现 rollup.py(net-new GROUP BY,用既有 idx_*_site_status 索引)+ handler + dispatch。
- [ ] 全套 pytest 零回归(基线 202);提交 `feat(4c): rollup portfolio counts + GET /api/org/rollup/portfolio (leg-1 SQL aggregation)`。

### Task 2(pipeline): Fable 终审 → PR → 部署(4c 后端)

- [ ] 整分支 diff → Fable 5 终审(镜头:ACL 收窄/跨公司隔离、GROUP BY 正确性与 FILTER、status 规则、site_id str、空 site_ids、JSON-safe、route 匹配含子路径、overdue 用 CURRENT_DATE 的 NZ 语义)。修→复审。
- [ ] PR(base develop)→ 用户合并 → 部署 success → 冒烟:admin GET /rollup/portfolio → 各站点计数;worker → 仅其站点。

### Task 3(ui): 4b Live 徽章(compliance 合并 + Badge)

**Files:** Modify `scripts/api/org.js`(getLiveItems)、`scripts/api/compliance-aggregator.js`(合并 live)、`scripts/composites/safety-flag-row.js`(+quality 对应行,Live Badge)、preview HTML busters。

- [ ] org.js:`getLiveItems({date})` 镜像 getObservations(orgLive 门 + mock 返回 `{topics:[]}`);挂 window.FS.api.org。
- [ ] compliance-aggregator:getSafetyRange/getQualityRange 末尾 try/catch 合并 live——对范围内每日 getLiveItems,取 is_live 的 topics 的 safety_observations(safety)/ topics(category=quality)映射为行,`source:'live'`,`.concat` 于报告行后(镜像 manual 合并的韧性模式,org 失败不拖垮)。行 id 唯一避开 dedupe。
- [ ] safety-flag-row.js(及 quality 对应 row 组件):在 Manual Badge 分支旁加 `flag.source === 'live' ? Badge(tone:'info', variant:'subtle', 'Live') : null`。
- [ ] node --check;busters;提交 `feat(4b): live-items merge + Live badge (mirrors manual pattern)`。

### Task 4(ui): 4c strategic dashboards 接真实聚合

**Files:** Modify `scripts/api/strategic-aggregator.js`(getProjectRollup live 路径)、`scripts/api/org.js`(getPortfolioRollup getter)、preview HTML busters。

- [ ] org.js:`getPortfolioRollup()` → orgLive 门 → `api.orgRequest('/rollup/portfolio')` 返回 `{sites:[...]}`;mock 返回 `{sites:[]}`(触发现有 fixture 回退)。
- [ ] strategic-aggregator.getProjectRollup:live 模式(orgLive)→ 调 getPortfolioRollup 取每站点计数(open safety/quality/actions/topics/overdue/status),**merge** 站点元数据(name/region/client/project_value/team_size 仍来自 getOrgSites/fixtures),映射为现有 project row 形状(site_id, name, safety_count=open_safety, safety_high=open_high_safety, quality_count, action_total, action_done=total-open, action_overdue, completion_rate, health 由 status 映射 A/B/C/D 或直接给 status, trend 可空/保留 fixture)。保持 RollupTable/projectColumns 现有 keys 不变。mock 模式保留现有 fixture 派生。envelope 守卫。
- [ ] node --check;busters;提交 `feat(4c): strategic dashboards consume real portfolio rollup (leg-1)`。

### Task 5(ui): Fable 终审 → UI PR

- [ ] 整 ui 分支 diff → Fable 5 终审(镜头:4b Live 合并韧性/mock 不网络/Badge token;4c getProjectRollup live/mock 双路径、row keys 不破 RollupTable、站点元数据 merge 正确、health/status 映射、envelope 守卫、聚合器铁律、busters 两处、UI 文案英文)。修→复审。
- [ ] PR(base dev)→ 用户合并。

## 自审
- 4b(T3)后端已就绪纯 UI 镜像 Manual;4c(T1-T2 后端 leg-1 + T4 接线不新建页面)。
- 接口一致:getLiveItems(T3)↔ live-items 端点;getPortfolioRollup/rollup.portfolio_counts(T1/T4);row keys 契约(T4↔RollupTable)。
- 预判:ACL 收窄、site_id str(uuid 教训)、JSON-safe(4d C1)、observations slug 桥 v1 规避、route 子路径匹配、busters 两处、聚合器铁律、UI 英文、leg-2/3 出界。
