# 规格核对表（对着需求逐条自查）

这张表存在的理由：需求清单里最容易骗自己的一句话是「做了」。所以每条都给**代码位置**和
**能跑的验证**；凡是与原始规格有偏差的，写清偏差和原因，不算完成。

图例：✅ 已实现且有测试 ｜ ⚠️ 实现了但与规格有偏差 ｜ ❌ 未做

---

## MVP · P0

| # | 需求 | 状态 | 代码 | 验证 |
|---|---|---|---|---|
| 1 | 统一入口 `POST /v1/chat/completions`，OpenAI 协议兼容 | ⚠️ 仅文本补全层 | `api/v1_chat.py`、`schemas.py:SUPPORTED_PARAMS` | `tests/test_param_passthrough.py`：`top_p`/`stop`/`user` 用假上游断言**上游收到的 body**；`tools`/`tool_choice`/`response_format` 等返回 400 `unsupported_parameter`（以前是收下就丢）。带 function calling 的 SDK 不能只改 `base_url` |
| 2 | 多 provider（DashScope / DeepSeek / vLLM）+ 虚拟模型名映射 | ✅ | `providers/`、`provider_config.py`、`route_policies` | `tests/test_roles_and_providers.py::test_providers_are_seeded_and_masked`；控制台「供应商」页 |
| 3 | 路由策略：成本优先 / 延迟优先 / 指定模型 | ✅ | `v1_chat._order_by_strategy` | `test_cost_first_actually_picks_the_cheaper_candidate`、`test_latency_first_uses_recent_history`、`test_static_keeps_the_configured_order` |
| 4 | 主模型 5xx / 超时 → 自动切备用 | ✅ | `v1_chat` 候选循环 + `core/circuit.py` | `tests/test_chat_endpoint.py`、`tests/test_circuit.py`、`smoke.sh` 第 4 段 |
| 5 | 语义缓存：embedding 相似度、阈值可调、命中记 `cache_hit` | ✅ | `core/cache.py`、`core/embedding.py` | `tests/test_cache.py`；`scripts/bench_cache.py` 实测成本 −66.7% |
| 6 | 配额：team / project / key 三级 | ✅ | `v1_chat` 限流段、`core/quota.py` | `test_key_level_rpm_is_enforced`、`test_team_rpm_is_enforced` |
| 7 | 日 / 月预算 | ✅ | `core/quota.check_budgets` | `test_daily_budget_stops_the_all_nighter`、`test_team_monthly_budget_is_enforced_and_reports_scope` |
| 8 | 超 80% 告警 | ✅ | `quota.raise_alert_if_needed` + `alerts` 表 | `test_crossing_eighty_percent_raises_exactly_one_alert`（唯一索引去重，不会每次都刷） |
| 9 | 超 100% 拒绝 | ✅ | `v1_chat` 预算段 → 429 `budget_exhausted` | 同上测试 + `tests/test_quota.py` |
| 10 | 记账：model / tokens / 单价 / 成本 / 延迟 / 状态；按部门·模型·时间聚合 | ✅ | `metering.py`、`metering_queue.py`、`api/admin.overview` | `tests/test_metering*.py`；`overview.by_team` 见 `test_usage_can_be_filtered_and_grouped_by_team` |
| 11 | PII 脱敏中间件（手机 / 身份证 / 邮箱，出网前） | ✅ | `guard.py`（4 条规则，身份证先于手机） | `tests/test_guard.py` |
| 12 | 全链路 trace：request_id 贯穿，可查原始 prompt（**可开关**） | ✅ | `request_traces`、`AIGATE_TRACE_PROMPT_MODE` | `test_trace_stores_masked_prompt_by_default`、`test_raw_prompt_mode_is_opt_in` |
| 13 | SSE 流式透传 | ✅ | `v1_chat._event_stream`（`X-Accel-Buffering: no`） | `tests/test_stream.py`、`smoke.sh` 第 5.5 段 |

## MVP · P1

| # | 需求 | 状态 | 代码 | 验证 |
|---|---|---|---|---|
| 14 | prompt 版本管理 | ✅ | `/admin/v1/prompts` 版本库 + `X-Prompt-Version` → 缓存 key / 账单 / trace | `tests/test_prompts.py`（6 例）+ `test_prompt_version_is_part_of_the_cache_key` |
| 15 | 影子流量双跑打分 | ⚠️ 见下 | `shadow.py`、`shadow_runs`、`/admin/v1/shadow{,/summary}`、调用链「影子对照」 | `test_shadow_runs_are_recorded_but_not_billed`、`test_shadow_failure_does_not_affect_the_response` |
| 16 | 模型质量-成本曲线 | ✅ | `/models` 页 + `GET /admin/v1/model-compare` + `scripts/quality_cost.py` | 页面实测；`tests/test_model_catalog.py` |

### 偏差说明（不做美化）

- **14 prompt 版本**：现在是完整的版本库（不同版本共存、可切换当前版本、可回滚），但**不做**
  变量渲染与 A/B 分流——那是上层应用的事。上线顺序有个坑：设了当前版本还要改请求头
  `X-Prompt-Version`，两步都做了才生效。
- **15 影子流量**：实现了「按路由配置影子候选（最多 2 个）→ 响应后异步调用 → 记录延迟/成本/字符相似度」。
  **没有**做「自动打分（LLM judge）」——相似度只是烟雾信号，我不想把一个 0.62 的相似度包装成"质量分"。
  需要 judge 的话接口已经留好（`shadow_runs.similarity` 可换成 judge 结果）。
- **16 质量-成本曲线**：`/models` 页按每千次成本排序，带成功率、P95、影子相似度。
  仍然**不是**「每个模型跑同一套评测集得到的准确率曲线」——没跑过影子的模型相似度显示 `—`。
  用行业平均值填充是最常见的作假方式，这里没做。

## 团队管理面（不在原始规格里，但是"内部能用"的必需品）

| 能力 | 状态 | 代码 | 验证 |
|---|---|---|---|
| key 归属（谁签的）、用途、环境、过期时间 | ✅ | `api_keys.*`、`api/admin.create_key` | `tests/test_key_lifecycle.py` |
| prod key 强制过期 | ✅ | `create_key` 的 `key_prod_needs_expiry` | `test_prod_key_must_expire` |
| 重名 active key 拒绝（而不是静默建第二把） | ✅ | `key_name_taken` 409 | `test_duplicate_active_name_is_refused` |
| 开发者自助签发（限自己项目）/ 只能吊销自己签的 | ✅ | `create_key` / `revoke_key` 的 `keys:own` 分支 | `test_viewer_can_issue_for_their_own_project_only`、`test_viewer_can_revoke_only_their_own_key` |
| 过期 key 401 且错误码指向"去换一把" | ✅ | `deps.get_principal` → `key_expired` | `test_expired_key_is_rejected_with_a_specific_code` |
| 吊销留原因、留操作人 | ✅ | `revoke_key` + `audit.key.revoke` | `test_revoke_records_reason_and_actor` |
| 成员可编辑（角色 / 可见项目 / 状态 / 重置密码） | ✅ | `PUT /console/v1/users/{id}` | `tests/test_member_admin.py`（9 例） |
| 不能改自己的角色或停用自己（防把自己锁门外） | ✅ | `gateway.self_lockout` 400 | `test_nobody_can_demote_or_disable_themselves` |
| 列表可用「60 天未用 / 14 天内过期」筛出风险项 | ✅ | `list_keys` 的 `idle_days` / 前端徽标 | 控制台「Key 管理」 |
| 在控制台新建团队 / 项目（不用改库） | ✅ | `POST /admin/v1/teams`、`POST /admin/v1/projects` | `tests/test_org_admin.py`（7 例） |
| 供应商密钥可在控制台录入（加密落库、只回显末四位） | ✅ | `secrets.py`、`PUT /admin/v1/providers/{n}/secret` | `tests/test_provider_secrets.py`（9 例） |
| OpenAI / 任意 OpenAI 兼容端点接入 | ✅ | `providers/__init__.py`、`provider_config.SEEDS` | `test_openai_is_a_known_provider` |
| 模型与单价登记（不靠种子脚本） | ✅ | `POST/PUT /admin/v1/models` | `tests/test_model_catalog.py`（5 例） |
| 运行状态（熔断/记账/数据库/告警一屏） | ✅ | `/ops` + `GET /admin/v1/ops-health` | `scripts/audit.py` 46 项 |
| 工作台 A/B 对比 + 接入指南 + 命令面板 | ✅ | `/playground`、`/guide`、⌘K | 浏览器实测 |
| 模型质量-成本对照做成页面 | ✅ | `/models` 页 + `GET /admin/v1/model-compare` | 页面实测：mock-alt ¥0.14/千次、相似度 1.00 |

## 技术架构对照

| 规格 | 实际 | 说明 |
|---|---|---|
| 令牌桶 + Postgres 配额表 | ✅ `core/quota.py`（memory / Redis 两种后端）+ `teams/projects/api_keys` | Redis 不可用时退化为进程内计数，README 已标注多副本后果 |
| 语义缓存「Redis + 向量索引」 | ⚠️ 存 `semantic_cache_entries`（PG/SQLite），暴力余弦 | 万级以下 <10ms；接口已隔离（ADR-0005）。换 pgvector/Redis 只改 `VectorStore` 实现 |
| 异步记账写 ClickHouse / PG | ⚠️ PG（SQLite 开发），批量 + spool + 回放去重 | 没有 ClickHouse：这个量级引入第二个存储只会增加故障面（ADR-0008 有取舍） |
| Grafana 看板 | ✅ `deploy/grafana/aigate-dashboard.json` | 只有次数与延迟；**没有 p95 与金额**，理由写在同目录 README（基数与账本归属） |

## 四个技术难点 → 代码与证据

见 [`INTERVIEW.md`](INTERVIEW.md)：流式与降级的冲突、语义缓存的正确性、token 与成本核算、多租户隔离，
每条都给了文件位置、测试名、以及「被追问第三层时怎么答」。

## 已知未做（明确排除，不是遗漏）

- 多副本一致性（限流/熔断状态在进程内）
- PostgreSQL 真机连通（只验证到离线 DDL 生成）
- 审计防篡改（只保证 HTTP 层不可删改，不防直接改库）
- 企业 SSO / 2FA
- K8s manifests

---

**怎么用这张表**：面试前扫一遍，重点记⚠️那三行——主动说出「哪部分是缩水实现的」，
比声称全做完更能证明你真的动手了。