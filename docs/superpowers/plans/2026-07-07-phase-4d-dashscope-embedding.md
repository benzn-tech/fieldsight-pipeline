# Phase 4d:DashScope 嵌入(A 回填 + B 增量)Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development。
> 背景:Bedrock 账号级封锁,改用阿里云 DashScope 国际站 text-embedding-v4 @ dim 1024(schema 零改)。用户已批 A+B 推荐路线。

**Goal:** 报告→切块→DashScope 嵌入→report_chunks;RAG 语料就位。A(回填):268 向量已本地算好,上 S3 边表跑回填。B(增量):新报告自动嵌入。

**Architecture(核心):** 嵌入需公网(DashScope),插库需 Aurora(VPC 内)——拆两半,复用 4b 模式:
- **非 VPC `embed-report`**:reports/ 事件 → 切块(chunking.py,报告+转录)→ DashScope 逐块嵌入 → 写 `embeddings/{date}/{user}/vectors.json`(内容 hash → 1024 向量的 map)。
- **in-VPC `ingest`(改造)**:触发改到 `embeddings/…/vectors.json` → 读报告 + 边表 → `embed_text` 从边表按 `sha256(chunk_text)` 查向量(S3 gateway endpoint,无外网)→ 现有身份桥 + source-key 幂等 + insert_chunk。**Bedrock 调用移除**。
- A 回填:本地由 268 向量(recordId 即 sha256)生成 17 份 per-report 边表上 S3 → invoke `{"backfill":true}`(ingest 列 reports/、读同名边表)。

**Tech Stack:** Python 3.11/pytest;urllib3(DashScope,报告生成器同款 HTTP 风格);psycopg;boto3 S3。

## Global Constraints

- **边表契约**:`embeddings/{date}/{user_folder}/vectors.json` = `{ sha256(chunk_text): [1024 floats] }`;两端(embed 写 / ingest 读)必须一致 hash 算法(hashlib.sha256(text.encode('utf-8')).hexdigest(),对**完整 chunk_text 不截断**——注意:旧 Titan 路径截 8000 字符,DashScope 侧 v4 限 ~8k token,embed 侧对 inputText 截 8000 字符**并且 hash 也用截断后的同一文本**,确保两端一致)。缺 hash → ingest raise(embedding NOT NULL,不可插空)。
- **DashScope**:`dashscope_utils.embed(texts:list[str], dim=1024) -> list[list[float]]`;端点 `https://dashscope-intl.aliyuncs.com/compatible-mode/v1/embeddings`,model `text-embedding-v4`,批 ≤10,429/5xx 退避重试;env `DASHSCOPE_API_KEY`(GitHub secret 已建)、`DASHSCOPE_BASE_URL`、`DASHSCOPE_EMBED_MODEL=text-embedding-v4`、`DASHSCOPE_EMBED_DIM=1024`。embed-report **非 VPC**(公网直连,与报告生成器同——它也非 VPC)。
- **触发迁移**:`fs-ingest-report` 从 `reports/` 前缀迁到 `embeddings/` 前缀(后缀 vectors.json);新增 `fs-embed-report` 于 `reports/` 前缀 daily_report.json 后缀 → embed-report。BUG-13:embed 写 `embeddings/`(不重叠 reports/);ingest 零 S3 写。**保留** vad/transcribe/extract/write 四条不动。
- **ingest handler**:事件 key 现为 `embeddings/{date}/{user}/vectors.json` → 解析出 (date, user_folder, report_key=`reports/{date}/{user_folder}/daily_report.json`);backfill 路径(列 reports/)不变,ingest_report 内按 report_key 推导边表路径读取。
- **Bedrock 退场**:移除 `bedrock()`/`BEDROCK_MODEL_ID`/invoke;db-template 的 bedrock-runtime endpoint **留着不删**(闲置无害,单独 cost cleanup),本计划不碰 infra 的 endpoint。
- 铁律:单行 Edit 锚(CRLF);绝不 `git add -A`;pytest 零回归(基线 133);sam validate(BUG-35 前缀);串行部署。**数据主权**:chunk 文本发往阿里云——已知并接受(用户决定)。

---

### Task 1: ingest 改边表嵌入(in-VPC,移除 Bedrock,TDD)

**Files:** Modify `src/lambda_ingest.py`;Modify `tests/unit/test_lambda_ingest.py`。

- Produces:`_sidecar_key(report_key) -> str`(reports/{d}/{u}/daily_report.json → embeddings/{d}/{u}/vectors.json);`_load_vectors(bucket, sidecar_key) -> dict`;`embed_from_sidecar(text, vectors) -> str`(sha256(text[:8000]) 查 map → '[...]';缺失 raise KeyError 明确信息)。
- [ ] 测试先行:sidecar key 派生;embed_from_sidecar 命中/缺失 raise;ingest_report 读边表并用查表向量(monkeypatch _load_vectors 返回 canned map,断言 insert_chunk 收到 '[...]' 且无 bedrock 调用);handler 解析 embeddings/ 事件 key → 正确 (date,user,report_key);backfill 路径仍列 reports/ 并对每份读边表。
- [ ] 实现:删 bedrock 客户端/常量/invoke;ingest_report 开头 `vectors = _load_vectors(S3_BUCKET, _sidecar_key(report_key))`;两处 `embed_text(c["chunk_text"])` → `embed_from_sidecar(c["chunk_text"], vectors)`;lambda_handler 事件分支解析 embeddings/ key。
- [ ] 全 PASS 零回归;提交 `feat(4d): ingest embeds from S3 vector sidecar (retire bedrock)`。

### Task 2: dashscope_utils + lambda_embed_report(非 VPC,TDD)

**Files:** Create `src/dashscope_utils.py`、`src/lambda_embed_report.py`、`tests/unit/test_lambda_embed_report.py`。

- Produces:`dashscope_utils.embed(texts, dim=1024) -> list[list[float]]`(批 ≤10,退避);`lambda_embed_report.lambda_handler`。
- [ ] 测试先行(monkeypatch dashscope_utils.embed + FakeS3):reports/ 事件 → 读报告 + 转录(复用 lambda_ingest 的 s3 读 + `_load_turns` 或等价)→ chunk_report + chunk_transcripts → 对每 chunk_text[:8000] 求 sha256 → embed → 写 `embeddings/{date}/{user}/vectors.json` = {hash: vector};幂等覆盖;dim 1024 校验;批处理 ≤10 分组;空报告不写。
- [ ] 实现:dashscope_utils(urllib3 POST,Bearer,input 列表,dimensions,encoding_format float,退避);embed_report handler。
- [ ] 全 PASS 零回归;提交 `feat(4d): dashscope embed-report lambda (non-VPC) + vector sidecar`。

### Task 3: 基础设施(template + deploy.yml + wire 脚本)

**Files:** Modify `src/template.yaml`、`.github/workflows/deploy.yml`、`scripts/wire-s3-events.sh`。

- [ ] template:新 `EmbedReportFunction`(**非 VPC**;FunctionName ${P}-embed-report;Handler lambda_embed_report.lambda_handler;Timeout 300/Mem 512;env S3_BUCKET: !Ref IngestBucketName + CONFIG_KEY + DASHSCOPE_API_KEY: !Ref DashScopeApiKey + DASHSCOPE_BASE_URL/EMBED_MODEL/EMBED_DIM;IAM:Get reports/*+transcripts/*+config、Put embeddings/*、ListBucket prefix transcripts/*);新增 `DashScopeApiKey` param(NoEcho,Default '')。IngestFunction 移除 BEDROCK_MODEL_ID env(留 PG/S3/CONFIG)。无 Events(手动接线)。
- [ ] deploy.yml:parameter-overrides 加 `DashScopeApiKey=$DASHSCOPE_API_KEY`;env 加 `DASHSCOPE_API_KEY: ${{ secrets.DASHSCOPE_API_KEY }}`。
- [ ] wire-s3-events.sh:`fs-ingest-report` 前缀 reports/→embeddings/、后缀 daily_report.json→vectors.json;新增 `fs-embed-report`(reports/ + daily_report.json → ${P}-embed-report)。注释在 jq 串外、无撇号。
- [ ] `sam validate --lint`(容忍 W2531)+ `bash -n`;提交 `feat(4d): infra — embed-report fn, dashscope secret, trigger migration`。

### Task 4: Fable 终审 → PR → 部署 → 回填 + RAG 冒烟(控制器)

- [ ] 整分支 diff → Fable 5 终审(镜头:两端 hash 一致性含截断、缺失向量 raise、触发迁移无 reports/ 双触发或断链、DashScope 退避/批限、非 VPC 无 VpcConfig、IAM 最小化、Bedrock 残留、wire 合并保真)。修→复审。
- [ ] PR → 用户合并 → 部署 success。
- [ ] **湖桶接线迁移**:`fs-ingest-report` 改到 embeddings/vectors.json + 加 `fs-embed-report` 于 reports/(备份→合并→验证,原 vad/transcribe/extract/write 不动)+ embed-report add-permission。
- [ ] **A 回填**:本地脚本由 `embeddings-dashscope-v4-1024.jsonl`(recordId=sha256)+ 重跑 chunker 生成 17 份 per-report `embeddings/{date}/{user}/vectors.json` 上 S3 → invoke `{"backfill":true}` → 核对 `SELECT chunk_type,count(*) FROM report_chunks GROUP BY 1`;重跑幂等计数不变。
- [ ] **RAG 冒烟**:对真实问题(如 "door inspection Ellesmere")DashScope 嵌入查询向量 → `search_chunks`(ACL 全站)→ 断言 top-K 命中 2026-02-09 相关块。**B 端到端**:force 重生成某日报告 → reports/ 事件 → embed-report → embeddings/ → ingest → chunks 刷新。
- [ ] 账本 + memory。

## 自审
- A(T1 边表读 + T4 回填)+ B(T2 embed-report + T3 触发迁移)全覆盖;共享 T1 的边表嵌入改造。
- 契约一致:sha256(text[:8000]) 边表 key 贯穿 T1/T2/T4;embeddings/{date}/{user}/vectors.json 贯穿全任务。
- 预判:两端截断一致(否则 hash 不匹配→回填全 miss)、缺失向量 raise 而非插空(NOT NULL)、触发迁移不留 reports/ 双触发、非 VPC 直连(VPC 内够不到 DashScope)、Bedrock 残留清理。
