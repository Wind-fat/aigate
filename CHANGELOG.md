# Changelog

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号从 `v0.3.0` 起按「能用的东西变了」发版，不按提交数发版。

## [Unreleased]

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

- viewer 角色被写权限规则误伤：连「改自己的密码」「退出」都返回 403。
- 只读角色能看到「签发 / 吊销 / 新增成员」按钮，点了必失败——现在按角色隐藏并说明原因。
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