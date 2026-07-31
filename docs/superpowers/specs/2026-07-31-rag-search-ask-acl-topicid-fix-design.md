# RAG Search + Ask — topic_id linkage, identity, graded ACL parity (design)

**Date:** 2026-07-31 · **Status:** design (approved approach, pending spec review)
**Scope:** prod客户站 `main.d2fssznicvuckr` 的中面板 **Search** 恒空 + **Ask agent** 400/403 的综合修复。

---

## 1. Problem (what's broken, verified 2026-07-31)

Diagnosis (Chrome + AWS + `rds-data` 直查 prod 库 `fieldsight`) 确认三个叠加缺陷,详见 memory
`fieldsight-search-ask-regression` / CLAUDE.md BUG-39:

- **S1 — Search 恒空 (回归, 断点 2026-07-17).** `report_chunks.topic_id` 自 2026-07-17 起新建的
  **全为 NULL**(按 `created_at` 分组: 07-16 前有、07-17/18/22/23/30 全 0)。搜索聚合
  `lambda_ask_agent._aggregate_topics` `if not topic_id: continue` **丢弃无-topic chunk** →
  count:0 对任何查询。触发 = **AUTHORITY_FLIP**(#64/#67, `PROD_AUTHORITY_FLIP`)+ **G5b #71**:
  defer 天 `lambda_ingest` 走 `defer_to_extraction` 分支 → `topic_seq_to_id={}` →
  每条 chunk `topic_id=None`(ingest 行341 注释明说是有意取舍)。检索本身健康
  (库 254×1024 向量; v4 嵌 "recording" 距 chunk 0.357<0.55; 直调 `fieldsight-prod-rag-search`
  给正确 sub → 8 条)。
- **S2 — Ask 400/403 (身份).** `/api/search`、`/api/ask`、`/api/ask/voice` 经 `/api/{proxy+}` →
  **遗留 `fieldsight-prod-api`**(dashboard 走 `/api/org/{proxy+}` → org-api 故正常)。其
  `get_caller_identity` 用遗留 DynamoDB `fieldsight-users`(仅4行)→ org 账号(≈现在所有真实账号)
  `role='viewer'`、`sites=[]`、`display_name=''`。`ask_question` 因此 400 `"Missing user"`(全局 Ask
  无 user)/ 403 `"Access denied to this user"`(带 user)。**media presign/audio 早已迁 org-api,不受影响**
  ——仅这三个 RAG 端点还在遗留栈。
- **S3 — rag-search ACL 只按站点, 缺 per-author 分级 (安全缺口).** `lambda_rag_search` 用遗留二元
  `resolve_scope`(ALL vs `accessible_site_ids`) + `search_chunks(qv, site_ids, k)`,**不应用**
  `visible_user_scope` 的 per-author 过滤。而 org-api dashboard **有**(`_author_filter` /
  `repositories/scope.py`,`topics.py` fail-closed)。故一旦解锁 search/Ask,site_manager 会搜到
  **同站其他 SM/PM/GM** 的内容 —— 比 dashboard 宽,越权。
- **S4 — site-scoped 搜索 UUID-vs-slug.** 前端 `search.js` 发 `site`=站点 **UUID**;
  `lambda_rag_search:79` `get_company_site_by_slug` 当 **slug** 查 → 11 站里 **7 个 slug=NULL**
  (含 UC PK) → 站点范围空短路 0。

## 2. Goals / Non-goals

**Goals**
1. Search 对 authority-flip 后的数据能返回结果(立即恢复全部存量 + 长期正确)。
2. Ask / Ask-voice 对 org 账号可用(去 400/403)。
3. **Search 与 Ask 的可见性与 dashboard 逐位一致**:site_manager = SELF+WORKERS,不漏到其他 SM/PM/GM;
   跨站点 deny-by-default 不变。
4. site-scoped(选中项目)搜索可用。

**Non-goals**
- 不把 `/search`+`/ask` 整体迁移到 org-api(更大重构,另议)。本次就地修遗留栈 + 升级 rag-search ACL。
- 不改 authority-flip 本身的行为(extraction topic 仍是 item store 的 SoR)。
- 不重做嵌入模型/阈值(已验证健康)。

## 3. Design — 四条工作流

### WS1 — Search topic_id: 热修 + 根治

**热修 (先上, 立即恢复全部存量, 零 re-ingest).** 改 `lambda_ask_agent._aggregate_topics`:
- 不再 `if not topic_id: continue` 一刀切;改为**只丢弃 `chunk_type != 'topic'` 的无-topic chunk**
  (原意就是"别把裸转写窗口塞进 topics 列表")。**`chunk_type=='topic'` 的 chunk 即使 topic_id 为 NULL 也保留。**
- 分组键从 `(date, site_id, topic_id)` 改为 **`(date, site_id, COALESCE(topic_id::text, topic_title))`**
  —— 有 topic_id 用 UUID,无则用标题,保证 defer 天也能聚合。
- 深链:有 topic_id 用现有 UUID 路径;无则继续用已支持的 `&topicTitle=`(Timeline 按标题 spotlight,
  defer 天 Timeline 服务报告 prose,标题可命中)。标题取用顺序 = chunk metadata `title` → 兜底
  `chunk_text` 前 60 字(现有 `_aggregate_topics` 已有此兜底: `c.get("topic_title") or chunk_text[:60]`)。
  实测 defer 天 chunk 的 `metadata.title` 可能为空但 `chunk_text` 以 `[time] 标题` 开头,兜底可用。

**根治 (随后, 长期正确).** 改 `lambda_ingest` 的 defer 分支:defer 天不再让 `topic_seq_to_id` 空着,
而是把每个报告 topic **关联到当天已存在的 Aurora extraction topic 的 UUID**:
- 载入 `(site_id, user_id, report_date)` 的 extraction-sourced topics(`source_s3_key` 前缀 `extractions/`);
  确认存在(实测 UC PK 07-17 起每天都有)。
- 匹配:主键 **时间重叠**(报告 topic 的 `time_range` ∩ extraction topic 的 `occurred_at`/时窗),
  标题相似度做 tiebreak;匹配到 → `topic_seq_to_id[report_seq]=extraction_topic_id`。
- 匹配不到的报告 topic → 保持 `topic_id=None`(由热修的标题路径兜底,不回退成不可搜索)。
- 幂等:走现有 source-key 幂等(`delete_chunks_for_source` 后重插),重跑安全。

**回填.** 对 **2026-07-17 → 今天** 逐 `(date, folder)` 重跑 `lambda_ingest`(或 reindex-vectors apply
路径)重建 chunk 的 topic_id。回填后 `count(topic_id)` 应恢复非零。范围来自 `report_chunks`
`WHERE topic_id IS NULL AND created_at::date >= '2026-07-17'` 的 distinct `(report_date, folder)`。

### WS2 — Ask/Search 身份: 去遗留门 + 依赖 caller_sub

改 `lambda_fieldsight_api`:
- `ask_question`(行966): **整段移除遗留 user/role 门** —— 不再 400 "Missing user"(`user` 降为
  可选软上下文),不再 `can_access_user_data` 的 403 预检。**ACL 单一来源 = 下游 rag-search 的分级
  ACL(WS3),经 `caller_sub` 强制**;报表 API 不再自己判权。
- `search_topics`(行1085): 已无阻断性 user 门,确认仅转发 `caller_sub`(已是)。
- `caller_sub` 仍来自 Cognito claims(`get_caller_identity` 行84,不被 DynamoDB 覆盖),对 org 账号正确。
- **不改** `get_caller_identity` 的 DynamoDB 主体(广谱化列为可选未来防御,非本次)。
- 向后兼容: 遗留 DynamoDB 账号若也在 Aurora `users`,行为不变(rag-search 经 caller_sub 照常解析);
  仅在 DynamoDB 而不在 Aurora 的账号本就已经 empty(rag-search "caller not provisioned"),非本次回归。

### WS3 — rag-search 分级 ACL 对齐 (安全, 必须与 WS2 同发)

改 `lambda_rag_search` + `search_chunks`,使其 per-author 可见性与 dashboard 逐位一致:
- rag-search 复用 **`repositories/scope.py`** 的同一 scope 解析器(org-api `_author_filter` 用的那套),
  由 `caller_sub` → `(site_ids, user_scope, author_ids)`。**不新写 ACL 逻辑**,避免与 dashboard 漂移。
- `search_chunks` 增参 `author_ids`(default None=不过滤):当 `user_scope ∈ {SELF, SELF+WORKERS}` 时,
  `WHERE site_id = ANY(site_ids) AND user_id = ANY(author_ids)`;`user_scope ∈ {ALL, SITE}` 时只按站点。
- **fail-closed**:SELF/SELF+WORKERS 下,`user_id IS NULL` 的 chunk 一律排除(镜像 `topics.py:252`),
  防未署名行泄漏。
- 尊重同一 `GRADED_ROLES` 开关(prod `PROD_GRADED_ROLES=true`):graded off 时回落旧站点-only 行为
  (与 dashboard 当前门控一致)。

### WS4 — site-scoped: 接受 UUID + 回填 slug

- `lambda_rag_search`(行74-83 的 site_filter 段): `site` 参数**先按 UUID 命中**该 caller 可访问站点
  集(`str(s)==site`),命中即用;未命中再退回 `get_company_site_by_slug`(兼容旧 slug 调用)。
  未知/越权 → `[]`(deny 不变)。
- 回填/自动生成 slug:给 `create_site`(org-api 建站)加 slug 自动生成(name→slug),并对现有 7 个
  NULL-slug 站 `set_slug` 回填。**用途**:`_aggregate_topics` 的 `&site=` 深链选择器联动需要 slug;
  搜索本身有了 UUID 兼容后不再依赖 slug。（此项独立、低风险,可最后做。）

## 4. Files touched

| 文件 | 改动 | WS |
|---|---|---|
| `src/lambda_ask_agent.py` `_aggregate_topics` | 保留 topic 型无-id chunk;按 `COALESCE(id,title)` 分组 | WS1 热修 |
| `src/lambda_ingest.py` defer 分支 | defer 天匹配 extraction topic 填 `topic_seq_to_id` | WS1 根治 |
| (回填脚本/重跑) | 07-17→今 逐 (date,folder) 重跑 ingest | WS1 回填 |
| `src/lambda_fieldsight_api.py` `ask_question`/`search_topics` | 去遗留 user/role 门 | WS2 |
| `src/lambda_rag_search.py` | 复用 `scope.py` 解析 author_ids;site 参数接受 UUID | WS3+WS4 |
| `src/repositories/chunks.py` `search_chunks` | 增 `author_ids` 过滤 + NULL-user fail-closed | WS3 |
| `src/repositories/sites.py` / org-api `create_site` | slug 自动生成 + 回填 | WS4 |

## 5. ACL correctness (核心安全)

修后 Search/Ask 的可见性 = dashboard 的可见性(同一 `scope.py` 解析器):

| 调用者 | 跨站点 | 站内 per-author |
|---|---|---|
| worker | 仅所属站 | SELF(仅自己)|
| **site_manager** | 仅所属站 | **SELF+WORKERS(自己+手下 worker,看不到其他 SM/PM/GM)** |
| pm / regional_manager | 所属站(pm)/跨项目 | SITE(站内所有人)|
| admin / gm | 全公司站 | ALL |
| platform_admin | 跨公司 | ALL |

跨站点 deny-by-default(空 site_ids → 空结果)始终生效。fail-closed:SELF/SELF+WORKERS 下丢弃
无 user_id 的 chunk。

## 6. Testing

- **单测 (`tests/unit`, FakeConn/FakeCursor)**:
  - `_aggregate_topics`: 无-topic_id 的 `chunk_type='topic'` chunk 被保留并按标题分组;裸 transcript_window 仍丢。
  - rag-search ACL: 各 `user_scope` 的 `author_ids` 生成 + `search_chunks` WHERE 组装 + NULL-user fail-closed;
    graded off 回落站点-only。site 参数 UUID 命中 / slug 回退 / 越权→[]。
  - `ask_question`/`search_topics`: 无 user 不再 400;org 账号(DynamoDB miss)不再 403;caller_sub 透传。
  - ingest defer 分支: extraction topic 时间重叠匹配填 topic_id;不匹配→None;幂等重跑。
- **端到端 (test 栈 + dev 前端指向 test 网关)**:site_manager 账号搜索只见 SELF+WORKERS;
  Ask 返回 citations;选中项目 site-scoped 有结果。回填后 prod `count(topic_id)` 非零。

## 7. Rollout

1. WS1 热修 + WS2 + WS3(+WS4 UUID)一批 → `develop` → test 栈验证(dev 前端 repoint test 网关,
   `fieldsight_test` 库)。
2. 合 `main` → prod 审批部署。
3. **WS1 回填**:prod 部署后跑 07-17→今 的 ingest 重跑(用户经 `!` 触发受权命令),验证 `count(topic_id)`。
4. WS4 slug 回填独立小 PR,最后做。
- prod/test 共用 Aurora 集群 `fieldsight-db-test-dbcluster-hywiixu8ihi9`(Data API 已开),
  但 `fieldsight`(prod)/`fieldsight_test` 物理隔离(BUG-38);回填只针对 prod 库。

## 8. Risks & rollback

- **越权风险(最高)**:WS3 必须与 WS2 **同批上线**——绝不能只上 WS2(去门)而不上 WS3(author 过滤),
  否则窗口期内 site_manager 可搜到同站他人内容。单测强制覆盖 fail-closed。
- **匹配不准(WS1 根治)**:report topic↔extraction topic 时间/标题匹配可能不完美;不匹配的 chunk
  回退热修标题路径,不会不可搜索。可接受。
- **回填破坏性**:走现有 source-key 幂等(先删后插),对 prod 客户数据是重建同源 chunk,幂等安全;
  失败可按 (date,folder) 重跑。
- **回滚**:各 WS 独立可回滚。WS1 热修是纯前端聚合逻辑改(无数据变更);WS2/WS3 是 lambda 代码,
  回退旧版即恢复(但会恢复 400/403);WS4 slug 回填是加列值,无破坏。
