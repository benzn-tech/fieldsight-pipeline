# Phase 4a:抽取入库管线 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development,逐任务执行,步骤 `- [ ]`。
> Spec(已批):`docs/superpowers/specs/2026-07-06-phase-4a-ingestion-design.md`。切片参数入场门定稿(2600 字符窗口/2 turns overlap/±2min 归属/脏 time_range 规则/participants 入 metadata/Titan V2 一段式)。

**Goal:** daily_report.json 落地(S3 事件)或回填命令 → topics 入 org 运营表 + 切块 + Titan embed → `report_chunks`;幂等;全量历史湖可一键回填。

**Architecture:** 新 `src/chunking.py`(纯函数切片器,样品脚本移植)+ 新 `src/lambda_ingest.py`(in-VPC,mirror OrgApiFunction 的 VPC/PG 模式)+ repo 增量(范围删除 + `::vector` 文本绑定,**不加新 layer**)+ `infra/db-template.yaml` 加 bedrock-runtime endpoint + `wire-s3-events.sh` 加第 3 条触发(deploy.yml 会自动 --apply)。幂等单元 = **(site_id, user_id, report_date)**:一次 ingest 先删该范围的 topics(children 级联)与 chunks,再全量重插。

**Tech Stack:** Python 3.11 / psycopg(既有 PsycopgLayer,无 pgvector 包)/ boto3 bedrock-runtime(runtime 自带)/ pytest(TDD)。

## Global Constraints(证据锁定的决定)

- **幂等**:范围先删后插;`topics` 删除按 `(site_id, report_date, user_id)`(action_items/safety_observations/topic_photos ON DELETE CASCADE 自动清);`report_chunks` 同键自删(topic FK 是 SET NULL,必须显式删)。
- **vector 绑定**:`insert_chunk` 的 embedding 占位改 `%s::vector`;ingest 以 `'[f1,f2,…]'` 字符串传入——**绕开 pgvector+numpy 打包问题,不新建 layer**(psycopg-layer/requirements.txt 自己注明 "Phase 3/4 revisit";本方案即答案)。既有 list 绑定(集成测试,adapter 在场)与 `::vector` 兼容。
- **身份桥回退链**:`report['site']`(显示名)→ `get_company_site_by_name` → miss 则 user_mapping.json 由 user 的 primary_site slug → `sites[slug].name` → 再 match → 仍 miss → **记日志跳过该报告,绝不自创站点**(真实案例:2026-03-20 的 site='BD Opportunity Brainstorm' 非真实站点)。user_id:display name 匹配 `list_company_users` 的 first+last,miss → NULL(列可空)。company 固定 `get_company_by_name('FieldSight')`。
- **Bedrock**:`amazon.titan-embed-text-v2:0`,boto3 `bedrock-runtime`(**net-new**,报告生成器走的是 Anthropic 直连 HTTP,不可参考);in-VPC 必须有 bedrock-runtime interface endpoint(否则 BUG-36 黑洞)。IAM 只授 `bedrock:InvokeModel` 于该模型 ARN。
- **S3 事件**:SAM 管不了外部桶(BUG-33)→ 加进 `scripts/wire-s3-events.sh`(deploy.yml post-deploy 自动 `--apply`);前缀 `reports/`、后缀 `daily_report.json`(**绝不重叠** vad/transcribe 的 users//audio_segments/ 前缀,BUG-13)。
- **db 栈重部署**(bedrock endpoint):手动 CLI,**必须重供既有全部参数**(EndpointSubnetIds=subnet-082dd4480f7e20014 单 AZ 等,Phase 3 命令原文在 2026-07-04-phase-3-org-api.md:125-142);先 describe-stacks 抄现值再 deploy。
- **切片器纪律**:时间归一化只用 `transcript_utils.normalize_transcript`(BUG-09);正则遵 BUG-01;deploy 并发铁律(等 UPDATE_COMPLETE);pytest 全套零回归;绝不 `git add -A`。
- Lambda 规格:Timeout 300 / Memory 512(embed 循环 + 大会议日);`Condition: HasDb`;`CodeUri: src/` 自动带上 chunking.py/repositories/transcript_utils。

---

### Task 1: src/chunking.py 纯切片器(TDD)

**Files:** Create `src/chunking.py`;Create `tests/unit/test_chunking.py`。

**Interfaces (Produces):**
- `chunk_report(report: dict) -> list[dict]` — topic 块;每项 `{chunk_type:'topic', chunk_text, topic_seq, metadata}`(metadata 含 user_name/site/report_date/topic_seq/time_range/category/participants/part?);>4500 字符切分,重复标题行 overlap。
- `chunk_transcripts(report: dict, normalized_turns: list[dict]) -> list[dict]` — transcript 窗口;输入 turns 为 `normalize_transcript` 输出的 speaker_turns 展平(每项含 abs_start/abs_start_str/abs_end_str/speaker/text/src_filename);目标 2600 字符、turn 边界、重叠 2 turns、±120s 归属缓冲;无归属窗口保留(metadata.topic_seq=None + note);每项 `{chunk_type:'transcript_window', chunk_text, topic_seq|None, metadata}`(metadata 含 window_index/turns/window_span/source_files/participants)。
- `parse_time_range(tr: str) -> tuple[int,int] | None` — 空串/单值(塌缩)→ None 或点区间(单值);导出供测试。
- 全部纯函数,无 IO;逻辑从已验证的样品脚本(scratchpad/phase4-sample/chunker.py)移植,参数为模块常量 `TARGET_CHARS=2600, OVERLAP_TURNS=2, TOPIC_SPLIT_CHARS=4500, ASSIGN_BUFFER_SEC=120`。

- [ ] Step 1(测试先行):`tests/unit/test_chunking.py`——构造小型合成 report/turns:
  ```python
  def test_topic_chunk_basic():            # 标题/摘要/决定/行动/安全全拼接,metadata 齐
  def test_topic_chunk_oversize_split():   # >4500 → 2 块,标题行重复,part 标注
  def test_parse_time_range_dirty():       # '' → None;'12:18' 单值 → (v,v);'10:30 – 10:32' 正常
  def test_window_respects_turn_boundary():# 不切断 turn;达标即 flush
  def test_window_overlap_two_turns():     # 相邻窗口共享最后 2 turns
  def test_unassigned_window_kept():       # 时间段外 turns → topic_seq None + note
  def test_assign_buffer():                # 边界 ±120s 内归属
  def test_participants_in_metadata():
  ```
  每个测试体完整(合成数据内联)。
- [ ] Step 2:`python -m pytest tests/unit/test_chunking.py -v` → FAIL(模块不存在)。
- [ ] Step 3:实现 chunking.py(移植样品逻辑,去 IO 化:文件读取/glob 移到调用方)。
- [ ] Step 4:pytest 该文件全 PASS;全套 `python -m pytest tests/unit -v` 零回归。
- [ ] Step 5:提交 `feat(phase4a): pure chunker module (gate-approved params) + tests`。

### Task 2: repository 增量(范围删除 + vector 文本绑定)

**Files:** Modify `src/repositories/chunks.py`、`src/repositories/topics.py`;Modify `tests/unit/`(如有对应 handler 级测试放 T3;本任务 py_compile + 现有集成测试兼容性说明)。

**Interfaces (Produces):**
- `chunks.delete_chunks_for_scope(conn, site_id, report_date, user_id) -> int`(返回删除数;user_id 为 None 时条件 `user_id IS NULL`)。
- `chunks.insert_chunk(...)`:SQL 的 embedding 占位改 `%s::vector`(其余签名不变——**既有调用方零改动**)。
- `topics.delete_topics_for_scope(conn, site_id, report_date, user_id) -> int`(children 级联)。
- `topics.upsert_topic` 不改名不改签名(插入语义;幂等由 scope-delete 保证);函数 docstring 的 "dedup key TBD" 注释更新为指向 scope-delete 方案。

- [ ] Step 1:实现两个 delete + `::vector` 改造 + docstring 更新(风格照抄各文件现状)。
- [ ] Step 2:`python -m py_compile` 两文件;跑全套单测零回归;在报告中注明:集成测试(list 绑定)与 `::vector` 兼容的理由(adapter 序列化后再 cast 为 no-op)。
- [ ] Step 3:提交 `feat(phase4a): scope-delete repos + ::vector text binding (no new layer)`。

### Task 3: src/lambda_ingest.py(TDD)

**Files:** Create `src/lambda_ingest.py`;Create `tests/unit/test_lambda_ingest.py`。

**Interfaces:**
- Consumes:T1 `chunk_report/chunk_transcripts`;T2 delete/insert;`transcript_utils.normalize_transcript`;repositories sites/users/companies;boto3 s3 + bedrock-runtime。
- Produces(调用契约):
  - S3 事件:`{Records:[{s3:{object:{key:'reports/2026-03-02/Jarley_Trainor/daily_report.json'}}}]}` → 逐 Record ingest 该 (user,date)。
  - 手动:`{"date":"2026-03-02","user":"Jarley_Trainor"}` 单份;`{"backfill":true}` 全量(list `reports/` 前缀,逐 (date,user) 处理,**单份失败记日志继续**,返回 `{processed, skipped:[{key,reason}], failed:[...]}`)。
  - 环境变量:`S3_BUCKET, CONFIG_KEY=config/user_mapping.json, BEDROCK_MODEL_ID=amazon.titan-embed-text-v2:0, PG*`。

- [ ] Step 1(测试先行):`tests/unit/test_lambda_ingest.py`(monkeypatch boto3 clients、repositories、chunking;FakeConn 模式照 test_lambda_org_api):
  ```python
  def test_s3_event_key_parsing():            # key → (date, user_folder)
  def test_site_bridge_by_report_name():      # report['site'] 直接命中
  def test_site_bridge_fallback_slug():       # 直配 miss → user primary_site slug → name 命中
  def test_site_bridge_miss_skips():          # 双 miss → skipped 带 reason,零写库(2026-03-20 案例)
  def test_idempotent_scope_delete_before_insert():  # 断言 delete_*_for_scope 先于 insert 调用
  def test_topic_uuid_flows_to_chunks():      # upsert_topic 返回的 id 写进对应块的 topic_id
  def test_embedding_string_format():         # bedrock 返回 1024 floats → '[...]' 字符串传 insert_chunk
  def test_backfill_isolates_failures():      # 一份炸不影响后续,failed 列表记录
  def test_user_bridge_null_on_miss():
  ```
- [ ] Step 2:pytest → FAIL。
- [ ] Step 3:实现:S3 读(report + `transcripts/{user}/{date}/` 全列)、user_mapping 读(桥)、`with get_connection() as conn` 单事务(单份报告内:scope-delete → topics 逐个 insert 拿 uuid → report 块挂 topic uuid → 转录归一化+切窗 → 逐块 bedrock embed(`invoke_model`,body `{"inputText": chunk_text}`,响应 `embedding` 1024 floats → 字符串化)→ insert_chunk)。metadata 补 `source_files`;chunk 的 `source_s3_key`:topic 块=报告 key,窗口块=报告 key(检索引用回报告;窗口的原始转录文件列表在 metadata.source_files)。日志量级:每份报告一行摘要(topics/chunks 数)。
- [ ] Step 4:pytest 全 PASS;全套零回归。
- [ ] Step 5:提交 `feat(phase4a): ingest lambda (event + backfill, idempotent, bedrock titan)`。

### Task 4: 基础设施(SAM 函数 + S3 事件接线 + bedrock endpoint)

**Files:** Modify `src/template.yaml`;Modify `scripts/wire-s3-events.sh`;Modify `infra/db-template.yaml`。

- [ ] Step 1:template.yaml 加 `IngestFunction`(**逐项 mirror OrgApiFunction 619-668 的 VPC/PG 模式**):`Condition: HasDb`;`FunctionName: ${P}-ingest`;`Handler: lambda_ingest.lambda_handler`;`Timeout: 300`;`MemorySize: 512`;`Layers: [!Ref PsycopgLayer]`;VpcConfig 同款;env `PG* 同款 + S3_BUCKET: !Ref DataBucketName + CONFIG_KEY: config/user_mapping.json + BEDROCK_MODEL_ID: amazon.titan-embed-text-v2:0`;Policies:VPCAccessPolicy + s3:GetObject/ListBucket(`arn:aws:s3:::${DataBucketName}` 与 `/reports/*`、`/transcripts/*`、`/config/user_mapping.json`)+ `bedrock:InvokeModel` 于 `arn:aws:bedrock:${AWS::Region}::foundation-model/amazon.titan-embed-text-v2:0`。无 Events(S3 手动接线)。
- [ ] Step 2:`scripts/wire-s3-events.sh` 加第 3 条配置(先读该脚本现有模式——它管理 vad/transcribe 两条 + add-permission):`reports/` 前缀 + `daily_report.json` 后缀 → `${P}-ingest`;**保留既有两条**(脚本应为全量声明式,核实其合并行为——若是整体 PUT 必须包含全部三条)。
- [ ] Step 3:`infra/db-template.yaml` 加 `BedrockRuntimeEndpoint`(copy CognitoIdpEndpoint 114-123,ServiceName 改 `com.amazonaws.${AWS::Region}.bedrock-runtime`,复用 EndpointSubnetIds + EndpointSG)。
- [ ] Step 4:`sam validate --lint`(BUG-35 前缀 env);提交 `feat(phase4a): ingest function + s3 wiring + bedrock vpc endpoint`。

### Task 5: Fable 终审 → PR → 部署链 → 回填 + 检索冒烟(控制器亲自执行)

- [ ] Step 1:整分支 diff → **Fable 5** 终审(镜头:幂等范围删除的边界(user_id NULL)、身份桥跳过规则、::vector 兼容性、IAM 最小化、BUG-13 前缀不重叠、wire 脚本不覆盖既有两条、backfill 失败隔离)。修 → 复审。
- [ ] Step 2:**db 栈先行**(endpoint 必须先于 lambda 首次调用 bedrock):describe-stacks 抄 fieldsight-db-test 现参数 → `aws cloudformation deploy infra/db-template.yaml` 重供全部参数 + 新资源;验证 endpoint Available。
- [ ] Step 3:push + PR(base develop)→ **用户合并** → 等 deploy completed/success(并发铁律)→ 验证 wire-s3-events 已含 ingest 条目(get-bucket-notification-configuration)。
- [ ] Step 4:**回填**:invoke `{"backfill":true}` → 返回 processed/skipped/failed;Data API 核对:`SELECT chunk_type, count(*) FROM report_chunks GROUP BY 1`、`SELECT count(*) FROM topics`;抽查一块的 metadata 完整性;**重跑回填 → 计数不变**(幂等实证)。skipped 里应见 2026-03-20('BD Opportunity Brainstorm' 桥 miss)——符合设计。
- [ ] Step 5:**检索冒烟**:invoke 一个临时查询(或经 psql/Data API):对 "door inspection Ellesmere" 之类真实问题,用 bedrock embed 后 `search_chunks`(ACL 传全部 site ids)→ 断言 top-K 含 2026-02-09 相关块。**端到端事件验证**:force 重生成某天报告(report-generator invoke)→ S3 事件触发 ingest → 该天 chunks 刷新(created_at 更新)。
- [ ] Step 6:账本 + memory;4b(实时抽取 + Dashboard 时效)另起。

---

## 自审

- Spec 三目标全覆盖:事件入库(T3/T4)、回填(T3/T5)、幂等(T2/T3/T5 实证)。
- 侦察锁定的三决定落位:scope-delete(T2)、::vector 文本绑定(T2,不新建 layer)、身份桥回退链+跳过(T3,含 3-20 真实案例测试)。
- 预判落位:BUG-36(endpoint 先行,T5 Step 2)、BUG-33/13(wire 脚本,T4)、BUG-35/27-31(命令均带前缀)、并发铁律(T5 Step 3)、db 栈全参数重供(T5 Step 2)。
- 接口一致性:chunk_report/chunk_transcripts/parse_time_range 贯穿 T1/T3;delete_*_for_scope 贯穿 T2/T3;metadata 字段=入场门定稿。
- TDD:T1/T3 测试先行;T2 兼容性论证;T5 幂等与检索都有实证步骤。
