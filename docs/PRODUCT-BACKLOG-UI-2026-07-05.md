# FieldSight 产品迭代 Backlog(2026-07-05,用户浏览 UI 后提出)

> 用户浏览 dev 站(Jarley/site_manager 登录)后提的 5 点。已逐条基于**真实代码 + S3 数据**核实根因,不是猜。"也许不是当下"——放这里排期。
> 归属:🟢=UI 小改(可并入批次 2b/2c 或 polish);🟡=数据/运维;🔴=战略新能力(Phase 4 / dashboard-first)。

---

## 1. 🟡 Topic 的 "Transcript" 面板空白 —— **数据问题,非代码 bug**

**现象**:每个 topic 的 Transcript 标签没内容。

**根因(已核实)**:
- **UI 取数链路正确**:`timeline.js:961` 渲染 `TranscriptList` → `transcript-list.js:60` 调 `FS.api.transcripts.getTranscripts({date,user,start,end})` → live 走 `transcripts.js:32` `request('/transcripts', …)`。API `lambda_fieldsight_api.py:404-543` 按 4 个前缀找 `transcripts/{user}/{date}/`,**找不到就返回空 200**(`:454` `message:"No transcripts found"`)。UI 忠实显示空。**不是 UI 门控 bug、不是 API 报错。**
- **是源数据被删**:S3 `transcripts/` 下有 `Ben_Lin/Ben_Test/David_Barillaro/MPI1/`,**没有 `Jarley_Trainor/`**;而 UI 登录正是 Jarley,报告(`reports/2026-03-02/Jarley_Trainor/`)还在——报告文本是**当时从转录生成后留下的**,原始 Transcribe JSON 已删。
- **无法重生**:Jarley 音频只剩 `2026-03-20`、`2026-03-24`(4 文件);Feb-早3月报告期的音频也没了 → 那些日期的转录**重跑不出来**(无源音频)。David 的转录只有 5-6 月,对不上 demo 报告日期。

**建议**:
- **短期**:接受历史 demo 期缺转录;新录制走完整管线时 Transcript 会正常填充(管线仍写 `transcripts/`)。若要 live 转录 demo 数据,只能用 Jarley 有音频的 3-20/3-24 重跑转录 + 生成报告。
- **长期(真正的修)**:Phase 4 把转录/报告切块入 Postgres `report_chunks`(已建库),数据落库后不受 S3 lifecycle 删除影响——"转录会消失"的根本解。
- **次要代码风险**(转录存在时才触发):`time_range` 塌缩成 `"12:18 – 12:18"`(pipeline BUG-09)→ API ±60s 窗口过滤到空。转录恢复后若仍空,查该 topic 的 `time_range` 值。

**归属**:数据/运维 + Phase 4 持久化。**非阻塞、非 bug。**

---

## 2. 🟢 Tasks 页加可选时间段

**现状(已核实)**:`tasks.js` **没有** RangeToolbar——硬编码 14 天窗口(`tasks.js:38` `DEFAULT_DAYS=14`),只有状态 chips(All/Mine/Open/Overdue/Done,`tasks.js:326`)。

**建议**:接现成的共享 `RangeToolbar` composite(`composites/range-toolbar.js`),照 `safety.js:216`/`quality.js:216`/`evidence.js:326`/`insights.js:550` 的接法(Today/7d/30d/All/Custom)。把 tasks 的 from/to 从固定 14 天改为工具栏驱动。

**归属**:🟢 UI 小功能,~半天。可并入批次 2b 或单独 polish。

---

## 3. 🟢 Settings/时间段选择器 Light 模式改导航蓝底白字(更醒目)

**现状(已核实)**:所谓 "Settings 时间段" 就是共享的 `RangeToolbar` + `DatePicker`(range 模式),不是独立控件。Light 模式下选中段偏弱:
- 端点 `.fs-date-picker__cell--selected`(`composites.css:1713`)**只设边框+阴影,无背景/字色** → 端点仍是面板底色。
- 区间内 `.fs-date-picker__cell--in-range`(`composites.css:1687`)用 accent 混色。

**建议**:`.fs-date-picker__cell--selected` **加** `background: var(--color-navy-…)` + `color:#fff`(端点=导航深蓝底白字);区间内视需要同步加深。若也要改预设 chip 的选中态,改 `range-toolbar.js:110-115`(内联样式,不是 CSS)。注意用 tokens、双主题都验(dark 已有 in-range 规则)。

**归属**:🟢 UI 样式,~1-2 小时。

---

## 4. 🔴 跨周"话题完成识别" + 人工确认(Construction Intelligence)

**用户想法**:很多 topic 带 due date;下周收集信息时,系统能否识别相关话题、把已解决的**候选**拎出来让 user **人工确认**关闭(**不要自动关**,要的是交互——一个 button 提取所有"可标记完成"的任务待确认)。

**现状(已核实)——目前后端基本不做这个**:
- 报告彼此独立。周报/月报是 LLM 对某窗口日报的**散文总结**(`lambda_report_generator.py:612` weekly prompt:"identify trends / compile outstanding items / mark overdue"),但**没有逐项身份、没有"周一开的项周四关了"的检测、没有回写标记完成**。
- action 勾选是**手动**,键是 `(date, topic_id, action_index)` 位置式且按日期分区(`lambda_fieldsight_api.py:602`)——**同一现实事项在另一天是不同 item,没有稳定跨日 ID、没有关联逻辑**。DynamoDB items 表 `ENABLE_DYNAMODB` 门控,生产关闭。
- 全仓搜 `resolv*/carry-over/auto-close/link topic/recurr*`:**无**跨报告解决/关联/自动完成逻辑。

**建议(战略新能力,分层)**:
1. **前置:稳定的跨报告 item 身份**——现在位置式键做不了跟踪。这正是 [[fieldsight-dashboard-first-direction]](item store = source of truth)要解决的:每个 action/topic 有持久 ID,跨日/跨报告可追踪。
2. **匹配/解决识别层**:新报告入库时,把新话题与"已开项"做语义匹配(Phase 4 pgvector 可复用),标出"疑似已解决"候选(带证据:哪份报告哪句话说做完了)。
3. **人工确认交互(用户明确要的)**:一个"复核可关闭项"入口,列出候选 + 证据,user 逐条确认才关闭——**绝不自动关**。
4. 可选:due date 逼近/超期的主动提醒。

**归属**:🔴 依赖 dashboard-first(持久 item)+ Phase 4(语义匹配)。中大型,排在 Phase 4 之后 / dashboard-first 一起设计。**记入长期路线。**

---

## 5. 🔴 中栏搜索:从关键字 → 自然语言问答

**用户想法**:搜索框现在只关键字;希望能直接问"二月九日工地有没有做关于门的检查,检查了哪几个建筑?"然后得到总结答案。

**现状(已核实)**:
- 搜索是**纯客户端关键字子串匹配**(`search-palette.js:184` `_search`,对缓存的 tasks/safety/sites/people/topics 做 `indexOf`),**无网络/LLM**。
- 有 "Ask FieldSight: …" 移交行(`search-palette.js:346`),但它只是**跳到某个具体日期+用户的报告**里的 AskChat(`:364` 导航 `/timeline?date=&user=`),不是直接问答端点。
- 真正的问答端点 `POST /api/ask`(`lambda_ask_agent.py`)是**单报告作用域**:塞入某 `date+user` 的报告文本 + 原始转录喂 Haiku(prompt-stuffing,`:368`),**没有向量检索**。所以"Feb 9 门检查、哪几栋"这种跨日/跨建筑问题,今天只能先导航到那天那人的报告再问,**搜索框本身答不了**。

**建议(Phase 4 的正题回报)**:
1. **长期正解**:Phase 4 已建 `report_chunks` + pgvector + HNSW + 库内 ACL——做**语义检索/RAG**:问题→检索跨日期/工地的相关切块→Ask agent 基于检索结果grounded 作答。这正是 Phase 4 向量层的用途。
2. **搜索框接线**:Phase 4 检索上线后,把搜索框的"Ask"从"跳单报告"改为"走 RAG 问答"(可跨日期回答)。
3. **交互**:搜索框输入问题 → 顶部给一个"问答"答案卡(带引用的报告/日期),下面仍列关键字命中。
4. **引用/cited(2026-07-05 用户强调)**:RAG 回答**必须带引用**——具体到哪一段 transcript / 哪份报告,提升准确性与信任度。RAG 天然产出检索到的来源切块,把它们作为 citations 随答案返回。**前端已就绪**:`ask-chat.js` 的消息结构已有 `citations?` 槽位(`ask-chat.js:159` 已渲染),后端 `/api/ask` 走 RAG 后填 `citations`(切块引用 + 报告/日期链接)即可,前端点开跳转对应报告/topic。

**归属**:🔴 依赖 Phase 4(pgvector 检索 + 抽取入库)。搜索框接线 + citations 是 Phase 4 之后的 UI 收尾。**记入长期路线。**

---

## 6. ✅ Ask 回复 markdown 渲染(2026-07-05 用户提出,**已做**)

**现象**:Ask 的 LLM 回复是 markdown,但当作纯文本显示。
**已做**(PR #26,fieldsight-ui):新增 `scripts/composites/markdown.js`——微型、无依赖、**XSS 安全**的 markdown→HTML(先 HTML 转义,再只产出固定安全标签集 p/br/strong/em/code/pre/ul/ol/li/h1-3/a[http-only],LLM 输出的 HTML/脚本无法存活)。assistant 气泡用它渲染,user 气泡保持纯文本;node 验证过渲染 + XSS 转义。无构建步骤/无库(CSP 禁 CDN)。

---

## 排期建议速览

| # | 项 | 类型 | 依赖 | 何时 |
|---|---|---|---|---|
| 6 | Ask 回复 markdown 渲染 | ✅ 已做 | 无 | PR #26 |
| 2 | Tasks 时间段工具栏 | ✅ 已做 | 无 | PR #26 |
| 3 | 时间段选中态 navy/white | ✅ 已做 | 无 | PR #26 |
| 1 | Transcript 空(数据) | 🟡 数据 | Phase 4 持久化 | 非阻塞;新数据自愈 |
| 5 | 搜索自然语言问答 + **cited 引用** | 🔴 战略 | Phase 4 RAG | Phase 4 后 |
| 4 | 跨周完成识别+人工确认 | 🔴 战略 | dashboard-first + Phase 4 | Phase 4 / dashboard-first |

相关记忆:[[fieldsight-dashboard-first-direction]] [[fieldsight-recording-site-attribution-gap]] [[fieldsight-current-progress]]
