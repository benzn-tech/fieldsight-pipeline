# UI 批次:Search+Ask / Admin 返回 / 历史天气 / Programme 上线 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development。
> 用户 4 项(2026-07-08)。F1/F2/F3 纯前端(fieldsight-ui dev);F4 = 后端(fieldsight-pipeline org-api + S3)+ 前端。

**Goal:** ①搜索框内联 Ask(无结果→回车直接问,不跳走);②admin 查看他人时稳健返回钮;③天气跟随选中日期(历史用 Open-Meteo archive,今天/无日期用实时);④Programme 真上线:S3 持久化、可编辑、按项目分、空态+上传。

**两个仓两组 PR:** pipeline(F4 后端:org-api programme GET/PUT + S3 + IAM)先行;ui(F1/F2/F3 + F4 前端接线)随后。

**Tech Stack:** 无构建浏览器 React(window.FieldSight.*);org-api Python(in-VPC,S3 gateway 可达);pytest;Open-Meteo(无 key fetch)。

## Global Constraints

- **铁律**:纯浏览器无构建(禁 npm/webpack);tokens.css 与 fs-globals.js 双镜像手改;改 .js/.css 必 bump 对应 HTML 的 `?v=N`;BEM `.fs-{block}__{el}--{mod}`;theme 用 `var(--token)` 不硬编码 hex,status token 不 theme-flip 要 pin 前景;`:focus-visible` 不用 `:focus`;reduced-motion 每个动画都要 override;node --check 每个改动 js;绝不 `git add -A`;单行 Edit 锚(CRLF)。
- **聚合器铁律**:site 过滤=显式 opts.site,聚合器体不读 FS.siteContext(除非该页本就全局)。
- **F4 后端**:org-api 已 in-VPC 且 S3 gateway 可达(org-assets presign 先例);programme 存 `programmes/{site_slug}/programme.json`;ACL:读=caller 可访问该 site,写=`programme:manage` 角色(admin/gm/pm);404→前端空态。org-api IAM 现仅授 org-assets/* 的 S3 → 需加 programmes/* 读写。
- **F4 保存语义**:显式"保存/推送"按钮(非每键自动存)——匹配用户"改动保存推送";PUT 整个 programme JSON。乐观并发 v1 不做(单公司低并发),但写入带 updated_at,读回展示。
- **F3**:Open-Meteo `https://archive-api.open-meteo.com/v1/archive`(历史,start_date=end_date=选中日,daily=temperature_2m_max/min,weathercode,windspeed_10m_max)与 `https://api.open-meteo.com/v1/forecast`(current_weather);普通 `fetch()`(不走 FS.api.request 的 Cognito 机制);失败回退现有 mock;站点无坐标回退默认(基督城)。数据主权:仅站点经纬度发往 open-meteo(非敏感)。
- **不做**:programme↔每日数据交互(单独设计,dashboard-first 跟进);weather 预报多日;Ask 跨公司(ACL 后端已管)。

---

### Task 1(pipeline): org-api programme 路由 + repo + 测试(TDD)

**Files:** Create `src/repositories/programme.py`;Modify `src/lambda_org_api.py`(dispatch + 2 handler);Modify `tests/unit/test_lambda_org_api.py`。

**Interfaces (Produces):**
- `programme.read_programme(s3, bucket, site_slug) -> dict | None`(GET S3 `programmes/{slug}/programme.json`;NoSuchKey→None)。
- `programme.write_programme(s3, bucket, site_slug, doc) -> dict`(PUT;doc 加 `updated_at`(NZ +13 约定)后写;返回写入的 doc）。
- org-api:`GET /api/org/programme?site=<slug>` → 读;`PUT /api/org/programme?site=<slug>`(body=programme JSON)→ 写(角色门)。

- [ ] 测试先行(FakeS3/FakeConn/caller 风格,照 test_lambda_org_api):GET 命中返回 doc / GET miss 返回 `{"programme": null}` 200(非 404,前端空态友好)/ ACL:caller 无该 site 访问 → 403 / PUT 角色门(worker→403,pm/admin→200)/ PUT 写对 key 且注入 updated_at / site 参数缺失→400。
- [ ] 实现:programme.py(S3 get/put,json,NoSuchKey 守卫);dispatch 加 `if route == "/programme"`(GET/PUT 分支);ACL 复用现有 site 可访问性判断(镜像 observations/live-items 的 company+membership 校验);写门用现有角色判断。**org-api 的 s3 client**:确认 org-api 已有 s3 client(org-assets presign 用)——复用;若无则加惰性 client。
- [ ] 全套 pytest 零回归;提交 `feat(programme): org-api GET/PUT programme (S3-backed, ACL)`。

### Task 2(pipeline): IAM——org-api 加 programmes/* S3 权限

**Files:** Modify `src/template.yaml`(OrgApiFunction Policies)。

- [ ] OrgApiFunction 的 S3 statement 现限 `${DataBucketName}/org-assets/*` → 增 `${DataBucketName}/programmes/*`(GetObject+PutObject)。**桶**:确认 org-api 的 S3_BUCKET 是哪个(org-assets 存 DataBucketName=test 桶还是数据湖?查现值)——programme 用同桶。
- [ ] `sam validate --lint`(容忍 W2531/W1001);提交 `feat(programme): org-api IAM for programmes/* s3`。

### Task 3(pipeline): Fable 终审 → PR → 部署(F4 后端)

- [ ] 整分支 diff → Fable 5 终审(镜头:ACL 读写门、跨公司隔离、S3 key 注入安全、NoSuchKey 守卫、桶正确、updated_at 注入)。修→复审。
- [ ] PR(base develop)→ 用户合并 → 部署 success。冒烟:PUT 一个最小 programme(admin sub)→ GET 读回一致;worker PUT→403。

### Task 4(ui): F1 搜索框内联 Ask

**Files:** Modify `scripts/composites/search-palette.js`(doAsk → 内联)、可能 `scripts/composites/ask-chat.js`(全局模式复核)、`styles/composites.css`、`app-shell-preview.html`(buster)。

- Consumes:AskChat(props date/user/scope/topic_id/initialQuestion;topic_id 可 null;date 可空——后端 Phase 5 已放宽);ask.js `FS.api.ask.ask({question,...})`。
- [ ] doAsk 改为**在面板内切到 Ask 模式**:选 ask 行(回车)→ 面板内容替换为 `AskChat`(compact,initialQuestion=query,无 topic_id、无 date=全局 RAG),带"← 返回搜索"退回;不再 Router.navigate + sessionStorage 跳转。保留现有 ask 行渲染。
- [ ] 复核 ask.js:无 date/topic_id 时走全局 RAG(后端 caller_sub 由 ApiFunction 注入;确认 mock 模式也返回合理占位)。
- [ ] node --check;buster;提交 `feat(ask): inline RAG chat in search palette (no navigation)`。

### Task 5(ui): F2 admin 稳健返回切换

**Files:** Modify `scripts/pages/timeline.js`、`app-shell-preview.html`(buster)。

- [ ] 现有 "View another user ↺"/"← All people on this site" 切换 + 选人卡的 history.back():改为**URL 驱动稳健返回**——查看他人(?user= 存在且 admin)时始终显示"← 返回全部/返回总览",点击 `Router.navigate('/timeline?date=&site=')`(丢 ?user=,不用 history.back());选人卡的返回同样改 URL 式。保持双向(总览↔某人)灵活。
- [ ] node --check;buster;提交 `fix(timeline): robust url-based back from admin user view`。

### Task 6(ui): F3 天气跟随日期(Open-Meteo)

**Files:** Modify `scripts/mock/sites.fixture.js`(加坐标)、`scripts/app-shell.js`(WeatherIndicator 读日期+站点+fetch)、`styles/app-shell.css`(如需)、`app-shell-preview.html`(buster)。

- [ ] sites.fixture 每站加 `coord: {lat, lng}`:sb1108-ellesmere 基督城 `{-43.5321, 172.6362}`;mpi 奥克兰 `{-36.8485, 174.7633}`;sb1131-northbrook-wanaka `{-44.7032, 169.1321}`。
- [ ] WeatherIndicator:读当前路由 `?date=`(FS.Router params)+ FS.siteContext.get() → 坐标;`selectedDate` 为过去(< 今天 NZDT)→ `archive-api.open-meteo.com/v1/archive?latitude=&longitude=&start_date=&end_date=&daily=temperature_2m_max,temperature_2m_min,weathercode,windspeed_10m_max&timezone=Pacific/Auckland`;否则(今天/无日期/未来)→ `api.open-meteo.com/v1/forecast?...&current_weather=true`。plain fetch,失败/无坐标 → 现有 mock。加载态 + 显示"历史(日期)"vs"实时"标签。weathercode→图标/文案映射(小表)。BUG-19:日期比较用 UTC 算术,别 new Date('YYYY-MM-DD')。
- [ ] node --check;buster;提交 `feat(weather): follow selected date via Open-Meteo (historical + realtime)`。

### Task 7(ui): F4 Programme 空态 + 按项目 + 接后端保存

**Files:** Modify `scripts/pages/programme.js`、`scripts/api/programme.js`、`app-shell-preview.html`(buster)。

- [ ] api/programme.js:`getProgramme` 改为读 org-api `GET /api/org/programme?site=<activeSite>`(orgLive 门;mock 保留);新增 `saveProgramme(site, doc)` → `PUT /api/org/programme?site=`(envelope 守卫)。
- [ ] programme.js:去掉硬编码 `DEFAULT_PROGRAMME_ID`,改按 `FS.siteContext.get()` 取 programme;无 site→提示选项目;GET 返回 `{programme:null}`→**空态卡**("本项目暂无 Programme")+ 醒目"上传 Programme"按钮(复用现有 ProgrammeImportModal)+ "+ 新建任务"。
- [ ] 导入/编辑后:现有 ctx.replaceTasks/updateTask/addTask 内存 reducer 之上,加**"保存/推送"按钮**(canWrite 门)→ saveProgramme(site, 当前 parents+leaves)→ 成功 toast;未保存改动标记(dirty)。读回以 S3 为准。
- [ ] node --check;buster;提交 `feat(programme): per-site empty state + upload + save to S3 backend`。

### Task 8(ui): Fable 终审 → PR(F1-F4 前端一并)

- [ ] 整 ui 分支 diff → Fable 5 终审(镜头:F1 内联 ask 不破坏搜索/回退、mock 路径不网络;F2 URL 返回覆盖深链;F3 Open-Meteo 失败回退、坐标缺失、BUG-19 日期、无 CSP 但外呼合规、加载态;F4 空态/dirty/保存 envelope 守卫、按项目键、canWrite 门、XSS(programme 名/任务名经 escape);token 存在性;buster 已 bump)。修→复审。
- [ ] PR(base dev)→ 用户合并。

## 自审
- 4 功能全覆盖:F1(T4)、F2(T5)、F3(T6)、F4(后端 T1-T3 + 前端 T7);终审分仓(T3 pipeline / T8 ui)。
- 接口一致:programme.read/write(T1)↔ api getProgramme/saveProgramme(T7);GET 返回 {programme:null} 空态契约贯穿 T1/T7;coord 契约(T6)。
- 预判:org-api 桶/IAM 现值需查(T2)、ask.js 全局模式复核(T4)、BUG-19 日期(T6)、programme 按 site 键(T7 去硬编码)、保存显式非自动、每日数据交互出界留后续。
