# 智库第二轮 · 合成：分包商段 + 开发商段

**日期**：2026-09-28 · **输入**：`2026-09-28-round2-subcontractor-segment.md`、`2026-09-28-round2-developer-owner-segment.md`。
**证据质量**：比第一轮好。网络策略放开后，律所、合同文本（SA-2017 全文）、行业刊、Capterra、Play 商店评论、NZ 建商播客、美国 lender 调研都是**第一手**。Reddit 仍然拿不到（Reddit 自己拦机器人）。两个段共同的空白是**NZ 本地人的第一人称原话**：NZ 分包工头怎么汇报、NZ 开发商怎么骂 contractor 的报告，公开语料里都没有。这两个空白只能靠电话。

---

## 1. 两个段各自的一句话

**分包商段**：分包不是为"记日报"付钱，是为**合同上的 5 个工作日**付钱。SA-2017（NZ 分包标准合同）9.1.3 条：收到口头指令 5 个工作日内不书面通知，这活就不算变更；反过来，分包一旦书面通知，GC 5 天不回就**自动视为变更**。这条进攻性条款没人用，因为没人在第一天写那封邮件。FieldSight 的碎片 + `nz_time` 工作日历 = 一个自动走的时钟。Hardline（US）、Construction Metric（UK）都在做"语音变日志"，没有一家懂 NZ 的合同时限。

**开发商段**：开发商付利息的是 **draw 周期**（Rabbet 2026 调研：25% 的 lender 退回过 draw，92% 归因于补文件；hud.loans：有开发商为不停工垫了 $800k），失眠的是**"绿的报告、红的现场"**（watermelon report）。NZ 非银 lender 已经在收照片验进度。而且**第一个付费用户很可能不是开发商本人，是 client-side PM 事务所**（Styles、RDT Pacific 那类）：他们已经在做"周巡场 + 拍照 + 一页报告"，现有 App 零改动就能接，不需要 GC 同意。

---

## 2. 两个段撞上的东西

| 结论 | 分包段 | 开发商段 |
|---|---|---|
| **时钟 / 日历是产品的心跳，不是日报** | SA-2017 的 5 WD 变更/EOT 时限 | lender 的 draw 日期、PCG 会议日期 |
| **"沉默"是最值钱的信号** | 收到口头指令 N、书面确认 M、超时 K | 本应在进行却三周没碎片的 programme task；连续四周未关闭的 thread |
| **记录归属：谁买的平台谁拥有，所以每一方都要自己的副本** | Procore 自己承认 subs "were simply renting access"，项目关闭后 "Account Not Found" | GC 抱怨业主买了 Procore 后锁走 GC 的历史记录 |
| **不碰钱** | 不算变更金额、不做 CCA 合规文书 | 不做 cost / budget / EOT 模块，那是 Mastt / Northspyre 的地盘 |
| **入口是办公室的人，不是现场的人** | 合同经理买、工头的负担 ≤ 一通电话 | owner's rep / PM 事务所买，开发商是读者 |
| **"语音→日志"已商品化，可守的是本地合同知识 + 跨周模式** | Hardline / Construction Metric / Nora | Procore Daily Log Agent（锁 Premier） |
| **Evidence Pack 要有确定的触发日** | PC 日、留置金释放日、DLP 结束 | draw 日、PCG 日 |

**对现有路线的含义**：两个段都把 Evidence Pack 从"争议时导出"变成**"按日历自动打包"**。这比第一轮的版本更可卖，因为它有固定的付费节奏。它依然依赖 Track B 的稳定身份。

---

## 3. NZ 沟通习惯：owner 的更正与证据的关系

owner 说 NZ 分包用电话或 email，不用 WhatsApp。能找到的 NZ 材料（Pedersen Homes："把 16 个晚间电话换成 2 点站会"；Master Builders 的 NZS 3910 指南默认"notice in writing"）**与此一致，但全部来自建商/GC 侧，没有分包工头的自述**。AU 和 UK 确实是 WhatsApp。

**设计上的后果**：分包的入口不是 WhatsApp 机器人，是**App 里一条 14:00 的三问 PTT**（卡住什么 / GC 让你做了什么额外的 / 明天缺什么），替代收工电话。第二问直接产出 `instruction_received`。老板收到的是转写 + 原音 + 一封次日的 9.1.3 通知草稿，不是 WhatsApp 消息。WhatsApp 入口留给 AU 扩张时再做。

---

## 4. 去重后的想法清单（按 12 个月可卖性）

### 分包商段
1. **SA-2017 时钟**：`instruction_received` → D+1 通知草稿 → D+5 提醒 → GC 侧 5 WD 反向计时 → "视为变更"标记。合同参数（天数、模板）site 级可配，因为 GC 自拟条款很常见。
2. **Daywork 记录含"拒签"事件**：`daywork_record{crew_n, hours, plant, area, signed, refusal_note}`，当天 PDF "for record purposes"。主包被明确教导"别签"，让拒签本身成为记录。
3. **延误通知**：`delay_event` 同一套时钟（EOT 也是 5 WD）。
4. **月度 substantiation 附件**：本月三类事件 × 状态，附在 payment claim 后。这是分包每月固定的付费理由。
5. **三问 PTT** 替代收工电话（§3）。
6. **自述式记录**是唯一过隐私关的形态："record what you were told, in your own words"；`from_person` 永不进声纹库。
7. 给 GC 的日报走**邮件**不走 API，格式对齐 Procore / Raken，原件留在分包。
8. Backcharge 防御：离场时"照片 + 5 秒语音"，不需要 LLM。
9. PC 日自动打包全项目档案。
10. 定价：NZ$249–399/公司/月（≤5 活跃工地），工头不限，办公室免费。锚点是"一次没收回来的小变更"。

### 开发商段
1. **owner's rep 当录音者**：不改产品，改销售对象。给 2–3 家 Christchurch client-side PM 事务所站点账号。
2. **业主周报 = 一页 delta 表**：`did / next / changed / needs your decision`，最后一段带 quote 和音频锚点。周五自动起草、site manager 点发。
3. **决策账本**：每条业主待决项的首次出现 / 最近提及 / 提及次数 / 引语 / 谁在等。不算钱。
4. **反 watermelon 两行**："未被提及的 programme 任务"和"连续 ≥3 周未关闭"。只给事实，不打分。**这两行会让 GC 犹豫**——所以要有卡 5。
5. **两本账**：`audience` 标签（`internal` 默认；`owner` 需勾选或按 domain 白名单），发布即快照，业主可导出。安全事件、分包纠纷、成本讨论默认不进业主视图。不能全自动发。
6. **Draw-calendar Evidence Pack**：站点设 `draw_dates[]`，draw 前 3 天出 PDF（programme 匹配 + 照片 + inspection 记录 + hash）。先找非银 lender。
7. **since-last-PCG diff**：Mastt 用户明说缺 point-in-time comparison。
8. 捕获时 geo + 时间戳 + hash，作为 NZ 非银 lender 的远程验收标准。
9. Progress claim 行 × 佐证覆盖：不算金额、不判百分比。
10. 付费顺序：owner's rep → 开发商 → GC。没有证据显示 GC 会为"让业主看见"付钱。

### 明确不做（两段交集）
通话录音 / 录 GC 员工 / 转写 GC 的话；任何 cost / budget / EOT 模块；CCA 合规的 payment claim 文书；留置金追讨；AI 判定 percent complete 或 watermelon 评分；全自动向业主发布；预售买家更新；GC 信用评分对外发布；10 年档案托管承诺；先卖银行 panel QS。

---

## 5. 对现有三条线的影响

| 线 | 影响 |
|---|---|
| **Track B** | 优先级再次被两个段独立确认。新增两个验收用例：`instruction_received` 的时钟状态要跨重抽取保留；业主发布快照要能引用稳定 id。 |
| **Track L** | 分包的 daywork / backcharge 记录都按区域；开发商周报按楼层排。位置维度对两个段都是必需。 |
| **Track S（待写）** | 第一轮给了三个产出面（delta 周报、沉默日、会前简报卡），这轮加两个：分包的"沉默账本"（N/M/K）和开发商的"反 watermelon 两行"。 |
| **抽取 schema** | 三个新事件类型：`instruction_received`、`daywork_record`、`delay_event`。都是 findings 的兄弟，走同一条稳定身份。加 `audience` 标签。 |
| **新的纯模块** | `contract_clock.py`：给定事件日期 + 合同参数（天数、工作日历）→ 到期日、剩余天数、状态。和 `nz_time` 一样是纯函数，可单测。 |

---

## 6. 下一步验证（两段合并，按便宜排）

| # | 验证什么 | 怎么做 | 成本 |
|---|---|---|---|
| 1 | NZ 分包工头怎么汇报 + 合同时限谁在盯 | 5 通电话（2 电工、1 HVAC、1 脚手架、1 幕墙的老板或合同经理），四个问题 | 5 个电话 |
| 2 | client-side PM 事务所愿不愿用 PTT 巡场 | 3 家 Christchurch 事务所各 15 分钟 | 3 个电话 |
| 3 | 非银 lender 收不收带 hash 的照片包 | 1 封邮件附样例页给 ASAP / Cressida 类 | 半天 |
| 4 | 业主周报哪些行 GC 不愿意业主看 | TEST 站点真实数据出 3 周四段表，给 1 个 site manager 看 | 1 天 |
| 5 | "I was told to…" 按钮工头肯不肯按 | App 加按钮 + 次日邮件草稿，2 个工头用一周 | 2 天 |
| 6 | 月度 N/M/K 状态表老板看不看 | 现有月度邮件加一段 fake-door | 半天 |

前三条都是电话和邮件，不写代码。

---

## 7. 没找到答案的（两段合并，下轮别重搜）

- NZ 分包工头第一人称的汇报习惯；NZ 开发商对 contractor 报告的原话。**都只能靠电话。**
- NZ 工地"禁止录音"的 site rules 文本；分包录 GC 员工引发摩擦的真实故事。
- BDT / BuildLaw 里"口头变更 + 分包 + 日记证据"的 NZ 裁决——判例页可达但要人工翻。
- NZIQS《Construction Financing Reports》原文里"QS 可否依赖 contractor 照片"那段——PDF 抓到了但没解析。
- NZ funder's QS 每次到场的费用——无公开数字。
- Hardline / Nora 的定价和"不录音但拦截"的机制。
- HN 对这两个段几乎零命中，下轮不必再搜。

---

## 8. owner 的回答（2026-09-29）

1. 分包段第一个 trade：**HVAC**。
2. owner's rep 事务所这条路：**走**。
3. `audience` 由 site manager 发布前复核：**接受**。
4. 三个新事件类型：**按倾向**——Track B 只预留 `findings.kind` / `findings.payload` 和各 item 表的 `audience`，字段等电话验证后再定。已写进 Track B 计划 Task 1。

## 9. 原始问题（保留）

1. **分包段的第一个 trade 选哪个？** 脚手架合同最简单、纠纷最时间化，但证据最弱；电工/HVAC 证据强、公司多。建议电话先打两个 trade 各两家，再定。
2. **owner's rep 事务所这条路走不走？** 它是开发商段最短的路（现有产品、新买家、不依赖 GC），但意味着和你现有 GC 客户的 PM 可能坐在同一张 PCG 桌上，两边都用 FieldSight。这是好事还是冲突，你比我清楚。
3. **`audience` 标签谁来打？** 抽取时 AI 打候选、site manager 发布前复核——这是唯一不会出事的设计，但它给 site manager 加了一步。接受吗？
4. **三个新事件类型要不要现在进 Track B 的 schema？** 加进去 B 的范围变大；不加，分包段要等 B 之后再起一个 migration。我倾向于**现在只把 `audience` 和事件的 `kind` 列留出来**，三个类型的字段等电话验证后再定。
