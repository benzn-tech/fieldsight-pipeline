# FieldSight 平台演进策划(Roadmap)

> **性质:** 主策划(program roadmap),不是单子系统的逐任务实施计划。
> 每个 Phase 独立可交付、可测试;开始执行某个 Phase 时,再用 `superpowers:writing-plans`
> 把它展开成逐任务(TDD)的实施计划。
> **日期:** 2026-07-01 · **延续:** ADR-0001(`docs/adr/0001-platform-architecture.md`)+
> `fieldsight-ui/docs/MIGRATION-HANDOFF.md`

---

## 0. 决策快照(本轮已锁定)

| # | 决策 | 结论 |
|---|------|------|
| D1 | 先连真实数据 | **Track 1 先行**:把 `fieldsight-ui` 原型接到**已存在**的报告 API(读侧),是配置+联调,不写新后端 |
| D2 | 数据层 | **一个 Aurora Serverless v2(Postgres + pgvector)**同时当:关系核心 + Dashboard 读模型 + 向量检索 |
| D3 | 各存储分工 | Cognito=认证;S3=归档+图片原文;DynamoDB=维持现职(job ledger/audit/报告读索引);Postgres=运营/关系/ACL/向量 |
| D4 | 向量平台 | 终态 **pgvector**(ACL 与向量同库,一条 SQL);不上 Pinecone/独立向量 SaaS。**起步先用 Bedrock KB 验证召回**,KB 的向量库直接落 **Aurora pgvector**、embedding 用 **Titan V2**——迁移 = 停用 KB Retrieve、改跑自写 ACL SQL,零 reindex、零搬数据 |
| D5 | 图片检索 | 图片**不向量化**;靠 topic↔图片关联,搜文字命中 topic → 带出图片。将来要"搜图片内容"再用"视觉模型生成 caption → 按文字 embed" |
| D6 | 数据库形态 | **Aurora Serverless v2**(负载尖峰+多数空闲,可缩到 0),非传统 provisioned RDS |
| D7 | Dashboard 时效 | **把抽取从夜间日报里拆出来**:转录完成即触发轻量抽取 → 立刻写 Postgres + embed pgvector;Dashboard 读 Postgres 近实时;夜间日报降级为"汇总归档" |
| D8 | IaC | **留 SAM**,把 out-of-SAM 资源(API GW/Cognito/DynamoDB/CloudFront/前端 bucket)补进 IaC(ADR-003) |
| D9 | 两个前端 | `fieldsight-ui` 是未来前端;老 `fieldsight_v5.jsx` 对等后退役 |

---

## 1. 依赖关系与落地顺序

```
Phase 0  连真数据(读侧, 用现有 API)   ── 独立, 立即可做, 最快见效
   │
Phase 1  补 IaC + UI dev CI/CD         ── 解锁"可复现的真·dev 后端"
   │
Phase 2  数据层地基(Aurora + pgvector + schema)  ── Track 2 一切的地基
   ├──────────────┬───────────────────────┐
Phase 3          Phase 4                 (3 与 4 都只依赖 2, 可并行)
app 写流程        事件抽取 + Dashboard 时效
(项目/成员/角色/图片)   (转录完成→抽取→Postgres+pgvector)
   └──────────────┴───────────────────────┘
                  │
Phase 5  全局 Ask Agent(retrieve+ACL → Claude → 引用)  ── 依赖 2 与 4
                  │
Phase 6  规模门槛:多行业 prompt overlay · WhisperX 自建转录  ── 客户/规模驱动, 可后置/并行
```

| Phase | 目标一句话 | 依赖 | 工作量 | 何时 |
|---|---|---|---|---|
| **0** | 原型接现有报告 API,读侧显示真实数据 | 无 | **S** | 现在 |
| **1** | out-of-SAM 资源进 IaC + fieldsight-ui dev 自动部署 | 0 | M | 紧接 |
| **2** | 立起 Aurora(Postgres+pgvector)+ 全套 schema | 1 | M | 地基 |
| **3** | 项目/成员/角色/资料/图片的真实写后端 | 2 | M→L | 与 4 并行 |
| **4** | 事件驱动抽取 + Dashboard 近实时 | 2 | L | 与 3 并行 |
| **5** | 全局 Ask agent(向量+ACL 检索) | 2,4 | M→L | 之后 |
| **6** | 多行业 overlay;自建转录 | 2(overlay)/ 规模(转录) | M/L | 规模门槛 |

---

## Phase 0 — 连真实数据(Track 1,读侧)

**目标:** 不动后端,把 `fieldsight-ui` 从 mock 切到**已存在**的报告 API,让 timeline/日历/sites/转录/音视频/Ask/action 勾选显示真实数据。

**前置:** 拿到真实 **API Gateway 调用 URL**(老前端走同源代理没硬编码,需从 AWS 取)。用户本机 `! aws login` 后由我用 CLI 查:`aws apigateway get-rest-apis --region ap-southeast-2` +（若走 CloudFront)确认前端源站。

**已核实的接线点(无需改造,只配置):**
- 开关:`fieldsight-ui/scripts/api/index.js` → `window.FS.api.useMocks`、`baseUrl`(现 `true` / `'/api'`)。
- Cognito 覆盖 seam:`scripts/auth/cognito.js`(读 `window.FS_COGNITO_CONFIG`)。真实值:pool `ap-southeast-2_ps7XIQGHB`、client `5npb81jbj1hgh9tsck25kan3os`、region `ap-southeast-2`。
- 运行时覆盖:`app-shell-preview.html` 已支持 `?baseUrl=…&mocks=0`。
- 真实后端契约:`fieldsight-ui/BACKEND-CONTEXT.md`(端点/schema/坑,已与 UI 数据层一一对应)。

**关键步骤:**
1. 取到 API GW URL;确认 CORS(BACKEND-CONTEXT §3 说已 `Access-Control-Allow-Origin: *`)对 Amplify dev 域名是否放行。
2. 新增 `scripts/env.js`(ADR-001 方案):按环境注入 `window.FS_COGNITO_CONFIG` + `FS.api.baseUrl`;把硬编码从源码挪走。
3. 先用 `?mocks=0&baseUrl=<APIGW>` 在 dev 上**手测**一条链路:登录(含 `NEW_PASSWORD_REQUIRED` 首登挑战)→ `/api/sites` → `/api/dates` → `/api/timeline`。
4. 逐屏核对已知坑已被现有代码吸收:NZ 日期(BUG-19)、CloudFront 404→HTML(BUG-20)、token 刷新、403 空态(`_fetch.js` 已处理)。
5. 通过后把 `useMocks=false` 作为 dev 默认(经 `env.js`),保留 `?mocks=1` 逃生。

**验证:** dev URL 用真实 Cognito 用户登录,报告类页面显示 S3 里真实报告;切不同角色(admin/pm/site_manager/worker)可见范围符合 BACKEND-CONTEXT §3。

**取舍/风险:** 写侧(Phase A/B/C 的新建项目/成员/图片)此阶段**仍是 mock**——它们没有后端(见 Phase 3)。别在此阶段假装它们连通了。

**展开计划时机:** 拿到 API GW URL 后,用 writing-plans 出 Phase 0 逐任务计划(约半天)。

---

## Phase 1 — 补 IaC + fieldsight-ui dev CI/CD

**目标:** 让"真·dev 后端"可复现,并让原型每次 push 自动部署到 dev。

**关键步骤:**
1. **补 IaC(ADR-003,留 SAM):** 把现在在 SAM 之外的资源纳管——API Gateway、Cognito User Pool/Client、DynamoDB 表(现仅 Parameter 引用)、CloudFront、前端 bucket、S3 event 通知(BUG-33 需手配)。可选:并列 stack `fieldsight-edge` 专管 API GW+Cognito+CloudFront+前端 bucket,与现 `fieldsight-pipeline` stack 解耦。已存在资源用 `cloudformation`/`sam` import,**不重建、不停机**。
2. **fieldsight-ui workflow(新建):** 无 build step,`aws s3 sync` 静态文件 + CloudFront invalidation;CI 写 `env.js`;OIDC role trust policy 追加 `repo:benzn-tech/fieldsight-ui:*`,权限含 dev bucket `s3:PutObject` + `cloudfront:CreateInvalidation`。**绝不放长期 key。**
3. **分支——不统一(D1 已定):** pipeline 保 `develop`(Actions 已挂 develop→test/main→prod),ui 保 `dev`(Amplify webhook)。各自 CI 已绑死,改名只断触发器、零收益。新建的 ui workflow 认 `dev` 即可。

**验证:** 从零 `sam deploy --config-env dev` 能拉起含 API GW+Cognito 的 dev 栈;push 到 `dev` 后 dev URL 自动更新。

**取舍:** 这部分 YAML 将来若迁 CDK 要再过一遍;用 `CfnInclude` 增量迁可减少返工(ADR-003)。

---

## Phase 2 — 数据层地基(Aurora Serverless v2 + pgvector + schema)

**目标:** 立起唯一的关系+向量库,定义 Track 2 / Dashboard / Ask agent 共用的 schema。

**关键步骤:**
1. IaC 增 **Aurora Serverless v2(PostgreSQL)**:min ACU 可设 0(空闲缩零),放私有子网,Lambda 经 RDS Proxy 连接;开 `CREATE EXTENSION vector;`。
2. 建 schema(下为地基草案,执行时细化 + 加索引/约束):

```sql
-- 认证在 Cognito;此处只存业务扩展 --------------------------------
companies(id pk, name, industry, created_at)
users(id pk, cognito_sub unique, company_id fk, email, first_name,
      last_name, avatar_s3_key, global_role, created_at)   -- role: admin/gm/...
sites(id pk, company_id fk, name, location, client, industry,
      icon_s3_key, created_at)                              -- industry 供 ADR-002 overlay
memberships(id pk, user_id fk, site_id fk, role,            -- 多对多 + 每站角色
            unique(user_id, site_id))

-- Dashboard 读模型:从报告"拍平"的结构化事实(供快速过滤/聚合) ----
topics(id pk, site_id fk, user_id fk, source_s3_key, report_date,
       occurred_at, category, title, summary, created_at)
action_items(id pk, topic_id fk, site_id fk, text, responsible,
             deadline, priority, status, created_at)        -- status: open/…
safety_observations(id pk, topic_id fk, site_id fk, observation,
                    risk_level, location, status, created_at)
topic_photos(id pk, topic_id fk, s3_key, caption_text null) -- D5:图片挂 topic,caption 可后加

-- 语义检索:块 + 向量 + metadata(与关系表同库 → ACL 一条 SQL) -------
report_chunks(id pk, site_id fk, user_id fk, source_s3_key, topic_id fk null,
              report_date, chunk_type, chunk_text,          -- 几 KB 文本
              embedding vector(1024), metadata jsonb, created_at)  -- Titan Text Embeddings V2 @1024
-- 索引: HNSW on embedding; btree on (site_id, report_date)
```

3. 索引:`report_chunks.embedding` 建 HNSW;运营表按 `(site_id, report_date, status)` 建 btree。
4. embedding 模型 = **Bedrock Amazon Titan Text Embeddings V2 `amazon.titan-embed-text-v2:0` @ 1024 维**(已锁定 2026-07-02)。理由:原生 AWS 最省事、最便宜、可直接当 Bedrock KB 的 embedding 模型 → KB 与 pgvector 用同一模型 = 迁移零 reindex。若日后需强多语(中文)可切 `cohere.embed-multilingual-v3`(同 1024 维)。**维度固定 1024,换模型才需 reindex。**

5. **切片策略(chunking)——顺着 pipeline 已有的语义边界切,不按固定字符数硬切。**
   本域优势:report generator 早已把转录切成 `topics[]`(title/summary/category/时间段/action/safety/photos),转录本身是带绝对时间戳的 speaker turn(`transcript_utils.normalize_transcript`)。沿这些天然缝切,语义天然保住。

   **切片单元(写进 `report_chunks.chunk_type`):**
   | chunk_type | 单元 | chunk_text | 目标大小 |
   |---|---|---|---|
   | `topic`(主力) | 1 个 topic | title + summary(+关键 action/safety 内联) | **自然长度,别为凑数硬切**,常见 ~400–900 tokens |
   | —— 超长才切 | topic > ~1,000–1,200 tokens | 按小标题/句子切,相邻块 overlap ~10–15%(1–2 句 / 50–80 tokens) | — |
   | `transcript_turn` | 连续 speaker turn 拼窗口,**不跨 turn 切** | 带绝对时间的逐字对话 | **~500–800 tokens**,overlap 1 个 turn |
   | `summary` / `action` | 周报/月报 rollup、行动项 | 一条一块 | 天然 <200 tokens |

   **硬规则:**
   - **绝不跨语义缝切**(句中/turn 中/topic 中都不切)。边界只落 topic → 段落 → speaker turn。
   - **粒度 = topic**,不是某个 token 数:切太小碎片脱离上下文;切太大一个向量稀释多子话题、弱匹配。topic 恰是"不碎又不混别话题"的平衡点。整份日报不当一块(topic 间互不相关,一个向量表示不了)。
   - 全部远在 Titan 8,192 token 上限之下 → 约束是**向量稀释 vs 上下文完整**,不是塞不下。

   **两个增强(让块能放大而不丢上下文):**
   - **Contextual Retrieval(Anthropic 技法,已在用 Claude):** embedding 前给每块前置 1–2 句 Claude 生成的上下文("本块摘自 XX 站 10/20 日报,关于 B2 层浇筑")→ 召回准确率显著提升。
   - **small-to-big:** 用中等块匹配,命中后经 `report_chunks.topic_id` 返回**整个 topic + 其 `topic_photos`**(图片不入向量,靠 `topic_id` 关联——即"搜关键词→指回带图 topic")。

**验证:** 能插入一条 chunk + 向量并跑通"向量近邻 + `WHERE site_id=ANY(...)` 权限过滤"的联合查询;运营表能被 Dashboard 查询模式命中索引。切片:一个真实 topic 入库后,`topic` 块保持完整未被拦腰切;一段长 transcript 按 turn 窗口化且有 overlap。

**取舍:** 维度/embedding 模型是"贵重决定"——换模型要 reindex,选前小样本评估召回。切片粒度调整(topic vs turn、overlap)不需换模型,但需重跑 ingestion 重灌块。

---

## Phase 3 — App 关系核心 + 管理写流程(Track 2 real)

**目标:** 给 Phase A/B/C 当前**无后端**的写功能建真实接口,把原型写流程从 mock 切过去。

**范围(对应 UI 已有的 mock):**
- 新建项目(`sites`)、加成员、分配/改角色(`memberships`)、改个人资料(`users`)。
- 图片:项目 icon / 成员头像 → **S3 presigned PUT 上传**,`*_s3_key` 存 Postgres;读用短时 presigned GET(BACKEND-CONTEXT §7,15 分钟过期,勿存 localStorage)。
- 成员创建走 **Cognito admin API**(`admin-create-user`,见 pipeline CLAUDE.md 示例)+ 落 `users`/`memberships`。

**关键步骤:**
1. 新增写端点(沿现有 `fieldsight-api` Lambda 风格 + JWT 角色校验):`POST /api/sites`、`POST /api/site-users`、`PATCH /api/users/{id}/role`、`PATCH /api/me/profile`、`POST /api/media/upload-url`(presigned PUT)。
2. UI:`scripts/api/sites.js` 等把 `useMocks=false` 分支指向真端点(形状已按 BACKEND-CONTEXT 写好)。
3. 权限:写操作 gate 到 `user:manage`/admin(UI 已有 `?dev=1` 角色开关做联调)。

**验证:** admin 在 dev 上新建项目/加成员/改角色/传图,刷新后**真实持久**(数据在 Postgres/S3,非内存);非 admin 被 403。

**取舍:** 这一步才真正"消灭 localStorage 权宜之计"——数据有了真家。

---

## Phase 4 — 事件驱动抽取 + Dashboard 时效性

**目标:** 会议/巡场结束后**一小时内(通常几分钟)**内容出现在 Dashboard,SM/PM 可即刻复制发部门;日报降级为夜间归档。

**关键步骤:**
1. 在**转录完成**这一步(S3 写 `transcripts/{user}/{date}/` 事件)挂一个**轻量抽取 Lambda**:对**这一场**跑一次 Claude,产出 topics / action_items / safety / 短摘要。
2. 抽取结果**立刻**:① upsert 进 Postgres 运营表(Phase 2 的 `topics`/`action_items`/…);② 切块 + embed + upsert 进 `report_chunks`。
3. **Dashboard 读 Postgres**(不读 S3 报告),近实时(短轮询或 WebSocket)。
4. **夜间日报重构为汇总**:对当天已抽好的结构化事实拼成正式 docx/JSON 归档(EventBridge 定时,低频时段),不再承担繁重抽取,不卡时效。
5. 幂等/重算:report 重生成时按 `source_s3_key` upsert,避免重复(注意 BACKEND-CONTEXT §8.8 topic_id 漂移)。

**时效预算:** 上传 + VAD + **Transcribe(最慢,几~十几分钟)** + 一次 Claude(秒~分钟)。"一小时内"宽裕;要更快再考虑 streaming 转录(后话)。

**验证:** 造一场测试会议音频 → 计时到 Dashboard 出现该会 action items;确认夜间日报内容 = 白天已入库事实的汇总。

**取舍:** 这是最大 UX 收益,也是 L 级工作量;先把"抽取写库 + Dashboard 读库"打通,WebSocket 实时可后置为轮询。

---

## Phase 5 — 全局 Ask Agent(RAG)

**目标:** "10/20 工地发生了什么"式全局问答,跨 site/跨时间,按角色层级过滤。

**关键步骤:**
1. 在现有 `lambda_ask_agent.py`(v1.0,单报告 grounded)**前面加 retrieve 层**:问题 embed → `report_chunks` 向量近邻 **+ 同一条 SQL 里 `WHERE site_id = ANY(:accessible_sites)`**(ACL 复用 `memberships`/角色层级)→ top-K 块。检索用 Phase 2 定义的**结构感知切片 + small-to-big**:命中细块后按 `topic_id` 上卷回整个 topic 再喂 Claude。
2. top-K 喂现有 Claude 直连生成 → **带引用**(引用经 `metadata`/`source_s3_key` 指回 S3 原文/topic)。
3. 命中 topic 的图片:经 `topic_photos` 一并返回(D5,无需图片向量)。
4. 可选增强:混合检索(BM25+向量)、reranker、会话记忆(现为无状态)。
5. 可选图片内容搜:入库时视觉模型给图生成 `caption_text`,按文字 embed 进 `report_chunks`。

**验证:** worker 问它别人 site 的事 → 检索被 ACL 过滤为空/拒答;admin 得到跨 site 带引用答案;引用点回真实原文。

**取舍:** ACL 与向量同库(pgvector)使"相似度+权限"一条 SQL 完成——这正是 D2/D4 选 Postgres 的核心收益。

---

## Phase 6 — 规模门槛(可后置/并行)

- **多行业 prompt overlay(ADR-002):** base + `config/industries/{x}.json`,按 **site.industry** 注入;加 `food_manufacturing`;把 output schema 从 Python 外置到 base 模板。客户驱动、带收入,可提前。
- **自建转录 WhisperX(ADR-005):** 拐点 **~500–1000 audio-h/月**;GPU(ECS-EC2/AWS Batch,**非 Fargate**)+ adapter 吐 Transcribe JSON 形状,下游 `transcript_utils` 不动;env flag 灰度、Transcribe 作 fallback。规模到了再做。

---

## 小决策(已确认 2026-07-02)

1. **分支名 — 不改。** 两仓已各有 CI 挂钩的集成分支:`fieldsight-pipeline` = **`develop`**(Actions `deploy.yml`:develop→test、main→prod),`fieldsight-ui` = **`dev`**(Amplify webhook:dev→dev URL)。改名会断掉各自的部署触发,零收益。放弃"统一命名"。
2. **embedding = Bedrock Titan Text Embeddings V2 @ 1024 维**(便利优先、原生 AWS、可复用为 KB 模型)。多语后备:Cohere Multilingual v3(同 1024)。
3. **Ask agent — 先验证再迁。** KB 的向量库从一开始就落 Aurora pgvector + Titan V2 → 验证期用 KB 托管入库/Retrieve 快速试召回,转正 = 换成自写 ACL SQL,零 reindex。(想在 Phase 2 立 Aurora *之前* 就验证,可用 KB+OpenSearch Serverless 快闪,代价是日后 reindex——不推荐,OpenSearch 空闲贵。)
4. **Phase 3 ‖ Phase 4 并行**(都只依赖 Phase 2)。

## 现在做 / 以后做

- **现在(低风险高价值):** Phase 0——拿 API GW URL,原型切真实报告 API(读侧真实数据)。
- **紧接:** Phase 1 补 IaC + UI dev CI/CD → 解锁真·dev 后端。
- **地基:** Phase 2 Aurora+pgvector+schema。
- **并行推进:** Phase 3(写流程)‖ Phase 4(Dashboard 时效)。
- **之后:** Phase 5 Ask agent;Phase 6 规模门槛项。
