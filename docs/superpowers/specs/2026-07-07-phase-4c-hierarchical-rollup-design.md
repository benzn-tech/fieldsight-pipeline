# Phase 4c:分层信息精炼(项目总览 + GM 一览)设计 spec

> 用户需求(2026-07-07):一个工地多个 SM 每天产生多人数据,PM 要"快速了解工地情况"而不是逐份读报告;GM 统领全部项目。层级按用户澄清收敛:**交互按项目分**——项目内总览(进入不同项目看各自的"总的"),PM/GM 差别只在能进的项目集合;RM 暂不做。

## 核心原则(反模式先立此存照)

**禁止"摘要的摘要"级联**(逐级 LLM 概括 = 传话失真 + 不可下钻 + 贵)。上层要的不是更短,是**不同视角 + 更低分辨率**,拆成三条腿:

1. **确定性聚合(骨架,零 LLM)**:计数/状态/趋势全部由 item store(topics/action_items/safety_observations/observations)上的实时 SQL 出——无损、秒级、天然按 memberships/accessible_site_ids 收 ACL。PM"看一眼"的 70% 是这个。
2. **例外升级(安全网,规则不是 LLM)**:触发规则的条目自动冒头,未触发的折叠成计数("另有 4 项低风险")。**高危项永远浮到眼前,概括丢不掉它。**
3. **LLM 叙事合成(血肉,受纪律约束)**:每项目每日一段"今天发生了什么"叙事——**从结构化 item 概括**(不从散文报告);**每条结论锚定 item id 可下钻**;物化存储附生成时间;Claude 直连 API。

## 例外规则 v1(用户 2026-07-07 给定方向)

| # | 规则 | 数据源 | v1 状态 |
|---|---|---|---|
| 1 | 高风险安全项(risk_level=high 未关闭) | safety_observations + observations(kind=safety) | ✅ 直接做 |
| 2 | 影响质量验收的(quality 类未关闭 + 关键词/字段标记) | observations(kind=quality) + topics(category=quality) | ✅ 先做"未关闭 quality 项"粗版 |
| 3 | 影响工期的 | programme 数据 | ⏸ 等 programme 联动,占位 |
| 4 | 词频比 7 日均值突增(如事故/延误词) | topics/chunk 文本统计 | ✅ 做(纯 SQL/统计,无 LLM) |
| 5 | 员工访谈得出的阈值 | — | ⏸ 用户访谈后增补 |

- 阈值配置:v1 **公司级默认值硬编码为常量表**(一处可改);阈值配置 UI(admin 在 settings 配)= v2,schema 预留 config 表不建。

## 两种视图

### A. 项目总览(项目内,PM/SM 视角同页,数据范围随 ACL)
- 顶部:例外条(触发规则的条目,红/橙,点击下钻到原始 item/来源报告);
- KPI 行:开放安全项/行动项/今日 topics 数/参与人数(SQL 实时);
- 叙事段:当日 LLM 合成(物化,标生成时间,每条锚 item);
- 人员折叠:每 SM 一行(今日 topics n / 行动 n / 安全 n),点开才见明细——"不用逐份读"的直接答案。

### B. GM 跨项目一览
- 每项目一行:绿/黄/红(绿=无例外,黄=有未关闭例外,红=有高危例外)+ 例外摘要 + KPI 缩略;
- 点行进入该项目的视图 A;
- **UI 落点:Sprint 9 已建的 Insights/Strategic dashboards(Portfolio/Executive)目前吃 mock——4c 后端就是它们的真数据源,接线而非新建页面。**

## 后端形态

- 聚合/例外:org API 新端点 `GET /api/org/rollup?site=`(视图 A)与 `GET /api/org/rollup/portfolio`(视图 B),纯 SQL,无物化(量小);
- 叙事:每日一次(EventBridge cron,夜间报告后)每项目一条,存新表 `rollup_narratives(site_id, period_date, narrative_md, item_refs jsonb, generated_at)`;白天可选"重新生成"按钮(admin);
- 词频突增:每日统计任务与叙事同 cron,结果进例外接口。

## 依赖与顺序

1. **硬依赖 4b**(live 条目是聚合的数据源;没有 4b,4c 只有夜间数据,"快速了解"退化为隔日);
2. **软依赖身份系阶段①**(org 目录收编让 memberships 边界精确;不阻塞开工,收编后自动更准);
3. 规则 3(工期)等 programme 联动;RM 层、阈值配置 UI、每周/月期叙事 = v2。

**推荐顺序:身份系阶段①(小)→ 4b → 4c。**

## 验证

- 聚合 SQL 单测(FakeConn 模式)+ 例外规则表驱动测试;
- 叙事:一个真实项目日的合成结果人工审(锚点可点、无编造);
- 端到端:视图 B 红黄绿与手工核对一致;PM 账号 ACL 收窄正确(只见所属项目)。
