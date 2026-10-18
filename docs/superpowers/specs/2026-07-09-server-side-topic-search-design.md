# 统一语义检索:Search(列表)+ Ask(合成)— 设计 spec (2026-07-09, v2)

> v1(词法扫 lake)已废弃。用户校准目标后 → **复用已建的语义引擎**(嵌入 + pgvector),
> Search 和 Ask 共享同一套检索,只是"头"不同。

## 目标(用户语)

- 搜关键字("door damage")→ **按 relevance 排的相关 topic 列表**,按日期分组,看"当天发生了什么"(不分 quality/safety)。
- relevance = **语义相似度**(看 topic 全文:标题 + breakdown/描述 + 转录细节),**不是时间**;`door damage` = door + damage 概念(非精确 match),类 Google `+` / Netflix。
- 语义列表不满足时 → 升级 **Ask the Agent**(RAG:检索 + LLM 合成带引用的答案)。

## 已验证证据(为什么复用现有引擎)

`door damage` 走现有 RAG 检索(纯语义,无精确匹配)召回 10 块:命中 "Door Hardware Issues/Specifications/Stop Delivery"(Jarley 2/9、3/2)+ 语义近邻 "Defect Management"、"Access/Delineation";含 `topic` 与 `transcript_window`(breakdown)两级。**证明:引擎已具备用户要的全部能力,只差一个"检索但不调 LLM"的出口。**

## 统一架构

```
查询 → [嵌入 DashScope v4] → [pgvector 检索 report_chunks, ACL 按 org 站点权限过滤]
                                          │
                     ┌────────────────────┴─────────────────────┐
         机制一 Search(mode=search)              机制二 Ask(mode=ask, 默认)
         chunks 聚合成 distinct topic,           chunks 喂 Claude Haiku,
         按最优相似度排、按日期分组,             合成 markdown 答案 + [n] 引用
         **不调 LLM**(快、便宜)                 (RAG 全链路)
```

**生态位**:嵌入=编码语义 · 向量搜索=按意思排序 · LLM=读检索到的片段写答案 · RAG=向量搜索+LLM。Search 用前两者,Ask 用全部。

## 范围:三条工作流

- **A. 覆盖回填**(前提):跑 ingest backfill,把 prod lake 全量报告嵌入进 `report_chunks`,保证"所有报告都能搜到"。(之前 park 的 backfill,现纳入。)
- **B. 后端 retrieve-only 检索 + 日期过滤**:ask-agent 加 `mode=search`;rag-search 加可选 `from/to`;报告 API 加 `/api/search` 路由。
- **C. 前端**:`search-palette.js` topic 改调 `/api/search`;Ask 变兜底。

---

## A. 覆盖回填(数据工作流,先跑)

1. 迁移 0007 已应用(get_by_folder_name 恢复,ingest 身份桥可用)。
2. `invoke fieldsight-test-ingest {"backfill":true}`(读 S3 边表 embeddings/{date}/{user}/vectors.json,幂等 upsert report_chunks + topics)。
3. **前置**:确认每份报告的嵌入边表存在;缺失的先 `invoke embed-report`(非 VPC,DashScope)补齐。
4. **验证**:`report_chunks` 覆盖 lake 6 个文件夹全部报告日期;抽查 Jarley door topics、David 3/9 均可被 rag-search 召回(dummy 探针 + 真实 "door"/"3月9日" 探针,site 分布覆盖多站)。记录 processed/skipped/failed。

## B. 后端

### B1. `lambda_rag_search.py` + `repositories/search_sql.py` — 日期过滤
`search_chunks(conn, qv, site_ids, k, date_from=None, date_to=None)`;SQL 加:
```sql
AND (%(date_from)s IS NULL OR c.report_date >= %(date_from)s)
AND (%(date_to)s   IS NULL OR c.report_date <= %(date_to)s)
```
rag-search event 读 `event.get("date_from")/("date_to")`(ISO date 或 None),透传。默认 None = 不过滤(与现状一致,Ask 不受影响)。

### B2. `lambda_ask_agent.py` — `mode=search` 分支(检索但不合成)
`_rag_answer(body)` 起始按 `mode = body.get("mode","ask")` 分流;`mode=="search"` 走 `_rag_search_list(body)`:
- 复用现有 `dashscope_utils.embed([question])[0]` + invoke rag-search(带 `date_from/date_to`,`k` 默认更大如 30——列表要广)。
- 拿 chunks 后**聚合成 topic 行**:按 `(report_date, site_id, topic_id)` 去重;无 `topic_id` 的 `transcript_window` 归到 `(report_date, site_id, source_s3_key)` 作"报告摘录"行。每行取该组**最小 distance**为 relevance 分。
- 排序:relevance 升序(distance 小=近);同分按 `report_date` 降序。
- 返回 `{"results":[{report_date, site_name, topic_id, title, snippet, chunk_type, route, score}], "count":N, "grounded":true}`。**不调 Claude。**
- `route` 后端拼好,与前端现有 topic route 完全一致:`/timeline?date=<d>&user=<folder>&topic=<String(topic_id)>`,全部 encodeURIComponent。
  - ⚠️ `route` 需要 `user`(folder)——chunk 有 `user_id`(可能 NULL)。folder 来源:citations 已带 `report_date/site_name`;topic route 的 user folder 需从 chunk 的 user_id→folder_name 解析(rag-search 可在 SELECT 加 `u.folder_name`;user_id NULL 时 route 退化为不带 &user,前端 Timeline 兜底解析)。**实现时在 rag-search 的 SELECT 补 folder_name。**

### B3. `lambda_fieldsight_api.py`(报告 API)— `/api/search` 路由
`search_topics(body, caller)`,镜像 `ask_question`(840–902 行):权限同款,payload 加 `mode:"search"` + `date_from/date_to`(来自 body),invoke ASK_AGENT_FUNCTION,透传结果。路由:`elif path == '/api/search' and method == 'POST': return search_topics(body, caller)`。
- `q<2` 字符 → 直接返回 `{"results":[],"count":0}`(不打后端)。

### ACL
**用 rag-search 既有 org 站点权限**(`get_user_by_sub`→`resolve_scope`→accessible_site_ids;admin/gm 全公司,余按 memberships)。**无需碰 user_mapping**——语义路径的 ACL 是站点级,天然正确、且是身份系正道。

### 延迟
Search 比词法多一次嵌入(~0.5–1s)+ pgvector(<100ms),端到端 ~1–2s。为语义质量值得;前端防抖 + loading 态吸收。

## C. 前端 `search-palette.js` + 新 `scripts/api/search.js`

- 新 `window.FS.api.search.topics({q, from, to})` → `request('/search',{method:'POST', baseUrl:orgBaseUrl, body:{question:q, date_from:from, date_to:to}})`(经 org 网关 `/api/{proxy+}`→报告 API;**不 prepend /org**)。mock 模式回退现有客户端 `_cache.topics` 行为。
- 删除 `_topicFanoutPairs`/`_loadTopics`/`TOPIC_FANOUT_CAP`/`_cache.topics` 及 `_loadCache` topic 分支。
- 查询异步 + **防抖 250ms**:输入变化 → 调 `search.topics`;tasks/safety/sites/people 仍同步走 `_cache`(小、正常)。合并展示;**topic 组按 `report_date` 分组小标题**("哪几天有 door 的事")。
- **loading 态**("Searching…"),避免结果未回时闪 Ask。
- 机制二兜底:**仅当 topic + 其它类型全零**才追加 "Ask FieldSight" 行(现为无条件)。
- (v1)日期:先支持前端把 `from/to` 传给 API(可选);自然语言日期("9号/3月9日"→日期过滤)**延后**,不做。

## 测试
- 后端单测:rag-search 日期过滤边界(含/排端点、None 不过滤);ask-agent mode=search 聚合(去重、无 topic_id 归组、relevance 排序、不调 Claude——mock call_claude 断言零调用);search_topics 权限(worker 只自己、admin 全量)、q<2 空、透传 date。
- 前端:防抖只发一次、合并顺序、零结果才出 Ask、mock 回退、route 字符串精确。

## 部署/回滚
- 后端串行 SAM deploy(`AWS_CLI_FILE_ENCODING=UTF-8 PYTHONUTF8=1`)。前端 search.js 新增 + palette 改 + cache-buster,Amplify 自动。
- 回滚点:git tag `backup-pre-search-20260709`、Aurora 快照 `fieldsight-pre-search-20260709`、`fieldsight-test-api:1`。B 只读检索、A 幂等 upsert——均可重跑/回退。

## 铁律
账户 509194952652 / ap-southeast-2;不碰公司 164088480050。串行部署。Windows 单行 Edit 锚点、不 `git add -A`。子代理 fresh sonnet 实现 + 文件式 brief/report + Fable 5 终审。
