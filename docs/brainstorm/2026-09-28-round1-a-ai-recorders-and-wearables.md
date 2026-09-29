# 智库首轮 · 切片 A：消费级 / prosumer AI 录音穿戴 + 会议记录器

**日期**：2026-09-28 · **产出者**：thinktank agent，切片 A。
**范围**：Plaud (Note / NotePin)、Limitless pendant、Bee、Omi、Friend、Rabbit r1、Humane Ai Pin、"LOKI"、Otter、Fireflies、Granola、tl;dv、Notion AI meeting notes。
**方法与网络说明（先读）**：本会话的出口代理拦截了**全部**直接抓取的页面：trustpilot.com、vice.com、news.ycombinator.com（7 个帖子全被拦）、techcrunch.com、medium.com、dev.to、apps.apple.com、old.reddit.com、bitdefender.com 以及所有个人博客。因此**所有引文都是通过 WebSearch 摘要读到的**（每条都跑了 2–4 个不同措辞的查询交叉核对）。凡摘要里只给到"紧贴转述"而非原话的，证据行标了「转述」。Reddit 原帖一条都没拿到。
**合成版**见 `2026-09-28-round1-synthesis.md`。

---

## 0. "LOKI" 是什么

**没有找到任何叫 LOKI 的 AI 硬件产品。** 中英文各查了一轮。命中的都是 Etsy 北欧神话吊坠、Loki.ai（印度 2017 数据处理公司）、Loki Mode（Claude Code 的 skill）、lokiagents.org、几个 GitHub 玩具项目。

**最可能的候选：Looki L1**（读音相同）。深圳光智时空科技，2024 年成立，CMU 校友创办，蚂蚁集团 2000 万美元 + 2026-07 A1 轮数亿人民币；产品是 32 g 挂脖 **AI 相机**（不是录音笔），12 MP、3 麦克风、IP67、$199，间隔拍照 9–13 h，输出"每日 vlog / 漫画 / 日记"；2025-08 海外首发售罄，2026 年初累计近 1 万台，CES 2026 曝光。
- https://www.looki.ai/products/looki-l1 · https://www.t3.com/tech/looki-l1-ai-wearable-ces-2026 · https://zhuanlan.zhihu.com/p/2039973740465895091
- 评测复述：Techspective 标题即"captures more than it understands"；Basic Tutorials 指出 App 允许**完全关闭录制指示灯**，"part privacy nightmare"；Serious Insights："still needs to learn how to wear the day"（电池得白天补电）。
  https://techspective.net/2026/04/15/looki-l1-review-a-wearable-ai-camera-that-captures-more-than-it-understands/ · https://basic-tutorials.com/reviews/gadget-reviews/looki-l1-review/ · https://www.seriousinsights.net/looki-l1-review/

**次候选**：Meta 的 AI pendant（2026-05 TechCrunch 报道在研，基于收购的 Limitless）；Nirva（CES 2026，前 Meta Reality Labs 团队）；Apple 传闻中的 pendant。**请 owner 确认。** 后文把 Looki L1 当作 LOKI 处理。

---

## 1. 这一片市场的四条主线（跨产品重复出现的）

1. **"然后呢"问题（the so-what problem）是最一致的抱怨**，而且它不是转写质量问题。会议记录器把"采纳率"当指标，没人量"消费率"。
2. **"忘了录"是穿戴录音的核心失效模式**——不是电池，是纪律。always-on 设备的电池 6–7 h 让它退化成"要记得开"的设备，然后就忘了开。
3. **错误归属 + 幻觉 = 一次就失去信任**。Bee 把电视对白当成你的生活；Otter 把承诺记到错的人头上，那个人收到了 deadline 邮件。
4. **硬件 = 被收购 / 变砖 / 被订阅锁死的风险**，用户已经被 Humane（变砖）、Limitless（停售 + 欧盟英国切断）、Bee（Amazon 收购后客服消失）、Plaud（撤掉 USB 导出逼订阅）教育过了。

---

## 2. 想法卡

### 1. 把"确认并发出"当成产品对象，用"消费率"而不是"录了多少"衡量自己
- **证据**：https://digitaldigest.com/ai-meeting-notes-nobody-reads/ — "Adoption climbs but usage craters—leadership tracks the number of meetings the bot joins, but nobody tracks whether anyone reads summaries or acts on action items"；"Nobody reads them; everyone says they're useful when asked."；https://dev.to/automate-archit/ai-meeting-notes-without-a-decision-log-are-just-expensive-noise-31pm — "fully automated decision logs get ignored exactly the way summaries do, because nobody committed to them. A log the boss visibly confirmed and sent is a different object. People act on it."；https://mrsproductivity.medium.com/ai-meeting-assistants-i-tested-otter-fireflies-fathom-and-5-others-116d10ebbfce — 作者追踪"有没有一个人打开过 AI 摘要"，"the results were depressing"（转述）；https://tldv.io/blog/fireflies-review/ — "if nobody owns the pipeline, Fireflies mostly produces beautiful summaries nobody reads"。
- **用户在解决什么**：不是"忘了会上说什么"，是**下周同一件事又被重新讨论一遍**，因为没人记得上次定了什么、谁负责。
- **我们为什么需要它**：直接命中 owner 的定位。而且它给了一个我们今天没有的**北极星指标**：确认邮件的打开 / 确认 / 被纠正比例，而不是录音分钟数。如果客户三周后不再打开确认邮件，我们和 Fireflies 死法一样。
- **已有的实现方式**：Fireflies / Otter 发摘要邮件、无人确认；Granola 让用户自己的笔记框住 AI 输出；dev.to 提的"AI 提候选 → 参与者核验 → 责任人接受 → 才入 log"三段式，没有主流产品完整实现。
- **我们怎么做**：本仓已有 session confirmation email + suggestion 的 pending/confirmed/rejected。缺两样：① 在 org-api 记**确认邮件的打开 / 点击 / 每条 item 的确认或改写**（一张 `brief_interactions` 表 + 打开像素或签名链接），做成 owner 自己看的周报；② 把"经理确认过"变成 item 上的可见状态并**只把确认过的推给他人**。前置：稳定事件身份——没有稳定 id 就记不住谁确认了哪条。
- **做不到 / 不该做**：不要做成"催你确认"的 nag；Otter 式邮件轰炸被骂最狠（见卡 6）。
- **12 个月内可卖？**：是——让现有确认邮件变成可以对客户说"你的团队 6 周内确认了 83% 的项"的东西。
- **信心**：高。

### 2. "漏录检测"：你在工地待了 6 小时，只录了 40 秒——收工时问一句
- **证据**：https://mrktcorrect.com/blog/pocket-ai-review — "The device only captures the conversations you remember to bring it to. The first week I forgot it on a charger twice and missed two calls I wish I hadn't."（作者称之为 "discipline tax"）；https://www.layer3labs.io/gear/reviews/limitless-pendant — "you'll learn to turn it off when you don't want it recording, which inevitably leads to forgetting to turn it back on. I've missed a few key conversations."；https://tldv.io/blog/plaud-notepin-review/ — NotePin squeeze-to-record "some users reporting missing recordings because they didn't squeeze the device just right"（转述）；Plaud 社区帖 "Forgot to stop recording on Plaud"。
- **用户在解决什么**：录音设备的价值 = 被记得带上/按下的那一部分。
- **我们为什么需要它**：PTT 是**有意按下**的，这是我们和 pendant 相反的选择。但它继承了同一个失效：**忘了按**。而且我们**看不见**漏录——一天 3 条 fragment 在数据里和"这天很闲"一模一样。和 BUG-43 的教训同构。
- **已有的实现方式**：没人做。
- **我们怎么做**：收工前（NZ 16:30，BUG-37 显式转 NZ）比对**当天在册工时 / 站点签到窗口 vs 有录音的时间段**，空洞 ≥ 2 h 就在 App 推一条 "10:00–12:30 在 Papakura 没有录音——补一段 30 秒？"。第二版用 `day_location_markers` 按区域拆。**不用**手机 GPS 连续定位。
- **做不到 / 不该做**：不要变成"经理录音时长排行榜"给 GM 看。
- **12 个月内可卖？**：是，作为留存功能。
- **信心**：中。需要看 prod 的 recordings 时间分布证明空洞真实存在。

### 3. 承诺不能挂到没被验证的人头上：说话人置信度必须出现在确认邮件里
- **证据**：https://cotera.co/articles/fireflies-vs-otter-ai — "A transcript attributed a product commitment to an engineering lead Rafael when it was actually a product manager Chen who'd said it, and Rafael got a follow-up email about a deadline he'd never agreed to."；https://tldv.io/blog/notion-ai-meeting-notes-review/ — Notion AI "struggles to identify who is speaking"；The Verge 测 Bee："confused differentiating background audio from TV from actual conversations"；Trustpilot Bee 1 星："the AI is hallucinating and not actually recording tasks"（转述）。
- **用户在解决什么**：一条错归属的 action item 会**制造一个不存在的承诺**。信任是一次性的。
- **我们为什么需要它**：工地比会议室更糟。CLAUDE.md 自己测过：speaker labels 只有 ~55% 纯度，rebind 后 96.3%。**prod 上 `SPEAKER_IDENTITY_MODE=off`、`EMIT_EVIDENCE=false`**——prod 今天发出去的每一条带人名的承诺，读者都看不到它是怎么来的。
- **已有的实现方式**：Fireflies / Otter 允许事后手改 speaker label，不显示置信度。没人在**发出去的邮件**里区分"他亲口说的" vs "有人在场说的"。
- **我们怎么做**：三档措辞，机械决定不靠模型：`evidence_status=verified` + 说话人已 rebind → "Paul 确认…"；quote 已核验但说话人未识别 → "现场有人确认…（说话人未识别）"；quote 未核验 → 不进 Assignee 列。改 `build_confirmation_email` 的模板 + 打开 prod 的 `EMIT_EVIDENCE`。经理在邮件里一键"是 Paul 说的"触发已有的 speaker-correction propagation。
- **做不到 / 不该做**：不要把 LLM 自报的 `decided_by` 当真。
- **12 个月内可卖？**：是——"我们的邮件不会冤枉你的分包"。
- **信心**：高。

### 4. "争议证据包"：原始音频是证物，不是副产品
- **证据**：https://www.speechmark.co/articles/does-granola-save-your-recordings — Granola 转写后删音频；"whether a client agreed to '$40k' versus '$14k'—the recording would serve as a referee, but without it, an AI-generated note is the only record"；https://zackproser.com/blog/granola-vs-zoom-transcription-comparison — "treat the raw audio as the record, not a byproduct"；Forbes 标题："I Let AI Record My Every Waking Moment And Now I'll Never Lose Another Friendly Argument Again"——消费者对 pendant 说得出的**唯一具体价值**就是"吵架有证据"。
- **用户在解决什么**："当时说好的"没法证明。建筑业这个问题有名字：variation、EOT、back-charge。
- **我们为什么需要它**：本仓的证据层在结构上**已经超过**所有消费产品——但今天只是内部字段，没有面向 QS / 商务经理的**出口**。
- **已有的实现方式**：Procore / Aconex 的 RFI/variation 记录是文本 + 附件，没有音频链。
- **我们怎么做**：`POST /evidence-packs`：给定 topic/finding/decision id 列表，产出 PDF + zip：每条 quote、绝对时间、说话人（含置信档）、±30 s 音频片段、同窗口照片、当天天气、programme 对应任务。**前置**：稳定事件 id。
- **做不到 / 不该做**：不做法律意见。NZ 是 one-party consent，法律面可行，但 pack 封面要写清录音方是谁、对方是否被告知。
- **12 个月内可卖？**：是，可能是**唯一一张能单独定价的卡**。
- **信心**：高（对需求）；中（对"经理会为此换工具"）。

### 5. 会前简报：见这个分包之前，把过去三次他承诺过、还没关的事摆出来
- **证据**：https://www.success.com/ai-meeting-notes-tools-compared-otter-vs-fireflies-vs-grain — Fireflies "Meeting Prep feature surfaces past meetings with the same attendees and can scan open action items"；https://wondertools.substack.com/p/granolaguide — 用户问的问题："What did we agree to last month? What themes keep coming up? What did I promise that I haven't followed up on?"；https://feedback.plaud.ai/b/nvk7pg0r/feature-ideas/ask-ai-search-all-recordings — Plaud 用户投票要求 "Ask AI + Search All Recordings"。
- **用户在解决什么**：不是"搜过去"，是**在下一次对话开始前 30 秒**知道该追什么。
- **我们为什么需要它**：这是 owner 定位（recurrence、slippage、silence）的**第一个面向用户的出口**。threads、Ask、`open_questions`、action_items 都有了；缺的是**触发**。
- **已有的实现方式**：Fireflies Meeting Prep（靠日历参会人匹配）。工地上和分包的对话没有日历。
- **我们怎么做**：触发用**人**：经理选了站点或说了分包名（`session_brief.entities` 已抽 22 实体/会但只留在 S3）。最小版：App 首页一张卡 "本周你会碰到的：Paul（水电）— 3 条未关：… 上次提及 9 天前"。
- **做不到 / 不该做**：`entity_name` 是自由文本，先用 `name_aliases` 手工合并。
- **12 个月内可卖？**：是。
- **信心**：高。

### 6. 被录的人也能看见：把"录音礼仪"做成产品，而不是 ToS 里的一句话
- **证据**：https://www.bitdefender.com/en-us/blog/hotforsecurity/otter-ai-keeps-joining-your-meetings-uninvited-heres-how-to-make-it-stop — Trustpilot 用户："they will invite all the company without your permission and spam everyone"；https://www.cnn.com/2025/11/16/tech/friend-ai-device-backlash-ceo-avi-schiffmann — WIRED 记者戴 Friend 去 SF 科技活动，"one researcher accusing her of 'wearing a wire'"；https://help.limitless.ai/en/articles/13004190-... — Limitless 专门给**被录者**写了一页帮助文档；https://www.masonllp.com/blog/your-ai-meeting-assistant-may-be-stealing-your-voiceprint/ — 2025-12 Illinois 对 Fireflies 的 BIPA 集体诉讼；https://www.darkreading.com/application-security/ai-notetaker-spy-government-corporate-video-calls — tl;dv 配置错误暴露 18.1 万场会议元数据。
- **用户在解决什么**：录音者想要记忆；被录者想要**知情和对等**。所有 backlash 都发生在第二方身上。
- **我们为什么需要它**：工地是"一个人录、二十个人被录"。owner 的隐私底线是内部的，分包看不见。Friend 的教训是社交摩擦会让人**不再和你说话**，那就没有 fragment 了。
- **已有的实现方式**：Limitless 的被录者页；Granola 不通知（被批评）。没有一家给被录者**看自己的话**。
- **我们怎么做**：① 站点 induction 一页纸"这个工地用 FieldSight：录什么、不录什么、你能看到什么"；② 分包收到的确认邮件里，**他自己说的话的那几条**附上 quote；③ 录音时 PTT 设备 LED + 手机通知条常亮。
- **做不到 / 不该做**：不做"给每个工人一个账号看全部转写"。
- **12 个月内可卖？**：取决于——它自己不卖钱，但是 GC 采购问"工人怎么想"时的答案。
- **信心**：中。

### 7. 导出保证 + 吃任何录音源：把"被收购 / 变砖 / 订阅锁死"的恐惧变成我们的卖点
- **证据**：https://www.techdirt.com/2025/02/24/another-startup-implosion-set-to-brick-700-ai-pins/ — Humane 给用户 10 天，全部变砖；Meta 收购 Limitless 后停售 pendant，多国 2025-12-19 起停服，36kr 标题 "Netizens Call for Refunds"；Trustpilot Bee："they basically went radio silent, their developer page was taken down"（转述）；https://tldv.io/blog/plaud-notepin-review/ — "Plaud removed the option to access files via USB, making the only option to pay the subscription"。
- **用户在解决什么**：把自己一年的记忆放进一个可能明年就不存在的盒子。
- **我们为什么需要它**：我们是一个人的公司，客户**一定**会问"你倒了怎么办"。反向也成立：**接受任何来源的音频文件**能绕开"再买一个设备"。
- **我们怎么做**：① `GET /export`：按公司打包 S3 + Aurora JSON，签名 URL 24 h；② 合同里写"数据 90 天可导出、停服 6 个月通知"；③ App "导入音频"入口：任何 .m4a/.wav 落到 `users/{name}/audio/{date}/`，复用现有 S3 触发链（BUG-13 注意前缀）。
- **做不到 / 不该做**：不承诺开放 API 给第三方。
- **12 个月内可卖？**：是——成交障碍的移除。
- **信心**：中。

### 8. 按站点 / 项目定价，分钟不限量——因为我们的 fragment 天生短
- **证据**：https://www.bluedothq.com/blog/plaud-review — "The subscription requirement is the most divisive topic among Plaud AI users"；300 免费分钟/月不结转；Humane "$699... plus $24/month subscription made possible customers say 'I'll just use my phone'"；Otter 免费版 "30-minute cap per call means most business meetings get cut off"。
- **用户在解决什么**：讨厌的不是付钱，是**计量单位和使用方式不匹配**。
- **我们为什么需要它**：我们的成本结构和 pendant 相反：fragment 短、VAD 已丢掉静音、ASR 按批。**分钟数不是我们的成本驱动**。建筑业分包按项目进出，按席位卖会逼客户每月增删账号。
- **我们怎么做**：定价页"按活跃站点/月，录音不限量，观看者免费"。
- **做不到 / 不该做**：别学 Plaud 的 300 分钟免费额——它训练用户**省着录**。
- **12 个月内可卖？**：是。
- **信心**：中。

### 9. 不做硬件；PTT 电台是工地上唯一已经被接受的"穿戴"
- **证据**：https://www.machinesociety.ai/p/why-ai-necklaces-and-pins-will-always — Mike Elgan："Don't waste your time on necklaces or pins. Nobody will wear them."；https://www.layer3labs.io/gear/reviews/humane-ai-pin — "what does this do that a phone with the same AI model cannot do better?"；Rabbit r1 "headed to a junk drawer"；NotePin 最多的投诉是**开始/停止录音的手势不可靠**；Meta / Apple / Nirva 仍在做 pendant——大厂在赌，小厂全死了。
- **我们为什么需要它**：**战略确认**。工地经理已经戴着对讲机，按键大、戴手套能按、噪音里能出。**Humane 的问题"手机做不到什么"，我们的答案是"手套 + 90 dB + 不用解锁"。**
- **已有的实现方式**：Plaud 出了 NotePin S for Field Workers 页面，说明他们看到了这个市场。
- **我们怎么做**：什么都不建。把 "works with the radio you already carry" 写进首页第一行。
- **12 个月内可卖？**：是（它是定位）。
- **信心**：高。

### 10. 电池和上传失败必须**当天**可见：一段没上传的录音比没录更糟
- **证据**：https://ticnote.com/en/blog/omi-ai-wearable-review — Omi："'Your Omi Disconnected' notifications constantly"；"if your phone loses connection, you lose that audio"；GitHub issue #3458 "only notifies me of disconnection when it reconnects"；Bee 宣称 7 天，实测 1.5–2 天；本仓 BUG-43：260 分片 129 进 S3。
- **用户在解决什么**：他们**以为**录上了。静默丢失比明确失败伤害大十倍。
- **我们怎么做**：确认邮件顶部加一行**对账**："今天收到 12 段 / 设备记录 14 段；2 段待上传（10:14、13:52）"。`recordings` 表已有两边。
- **12 个月内可卖？**：是（留存功能）。
- **信心**：高。

### 11. 一键 highlight / 口头标签："这一条是 variation"
- **证据**：NotePin S "long press to start recording and a short press to add a highlight"；https://humla.team/blog/granola-review — "the note is the product"；https://www.bluedothq.com/blog/ai-note-takers-for-construction — 建筑用户的做法："having project directors announce what they're doing when entering each room during site visits"。
- **用户在解决什么**：录了两小时，事后找不到"那一句"。用户自己发明的解法是**在录的时候口头打标**——这已经是 fragment 思维。
- **我们为什么需要它**：一天 30 条 fragment 里哪 3 条是要追的，今天靠模型猜 `priority`（44% 都是 high，字段已失效）。**经理说一个词比模型猜准。**
- **我们怎么做**：抽取 prompt 加一条规则：fragment 开头或结尾出现固定词表（"flag this / variation / safety / 追一下 / to QS"）→ 直接写入 `findings.domain` 或 `manual_tag`，**不经模型判断**，用 `a-literal-token-is-findable` 已有的字面匹配。
- **12 个月内可卖？**：是。
- **信心**：中（需要看两周真实录音里有没有自发出现的标签词）。

### 12. 按压绑定的照片连拍（Looki 的反面）：只在 PTT 按下的 ±N 秒拍，不 always-on
- **证据**：Looki "captures more than it understands"；"continuously records its surroundings … without their active consent"；正面："turned my everyday life into my favorite digital journal"——价值在**有图的记忆**。
- **我们怎么做**：App 在 PTT 按下时若相机可用，拍 1–3 张低分辨率静帧走已有 ±2 min 绑定。**只在按压窗口内**。
- **做不到 / 不该做**：手机在口袋里拍到的多半是口袋。**很可能不成立**。
- **12 个月内可卖？**：否（先 fake-door）。
- **信心**：低。拉伸卡。

### 13. 说清"我们不是 Otter"：不进你的会、不群发你的同事、不生成 voiceprint 除非你点头
- **证据**：卡 6 全部 + https://tldv.io/blog/ai-meeting-recorder-lawsuits/ + 2026-09-24 标题 "The AI notetaker has become a corporate liability"。
- **我们为什么需要它**：我们**已经**做对了，但没有一页对外的 trust page。
- **我们怎么做**：一页 "How FieldSight records"：五条不做 + 数据在 ap-southeast-2 + 导出 + 被录者页。半天。
- **12 个月内可卖？**：是（成交障碍移除）。
- **信心**：高。

---

## 3. 与 owner 定位的关系（直说）

- **强化定位的**：卡 1、2、5、9、10、11。消费 pendant 的死法（always-on → 电池 → 忘开 → 摘要没人读）等于是替 owner 的反面立场做了两年的对照实验。
- **中立的**：卡 3、4、6、7、8、13——把已建成的证据层和隐私底线**变成可见的商业物**。
- **拉伸**：卡 12。
- **威胁现有计划的一点**：评估文档把"稳定事件身份"排第一，这份调研**独立地**得出同一结论——卡 1、4、5 三张最值钱的卡都卡在它上面。

## 值得下一步验证的 5 个
1. **争议证据包（卡 4）** — 5 位 QS / 商务经理电话。不写代码。
2. **消费率指标（卡 1）** — 确认邮件加打开像素 + 每条 item 的"确认 / 不对"签名链接，跑 3 周。2 天。
3. **漏录检测（卡 2）** — 用 prod `recordings` 表画录音时间分布，看 ≥2 h 空洞占比。一个 SQL。
4. **会前简报（卡 5）** — App 首页 fake-door 卡片，点击计数。一天。
5. **口头标签（卡 11）** — prod 两周 transcripts 里 grep 经理**自发**说的标记词。一个脚本。

## 明确不做的
- 自己的硬件 / helmet cam / 接入 Looki 类 always-on 相机。
- all-day 录音模式。
- 按分钟计费 / 免费分钟额度。
- 用 LLM 自报的 `decided_by` / `priority` 填 Assignee。
- 给每个工人开账号看全部转写。
- 开放第三方 API。

## 没找到答案的问题
- **LOKI 到底是什么**——Looki L1 是唯一同音候选。需 owner 确认。
- **Reddit 原帖一条都没拿到**。r/PlaudNote、r/omi_ai、r/rabbitr1 的一手弃用故事需换路径。
- **HN 七个帖子全部被拦**（40069141、46168088、41380860、43578959、44958410、47612681、44944348）。
- **Plaud 1–3 星评论原话**——Trustpilot 被拦。
- **Humane / Rabbit 用户"我到底用它干了什么"**——只有评测。
- **NZ 建筑工地录音的行业惯例**（Site Safe / CHASNZ）——只查到 one-party consent 一般法律。
- **Looki L1 中国用户评价**——只有销量。
- **Notion AI meeting notes 一手负评**——只有 tl;dv 二手。
