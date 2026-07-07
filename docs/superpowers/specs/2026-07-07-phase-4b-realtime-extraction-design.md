# Phase 4b:实时抽取(会后 ≤30 分钟可见)设计 spec

> 用户目标(2026-07-07 原话):SM 在工地开完 1 小时会,回办公室最多半小时,刷新页面就能在 ToDoList/Task/Timeline 看到会议要点。
> **不依赖 Bedrock**:抽取走 Claude 直连 API(报告生成器同款),可立即开工;向量化(chunks embed)在 Bedrock 放行后由同管线补上。

## 现状与差距

- 下载编排:orchestrator 每晚 NZDT 20:00 cron 一次,轮询 RealPTT → 分派 downloader 落 S3 `users/`;去重只有 `check_s3_exists`(防已完成),**无进行中 claim**——RealPTT 慢速下载 + 重复触发 = 重复下载(用户实际痛点)。
- 落 S3 后的链路已经是事件驱动(users/ → VAD → audio_segments/ → Transcribe → transcripts/),逐文件、分钟级——**中段不用改**。
- transcripts/ 落地后无人消费(要等夜间报告)——差的就是"转录完成 → 运营表"这一跳。

## 设计(四个部件)

### 1. 下载频率 + claim 锁(防重复下载)

- orchestrator 调度改**工作时段高频扫**:`rate(15 minutes)`,限 NZDT 06:00–19:00(cron 表达);夜间保留原 20:00 全量扫兜底。
- **claim 锁**:开始下载前对 `download_claims/{目标s3_key}.claim` 做 **S3 条件写(If-None-Match:*)**——原子抢占,零新增基础设施。抢到才下载,完成后删 claim;**陈旧接管**:claim 存在但 age > 30 min(对照 LastModified)视为宕机残留,允许接管重下。`check_s3_exists`(防已完成)保留在前。
- UI 顺带获益:claim 存在 = "处理中",报告 API 可暴露该状态(v1 可选)。

### 2. session 抽取(公网侧,不进 VPC)

- **新 Lambda `fieldsight-extract-session`(非 VPC)**,S3 事件触发于 `transcripts/` 前缀 `.json` 后缀(BUG-13:输出写 `extractions/` 前缀,不重叠)。
- 抽取单元 = **录制 session**(文件名 base,BUG-11 元数据):每个转录段落地即触发,**收集该 session 当前全部转录段**(同 base 前缀 list)→ normalize(transcript_utils)→ 一次 Claude 直连调用(报告生成器的 urllib3 模式 + ANTHROPIC_API_KEY)抽 topics/action_items/safety(小 prompt,单 session)→ 写 `extractions/{user}/{date}/{session_base}.json`(**幂等覆盖**)。
- **语言 trigger 试点**(身份系分析修订版信号层#4):抽取输出增加 `declared_site` 字段——仅识别**显式到场声明**("我到了/现在在 XX 工地",谈及≠到场),对照站点目录模糊匹配,附 confidence;v1 只随 extraction JSON 存证(item-writer 落 metadata),不改归属——归属消费等身份系阶段③的 recording_sessions 就绪后接入。
- 同 session 多段陆续到达 = 每段重抽全 session(段数少,单次 ~$0.01-0.05,可接受);防抖(如 90s 内跳过)列为后续优化,v1 不做。
- **会议纪要互斥(BUG-18)不适用**:session 抽取是运营条目不是文档,meeting manifest 不拦。

### 3. item 写入(VPC 侧)

- **新 Lambda `fieldsight-item-writer`(in-VPC,mirror IngestFunction 模式)**,S3 事件触发于 `extractions/` 前缀。
- 读 extraction JSON → 身份桥(复用 lambda_ingest 的 resolve_site/resolve_user;session 无 report['site'],直接走 user_mapping primary_site 链;双 miss 跳过)→ **source-key 幂等**(source_s3_key = extraction 键;delete_topics_for_source → upsert_topic,Phase 4a 的同款货架)。
- **夜间报告收编**:daily_report.json 的 ingest(Phase 4a 既有)在同事务中**追加删除该 (user,date) 的全部 session 来源 topics**(`source_s3_key LIKE 'extractions/{user}/{date}/%'`)——白天看 live 条目,夜里被整编后的报告版取代,不重复。
- Bedrock 放行后:writer 在同函数内补 chunk+embed(chunking.py 同款),session 级语料进 report_chunks——**零架构改动的汇合点**。

### 4. Dashboard 读(UI)

- org API 新端点 `GET /api/org/live-items?date=&site=`(topics + action_items + safety_observations,ACL 走 accessible_site_ids)。
- Tasks / Timeline / Today 合并渲染 live 条目,标 **Live 徽章**(区别于夜间报告条目);刷新即见(无推送,v1 手动刷新符合用户描述)。

## 时延预算(达标验证)

RealPTT 侧上传(不可控)→ 15 min 扫描窗 → VAD+Transcribe ~3-8 min → 抽取 ~1 min → **落表 ≤25 min 典型**,满足"半小时刷新可见"。

## 不做(明确出界)

- WebSocket/轮询推送(刷新即可);防抖;跨工地同日归属(身份系阶段③);PROD 手装栈的调度改造(先在 test 栈全链路验证,PROD 切换单独执行);向量化(等 Bedrock)。

## 依赖与顺序

- 不依赖 Bedrock 工单;不依赖身份系阶段①(身份桥现状够用,收编后自动更准)。
- 与 4c 的关系:4b 产出 live 运营条目,是 4c 聚合的数据源——**4b 先行**。

## 验证

- 单测:claim 条件写/接管逻辑;session 收集与幂等覆盖;writer 身份桥+收编删除。
- 端到端(test 栈):重触发一个真实录音文件 → 观察 users/→VAD→transcribe→extraction→Aurora 全链路 + 计时;重复触发同文件验证 claim 拒绝;夜间报告 force 重生成验证 session 条目被收编。
