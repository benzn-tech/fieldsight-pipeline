# 智库首轮 · 切片 B：施工现场文档 / 项目软件——从用户这一侧看

**日期**：2026-09-28 · **产出者**：thinktank agent，切片 B。
**范围**：Procore、Fieldwire、Raken、CompanyCam、Buildertrend、OpenSpace、Autodesk Build/ACC、Bluebeam、SafetyCulture、HammerTech、Dalux、Sitemate/Dashpivot、NZ 本地工具（NextMinute/Buildxact/Tradify/Builder Base/Site App Pro），加中文 施工日志 / 监理日志 / 安全巡检 语料。
**取证方式与局限**：本会话出口代理拦截了 reddit.com、old.reddit.com、apps.apple.com、capterra.com、g2.com、zhihu.com、procore community、buildingdisputestribunal.co.nz、qs4nz、trueworks 等几乎所有一手页面。**下文所有引用均通过 WebSearch 摘要读取**。Reddit 帖子一条也没能直接读到。
**合成版**见 `2026-09-28-round1-synthesis.md`。

---

### 1. 日报的真正买家是"未来的争议"，不是今天的老板 → 卖 **Evidence Pack**，不卖日报
- **证据**：https://www.trueworks.co.nz/post/extension-of-time-not-time-barred-nzs-3910-nz — "The single biggest predictor of a successful extension of time claim is contemporaneous documentation created before anyone knew there would be a dispute… a clean contemporaneous record — site diaries, dated photographs, the instruction or RFI that triggered the delay, programme impact shown against the baseline — is in a different league to a late claim reconstructed from memory months later."（NZ 合同顾问，摘要）；https://buildingdisputestribunal.co.nz/what-adjudicators-actually-want-from-expert-evidence-in-construction-delay-claims/ — "Poor record keeping and the habit of deferring EOT claims mean experts are frequently called upon to reconstruct events retrospectively from incomplete information."（NZ 裁决机构，摘要）；https://www.on-sitemag.com/features/cover-your-assets-the-power-of-contemporaneous-documentation/ — "reports that appear to have been created after the fact… can introduce doubt"；https://www.gatherinsights.com/en/site-diary — "Instructions given verbally at the workface have a habit of being disputed at final account. A diary entry made the same day is often the only record that the instruction existed."
- **用户在解决什么**：EOT / 变更 / 口头指令 / backcharge 时，需要在几个月后证明"那天谁说了什么、谁在场、为什么停"。写日报的人和用日报的人不是同一个人。
- **我们为什么需要它**：FieldSight 现有资产（quote 级证据 + 音频锚点 + 逐句核验 + 跨天 thread）在市面上**没人有**。完全符合 owner 定位：碎片便宜收，意义在争议发生时才需要浮现。
- **已有的实现方式**：Procore/Autodesk/Raken 导出 PDF 日志；律师事后从 email/短信/照片"重建时间线"；QS 顾问按小时收费重建。没人从**录音碎片**出发。
- **我们怎么做**：`evidence_pack` 导出：输入一个 thread / programme_task / 日期范围 + 关键词，输出 PDF/ZIP：按绝对时间排序的 quotes（含 speaker、evidence_status）、音频切片、±2 min 照片、天气、programme 基线对比。**前置条件是稳定事件身份**。prod 先开 `EMIT_EVIDENCE`。
- **做不到 / 不该做**：不承诺"法庭可采"，只说"contemporaneous, timestamped, unaltered"。不做 delay analysis。
- **12 个月内可卖？**：**是**——对象是有索赔暴露的分包商和中型 GC 的 commercial manager；按 pack 或按项目定价。
- **信心**：高（需求）；中（site manager 会不会为此付钱）。

### 2. 分包商在 GC 的 Procore 里填的日志**不属于分包商** → 做分包商自己的私有记录，单向推给 GC
- **证据**：https://logloon.com/blog/construction-foreman-daily-report/ — "Logging into the GC's Procore platform to report your day to the GC doesn't give you your own record—the log lives in the GC's project, not yours, and when you need it for a dispute, you're pulling records from a system you don't control."；现状："the foreman finish at 5 PM, call or text the PM with what happened, and the PM writing up the daily report to the GC before end of day."；https://planajob.com/blog/software-for-managing-subcontractors-reddit — r/Construction 汇总："Procore is geared towards generals not subcontractors"；Fieldwire："The per-seat model compounds quickly once subcontractors and clients need drawing or task access."
- **用户在解决什么**：分包 foreman 每天 5 点给 PM 打电话口述，PM 再敲进 GC 的系统；分包自己没有留底。
- **我们为什么需要它**：FieldSight 的输入形态（PTT 碎片）**就是那个 5 点的电话**。分包不受 GC 平台选择的绑架。
- **已有的实现方式**：LogLoon、Raken Specialty Contractor 模式。都是表单，不是对话。
- **我们怎么做**：定位文案"你的记录，你的账户"；日报导出对齐 GC 常见要求（manpower/equipment/work performed/delays）；Procore 单向推送作为付费选项。
- **做不到 / 不该做**：不做双向同步；不做 GC 的全项目管理。
- **12 个月内可卖？**：**是**——NZ 分包（电工、管道、幕墙、脚手架）10–60 人规模。
- **信心**：中高。

### 3. "今天什么都没做，施工日志怎么写？" → 把**沉默日**当一等事件
- **证据**：https://www.zhihu.com/question/579597362 — 问题标题即"今天什么都没做施工日志怎么写?"；高赞回答："写清楚为什么没干活、是谁的原因、多少人休息、多少设备闲置……为后期索赔也要有前瞻性"；"一个项目因甲方原因停工一年，使用施工日记汇总损失金额和事项"；NZ 侧 https://qs4nz.co.nz/managing-delay-claims-in-nz-construction-projects/ — "Whenever there is a delay event, 'real-time' documenting of the cause and effect of the delay will help determine whether the contractor is entitled to an EOT"。
- **用户在解决什么**：停工/等料/等图的日子，恰恰是将来 EOT 最值钱的日子，却是最不想写日志的日子。
- **我们为什么需要它**：这就是 owner 说的"silence"维度的具体产品化——**没有录音本身是信号**。
- **已有的实现方式**：Procore "Missing Companies" 横幅；没人对"整站没记录"报警并引导记录原因。
- **我们怎么做**：站点 X 在工作日 NZ 14:00 前无任何 recording → 给 site manager 推一条 10 秒 PTT 提示："今天没有记录。是没开工、还是忘了？说一句原因"；答复进入 `findings` 域 `delay`，带 `cause_party`（principal/contractor/weather/none）；周报单列"空白日 + 原因"。
- **做不到 / 不该做**：不能变成考勤——提示只对 site manager，措辞关于"站点"而非"人"。
- **12 个月内可卖？**：**是**，作为功能不单卖。
- **信心**：中。

### 4. "回忆录式补写"是中外通病 → 把"**无法补写**"做成卖点
- **证据**：https://zhuanlan.zhihu.com/p/465680632 与 https://www.sohu.com/a/896172456_121123724 — "为了迎接检查，把自己关在办公室里写'回忆录'"；"今天已经是六月几日，但施工安全日志的填写还停留在五月份中旬"；https://www.mastt.com/resources/construction-daily-log-template — "courts and arbitrators can detect inconsistent handwriting, identical weather entries, or suspiciously perfect narratives"；今日水印相机自称"3亿人都在用的工作相机"；企业巡检系统卖点"强制现场拍摄，禁止从相册选旧图"。
- **用户在解决什么**：**被迫补写的人**想少挨骂；**读记录的人**想知道这条记录是不是事后编的。
- **我们为什么需要它**：FieldSight 的碎片天然是 contemporaneous 的（设备时间 + S3 写入时间 + VAD 偏移）。**已经有、但从没当卖点说**。
- **我们怎么做**：每个 recording 记 `captured_at`、`received_at`、SHA-256；报告和 pack 脚注"录于 09:41:12，上传于 09:41:40，未被编辑"；人工编辑另存为覆盖层。成本几乎为零。
- **做不到 / 不该做**：不做区块链/公证；不声称"防伪"。
- **12 个月内可卖？**：**是**（差异化文案）。
- **信心**：高。

### 5. "每天都一样，复制昨天的" → 报告只写 **delta**（变化、重复、缺席）
- **证据**：Procore Community — "Is there a way to copy equipment over to the next day on daily log instead of adding all manually?"；Raken Capterra："does not have the option to duplicate your daily activities everyday"；业主代表侧 https://www.tenerapro.com/reports/blog/how-to-write-a-construction-progress-report — "Copy-pasting last period's text with dates changed gets noticed, and it undermines every other section."；Buildertrend："Daily logs can't be templated… copy-and-paste formats saved externally"。
- **用户在解决什么**：写的人想"复制昨天"；读的人一眼识破复制粘贴并因此不信整份报告。两边指向同一个结论：**重复的内容不该是报告的正文**。
- **我们为什么需要它**：owner 说"不要 today was productive 的作文"的用户侧证据。报告默认视图改成三栏：**新出现 / 持续（第 N 天）/ 消失或被解决**。
- **我们怎么做**：`report_sections` 加 `since_yesterday` 段（`SUGGEST_THREADS` 在 prod 还关着——先开）；邮件主题行用 delta 数字（"2 new, 3 recurring, 1 resolved"）。
- **做不到 / 不该做**：合同要求的固定字段（manpower/weather/equipment）仍需完整。
- **12 个月内可卖？**：**是**。
- **信心**：高。

### 6. GC 每天追分包报人数 → site manager 一句 15 秒 PTT 点名
- **证据**：Procore 官方为此做了功能：https://www.procore.com/blog/simplify-tracking-your-daily-logs-with-these-new-features-in-project-management — "Missing Companies" banner；SmartBarrel 卖点 "eliminate manual follow-ups with subcontractors"；HammerTech 评论 "QR codes… change daily so they cannot be printed and left onsite"。
- **我们怎么做**：抽取模板加 `manpower[]`（company, trade, count, note）；只从 site manager 口述抽。
- **做不到 / 不该做**：**不用 voiceprint 推断谁在场**。
- **12 个月内可卖？**：**是**。
- **信心**：中高。

### 7. **威胁卡**：「说一句话就生成日报」在 2025–2026 已被商品化——不要在这上面竞争
- **证据**：Procore Daily Log Agent "aggregates photos, emails, and voice notes to draft daily logs"，但锁在 Premier（"$80,000 to $150,000 annually"）https://contractortoolstack.com/software/procore/ ；Raken AI executive summary；Sitemate Storm；App Store 独立小应用 "Daily Report: Construction AI"（"No account, no typing"）；SiteVoice.ai、Hardline；Buildertrend AI Client Updates "97% faster"。**没有找到任何 super/foreman 对这些 AI 日志的公开负评或好评**。
- **用户在解决什么**：少花 1–2 小时敲字（"Superintendents typically spend at least an hour after the end of their shift… two hours every evening re-typing" https://www.letsbuild.com/blog/construction-daily-reports ）。
- **我们为什么需要它**：**认清**：单日"voice → log"是 2026 年每家都有的 checkbox。FieldSight 的可辩护部分是卡 1/3/5 那种跨天、跨人、带音频锚点的东西。Procore 的 Premier 门槛意味着**年营收 <$50M 的 GC 与全部分包**拿不到——这是 12 个月窗口。
- **我们怎么做**：把"AI 日报"从主卖点降级为"当然有"；主打证据锚点、跨天 recurrence、沉默日、分包私有记录。
- **12 个月内可卖？**：窗口**是**，但仅对 Procore Premier 以下的客户。
- **信心**：高（对商品化）；对用户满意度——**未知**。

### 8. 用户恨 per-seat：**按站点/按录音端计价，阅读者不限量**
- **证据**：SafetyCulture："Per-seat pricing scales quickly, described as the single most common complaint from teams of 20+ users"；CompanyCam："A 20-person company… around $727/mo"；Fieldwire："Add four subcontractors and two clients… $1,024 per month"；Raken："The pricing is also way too high for a daily reporting app"、year-two price hikes；OpenSpace "$2,000 per month per project"；Procore "renewal price increases of 10 percent or more"。
- **用户在解决什么**：想让 PM、QS、业主、分包都能**看**，但每加一个看的人都要付一个座位。
- **我们为什么需要它**：FieldSight 的成本按**录音时长**，不按人。"阅读者无限"是对手结构性做不到的。
- **我们怎么做**：每个活跃录音端 $X/月 + 每站点 $Y/月，阅读/评论/导出免费；公开价格页（Raken/OpenSpace 被抱怨"pricing is a sales call"）。
- **做不到 / 不该做**：别做免费层承诺"无限录音"——ASR 成本线性。
- **12 个月内可卖？**：**是**。
- **信心**：高。

### 9. "上传卡住 / 日志一分钟就丢" → 把**零丢失**做成可见的收据
- **证据**：Raken App Store 1 星："how come your #1 loses my daily log within a minute of using it"；CompanyCam："struggles in low-signal rural areas, with uploads stalling"；HammerTech："internet can be limited, creating difficulty…"；Dashpivot："if more than one person works on the same template, one erases the other"。
- **我们为什么需要它**：BUG-43 亲历过。修是修了，但用户看不到。
- **我们怎么做**：App 一屏 "Today's ledger"：每条碎片 recorded/uploaded/transcribed/in-report 四状态；收工推送"今天 14 条，14 条已入库"。
- **12 个月内可卖？**：**是**（留存）。
- **信心**：高。

### 10. 会议：每周从上周的纸里"重建上下文"、开口项静默丢掉 → **Carry-forward register + 可回听的决策**
- **证据**：https://blog.tasktag.com/construction-meeting-minutes-template-new-2026 — "Each OAC meeting often starts from scratch… spending the first 15 minutes reconstructing context"；"a subcontractor disputes a decision made in a site meeting, with the PM and sub remembering it differently and no signed record"；NZS 3910 实务 — "Verbal instructions still happen on site, but the contract requires written confirmation"。
- **我们怎么做**：`action_items` 稳定 id 后，纪要生成时先拉上次会议的 open items 对照，输出 `carried / closed / new`；每条 decision 附 `offset_sec` 深链到音频。
- **做不到 / 不该做**：会议录音全员知情同意；不做"自动判定谁失约"评分。
- **12 个月内可卖？**：**是**（PM/合同管理者付费意愿高于 site manager）。
- **信心**：高。

### 11. 住宅建商的"周五下午给业主写更新" → 从碎片自动起草**业主周报**
- **证据**：https://buildertrend.com/press-releases/buildertrend-launches-ai-tool-for-97-faster-client-updates/ — "Friday afternoons have long been reserved for client updates… up to an hour per project—multiplied across a dozen active jobs"；早期用户 "6.5 minutes on average compared to 30 to 60 minutes"（**厂商渠道，来源偏向**）；缺点侧 "If clients or subs ignore the portal, its value drops—and you still pay for it"。
- **我们为什么需要它**：**这张卡部分违背 owner 定位**（给业主看的叙事）。但证据强，且 NZ 住宅建商没有等价物。若做，用卡 5 的 delta 结构。
- **做不到 / 不该做**：业主视图必须过滤安全事件、分包纠纷、成本讨论——需"对外可见"标记 + site manager 复核，**不能全自动发送**。
- **12 个月内可卖？**：**取决于**是否进住宅建商细分。
- **信心**：中高。

### 12. 照片没标题、没绑到日志条目 → **"照片 + 5 秒语音说明"作为最小采集单元**
- **证据**：ACC 用户："There is no way to add captions to photos attached to forms"；"Many contractors still store photos in private WhatsApp chats"；律师侧 "A photograph with a date and a one-line caption is worth a paragraph of description"；Raken 用户 "in loud environments, talk to text is not always possible"（所以要**录音而非听写**）。
- **我们为什么需要它**：`topic_photos` ±2 分钟绑定是**推断**。"拍照后立刻说 5 秒"把绑定从推断变成声明。
- **我们怎么做**：App 拍照后自动进入 5 秒 PTT（可跳过）；照片元数据带 recording id；报告照片区用语音转写作 caption。
- **做不到 / 不该做**：不做图像识别。
- **12 个月内可卖？**：**是**。
- **信心**：高。

### 13. 现场在 WhatsApp/Teams 里说话 → 把**群里的语音条**当作碎片来源（Velora 模式）
- **证据**：https://velora.ai/ — "WhatsApp-based construction site reporting is the default operating model for construction sites across India, the Middle East, Southeast Asia…"；https://gobuid.com/en/blog/all-articles/whatsapp-is-not-for-construction-management — "25% of requests never get resolved"（出处未核）；知乎 "微信群太多，信息超载"。
- **我们为什么需要它**：管线对输入来源无感。WhatsApp 语音条 = 天然 PTT 碎片。获客成本从"换习惯"降到"把机器人拉进群"。
- **我们怎么做**：WhatsApp Business Cloud API 或 Teams bot 作为 ingest；语音条落 `users/{name}/audio/{date}/` 沿用触发器。最小版 1–2 周。
- **做不到 / 不该做**：**群成员必须被明确告知并同意**；不读私聊；不读历史。不学 Velora 桌面版的灰色路径。
- **12 个月内可卖？**：**是**，尤其对分包商。
- **信心**：中。

### 14. 安全记录（toolbox / prestart）是 WorkSafe 必看、又最形式主义 → 录下 prestart 本身
- **证据**：https://siteconnect.io/blog/worksafe-nz-inspection-checklist/ — 检查包括 "toolbox talk registers"；https://www.thinksafe.co.nz/toolbox-talk-template/ — "a regular, recorded talk is the simplest evidence that it's happening"；HammerTech 用户 "The daily pre start meetings are difficult and time consuming to use"；知乎 "检查过程只是做做样子"。
- **我们怎么做**：`meeting_type=prestart` 模板：topics、hazards raised、questions、attendee count（口头申报）；PDF register 页。
- **做不到 / 不该做**：出席不靠 voiceprint。
- **12 个月内可卖？**：**取决于**是否接触 H&S 采购人。
- **信心**：中。

### 15. （5 年视野）中国市场：施工日志是**法定竣工资料**，3 亿人用水印相机，无语音碎片产品
- **证据**：施工日志"是工程交竣工验收资料的重要组成部分"；水印相机 + 施工日志 App 免费；智慧工地是政府/国企采购。**没有找到任何中文用户对"语音生成施工日志"的评价**。
- **我们为什么需要它**：只标记空白。合规驱动的市场对"意义在几周后浮现"不买账。
- **12 个月内可卖？**：**否**。
- **信心**：低，speculative。

---

## 值得下一步验证的 5 个
1. **Evidence Pack（卡 1）** — 3 个 NZ QS/claims 顾问 + 2 个建筑律师看一份脱敏样例 pack。1 周。
2. **分包商私有记录（卡 2）+ WhatsApp 入口（卡 13）** — 5 家 NZ 分包 foreman 三个问题。
3. **沉默日追问（卡 3）** — fake-door，只统计点开率和回复率。2 天。
4. **Delta 报告（卡 5）** — 日报邮件主题前缀 "2 new · 3 recurring · 1 resolved"，A/B 两周。
5. **业主周报（卡 11）** — landing page 面向 NZ 住宅建商。

## 明确不做的
- 与 Procore/Autodesk 比日志模板完整度。
- 用 voiceprint 做出席/人数。
- 图像识别（安全帽/进度）。
- 中国市场。
- 双向 Procore 同步。
- Velora 式"监控个人 WhatsApp"。
- "法庭可采"类承诺。

## 没找到答案的问题
- **Reddit 一手帖子零命中**——需在非云会话里读。
- **对 Procore Daily Log Agent / Raken AI / Sitemate Storm 的真实用户评价**——一条都没有。
- **NZ 本地工具的 site diary 差评**——只有厂商对比页。
- **Building Disputes Tribunal 公开裁决中提到 site diary 质量的具体案例**——站点被拦。
- **中文侧"语音生成施工日志"的用户评价**——品类不存在于公开语料。
- **分包商为文档工具付费意愿的定量数据**——没有。
- **业主代表对承包商日报的直接抱怨原话**——没有。
