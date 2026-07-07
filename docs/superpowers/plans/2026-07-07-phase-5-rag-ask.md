# Phase 5:RAG Ask + 引用 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development。
> 兑现用户最初需求:"Ask 之后有 cited 的信息"。语料已由 Phase 4d 回填(~198 chunks,DashScope v4@1024)。

**Goal:** 用户问题 → DashScope 嵌入 → `search_chunks`(ACL 收窄)→ Claude 合成答案 + 引用 → UI Ask 面板渲染答案(markdown)+ 引用卡片。

**Architecture(边界两跳,Phase 4d 已证):** 检索必须 in-VPC(够 Aurora),问题嵌入 + 答案合成需公网(NAT-less VPC 够不到 DashScope/Claude,BUG-36)。因 Ask 是交互式(不能走 S3 中转),用**同步两跳 Lambda invoke**(镜像 ApiFunction→AskAgent 既有模式):
```
UI POST /api/ask → ApiFunction(非VPC,校验 token,补 caller_sub)
  → invoke AskAgentFunction(非VPC):dashscope_utils.embed([question])[0]
      → invoke RagSearchFunction(in-VPC):get_user_by_sub→accessible_site_ids→search_chunks → {chunks}
      → claude_utils.call_claude 基于 chunks 合成答案 + 引用
  → {answer(markdown), citations:[…], model}
```

**Tech Stack:** Python 3.11/pytest;dashscope_utils(4d)/claude_utils(4b);psycopg;boto3 lambda invoke。

## Global Constraints(侦察锁定)

- **检索全局化(设计决定)**:RAG 检索**跨调用者 ACL 可见的全部站点**做语义搜索,不按 UI 的 date/user 硬过滤——用户的样例问题("二月九日工地有没有做关于门的检查,检查了哪几个建筑")日期在问句里而非过滤器;date/user 作软上下文传入 prompt,不作 WHERE。
- **ACL 铁律**:`resolve_scope(caller.global_role)=="ALL"`(admin/gm)→ 全公司站点;否则 `memberships.accessible_site_ids(conn, caller.id, role)`。**deny-by-default**:空 site_ids → search_chunks 返回空(WHERE site_id=ANY([]) 无行)。绝不跨公司。
- **身份桥**:Cognito sub 从 token(ApiFunction)→ ask_agent payload(`caller_sub`)→ rag_search(`get_user_by_sub(conn, sub)`)。sub 是 report 侧登录身份与 org 侧 users 的唯一桥。
- **查询向量跨界预算**:rag_search(in-VPC)**只收已算好的 1024-float 向量**,绝不自己嵌入(够不到 DashScope);ask_agent(非 VPC)嵌入后传入。向量以 `list[float]` 直接绑 `search_chunks`(chunks.py 注释证 list→vector cast 可行)。
- **引用契约**(UI ask.js 已约定 `{answer, citations, model}`):citations 每项 `{source_s3_key, report_date, site_name, topic_title, chunk_type, snippet}`(snippet=chunk_text 前 ~200 字符)。需扩 `build_search_sql` 增 `c.report_date, c.site_id` + JOIN sites 取 `site_name`。
- **合成 prompt 纪律**:严格 grounding 于检索到的 chunks(反幻觉,ask_agent 现有 SYSTEM_CONTEXT 精神延续);答案 markdown(UI 有 renderMarkdown);引用只列实际用到的 chunk 的 source。答案里可用 [1][2] 标注对应 citations 顺序。
- **无检索结果**:chunks 空 → 答案明说"未找到相关记录",citations 空(不编造)。
- **模型**:嵌入 text-embedding-v4@1024;合成用 claude_utils 的 CLAUDE_MODEL(默认 sonnet;ask 可另设 env 用 haiku 省钱——沿用 ask_agent 现有 HAIKU_MODEL 精神,但走 claude_utils)。
- 铁律:单行 Edit 锚;绝不 `git add -A`;pytest 零回归(基线 159);sam validate(BUG-35 前缀);串行部署;向量/答案跨境数据主权已知接受。

---

### Task 1: search_sql 扩列(引用需 report_date/site_name)+ 测试

**Files:** Modify `src/repositories/search_sql.py`;Modify `tests/unit/`(search_sql 若有测试;否则新建 test_search_sql.py)。

- [ ] build_search_sql 的 SELECT 增 `c.report_date`、`c.site_id`、JOIN `sites s ON s.id = c.site_id` 取 `s.name AS site_name`;WHERE/ORDER BY/LIMIT/ACL 不变;topics LEFT JOIN 保留。docstring 更新。
- [ ] 测试:断言 SQL 含 report_date、site_name、`s.id = c.site_id` JOIN、`::vector` 两处、`site_id = ANY`、`ORDER BY … <=>`、LIMIT。(纯字符串构造,无 DB。)
- [ ] 全套 pytest 零回归;提交 `feat(5): search_sql adds report_date + site_name for citations`。

### Task 2: lambda_rag_search(in-VPC,TDD)

**Files:** Create `src/lambda_rag_search.py`、`tests/unit/test_lambda_rag_search.py`。

- Consumes:`users.get_user_by_sub`、`memberships.accessible_site_ids`、`resolve_scope`、`sites.list_company_sites`、`chunks.search_chunks`、`db.connection.get_connection`。
- Produces(invoke 契约):事件 `{"sub": "...", "query_embedding": [1024 floats], "k": 8}` → `{"chunks": [rows], "site_count": N}`;caller 未找到 → `{"chunks": [], "error": "caller not provisioned"}`(不抛,让 ask_agent 优雅降级)。
- [ ] 测试先行(FakeConn/monkeypatch repos):sub→caller 解析;admin ALL 走 list_company_sites vs worker 走 accessible_site_ids;空 site_ids→空 chunks 零查询;search_chunks 收到 (query_embedding, site_ids, k);caller miss→error 对象非抛;k 默认 8。
- [ ] 实现:`with get_connection() as conn:` get_user_by_sub → ACL 分支(镜像 org_api list_live_items)→ search_chunks → 返回 rows。
- [ ] 全 PASS 零回归;提交 `feat(5): rag-search lambda (in-VPC ACL + search_chunks)`。

### Task 3: lambda_ask_agent RAG 模式(非 VPC,TDD)

**Files:** Modify `src/lambda_ask_agent.py`;Modify `tests/unit/`(若有 ask agent 测试;否则新建)。

- Consumes:`dashscope_utils.embed`、`claude_utils.call_claude`、boto3 lambda invoke(RAG_SEARCH_FUNCTION env)。
- Produces:handler 事件增 `caller_sub`;流程:embed([question])[0] → invoke RagSearchFunction `{sub:caller_sub, query_embedding, k}` → 取 chunks → 无 chunks 则答"未找到";否则 build_rag_prompt(question + chunks 的 chunk_text/topic_title/site_name/report_date,编号)→ call_claude 合成 markdown 答案(引用 [n])→ 返回 `{answer, citations:[{source_s3_key, report_date, site_name, topic_title, chunk_type, snippet}], model, grounded:true}`。旧 S3-file 模式:若无 caller_sub(向后兼容/直接 invoke)可保留旧路径,或明确走 RAG——**v1 保留旧 S3 模式为 fallback**(caller_sub 缺失时),RAG 为主路径。
- [ ] 测试先行(monkeypatch dashscope_utils.embed + claude_utils.call_claude + lambda invoke):embed 调用 question;rag_search invoke 收到 caller_sub+向量;chunks→prompt 含各 chunk 文本与编号;citations 形状(snippet 截断);无 chunks→答未找到+citations 空;call_claude 失败→error 优雅返回。
- [ ] 实现。
- [ ] 全 PASS 零回归;提交 `feat(5): ask-agent RAG mode (embed→search→synthesize with citations)`。

### Task 4: ApiFunction 传 caller_sub + 放宽 date(小改)

**Files:** Modify `src/lambda_fieldsight_api.py`(ask_question);Modify 其测试(若有)。

- [ ] ask_question payload 增 `'caller_sub': caller.get('sub')`(确认 caller 有 sub;report 侧 caller 来自 Cognito claims);`date` 由硬错误改为可选(RAG 全局检索不需要 date;UI 仍会传,不破坏)。worker 自限保留(报告侧权限)但不影响 org ACL(rag_search 独立按 sub 收窄)。
- [ ] 测试:payload 含 caller_sub;缺 date 不再 400。
- [ ] 提交 `feat(5): api forwards caller_sub to ask-agent, date optional for RAG`。

### Task 5: 基础设施(rag-search 函数 + ask-agent env + wire)

**Files:** Modify `src/template.yaml`。

- [ ] `RagSearchFunction`(in-VPC,mirror OrgApiFunction 的 VpcConfig/PsycopgLayer/PG env;Condition HasDb):FunctionName ${P}-rag-search;Handler lambda_rag_search.lambda_handler;Timeout 30;Mem 512;无 Events(ask-agent invoke)。
- [ ] `AskAgentFunction` 增 env:`RAG_SEARCH_FUNCTION: !Ref RagSearchFunction`、`DASHSCOPE_API_KEY: !Ref DashScopeApiKey`、`DASHSCOPE_BASE_URL/EMBED_MODEL/EMBED_DIM`、`CLAUDE_MODEL`(若未有);Policies 增 `LambdaInvokePolicy: FunctionName: !Ref RagSearchFunction`。
- [ ] `sam validate --lint`(容忍 W2531);提交 `feat(5): infra — rag-search fn, ask-agent dashscope+invoke wiring`。

### Task 6: Fable 终审 → PR → 部署 → 端到端(控制器)

- [ ] 整分支 diff → Fable 5 终审(镜头:ACL deny-by-default 与跨公司隔离、sub 桥、查询向量跨界不在 VPC 内嵌入、citations 不泄漏未授权站点、无结果不编造、prompt 注入(chunk 文本围栏)、两跳 invoke 错误传播、date 放宽不破坏旧流)。修→复审。
- [ ] PR → 用户合并 → 部署 success。
- [ ] **端到端**:直接 invoke ask-agent `{question:"door inspection at Ellesmere", caller_sub:"<Ben admin sub f99e04e8-a0c1-7091-3da6-ed96fd63eb08>"}` → 断言 answer 非空、citations 含 2026-02-09 相关 source_s3_key、grounded=true;worker sub(Ben Test 899e6408-…)问同问题 → citations 仅其可见站点(ACL 验证)。
- [ ] 账本 + memory。

### Task 7: UI 引用卡片(fieldsight-ui,dev,另 PR)

**Files:** Modify `scripts/composites/ask-chat.js`(citations 占位→真卡片)、`styles/`(卡片样式)、`app-shell-preview.html`(buster)。

- [ ] ask-chat.js 的 `fs-ask-chat__citations` 块(现仅显示 count)→ 渲染引用卡片列表:每项 site_name · report_date · topic_title + snippet;点击可跳该 report(源 date/user 从 source_s3_key 解析);答案已走 renderMarkdown 不动。
- [ ] node --check;Fable 终审(镜头:XSS——snippet 经 escape;envelope 守卫;mock 路径 citations 空不炸)→ 修 → PR。

## 自审
- 需求闭环:嵌入(T3)→检索 ACL(T2)→合成引用(T3)→UI 渲染(T7);search_sql 引用列(T1);两跳边界(T2 in-VPC / T3 非 VPC)。
- 接口一致:query_embedding 契约贯穿 T2/T3;caller_sub 桥贯穿 T3/T4;citations 形状贯穿 T3/T7;site_name/report_date 贯穿 T1/T2/T3。
- 预判:BUG-36(向量预算跨界)、deny-by-default(空 site_ids)、跨公司隔离、sub 桥缺失 fallback、date 放宽不破 UI、prompt 注入围栏。
