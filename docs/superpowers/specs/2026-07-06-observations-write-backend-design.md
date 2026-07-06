# 批次 B:报告侧写后端(Observations)设计 spec

> 一页纸,等用户过目后进 writing-plans。存储方案 = 用户拍板的**选项 3:Aurora(org 库)**——观察条目是 dashboard-first item store 的第一批公民,不走 S3/DDB 的二次迁移弯路。

## 目标

Safety / Quality 页的 "+ Raise Observation" / "+ 新增质量条目" 真正可用:提交 → 落 Aurora → 刷新持久 → 与报告提取的条目**合并显示**在既有列表/KPI 里。(Templates/Library 上传不在本批——另立。)

## 数据模型(migration 0006)

```sql
CREATE TABLE observations (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id    uuid NOT NULL REFERENCES companies(id),
  kind          text NOT NULL CHECK (kind IN ('safety','quality')),
  -- 报告侧身份(文本列,身份系合并前的桥):
  site_slug     text NOT NULL,            -- 报告侧站点 slug(如 sb1108-ellesmere)
  report_date   date NOT NULL,            -- 归属日期(默认创建日,NZ 时区)
  author_sub    text NOT NULL,            -- Cognito sub(登录身份)
  author_name   text NOT NULL,            -- 显示名快照
  -- 内容(与报告提取的 safety_flags / quality 条目同构):
  observation   text NOT NULL,
  risk_level    text CHECK (risk_level IN ('low','medium','high')),   -- safety 用
  recommended_action text,
  status        text NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed')),
  archived_at   timestamptz,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_obs_company_kind_date ON observations (company_id, kind, report_date);
CREATE INDEX idx_obs_site ON observations (company_id, site_slug, report_date);
```

## API(org 网关,lambda_org_api.py 增路由)

| 路由 | 权限 | 说明 |
|---|---|---|
| `POST /api/org/observations` | 登录即可(worker 也能上报) | body `{kind, site_slug, observation, risk_level?, recommended_action?, report_date?}`;author 取 caller |
| `GET /api/org/observations?kind=&from=&to=&site_slug=` | 登录即可 | 公司内按窗口/站点过滤;返回列表(与 UI 现有条目形状对齐) |
| `PATCH /api/org/observations/{id}` | author 本人或 admin/gm | `{status?}`(open/closed);其余字段不改(轻量 v1) |
| `POST …/{id}/archive` | admin/gm | 软删(复用既有模式) |

权限说明:读取范围 v1 = 全公司(与报告聚合的可见性由前端按 site 过滤对齐);细粒度 ACL(worker 只见自己站)留 v2——当前公司就一个,先不过度设计。

## UI 接线

1. `api/org.js` 加 `createObservation/getObservations/updateObservation/archiveObservation`(orgWrite/orgLive 门控照旧;mock 分支返回本地对象,mock 演示不破)。
2. `safety-create-modal.js` / `quality-create-modal.js`:live 提交路径改调 `org.createObservation`(kind 对应);`siteId` 用 **FS.siteContext.get()**(A2 现成!未选项目 → 模态里加个必选站点下拉,选项来自 getSites)。成功 → toast + 触发页面 refetch。
3. **读路径合并**:compliance-aggregator 的 getSafetyRange/getQualityRange 在返回前追加 `org.getObservations({kind, from, to, site})` 的条目(转成与报告提取条目同构的行,带 `source:'manual'` 标记 + 作者名);页面零改动即显示,KPI 计数自然并入。战略页/Insights 复用同一出口 → 手动条目自动进全局视图(合理:它们本来就是真实事件)。
4. 手动条目的行 UI:复用现有 flag 行,加个小 "Manual" 徽章(区分报告提取);status 勾选走 PATCH。

## 关键取舍(记录)

- **身份桥**:site_slug/author 是报告侧身份的文本快照进 org 库——设备转交/身份系合并批会统一,列已预留兼容。
- **读合并在前端聚合器**(而非后端跨库 join):报告在 S3、观察在 Aurora,当前规模前端合并最简;Phase 4 落 report_chunks 后可下沉。
- worker 上报权限开放(工地一线本来就是上报主体);archive 收紧到 admin/gm。
- Templates/Library 上传另立批次(涉及文件存储,与本批数据形状无关)。

## 验证

后端:pytest 单测(路由/权限/校验)+ 部署后 live 冒烟(建→查→关→归档)。前端:node --check + Chrome(选项目 → Raise Observation → 刷新持久 → Safety 列表含 Manual 徽章条目、KPI +1;Insights 也能看到)。
