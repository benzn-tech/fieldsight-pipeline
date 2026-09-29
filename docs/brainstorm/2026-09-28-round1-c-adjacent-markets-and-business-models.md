# 智库首轮 · 切片 C：相邻市场、非记录者买家、商业模式

**日期**：2026-09-28 · **产出者**：thinktank agent（`.claude/agents/thinktank.md`），切片 C。
**方法说明**：本会话出口代理拦截了 hka.com、lawyersweekly.com.au、community.whoop.com、substack、blog.ifma.org、mkt-cdn.procore.com、numericcitizen.me 等域名，下列所有引文均来自 WebSearch 摘要（多次查询交叉），未能读到原文全文；标注"（摘要）"的地方请在引用前复核原页。reddit 未直接命中，用了 Planning Planet、Whoop 官方社区、知乎、Trustpilot/G2 摘要、App Store 评论替代。
**合成版**见 `2026-09-28-round1-synthesis.md`。

**先说结论（三句）**
1. 非记录者买家里，**唯一在 12 个月内能付钱、且与 owner 的"碎片→数周后意义浮现"立场完全一致的，是"合同争议"这条线**：EOT / 变更 / daywork 的败因几乎全是"当时没记、后来补"。FieldSight 的机器生成时间戳天然证明 contemporaneity——这是市面日志 app 做不到的（它们的日期是人填的）。
2. 消费级"周报/月报"产品的用户反馈非常一致：**叙事体废话被骂，趋势+对比+"什么变了"被要**。这正好否定"今天很高效"式日报，支持 owner 的立场，并给出了周报的形状。
3. "Ask your life"（Rewind / Limitless / Bee）的真实使用率极低、且会编造。**Ask 不是 hook，主动推送的结构化 digest 才是**；而 prod 上 `EMIT_EVIDENCE=false` 意味着今天的 prod 输出恰恰没有防编造的 quote 级证据——这是最该先开的开关。

---

### 1. 把"当时记的"做成可证明的商品：Claims-grade Evidence Pack（EOT / 变更 / 争议）
- **证据**：https://www.gatherinsights.com/en/site-diary/how-to-write — "On a major water treatment project, Friday's diary entries were being written on Monday, and by month four, the commercial manager was writing weekly summaries from memory and calling them daily records."（供应商博客，摘要）；https://www.lawyersweekly.com.au/sme-law/44981-adjudication-is-won-on-site-not-in-the-registry — "The strongest adjudication applications were written months earlier, in signed site diaries, contemporaneous delay notices and disciplined progress reporting."（律师专栏，摘要）；https://www.mosaicprojects.com.au/PDF-Gen/White_Constructions_Pty_Ltd-v-PBS_Holdings_Pty_Ltd.pdf — NSWSC 2019 法官拒绝双方的延误分析方法，回到"当时的进度记录（含 site diary）"看因果（判决）；https://zhuanlan.zhihu.com/p/34242958 — 施工/监理日志"在办公室写'回忆录'应付检查……有的日志落后施工日期几个月""如果项目不严，监理每周抄一遍施工日志换个视角"（知乎，摘要）。
- **用户在解决什么**：commercial manager / QS / 索赔顾问在争议发生**之后**去翻记录，发现记录要么没有、要么明显是事后补的；补写的记录在 adjudication 里权重极低。谁都知道要"当天记"，但没人做到，因为记的动作发生在下班后。
- **我们为什么需要它**：这是 FieldSight 现有产出（PTT 碎片 + 文件名基准时间 + VAD 偏移 + 词级时间 → 绝对时间）**已经具备而竞品没有**的属性：contemporaneity 是机器写的，不是人填的。它把"碎片式记录"从"懒人的折衷"变成"证据学上更强的做法"，直接服务 owner 的立场。买家可以是 GC 的 commercial manager，也可以是被 GC 拖欠的分包（见 #2）。
- **已有的实现方式**：Gather（UK，£59/user/月起，per-project 可谈）把 site diary 和 NEC4 compensation events 绑在一起卖；Site-chronicle、Surtori 都在博客里教"如何记才能打赢索赔"，但产品本身仍是"人填表"。索赔顾问（HKA、Driver Trett）不卖工具，收咨询费。
- **我们怎么做**：① 一个"Evidence Pack" 导出：按日期范围 / 按 thread（跨天同一话题）把 topics + findings + quote + 音频锚点 + 照片 ±2min 绑定打成一个 PDF/ZIP，附每条的 capture-time 与 hash（见 #7）；② 在周报里加一个固定栏目"**本周有指令/变更但无书面确认的话题**"（Silence 的一种）——这是索赔顾问最想看的；③ 先对 5 个 NZ 索赔顾问 / QS 打电话问"你们接案时最缺什么形状的记录"。
- **做不到 / 不该做**：不要宣称"法庭可采"——AI 转写的 admissibility 正在被重新立规（见 #7）；卖的是"原始音频 + 机器时间戳 + 逐句 quote"的**可核验性**，不是 AI 摘要本身。
- **12 个月内可卖？**：是——作为现有 GC 客户的加购（每工地/月）或"争议发生时一次性导出"付费。
- **信心**：高（"没记录就没索赔"是全行业反复说的话；"补写"是全球现象，知乎、UK 博客、澳洲律师都在说）。

### 2. 分包侧的"口头指令账本"：Verbal Instruction Ledger for Subbies
- **证据**：https://planningplanet.com/forums/forensic-claims-analysis/414812/signed-recordsdayworks-main-contractor-rep — 主承包代表拒签分包 daywork 单；回帖引 *JDM Accord v DEFRA (TCC 2004)*："If the Client can't prove the Contractor's records are inaccurate, the Contractor's records will stand and payment must be made."（论坛，摘要）；https://www.sitesamurai.co.uk/resources/get-paid-on-time/what-is-daywork — "daywork without an instruction is not a variation — it is unpaid work. The instruction creates the entitlement; the sheet only prices it."；https://www.streetwisesubbie.com/daywork/ — "Sheets submitted days later are harder to get signed without dispute."（供应商，摘要）。
- **用户在解决什么**：分包工头在工作面收到 GC 的口头指令，干了，月底 GC 的 QS 说"没这回事"。分包没有 PM，没有人下班后写 email 确认。
- **我们为什么需要它**：这是与"GC 站长"不同的第二个付费者，而且**付费动机比 GC 更强**（GC 是被动防御，分包是主动追款）。同一条流水线，只是 prompt 里多一个 claim_type = "instruction received"。
- **已有的实现方式**：Site Samurai / StreetwiseSubbie 卖模板和培训；没有一个产品把"我当时在工作面听到的指令"变成带时间戳的记录。
- **我们怎么做**：PTT 一句"Sam from [GC] just told me to move the penetration 300 west, doing it now" → 抽取为 `instruction_received{from, what, time}` → 当天自动生成一封"for record purposes only"确认邮件草稿给 GC（复用 session confirmation email）。周报栏目："本周收到 N 条口头指令，M 条未获书面确认"。
- **做不到 / 不该做**：录到 GC 员工的声音 → NZ Privacy Act 下"知情"问题；产品上要求 subbie 当面说"我记一下"，且不做 GC 方 speaker identity。不要把它包装成"对付 GC 的武器"——会毁掉 GC 侧的销售。
- **12 个月内可卖？**：是，取决于 <owner 是否愿意开第二个客户段>；单价低但数量大（NZ 分包数量是 GC 的 10 倍以上）。
- **信心**：中高（Planning Planet 是真实争议；但"分包愿意每月付 NZ$XX"没有直接证据）。

### 3. 给 Funder's QS 的进度证据流：Bank Draw Verification Feed
- **证据**：https://www.nziqs.co.nz/Common/Uploaded%20files/Membership%20Forms%20and%20Documents/Construction%20Financing%20Reportsv2_October%202019.pdf — NZIQS 指南要求 funder's QS 每期"undertake a site inspection, review the contractor's progress claim, assess the programme in relation to works completed on-site"（协会指南，摘要）；https://www.truepic.com/blog/draw-inspection — "lenders fund verified progress, not reported progress"；上门检查"expensive, time-consuming, difficult to schedule"（供应商，摘要）；https://truescreen.io/articles/construction-draw-inspections-legally-valid-photo-reports/ — 手机照片 EXIF 可改，需要"SHA-256 hash at capture, a qualified timestamp, and a chain of custody log"（供应商，摘要）。
- **用户在解决什么**：银行 QS 每月跑一趟工地只为确认"进度是否与 claim 一致"，费用由开发商承担；NZ 一次 QS 报告数千纽币。
- **我们为什么需要它**：FieldSight 已经有 programme 匹配（`programme_progress_suggestions`）+ 照片时间绑定 + 位置标记。把"本月哪些 programme task 有现场语音+照片佐证其完成"打成一页给 QS，是**现有数据的重新切片**，不是新功能。付费者是开发商（想少付 QS 出场费）或 QS 事务所（想远程做）。
- **已有的实现方式**：Truepic Vision（美国，controlled capture，中位 24h 周转，价格不公开）；Cotality 2026-06 合作。NZ 没有对应产品；Zyte 做的是 council 远程验收，不是 funder。
- **我们怎么做**：月度"Progress Evidence Sheet"：按 programme task 列 → 佐证碎片数 / 最近一条时间 / 照片缩略图 / hash。先找 2 家 Christchurch 的 bank QS 事务所问"如果开发商给你这个，你会少去几次现场"。
- **做不到 / 不该做**：不能替代 QS 的专业判断和责任；不要做"AI 判定完成百分比"——那是 QS 的签字责任，也是自报置信度失效的重演。
- **12 个月内可卖？**：取决于 <能否拿到 1 家 QS 事务所试用>；QS 行业保守，直接卖给开发商更快。
- **信心**：中（QS 的痛是真的，但"愿意信一个新工具的照片"没有 NZ 直接证据）。

### 4. 保险费折让（Insurer Premium Credit）—— 方向对，**现在不该做**
- **证据**：https://www.enr.com/articles/63217-insurers-offer-discounts-for-using-site-monitoring-tech-to-reduce-risk — Shepherd × Brickeye（2026-06）：IoT 水损监测 → deductible 降 50%+、premium credits；Brickeye CEO："insurers are no longer satisfied with general statements about risk management … want to understand what controls are actually in place on the jobsite"（行业媒体，摘要）；https://buildlogapp.com/blog/construction-documentation-insurance-claims.html — "when a contractor submits a claim with documentation that was clearly reconstructed after the fact, the adjuster flags it immediately."（供应商博客，摘要）。
- **用户在解决什么**：承包商想降 builder's risk / public liability 保费；保险公司想要"可核验的控制措施"而不是一页 H&S policy。
- **我们为什么需要它**：五年愿景里，"每天都有机器时间戳的现场记录"是 underwriter 想要的信号。但今天所有折让案例都是**传感器（水、混凝土成熟度）**，没有一例是"人的口头记录"。
- **已有的实现方式**：Shepherd（美国 MGA）；Brickeye、LumiCon 传感器。NZ 建筑险市场小、由几家大保险商+经纪人主导，没有类似程序。
- **我们怎么做**：不做产品；只在 Evidence Pack（#1）里保留"incident / near-miss" claim_type，一年后拿真实数据去和一个 NZ 经纪人聊。
- **做不到 / 不该做**：一人团队搞不动保险精算合作；NZ 没有 Shepherd 式的科技友好 MGA；语音记录对 underwriter 的价值未被任何人证明。
- **12 个月内可卖？**：否（标注：5 年愿景）。
- **信心**：低（对"保险商会为语音记录付费/折让"）。

### 5. WorkSafe 可辩护性：语音 Pre-start / Toolbox Talk 记录
- **证据**：https://www.worksafe.govt.nz/dmsdocument/56808-worksafe-new-zealand-v-rs-construction-limited/latest — 判决摘要指出事故当日"the workers did not have a toolbox meeting"（判决，摘要）；https://sitemate.com/safety/toolbox-talk-app/ — "Paper attendance records can disappear or get damaged, making it impossible to prove who attended safety briefings during compliance audits."（供应商，摘要）；Duncan Cotterill insights — WorkSafe 可正式要求提供"records relating to training, incidents, equipment maintenance, and contractor management"（律所，摘要）。
- **用户在解决什么**：站长每天早上口头讲了 pre-start，但纸质签到表在车里；出事后拿不出"那天讲了什么、谁在"。
- **我们为什么需要它**：这是 FieldSight 现有碎片里**已经存在**的一类（早晨 PTT），只差一个 claim_type 和一个"attendance"字段；且它是 GC 采购决策者（H&S manager）听得懂的价值。
- **已有的实现方式**：HazardCo、SiteSafe、Sitemate、SafetyCulture——全是表单+QR 签到，很拥挤；没有一个从自然语音抽"讨论了哪些 hazard"。
- **我们怎么做**：pre-start 的 PTT 片段 → 抽取 `hazards_discussed[]`、`controls[]`、`attendees_named[]`（只用说话人**自己念出**的名字，不做 voiceprint 点名）→ 周报栏目"本周 pre-start 覆盖天数 / 未覆盖天数"（Silence）。
- **做不到 / 不该做**：不要用 voiceprint 做出勤识别——那是把工人变成 surveillance target，违反隐私底线；只记"站长说了谁在"。不要做 hazard 库/法规映射——HazardCo 已经做了十年。
- **12 个月内可卖？**：是，作为现有客户的免费栏目（提高留存），不单独收费。
- **信心**：中（"要能证明"是真痛点；"愿意为此换工具"证据弱，因为 HazardCo 已经在用）。

### 6. 交付后的"决策轨迹"给 FM / 业主：Handover Decision Trail
- **证据**：https://blog.ifma.org/construction-handover-the-step-that-can-make-or-break-facility-operations — "One of the most common breakdowns during construction handover is incomplete, inconsistent or inaccessible documentation"（协会博客，摘要）；https://facilitiesmanagementadvisor.com/maintenance-and-operations/when-documentation-fails-facility-operations-pay-the-price/ — "The biggest impact is not always the repair itself, but the delay caused by trying to locate information that should have been readily available."；https://www.arcfacilities.com/blog/construction-to-operations-handover-best-practices-guide — FM 拿到"a flash drive, folder of PDFs, and native BIM files they cannot open"（供应商，摘要）。
- **用户在解决什么**：楼交付两年后，FM 发现管线不在图上的位置，没人知道为什么改；as-built 没反映现场变更。
- **我们为什么需要它**：FieldSight 的 `decisions` + `day_location_markers` + 照片，正是"为什么在这里改了"的原始来源。业主/FM 是**不在工地、但为记录付钱**的最纯粹买家。
- **已有的实现方式**：ARC Facilities、Oxmaint 卖 handover 数字化；全是"整理 GC 交来的 PDF"，没有原始现场决策。
- **我们怎么做**：**依赖稳定事件身份（Track B）**——决策今天是 topics 里的 jsonb，无稳定 id，无法在 18 个月后被引用。先把 decision 变成有 id 的行，再做"按位置 / 按 programme task 导出全部决策 + 引用"。
- **做不到 / 不该做**：不做 BIM/IFC 关联；不做 O&M 手册。
- **12 个月内可卖？**：否（依赖稳定事件身份 + 需要一个已交付项目做样板）；标注 2–3 年。
- **信心**：中（FM 的痛是行业共识；"FM 会为 GC 的语音记录付费"是推断）。

### 7. Capture-time 证明 + 长期保管：Evidence Vault 定价
- **证据**：https://theconstructionadrtoolbox.com/2026/04/guide-to-evaluating-ai-generated-evidence/ — 建筑 ADR 圈已在讨论"counsel needing to ask about AI tools used on projects at the beginning of cases"；https://truescreen.io/articles/construction-draw-inspections-legally-valid-photo-reports/ — 手机照片"fail the self-authentication pathway of FRE 902(14) because their EXIF metadata and timestamps are editable"（供应商，摘要）；Autodesk KB — ACC 订阅结束"projects and accounts become inaccessible … a brief period (for example, 30 days)"后可能删除（厂商 KB，摘要）；https://www.levelset.com/blog/construction-document-retention/ — 索赔几乎都在 substantial completion 后 10 年内（NZ Building Act 同为 10 年）。
- **用户在解决什么**：项目结束后不想继续付每月订阅，但 10 年内可能要用这些记录；同时争议对方会质疑"这是 AI 生成的"。
- **我们为什么需要它**：①差异化：把"我们的时间戳是机器在采集时写的、原始音频保留、hash 可验"做成明示功能；②商业模式：**活跃工地按月收费，交付后按"保管年"收一笔小钱**（archive tier），这是 Buildertrend/Procore 都没有的价格形状，且和"记录的价值在数周/数年后浮现"的立场一致。
- **已有的实现方式**：Truepic controlled capture（照片）；Kraaft、CompanyCam 宣传"timestamped photos"但不做 hash；没有人卖"交付后保管"。
- **我们怎么做**：S3 上传时记录 `sha256 + server receive time` 到 Aurora（对象元数据已有一半）；Evidence Pack 附 manifest；S3 Glacier Deep Archive 每 GB 每年 ≈ NZ$0.02，"每工地每年 NZ$99 保管"毛利极高。
- **做不到 / 不该做**：不做区块链、不做"法院认证"营销；不承诺 admissibility。
- **12 个月内可卖？**：是（保管费在第一个项目交付时就能收）。
- **信心**：中高（数据被"劫持"是普遍抱怨；"愿为保管付费"是推断，但金额小、阻力小）。

### 8. 相邻垂直：房屋检测 / 损失理算（Home Inspection, Loss Adjusting）—— **不做**
- **证据**：https://inspectordata.com/blog/homegauge-vs-spectora-comparison.html — 检测员"10 different complaints—usually about per-report fees, pricing tiers that hide what you actually pay"（竞品博客，摘要）；https://zackproser.com/blog/ai-voice-tools-for-home-inspectors — Spectora 2026-06 推出 Report Assist（说+拍→报告）；https://fieldnotesai.com/ — 理算师报告"2 to 4 hours per claim"降到"15 to 30 minutes"（供应商，摘要）。
- **用户在解决什么**：一次现场 → 一份报告，当天出。
- **我们为什么需要它**：不需要。形状不同：它是**单次会话→单份文档**，FieldSight 的价值在**多周纵向**。而且 Spectora（美国市占最高）已经内建了。
- **已有的实现方式**：Spectora Report Assist、SwiftReporter、PIM（NZ，Siri 听写）、FieldScribe AI。价格战已开始。
- **我们怎么做**：不做。唯一可借用的是它们的定价抱怨——"per-report 隐藏费用"被骂，印证 #11。
- **做不到 / 不该做**：进入即与 5 个融资过的专用产品正面竞争，且没有纵向优势。
- **12 个月内可卖？**：否。
- **信心**：高（对"不该做"）。

### 9. 相邻垂直：交接班（Shift Handover：矿业 / 公用事业 / 维护巡检）—— 形状最像，市场最难
- **证据**：https://ifactoryapp.com/shift-logbook/shift-handover-employee-turnover — "the feeling of walking into a situation you don't understand, being held accountable for problems you didn't cause, and having no reliable way to communicate what you observed to the next crew"；"average frontline worker spends 32 minutes per shift just searching for information they should have received at handover"（供应商博客，摘要）；https://www.fulcrumapp.com/blog/ai-powered-inspections-the-future-of-td-fieldwork/ — 巡检记录"vary by crew and location … engineering teams spend time validating reports rather than acting on them"（供应商，摘要）。
- **用户在解决什么**：接班的人不知道上一班发生了什么；同一设备的问题在几周里反复出现却没人串起来。
- **我们为什么需要它**：这是 FieldSight "threads across days + recurrence + silence" 的**教科书场景**——比建筑更纯粹。值得记下来作为定位的旁证。
- **已有的实现方式**：SafetyCulture 模板、iFactory、MicroMain（CMMS）——全是表单；Fulcrum Audio FastFill 是语音填表单，不做跨班关联。
- **我们怎么做**：12 个月内不做矿业（enterprise、IT 安全审查、一人团队打不动）。可考虑的近亲：**NZ 的 FM/维护承包商巡检**（同一栋楼每周巡一次，记录碎片，问题反复出现）——同城、小公司、同样用手机。先问 3 家 Christchurch FM 承包商。
- **做不到 / 不该做**：矿业/公用事业的采购周期 12–24 个月，且要求本地部署。
- **12 个月内可卖？**：矿业否；FM 巡检取决于 <3 个电话>。
- **信心**：中（痛点真实且吻合；"付费者是谁"不清）。

### 10. 相邻垂直：养老护理记录（Aged Care Progress Notes）—— **不做**
- **证据**：https://www.jmir.org/2026/1/e86078/ — 德国长期护理时间动作研究：护士文档时间占工作 1/3，AI 语音助手显著降低（学术）；https://www.mediqo.health/resources/blog/reducing-administrative-burden-in-aged-care — "consuming up to three hours per shift for registered nurses"（供应商，摘要）。
- **用户在解决什么**：护士每班 3 小时打字。
- **我们为什么需要它**：痛点最大、且有"数周纵向"（同一住民的状态变化），但**隐私/健康数据监管**（NZ Health Information Privacy Code）与 FieldSight 的隐私底线和团队规模不匹配。
- **已有的实现方式**：多家医疗 AI scribe；在 NZ 需 HISO 合规。
- **我们怎么做**：不做。
- **做不到 / 不该做**：健康信息 + 弱势人群 + 一人团队 = 不该。
- **12 个月内可卖？**：否。
- **信心**：高（对"不该做"）。

### 11. 定价形状：每工地包月、记录者无限、工人免费；绝不按 seat
- **证据**：https://www.scanmanifold.com/blog-posts/companycam-pricing-2026-complete-guide-f0fd7 — "The 3-user minimum is the number one reason small contractors look for alternatives … forced to pay for users that don't exist"；G2 用户："We're now paying almost $500/month just for photo management."（摘要）；https://projul.com/blog/buildertrend-pricing-analysis-2026/ — ContractorTalk 用户报告 65% 涨价，"not your friend unless your volume exceeds two million dollars"；https://www.workyard.com/compare/raken-review — Raken "pricing is a sales call, not a price page"，$15–46/user/月；https://www.getmonetizely.com/articles/construction-software-pricing-project-based-vs-subscription-models — Procore per-project "hurts small companies that might have one or two projects but need every worker on a license"；https://www.toolvetting.com/fathom-ai-review/ — Fathom 免费版"unlimited … no minute counter, no storage meter, no trial clock … the single fact that explains Fathom's growth"（摘要）。
- **用户在解决什么**：小承包商每加一个人就多一笔钱，于是只给 2 个人账号，其他人不记——记录的覆盖率被定价杀死。
- **我们为什么需要它**：FieldSight 的价值随**碎片数量**上升，任何按 seat / 按分钟的定价都在惩罚自己的价值来源。Otter/Fireflies 的"AI credits 让人无法预测何时撞限"（https://opentools.ai/resources/fireflies-vs-otter）是同一错误。
- **已有的实现方式**：CompanyCam 2026 新增 $63 单人 Core 档（承认错误）；Fathom PLG（个人免费无限，团队收费）；Procore per-project $375+/月。
- **我们怎么做**：价目公开：**每活跃工地 NZ$X/月，记录者不限，工人免费**；工地交付后转 #7 的保管档。个人 site manager 免费无限录（Fathom 模式）→ 公司为周报/Evidence Pack/多工地视图付费。变动成本（ASR + LLM）按 CLAUDE.md 的实测足够低，可以承受"无限"。
- **做不到 / 不该做**：不按分钟计费；不设"3 人最低"；不把导出锁在高档位（inspectordata 的抱怨点）。
- **12 个月内可卖？**：是。
- **信心**：高（多个独立来源、同一抱怨）。

### 12. 硬件 + 订阅：**不卖硬件**
- **证据**：https://www.bluedothq.com/blog/plaud-review — "the most divisive topic among Plaud AI users … frustrated by the idea of buying a $159 device and then requiring a paid subscription"；免费 300 分钟"you won't get far"（评测，摘要）；https://bigguyonstuff.com/ai-wearables-2026-honest-review/ — Bee 被 Amazon 收购后免费；Limitless 被 Meta 收购后停售（https://moelueker.com/blog/limitless-ai-pendant-review-5-use-cases-worth-199）。
- **用户在解决什么**：想要一个"按一下就记"的物理按钮，又不想为设备+订阅双重付费。
- **我们为什么需要它**：owner 提过硬件兴趣。证据说：独立 AI 录音硬件公司在 18 个月内**全部**被收购或转免费，硬件本身没有护城河；用户接受的是"设备是订阅的赠品"。
- **已有的实现方式**：Plaud（$159 + $99–239/年）；RealPTT 设备已在 FieldSight 管线里。
- **我们怎么做**：继续 BYO（RealPTT / 手机）；若要送设备，作为年付赠品，不单独定价。
- **做不到 / 不该做**：不做自研硬件；不为硬件做库存。
- **12 个月内可卖？**：（此卡是"不做"）。
- **信心**：高。

### 13. 周报的形状：数字 + 差值 + 命名的重复 + "什么变了"，**不是散文**
- **证据**：https://cheekyrc.substack.com/p/product-review-oura-ring-pt-2 — Oura 月报"exactly 0 useful information … 'your resilience charted a unique course over time' and 'your stress and recovery levels moved with the beat of your life'"（博客，摘要）；https://www.community.whoop.com/t/new-month-in-review-is-a-huge-disappointment/9035 — 长期会员：新版 Month in Review "doesn't provide the same amount of information and insight as the legacy Monthly Performance Assessment did, which was a valuable tool for tracking trends and identifying areas for improvement"（官方社区，2025-10，摘要）；https://candlelightconversations.substack.com/p/weekly-spark-223-my-experience-with — "Data is just noise without insight and actions"，无法调整日程的人只用得上一个指标（博客，摘要）；https://ouraring.com/blog/trends/ — 被赞的是 Trends 视图，"lines not dots"。
- **用户在解决什么**：想知道"我这周和上周比怎么样、哪个数在往坏走"，结果收到一段温暖的废话。
- **我们为什么需要它**：直接支持 owner 的立场，并给出反面清单——**Whoop 把结构化的 MPA 换成"casual content"被骂**，Oura 的 LLM 叙事被骂。FieldSight 周报每一行都应是：`<话题/thread> — 出现 N 次（上周 M） — 最早/最近 — 状态未变 X 天 — 引用`。"Silence"栏（该出现却没出现的东西）是消费产品没有、而建筑争议最需要的。
- **已有的实现方式**：Whoop WPA（周一推送，含 population 对比）；Oura Trends；Gyroscope 周报（定价被骂）。建筑侧没有任何产品做"周级纵向"。
- **我们怎么做**：把 `lambda_rolling_summary` 的输出改成表格优先、叙事最多两句；每条带 delta 和 quote；对比对象是**同一工地的上周**，不是别的工地（population 对比在建筑里是隐私雷区）。
- **做不到 / 不该做**：不做"本周高效指数"之类合成分数（Oura readiness 式）——自报置信度已被实测失效。
- **12 个月内可卖？**：是——这就是产品本身。
- **信心**：高（跨三个消费产品的一致反馈）。

### 14. "Ask your life" 不是 hook：主动 digest 才是，且必须有 quote 级证据
- **证据**：https://numericcitizen.me/rewinding-30-days-of-my-experience-with-rewind-for-mac/ — 一个月里"did only two searches and asked a few questions … failed to see numerous use cases so far"（博客，摘要）；https://www.layer3labs.io/gear/reviews/limitless-pendant — "The overall organization of accumulated months of recordings needed work"（评测，摘要）；https://www.vice.com/en/article/this-ai-memory-device-records-your-life-and-gets-most-of-it-wrong/ — Bee 把电视剧情当成用户人生事实、编造"路易斯安那的病人"，"has a hard time understanding what is and isn't something worth remembering"（媒体）；https://zackproser.com/granola — 12 个月日用 Granola，留住他的是"type fragments during meetings … steer the generated draft"，价值在"what happens between meetings"（博客，摘要）。
- **用户在解决什么**：录了很多，从来不搜；偶尔搜一次，答案还编。真正被用的是"推到面前的东西"。
- **我们为什么需要它**：①资源分配：Ask agent 不该是增长重点，周报/邮件推送才是；②风险：**prod 上 `EMIT_EVIDENCE=false`**，意味着今天 prod 的每条 topic 都没有 quote 核验——FieldSight 最强的防编造机制在客户看到的版本里是关的。Bee 的翻车就是没有这层。③Granola 的启示：让站长在录的时候说一句"记这个"，比事后搜更有效——PTT 本来就是这个动作。
- **已有的实现方式**：Rewind/Limitless/Bee（都已被收购或停售）；Granola（会议场景，活得最好，靠"边记边引导"）。
- **我们怎么做**：prod 开 `EMIT_EVIDENCE`；周报每条带 quote；Ask 留着但不推。
- **做不到 / 不该做**：不做"全天录音 + 事后问"——与立场、隐私、以及以上三个失败案例都相悖。
- **12 个月内可卖？**：是（是开关和优先级问题，不是新功能）。
- **信心**：高。

### 15. Procore Marketplace 作为渠道：**12 个月内不是增长来源**
- **证据**：https://developers.procore.com/partner — Foundation 档要求"1+ test customer, 1+ monthly active customer, completed app validation, <48-hour partner SLA"；Select 需"100+ monthly active customers, bi-directional data integration"；"agentic APIs for AI agents coming in 2026"（官方，摘要）。费用/分成条款在 Partner Program Guide PDF 里，**本会话无法读取，未找到任何公开的 revenue share 数字**。
- **用户在解决什么**：GC 想让记录进 Procore 的 Daily Log。
- **我们为什么需要它**：只有 NZ 的大 GC 用 Procore；owner 的目标客户（site manager、分包、owner's rep）大多不在里面。2026-09-09 的 Procore spike 卡在两个非技术门，此处再加一条：Marketplace 要先有 1 个付费 Procore 客户才能上架。
- **已有的实现方式**：Raken、CompanyCam 都在 Marketplace；它们仍主要靠直销。
- **我们怎么做**：有第一个 Procore 客户时再做单向推送（FieldSight → Daily Log），不做双向。
- **做不到 / 不该做**：不为上架而上架。
- **12 个月内可卖？**：否（作为渠道）。
- **信心**：中（费用未知是明确的空白）。

---

## 值得下一步验证的 5 个

1. **Evidence Pack（#1）**——最便宜的验证：给 5 个 NZ 索赔顾问 / QS 打电话，问一句"你上次接的案子，如果承包商有每天几条带时间戳的语音+照片，你会怎么用、值多少"。不用写代码。
2. **分包侧口头指令（#2）**——App 里加一个 fake-door 按钮"Record an instruction I received"，看 2 周内点击率；同时问 3 个分包工头。
3. **每工地包月 + 保管档定价（#11 + #7）**——做一页 pricing landing page，两个方案 A/B（per-seat vs per-site），看哪个转化更高；成本一天。
4. **周报形状（#13）**——用现有一个客户工地的 4 周数据，手工做两版周报（叙事版 vs 表格+delta+silence 版），发给站长和他的老板，问"哪版你会转发给谁"。2 天。
5. **FM/维护巡检近亲（#9）**——3 个 Christchurch FM 承包商电话，问"同一栋楼反复出现的问题你怎么串起来"。若 2/3 说"Excel/WhatsApp"，值得一个 2 天原型。

## 明确不做的

- **卖硬件**（#12）：所有独立 AI 录音硬件 18 个月内被收购或转免费。
- **房屋检测 / 损失理算**（#8）：单次会话形状，Spectora 已内建。
- **养老护理**（#10）：健康数据监管与隐私底线不匹配。
- **保险费折让合作**（#4）：方向对，但 NZ 没有对应保险方，且无任何"语音记录换折让"先例——标为 5 年。
- **矿业/公用事业**（#9）：采购周期与部署要求超出一人团队。
- **Procore Marketplace 作为增长渠道**（#15）。
- **"Ask your life"式全天录音 + 事后搜索**（#14）：三个失败案例 + 立场 + 隐私。
- **voiceprint 出勤点名**（#5）：把工人变成监控对象。
- **合成分数 / population 对比**（#13）：Oura/Whoop 被骂的正是这个，且自报置信度已实测失效。

## 没找到答案的问题

- **Procore Partner Program 的费用 / revenue share 数字**——Guide PDF 被代理拦截，公开页面无数字。下次在能访问的环境直接读 `mkt-cdn.procore.com/downloads/guide/Procore_Partner_Program_Guide.pdf`。
- **NZ 索赔顾问 / 仲裁员对"AI 转写 + 机器时间戳"的实际态度**——只找到英美的 admissibility 讨论（FRE 707 草案、Construction ADR Toolbox），没有 NZ Building Disputes Tribunal 或 adjudicator 的公开表态。
- **Funder's QS 愿不愿意用第三方照片/语音替代现场**——NZIQS 指南只讲要求，没有任何 QS 公开谈远程验证；Truepic 价格也不公开。
- **业主代表（owner's rep）对承包商日报的直接抱怨**——搜了 reddit 相关词全部落空，只找到合同条款样本。下次试 r/ConstructionManagers 的 old.reddit 镜像或 LinkedIn 帖。
- **Whoop 社区帖的原文评论**——域名被拦，只有摘要。
- **HKA "Project records from a delay analysis perspective" 原文**——摘要只给了一句"delay experts are often provided with insufficient records"，想要的是他们列的"最缺哪类记录"清单。
- **ElevenLabs 等 ASR 在争议中是否被质疑过准确率**——没有找到任何案例。
- **中国市场的付费可能**——知乎证据只证明"补写/造假"普遍，没找到任何"为真实性付费"的迹象；且监理制度下记录是合规品而非商品。未深挖小红书。
