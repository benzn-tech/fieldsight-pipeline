# Construction Event Graph + Jev 决策层：对照本仓现状的评估

**Date:** 2026-09-24
**Status:** assessment（评估，非设计、非计划）。输入是附件
`FieldSight_Construction_Event_Graph_AI_Decision_Architecture_v0.1.md`；输出是
"哪些已经有了、哪些是真缺口、Jev 到底能放哪、顺序应该怎么排"。
**依据:** 全量读了 64 个 migration、`src/repositories/*`、`lambda_extract_session` /
`lambda_item_writer` / `lambda_programme_matcher` / `thread_match` / `evidence_match` /
`lambda_ask_agent`、`template.yaml`、两个 deploy workflow，以及 2026-08/09 的相关 spec。
Jev 的资料来自公开二手来源（typesafe.ai 与 docs.typesafe.ai 被本会话的出口代理拦截），
标注在 §4。

---

## 0. 一句话结论

附件的分层（Reality → Evidence → Event → Decision → Action）是对的，而且**本仓在"实质"上已经
走到了六成，缺的是"形式"**：

1. 证据层已经建成且比附件描述的更强（逐句 quote 机械核验 + 音频锚点），但 prod 上是关的。
2. 事件层存在（topics / findings / action_items / decisions / questions），但**没有稳定身份**：
   item-writer 每次抽取都 delete-and-reinsert，id 每一遍都换。这一件事同时卡住了生命周期状态、
   人工编辑保留、决策记录挂载、外部同步（Procore `origin_id`）和反馈数据集——**它是整份附件
   真正的前置条件，附件却没有提到。**
3. 决策层今天全是"LLM 自报置信度 ≥ 0.70 + 固定阈值 + 人工确认"。附件把 Jev 放在 Event → Decision
   的位置是对的，但公开测评显示 Jev **单个宽泛判断只有 62.6%，拆成 5 个原子问题 + 1000 条人工标签
   + 一层逻辑回归才到 95%**——那 95% 是 Jev + 你的标签 + 你维护的校准层，不是 Jev。
4. 因此顺序要反过来：**Jev 的 PoC 可以第一周就在现有 suggestion 表上跑影子评测（标签已经在
   Aurora 里）**，不需要先建图；而稳定事件身份是第一个要动的 schema 改动，不是附件排在第 5 步的
   "Event Graph"。

---

## 1. 附件七实体 × 本仓现状

| 附件实体 | 本仓对应 | 差距 |
|---|---|---|
| **Evidence** | `recordings`、`audio_segments/`、`transcripts/`（绝对时间 = 文件名 base + VAD offset + 词级时间，BUG-09）、`topics.evidence jsonb`（quote + `verified/verified_fuzzy/weak/unverified/absent/unchecked` + `segment_key_source` + `offset_sec`）、`topic_photos`（±2 min 时间绑定）、keyframes、`day_location_markers.quote` | 结构上**已超过附件 §9–11 的要求**（附件只要求"可寻址"，本仓还做了机械核验）。缺口是运营的：`EMIT_EVIDENCE` prod 为 false，所以 prod 的每个 topic 的 `evidence` 都是 NULL。 |
| **Event** | `topics`（容器）+ `findings`（最像附件的"事件"：observation/domain/severity/entity/recommended_action）+ `action_items` + `topics.decisions jsonb` + `topics.open_questions jsonb` | ① 无稳定 id（§2.1）；② decisions/questions 是 jsonb 无行身份；③ 无 `claim_type`；④ 无 `supersedes`。 |
| **Actor** | `users`（含 directory-only `kind`）、`memberships`、`speaker_voiceprints`（ECAPA 192 维，consent、employer）、`speaker_turn_names`、`name_aliases` | 说话人身份是全仓唯一真正的实体解析。其余全是自由文本：`action_items.responsible`、`findings.entity_name/entity_trade`、`decisions[].decided_by`、`participants`。`session_brief.entities` 抽了 22 个实体/会但只留在 S3，spec 明说"No entity resolution across sessions"。 |
| **Location** | `sites`（lat/long、address）、`day_location_markers`（说话人自报位置，按时间保持） | 无 zone/level/room 层级；`programme_tasks.zone` 是文本。 |
| **Object** | 只有 `programme_tasks`（层级、deps FS/SS/FF/SF、assignees、版本快照） | 无构件/材料/设备实体。IFC/BIM 关联现在谈为时过早（§5）。 |
| **Decision** | `programme_progress_suggestions`（confidence、match_evidence、pending/confirmed/rejected/stale）、`topic_thread_suggestions`（score、gap_days）、`findings.impact_*` 列（直接应用，不经人）、`classification_feedback` | 每个判定都是"一次 LLM 调用 + 一个阈值"，**没有"决策记录"这个对象**：问了什么问题、哪个模型哪个版本、输入快照、概率、阈值、人最后怎么判——散落在三张形状不同的表里，无法汇成评测集。 |
| **Action** | suggestion confirm/reject 端点、`programme_delay_flags`、action-item PATCH、`compliance_resolutions`、SES 邮件；Teams/Notion 仅用于设备告警 | 无对外工作流。Procore 是 2026-09-09 的 spike（两向同步，卡在两个非技术门槛）。Jira 无。 |

**关系边**：只有 `findings.programme_task_id`（列，非表）、`topics.thread_id`（人工确认）、
`programme_task_deps`、`report_chunks.topic_id`。附件 §19 的 `blocks / caused_by / supersedes /
contradicts / resolves / supported_by` 一条都没有。

**生命周期**：附件 §20 的 RAW→CANDIDATE→VALIDATED→ACTIVE→RESOLVED→VERIFIED 不存在；抽取项直接
以 `open` 入库。已有的三套小状态机（suggestion 的 pending/confirmed/rejected/stale、threads 的
"AI 永远不写 closed"、`compliance_resolutions` 按 content hash 存 resolved）是本仓自己测出来的
正确形状，比附件的六级梯子更可靠（§5.3）。

**置信度**：附件 §21 要求"抽取置信度 ≠ 决策置信度"。本仓实际有三种且已经分开：
`evidence_status`（机械核验，最可信）、`work_confidence` / `declared_site.confidence`（模型自报，
已实测不可靠——`priority` 44% 是 high、`urgency` 48% 是 high，两个字段都已作为排序键失效）、
matcher 的 `confidence`（模型自报 + 0.70 门）。附件漏了第一种，而它是三种里唯一有测量基础的。

---

## 2. 真正的结构性缺口（按阻塞面排序）

### 2.1 事件没有稳定身份（阻塞一切）

`lambda_item_writer.write_extraction_items` 对每个 extraction key 执行
`DELETE FROM topics WHERE source_s3_key=%s` 再整体重插，children CASCADE。live → final 是同一个
key 的常规两遍，所以：

- check-off 在 final 落地时丢失（代码只 `_warn_if_discarding_checkoffs`）；
- `questions` 不能有 `answered` 状态（2026-09-07 spec §5 明确因此放弃）；
- `todo_collapse` 只能读时去重、不能写；
- `compliance_resolutions` 被迫用 content hash 而不是 row id 做键；
- 任何外部系统（Procore `origin_id`）无法引用一个"事件"。

附件 §6.3 推荐 event sourcing（"当前状态 = 事件历史的结果"），推荐得对，但应用点不在"项目状态"
这个宏观层，而在**抽取项本身**：抽取项应当 append-only，新一遍抽取产生的项通过
`supersedes`（按 content hash / 相似度对上旧项）接到旧项上，旧项不删。本仓已有的两个先例
正是这个形状：`speaker_turn_names.superseded_at`（supersede-then-insert，按 `_SOURCE_RANK` 排）
和 `compliance_resolutions` 的 hash 键。**这是第一个要做的 schema 改动，不是附件排在第 5 步的
"Event Graph"。**

### 2.2 决策不是对象

要评测任何决策模型（Jev 或别的），需要的记录是：`question_id, model, model_version, input_hash,
input_snapshot_key, probabilities, threshold, auto_outcome, human_outcome, human_reason,
decided_at`。今天 matcher 把其中一半塞进 `match_evidence jsonb`，threads 存 `score`，
classification 存 `classifier_confidence`，三张表三种形状，**没有一张能直接变成校准集**。
一张 `decision_records` 表（每一次门控判定写一行，无论谁做的判定）是 Jev PoC 的前置条件，
也是附件 §22.9 "Feedback Loop" 真正需要的东西。

### 2.3 反馈是死胡同

`classification_feedback`、`content_edits`、suggestion 的 confirmed/rejected、
`speaker_name_rejections` 都在写，**没有任何读者把它们喂回 prompt、阈值或模型**。拒绝只通过
dedupe key 阻止再提议。而这批数据正好是 Jev 测评报告里"1000 条人工标签"的那一类——它已经存在，
只是散着。

### 2.4 `claim_type` 缺失，但不能靠模型自报

附件 §3.2 的 observed / stated / inferred 很有价值。但 2026-08-12 spec 实测：让模型多填一个
`evidence` 字段，它会给编造的断言配上引用并被判 `verified`；唯一有效的是"允许为空"的逃生口。
所以 `claim_type` 不应该是又一个模型自报字段。可行的来源有两个：
① 从已核验的 quote + 说话人推导（说话人是 contractor 且句式是承诺 → `stated`；说话人是
录音者且句式是描述 → `observed`）；② 作为 Jev 的一个 noul 问题（"这句话是说话人亲眼所见吗？"）
并用人工标签校准。两者都能测，自报不能。

### 2.5 实体解析：不要从零建本体

`findings.entity_name`、`responsible`、`decided_by` 是自由文本，`session_brief.entities` 有
kind + aliases 但不落库。**Procore Block B（拉 directory/projects）恰好提供权威的 actor 与
project 列表**，那才是实体解析的锚，而不是自建 ontology。在 Procore 之前，最便宜的一步是把
brief 的 `entities` 落到已有的 `name_aliases`（2026-08-28 spec 已经这么定了）。

---

## 3. 对附件的逐条修订意见

| 附件章节 | 意见 |
|---|---|
| §4 核心事件字段 | 加 `content_hash`、`supersedes_id`、`validation`（§5.3）；`subject_id` / `location_id` 在很长时间内 95% 为 NULL，改成 `{stated, matched_id, confidence}` 三元组（`declared_site` 已经是这个形状）。`source_ids` 已有对应物（`evidence[].segment_key_source + offset_sec`）。 |
| §7 "LLM 不直接建图" | 本仓已是如此。但附件的"segmentation → 每段抽候选事件"不必做：本仓是整个会话一次 JSON 调用（≤300k 字符，elide middle），实测质量可接受；"candidate"是**写端**的状态，不是 prompt 的改动。 |
| §8 中间抽取 schema | 与 `EXTRACTION_SCHEMA` 基本重合。差异只在 `certainty` 字段——按 §2.4 处理。 |
| §12–15 Decision Engine | 方向对。但 D1–D10 写法是"宽泛判断"（"Is there programme risk?"），正是 Jev 测出 62.6% 的那种问法。要拆成可观察的原子问题（"提到了具体日期吗"、"被点名的一方是分包吗"、"该任务在 programme 关键路径上吗"），权重逻辑留在代码侧。另：决策引擎要**读 Aurora**又要**调外部 API**，按 BUG-36 必须拆成 in-VPC 读 + 非 VPC 调用，走 `match_requests/` 那套 S3 请求件模式——附件把它画成一个框。 |
| §16 三种 AI 能力 | 同意。本仓已有全部三种：LLM（qwen3.8-flash thinking / claude-sonnet-4-6）、向量（DashScope text-embedding-v4 @1024 + pgvector HNSW + tsvector）、决策（阈值）。补一条附件没写的：**机械核验**（evidence_match、question_admission、corroboration_gate 都是纯规则），它比三者都可靠且免费，应保留为第零层。 |
| §20 生命周期 | 六级线性梯子会重蹈 `priority`/`urgency` 的覆辙。改成三个正交小枚举：`validation`（candidate / confirmed / rejected）、`status`（open / resolved / closed + reason）、`evidence_status`（已存在）。AI 只能写 candidate 与 resolved-proposed，`closed` 只有人能写（threads spec 决策 1）。 |
| §22 顺序 | 见 §6。Jev PoC 从第 7 步提到第 1 步（影子评测，零 schema 改动）；"Event Schema"从第 1 步改为"稳定身份 + 决策记录"。 |
| §22.8 Workflow（Jira/ACC/Teams） | 本仓方向是 Procore（spike 已做，两向）。Observation / RFI / Punch 的 `origin_id` 需要稳定事件 id → 回到 §2.1。Jira/ACC 不在任何现有材料里，先不列。 |
| §24 moat | 同意。补一句：本仓**已经测出来**的差异化是证据溯源（quote 机械核验 + 音频锚），附件 §11 把它写成"潜在差异点"，其实已经建成，只差 prod 打开。 |
| 附件完全没提的 | 多租户 ACL（每张表 company/site 双限定，图必须继承 `scope.visible_scope`）；NZ 时间语义（BUG-19/37）；隐私与同意（voiceprint 有 consent_basis；把转写文本送第三方是同类决定，见 §4.4）；删除传播（`redactions` 的 tombstone 必须能删到事件与决策记录）。 |

---

## 4. Jev 现实核查（公开二手来源，2026-09-24）

typesafe.ai / docs.typesafe.ai 被代理拦截；以下来自搜索摘要，**接入前要用官方文档复核**。

### 4.1 它是什么

- 2026-09-15 发布，09-21 托管 API 开放。`POST https://api.typesafe.ai/v1/systemone`，
  `model: "jev-latest"`，`state`（文本或 JSON）+ `questions` map，三种问题类型：
  `choice`（≤255 选项，返回每项概率 + confidence）、`score`（2–10 级有序量表，返回加权位置）、
  `noul`（是/否，返回 P(yes)）。一次调用并行回答全部问题。
- 上下文 64k，`state` + 最长单个问题 ≤ 32k。限速 1200 rpm / 250k tps。
- $0.042 / M 输入 token，输出免费；70–500 ms。
- 训练目标是 RLCD（校准），不是 RLHF。非自回归，不产生文本，**不可能输出 schema 外的值**。
- **不提供 fine-tune / LoRA / 按客户适配**；不用客户数据训练。任何"学习"都在你这边做。
- Python SDK `typesafe-sdk`（≥3.10）；也可经 Cloudflare AI、Pydantic AI 接入。
- 托管："GLOBAL; residency unknown; DPA available"。

### 4.2 测评数字（都要打折看）

| 来源 | 数字 | 含义 |
|---|---|---|
| TypeSafe 自测 4 workflow | Jev 67.8%，GPT-5.6 Sol 74.1%，Claude Opus 5 73.1% | "准确率"= 与 GPT-6 / Claude Fable 5.1 共识标签的一致率，**不是**人工 ground truth |
| 独立：2000 封邮件钓鱼判定 | 单问 **62.6%** → 拆 5 个原子问题 + 1000 人工标签 + 逻辑回归 **95.0%** | 95% 是 Jev + 你的标签 + 你维护的回归层。95% 门槛下自动接受率：Jev 单独 46% → 加层 72% |
| 独立：合成工单 | ECE 0.107，噪声底 4.4× | "校准"是目标，不是已达成 |
| TypeSafe 员工实测 | 端到端提速 15.9%（而非 193×） | 流水线延迟被别的环节主导 |

**结论：Jev 是"廉价、快、结构安全的门控器"，不是"更准的判断者"。它的价值取决于你有多少标签
和把问题拆得多细——两者都是本仓的工作，不是它的。**

### 4.3 在本仓今天就能放的位置（不需要图）

这些判定今天都是"LLM 自报 confidence ≥ 0.70"或纯规则，**每一个都已经有人工标签**：

| 判定 | 今天 | 标签来源 | Jev 形状 |
|---|---|---|---|
| finding → programme task 匹配 | embedding ≤0.55 → LLM 从幸存者里选 + conf ≥0.70 | `programme_progress_suggestions.state`（confirmed/rejected） | 每个候选一个 noul "这条 finding 说的是这个任务吗"，5 候选一次调用 |
| topic → thread | TF-IDF ≥0.25、gap ≤45 天，仅提议 | `topic_thread_suggestions.status` | 同上 |
| work / non_work | 模型自报 `work_confidence` | `classification_feedback`（已算 precision） | choice + 校准 |
| action-item 准入 | prompt 规则 | `tests/fixtures/task_admission_ground_truth.json` | noul "一个具体的人能做完并打勾吗" |
| finding severity / domain | 模型自报 | `compliance_resolutions`、`content_edits` | score 3 级 |
| Ask 路由（metric / RAG / web）、`question_admission`、`corroboration_gate` | 正则 | ask 日志 | choice / noul；正则保留为硬否决 |

前两项最值钱：它们的"错匹配比漏匹配代价高"的性质，正好是 noul + 按问题校准阈值 + 覆盖率报告的
用武之地。

### 4.4 接入前必须过的两道非技术门

1. **数据出境**：把客户站点的对话内容（哪怕是抽取后的事件文本）送到一家发布一周、托管地不明的
   第三方。与 Procore spike §5.1（RAG 走 Agentic API 的政策）和 2026-08-31 corroboration spec
   的 P1 隐私决定同一类，需要产品决定。可缓解：只送结构化事件 JSON（不送转写）、脱敏人名
   （`name_aliases` 反向）、先只在 TEST 上跑。
2. **供应商成熟度**：一周龄的 API，无 SLA 记录。设计上必须能整体降级回今天的 LLM 门（把 Jev
   当 provider 插在 `decision_records` 后面，而非替换）。

---

## 5. 明确不做 / 推后

- **IFC / BIM 对象关联**（附件 §5–6.1）：没有对象数据源，客户没给模型。`programme_tasks` 是
  唯一结构化对象，够用两个季度。
- **Zone / level / room 本体**：先让 `programme_tasks.zone` 与 `day_location_markers` 的文本能
  互相匹配（一个 alias 表的事），不建层级。
- **通用 workflow 引擎**（Jira / ACC / Teams）：只做 Procore + 已有邮件。
- **全会话 ASR 二次转写 / 换供应商**：CLAUDE.md 已有测量结论，不重开。
- **附件 §20 的六级生命周期**：替换为 §3 的三个正交枚举。

---

## 6. 建议顺序（三条并行轨道）

```
Track A  Jev 影子评测（1–2 周，零 schema 改动，TEST 环境）
         ├─ 从 programme_progress_suggestions / topic_thread_suggestions /
         │  classification_feedback 导出 (输入, 人工判定) 对
         ├─ 每类判定拆 3–5 个原子 noul/choice；同一配置跑两遍（CLAUDE.md 方法规则）
         ├─ 报告：每问题 accuracy / ECE / 在 95% 门槛下的覆盖率 vs 今天的 0.70 门
         └─ 产出决定：Jev 是设计中心还是可插拔 provider
                         │
Track B  稳定事件身份 + 决策记录（2–4 周，关键路径）
         ├─ 抽取项 append-only + content_hash + supersedes_id；item-writer 改为
         │  supersede-then-insert（照 speaker_turn_names 先例），不再 DELETE
         ├─ decisions / questions 从 jsonb 升为行（2026-09-07 spec 里被 id 漂移否掉的
         │  status 列这时才能加）
         ├─ decision_records 表；matcher / thread_match / classifier 每次判定写一行
         └─ 顺带解决：check-off 丢失、questions 无法 answered、todo_collapse 只读
                         │
Track C  边、claim_type、生命周期、Procore（B 之后）
         ├─ event_links(src, dst, type ∈ {supersedes, supported_by, blocks, resolves,
         │  contradicts, located_at, involves})，先只写 supersedes / supported_by
         ├─ claim_type 按 §2.4 推导 + Jev 校准，不自报
         ├─ prod 打开 EMIT_EVIDENCE（Track B 之前就可以，独立）
         ├─ Procore A → B（B 提供 actor / project 权威列表 = 实体解析的锚）
         └─ Procore D（push），origin_id = 事件稳定 id
```

Track A 与 B 互不依赖；A 的结论决定 C 里"决策引擎"那格是 Jev 主导还是 LLM 主导 + Jev 门控。

---

## 7. 需要 owner 决定的事（2026-09-24 已决，记录在此）

**已决**：①事件 = findings + action_items + decisions + questions 四类行，topic 为容器；②Jev 出境接受，但只送结构化事件 JSON、不送转写、人名脱敏，先 TEST；③对外工作流目标 = Procore；④Location 先做 2 层；⑤标签只开平台级词表；⑥Track A 与 B 并行。实施计划：`plans/2026-09-24-track-a-jev-shadow-eval.md`、`plans/2026-09-24-track-b-stable-identity-and-decision-records.md`。

原始问题保留如下。


1. **"事件"的粒度**：附件例子"Ductwork incomplete"对应本仓的 `finding`，不是 `topic`。
   建议：事件 = findings + action_items + decisions + questions（四种类型的行），topic 降为
   "episode / 容器"。这决定 Track B 的表结构。
2. **数据出境**（§4.4.1）：能否把事件文本送 TypeSafe；不行的话 Track A 只能在脱敏后的
   TEST 数据上跑，结论会打折。
3. **第一个对外工作流目标**：Procore（已 spike）还是附件写的 Jira / ACC。影响 Track C 的
   `event_links` 与导出 schema 设计。
4. **是否把这份评估升级为 ROADMAP.md 的替代**：ROADMAP.md 停在 2026-03-22，README /
   DEPLOYMENT-RUNBOOK 的部署描述也已过期（prod 现在是完整 SAM deploy，不是 code-only）。

---

## 附：本仓当前状态速记（供附件读者对齐）

- **模型**：`LLM_PROVIDER` = `qwen`（DashScope / OpenRouter 兼容口，`qwen3.8-flash` 结构化任务、
  `qwen3.6-flash` Ask）或 `anthropic`（`claude-sonnet-4-6`；Ask 用 `claude-haiku-4-5`）。
  thinking 在 report / minutes / extract final 开，live / rolling 关。Corroboration 与 web answer
  走 OpenRouter `google/gemini-3.8-flash`。Embedding DashScope `text-embedding-v4` @1024。
  ASR prod `transcribe`、test `elevenlabs scribe_v2`（CLAUDE.md 结论：留在 ElevenLabs）。
- **部署**：`develop` → `deploy.yml` → `fieldsight-test`；`main` → `deploy-prod.yml`（需 production
  环境审批）→ `fieldsight-prod`。同一 Aurora 集群两个库（`fieldsight` / `fieldsight_test`）。
  S3 触发器全部在模板外，由 `scripts/wire-s3-events.sh` 接线（BUG-33）。
- **prod 关着的开关**：`AUTHORITY_FLIP`（workflow 默认 false，CLAUDE.md 说 prod 已开——以 repo
  var 为准）、`EMIT_EVIDENCE`、`ENABLE_FINALIZE`、`ENABLE_GROUP_MERGE`、`SUGGEST_THREADS`、
  `SESSION_BRIEF`、`SPEAKER_IDENTITY_MODE=off`。附件里"证据层"的大半功能在 prod 上未启用。
- **测试**：393 个 unit 文件（≈5068 用例）+ 44 个 integration（pgvector pg16 容器，CI 全跑）。
