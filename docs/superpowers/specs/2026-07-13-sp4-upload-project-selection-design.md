# SP4 上传 + 项目选择 设计文档

> 跨两仓契约:后端 `fieldsight-pipeline`(SP4a,先建)+ 移动端 `GrandTime`(SP4b,后接)。本文是二者共享的接口契约。

**日期**:2026-07-13
**状态**:设计已过口头评审,待用户书面审阅本 spec

## 1. 目标

让 GrandTime(F2SP 现场记录仪,替代 SMART-PTT)把录制的视频/音频/照片**直接上传到用户自己的 FieldSight 后端**,并在录制时打上**当前工地(site)标签**,使跨工地同日录制可精确归属。

**为什么必须做**:现有录制数据靠每天定时从 `realptt.com` 拉取进 S3;而 GrandTime 设备上 SMART-PTT 已停用、不再喂 realptt——这些设备的录制**只能靠 App 直推**才能进 FieldSight。这是 GrandTime 可用的必要条件。

## 2. 现状(探查 fieldsight-pipeline @ 310318a 确认)

**已就绪、直接复用**:
- 认证:Cognito 池 `ap-southeast-2_q88pd6XXr` / client `4ratjdjonqm17tln6bs2761ci3`;**idToken 裸放 `Authorization` 头**(不带 "Bearer");`sub` 认身份。一 token 通两网关。
- 工地列表:`GET /api/org/sites`(Org API `wdsgobb7b0`,Aurora,按 ACL 过滤)→ `{sites:[{id(uuid), company_id, name, location, client, industry, icon_s3_key, created_at, archived_at, slug}]}`。
- 当前用户可选工地:`GET /api/org/me` → `{..., site_ids:[uuid,...], scope:"ALL"|"MEMBERSHIPS"}`。
- S3 桶 `fieldsight-data-509194952652`;现有键约定 `generate_s3_key`(`lambda_orchestrator.py:618-712`)= `users/{display_name}/{video|audio|pictures}/{date}/{device}_{timestamp}.ext`(注:photo→`pictures`)。
- sub→users.id:`repositories/users.py:get_user_by_sub`。

**不存在、须新建**:
- 任何接收录制字节的上传端点(现管道是"拉",非"推")。
- 录制媒体元数据表(`capture_records` 后端零对应)。
- S3 键/任何路径里的 site 段。

**刻意不碰**:计划中的 `recording_sessions`(identity 合并分析 spec §3)是**粗粒度归属覆盖账本**(时间窗×设备→site/user,带 source/confidence,无媒体元数据),用于给未打标签的 legacy 数据做事后推断 + admin 覆盖。App 每条录制**直接打 site_id 是更细更准的自归属**,不需要该账本。账本 + `resolve_recording()` 属独立后续工作,SP4 不建。

## 3. 架构

```
[设备] 选当前工地(GET /api/org/sites)→ 存 DataStore
   ↓ 录制(video 分段 / photo / audio)完成
[设备] capture_records 行(含 siteId, uploadStatus=pending)
   ↓ 上传队列(WorkManager,联网约束+退避)
[设备→后端] POST /api/recordings/upload-url  →  建 recordings 行 + 预签名 PUT URL
   ↓
[设备→S3]  PUT 文件到预签名 URL(键 users/{name}/{kind}/{date}/...,与下游管道无缝)
   ↓
[设备→后端] POST /api/recordings/{id}/complete  →  标 uploaded_at
```

site 归属只进 `recordings.site_id`,**S3 键不变**——现有下游(转写/报告/recording-stats 按 user+date 读路径)零改动即可消费 GrandTime 上传的文件。

## 4. SP4a 后端契约(fieldsight-pipeline)

技术栈:Aurora Serverless v2 PostgreSQL 16.4,psycopg 原生(无 ORM),`repositories/*.py`(module 级 `_COLS`,函数首参 `conn`,`dict_row`,参数化 SQL),SAM。

### 4.1 迁移 `NNNN_recordings.sql`(实建时确认下一空号,当前最高 0008 → 应为 0009)

```sql
CREATE TABLE recordings (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id   uuid NOT NULL REFERENCES companies(id),
  user_id      uuid NOT NULL REFERENCES users(id),   -- 由 cognito sub 经 get_user_by_sub 解析
  site_id      uuid REFERENCES sites(id),            -- 可空:允许未选工地时上传,归属后补
  kind         text NOT NULL,                        -- 'video' | 'audio' | 'photo'
  s3_key       text NOT NULL,
  started_at   timestamptz NOT NULL,
  ended_at     timestamptz,
  duration_s   numeric,
  resolution   text,
  codec        text,
  size_bytes   bigint,
  gps_track    jsonb,                                 -- 空;SP-Watermark(#69)填
  client_uuid  text,                                  -- 设备端 capture_records.id,幂等去重用
  uploaded_at  timestamptz,                           -- 空=pending
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX idx_recordings_s3_key ON recordings (s3_key);
CREATE UNIQUE INDEX idx_recordings_client_uuid ON recordings (client_uuid);  -- 重复上传请求幂等
CREATE INDEX idx_recordings_site ON recordings (site_id);
CREATE INDEX idx_recordings_user_started ON recordings (user_id, started_at);
```

配套 `repositories/recordings.py`:`insert_pending(conn, ...)`、`mark_uploaded(conn, id, size_bytes)`、`get_by_client_uuid(conn, user_id, client_uuid)`(幂等)。

### 4.2 端点(挂在 Org API `wdsgobb7b0`)

放 Org API 因:recordings 表在 Aurora、需 sites ACL、需 sub→users.id。须给 `OrgApiFunction` 的 IAM role 加 `s3:PutObject`(+ 可选 `s3:PutObjectTagging`)on `arn:.../fieldsight-data-509194952652/users/*`(现仅 org-assets/* + programmes/*,`template.yaml:729-737`)。

**`POST /api/recordings/upload-url`**
- 认证:idToken → sub → `get_user_by_sub` → user_id + company_id。site_id(若给)须校验属该用户可访问集(ACL)。
- Body:
  ```json
  { "kind":"video", "clientUuid":"<capture_records.id>", "siteId":"<uuid|null>",
    "fileName":"jarley_trainor_20260713_160158.mp4", "contentType":"video/mp4",
    "startedAt":"2026-07-13T16:01:58Z", "endedAt":"...", "durationS":123.4,
    "sizeBytes":20480000, "resolution":"1920x1080", "codec":"h264" }
  ```
- 行为:按 `clientUuid` 幂等——已存在则返回同一 recordingId + 新的预签名 URL(不重复建行)。否则服务端用现有 `generate_s3_key` 约定生成键(display_name 取自 users 行),建 recordings 行(uploaded_at=null)。
- 响应:`{ "recordingId":"<uuid>", "uploadUrl":"<S3 presigned PUT, expires_in 900>", "s3Key":"users/.../..." }`
- 预签名 PUT 须含 `Content-Type` 约束,与客户端 PUT 头一致。

**`POST /api/recordings/{id}/complete`**
- Body:`{ "sizeBytes": 20480000 }`(可选,S3 落定后的真实大小)。
- 行为:校验该 recordingId 属该 user;标 `uploaded_at=now()`,更新 size_bytes。响应 `{ "ok":true }`。
- (备选:S3 事件触发的 lambda 标 uploaded——本期从简,由 App 显式 complete。)

**复用不改**:`GET /api/org/sites`、`GET /api/org/me`。

### 4.3 后端验收要点
- 迁移可 apply/rollback;`recordings` 表 + 索引建成。
- upload-url 端点:合法 idToken 返回可用预签名 URL + 建行;非法/过期 token 401;site_id 不属该用户 → 403;clientUuid 幂等。
- 真机或脚本:PUT 一个文件到返回的 URL 成功落 `users/...`;complete 标 uploaded。
- IAM 最小化(仅 users/* PutObject)。

## 5. SP4b 移动端(GrandTime)

### 5.1 前置(SP4 第一任务,即遗留 #82)
`CognitoAuthManager.silentLogin`/`freshIdToken` 现把所有 refresh 错误都保留登录态。上传依赖 `freshIdToken()`——须让 `CognitoClient.refresh` **区分「auth 失效」(NotAuthorized/token 无效 → 登出回登录页)vs「网络错误」(保留登录态,稍后重试)**。否则会"显示已登录但永远拿不到 idToken"。

### 5.2 选工地
- 登录后拉 `GET /api/org/sites`,按 `GET /api/org/me` 的 `site_ids`/`scope` 得可选集。
- 用户选"当前工地" → 存 DataStore(`{id, slug, name}`)。
- **Home 顶部显示 "Site: <name>"**,可点切换(弹选择器)。
- 每条 `capture_records` 新增/写入 **site 的 UUID**(需给 Room 实体加 `siteId` 列并做 Room 迁移;现有 `siteSlug` 保留作展示或弃用,以 plan 为准)。
- 未选工地:允许录制,site 记 null(后端可空);Home 提示"未选工地"。

### 5.3 上传队列
- 依赖:加 `androidx.work`(WorkManager);HTTP 复用 OkHttp(SP2 已引)。
- `capture_records.uploadStatus` 状态机:`pending → uploading → uploaded | failed`。
- 触发:每段视频/每张照片/每段音频落盘后入队,**实时上传**(产品定:恒实时无开关);Worker 约束 `NetworkType.CONNECTED`,失败指数退避重试。
- 单条流程:`freshIdToken()` → `POST upload-url`(带 clientUuid=capture_records.id 幂等)→ 预签名 `PUT` 文件 → `POST complete` → 置 uploaded。任一步失败 → failed,择机重试。
- 断电/重启续传:开机 CoreService 起来后扫 pending/failed 重新入队。

### 5.4 键位修复(顺带解 #80)
上传恒实时无开关 → `KeyAction.TOGGLE_VIDEO_UPLOAD` 作废。把 `(HardKey.VIDEO, PressType.LONG)` 从 TOGGLE_VIDEO_UPLOAD 改为 `TOGGLE_VIDEO`(与短按一致的录制开关),长按不再"无反应"。移除 TOGGLE_VIDEO_UPLOAD 枚举 + Labels 文案(或保留枚举但不绑定,以 plan 为准)。

### 5.5 UI 上传状态
Files 缩略图网格每项加小角标:pending(待上传)/ uploaded(已上传)/ failed(点可重试)。

### 5.6 移动端验收要点
真机 + 真实账号:选工地 → 录一段视频 + 拍一张照 → 观察 uploadStatus 走 pending→uploaded;S3 里出现对应 `users/{name}/{video|pictures}/{date}/` 文件;后端 recordings 表出现行且 site_id = 所选工地、uploaded_at 非空;断网录制 → 联网后自动补传;长按视频键能起停录制。

## 6. 决策汇总 & 延后

| 项 | 决策 |
|---|---|
| 推进 | 一份契约 spec;SP4a 后端先行 → SP4b 移动端 |
| 元数据表 | 新建独立 `recordings` 媒体表;**不建** recording_sessions 账本 |
| site 归属 | 只进 `recordings.site_id`;**S3 键不变** |
| 上传方式 | 预签名 S3 PUT(视频太大,base64→Lambda 撞 6/10MB 上限) |
| 端点位置 | Org API `wdsgobb7b0`(+ users/* PutObject IAM) |
| 上传时机 | 恒实时无开关,按段/照片,离线队列重试 |
| 长按视频键 | 作废 upload-toggle,改回录制(解 #80) |
| GPS | 只留 `gps_track` 列,实拍延到 SP-Watermark(#69) |
| 幂等 | clientUuid(=capture_records.id)唯一约束,重复请求不重复建行 |

## 7. 风险 / 待实现时确认
- 迁移号:draft 里写的 0008 已被 programme_suggestions 占用,实建须取下一个空号。
- 登录用户须在 Aurora `users` 表有行(get_user_by_sub 命中);若某 Cognito 用户不在 users 表,upload-url 应返回明确错误(而非 500)。
- 预签名 PUT 的 Content-Type 必须与客户端 PUT 头逐字一致,否则 S3 拒签。
- 大视频上传耗时/耗流:WorkManager 约束仅 CONNECTED(不强制 WiFi,产品定实时);后续可加"仅 WiFi"选项(本期不做)。
- `recordings` 与未来 recording_sessions 账本的关系:app 自归属够用;若日后 resolve_recording() 上线要读 app 归属,可从 recordings.site_id 直接取,无需回填账本。

## 8. 交付拆分
- **SP4a**(fieldsight-pipeline):迁移 + repository + 2 端点 + IAM,独立 plan + SDD,后端验收。
- **SP4b**(GrandTime):#82 前置 + 选工地 + 上传队列 + 键位 + UI 状态,独立 plan + SDD,真机验收。
- 本 spec 同步一份到 fieldsight-pipeline `docs/superpowers/specs/`(用户审阅通过后)。
