# 智库第二轮 · 分包商客户段（NZ 优先，AU/UK/US 作模式佐证）

**日期**：2026-09-28 · **产出者**：thinktank agent。**合成版**见 `2026-09-28-round2-synthesis.md`。

## 0. 来源可达性（实测）

第一手读到（WebFetch 或 curl 全文）：`nzstcf.org.nz`（**SA-2017 全文 PDF，用 node pdf-parse 抽了文本**）、`norlinglaw.co.nz`、`raineycollins.co.nz`、`trueworks.co.nz`、`resolutelawyers.co.nz`、`forums.mikeholt.com`（含一位 Canterbury NZ 电工）、`planningplanet.com`、`contractflooringjournal.co.uk`、`forums.contractoruk.com`、`merlolaw.com.au`、`levelset.com`、`procore.com/advantage`、`capterra.com` Procore 评论、`play.google.com` Raken 评论、`hardlineapp.com`、`constructionmetric.com`、`getresq.com/nora`、`pipcall.com`、`privacy.org.nz`、`sprintlaw.co.nz`、`theprofessionalbuilder.com`（NZ 建商播客）、`masterelectricians.org.nz`、`nzbe.co.nz`、Master Builders "Good Contracting Principles" PDF、HN Algolia（分包相关几乎为零）。

只通过摘要：Building Disputes Tribunal / BuildLaw、NZ Herald / BusinessDesk Teak 报道（付费墙）、Electrician Talk（tollbit 402）、Procore Community（503）、seek.co.nz（JS 墙）。Reddit：按指令未直连；本轮没有新的二手引用。

**总体判断**：Q2（分包为缺记录丢钱）证据最硬，且 NZ 本地有一份现成的"时钟"——SA-2017；Q1（NZ 沟通习惯）证据最薄，没找到任何 NZ 分包工头自述"我怎么向办公室汇报"的公开文本；Q5（口头指令台账）美英已有 3–4 家在做，没有一家做 NZ/AU、没有一家绑合同时限。

---

## 1. NZ 实际沟通习惯：证据（Q1）

**直说：NZ 分包工头"怎么向自己公司汇报一天"的公开证据非常薄。** SEEK/Jora/Indeed 职位描述（JS 墙，摘要只有通用句）、Master Electricians / Plumbers 网站（无指引）、Geekzone / Trade Me 社区（无相关帖）。没有第一人称描述。

**能找到的 NZ 第一手材料（都是建商/GC 侧）：**
- https://theprofessionalbuilder.com/podcast/onsite-14/ — Pedersen Homes（NZ）："Replacing 16 evening phone calls with a strict 2:00 PM daily stand-up meeting to triage site issues."（第一手）→ 老板收工后接**电话**。
- https://theprofessionalbuilder.com/podcast/onsite-13/ — Chain Construction（Auckland）："the site team to bring answers instead of just complaints"。
- https://forums.mikeholt.com/goto/post?id=1327253 — 2010 帖，Canterbury NZ 电工："You should do these [daily sheets] … these sheets capture info you need to improve your pricebook." → NZ 小电工把日报当**报价校准**工具。
- Master Builders "Good Contracting Principles"（2024 PDF，第一手）：整份讲 NZS 3910 变更/EOT 的**书面通知时限**，默认介质就是"notice in writing"= 邮件。

**AU/UK/US：** AU 确实是 WhatsApp（builtsimple.com.au 摘要；Nora、BuildPass 以 WhatsApp 为入口）；UK 是 WhatsApp 群 + 手机通话并行（constructionmetric.com、pipcall.com 第一手）；US 工头"call or text the PM"，且 Raken Play 评论（第一手）："If my company didn't require this app I would definitely not be interested in it."

**结论**：owner 的"NZ 是电话/邮件"与能找到的 NZ 材料**一致但未被分包侧证实**。假设：NZ 分包工头收工后**打电话给老板/合同经理**，正式的东西走**邮件**，WhatsApp 是工地内部群而非对公司的汇报通道。

---

## 2. 六个问题的直接回答

- **Q2 分包为缺什么记录丢钱**：(a) 口头变更——Norling Law（第一手）："the contractor is not entitled to payment if it cannot prove that the variation was instructed"。(b) **SA-2017 9.1.3（第一手）：分包在收到指令后 5 个工作日内须书面通知"这是变更"，否则"may result in the work not being treated as a variation"**；9.1.3.2：分包书面通知后 GC 5 个工作日不回则视为变更；10.2.2：EOT 也是 5 个工作日；12.3.4：final account 里补报的变更 44 个工作日后才到期。(c) daywork 未签——CFJ（第一手）："Getting it signed on site, on the day, is the part that decides whether you are paid"。(d) backcharge——levelset（第一手）分包原话："If I get back charged 6 months later for cleanup, no pictures, no email notices…you will see a mechanics lien."。(e) 留置金——Teak 清算：一家分包被欠 $790k；留置金是**钱在谁手上**的问题，不是记录问题——不要往这上面卖。
- **Q3 对 GC 工具**：Procore 自己承认（第一手）subs "were simply renting access to it"，项目关闭后 "Account Not Found"；Capterra "Forced to use by other companies"。没有 NZ/AU 分包对 Aconex/ACC 的公开抱怨。
- **Q4 谁买**：NZ 分包公司的 "Commercial Manager" 职责就是 "driving progress claims, variations and final accounts, and handling disputes, extensions of time and claims"。买家 = 老板或合同经理；价格锚点：一次未收回的变更；trueworks（第一手）：欠款 $30k 拖 3 个月利息 $750–1,050，裁决费 "low thousands"。
- **Q5 口头指令台账**：有，但都不在 NZ/AU、都不绑合同时限：Hardline（US，$2M pre-seed，"No call audio stored"）、PiPcall（UK，通话录音）、Construction Metric（UK，WhatsApp→"tamper-evident record"，£199/site/月）、Nora（WhatsApp）。
- **Q6 隐私**：NZ 一方同意即合法，但 Privacy Commissioner（第一手）："there is a strong presumption that it is unfair" 对偷录；Sprintlaw NZ（第一手）："Best practice is to tell people upfront you're recording"。**没有找到任何 NZ 工地"禁止录音"的 site rules 文本**，也没找到分包录 GC 员工引发摩擦的真实故事。

---

## 3. 想法卡片

### 1. 把 SA-2017 的"5 个工作日"做成产品的心跳：碎片 → 次日早上一封 9.1.3 书面通知草稿
- **证据**：https://www.nzstcf.org.nz/wp-content/uploads/2021/08/SA-2017-Entire.pdf（第一手）— 9.1.3 "the Subcontractor must notify the Contractor in writing that they believe a variation is involved … [默认 5 Working Days]. Failure … may result in the work not being treated as a variation."；9.1.3.2 "the work will be treated as a variation unless the Contractor notifies the Subcontractor otherwise within 5 Working Days of written notification"；https://norlinglaw.co.nz/blog-posts/variations/（第一手）— "the contractor is not entitled to payment if it cannot prove that the variation was instructed."
- **用户在解决什么**：工头被 GC 叫去做额外的活，回头没人写邮件；5 天一过合同上就不是变更。反过来，分包一旦书面通知，**GC 5 天不回就自动视为变更**——分包手里少有的进攻性条款，没人用，因为没人在第 1 天写那封邮件。
- **我们为什么需要它**："碎片 → 几周后才见意义"定位的最强分包版本：意义是"这条指令的时钟走到第几天了"。NZ 本地、别人没有的钩子（Hardline/Construction Metric 都不懂 SA-2017）。
- **已有的实现方式**：GoCanvas CVI 表单（手填）；Hardline（美国语境）；律所建议"发一封短邮件"。没有人把**合同时限**做成提醒。
- **我们怎么做**：抽取类型新增 `instruction_received{from_org, from_person(自由文本), what, when, where}`；入库起 5 WD 计时器（`nz_time` 已有）；D+1 早上生成 9.1.3 通知草稿，一键发；D+5 未发 → 提醒；已发 → GC 侧 5 WD 反向计时，到期未回 → "视为变更"标记。合同参数做成 site 级设置。
- **做不到 / 不该做**：不做法律意见；不自动发给 GC；不假设都用 SA-2017（GC 自拟条款常见）——时限可配。
- **12 个月内可卖？**：**是**。分包段主打卖点。
- **信心**：**中高**——"变更要书面、要证明"反复出现，但**没有用户说"我想要个 app 帮我记时钟"**：需求真，形态是推的。

### 2. Daywork / 资源记录：当天、带机器时间戳、"签了 / 拒签"都入账
- **证据**：https://contractflooringjournal.co.uk/point-of-view/can-the-qs-knock-my-dayworks-back（第一手）— "Getting it signed on site, on the day, is the part that decides whether you are paid"；JDM Accord v SoS (2004)：未签的 timesheet 若对方未及时争议仍是有效记录。https://planningplanet.com/forums/forensic-claims-analysis/414812/（第一手）— 主包被教 "instruct the MC reps not to sign any daywork sheets, and if signed it should be clearly written ONLY FOR RECORD NOT FOR PAYMENT"。
- **用户在解决什么**：主包被明确教导"别签"。分包唯一的对策是让"拒签"本身成为记录。
- **我们怎么做**：工头一条 PTT："daywork，level 2 走廊改线，3 个人 2 点到 5 点，Jonno 说他不签" → `daywork_record{crew_n, hours, plant, area, signed:false, refusal_note}` → 当天 PDF + 邮件给 GC "for record purposes"。月末 Evidence Pack 按 signed/unsigned 分列。
- **做不到 / 不该做**：不做电子签名工作流。
- **12 个月内可卖？**：**是**，与卡 1 打包。
- **信心**：**高**。

### 3. Backcharge 防御：收工时每个区域"照片 + 5 秒语音"的离场状态记录
- **证据**：https://www.levelset.com/blog/smart-contractors-avoid-back-charges/（第一手）— "If I get back charged 6 months later for cleanup, no pictures, no email notices…you will see a mechanics lien."
- **我们怎么做**：App "leaving area" 快捷按钮 = 照片 + 语音一句；`day_location_markers` 复用；查询走现有 Ask/RAG。**不需要 LLM 抽取**就有价值。
- **12 个月内可卖？**：**是**，作为功能。
- **信心**：中（NZ 语境是 set-off / contra charge，机制一样）。

### 4. 延误/EOT：把"stood down / no access"碎片变成 5 个工作日内的延误通知
- **证据**：SA-2017 10.2.2（经 https://www.nzbe.co.nz/post/subcontractor-extension-of-time-dis-entitlement-no-harm-no-foul，第一手）— "not entitled to an extension of time unless … the notice is given within 5 Working Days"；SA-2017 5.18.2：未及时通知的变更 "shall be valued … as if notification had been given … reduced accordingly"。
- **我们为什么需要它**：与卡 1 同一套机制，第二个事件类型（`delay_event`）。几周后的意义：**同一个 GC、同一种延误反复出现却从未通知**。
- **我们怎么做**：`delay_event{cause, crew_idle_n, hours, area}` → 5 WD 计时 → 通知草稿；挂到 programme task。
- **12 个月内可卖？**：**是**。
- **信心**：中高。

### 5. 付款申请的"substantiation"附件：月底自动出本月变更/daywork/延误清单，带录音锚点
- **证据**：https://www.trueworks.co.nz/post/subcontractor-not-paid-nz（第一手）— 裁决 "won on documents: the contract, the claims and schedules, the variation trail, the measure, the programme"；SA-2017 12.1.1：变更申请须在到期日前 5 个工作日提交。
- **我们为什么需要它**：分包公司**每月固定的付费理由**（月度节奏 = 订阅节奏）。
- **我们怎么做**：月度视图 = 本月 `instruction_received / daywork_record / delay_event` × 状态 → PDF 附在 claim 后。不碰金额。
- **做不到 / 不该做**：不做 CCA 合规的 payment claim 本身。
- **12 个月内可卖？**：**是**。
- **信心**：中。

### 6. "沉默账本"：按 GC、按项目统计"收到口头指令 N、书面确认 M、超时未确认 K"
- **证据**：同卡 1；https://www.pipcall.com/blog/construction-disputes-dont-start-in-contracts-they-start-in-conversations（第一手）。
- **我们怎么做**：卡 1/2/4 状态表做 rollup 到老板月度邮件。
- **做不到 / 不该做**：不做"GC 信用评分"对外发布——诽谤风险。
- **信心**：中（推的）。

### 7. 定价：按公司包月、工头不限、价格低于"一次没收回来的小变更"
- **证据**：constructionmetric.com（第一手）"From £199 a site a month … not a subscription for every employee"；trueworks（第一手）$30k 拖 3 个月利息 $750–1,050；Raken 评论 → 工头不会自己买。
- **我们怎么做**：NZ$249–399/公司/月（≤5 个活跃工地），工头不限，老板/QS/admin 免费；年付送 Evidence Pack 导出。
- **信心**：中。

### 8. 口头指令台账的形态：**工头自述**，不是录 GC —— 唯一能过 NZ 隐私关的设计
- **证据**：https://www.merlolaw.com.au/post/no-written-contract-how-subcontractors-can-build-an-evidence-arsenal-and-pursue-payment-in-qld（第一手）— 日记应记 "verbal instructions received from the site manager"，例："Johnno said to use the premium undercoat on all walls"；https://www.privacy.org.nz/resources-and-learning/knowledge-base/view/324/（第一手）；https://sprintlaw.co.nz/articles/recording-conversations-in-new-zealand-legal-rules-for-business-owners/（第一手）；Hardline 自称 "not by recording calls, but by intercepting and documenting"（机制未说明）。
- **我们为什么需要它**：决定了卡 1/2/4 能不能在工地上活下来。自述是**工头自己的声音、自己的记录**，法律上等同于日记，社交上等同于"我记一下"。
- **我们怎么做**：文案固定为 "record what you were told, in your own words"；`from_person` 为自由文本，**永远不进 speaker_voiceprints**；给分包一页 "Site recording etiquette" 可转发给 GC。
- **做不到 / 不该做**：不做通话录音；不做"把 GC 说的话转成文字"。
- **信心**：高（法律来源第一手；社交摩擦部分是推断）。

### 9. 收工电话的替代品：一条 2 点钟的三问 PTT（卡住什么 / GC 让你做了什么额外的 / 明天缺什么）
- **证据**：theprofessionalbuilder.com onsite-14（第一手）— "Replacing 16 evening phone calls with a strict 2:00 PM daily stand-up"；onsite-13 — "bring answers instead of just complaints"。
- **我们为什么需要它**：NZ 本地唯一"汇报习惯"证据，指向电话不指向 WhatsApp。把电话变成固定时刻、固定三问的 PTT，第二问就是 `instruction_received`。"没说"也是数据。
- **我们怎么做**：App 每天 14:00 本地通知，三个按钮各录 ≤30s；缺一条就在老板邮件里显示"未答"。零后端改动。
- **12 个月内可卖？**：**是**。
- **信心**：中（证据来自建商而非分包）。

### 10. 给 GC 的日报走**邮件**、不走 API：格式对齐 Procore Daily Log / Raken Collaborator，原件留在分包
- **证据**：https://www.procore.com/advantage/what-subs-lose-when-gc-closes-project（第一手，Procore 自己写）— "they were simply renting access to it … 'Account Not Found'"；Raken Play 评论（第一手）；Capterra "Forced to use by other companies"。
- **我们怎么做**：每日 PDF（manpower / work performed / delays / photos）+ SES 邮件给 GC 指定地址；模板按 GC 可配。现有 daily report 生成器改一个模板。
- **12 个月内可卖？**：**是**。
- **信心**：中高。

### 11. 威胁卡："语音 → 日志"在分包侧也已商品化——不要在这里竞争
- **证据**：hardlineapp.com（第一手）— "turns calls, site walks, photos, and videos into … daily logs, RFIs, punch items, tasks, change orders"，$2M pre-seed 2026-05；constructionmetric.com（第一手）£199/site/月；Nora（第一手）。
- **可守的差异化只有两条**：(a) **NZ 合同时钟**（SA-2017 / NZS 3910 / GC 自拟的 5 WD）——需要本地知识；(b) **跨周的模式**（同一 GC 反复口头指令、反复未确认）——需要 threads/recurrence，本仓已有而他们没有。
- **信心**：高。

### 12. 脚手架专用：交付/拆除/租期的语音时间戳记录（snippet-based，证据弱）
- 脚手架是 NZ 分包里合同最简单、纠纷最时间化的 trade，是卡 1/2 的最小试点客户。三种碎片模板（handover / alteration / dismantle）。
- **12 个月内可卖？**：取决于 2 个脚手架公司电话。**信心**：低。

### 13. 项目关闭时的"分包自己的档案"：PC 那天自动打包全项目记录
- **证据**：procore.com/advantage（第一手）— "a claim arises three years after turnover … Without access to the original RFI trail, time-stamped photographs … you are forced to rely on distant memories"；SA-2017 12.4.2：留置金首次释放在 PC 后 22 个工作日。
- **我们怎么做**：site 加 `practical_completion_date`；到期生成 ZIP 到分包自己的下载链接；DLP 结束再打一次。
- **做不到 / 不该做**：不做 10 年托管承诺。
- **信心**：中。

### 14. 用"合同经理"做入口，不是工头：第一个屏幕是"本月未确认的口头指令"，不是日报
- **证据**：SEEK Commercial Manager 职责（摘要）；knowify（摘要）"if … logging a day or snapping a change order takes more than a couple of taps, the data will never exist"；Raken 评论工头被强制才用。
- **我们怎么做**：Web 首页 = 状态表（卡 6）；App = 三问 PTT（卡 9）。Demo 从"上个月你们漏了几个变更"开始。
- **信心**：中高。

### 15. （5 年视野）跨市场同构：中国"签证"、UK CVI、NZ SA-2017 9.1.3 是同一个对象
- `notice_deadline_days` 和 `notice_template` 做成 site/合同级配置，不硬编码。12 个月内不进 AU/UK/CN。

---

## 4. 值得下一步验证的 5 个
1. **NZ 分包工头收工后到底怎么汇报**— 5 通电话：2 电工、1 HVAC、1 脚手架、1 幕墙。三问：几点、用什么说今天的事；GC 口头让你们干额外的活，谁写邮件、多久写；上季度有没有一件事因为没书面而没收到钱。
2. **SA-2017 5 WD 时钟有没有人在用**— 同一批电话加一问："合同是 SA-2017 还是 GC 自己的？变更通知期限几天？谁盯？"
3. **"自述式"指令记录工头肯不肯说**— 2 天原型：App 加 "I was told to…" 按钮 + 次日邮件草稿，2 个分包工头用一周。
4. **月度"未确认指令"状态表老板看不看**— fake-door。
5. **定价锚点**— landing page A/B。

## 5. 明确不做的
通话录音 / 录 GC 员工；CCA 合规的 payment claim 文书本身；留置金追讨；Procore/Aconex 双向；"说一句话出日报"作为主卖点；GC 信用评分对外发布；10 年档案托管承诺。

## 6. 没找到答案的问题
- NZ 分包工头第一人称的汇报习惯——下轮直接打电话，别再搜。
- NZ 工地"禁止录音/拍照"的 site rules 文本——Site Safe、CHASNZ 都没有；大 GC induction 不公开。
- NZ/AU 分包录 GC 员工引发摩擦的真实故事——没有。
- BDT / BuildLaw 里"口头变更 + 分包 + 日记证据"的 NZ 裁决——要人工翻。
- Electrician Talk 两帖——付费墙。
- NZ/AU 分包对 Aconex / ACC 的公开抱怨——一条都没有。
- Hardline / Nora 定价与机制——不公开。
- NZ 分包 WhatsApp 使用率的任何数据——没有。
