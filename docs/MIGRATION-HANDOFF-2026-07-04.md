# FieldSight 会话迁移交接文档(2026-07-04)

> 上一份交接:`fieldsight-ui/docs/MIGRATION-HANDOFF.md`(2026-06-30)。本文档覆盖其后全部进展。
> 恢复工作先读:本文档 → 两仓 `.superpowers/sdd/progress.md` 台账 → 长期记忆(自动加载)。
> 账号铁律:一切都在**用户自己的 `509194952652`**(ap-southeast-2);公司的 `164088480050` CDK 账号**不碰**。

---

## 1. 当前状态一句话

**Phase 0/1/2A/2B/3(后端)全部完成**:新前端(Amplify dev)跑真实数据真实登录;TEST 后端管线全绿可复现;Postgres+pgvector 数据层代码+真库全部上线;**Phase 3 组织写后端已上线 TEST 并冒烟通过**(OrgApiFunction `/api/org/*` in-VPC 直连 Aurora,种子回填 company+4 用户+4 站点+memberships,`/me`·`/sites`·`/members` 均 200)。**下一步 = Phase 3 的 UI 接线(fieldsight-ui 仓,另起计划)+ Chrome 全流程验证;Phase 4(抽取→Postgres)有用户审核门。**

## 2. 活着的 Infra(全部 509194952652 / ap-southeast-2)

| 资源 | 标识 | 说明 |
|---|---|---|
| **Aurora 集群** | `fieldsight-db-test`(栈)/ 集群 `fieldsight-db-test-dbcluster-hywiixu8ihi9` | Serverless v2 **PG16.4**,min **0** ACU(闲时≈存储费<$1/月),**Data API 开**(Console Query Editor 可直查),RDS 托管密钥 `rds!cluster-1757a281-...`;endpoint `...cluster-ctugu28wme3y.ap-southeast-2.rds.amazonaws.com`;**已应用 2A 全部 4 个迁移**(10 表、vector+pgcrypto、HNSW 索引),幂等验证过 |
| **TEST 应用栈** | `fieldsight-test`(SAM,src/template.yaml canonical) | 10 函数:orchestrator/downloader/transcribe/report-generator/transcribe-callback/meeting-minutes/ask-agent/api/fargate-trigger/**migrate**;含 test Cognito 池、FieldSightApi 网关;S3 事件已接线(无 VAD——没传 VadLayerArn);冒烟通过 |
| **PROD(手工搭建,非 IaC)** | API GW `khfj3p1fkb`(stage prod)、10 个 `fieldsight-*` Lambda、CloudFront `E12IVML224YUEE` | **别用 sam deploy 碰**;代码更新走 `deploy-prod-code.yml`(main 分支,update-function-code) |
| **Cognito(prod)** | 池 `ap-southeast-2_q88pd6XXr`(fieldsight-users)/ client `4ratjdjonqm17tln6bs2761ci3` | 文档里旧池 `ps7XIQGHB` **已删除**;4 用户全是用户的邮箱别名:admin=`benl.tech@outlook.com`(Ben Lin),`benlin.chch+jt`(Jarley/site_manager),`+db`(David),`+test`(Ben Test) |
| **前端** | Amplify `d2fssznicvuckr`,分支 dev → https://dev.d2fssznicvuckr.amplifyapp.com | env vars:`FS_BASEURL=https://khfj3p1fkb.execute-api.ap-southeast-2.amazonaws.com/prod/api`、`FS_USEMOCKS=false`、`FS_WRITEMOCKS=true`;构建时生成 env.js |
| **数据桶** | `fieldsight-data-509194952652`(prod)/ `-test-`(test) | 报告 2-6 月分属多人(Jarley 只有 2-3 月);**2-3 月转录已不在 S3**(Ask 靠报告文本);DynamoDB items 表 0 行(服务端 /api/search 无数据源) |
| **DynamoDB** | fieldsight-{users,audit,items,reports,transcripts} + test 前缀版 | audit 表被 UI 复用做 flag resolve(键 `<topic_id>_flag_<idx>`/`-1_obs_<idx>`) |
| **CI/CD** | pipeline:develop→test(deploy.yml,OIDC 角色 `github-actions-fieldsight-deploy`),main→prod 代码(deploy-prod-code.yml),test.yml(pytest+pgvector 容器),ci.yml(cfn-lint) · ui:dev→Amplify webhook | pipeline 根 template.yaml 已退役;CI Python=3.11(必须匹配 Lambda runtime) |

## 3. 已完成的 Phase(with 关键事实)

- **Phase 0(读侧真数据)**:UI 的 `_fetch` 裸 ID token(无 Bearer、非 access token!)+ `X-Request-Id` 仅同源(网关预检白名单只有 Content-Type,Authorization)+ 真实池/client 修正。写路径 `writeMocks` 隔离(10 个无后端写函数),action toggle 真上线(`POST /actions/toggle`,4 调用点已核)。会话桥(AuthMock←FS.session)+ 默认落地页在桥后计算。
- **UI 反馈批 ×2**:数据窗口层 `FS.api.window`(months=24)、DatePicker 范围模式+hover 预览、RangeToolbar(Today/7d/30d/All/Custom)接入 Safety/Quality/Evidence/Insights(默认 All)、Today 空态 CTA、搜索面板 topic 索引(字段是 `topic_title`!)+ Ask 移交行、safety 观察去重(生成器把同一事件双写进 `safety_observations` 和 topic `safety_flags`,聚合层模糊去重保留可定位版)、flag resolve/reopen(piggyback actions-toggle)、deep-link 字符串 id 匹配。
- **Phase 1(IaC)**:根模板退役(PR #7);SAM 托管栈修复(OIDC 角色缺 S3 权限→桶建失败→栈卡死;`fix-sam-deploy-role.sh`);wire-s3-events 两个 bug(空配置返回空串炸 jq;给不存在的函数接线且 `||echo` 吞错);TEST 部署 6/14 以来首绿。
- **Phase 2A(数据层代码)**:PR #6;migrations 0001-0004 + db/ + repositories/(psycopg 原生,无 ORM)+ ACL 纯逻辑 + 检索 SQL 守卫测试 + lambda_migrate;21 测试(CI pgvector 容器);**已定语义**:upsert_user None=不覆盖、admin/gm=公司级(均已实现+对抗测试)。
- **Phase 2B(真库)**:见 §2 第一行。跨栈导出统一 `${AWS::StackName}-*` 派生命名。

## 4. 坑清单(新会话必读,别重踩)

除 pipeline CLAUDE.md 的 BUG-01~36 外,本轮新增/强化:
1. **BUG-35**:中文 Windows AWS CLI 用 GBK 读文件 → `export AWS_CLI_FILE_ENCODING=UTF-8 PYTHONUTF8=1`。
2. **BUG-36**:VPC Lambda 无 NAT/端点时任何 AWS API 调用黑洞至超时且**零日志**。凭据一律部署时注入(`{{resolve:secretsmanager:${Param}}}`,ARN 走 Parameter 而非 ImportValue——动态引用不与 ImportValue 组合);cfn-lint E1051 连**注释里的** resolve 字面量都抓。
3. SAM layer 构建:CI Python 版本必须=运行时(3.11);pgvector(python 包)硬依赖 numpy 且 SAM 构建器解析不了 → 迁移 layer 只装 psycopg,`connection.py` 的 register_vector 是可选导入(Phase 4 要绑向量的函数需自己解决 pgvector 打包——考虑容器镜像或预编译 layer)。
4. 跨栈命名:导出一律 `!Sub '${AWS::StackName}-X'`,消费端 `${DbStackName}-X`,两端天然对齐。
5. 部署角色最小权限增量:sam 托管桶 S3、test 桶 PutBucketNotification、cloudformation:ListExports、`rds!cluster-*` GetSecretValue——全部限定资源。
6. UI 侧:mock 时代形状≠真实契约(title vs topic_title、单 token 类型、时间窗口以"今天"为中心 vs 历史数据)——**任何 mock→live 切换都要在真浏览器验证**(Claude Chrome 极高效:页面内 A/B 实验直接定位 X-Request-Id 案)。
7. 本地 checkout 状态:提交前必查 `git branch --show-current`(曾推错分支);用户的未提交 roadmap 笔记(monorepo 备选)在 pipeline 工作区,**保护它**。
8. gh pr merge 本地脏区会 Abort → 用 `gh api -X PUT .../merge`。

## 5. Phase 3 —— 后端已上线 TEST(2026-07-04),UI 接线待起

> **已完成(commit 链 a48d8b6..PR#9 squash 2a23eda + seed fix PR#10)**:计划 `docs/superpowers/plans/2026-07-04-phase-3-org-api.md`(11 任务,逐任务审查 + 全分支终审,抓修 2 个必炸级:SAM transform 嵌套 providerARNs、create_member 跨租户改嫁)。
> - **DB 栈**:db-template 加 cognito-idp interface(单 AZ ~$8/月)+ S3 gateway endpoints,均 available(BUG-36 出口)。
> - **OrgApiFunction**(`fieldsight-test-org-api`,in-VPC,PsycopgLayer):`/api/org/{me,sites,members,members/{sub}/role,upload-url,asset-url}` 挂 test `FieldSightApi`;dual-pool authorizer(test 池 + prod 池 `q88pd6XXr`)。
> - **OrgSeedFunction**(`fieldsight-test-org-seed`,手动 invoke,幂等):已回填 Aurora = company `FieldSight` + 4 用户(benl.tech=admin/+jt·+db=site_manager/+test=worker)+ 4 站点(线上 config 含 Mangere,比 repo 多 1)+ 2 memberships。
> - **冒烟(合成 admin claims 直接 invoke)**:`/me`(scope=ALL,4 site_ids)、`/sites`(4)、`/members`(4 含 join)全部 200,证明 in-VPC→Aurora 连通 + ACL + join。
> - **仓储扩展**:users(list/set_global_role 公司守卫/update_profile)、memberships(ensure_membership/list_company_memberships 双 JOIN 防越权)、sites/companies 查询。asset key 服务端生成属主前缀隔离。
> - **CI/CD**:deploy.yml 加 `OrgUserPoolId` + Wire-bucket-CORS;deploy role 加 `s3:PutBucketCORS`(test 桶);test 桶补 `config/user_mapping.json`。
>
> **未做/待办**:① 端到端过网关(真 idToken 走 dual-pool authorizer)—— 留给 UI 计划的 Chrome 验证;② **UI 接线(下一个计划,fieldsight-ui 仓)**:env.js 加 `FS_ORG_BASEURL`(指 test 网关 `wdsgobb7b0`)+ `FS_ORGWRITES` 开关(只放行 org 写,其余仍 mock)、team/settings/sites 页面接真实 org API、admin fan-out 换真实成员源、头像/图标走 presign;③ seed 二跑会重置映射角色(API 改过的会被覆盖);④ post-merge backlog:连接缓存+parse-before-connect、asset-url 租户隔离(第二家公司入库前)、CORS 通配收紧(prod 前);⑤ **test 栈 OrgApi 持有 prod 池 `AdminCreateUser`**——"test 不碰 prod"的唯一有意例外(建真实登录 + 发真实邮件邀请)。

### 原始架构定稿(存档)

**目标**:项目/成员/角色/资料/图片的真实写后端,UI 写流程去 mock。

**已定架构**:
- **新 `OrgApiFunction`**(src/template.yaml,`HasDb` 门):in-VPC(psycopg 直连 Aurora),路由挂 test 栈 FieldSightApi 的 `/api/org/{proxy+}`(Cognito authorizer);**不碰手工 prod 网关**。
- **网络已探明**:悉尼区 `cognito-idp` **有** VPC 接口端点(bedrock 系也有)→ 单函数方案成立:加 cognito-idp interface endpoint(~$8/月,放 db 栈)+ S3 gateway endpoint(免费);presign 本身是离线签名不需网络;DB 凭据沿用 BUG-36 的部署时 PG* 注入。
- **端点集**:GET/POST `/api/org/sites` · POST `/api/org/members`(cognito admin-create + upsert_user + add_membership;角色服务端校验,防提权) · PATCH `/api/org/members/{sub}/role`(显式 set_role,ACL=admin/公司级) · GET/PATCH `/api/org/me`(资料) · POST `/api/org/upload-url`(presigned PUT,头像/项目 icon → data 桶 `org-assets/`,key 存 users.avatar_s3_key/sites.icon_s3_key)+ GET asset-url。
- **双基址**:UI env.js 增 `FS_ORG_BASEURL` 指 test 网关(org 数据在 Aurora=唯一 org 库),报告读取仍走 prod;`writeMocks` 保持 true,新增 org 专用开关(如 `FS_ORGWRITES`)只放行已有后端的 org 写,programme/safety-create 等仍 mock。
- **种子任务**:公司行 + 从 Cognito 池(4 用户)与 user_mapping(4 站点)回填 Aurora,否则 UI org 页面为空。
- **任务切法(草案,开工时 writing-plans 细化)**:T1 端点/VPC(db 栈加 endpoints)→ T2 OrgApi 骨架+路由+模板接线 → T3 各端点 TDD(handler 层 mock conn;仓储已有集成测试)→ T4 种子回填 → T5 UI 接线+开关 → T6 Chrome 全流程验证(建项目/加成员/改角色/传图,刷新持久)。

## 6. Phase 4 —— 有用户审核门

**开工前必须**:用真实报告(建议 2026-03-02,19 topics)按已定切片策略产出**样例**(topic 块/transcript 窗口/metadata/超长切分+overlap 演示)交用户审核(已写入 roadmap Phase 4 入场门)。策略本体在 roadmap Phase 2 步骤 5(topic 自然长度、窗口 500-800、Contextual Retrieval、small-to-big)。**待定**:topic 去重键(upsert_topic 现为纯 insert,重跑会重复)。注意:抽取函数要绑向量 → pgvector/numpy 打包问题(见坑 3);Bedrock 接口端点悉尼可用。

## 7. 为什么 HNSW(用户问,决策论证归档)

pgvector 只有两种 ANN 索引:**IVFFlat** 和 **HNSW**(加不建索引=精确扫描)。选 HNSW:
- **IVFFlat 需要"训练"**:建索引时按已有数据聚类分桶(lists 参数),**空表/持续增长的表**上先建会严重失准,数据漂移后要重建——我们的 report_chunks 恰好从零开始持续增长;
- **HNSW 图结构免训练、增量插入友好**,召回-延迟曲线普遍优于 IVFFlat,是当前行业默认;
- 代价(建得慢、内存高)在我们的量级(千~万块)无感;
- 库外方案(FAISS/独立向量库)在 D4 已否——ACL 与向量必须同库一条 SQL。
诚实说:当前数据量连精确扫描都够快,HNSW 是"建好就不用再想"的正确默认,不是性能刚需。

## 8. 零散待办(全部记录在案)

- **logo**:等文件落到 `Dropbox\fieldsight-ui\assets\logo.png`(用户在另一台机上,需存进 Dropbox 同步目录;2026-07-04 搜索未见)→ 接线侧栏 F 方块(left-nav.js:352 logoMarkStyle)/登录页/favicon;
- **Library 种子隐藏**(live 模式下藏 template-store.js 的 localStorage 演示模板);
- toggle 响应无 checked_by/at → 总线事件为 null(可本地补当前用户);
- admin 聚合 fan-out 在 live 用 mock 用户列表(fixtures.sites.users)——Phase 3 org API 上线后改为真实来源;
- getSpan 不透传 403;components-preview 未注册 RangeToolbar;pipeline 仓 `ui/BACKend-CONTEXT.md`(subtree)仍留死池 ID;
- prod 手工资源纳管 IaC(Phase 1 递延,B/C 方案见 2026-07-03 侦察);test 栈补 VadLayerArn;
- 用户未提交的 roadmap monorepo 笔记仍在 pipeline 工作区(勿丢)。

## 9. 工作方式(经验证有效,新会话沿用)

- 中文回复;汇报格式:已完成/如何/影响(记忆里有);
- 子代理流水线:writing-plans 展开 → 实施(计划含完整代码用 haiku,判断型用 sonnet)→ 逐任务审查(sonnet)→ 修复循环 → 全分支终审(最强模型);审查抓出过 4 个必炸级问题,流程价值已证明;
- 台账:两仓 `.superpowers/sdd/progress.md`(compaction 后以台账+git log 为准);
- UI 验证用 Claude Chrome(用户两台机,注意 list_connected_browsers 选对);AWS 会话常过期,让用户 `! aws login`;
- 权限门会拦 IAM/删栈/改凭据类操作——向用户要明示批准或给命令让其 `!` 自跑。
