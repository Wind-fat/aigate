# 面试怎么讲这个项目

四段话，每段都有代码位置和可复现的证据。不是话术——被追问到第三层时，只有代码和测试能接住。

---

## 1. 流式和降级的冲突

**一句话**：首 token 之前可以降级，之后只能标记中断，不能假装成功。

**代码**：`src/aigate/api/v1_chat.py` 的 `_event_stream()` —— 循环里只有 `started_emitting` 为
假时才允许 `continue` 到下一个候选；一旦吐出过 delta，上游再挂就只能发
`gateway.stream_interrupted` 并如实记账。

**证据**：

```bash
uv run pytest -q tests/test_stream.py -k interrupted
bash scripts/smoke.sh          # 第 6 段：stream-interrupt 路由
```

**常见错答**：「降级重试一下就好了」。追问「已经发了 300 个 token 怎么办」，答不上来说明没写过。
**准确答法**：说清三个状态——未开始（可换供应商）、已开始（只能中断）、已结束（正常记账），
并说明为什么「中断」要单独一个错误码而不是 500：客户端要区分「重试可能有用」和「重试会重复计费」。

---

## 2. 语义缓存的正确性风险

**一句话**：缓存会答错，所以设计目标不是「命中率」，是「不该命中的时候别命中」。

**代码**：`src/aigate/core/cache.py` 的 `eligible()` 与 `prompt_key()`：
只缓存 `temperature=0`、单轮、非流式、无 tools 的请求；key 由
`租户 + 虚拟模型 + prompt 版本 + 归一化文本` 组成；阈值挂在 embedder 上（`HashEmbedder.recommended_threshold = 0.65`）。

**证据**：

```bash
uv run pytest -q tests/test_cache.py            # 租户隔离、温度、版本失效
uv run python scripts/bench_cache.py            # 成本 −66.7% 的实测脚本（含反直觉结论）
```

**值得主动说的一句**：L2 命中可能比不缓存更慢（要算 embedding 再扫库）。缓存换的是**钱**，
不一定换**时间**；上游 10ms 的 mock 上尤其明显，接真实模型（800ms）时结论会反过来。

---

## 3. token 计数与成本核算

**一句话**：流式响应默认不返回 usage，所以「账单」这个数字必须自己算，并且要能对账。

**代码**：`src/aigate/metering.py`（`compute_cost` 用 Decimal、`estimate_messages_tokens` 兜底估算）
+ `src/aigate/metering_queue.py`（先落 spool、批量 `insert()`、提交后 ack、回放按 `request_id` 去重）。

**证据**：

```bash
uv run pytest -q tests/test_metering.py tests/test_metering_writer.py tests/test_metering_spool.py
# 崩溃恢复：kill -9 之后不丢账（ADR-0008）
```

**被问到「估算不准怎么办」**：诚实回答——估算用于**成本预警**，不用于对外结算；预算是软的，
超了拒绝；真实金额以供应商账单为准，本系统做的是**归因与分摊**，不是计费系统。

---

## 4. 多租户隔离

**一句话**：越权不是「能不能看到按钮」，是「数据出不出得去」。

**代码**：三层，任意一层单独失守都不致命——

| 层 | 位置 | 作用 |
|---|---|---|
| Key → 项目 | `api/deps.get_principal` | 一个 key 只能代表一个项目，项目属于一个团队 |
| 角色的数据范围 | `console_auth.CAPABILITIES` + `Actor.scoped_projects` | viewer 只能查自己项目的用量/trace/key |
| 查询过滤 | `api/admin.py` 的 `usage` / `trace` / `keys` | 越界 trace id 返回 404 而不是 403 |

**证据**：

```bash
uv run pytest -q tests/test_roles_and_providers.py
# test_viewer_is_scoped_to_its_own_project / test_finance_sees_costs_but_not_prompts_or_keys
```

**为什么 404 而不是 403**：403 等于确认「这个 request_id 存在，只是不给你看」。
ID 的存在性本身就是信息。

---

## 5. 怎么证明「这不是套壳」

一句判断标准：**把底层模型供应商换掉，这个系统会死吗？**

- 套壳：死。护城河是一段 prompt。
- AIGate：只改「模型路由」里的候选链，业务代码一行不动，下一条请求就换了供应商。
  证据：`test_route_edit_takes_effect_on_the_next_request`——改完路由，响应里的 `provider` 立刻变。

再加三个只有网关才有的问题，套壳根本不会遇到：

1. **账单为什么和客户端调用次数不一致**（限流与预算拒绝不落库，是设计）
2. **缓存命中率为 0 时成本为什么没降**（没命中却仍要付 embedding 的钱）
3. **两个团队抢同一个项目额度时，谁先被 429**（团队级 token bucket 是共享的还是各自的）

---

## 6. 主动交代缺点（这一段比优点加分）

面试官最怕的是「全是优点」的项目。准备好这三条，说清楚代价和边界：

1. **单机限制**：限流与熔断状态在进程内，多副本时实际额度是 `N × rpm`。要上多副本，先换 Redis 后端。
2. **PostgreSQL 路径没在真机上跑过**：迁移能生成 PG DDL，也用 SQLite 验过 upgrade/downgrade，
   但没有连过真实 PG。这是环境限制，不是设计缺陷——但必须自己说出来。
3. **审计只覆盖 HTTP 层**：直接改数据库不留痕。真防篡改要外部 WORM 日志。

配套的一句：`scripts/check_deploy.py` 在没有 Docker 的机器上做了 25 项部署静态检查——
**承认没验证的部分，同时给出你为了缩小风险做了什么**，这才是工程判断力。