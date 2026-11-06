# Changelog

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号从 `v0.3.0` 起按「能用的东西变了」发版，不按提交数发版。

## [Unreleased]

### Added

- **供应商密钥可在控制台录入**（`PUT/DELETE /admin/v1/providers/{name}/secret`）。
  Fernet 加密落库，主密钥 `AIGATE_SECRET_KEY`；读接口只回 `key_source` 与末四位 `key_hint`。
  解析顺序：库内密文 → `api_key_env` 环境变量。解决"运维拿到 key 却没法填进去"。
- **OpenAI 作为内置供应商**（`openai`），以及任意 OpenAI 兼容端点接入（Azure / 聚合网关 / 自建）。
  `AIGATE_OPENAI_API_KEY` / `AIGATE_OPENAI_BASE_URL`，控制台可直接录入。
- **模型与单价登记**（`POST/PUT /admin/v1/models`）：接新供应商后的模型不必再靠种子脚本，
  否则路由页会以"候选不在 model_prices 里"拒绝添加。
- **模型对比页**（`/models`）：近 N 天真实调用的成本/成功率/P95/相似度，数据来自
  `usage_records` 与 `shadow_runs`；没跑过影子的模型相似度显示 `—`。
- 供应商页重做：卡片式、状态灯、密钥来源徽标、内联录入/更换/清除、测试连通结果带下一步建议、
  模型单价 chips、被哪些虚拟模型使用。

### Security

- 控制台 `/api/admin/*` 代理的机器令牌回退改为显式开关 `CONSOLE_ALLOW_TOKEN_FALLBACK`
  （生产 compose 显式设为 0）。此前任何能访问 3000 端口、没有 cookie 的请求都能借代理拿到控制面。
- 模型对比端点要求 `usage:all`：财务可见，开发者不可见。

### Fixed

- **换身份登录后侧栏仍显示上一个身份**：`useSession()` 的模块级缓存从不失效。
  新增 `clearSession()`，登录与退出各调一次（服务端权限一直是对的，错的是界面）。
- 供应商"测试连通"在无密钥时只提示环境变量为空，现在同时提示"控制台未录入"。

### Security

- **`POST /admin/v1/keys` 与 `/keys/{id}/revoke` 之前没有鉴权**：任何登录会话（含只读开发者）
  都能给任意项目签发 key、吊销任意 key，且返回 201。现在：owner/admin 走 `keys:write`；
  开发者走 `keys:own`，且只能签自己 `project_scope` 内的项目、只能吊销自己签的 key。

### Added

- key 的归属与生命周期：`created_by` / `created_by_email` / `description` / `environment` /
  `expires_at` / `revoked_reason` / `revoked_by`（迁移 `e1a2b3c4d5e6`）。prod key 必须有过期时间。
- 过期 key 在数据面返回 `401 gateway.key_expired`（区别于 `invalid_key`，错误码直接指向"去换一把"）。
- `PUT /console/v1/users/{id}`：改角色、分配可见项目、停用/启用、重置密码。三条护栏：
  只有 owner 能改 owner；不能改自己的角色或停用自己；viewer 必须至少一个**存在**的项目。
- Key 页重做：环境徽标、过期时间、"60 天未用 / 14 天内过期"计数、搜索与状态过滤、
  一次性明文带复制按钮、吊销填原因、无法吊销的行给出"仅签发人可吊销"而不是一个必 403 的按钮。
- 成员页：可见项目列（空 scope 直接标红「未分配」）、行内编辑、重置密码入口。
- `POST /admin/v1/teams` 与 `POST /admin/v1/projects`：在控制台就能开新团队/项目，
  不用再改数据库。slug 强制小写字母/数字/连字符（它会进账单、CSV 和 URL），
  团队内项目 slug 唯一，重复返回 409。
- 控制台 `adminPost` 支持 JSON body；错误 toast 从「只显示 code」改成「message（code）」——
  `gateway.key_name_taken` 这种码对用户没有意义。
- `as_aware()`：SQLite 返回 naive datetime、PG 返回 aware，二者相减会 TypeError 变成 500；
  所有读库时间比较统一走它。

### Fixed

- 模型白名单 `a, b` 里的空格会让 key 永远 `model_not_allowed`（表现为"key 用不了"）。
- 重复签发同名 key 现在返回 409 而不是静默创建第二把。

### Added

- **路由策略真正生效**：`cost_first` 按（输入+输出）单价排序，`latency_first` 按近 24h 平均延迟排序，
  没有历史的候选排最后（"未知"不等于"快"）。降级链不参与排序——它的顺序是刻意的。
- **三级配额**：key / project / team 各自的 RPM（0=不限），按「窄 → 宽」顺序检查，
  429 的 `scope` 字段说明是哪一级。团队额度是跨项目共享的。
- **日预算**：`projects.daily_budget_cny`。防的是「凌晨跑飞的循环」——月预算要到月底才发现。
- **80% 告警**：`alerts` 表按（范围/指标/周期）唯一索引去重；概览页顶部展示，可确认。
- **prompt 版本**：`X-Prompt-Version` 参与缓存 key，写入 `usage_records.prompt_version` 与 trace。
  改版本即整体失效，不需要清缓存。
- **原始 prompt 开关**：`AIGATE_TRACE_PROMPT_MODE=raw` 才存用户原文；默认只存脱敏文本。
- **影子流量**：路由可配最多 2 个影子候选，响应返回后异步调用，记录延迟/成本/字符相似度；
  单独表 `shadow_runs` 与 `/admin/v1/shadow[/summary]`，调用链页显示对照。
- **质量-成本对照**：`scripts/quality_cost.py` 用真实数据出表，`docs/quality-cost.md`。
- **Grafana 看板**：`deploy/grafana/`（含"为什么没有 p95 与金额"的说明）。
- `docs/SPEC-COVERAGE.md`（逐条核对 + 偏差说明）、`docs/INTERVIEW.md`（难点→代码→证据）。

### Added

- **四个角色 + 数据级隔离**：新增 `finance`；`viewer` 绑定项目范围（`project_scope`），
  API 按范围过滤用量、调用链与 Key。开发者看不到公司成本、审计、路由、供应商；
  财务看得到全公司成本但看不到 prompt、Key、路由。能力表 `CAPABILITIES` 是唯一来源，
  控制台导航、页面守卫与接口守卫都从它派生。
- **供应商页**：把「来源」变成可管理的对象——通义千问（DashScope）/ DeepSeek / 自建 vLLM /
  离线 mock，各自的 base_url、密钥所在环境变量、模型与单价、被哪些路由使用，以及
  **一键真打 `/models` 证明可用**（401/404/超时都原样返回并给下一步）。
  运行时读库，改完下一条请求生效，不需要重启。
- Key 签发可限定允许的模型（`allowed_models`），Key 列表显示可用范围。
- `tests/test_migrations.py`：从空文件只跑 alembic，验证 upgrade/downgrade/幂等。

### Changed

- 登录后按角色落点：开发者去工作台，其余去概览。
- 供应商停用后，指向它的请求返回 `provider_disabled`，而不是笼统的 `no_candidate`——
  值班看到错误码就知道该去哪个页面。

### Fixed

- `console_users.project_scope` 与 `provider_configs` 之前只存在于模型里，
  真机 `alembic upgrade head` 之后登录 500（表在但列不在）。迁移补齐，并加了
  只跑迁移的测试，防止再靠 `create_all` 兜底。

## [0.3.0] — 2026-11-06

这一版的主题：**从「能演示」到「团队能接手」**。

### Added

- **控制台账号**：`owner / admin / viewer` 三角色，stdlib scrypt 口令（参数写进哈希串），
  无状态 HMAC 会话 cookie。轮换 `AIGATE_SESSION_SECRET` 可让所有人立刻下线。
- **只追加审计**：`/admin/v1/*` 与 `/console/v1/*` 的写操作各留一行
  （actor / role / action / target / status / ip / request_id），没有删除或修改接口。
  actor 由鉴权依赖写进 `request.state`，所以行里是邮箱而不是一串 id。
- **生产校验**：`AIGATE_ENV=prod` 时占位 token、过短 secret、`COOKIE_SECURE=false`
  任一存在就拒绝启动。
- **预算与限流可改**：`PUT /admin/v1/projects/{id}`，控制台「预算配额」页行内编辑；
  新增「暂停项目」——保留 Key 与历史，立即 403 停计费。
- **路由可改**：`PUT /admin/v1/routes/{match_model}` + `POST /admin/v1/routes`，
  改完下一条请求即生效（路由策略是每请求读库的）。写入时校验 provider 是否注册，
  未定价模型只告警不拦。
- **账单导出**：`GET /admin/v1/usage/export.csv`，带 `Content-Disposition` 与行数头，
  并对以 `= + - @` 开头的字段做公式注入转义。
- **调用明细**：时间窗口 / 状态 / 模型 / 项目四维筛选、分页（`offset` + `total`）、
  一键跳转到该请求的调用链。
- **调用链瀑布图**：按累计时间定位的堆叠条，并显式画出「未计入（框架/网络）」那一段，
  避免 644ms 与 596ms 两个数字对不上的困惑。
- **控制台新页面**：模型路由（候选链上下移、增删）、预算配额。
- `scripts/check_deploy.py`：不依赖 Docker 的部署静态检查（构建上下文、COPY 源、
  必填环境变量、端口冲突、`depends_on` 健康检查、生产必须 postgres+redis）。
- `DEPLOY.md` / `ARCHITECTURE.md` / `CONTRIBUTING.md` / `SECURITY.md`。

### Changed

- 控制台改成 `app/(shell)` 路由组 + `app/login`，登录页不再有侧边栏；
  admin token 只活在 `console/.env.local`，**不再进浏览器 bundle**。
- `scripts/seed_demo.py` 同时写 `request_traces`，并改为按 `req_demo_` 前缀幂等重建：
  演示数据点进去能看到调用链，而不是「trace not found」。
- 错误响应保留结构化附加字段（如 `unknown: [...]`），控制台可以指出「哪个候选错了」。
- `docker-compose.yml` 补齐完整栈：db / redis / migrate / gateway / console，
  启动顺序由健康检查驱动；spool 挂到独立卷上。

### Fixed

- **迁移漏表**：`console_users` / `audit_logs` 之前只靠 `create_all` 兜底，
  真机 `alembic upgrade head` 会少两张表。新增 revision `8c1f2d4a9b31` 补上，
  并在 SQLite（upgrade / downgrade base / 再 upgrade）与 PostgreSQL 离线 DDL 三处验证。
- viewer 角色被写权限规则误伤：连「改自己的密码」「退出」都返回 403。
- 只读角色能看到「签发 / 吊销 / 新增成员 / 编辑路由 / 改预算」按钮，点了必失败——
  现在统一走 `lib/session.ts` 的 `canWrite`，按角色隐藏并说明原因。
- 审计表 7 列横向溢出 1180px 主列，截图带横向滚动条。
- `.local` / `.internal` 邮箱被 `EmailStr` 判 422，内部工具无法建账号。
- 路由编辑器打开时会把「未定价但已在用」的候选静默替换成第一个选项。

## [0.2.0] — 2026-11-05

### Added

- 记账崩溃可恢复：NDJSON spool → 批量 `executemany` → 提交后 ack → 启动回放按
  `request_id` 去重。`kill -9` 不丢账（ADR-0008）。
- 批量异步记账：20 并发 × 200 请求下 p95 1500ms → 687ms，错误归零（ADR-0007）。
- Alembic 全量迁移（upgrade / downgrade / 幂等均已验证）。
- 评测执行器（smoke + regression 20 例，失败退出码 1）、压测脚本（Python / k6）、
  547 行值班手册。
- 语义缓存 L1/L2 + 阈值挂在 embedder 上（ADR-0005）。

## [0.1.0] — 2026-11-04

### Added

- OpenAI 兼容 `/v1/chat/completions`、流式 SSE、虚拟模型 + 候选链 + 降级。
- 多租户 Key（只存 SHA-256）、令牌桶限流、月度预算硬限。
- PII 脱敏、熔断、Trace、Prometheus 指标、Next.js 控制台 5 页。
- 93 项测试 + 23 项端到端冒烟断言 + 三 job CI。