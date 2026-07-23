# FieldSight 架构评审报告：Dashboard 优先 + 上场会议要点即时上 Today

> 本报告基于对 `fieldsight-pipeline` 与 `fieldsight-ui` 真实代码的逐行核验。综合方案整体正确且扎根于代码，但对抗性评审发现了若干被当成"现成可用"的真实代码缺陷——这些缺陷我已逐条复核确认属实，并在下文修正。所有 file:line 引用均已实测。

---

## 1. 结论速览（TL;DR）

- **基线选 Angle C（拆分 digest 写入 + Today 快路径，保留现有 pipeline 不动），嫁接 Angle A 的"增量物化 + `GET /api/today` 乐观流"和 Angle B 的"投影保真 + 不可变冻结"纪律。** 因为 `feature/p2-dashboard-digest-qaqc-realtime` 分支已实现了约 80% 的机械结构，这是一次**重新接线（re-wiring），不是重写**。
- **反转的核心一招（Q2）：让 `ItemsTable` 的 `ITEM#` / 新增 `DEADLINE#` / `TODAY#` 行成为"第一权威提交"，而 `daily_report.json` / `.docx` 降级为按需/每日一次的"投影导出"用于追责。** 现状是 `daily_report.json` 文件本身**就是** dashboard payload（`get_timeline` 逐字返回它）——反转就是把这层关系倒过来。
- **关键低风险决策：`GET /api/timeline` 的响应体保持字节兼容的 `daily_report` 字典形状不变**，于是 `today-adapter.js`、`timeline.js`、三个战略 dashboard **零改动**，内部读源在 Phase 3 才悄悄切到 item store。
- **Q3 时效性：当前 meeting-end → 上 Today 被两个每日 cron 边界卡住（不是算力）**——20:00 拉取 + 05:00 报告 cron，最坏约 20 小时。同日快赢：落地 p2 的 `AUTO_REPORT` 钩子 + 缩短 orchestrator cron + 新增基于 ledger 的乐观 `GET /api/today`。
- **⚠️ 但 p2 的 `AUTO_REPORT` 钩子"按原样不工作"——这是已核实的 blocker。** 它发的 payload key 是 `users_filter`，而 handler 只读 `event.get('user')`（`lambda_report_generator.py:1802`）；且它发的是空格形用户名 `[user.replace('_', ' ')]`，而 handler 用下划线形 S3 文件夹名做交集（`:1212-1218`）。**双重不匹配**：要么全用户重算，要么"无匹配用户、根本不生成"。Quick Win 1 必须含一个**必需的后端代码修复**，不是干净 cherry-pick。
- **三个必须正视的硬约束（评审命中、我已复核）：** (1) **BUG-18 会议清单竞态**——`AUTO_REPORT` 在每个 COMPLETED 上无条件触发，可能在 meeting-minutes 写 `.meeting_manifest.json` 之前就把会议转录吞进日报；(2) **多段转录导致冗余 Claude 调用**——freshness 门只在 `recordings_processed == 当前转录数` 时跳过（`:1168`），录音被 VAD 切成多段、各段在不同时刻 COMPLETED，计数一直变、永不匹配，每个事件触发一次全天重算；(3) **管理层 summary 不刷新**——单用户 `AUTO_REPORT` 走过 `:1412` 的 `if combined_transcripts and not users_filter` 守卫被跳过，admin/gm 读的 `summary_report.json` 保持陈旧。

---

## 2. 现状：报告优先的链路（report-first，已实测）

### 2.1 生成端：报告文件 = dashboard payload

`generate_daily_report`（`lambda_report_generator.py:1200`）对每个用户构建报告字典（`:1361-1378`，含 `executive_summary`、`topics`、`_report_metadata`），然后**只写 S3 文件**：

- `daily_report.json` put 在 `:1380-1385`
- `.docx` put 在 `:1388-1399`（`generate_word_document`）
- `summary_report.json`（综合）在后面的 combined 块（受 `:1412` 守卫）

**DynamoDB item store 是死代码**：`write_items_to_dynamodb`（`:765-792`）第二行 `if not ENABLE_DYNAMODB: return`（`:766-767`）直接返回，`ENABLE_DYNAMODB` 默认 false。`template.yaml:333-335` 接入了 `ITEMS_TABLE` / `REPORTS_TABLE` / `AUDIT_TABLE` 三张表名、`:341-346` 授予 3× `DynamoDBCrudPolicy`，**但没有 `ENABLE_DYNAMODB` 变量**。`ROADMAP.md` 亦确认"无数据写入"。

> 实测补充：即使开了 `ENABLE_DYNAMODB`，`write_items_to_dynamodb`（`:765-792`）**只写 `ITEM#` 行**——没有任何 `DEADLINE#` 写入。而 p2 的 `get_site_dashboard` 会 query `SK begins_with('DEADLINE#')`。**这意味着 deadline 面板在开启 DynamoDB 后仍会静默为空**，除非 Phase 1 补上 `DEADLINE#` 写入。这是代码里实打实的洞。

### 2.2 读端：报告文件被逐字返回

`get_timeline`（`lambda_fieldsight_api.py:219-262`）对 `daily_report.json` 做原始 `s3:GetObject` 并逐字返回；找不到则返回 404 "No report"（`:262`）。admin/gm 优先读 `summary_report.json`（`:241-247`）。**角色隔离当前是"免费"的**——靠 `reports/{date}/{user}/` 路径里的 `{user}` 文件夹（`:254-262`，对应 BUG-25 site-manager 隔离）。

UI 端 `today-adapter.js`（`scripts/api/today-adapter.js`，注意是 `api/` 不是 `composites/`）把该 `daily_report` 映射成 Today 形状。所有 Insights/Strategic 聚合在浏览器端做（`insights-aggregator.js` / `strategic-aggregator.js`）。

**耦合本质：报告文件即真相源（source of truth），dashboard 是它的客户端派生。** 这正是要反转的。

---

## 3. Q2 — 反转为 Dashboard 优先、报告按需

### 3.1 目标数据模型（真相源 = ItemsTable）

| 行类型 | Key 设计 | 内容 | 状态 |
|---|---|---|---|
| `ITEM#` | PK=`SITE#{site_id}#DATE#{date}`（`:770`）, SK=`ITEM#{start_time}#{topic_id}`（`:775`） | topic_title/category/time_range/participants[]/summary/key_decisions[]/action_items[]/safety_flags[]/related_photos[]/hidden（`:776-790`） | 已有写入逻辑（死） |
| `ITEM#` 新增字段 | — | `materialized_at`、`source_transcript_keys[]`（幂等去重）、`generated_at`、`site_name` | **新增** |
| `DEADLINE#` | SK=`DEADLINE#{...}` 源自 `critical_dates_and_deadlines` | deadline 项 | **新增（必需，否则面板空）** |
| `TODAY#` 聚合行 | 每 site/date（建议也含 user 维度） | `executive_summary[]`、`safety_observations[]`、`critical_dates[]`、rollup 计数（topic/safety/action）、`generated_at` | **新增（一次 GetItem 出 Today 头 + 日历点）** |
| Transcript ledger | `fieldsight-transcripts`，`TRANSCRIPT_TABLE` | status: transcribing→pending→**reported(?)** | 已有，作 `/api/today` 乐观读源 |
| S3 报告文件 | `reports/{date}/{user}/{daily_report.json,.docx}` | **降级为投影/导出**；`.docx` 冻结快照用于追责 | 投影产物 |

> ⚠️ **ledger 的 `'reported'` 终态写入器不存在**——已实测：全 `src/` 搜不到任何写 `'reported'` 的代码。callback 只写 `'pending'`（`lambda_transcribe_callback.py:130`）。`/api/today` 的乐观状态机（transcribing→pending→reported）依赖某处把它翻成 `reported`，否则**processing 卡片永远不消失**。必须先实现这个转换（应在 report_generator 成功 put 之后写）。

### 3.2 后端改动（文件级，最低风险）

1. **开启 store（1 行 + 验证）**：`template.yaml:325-335` 的 `ReportGeneratorFunction.Environment.Variables` 加 `ENABLE_DYNAMODB: 'true'`。Policy 与表名已接入，激活后 `write_items_to_dynamodb` 开始填充（影子，无人读，安全）。
2. **让 item 行成为首要提交 + 重排**：把 `write_items_to_dynamodb`（当前 `:1402`，在 S3 报告/docx put **之后**）移到 `daily_report.json` put（`:1380`）**之前**；并扩展它写 `DEADLINE#` 行（源自 `critical_dates_and_deadlines`）和一个 `TODAY#` 聚合行。
3. **报告降级为投影**：把单体写入块（`:1344-1409`）拆成 `build_digest()`（主内容）和 `project_report_from_items(site,date,user)`（从 item 行渲染**字节兼容**的 `daily_report.json` / `summary_report.json` / `.docx`）。**完全照搬已在生产验证的 `lambda_meeting_minutes.convert_to_daily_report_format` 模式（`:760-877`，含 owner→responsible 映射在 `:802`）。** 投影必须复现确切的报告字典形状（`:1361-1378`，尤其 `executive_summary` + `_report_metadata`），否则 `today-adapter` 静默崩。
4. **读端切到 item store**：在 Phase 3 落地 p2 的 `get_site_dashboard`（`GET /api/dashboard`）和新 `GET /api/today`；`get_timeline` 在过渡期保持返回 `daily_report.json`（带 item-backed 回退）。

### 3.3 为什么这是用户想要的反转

生成现在**首先瞄准 dashboard 内容**（`ITEM#` / `TODAY#` / `DEADLINE#` 行），报告渲染是**第二位**、按需投影。`/reports` 归档页已把 `.docx` 当只读导出（`reports.js`），**零改动**——它本来就是次级追责面。因 `daily_report.json` 保持字节兼容投影，反转在底层发生时**无 UI 契约破裂**。

> **Angle B 纪律（关键）**：`.docx` / JSON 导出从**可变** item 行渲染（QA/QC 可经 `hidden` 标志隐藏/编辑），所以追责导出**必须在投影时刻冻结**（写一份按 `generated_at` 键控的不可变副本），否则下载过的报告会被后续编辑追溯性改写。

> **executive_summary 类型修正（评审命中，已实测）**：现状 `executive_summary` 默认是**字符串**（`:1361` `get('executive_summary','')`），不是数组。UI 已防御性兼容两者（`today-adapter.js:113-115` 的 `Array.isArray` 检查）。`.docx` 渲染器把它当字符串段落（`generate_word_document`，`:877` `report_data.get('executive_summary', 'No summary available')`）。**若投影要"钉死为数组"，必须同时改 `:877` 让它 join 数组，否则冻结的 `.docx` 导出会坏。**

### 3.4 评审补充的反转范围（综合方案漏了）

**weekly/monthly 必须纳入范围**：weekly/monthly 报告走独立 cron（`template.yaml` 的 FRI / 月末 L 调度），它们**读 daily 报告文件**做汇总。一旦 daily 变成 item 行的投影，这两个生成器必须重新指向投影输出或 item store。综合方案完全没提它们——这是一个真实遗漏。

---

## 4. Q3 — 让上一场会议要点尽快上 Today

### 4.1 当前延迟时间线（已实测，瓶颈是 cron 边界不是算力）

```
[会议结束]
   │  ⏳ 边界#1（最大）：每日 20:00 NZDT 拉取
   │     orchestrator cron(0 7 * * ? *)（template.yaml:114, 经 !Ref 接 :248）
   │     早上的会要等 ~11h 才能进入 pipeline（无 RealPTT webhook）
   ▼
[进入 pipeline]  ← 这一段是事件驱动且快的：
   S3 ObjectCreated → VAD → Transcribe Lambda(StartTranscriptionJob 异步)
   → EventBridge 状态变更 → transcribe_callback.handle_completed
   → ledger 翻成 'pending'（lambda_transcribe_callback.py:122-141）
   主要算力 = AWS Transcribe 异步作业（~0.2-0.5× 音频时长，不可约）
   │  ⏳ 边界#2：Today 读的产物（daily_report.json）只由
   │     05:00 NZDT 报告 cron 产出（template.yaml:351, cron(0 16 * * ? *)）
   │     且 target_date 默认 get_yesterday_date()（:1797）——目标是"昨天"
   ▼
[上 Today]  读路径 ~0 延迟但 ~0 新鲜度：
   get_timeline 要么 GET 到文件，要么返回 404（:262）；
   等待期间 today.js 静默回退到更旧的一天（today.js:196-233）
```

**最坏 ~20h，几乎全是批处理等待。**

> 实测加强 Quick Win 5：`today.js:193-233` 在"今天无报告"时，调 `getDates` → `findLatestReportDate` → `loadFor(latest, true)`，然后 **`setState` 直接置 `status: 'ok'`**——它**没有**渲染 "Latest available" 横幅，所以 UX 比综合方案描述的更隐蔽：用户会以为旧的一天就是"今天"。

### 4.2 让上场会议要点快速上 Today 的具体改动

**(1) 落地 p2 `AUTO_REPORT` 钩子 —— 但必须先修两个 blocker。**
钩子本体已验证存在于 `feature/p2-dashboard-digest-qaqc-realtime`（callback `handle_completed` 之后，约 `:146-163`）：COMPLETED 时异步 `InvocationType='Event'` 调报告生成器。这能整段消除边界#2。**但实测两处缺陷使"只重算新用户"的幂等故事双重失效：**

- **Blocker A — payload key 不匹配**：callback 发 `'users_filter': [...]`，handler 在 `:1802` 读 `event.get('user', None)`，**从不读 `users_filter`**。结果 `users_filter=None`，对该日期**全用户**做全天 Claude 重算。
- **Blocker B — 用户名形不匹配**：callback 发 `[user.replace('_', ' ')]`（如 `'Ben Lin'`），但 handler 从 S3 转录文件夹名构建用户集（`:1212-1215`，下划线形如 `'Ben_Lin'`），`users = users & set(users_filter)`（`:1218`）会**为空** → "No matching users found"（`:1220`）→ **该新转录根本不生成报告**。

**必需修复（Phase 0）**：把 callback payload key 改成 `'user'`（或让 handler 兼读 `users_filter`/`user`），**并**规范化用户名到转录文件夹形（不要 `replace('_',' ')`，或对两种变体大小写不敏感匹配）。加集成断言：单转录 → 恰好该用户的 `daily_report.json` 刷新。**把"已验证 ~20 行干净 cherry-pick"降级为"~20 行 cherry-pick，按原样不工作，需 key+name 修复"。**

**(2) 乐观 `GET /api/today`（从 ledger）**：ledger 已跟踪 transcribing→pending。暴露它，让 Today 在媒体落地瞬间显示 "录音已上传 → 转录中 → 总结中 → 就绪" 卡片，替换 404 和陈旧回退。感知延迟 ~0。**前提**：先确认/实现 `'reported'` 终态写入器（见 3.1），否则卡片不消失。

**(3) 增量物化（嫁接 Angle A）**：让 `AUTO_REPORT` 路径只 append/merge 新会议的 `ITEM#` 行（按 `source_transcript_keys` 幂等），不做全天 Claude 重算（`:1239-1385`）。

**(4) 真实新鲜度**：每个 `ITEM#`/`TODAY#` 行带 `materialized_at`/`generated_at`；UI 显示 "上场会议 X 分钟前捕获"，替换 `today-adapter.js:118` 硬编码的 `'5:42 AM'`。

### 4.3 评审命中的两个时效硬约束（必须正视）

- **BUG-18 会议清单竞态（major）**：`AUTO_REPORT` 在每个 COMPLETED 无协调触发。CLAUDE.md BUG-18 要求 meeting-minutes **先跑**并写 `.meeting_manifest.json`，报告生成器读它排除（`:1230-1241`）。但 **meeting-minutes 没有自动触发器**，清单可能在 `AUTO_REPORT` 跑时还不存在 → 会议内容被吞进日报、重复。**Phase 0 必须加守卫**：fast-path 跳过可能是会议素材的转录，或延迟、或门控在清单存在之后。
- **多段转录冗余 Claude（major，已实测）**：freshness 只在 `existing_count == current_transcript_count`（`:1168`）时跳过。一段录音被 VAD 切多段、各段不同时刻 COMPLETED，计数一直变 → freshness 永不匹配 → **每个事件一次全天重算**。在 Phase 4 增量落地前（Phase 0-3），这是"每会议每用户 N 次 Claude 调用"。**从 day-one 加 debounce/coalescing**（短 SQS 延迟或 "最后 COMPLETED + N 秒" 窗口）把一串事件收敛成一次重算。

### 4.4 残余地板 + 同日快赢 vs 深层改动

- **同日快赢**：QW1（`AUTO_REPORT` + 修复，消除边界#2）、QW2（orchestrator cron `0 7`→`0/15`，`template.yaml:114` 参数默认，纯配置）、QW3（`ENABLE_DYNAMODB:'true'` 影子写）、QW4（UI 读真 `generated_at`）、QW6（`GET /api/today` 乐观卡片）。
- **深层改动**：增量物化（Phase 4，唯一全新算法）、读端切 item store + 访问控制重写（Phase 3）。
- **残余地板**：即使两个 cron 都去掉，**AWS Transcribe 异步作业时长**是不可约地板（无法从源码确定，需在账号内实测队列+运行分布）。

---

## 5. Q1 — UI 与后端融合

### 5.1 一致处（干净融合缝）

UI 每个 api 模块都已写好真实抓取分支（`if(!window.FS.api.useMocks) return FS.api.request(...)`），`useMocks` 在 `scripts/api/index.js:75` 硬编码为 `true`。后端 canonical 产物 `reports/{date}/{user}/daily_report.json` 被 `get_timeline` 逐字返回，`today-adapter.js` 直接映射。**融合面很小：按 BACKEND-CONTEXT §4 形状立起 `/api` 路由、接 Cognito、翻 `useMocks=false`。**

### 5.2 分歧处（真正的融合活）

| # | 分歧 | 证据 | 处置 |
|---|---|---|---|
| 1 | **action 写动词不匹配** | UI `actions.js` 发 `PATCH /api/actions/{id}`（`:67`）+ `POST /api/actions`（`:132`）；后端只有 `POST /api/actions/toggle {date,topic_id,action_index,checked,action_text}`（`lambda_fieldsight_api.py:602-661`，且 BACKEND-CONTEXT §4.10 与后端一致） | 加后端路由别名，或把 UI 模块指向 `/api/actions/toggle`。预存契约 bug |
| 2 | **meeting minutes 两端都无端点** | `meetings.js` 自建 S3 key 经通用 presigner 拉 JSON；schema 分歧（owner vs responsible，§5.4） | 加一级 `GET /api/meetings` 读路径 |
| 3 | **freshness 是假的** | `today-adapter.js:118` 硬编码 `generatedAt:'5:42 AM'`；真 `_report_metadata.generated_at` 存在（`:1368`）但 UI 从不读 | QW4 |
| 4 | **p2 端点全在未合并分支** | develop API = **973 行**（实测）无这些端点；p2 = 1601 行 | 所有 p2 端点引用必须限定"未合并 feature/p2 分支，须先合并/部署" |

### 5.3 集成面（具体）+ 契约增量 vs BACKEND-CONTEXT.md

后端须暴露（按 BACKEND-CONTEXT 形状）：`GET /api/timeline`（存在，保持，**响应体字节兼容不变**）、`GET /api/dates`（存在）、`POST /api/actions/toggle`（存在，调和动词）、`GET /api/reports/history` + `POST /api/reports/generate` + presigned 下载（存在，这已是"报告即导出"面）、**新 `GET /api/today`**。p2 落地后再补 `GET /api/dashboard` / `/api/search` / `/api/calendar-events` / `/api/onepager` / `/api/digest`。

**契约文档增量（修正评审命中的事实错误）**：
- ⚠️ **综合方案两次引用的 "BACKEND-CONTEXT §10 blesses polling" 不存在**。实测：BACKEND-CONTEXT.md 确有 `## 10. What's NOT in the API yet`（`:501`），**但它讲的是"尚未实现的端点"，不讲 polling**；全文件零 "poll/polling" 匹配（共 524 行，§11 止）。正确表述：**BACKEND-CONTEXT 当前对 polling/实时只字未提；新增 `GET /api/today` 及其轮询节奏（30-60s）是一个全新契约章节，不是"放宽现有文字"。**
- `POST /api/reports/generate` **语义变（形状不变）**：变成按需 store→report 投影/导出触发器（渲染并冻结 `.docx`），仍返 202。
- 标注 `daily_report.json`/`.docx` 为次级冻结导出；`/api/timeline`、`/api/dashboard` 在 Phase 3 后 item-backed。

> **canonical 目录警告**：实测存在 `fieldsight-ui - 副本`（副本）目录。所有 UI 编辑必须针对 `C:/Users/camil/dropbox/fieldsight-ui`（`claude/sprint11`），勿改副本。

**反转（Q2）对 UI 基本不可见**——因 `daily_report.json` 仍是契约。

---

## 6. 分阶段迁移计划（每阶段可独立发布）

### Phase 0 — 同日延迟快赢（无反转，完全独立）
**目标**：延迟从 ~20h 砍向分钟级，消灭空/陈旧 UX。
- `lambda_transcribe_callback.py`：落地 `AUTO_REPORT` 块 **+ 必需修复**（key `users_filter`→`user`；用户名规范化到下划线形）；加 env `REPORT_FUNCTION`、`AUTO_REPORT`。**在部署的 Lambda 上验证（BUG-22/33）。**
- `template.yaml:114`：orchestrator cron `0 7`→`0/15`（参数默认）。可选：>75MB Fargate 路径（`:540`）改 S3-event/`ecs:RunTask`。
- `lambda_fieldsight_api.py`：加 `GET /api/today` 读 ledger → 乐观卡片，替换 `:262` 的 404。**含角色隔离（见下）。**
- UI：新 `scripts/api/today.js` + `index.js` 注册；`today.js` 并行抓取并渲染 processing 卡片；守卫 `findLatestReportDate` 静默回退（`:196-233`）；`today-adapter.js:118` 改读真 `generated_at`。
- **加 debounce/coalescing**（评审命中，从 day-one）：短 SQS 延迟收敛一串 per-segment COMPLETED 为一次重算。
- **BUG-18 守卫**：fast-path 不得消费可能是会议素材的转录。
- **/api/today 角色隔离提前到 Phase 0**（评审命中）：ledger-backed `/api/today` 必须从一开始按调用者角色过滤（worker→自己设备；site_manager→自己+所辖 worker，BUG-25）。加 per-role 隔离测试。
- **验证/实现 ledger `'reported'` 写入器**（否则 processing 卡片不消失）。

**风险（中，被评审上调）**：`AUTO_REPORT` 非干净 cherry-pick（需 key+name 修复，否则全用户重算或零生成）；多段冗余 Claude（debounce 缓解）；BUG-18 竞态；部署漂移（callback 在 SAM 外，BUG-22）；`/api/today` 访问控制就地落地。

### Phase 1 — 影子物化 item store（双写，报告仍 canonical）
**目标**：让 `ITEM#`+新 `DEADLINE#`+`TODAY#` 成为首要内容提交，与 `daily_report.json` 并写，无人读、可安全验证。
- `template.yaml:325-335`：加 `ENABLE_DYNAMODB:'true'`。
- `lambda_report_generator.py`：`write_items_to_dynamodb`（`:1402`）移到 `daily_report.json` put（`:1380`）**之前**；扩展写 `DEADLINE#`（源自 `critical_dates_and_deadlines`）+ `TODAY#` 聚合行（**补上当前只写 `ITEM#` 的洞**）；`:765-792` 每行加 `materialized_at`+`source_transcript_keys`+`site_name`。
- **确定性 topic_id / 幂等 UPSERT key 提前到 Phase 1**（评审命中）：当前 SK `ITEM#{start_time}#{topic_id}`（`:775`）中 `topic_id` 是 Claude 每次非确定性分配，且 `start_time` 用全角破折号 `\u2013` split（`:774`）易碎。影子写就需要稳定 SK 才能对账。
- 一次性 backfill 脚本（`force:true, skip_backfill`）覆盖近期日期。**backfill 写 `SITE#` 键控行——读时角色过滤必须已正确，否则首次 dashboard 读跨站泄漏。**

**风险（低-中）**：写是加性且无人读，bug 不会破坏 live dashboard；主要是把从未跑的路径变载重 + DynamoDB 成本（用 on-demand 容量）。

### Phase 2 — 报告降级为投影（拆 `build_digest`/`project_report_from_items`）
**目标**：`daily_report.json`/`summary_report.json`/`.docx` 成为 item store 的字节兼容投影；`.docx` 成按需/每日冻结导出。
- `lambda_report_generator.py:1344-1409`：拆 `build_digest()` + `project_report_from_items()`，照搬 `convert_to_daily_report_format`（`:760-877`）；投影**必须**复现 `executive_summary` 数组 + `_report_metadata`（**同时改 `:877` 让 docx join 数组**）。
- `.docx` 移到 `export_report()`，按需（`POST /api/reports/generate`）+ 每夜调用；**每份导出冻结**（按 `generated_at` 键控不可变副本）。
- `POST /api/reports/generate` 重指向投影/导出路径，非全 Claude 重生成。
- 保留 05:00 cron（`:351`）但改用途为每日对账 + 归档导出。
- **weekly/monthly 重指向**（评审命中）：FRI/月末 cron 读 daily 文件，须重指向投影输出或 item store。

**风险（中）**：投影须字节保真，否则 `today-adapter` 静默崩——把报告字典钉为冻结契约，对多个真实日期做快照测试。无读路径改动，live dashboard 隔离。

### Phase 3 — dashboard/Today 读切到 item store
**目标**：从 item store 读 dashboard/Today（保留 `daily_report.json` 回退）。
- 落地 p2 `get_site_dashboard`（`GET /api/dashboard`）。
- `get_timeline`（`:219-262`）从 item 行组装 `daily_report` 响应形状（schema 不变），仅当行空回退 `s3:GetObject`。
- `get_dates`（`:339-365`）从 `TODAY#` 聚合计数读日历点，替换 get_object+计数。
- **访问控制（载重风险）**：在 query 层重实现角色过滤，保 BUG-25 site-manager 隔离（`:80-193`, `:254-262`）。item 行 `SITE#` 键控，隔离须在 query/filter 强制，不靠文件夹路径。
- **GSI1（`SITE#{id}#DATE`）提前到 Phase 3**（评审命中）：admin/gm summary 读需 query 一个 site/date 全用户，否则 scan 或 N 次 query。
- **管理层 summary 刷新**（评审命中）：per-user `AUTO_REPORT` 跳过 `:1412` 守卫，`summary_report.json` 陈旧。要么 `AUTO_REPORT` 后另刷 summary / per-site `TODAY#` rollup，要么让管理角色直接读 item 行。

**风险（中-高）**：访问控制从"路径免费"变"query 强制"，错了跨站泄漏（BUG-25）。用报告文件回退 + per-role 隔离测试（admin/gm/pm/site_manager/worker）兜底。

### Phase 4 — 增量 per-meeting 物化 + 服务端 rollup
**目标**：用便宜的 append/merge 替换每次 `AUTO_REPORT` 的全天 Claude 重算。
- `lambda_report_generator.py`：加 `mode='materialize_incremental'`——单 `users_filter` + `triggered_by='transcribe_callback'` 时只处理新转录的 topic、UPSERT 那些 `ITEM#` 行 + 重算 `TODAY#`，无全天重算、无 docx。cron 路径保持全重建（正确性后盾）。
- `lambda_transcribe_callback.py`：payload 设 `{mode:'materialize_incremental', users_filter:[user], date}`。
- 确定性稳定 `topic_id` + `source_transcript_keys` 去重（防部分写重复）。
- （可选）`ItemsTable GSI1` + 服务端 `/api/insights` rollup，让聚合器停止浏览器端每次重算。

**风险（中）**：增量 merge 是唯一全新算法——稳定 `topic_id` 键控、幂等 UPSERT、`TODAY#` rollup 重算需谨慎；重复 COMPLETED 不得双计。限定在增量路径，cron 全重建作后盾。

### 横切：可观测性子阶段（评审命中，MONITORING.md 缺口）
MONITORING.md 把 DynamoDB 监控列为"可选"（§9），无延迟/新鲜度/throttle 告警。架构使这些载重后需新增：transcript-COMPLETED→daily_report put 延迟、DynamoDB throttle/WCU（per-meeting put + `/api/today` 轮询）、`AUTO_REPORT` 调用/错误率、"report age" 新鲜度指标。**当前一个都没有。**

---

## 7. 风险与未决问题

| # | 风险 | 严重度 | 缓解 |
|---|---|---|---|
| R1 | **访问控制**：读从 `{user}` 文件夹（免费隔离）移到 `SITE#` query，错误 filter 跨站泄漏（BUG-25） | 最高 | Phase 3 前保报告文件回退 + per-role 隔离测试；`/api/today` 隔离提前到 Phase 0 |
| R2 | **`AUTO_REPORT` 按原样不工作**：key（`users_filter` vs `user`，`:1802`）+ 名形（空格 vs 下划线，`:1218`）双重不匹配 | Blocker | Phase 0 必需代码修复 + 单转录单用户刷新断言 |
| R3 | **BUG-18 清单竞态**：`AUTO_REPORT` 可能在 `.meeting_manifest.json` 前吞会议转录 | 高 | fast-path 跳过会议素材 / 门控清单存在 / debounce |
| R4 | **多段冗余 Claude**：freshness（`:1168`）在 ingestion 期间永不匹配 → N 次全天重算 | 高 | day-one debounce/coalescing；Phase 4 增量 |
| R5 | **管理层 summary 陈旧**：per-user `AUTO_REPORT` 跳过 `:1412`，admin/gm 读 `summary_report.json` 不刷新 | 高 | Phase 3 另刷 summary / per-site `TODAY#`，或管理角色直读 item 行 |
| R6 | **`'reported'` ledger 终态写入器不存在**（全 src 无）；callback 只写 `'pending'`（`:130`） | 高 | `/api/today` 卡片清除前先实现该转换 |
| R7 | **`DEADLINE#` 洞**：`get_site_dashboard` query `DEADLINE#` 但 writer 只写 `ITEM#`（`:765-792`） | 中 | Phase 1 补 `DEADLINE#` 写入 |
| R8 | **部署漂移**：callback/VAD/meeting-minutes/API 在 SAM 外（BUG-22/33/34）；template 改 `ENABLE_DYNAMODB` 若函数 out-of-band 更新可能不生效 | 中 | 每个改动函数走 BUG-22 部署代码验证 |
| R9 | **weekly/monthly 未纳入**：FRI/月末 cron 读 daily 文件，daily 变投影后须重指向 | 中 | Phase 2 重指向/重验证 |
| R10 | **幂等/双计**：SK `topic_id` 非确定性、`start_time` 全角破折号 split（`:774`）易碎 | 中 | Phase 1 前定确定性 key（hash of `source_transcript_keys`+time） |
| R11 | **成本/吞吐**：per-meeting put + `/api/today` 轮询 + dashboard query 飙 RCU/WCU；事件驱动 Claude 可能撞 Lambda 并发/Claude 限速 | 中 | on-demand 容量；debounce；并发护栏 |
| R12 | **残余 ingestion 延迟**：即使 `AUTO_REPORT`，20:00 拉取（`:114`）仍封顶新鲜度——不缩短则客户仍等数小时进入 pipeline | 中 | QW2 cron `0/15` |
| R13 | **Transcribe 异步时长**：两 cron 去掉后的不可约地板，源码无法确定 | 低 | 账号内实测队列+运行分布 |
| R14 | **observability 缺口**：MONITORING.md DynamoDB 监控"可选"，无相关告警 | 低-中 | 横切可观测性子阶段 |
| R15 | **文档/分支归因漂移**：p2 端点仅在未合并分支（develop=973 行实测无）；canonical UI 目录须排除 `- 副本` | 低 | 所有 p2 引用限定"未合并"；锁定 canonical 目录 |

---

**核心判断**：方案方向正确、扎根代码、低风险路径选对（Angle C 为基）。**但 Phase 0 的 `AUTO_REPORT` 钩子必须当作"需修复的代码改动"而非干净 cherry-pick**——这是整个 Q3 时效性论点的支点。修好 R2-R6 这五个已实测缺陷，方案即可执行。

**关键证据文件**：`C:/Users/camil/dropbox/fieldsight-pipeline/src/lambda_report_generator.py`（`:766, :1168, :1212-1218, :1361, :1380-1402, :1412, :1802`）、`src/lambda_transcribe_callback.py`（`:130`，及 p2 分支 `:146-163`）、`src/lambda_meeting_minutes.py`（`:760-877`）、`src/lambda_fieldsight_api.py`（`:219-262, :339-365, :602-661`）、`template.yaml`（`:114, :325-346, :351, :540`）、`C:/Users/camil/dropbox/fieldsight-ui/scripts/api/today-adapter.js`（`:113-118`）、`scripts/api/actions.js`（`:67, :132`）、`scripts/api/index.js`（`:75`）、`scripts/pages/today.js`（`:196-233`）、`BACKEND-CONTEXT.md`（`:501` §10 ≠ polling）。
