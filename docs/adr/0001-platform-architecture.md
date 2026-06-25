# ADR-0001 — FieldSight 平台架构决策记录

- **Status:** Accepted
- **Date:** 2026-06-25
- **Scope:** 5 个架构议题(CI/CD 多环境、多行业 prompt、IaC、数据层 + 全局 Ask agent、转录 backend)
- **相关文档:** dev 部署可执行 runbook 见 `fieldsight-ui/docs/dev-deployment.md`

> 本 ADR 的结论全部 grounded 在仓库实际代码(已核实文件/行号),不是通用最佳实践罗列。
> 每个议题给出 (a) 现状评估、(b) 决策与具体方案、(c) 取舍。

---

## 0. 通读后核实的关键事实(决策前提)

1. **后端多环境 CI/CD 已经存在。** `fieldsight-pipeline/.github/workflows/deploy.yml`
   已实现 `develop`→test(`fieldsight-test` stack)、`main`→prod,走 **OIDC**(`AWS_ROLE_ARN`,
   无长期 key),`sam deploy --config-env test/prod`。`samconfig.toml` 有 `[test]`/`[prod]`
   两套参数(独立 bucket `fieldsight-data-test-509194952652`、独立 DynamoDB 表、test 默认
   `EnableSchedules=false`)。`template.yaml` 有 `Stage` 参数 + `StageConfig` Mappings
   (`Prefix: fieldsight` / `fieldsight-test`)+ Conditions。
2. **CI 部署的前端不是当前在做的那个。** `deploy.yml` 同步的是 `fieldsight-pipeline/frontend/`
   (老的 `fieldsight_v5.jsx`,带 build step、`CONFIG` 硬编码 Cognito)。`fieldsight-ui` 原型
   (Sprint 0–11)**没有任何 CI/CD,没部署在任何地方。**
3. **fieldsight-ui 接 dev 后端是配置而非改造。** 已支持运行时 `?baseUrl=…&mocks=0`
   (`app-shell-preview.html:247-260`)和 `window.FS_COGNITO_CONFIG` 覆盖 seam
   (`scripts/auth/cognito.js:44-51`)。
4. **Ask agent 已有雏形但只覆盖单份报告。** `lambda_ask_agent.py` v1.0(Haiku)按 date+user
   取**一份**报告+转录做 grounded Q&A,**无检索/向量层**——正是"全局/全公司"的缺口。
5. **行业切换 seam 已具备。** `load_prompt_templates()`(`lambda_report_generator.py:99-113`)+
   `get_template()`(:116)+ S3 `config/` 热加载;`user_mapping.json` 的 `sites` 已有 `client`
   字段(MoE / MPI / Northbrook),只缺 `industry` 维度和注入逻辑。
6. **转录下游已被一层 normalize 解耦。** `transcript_utils.normalize_transcript()` 是替换
   Transcribe 的天然 seam,但它**期望 AWS Transcribe 的 JSON 形状**(`items[]` 带
   start/end/speaker_label、`speaker_labels.segments`)——自建 backend 必须吐同样形状(adapter)。
7. **数据全在 S3;DynamoDB 在用(但非报告 items 表)。** 在用:`fieldsight-transcripts`
   (job ledger)、`fieldsight-users`(JWT→profile)、`fieldsight-audit`。`ENABLE_DYNAMODB=false`
   关掉的只是报告 items/reports 去规范化表。**无 RDS。** Claude 走**直连 Anthropic API**(urllib3),
   不是 Bedrock(`call_claude_structured` :410-441,`claude-sonnet-4-6`)。

---

## Executive Summary + 优先级矩阵

| # | 决策 | 一句话结论 | 何时做 | 工作量 |
|---|------|-----------|--------|--------|
| Q1 | CI/CD 多环境 | 后端已就绪;**给 fieldsight-ui 建独立 workflow**,先 mock 模式上 dev 拿到可分享 URL | **现在** | S–M |
| Q3 | CDK vs SAM | **留 SAM**;先把 API GW/Cognito/DynamoDB/CloudFront 这些 out-of-SAM 资源补进 IaC | 现在补 IaC / CDK 暂缓 | M |
| Q2 | 多行业 prompt | base + `config/industries/{x}.json` overlay,行业按 **site** 维度注入,加 food_manufacturing | 较快(客户驱动) | M |
| Q4 | 数据层 + Ask agent | S3 仍是源;DynamoDB 当索引/ledger;**全局 Ask = 现有 ask agent 前加检索层**(先 Bedrock KB over `reports/`) | 分阶段 | M→L |
| Q5 | Transcribe vs 自建 | 量小留 Transcribe;**~500–1000 audio-h/月**是拐点;自建用 **WhisperX on GPU**(非 Fargate)吐 Transcribe 形状 JSON | 规模到了再做 | L |

**落地顺序:** Q1(dev 后台测试,立即见效)→ 补 IaC(Q3,解锁"真·dev 后端")→ Q2(食品客户,
带收入)→ Q4 索引/Ask → Q5 自建转录(规模门槛)。

---

## ADR-001 — CI/CD:GitHub → AWS 多环境自动部署

### (a) 现状评估
- **后端:已支持。** `deploy.yml` + `samconfig.toml` + `template.yaml` 的 Stage/Mappings/Conditions
  组合已是干净的 test/prod 多 stack 方案,且用 OIDC。无需"改造 SAM 才能多环境",它已经多环境了。
- **真正的缺口(3 个):**
  1. **fieldsight-ui 完全没接进来。** 没有 `.github/`,CI 部署的是 pipeline 仓库里的老前端。
  2. **分支命名/存在性。** workflow 认 `develop`,你要 `dev`;两者都还没建。
  3. **dev "后端" 不完整。** API Gateway、Cognito、DynamoDB 表、CloudFront、前端 bucket 都
     **不在 SAM template 里**(见 ADR-003),`fieldsight-test` stack 很可能只有 Lambda/Fargate,
     没有可供 UI 真正联调的 API/Cognito。S3 event 通知也要手动配(BUG-33)。

### (b) 决策与方案
**推荐:GitHub Actions + `sam deploy`(沿用现状),不引入 SAM Pipelines。**
SAM Pipelines 生成 CodePipeline/CodeBuild,多一层 AWS 侧编排资源和心智负担,对单账号小团队是
过度工程。继续用 GH Actions 还能让前后端两仓库各自拥有 workflow(关注点分离)。

**两条腿,协调点 = 先后端、再前端、最后 invalidation:**
1. **后端(已存在,微调):** trigger 从 `develop` 统一成 `dev`(或保留 `develop`)。`dev` push→test,
   `main` push→prod。
2. **前端(新建,fieldsight-ui 自己的 workflow):** 完整步骤见 `fieldsight-ui/docs/dev-deployment.md`。
   要点:无 build step,直接 `aws s3 sync` 静态文件;**运行时配置注入**用新增 `scripts/env.js`
   (CI 按环境写 `window.FS_COGNITO_CONFIG` + `FS.api.baseUrl`,复用既有 override seam);hash 路由
   (`router.js`)使 CloudFront 当纯静态站即可,**无需 SPA rewrite**;invalidation 放在 sync 之后。

**OIDC:** 沿用 `aws-actions/configure-aws-credentials@v4`。**新增**:现有 OIDC role 的 trust policy
目前只信任 `fieldsight-pipeline`,要给 fieldsight-ui workflow 用,需在 `sub` condition 加
`repo:benzn-tech/fieldsight-ui:*`,权限含 dev UI bucket 的 `s3:PutObject` + `cloudfront:CreateInvalidation`。
绝不放长期 access key。

**"后台测试"的最快路径(关键洞察):**
> **Phase 1:** fieldsight-ui 静态文件部到 dev S3+CloudFront,跑 **mock 模式**(默认 `useMocks=true`,
> 零后端依赖)→ 立刻得到可分享 URL。**Phase 2:** dev 后端就位后,`env.js` 指过去 + `mocks=0` 联调。
> 这样"后台测试"不被"dev 后端不存在"卡住。

### (c) 取舍
- GH Actions vs SAM Pipelines:选前者,换简单 + 双仓库各自 own 部署;代价是没有 CodePipeline 的
  可视化 stage gate(对你规模不值)。
- 前端 workflow 放 fieldsight-ui:换关注点分离 + 原型独立演进;代价是短期两个前端并存。

### 两个前端的推荐
**fieldsight-ui 是未来前端,老的 `fieldsight_v5.jsx` 走向退役。**
fieldsight-ui 成熟度远超老单文件应用,但**还没接后端**,不能直接替换 prod。路径:① 现在部 dev
(mock→dev 后端联调);② 与老前端"真实数据"对等后提升到 prod,退役 `fieldsight-pipeline/frontend/`;
③ 届时前端部署从 pipeline workflow 移到 fieldsight-ui workflow。**过渡期 pipeline workflow 继续部
老前端到 prod,互不影响。**

---

## ADR-002 — 多行业 Prompt 架构(单产品线 + 行业外挂)

### (a) 现状评估
- 模板:`config/prompt_templates.json`(v3.0,daily/weekly/monthly)+ `prompt_templates_meeting.json`
  (v1.1)。每个模板有 `system_context` + `prompt`,用 `{variable}`/`{schema}` 占位。**全部硬编码
  NZ construction**;`{schema}` 实体来自 Python 多行字符串(`lambda_report_generator.py:141-226`)。
- **无 industry 维度**;`user_mapping.json` 的 `client` 字段只是元数据,从不参与模板选择。
- **加载层已通用**(`load_prompt_templates`/`get_template` + S3 热加载 + 占位替换),扩展点干净。

### (b) 决策:base prompt + 行业 module overlay
**目录/配置:**
```
config/
  prompt_templates.json            # base:行业中立骨架 + 默认 schema(保留)
  prompt_templates_meeting.json    # base meeting(保留)
  industries/
    construction.json              # 行业 module
    food_manufacturing.json        # 行业 module
    _schema.md                     # module 字段说明
```
行业 module 形如:
```json
{
  "industry": "food_manufacturing",
  "system_context": "You are a food manufacturing operations documentation assistant (NZ, MPI/FSANZ context).",
  "glossary": ["HACCP","CCP","CIP (clean-in-place)","allergen","GMP","batch/lot","RMP","ATP swab","retort","brix"],
  "transcribe_vocabulary": "custom_vocabulary_food_nz",
  "categories": ["food_safety","production","quality","compliance"],
  "constraints": [
    "Flag any HACCP/CCP deviation or temperature excursion as critical.",
    "Surface allergen cross-contact and traceability/lot issues.",
    "Cite MPI/RMP requirements where relevant."
  ],
  "schema_extensions": {
    "ccp_deviations": [{"ccp":"","reading":"","limit":"","action":""}],
    "allergen_controls": [],
    "batch_lot_refs": []
  },
  "few_shot": []
}
```

**运行时注入(对加载层的最小扩展):**
- 新增 `load_industry_module(industry)` + `merge_template(base, module)`:module 覆盖 `system_context`、
  并入 `glossary`/`constraints`、把 `schema_extensions` merge 到 base schema。
- prompt 新增占位 `{system_context}`/`{glossary}`/`{constraints}`/`{schema}`(扩展后)。其余 build
  流程(metadata、transcript、truncation per BUG-15、max_tokens per BUG-16)不变。
- **唯一结构性改动:把 output schema 从 Python 字符串外置到 base 模板**,行业才能 config 驱动地扩展 schema。

**行业选择/传递:按 site 维度。**
- 在 `user_mapping.json` 的 `sites.{id}` 加 `"industry": "construction"`(已有 `client`,顺手补)。
- 管线对每条录音已能 device→site 解析 → device→site→`site.industry`→module。**无需 per-request
  用户输入。** meeting(可能跨 site)允许 event payload 显式传 `industry` 覆盖。
- 不用 Cognito 用户属性承载行业:数据本身 site-scoped,且单部署同时服务多行业/客户——site 维度更贴合。

**第三个行业零代码接入:** 丢 `config/industries/{new}.json` 到 S3 + 给相关 site 标 `industry: "{new}"`
(可选加一张 Transcribe 自定义词表)。解析/merge 逻辑通用,**不改代码。**

**两个示例:**
- `construction.json`:system_context "NZ construction site documentation";glossary(RFI、ITP、scaffold、
  pour、PS3/producer statement、toolbox talk、dwang/nogging);categories safety/progress/quality;
  constraints(标记安全隐患、记录 inspection/ITP、引用 NZ building code)。
- `food_manufacturing.json`:见上(HACCP/CCP/CIP/allergen/RMP;categories
  food_safety/production/quality/compliance;schema 扩展 `ccp_deviations`/`allergen_controls`/`batch_lot_refs`)。

### (c) 取舍
- overlay 而非整套 fork:base 改一次全行业受益 + 接新行业零代码;代价是写 merge 逻辑 + 外置 schema。
- site 维度而非 client/project/user:贴合 site-scoped 数据流,单部署多租户最省;代价是跨 site 的
  meeting 要显式传 `industry`(已留 override)。

---

## ADR-003 — IaC:CDK vs SAM

### (a) 现状评估
SAM template(~775 行,5 Lambda + Fargate + 告警)结构干净,Stage/Mappings/Conditions 用得对,CI 在用。
**但相当一部分核心资源在 SAM 之外**:API Gateway、Cognito User Pool/Client、DynamoDB 表(只作
Parameter 引用)、CloudFront、前端 S3、S3 event 通知(BUG-33)、python-docx Layer。另有硬编码
`SITE_NAME: 'SB1108 Ellesmere College'`(`template.yaml:332`)。

### (b) 决策:**留 SAM(明确)。**
当前规模 SAM 够用,迁 CDK 现在是净负债(重写成本 + 团队要吃 TS/Python CDK)。**但要做一件事:把
out-of-SAM 资源补进 IaC**(继续用 SAM,或加一个并列 stack `fieldsight-edge` 管 API GW + Cognito +
CloudFront + 前端 bucket)。这是解锁"真·dev 后端"和环境可复现的前提,**与是否迁 CDK 无关**。

**何时迁 CDK(明确门槛):** 出现任一即考虑——① 多行业/多租户导致 per-client stack 或资源 fan-out
组合爆炸(需要循环/抽象,YAML 写不动);② out-of-band 资源补齐后 template 超 ~1500 行且充满
`!Sub`/`!FindInMap`/`Conditions`;③ 需要跨 stack 类型化引用 + 单元测试基建。

**迁移路径(增量,非大爆炸):** 用 `cloudformation-include`(`CfnInclude`)把现 template 整体包进一个
CDK stack,先 1:1 等价,再逐资源用 L2 construct 重构;已存在资源用 `cdk import`。全程不停机、不重建。
**成本量级:** 包进来 ~0.5 天;逐资源重构 ~3–5 天(当前 ~25 资源);CI 改 `cdk deploy` ~0.5 天。

### (c) 取舍
- 留 SAM:零迁移成本 + 复用现有 CI;代价是 YAML 在多租户维度的天花板(到点再迁)。
- 先补 IaC 再考虑 CDK:"环境可复现"立刻兑现;代价是这部分 YAML 之后迁 CDK 要再过一遍(`CfnInclude`
  让这笔钱不冤)。

---

## ADR-004 — 数据层:S3 / RDS / DynamoDB + 全局 Ask Agent

### (a) 现状 & 三者定位
- **S3 = 唯一源(media + 生成产物),保留。** report JSON/docx、transcript、meeting minutes 全在 S3。
  API 直接按 key 取(`lambda_fieldsight_api.py`),发现用 `list_objects_v2`。
- **DynamoDB 的生态位 = 索引 + ledger + ACL,已在用。** `fieldsight-transcripts`(job 状态机)、
  `fieldsight-users`(JWT→role/site)、`fieldsight-audit`。关掉的 `ENABLE_DYNAMODB` 只是报告
  **items/reports 去规范化表**。
  - **需不需要?** 现在"按 key 取单份报告"不开 items 表也能跑。但**未来 app + Ask agent** 要做
    "本月全 site 的 safety flags""跨项目检索"这类结构化过滤时,可查询索引(DynamoDB GSI 或 Postgres)
    价值很大。建议:**保留 DynamoDB 做 ledger/ACL/索引;app 需要快速结构化读时打开 items/reports 表
    (GSI:`site#date`、`type`、`user`)。**
- **RDS 的适用 = 关系查询/事务 + pgvector。** 现在**无 RDS**。当自研 app 需要关系建模(用户/项目/
  权限/计费)或想用 pgvector 检索时再引入(Aurora Serverless v2 起步)。现在不需要。

**小结:** 直连 S3 拿原始文件/报告产物;DynamoDB 高频 key 查 + 状态/ACL/聚合索引;RDS 关系查询 +
未来 app 事务 + pgvector。三者分工,非互斥。

### (b) 全局 Ask Agent(RAG)
**起点:已有 `lambda_ask_agent.py` v1.0**,但**单报告 grounded**(给 date+user 取一份),**无检索层**。
做到"全局/全公司",缺的是**向量检索 + ACL 过滤**这一层,**生成仍复用现在的 Claude 直连**。

| 方案 | 适配度 | 说明 |
|------|--------|------|
| **Bedrock Knowledge Base** | **最快上手(推荐起步)** | 托管 RAG:数据源直接指现有 `reports/` S3,自动 chunk+embed + 存向量库 + `RetrieveAndGenerate`,新基建最少。注意会引入 Bedrock(现 Claude 走直连);底层若用 OpenSearch Serverless 有 OCU 成本地板,小规模要算账。 |
| **pgvector on Aurora** | 成本可控/未来 app 顺带 | 反正要上 Postgres 给 app 时顺手做向量,自己控 chunk/embed。代价:多运维一个 RDS。 |
| **OpenSearch(provisioned)** | 偏重/偏贵 | 混合检索强,对你规模过重。 |
| **DIY(S3 存 embedding + Lambda 暴力余弦)** | 极小规模够用 | 几千 chunk 时最省,规模上来要换。 |

**推荐:先 Bedrock KB over `reports/`**;若成本地板顶不住或决定上 Postgres 给 app,则切 pgvector。

**index / 衔接:**
- **索引对象**:已落 S3 的 report JSON / transcript / meeting minutes。按 topic/speaker-turn 切块,embed,
  带 metadata(date、site、user、type、industry)存向量库——metadata 用于**按角色层级 ACL 过滤检索**
  (复用 `fieldsight-users` 的 role/site)。
- **衔接点**:报告生成"写 S3"那步后挂 S3 event/管线步骤 upsert embedding(Bedrock KB 可设 S3 数据源 +
  事件/定时同步,几乎零代码)。
- **Ask agent v2**:retrieve(向量 + ACL 过滤)→ 现有 Claude 直连生成 → 带引用答案。即在
  `lambda_ask_agent.py` 前加 retrieve,后段基本不动。

### (c) 数据层演进路线图
1. **S3 为源**(不动)。
2. **补结构化索引**:打开/建 DynamoDB items/reports 表(GSI:site#date、type、user)。
3. **加向量索引**:Bedrock KB 指 `reports/`(或 pgvector),管线写 S3 后触发同步。
4. **Ask agent v2**:retrieve(向量 + ACL)→ Claude → 引用答案;逐步加会话记忆。
5.(可选)app 上 Postgres 后,把向量并入 pgvector,统一数据面。

---

## ADR-005 — 转录:托管 Transcribe vs 自建(Whisper)

### (a) 对比
| 维度 | AWS Transcribe(现状) | 自建 Whisper |
|------|----------------------|--------------|
| 单价 | ~$0.024/audio-min ≈ **$1.44/audio-h**(量大分级下降;**需复核当前价**) | 计算层可低 1–2 个数量级,但加运维/调优 |
| 运维 | 近乎零;托管 | 自扛容器/GPU 容量/重试/词表/diarization 管线 |
| 延迟 | 批处理分钟级;有 streaming | 取决于实例;GPU 批处理可很快 |
| 准确率 | 好,**自带 diarization + 词级时间戳**(`transcript_utils` 依赖!)+ 语言识别 | large-v3 在口音/多语种常**更好**;但**不自带 diarization**(需 WhisperX/pyannote),词级时间戳也要 WhisperX |

### (b) 拐点测算逻辑
- **关键纠正:Fargate 没有 GPU。** "Whisper on Fargate" 只能 CPU(faster-whisper/whisper.cpp),
  large-v3 在多核 CPU 约 0.5–2× 实时,贵且慢。**要快必须 GPU** → ECS-on-EC2(g4dn/g5)、
  **AWS Batch(GPU)** 或 SageMaker async,**不是 Fargate**。
- **GPU 成本直觉**:g4dn.xlarge(T4)~$0.526/h on-demand(spot ~$0.16/h)。faster-whisper large-v3
  在 T4 约 10–30× 实时 → 1 GPU-h 处理 ~15 audio-h → **~$0.035/audio-h(on-demand)/~$0.01(spot)**,
  对比 Transcribe ~$1.44/audio-h,计算层便宜 ~40–140×。
- **拐点不止看单价**:自建有固定成本(工程时间、GPU 容量、diarization 管线、词表/语言识别、监控)。
  **经验拐点:**
  - **< ~100–200 audio-h/月**:留 Transcribe(省的钱 < 运维成本)。
  - **~500–1000 audio-h/月以上**:自建年省数千~上万刀,值得建。
  - 测算式:`月省 ≈ 月audio小时 × ($1.44 − 自建每小时摊销) − 月固定运维`;稳定为正且覆盖一次性建设
    (~1–2 周工程)时迁。

### (c) 自建架构 + 渐进迁移(保住接口)
- **架构**:S3 上传 audio_segments → 触发 → **WhisperX on GPU**(ECS-EC2 或 AWS Batch GPU 队列,
  spot 优先)→ **adapter 把 WhisperX 输出转成 AWS Transcribe JSON 形状**(`items[]` 带
  start/end/speaker_label + `speaker_labels.segments`)→ 写到现在的 `transcripts/{user}/{date}/`。
  WhisperX 提供词级时间戳 + diarization(pyannote),正好映射到 Transcribe schema。
- **接口保住**:下游 `transcript_utils.normalize_transcript()` 和报告生成**完全不动**——它们只认那个
  JSON 形状。这就是"可切换 backend"。
- **渐进迁移**:加 env flag(`TRANSCRIBE_BACKEND=aws|whisper`),在 S3 event 处理处分流;先按 site/比例
  灰度,对比抽样准确率,再全量。**Transcribe 作 fallback 保留**(Whisper 队列失败→回退托管),零停机。

### (c-取舍)
- 留 Transcribe:省运维 + 现成 diarization;代价是规模上单价高。
- 自建 WhisperX/GPU:规模上大幅省钱 + 口音/多语种或更准;代价是建 diarization 管线、扛 GPU 容量与
  运维、做 adapter。**adapter + flag 让这一步可逆、可灰度**,是降低风险的关键。

---

## 待确认的小决策
1. **分支名:** `dev`(你的说法)还是对齐后端现有的 `develop`?建议统一一个(倾向把后端 workflow 也改
   认 `dev`,全栈一致)。
2. **dev 后端是否现在就补进 IaC**(ADR-003):决定 Phase 2 联调能多快开始;Phase 1(mock)不受此阻塞。

## 现在做 / 以后做
- **现在(低风险高价值):** 给 fieldsight-ui 建 dev workflow + `env.js` + 部到 dev(先 mock);把硬编码
  Cognito 从 fieldsight-ui 源码挪到 `env.js`。
- **soon(中等):** 把 out-of-SAM 资源补进 IaC(解锁真·dev 后端);多行业 prompt overlay + 加
  food_manufacturing + 外置 schema;按需打开 DynamoDB items/reports 索引表。
- **later(规模门槛):** Bedrock KB / pgvector + Ask agent v2;WhisperX 自建转录;SAM→CDK(YAML 到天花板时)。
