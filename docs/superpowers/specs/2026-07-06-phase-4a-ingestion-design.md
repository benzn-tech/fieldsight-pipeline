# Phase 4a:抽取入库管线(Ingestion)设计 spec

> 入场门已过(2026-07-06):切片样例双日验证,五参数 + 脏数据规则定稿(见 artifact / phase4-sample)。
> **范围判断(需用户确认)**:roadmap 的 Phase 4 同时包含"入库管线"与"Dashboard 分钟级时效"。本 spec 把它拆成 **4a(入库,本期)** 和 **4b(实时抽取+Dashboard 时效,下期)**——理由:你的三个驱动需求(#1 转录持久化、#5 RAG 搜索、#4 完成识别)全部只依赖 4a;4a 还能**回填整个历史数据湖**,让 Phase 5 的 Ask RAG 立刻有全量语料。实时性单独做,不拖累地基。

## 目标(4a)

1. **新数据自动入库**:daily_report.json 落地(S3 事件)→ topics 入 org 运营表 + 切块 + embed 入 `report_chunks`;对应日转录 → transcript 窗口块同样入库。
2. **历史回填**:一条命令把并桶后的**全量数据湖**(反正 61 报告 + 382 转录,量小)按同一管线灌入。
3. 幂等:按 `source_s3_key` upsert,报告重生成不产生重复。

## 定稿的切片参数(入场门)

- topic 块:一 topic 一块,>4500 字符切分(重复标题行 overlap);transcript 窗口:2600 字符目标、turn 边界、重叠 2 turns、±2min 归属缓冲;无归属窗口保留;time_range 空/塌缩 → 不参与归属;metadata 含 participants;embedding 一段式 Titan V2 1024。

## 架构

```
S3 事件(reports/…/daily_report.json 创建)          回填命令(手动 invoke)
        │                                              │
        └────────────► fieldsight-ingest Lambda ◄──────┘
                       (in-VPC:psycopg → Aurora)
                       1. 读报告 JSON + 该(user,date)全部转录(S3)
                       2. 身份桥:site 名/slug → org sites.id;topic 先 upsert
                          org topics 表拿 uuid(§8.8 topic_id 漂移:按
                          source_s3_key+topic_seq 定位而非裸序号)
                       3. 切片(入场门定稿参数,复用 transcript_utils)
                       4. Bedrock Titan V2 embed(批量)
                       5. chunks.insert_chunk upsert(按 source_s3_key+
                          chunk_type+seq 删旧插新)
```

**关键基础设施决定(预判)**:
- ingest Lambda **必须同时够到 Aurora(VPC 内)和 S3/Bedrock(VPC 外)** → 沿用 BUG-36 的既定方案:in-VPC + **bedrock-runtime VPC interface endpoint**(单 AZ,同 cognito-idp 端点先例,≈$8/月)+ 既有 S3 gateway endpoint。
- S3 事件绑定 `reports/` 前缀 + `daily_report.json` 后缀:**手动桶通知配置**(BUG-33,SAM 管不了外部桶)——沿用并桶时的"备份→改→验证"流程,加第 3 条 LambdaFunctionConfiguration,**不碰既有 vad/transcribe 两条**。
- 触发选择:报告落地才触发(而非每个转录文件触发)——转录是分段陆续到达的,按报告触发天然聚合一天一次,无需防抖;实时性(转录完成即抽取)留给 4b。

## 不在 4a(留 4b/后续)

- 每场转录完成即时 Claude 抽取、Dashboard 分钟级时效、夜间日报重构为汇总、WebSocket/轮询——4b。
- Ask RAG 检索层(retrieve→ACL SQL→引用)——Phase 5(4a 完成后语料即就绪)。
- action_items 等其它运营表的抽取入库(v1 只入 topics + chunks;action_items 结构入库跟 #4 完成识别一起设计)。

## 验证

- 单测:切片器纯函数(参数边界/脏 time_range/overlap);身份桥映射;幂等 upsert。
- 集成:回填跑全量湖 → Data API 抽查 chunk 计数/元数据;重跑回填 → 计数不变(幂等)。
- 端到端:重新生成某天报告(force)→ S3 事件触发 → chunks 更新;`search_chunks` 用一个真实问题的 embedding 试召回(验证 HNSW + ACL JOIN 路径通)。

## 成本/时效

Titan V2 embed:全量湖 ~600 块 × ~700 tokens ≈ 42 万 tokens,约 $0.008——忽略不计。bedrock endpoint 单 AZ ≈ $8/月(唯一新增固定成本)。
