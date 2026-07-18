# Content-Filter & Privacy System — Design (2026-07-17)

**Status:** Design / for review. Pairs with
`2026-07-17-visibility-permission-model-design.md` (they interlock at the
site_manager's authority and the layered review gate).

**Scope:** fieldsight-pipeline (extraction/classification, redaction store,
downstream enforcement) + fieldsight-ui (review UI, masked display). Adds a
three-layer content filter and a privacy-preserving soft-delete so field
recordings can flow to team/company analytics without exposing profanity, PII,
or personal off-work conversation.

---

## 1. Problem & intent

Field recordings faithfully capture everything said on site — including swearing,
personal/off-work chatter, and PII. Today that content flows into topics/findings
and (soon) company-level analytics with no cleansing and no human gate. The
customer (esp. the **site_manager**, who reviews the minutes first after an
inspection) needs:

1. Profanity / uncivil words **masked** in what people read.
2. Non-work conversation **removed** — but at the right **granularity** (a topic
   may hold several conversation segments; only the *personal* segments should
   go, not the whole topic).
3. The **right to delete** a topic or segment so a person's privacy is **not
   pulled into later team/company analysis** — while the **record is still
   correctly preserved** (recoverable, auditable), not truly destroyed.

## 2. Benchmark (how the field does it)
- **Heidi Health** (AI medical scribe): audio discarded immediately after
  transcription; **transcript is ephemeral, the structured note is the durable
  artifact**; the **clinician reviews/edits before it counts** (human is the
  final arbiter); auto **pseudonymization** (name/DOB/address → "Jane Doe") before
  any third-party sharing.
- **Contented.ai** (records → structured docs; used in **construction**):
  **faithful transcription** — explicitly keeps "lots of swearing" verbatim —
  and applies **structured templates** to the transcript rather than generating
  from scratch (no fabrication); never trains on customer data; never stores
  recordings.
- **Takeaways adopted here:** (a) transcribe faithfully, filter/mask at the
  **display and structured layers**, never mutate the raw transcript; (b) the
  **LLM extraction is itself the first non-work filter** (templates only pull
  relevant content); (c) **human review happens before the content is treated as
  official** — which for FieldSight means *before company aggregation*.

## 3. Design — three-layer pipeline

```
raw transcript (faithful, immutable)
      │
      ├─(F1) profanity/PII MASK  ──────────────► display layer (mask on render)
      │
      ├─(F2) work-relevance CLASSIFY (per turn) ─► auto-FLAG suspected non-work
      │                                            → site_manager CONFIRMs → soft-exclude
      │
structured topics / segments / findings
      │
      └─(F3) site_manager REVIEW ───────────────► soft-delete any topic/segment
                                                   (tombstone; excluded from analysis)
```

### 3.1 Filter 1 — profanity / PII masking (display layer)
- Underlying transcript/segment text is stored **faithfully** (Contented model).
- Masking is applied at **render time** and on any surface a human reads
  (minutes UI, topic detail). A `mask(text)` function replaces matched tokens
  with `f***`-style masks.
- Sources: a **profanity lexicon** (Open decision E1: standard list vs
  company-configurable) + **PII detection** (names not in the site roster,
  phone numbers, addresses → mask). PII masking mirrors Heidi's pseudonymization.
- **Also applied to analytics/RAG inputs** so masked tokens never reach
  cross-company embeddings/LLM prompts (Open decision E2: mask vs drop for RAG).

### 3.2 Filter 2 — non-work removal (auto-flag + human-confirm)
- **Granularity = the conversation turn/segment**, not the topic. The transcript
  already carries speaker turns with timestamps (`transcript_utils.normalize_transcript`);
  extraction attaches segments to each topic.
- The extraction step **classifies each segment** as `work` | `non_work`
  (personal life, off-topic banter) with a confidence.
- Segments classified `non_work` are **flagged (soft), not auto-removed** —
  surfaced to the site_manager highlighted. The site_manager **confirms** →
  the segment is soft-excluded (tombstoned, §3.4); **unconfirmed flags stay in
  the minutes** (a suspected-personal segment is never silently dropped).
- This keeps a topic's *work* segments intact while lifting only the confirmed
  personal ones out — answering the granularity concern.

### 3.3 Filter 3 — site_manager human review
- After an inspection, the site_manager opens the day's minutes (they are the
  first reviewer). Heidi-style **tabbed** view: structured minutes ↔ underlying
  segments.
- They can **soft-delete** any **topic** or **segment** (a superset of confirming
  F2 flags), with an optional reason.
- Review is also the **publish gate** (§3.5): reviewing/publishing releases the
  (redacted) minutes to company/team aggregation.

### 3.4 Soft-delete / tombstone model (the "delete but preserve" answer)
Never hard-delete. A redaction is a **tombstone** on the target:

```
redactions(
  id, company_id,
  target_type   ('topic' | 'segment' | 'finding'),
  target_id,
  reason,                       -- 'non_work' | 'privacy' | free text
  actor_user_id, actor_role,
  created_at,
  scope         ('analysis'      -- excluded from team/company analysis, still
                                 --   visible to the site_manager/recorder + admin
                | 'all')         -- hidden from everyone below admin
)
```

- **Original content is retained** (access-controlled), so the record is correct,
  **recoverable**, and **auditable** — answering *"如何保证记录能被正确保存"*.
- **Every downstream read honors the tombstone**: team/company aggregation,
  portfolio/insights roll-ups, **cross-project RAG**, and exports **exclude**
  redacted targets. This is the single enforcement point that keeps privacy out
  of later analysis — answering *"不纳入之后的分析"*.
- **Who still sees it** (Open decision E3): default = the site_manager/recorder
  and admin can still see redacted-for-`analysis` items (marked "excluded by X");
  everyone else cannot. `scope='all'` hides from all non-admins.
- **Recovery**: a redaction can be reverted (un-tombstone) by the actor or admin.
- **Optional hard purge** (Open decision E4): a scheduled job truly deletes
  content tombstoned `> N` days (GDPR-style erasure) — off by default, since the
  stated need is "preserve the record."

### 3.5 Review gate & state model (layered — from companion spec)
- Each daily report / topic set has a state: **`open` (site-immediate)** →
  **`reviewed` (published to company)**.
- **Site/self tier reads everything immediately** regardless of state
  (timeliness — the site_manager and their own site see items as they land).
- **Company/regional aggregation reads only `reviewed` topic sets, minus
  redactions.** So personal content never enters company analysis even briefly:
  it is either redacted before review, or the whole set is unpublished until
  reviewed.
- Auto-flagged (F2) but unconfirmed segments: included at site level, **excluded
  from the company tier until the site_manager reviews** (fail-safe toward
  privacy at the company tier, toward completeness at the site tier).

## 4. Data-model changes
- `redactions` table (above); indexed by `(company_id, target_type, target_id)`.
- `topics` / segment rows gain a derived `is_redacted` read helper (join or
  materialized flag) so hot read paths don't N+1.
- Report/topic-set `review_state` (`open` | `reviewed`) + `reviewed_by` /
  `reviewed_at`.
- Segment-level `work_class` (`work` | `non_work` | null) + `work_confidence`
  from extraction, and `f2_confirmed` (bool) once the site_manager acts.
- No change to the **raw transcript** artifacts (faithful, immutable).

## 5. Enforcement points (must all honor redaction + review_state)
`repositories/topics.list_topics_for_date`, the compliance/tasks/insights/
strategic aggregators, the RAG retrieval/embedding inputs (`lambda_ask_agent`,
embedding jobs), Word/report exports, and any company-tier dashboard query. A
shared `exclude_redacted(rows, tier)` / `company_visible(...)` helper keeps the
rule in one place.

## 6. Rollout
1. `redactions` table + `exclude_redacted` helper wired into aggregation/RAG
   (no UI yet) — establishes the enforcement backbone.
2. F1 masking (display + RAG input).
3. F3 review UI (site_manager soft-delete topics/segments) + `review_state`
   publish gate.
4. F2 auto-classification + confirm UI (needs the extraction-side classifier).
Each step ships independently; F3 before F2 so the human gate exists before any
automated flagging.

## 7. Open decisions (for your review)
- **E1** — profanity lexicon: standard list vs per-company configurable. Recommend
  **standard + per-company additions**.
- **E2** — RAG/analytics input: **mask** profanity/PII vs **drop** the token.
  Recommend **mask** (preserves meaning, hides the word).
- **E3** — post-redaction visibility: site_manager/recorder+admin still see
  (marked) vs fully hidden below admin. Recommend **still see, marked** for
  `analysis` scope.
- **E4** — hard-purge job: on (with N-day timer) vs off. Recommend **off by
  default**, configurable.
- **E5** — does F2 classification run in the existing extraction LLM call
  (cheaper, one pass) or a dedicated pass? Recommend **fold into extraction**.
- **E6** — publish granularity: whole daily report vs per-topic review. Recommend
  **per-report with per-topic redactions**.

## 8. Risks
- **Over-masking / over-flagging** erodes trust — F1/F2 must be tunable and F2
  never auto-drops (human confirm is the guard).
- **Redaction bypass** = privacy breach — every company-tier read path must go
  through the shared helper; add tests asserting redacted/unreviewed content is
  absent from each aggregation and from RAG.
- **Raw transcript retention** vs privacy law — the tombstone keeps originals;
  E4's purge is the escape valve if a customer/jurisdiction requires true erasure.

---
---

# 【中文翻译】内容 Filter 与隐私系统 —— 设计(2026-07-17)

**状态:** 设计 / 待审。与 `2026-07-17-visibility-permission-model-design.md` 配套(两者在 site_manager 权限与分层审阅门处咬合)。

**范围:** fieldsight-pipeline(抽取/分类、redaction 存储、下游执行)+ fieldsight-ui(审阅 UI、遮蔽显示)。加入三层内容 filter 与隐私软删除,使现场录音能流入团队/公司分析,同时不暴露脏话、PII 或个人非工作对话。

---

## 1. 问题与意图
现场录音忠实记录工地说的一切——包括脏话、个人/非工作闲聊、PII。现状是这些内容无清洗、无人工门就流入 topics/findings 并(即将)进入公司级分析。客户(尤其巡检后**第一个审阅 minutes 的 site_manager**)需要:
1. 脏话/不文明词在阅读界面被**遮蔽**。
2. 非工作对话被**剔除**——但要对**颗粒度**:一个 topic 可能含多段对话,只该去掉*个人*段,而非整个 topic。
3. 有**删除权**,把某人的隐私从**之后的团队/公司分析中剔除**——同时**记录仍被正确保存**(可恢复、可审计),而非真正销毁。

## 2. 基准(行业怎么做)
- **Heidi Health**(医疗 AI scribe):转写后音频立即弃;**transcript 临时、结构化 note 才是留存物**;**医生审阅/编辑后才算数**(人是最终裁判);对第三方共享前自动**伪匿名化**(姓名/生日/地址→"Jane Doe")。
- **Contented.ai**(录音→结构化文档;**建筑业**在用):**忠实转写**——明确保留"大量脏话"原样——并对 transcript **套结构化模板**而非凭空生成(不编造);不拿客户数据训练;不存录音。
- **采纳的启示:**(a)忠实转写,过滤/遮蔽放在**展示层与结构化层**,绝不改原始 transcript;(b)**LLM 抽取本身就是第一道非工作 filter**(套模板只拉相关内容);(c)**人审在内容被当作正式之前**——对 FieldSight 即*在公司聚合之前*。

## 3. 设计 —— 三层管线

```
原始 transcript(忠实、不可变)
      │
      ├─(F1) 脏话/PII 遮蔽 ──────────────► 展示层(渲染时遮蔽)
      │
      ├─(F2) 工作相关性分类(逐 turn)──► 自动标记疑似非工作
      │                                    → site_manager 确认 → 软排除
      │
结构化 topics / segments / findings
      │
      └─(F3) site_manager 审阅 ──────────► 软删除任意 topic/段
                                            (tombstone;排除出分析)
```

### 3.1 Filter 1 —— 脏话/PII 遮蔽(展示层)
- 底层 transcript/段文本**忠实存储**(Contented 模型)。
- 遮蔽在**渲染时**、以及任何人读的界面(minutes UI、topic 详情)施加。`mask(text)` 把匹配 token 换成 `f***` 式遮蔽。
- 来源:**脏话词表**(开放决策 E1:标准表 vs 公司可配)+ **PII 检测**(不在工地名册的人名、电话、地址→遮蔽)。PII 遮蔽仿 Heidi 伪匿名化。
- **同样施加于分析/RAG 输入**,使遮蔽 token 绝不进入跨公司嵌入/LLM prompt(开放决策 E2:RAG 用遮蔽 vs 丢弃)。

### 3.2 Filter 2 —— 非工作剔除(自动标记 + 人工确认)
- **颗粒度 = 对话 turn/段**,非 topic。transcript 已带说话人 turn + 时间戳(`transcript_utils.normalize_transcript`);抽取把段挂到每个 topic。
- 抽取步**对每段分类** `work` | `non_work`(个人生活、题外闲聊)带置信度。
- 判为 `non_work` 的段**只标记(软)、不自动删**——高亮呈给 site_manager。site_manager **确认** → 该段软排除(tombstone,§3.4);**未确认的标记仍留在 minutes**(疑似个人段绝不静默丢)。
- 这样保住一个 topic 的*工作*段,只把确认的个人段抬走——回答颗粒度诉求。

### 3.3 Filter 3 —— site_manager 人审
- 巡检后 site_manager 打开当日 minutes(第一审阅人)。Heidi 式**并排(tabbed)**:结构化 minutes ↔ 底层段。
- 可对任意 **topic** 或 **段**做**软删除**(F2 确认的超集),可选原因。
- 审阅同时是**发布门**(§3.5):审阅/发布把(已 redact 的)minutes 释放给公司/团队聚合。

### 3.4 软删除 / tombstone 模型("删而不失"的答案)
绝不硬删。redaction 是目标上的 **tombstone**:

```
redactions(
  id, company_id,
  target_type   ('topic' | 'segment' | 'finding'),
  target_id,
  reason,                       -- 'non_work' | 'privacy' | 自由文本
  actor_user_id, actor_role,
  created_at,
  scope         ('analysis'      -- 排除出团队/公司分析,site_manager/记录者+admin 仍可见
                | 'all')         -- 对 admin 以下全隐藏
)
```

- **原文受控保留**,故记录正确、**可恢复**、**可审计**——回答*"如何保证记录能被正确保存"*。
- **每条下游读取都尊重 tombstone**:团队/公司聚合、portfolio/insights roll-up、**跨项目 RAG**、导出——**一律排除** redacted 目标。这是把隐私挡在之后分析之外的**单一执行点**——回答*"不纳入之后的分析"*。
- **谁仍可见**(开放决策 E3):默认=site_manager/记录者与 admin 仍可见 `analysis` 域 redacted 项(标注"已由 X 排除");其余不可见。`scope='all'` 对所有非 admin 隐藏。
- **恢复**:redaction 可被 actor 或 admin 撤销(un-tombstone)。
- **可选硬清**(开放决策 E4):定时任务真删 tombstone `> N` 天的内容(GDPR 式擦除)——默认关,因为诉求是"保留记录"。

### 3.5 审阅门与状态模型(分层——来自配套 spec)
- 每份日报/topic 集有状态:**`open`(本站即时)** → **`reviewed`(发布到公司)**。
- **本站/自己层不论状态都即时读全部**(时效性——site_manager 与本站一落地就见)。
- **公司/区域聚合只读 `reviewed` 的 topic 集,减去 redaction。** 于是个人内容绝不进入公司分析、哪怕短暂:要么审前已 redact,要么整集在审前不发布。
- F2 自动标记但未确认的段:本站层含,**公司层在 site_manager 审前排除**(公司层偏隐私、本站层偏完整)。

## 4. 数据模型改动
- `redactions` 表(见上);按 `(company_id, target_type, target_id)` 建索引。
- `topics` / 段行增派生 `is_redacted` 读辅助(join 或物化标记),避免热路径 N+1。
- 报告/topic 集 `review_state`(`open` | `reviewed`)+ `reviewed_by` / `reviewed_at`。
- 段级 `work_class`(`work` | `non_work` | null)+ `work_confidence`(来自抽取),及 `f2_confirmed`(bool,site_manager 动作后)。
- **原始 transcript** 制品不变(忠实、不可变)。

## 5. 执行点(都必须尊重 redaction + review_state)
`repositories/topics.list_topics_for_date`、compliance/tasks/insights/strategic 各聚合器、RAG 检索/嵌入输入(`lambda_ask_agent`、嵌入任务)、Word/报告导出、任何公司层看板查询。用共享 `exclude_redacted(rows, tier)` / `company_visible(...)` 把规则收在一处。

## 6. 上线
1. `redactions` 表 + `exclude_redacted` 辅助接入聚合/RAG(先无 UI)——搭好执行骨干。
2. F1 遮蔽(展示 + RAG 输入)。
3. F3 审阅 UI(site_manager 软删 topic/段)+ `review_state` 发布门。
4. F2 自动分类 + 确认 UI(需抽取侧分类器)。
每步独立可发;**F3 先于 F2**,使人工门在任何自动标记之前就存在。

## 7. 开放决策(待你审)
- **E1** —— 脏话词表:标准 vs 公司可配。推荐**标准 + 公司增补**。
- **E2** —— RAG/分析输入:**遮蔽** vs **丢弃** token。推荐**遮蔽**(保语义、藏词)。
- **E3** —— redaction 后可见性:site_manager/记录者+admin 仍可见(标注)vs 对 admin 以下全隐藏。推荐 `analysis` 域**仍可见带标注**。
- **E4** —— 硬清任务:开(带 N 天计时)vs 关。推荐**默认关**、可配。
- **E5** —— F2 分类跑在现有抽取 LLM 一趟(更省、一遍)vs 独立一趟。推荐**并进抽取**。
- **E6** —— 发布粒度:整日报 vs 逐 topic 审。推荐**整日报 + 逐 topic redaction**。

## 8. 风险
- **过度遮蔽/过度标记**伤信任 —— F1/F2 须可调,F2 绝不自动删(人工确认是守卫)。
- **redaction 绕过** = 隐私泄漏 —— 每条公司层读路径必须走共享辅助;加测试断言 redacted/未审内容不出现在各聚合与 RAG 中。
- **原始 transcript 保留** vs 隐私法 —— tombstone 保原文;E4 的硬清是"某客户/法域要求真擦除"时的逃生阀。
