# FieldSight 进度汇报 — 2026-04-01

## 已实现功能 (Production Ready)

| 功能 | Branch | 状态 |
|------|--------|------|
| **AI 报告生成** (日报/周报/月报 + Word) | main | ✅ 生产 |
| **语音管道** (VAD → Transcribe → S3) | main | ✅ 生产 |
| **Ask Agent** — 全局浮动问答面板 (Haiku) | feature/p0-ask-agent-search | ✅ |
| **Knowledge Search** — 跨日期/站点/类别搜索 | feature/p0-ask-agent-search | ✅ |
| **行业词库** — 129 条 NZ 建筑术语 TSV | feature/p0-ask-agent-search | ✅ 代码就绪 |
| **日历截止日** — 报告→日历自动关联+视觉标记 | feature/p1-calendar-priority-onepager | ✅ |
| **主题优先级覆盖** — 用户可调整 AI 分类 | feature/p1-calendar-priority-onepager | ✅ |
| **One-Pager** — 单页 HTML 摘要 (可打印PDF) | feature/p1-calendar-priority-onepager | ✅ |
| **站点仪表盘** — 多站点卡片总览 | feature/p2-dashboard-digest-qaqc-realtime | ✅ |
| **QA/QC 修正** — 用户编辑报告内容 + "已修正"标签 | feature/p2-dashboard-digest-qaqc-realtime | ✅ |
| **近实时处理** — 转录完成自动触发报告生成 | feature/p2-dashboard-digest-qaqc-realtime | ✅ |
| **前端埋点** — EventTracker 7 类事件追踪 | feature/p0-ask-agent-search | ✅ |

## 已铺垫 (API/基础就绪，待部署或完善)

| 功能 | 现状 | 缺什么 |
|------|------|--------|
| **Digest 摘要报告** | API 路由就绪 (POST/GET /api/digest) | 需部署 digest Lambda |
| **QA/QC Layer 2-3** | Layer 1 用户修正完成 | Layer 2: 修正→周报/月报自动传播; Layer 3: 系统从修正中学习改进词库/prompt |
| **自定义词库部署** | TSV + Lambda 代码就绪 | 1h ops: create-vocabulary |
| **Analytics 后端** | 前端 EventTracker 已部署 | S3→Athena 分析管道 |
| **语义搜索 (P0 Phase 3)** | DynamoDB 文本搜索已有 | OpenSearch/Embedding |

## 未实现 Roadmap (新增 3 大 Agent)

| 优先级 | 功能 | 工作量 | 说明 |
|--------|------|--------|------|
| **P2** | **Agent 8: 深度解构分析** | 7-9天 | 跨天/人/主题 pattern recognition，输出 mind map JSON，DynamoDB 数据基础已有 |
| **P3** | **Agent 6: 模板适配** | 7-10天 | 客户上传 DOCX/PDF → AI 解析 → 映射 → 客户格式输出，与 One-Pager 整合 |
| **P3** | **Agent 7: Procore 对接** | 10-12天 | Adapter Pattern，先做 Procore Daily Log 推送，OAuth2 + Secrets Manager |
| P3 | 官网 + 自定义域名 | 1-2天 | www vs app 分离 |
| P3 | 人脸模糊 / H264 批转 / 音频标准化 | 各 1-3天 | 设计完成，待实施 |

## Agent 架构总览

| # | Agent | 状态 | 功能 |
|---|-------|------|------|
| 1 | Pipeline Agent | ✅ 生产 | Download → VAD → Transcribe → Report |
| 2 | Ask Agent | ✅ 生产 | 报告问答 (Haiku) + 全局浮动面板 |
| 3 | QA Agent | ✅ L1 | 用户修正 UI + API; Layer 2-3 待实施 |
| 4 | Analytics Agent | 🟡 前端就绪 | EventTracker 已部署; 后端分析待建 |
| 5 | Digest Agent | 🟡 API 就绪 | POST/GET /api/digest; Lambda 待部署 |
| 6 | Template Agent | ⬜ P3 | 客户 DOCX/PDF 模板适配 |
| 7 | Platform Agent | ⬜ P3 | Procore/Aconex/SafeBase 对接 |
| 8 | Analysis Agent | ⬜ P2 | 深度 pattern recognition + mind map |

## 技术决策

- **Agent 框架**：三个新 Agent 均用 **Lambda + Claude API 直调**，不引入 LangChain/Dify/Strands（保持架构一致，避免 cold start 和包膨胀）
- **下一步**：Agent 8 优先（无外部依赖、差异化最强）→ Agent 6（需客户模板）→ Agent 7（需 Procore 开发者账号）

## Branch 结构

```
main (初始 + 基础设施)
 └─ feature/p0-ask-agent-search         ← Ask Agent + Search + EventTracker
     └─ feature/p1-calendar-priority-onepager  ← 日历 + 优先级 + One-Pager
         └─ feature/p2-dashboard-digest-qaqc-realtime  ← Dashboard + Digest + QA/QC + 近实时
             └─ claude/review-feature-content-hsaO3     ← Roadmap 更新 (Agent 6/7/8)
```
