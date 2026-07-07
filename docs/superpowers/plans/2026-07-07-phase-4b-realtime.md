# Phase 4b:实时抽取 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development,逐任务执行。
> Spec(已批,PR #20):`docs/superpowers/specs/2026-07-07-phase-4b-realtime-extraction-design.md`(含 declared_site 试点修订)。

**Goal:** 转录段落地 → session 级 Claude 抽取 → Aurora 运营表 → Dashboard 刷新可见(典型 ≤25 min);下载 claim 锁防重复。

**Architecture:** claim 锁插在 orchestrator `process_file` 的 exists-guard 与 invoke_downloader 之间(S3 条件写);抽取拆两级 S3 事件链(非 VPC `extract-session` 调 Claude 直连 → `extractions/` → in-VPC `item-writer` 写 Aurora,复用 Phase 4a 身份桥+source-key 幂等);夜间报告 ingest 收编当日 session 条目;org API `/live-items` + UI Live 徽章。

**Tech Stack:** Python 3.11/pytest;boto3 S3 条件写(IfNoneMatch);urllib3 Claude 直连(报告生成器同款);psycopg(PsycopgLayer)。

## Global Constraints(侦察锁定)

- **claim 锁**:key=`download_claims/{s3_key}.claim`;`put_object(..., IfNoneMatch='*')` 412=已被抢;陈旧接管:HEAD LastModified age>30min → 普通 put 覆盖后继续。orchestrator 现无 S3 写权限 → template 加 Put/Delete 限 `download_claims/*`;downloader 成功上传后删 claim(S3WritePolicy 无 DeleteObject → 显式加);downloader 转交 Fargate(pending_downloads/ 大文件路径)时**也删 claim**(Fargate 下载可 >30min,留着会被误接管)。
- **session 识别**(transcript_utils 无现成函数):`{device}_{YYYY-MM-DD_HH-MM-SS}` 前缀 = `filename.split('_off')[0]` 去 `.json`(无 `_off` 的整段文件 = 自身即 session);用 `extract_device_from_filename` + `extract_base_time_from_filename` 校验。
- **Claude 直连**:新 `src/claude_utils.py` 提供 `call_claude(prompt, max_tokens)->（text,err)` 与 `extract_json(raw)->dict|None`(照抄 lambda_report_generator :410-462 的 urllib3 + 三级 JSON 解析;报告生成器本体不动,重构另议);env `ANTHROPIC_API_KEY: !Ref ClaudeApiKey` + `CLAUDE_MODEL`(模板既有 param,deploy.yml 已传 secret)。
- **extraction JSON 契约**(extract→writer 的接口):`{schema_version:1, user_folder, date, session_base, source_transcripts:[filenames], extracted_at, declared_site: {stated, matched_site, confidence}|null, topics:[{topic_title, category, summary, time_range, participants, action_items:[{action,responsible,deadline,priority}], safety_flags:[{observation,risk_level,recommended_action}]}]}`——topic 形状与 daily_report 对齐,writer 直接复用 lambda_ingest 的 `_map_action_items/_map_safety/resolve_site/resolve_user`(import 复用,不复制)。declared_site 只存证于此 JSON(topics 表无 metadata 列;落库等身份系阶段③ recording_sessions)。
- **幂等**:writer 按 `source_s3_key = extraction 键` delete→insert(Phase 4a 货架);**收编**:`lambda_ingest.ingest_report` 在 delete_*_for_source(report_key) 后追加 `topics.delete_topics_for_source_prefix(conn, f"extractions/{user_folder}/{date}/")`(新 repo 函数,LIKE prefix+'%',注意 % 转义)。
- **BUG-13 前缀纪律**:extract 触发 `transcripts/` 写 `extractions/`;writer 触发 `extractions/` 零 S3 写;均与 users//audio_segments//reports/ 不重叠。
- **桶**:两个新函数的 S3_BUCKET 都是 **IngestBucketName(数据湖)**;wire-s3-events.sh 加 `fs-extract-transcripts`(prefix transcripts/,suffix .json)与 `fs-write-extractions`(prefix extractions/,suffix .json)——test 桶 CI 自动;**湖桶手工**(既定备份→合并→验证流程,变 5 条)。
- **调度**:模板把单 ScheduleEvent 改双(SweepEvent `cron(0/15 17-23,0-7 * * ? *)`≈NZ 工作时段 15min 扫 + NightlyEvent 原表达式),均挂 ShouldEnableSchedules;test 栈 schedules=false 不生效,PROD 采纳单独执行(spec 出界项)。
- **/live-items**:org_api dispatch if-ladder 加 `if route=="/live-items" and method=="GET"`(proxy 网关零改动);ACL 走 `resolve_scope=="ALL"` else `memberships.accessible_site_ids`(镜像 list_org_sites :227-238);v1 返回该 date 全部可访问站点条目,UI 端过滤;响应 `{topics:[{..._TOPIC_COLS, site_name, user_name, is_live, action_items:[...], safety_observations:[...]}]}`,`is_live = source_s3_key LIKE 'extractions/%'`。
- 铁律:单行 Edit 锚(CRLF);绝不 `git add -A`;pytest 全套零回归;sam validate(BUG-35 前缀);部署串行。

---

### Task 1: 下载 claim 锁(orchestrator + downloader + template 权限)

**Files:** Modify `src/lambda_orchestrator.py`(claim helper + process_file 插入)、`src/lambda_downloader.py`(成功/转交后删 claim)、`src/template.yaml`(两函数 IAM);Create `tests/unit/test_download_claims.py`。

**Interfaces (Produces):** `claim_download(s3_client, bucket, s3_key) -> bool`(True=抢到;412 且未陈旧=False;陈旧接管=True)与 `release_claim(s3_client, bucket, s3_key)`——放 orchestrator 模块内,downloader import 复用 release。CLAIM_PREFIX="download_claims/",STALE_MINUTES=30。

- [ ] 测试先行(stub s3 client 记录调用):抢占成功/412 未陈旧拒/412 陈旧接管/release 删对 key/process_file 未抢到不 invoke downloader(旁路断言 stats)。FAIL→实现→PASS。
- [ ] process_file:exists-guard 后 `if not claim_download(...): stats['in_progress'] += 1; return`;invoke 前不 release(claim 活到 downloader 完成)。
- [ ] downloader:upload_to_s3 成功后与转交 pending_downloads/ 后均 `release_claim`;失败路径不 release(等陈旧接管)。
- [ ] template:Orchestrator 加 Put/Delete on `${DataBucketName}/download_claims/*`;Downloader 加 Delete 同前缀。sam validate。
- [ ] 全套 pytest 零回归;提交 `feat(4b): download claim lock (conditional put + stale takeover)`。

### Task 2: claude_utils + lambda_extract_session(TDD)

**Files:** Create `src/claude_utils.py`、`src/lambda_extract_session.py`、`tests/unit/test_lambda_extract_session.py`。

**Interfaces (Produces):** extraction JSON 契约(见 Global);`session_base_from_key(key) -> (user_folder, date, session_base)|None`。

- [ ] claude_utils:照抄报告生成器模式(env 读取、urllib3、三级 JSON 解析);py_compile。
- [ ] 测试先行(monkeypatch s3 + call_claude):key 解析(带/不带 _off、非 .json 跳过)/session 收集只取同 base/normalize None 与 abs_start None 容错(Phase 4a 同款)/prompt 含全部段落文本/Claude 返回经 extract_json 落 extraction JSON 幂等覆盖/declared_site:显式声明→模糊匹配站点名(prompt 指令:谈及≠到场,无声明→null)/Claude 失败→不写 JSON、抛错(S3 事件重试)。
- [ ] 实现:S3 事件 → 收集 session 段 → normalize_transcript 逐段 → turns 拼 prompt(绝对时间,BUG-09;文本上限 60000 字符,BUG-15 site 口径)→ call_claude(max_tokens 按输入缩放,BUG-16:`min(4096+段数*350, 8000)`)→ extract_json → 写 `extractions/{user}/{date}/{session_base}.json`。
- [ ] 全 PASS;提交 `feat(4b): session extraction lambda (claude direct, declared_site pilot)`。

### Task 3: repo 增量 + lambda_item_writer(TDD)+ ingest 收编

**Files:** Modify `src/repositories/topics.py`(`delete_topics_for_source_prefix(conn, prefix)->int`;`list_topics_for_date(conn, site_ids, report_date)->list[dict]` 带 children+site_name+user_name 两段查询拼装);Create `src/lambda_item_writer.py`、`tests/unit/test_lambda_item_writer.py`;Modify `src/lambda_ingest.py`(收编一行)+ `tests/unit/test_lambda_ingest.py`(收编断言)。

- [ ] repo:LIKE 前缀删除(参数化 prefix+'%',文档注明 % 转义);list 函数(topics WHERE site_id=ANY AND report_date=%s → children WHERE topic_id=ANY 分组回填;JOIN sites.name、users first+last)。
- [ ] 测试先行(FakeConn/monkeypatch,repos+get_connection+s3):extraction 读→身份桥(import lambda_ingest.resolve_*;site None 走 primary_site 链;双 miss skip 零写)/source-key 先删后插/topic children 映射复用 _map_*/收编:ingest_report 断言 delete_topics_for_source_prefix 以 `extractions/{user}/{date}/` 调用。
- [ ] 实现 writer(commit-per-extraction,`with get_connection()`)+ ingest 一行收编。
- [ ] 全 PASS 零回归;提交 `feat(4b): item writer + nightly-report supersession + repo deltas`。

### Task 4: org API /live-items + 测试

**Files:** Modify `src/lambda_org_api.py`、`tests/unit/test_lambda_org_api.py`。

- [ ] 测试先行:date 必填校验/admin 全站 vs worker accessible_site_ids/is_live 派生(source_s3_key LIKE extractions/%)/children 嵌套形状。
- [ ] dispatch 加分支 + `list_live_items(conn, caller, event)`(镜像 list_org_sites ACL 分支 → topics.list_topics_for_date → ok({"topics": rows}))。
- [ ] 全 PASS;提交 `feat(4b): GET /api/org/live-items`。

### Task 5: 基础设施(template 双函数/双调度 + wire 脚本)

**Files:** Modify `src/template.yaml`、`scripts/wire-s3-events.sh`。

- [ ] ExtractSessionFunction:非 VPC;Handler lambda_extract_session.lambda_handler;Timeout 180/Mem 256;env ANTHROPIC_API_KEY/CLAUDE_MODEL(照 ReportGenerator 440-441)+ S3_BUCKET: !Ref IngestBucketName;IAM:Get `${IngestBucketName}/transcripts/*`、Put `${IngestBucketName}/extractions/*`、ListBucket 限 prefix transcripts/*;无 Events。
- [ ] ItemWriterFunction:mirror IngestFunction(HasDb、PsycopgLayer、VpcConfig、PG env、CONFIG_KEY);Timeout 120/Mem 512;IAM:VPCAccess + Get `extractions/*` + `config/user_mapping.json` + ListBucket prefix extractions/*;无 Events。
- [ ] Orchestrator 调度改双 Events(SweepEvent + NightlyEvent,均 ShouldEnableSchedules 门)。
- [ ] wire-s3-events.sh 加两条(fn_exists 门、全声明合并保真——现有 jq 模式照抄,**注释不得入 jq 单引号串**)。
- [ ] `export AWS_CLI_FILE_ENCODING=UTF-8 PYTHONUTF8=1; sam validate --lint`(容忍既有 W2531)+ `bash -n`;提交 `feat(4b): infra — extract/writer functions, dual schedule, s3 wiring`。

### Task 6: Fable 终审 → PR → 部署 → 湖桶接线 → 端到端(控制器)

- [ ] 整分支 diff → Fable 5 终审(镜头:claim 竞态与 Fargate 生命周期、extraction 契约两端一致、收编事务性、/live-items ACL、IAM 最小化、BUG-13/15/16、wire 合并保真)。修→复审。
- [ ] PR → 用户合并 → 部署 completed/success → 湖桶手工加 2 条通知(备份→合并→验证,5 条齐)+ 两函数 add-permission(湖桶 ARN)。
- [ ] **端到端计时**:重触发一个真实转录段(`aws s3 cp key key --metadata-directive REPLACE`)→ extraction JSON 出现 → Aurora topics 行(Data API 查)→ /live-items 返回该条 → force 重生成该日报告 → session 条目被收编(live 行消失,报告行留存)。重复触发同一转录段 → extraction 幂等覆盖、topics 不重复。
- [ ] 账本 + memory;UI 批次(T7)另 PR。

### Task 7: UI 批次(fieldsight-ui,dev 分支)

**Files:** Modify `scripts/api/org.js`(getLiveItems)、`scripts/api/tasks-aggregator.js`(live 行合并)、`scripts/pages/timeline.js`(Live 区)、`scripts/composites/*`(Live Badge)、`styles/tokens.css` + `scripts/fs-globals.js`(--source-live,双镜像)、`app-shell-preview.html`(buster)。

- [ ] org.js:`getLiveItems({date})` 镜像 getObservations(orgLive 门 + mock 空);挂 window.FS.api.org。
- [ ] tasks-aggregator:getActionsResolvedRange 末尾 try/catch 合并(compliance :522-543 韧性模式照抄——org 失败绝不拖垮主行);live 行映射到既有 row 形状,`id: 'live_'+topic.id+'_'+idx`、`source:'live'`;Tasks 行渲染 Live Badge(safety-flag-row :83-87 Manual 模式照抄,tone info)。
- [ ] timeline.js:选中日 fetch live items,按 user_name 过滤;报告 _notFound 时单独渲染"Live(报告未生成)"区,有报告时 Live 区置于 topics 上方;TopicCard 复用。
- [ ] node --check 全部改动文件;Fable 终审(镜头:聚合器铁律——live 合并不得读 FS.siteContext,site 过滤仍走 opts.site;envelope 守卫;mock 路径不网络)→ 修 → PR。

## 自审

- spec 四部件全落位:claim(T1)、抽取(T2)、写入+收编(T3)、UI(T7);/live-items(T4)、infra(T5)、e2e 计时(T6)。
- 接口一致:extraction JSON 契约贯穿 T2/T3;claim helper 贯穿 T1 双 lambda;list_topics_for_date 贯穿 T3/T4;is_live 贯穿 T4/T7。
- 预判:orchestrator 无写权限(T1 template)、Fargate 长下载与 30min 接管冲突(T1 转交即删)、declared_site 无落库列(存证于 JSON,阶段③再落)、wire 脚本注释坑(T5 明示)、NZ DST(cron 并集 17-07 UTC)。
