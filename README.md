# AIGate

[![CI](https://github.com/Wind-fat/aigate/actions/workflows/ci.yml/badge.svg)](https://github.com/Wind-fat/aigate/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)

**多租户 LLM 网关**：统一接入 · 按团队记账 · 路由与降级 · 熔断 · 流式透传 · 脱敏 · 可观测。

> 业务方只改一行 `base_url`，就获得鉴权、配额、成本归因、故障降级和全链路追踪。
> AIGate 不是聊天工具——它没有聊天界面。它是一个公司**管理大模型资源**的那一层。
>
> 当前形态是**本地单用户工具**（一条命令起停、SQLite 存储）。给自己和小团队用够；
> 对外提供服务还差的六件事在文末「已知限制」里逐条列出。

## 已实现 / 待实现

| 能力 | 状态 | 证据 |
|---|---|---|
| OpenAI 兼容 `/v1/chat/completions` | ✅ | `tests/test_chat_endpoint.py` |
| 多租户 + API Key（只存 SHA-256） | ✅ | `tests/test_security.py` |
| 虚拟模型 → 候选 + 显式 fallback | ✅ | `failover-demo` 现场可演示 |
| 降级只在**可重试**错误上触发 | ✅ | 4xx 不重试，`test_non_retryable_*` |
| 熔断（连续失败 → 停止调用 → 半开探测） | ✅ | `tests/test_circuit.py` |
| 配额（令牌桶 RPM + 月度预算硬限） | ✅ | `tests/test_quota.py` |
| 流式 SSE 透传 + TTFT 计量 | ✅ | `tests/test_stream.py` |
| PII 脱敏（国内 4 类规则，出网前） | ✅ | `tests/test_guard.py` |
| Decimal 记账 + 用量明细（批量异步写入） | ✅ | `tests/test_metering_writer.py` |
| Trace（分段耗时 / 路由路径 / 脱敏 prompt） | ✅ | `request_traces` 表 |
| 语义缓存（L1 精确 + L2 向量） | ✅ | 60 次调用实测成本 −66.7% |
| 控制台（Next.js，5 页） | ✅ | `docs/screenshots/` |
| 评测执行器（smoke + regression 20 例） | ✅ | `make eval`，失败退出码 1 |

---

## 为什么不是套壳

判断标准只有一条：**把底层模型换掉，谁会死？**

| | 套壳工具 | AIGate |
|---|---|---|
| 产出物 | 一个聊天界面 | 治理能力（配额 / 账单 / 降级 / trace） |
| 换模型 | 改代码、改 prompt | 改一行路由映射，业务侧无感 |
| 多租户 | 没有 | 团队 → 项目 → Key 三级 |
| 供应商故障 | 整站不可用 | 熔断 + 降级，业务侧只见延迟上升 |
| 能不能被审计 | 不能 | 每次调用可追溯到成本与责任人 |

---

## 架构

```mermaid
flowchart LR
  C1[业务应用 A] --> GW
  C2[业务应用 B] --> GW

  subgraph GW[数据面 · Gateway]
    direction LR
    G1[鉴权 Key→Team] --> G2[配额/限流<br/>令牌桶]
    G2 --> G3[预算硬限]
    G3 --> G4[PII 脱敏]
    G4 --> G5{路由 + 熔断}
    G5 --> G6[Provider 适配]
    G6 --> G7[流式/非流式响应]
    G7 --> G8[记账 & Trace]
  end

  G2 -.-> RD[(Redis<br/>或进程内)]
  G7 --> P1[DashScope]
  G7 --> P2[DeepSeek]
  G7 --> P3[本地 vLLM]
  G7 --> P4[Mock（离线/测试）]
  G8 --> DB[(PostgreSQL / SQLite)]
```

**请求路径**

```
鉴权 → 模型白名单 → 令牌桶配额 → 月度预算 → PII 脱敏
     → 路由解析（候选 + 显式 fallback，逐个过熔断）
     → Provider 调用（chat 或 stream）
     → 成本计算 → usage_records + request_traces → 响应
```

两个实现细节值得看：

- **`retryable` 是唯一的降级依据**。超时/5xx/429 才切下一个候选；4xx 一律不重试——重试只是多付一次钱，还掩盖真实错误。
- **流式响应无法回滚**。首 token 前失败可以静默降级；首 token 后只能发一条 `gateway.stream_interrupted` 事件并如实记账。第三条路（假装成功）是错的。

非流式响应带 `x_aigate` 元数据块：

```json
"x_aigate": {
  "request_id": "req_7f3c1a...",
  "team": "acme", "project": "support-bot",
  "provider": "mock", "model_requested": "chat-default", "model_used": "mock-ok",
  "degraded": false, "cache_hit": null, "pii_masked": 1,
  "cost_cny": 0.000042, "priced": true,
  "latency_ms": { "quota_ms": 0, "guard_ms": 0, "upstream_ms": 12, "gateway_total": 14 }
}
```

流式响应没法在 body 里塞元数据（body 是协议），所以尾部追加一条 **SSE 注释**（所有客户端都会忽略注释，这正是它的用处）：

```
: aigate {"request_id":"req_...","provider":"mock","degraded":false,"ttft_ms":31,"cost_cny":0.000018}
data: [DONE]
```

---

## 快速开始（3 分钟，一条命令）

**前置**：Python ≥ 3.11、[uv](https://github.com/astral-sh/uv)、Node ≥ 20（只为控制台）。
无需 Docker、无需数据库服务。

```bash
git clone https://github.com/Wind-fat/aigate && cd aigate
uv sync --all-groups
./scripts/up.sh   # 建 .env（随机 admin token）→ 灌示例数据 → 起网关+控制台 → 开浏览器
```

打开 <http://127.0.0.1:3000>，进「工作台」签一把 Key 就能发请求。Ctrl-C 一起停。
用法详见 **[docs/USAGE.md](docs/USAGE.md)**（接自己的代码、换真实模型、日常看什么）。

```bash
export KEY=sk-ag-xxxx                                  # 控制台「Key 管理」里签发
curl -s localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"chat-default","messages":[{"role":"user","content":"hello"}]}' | jq
```

### 四个可现场演示的证据

```bash
# 1) 降级：主候选被故意配置成 500，网关自动切备用
curl -s localhost:8080/v1/chat/completions -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"failover-demo","messages":[{"role":"user","content":"hi"}]}' | jq '.x_aigate.degraded'
# => true

# 2) 流式：SSE 逐块返回，尾部带 TTFT 与成本
curl -sN localhost:8080/v1/chat/completions -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"chat-stream","stream":true,"messages":[{"role":"user","content":"stream"}]}'

# 3) 脱敏：手机号出网前已被掩码（mock 会原样回显，能直接看出有没有漏）
curl -s localhost:8080/v1/chat/completions -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"chat-default","messages":[{"role":"user","content":"call 13812345678"}]}' | jq -r '.choices[0].message.content'
# => [mock:mock-ok] call 138****5678

# 4) 熔断：连打 4 次失败候选，第 4 次主候选根本不会被调用
for i in 1 2 3 4; do curl -s localhost:8080/v1/chat/completions -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"failover-demo","messages":[{"role":"user","content":"hi"}]}' | jq -c '.x_aigate.degraded'; done
```

完整断言脚本：`bash scripts/smoke.sh`（23 项，含数据库副作用校验）。

---

## 评测与压测

### 评测执行器

对**运行中的网关**发真实 HTTP 请求，按 YAML 断言判分：

```bash
uv run python evals/run.py smoke regression --base-url http://127.0.0.1:8080
# verdict: PASS (20/20)   exit 0
```

- 断言不只看状态码：`fallback_attempts` 会反查 `/admin/v1/traces/{id}` 的 `route_path`，
  才能证明「4xx 确实没有触发 fallback」这种**负向**结论。
- 任一用例失败退出码 1，执行器自身出错退出码 2 —— 可以直接挂进 CI。
- 缓存用例带 `{nonce}`，避免热库把「本该 miss」伪装成命中。

### 压测（20 并发 × 200 请求，SQLite 配置）

```bash
uv run python tests/load/loadtest.py --base http://127.0.0.1:8080 --key $KEY \
  --concurrency 20 --requests 200
```

| 实现 | rps | 客户端 p50 | p95 | p99 | 错误 |
|---|---|---|---|---|---|
| 同步记账（改造前） | 42.3 | **238 ms** | 1500 ms | 3216 ms | 2–4 次 500 |
| 批量异步记账（现在） | **51.4** | 365 ms | **597 ms** | **711 ms** | **0** |
| 上游（mock） | — | 15 ms | 22 ms | 27 ms | — |

**这不是一个纯赢的改造，两个方向都要说：**

- 尾部大幅改善：p95 −60%、p99 −78%，并且 `database is locked` 的 500 归零（正确性 > 延迟）。
- 中位数变差：238 → 365ms。批次的 CPU 工作（构造 100 行 + 编译 executemany）仍在事件循环上，
  形成周期性 head-of-line 阻塞。
- 吞吐只 +22%，因为 SQLite 单写者仍是硬约束——批量只是把「每请求一次锁竞争」换成「每批一次」。

完整推导与后续三条优化路径见 [ADR-0007](docs/adr/0007-batched-metering.md)。

### 值班手册

用法见 [docs/USAGE.md](docs/USAGE.md)；值班处置见 [`docs/runbook.md`](docs/runbook.md)：
限流误触发、熔断打开、缓存错答、记账偏差、DB 锁、供应商区域故障 6 个场景，每个都是「症状 / 先看哪里（具体表或接口）/ 处置命令 / 回滚」。

---

## 控制台

管理后台 + 开发者工作台，同一个 Next.js 应用，分两个区：

| 区 | 页面 | 给谁看 |
|---|---|---|
| 管理后台 | 概览 / 调用明细 / Trace 瀑布 / Key 管理 | 平台、财务、审计 |
| 工作台 | Playground：选模型、发请求、实时看 `x_aigate` | 接入的开发者、面试演示 |

![Console 概览](docs/screenshots/overview.png)

![调用明细](docs/screenshots/usage.png)

![Key 管理](docs/screenshots/keys.png)

启动（网关需先在 8080 运行）：

```bash
cd console
npm install
npm run build && npm start        # http://127.0.0.1:3000
```

控制台通过 `X-Admin-Token` 调 `/admin/v1/*`，token 走 `NEXT_PUBLIC_AIGATE_ADMIN_TOKEN`。
**这是开发期做法**：token 会进浏览器包，生产必须换成服务端会话 + SSO（已列在已知限制里）。

截图里的数据由 `uv run python scripts/seed_demo.py` 生成，是**合成数据**：
它有工作日/周末的形状，是为了让图表不至于空着，不代表任何真实业务量。

---

## 语义缓存实测（60 次调用）

`uv run python scripts/bench_cache.py` 会起一个独立网关（专用 `bench.db`），
用同一批 20 个问题跑三遍：冷启动 / 原样重复 / 换个问法。

| pass | calls | hit rate | total cost (CNY) | avg latency |
|---|---|---|---|---|
| cold (no cache) | 20 | 0.0% | 0.000692 | 48.3 ms |
| identical (L1) | 20 | 100.0% | 0.000000 | 31.1 ms |
| paraphrased (L2) | 20 | 100.0% | 0.000000 | 57.2 ms |

**同样 60 次调用：无缓存 ¥0.002076 → 有缓存 ¥0.000692，成本 −66.7%。**

三个必须同时说出来的前提，否则这个数字就是吹的：

1. **命中率是场景相关的**。这里每个问题都重复出现，真实业务重复率可能只有 10–30%，
   成本降幅按同比例缩小。换场景就要重测，不能搬数字。
2. **L1 更快（31ms），L2 更慢（57ms）**。L2 要算 embedding 再扫库。这里上游是 10ms 的
   mock，缓存开销显得刺眼；接真实模型（800ms+）时结论会反过来。说"缓存一定更快"是错的。
3. 金额用 mock 单价算，绝对值没意义，**比例**才有意义。

缓存只对**确定性单轮请求**生效：`temperature=0`、无 assistant 轮次、非流式、无 tools。
降级（fallback）产生的答案不写缓存——它来自备用模型，缓存它等于把较低质量的结果钉住一整天。

---

## 接入真实模型

改 `.env`，把某个候选指向真实供应商（三家都走 OpenAI 兼容协议，一个适配器即可）：

```bash
AIGATE_DEFAULT_PROVIDER=dashscope
AIGATE_DASHSCOPE_API_KEY=sk-xxx
```

然后改 `route_policies` 的候选为 `{"provider":"dashscope","model":"qwen-plus"}`。
没有真实 key 时，`mock` 保证整套链路（鉴权、配额、降级、熔断、流式、记账、trace）**始终可运行**——这也是它存在的理由。

---

## API

| 方法 | 路径 | 说明 | 鉴权 |
|---|---|---|---|
| POST | `/v1/chat/completions` | OpenAI 兼容；`stream=true` 走 SSE | Bearer API Key |
| GET | `/v1/models` | 当前 Key **有权调用**的模型 | Bearer API Key |
| GET | `/health` `/ready` `/metrics` | 存活 / 就绪 / Prometheus 指标 | 无 |

**统一错误码**（调用方据此决策，不必读文档）：

```
gateway.missing_credentials · gateway.invalid_key · gateway.model_not_allowed
gateway.quota_exceeded · gateway.budget_exhausted · gateway.stream_interrupted
gateway.stream_unsupported · upstream_timeout · upstream_5xx · upstream_4xx
upstream_rate_limited · upstream_stream_error
```

---

## 配置（环境变量，前缀 `AIGATE_`）

| 变量 | 默认 | 说明 |
|---|---|---|
| `AIGATE_DATABASE_URL` | `sqlite+aiosqlite:///./aigate.db` | 生产用 `postgresql+asyncpg://...` |
| `AIGATE_ADMIN_TOKEN` | `dev-admin-token` | 控制面管理令牌 |
| `AIGATE_DEFAULT_PROVIDER` | `mock` | 无匹配路由时的兜底 provider |
| `AIGATE_DEFAULT_RPM` | `60` | 项目未单独配置时的每分钟请求上限 |
| `AIGATE_RATE_LIMITER` | `memory` | `memory`（单进程）/ `redis`（多副本） |
| `AIGATE_REDIS_URL` | `redis://127.0.0.1:6379/0` | `rate_limiter=redis` 时生效 |
| `AIGATE_CIRCUIT_THRESHOLD` | `3` | 连续可重试失败多少次后熔断 |
| `AIGATE_CIRCUIT_COOLDOWN_S` | `30` | 熔断后多久允许一次半开探测 |
| `AIGATE_REQUEST_TIMEOUT_S` | `30` | 上游超时（秒） |
| `AIGATE_SEMANTIC_CACHE_ENABLED` | `true` | 语义缓存开关 |
| `AIGATE_CACHE_THRESHOLD` | 空 | 相似度阈值；空 = 用 embedder 的校准值（hash 0.65 / api 0.92） |
| `AIGATE_CACHE_TTL_S` | `86400` | 缓存条目存活时间 |
| `AIGATE_CACHE_MAX_SCAN` | `2000` | L2 每次最多扫描多少条（暴力检索的上限） |
| `AIGATE_DASHSCOPE_*` / `AIGATE_DEEPSEEK_*` / `AIGATE_VLLM_*` | — | 各供应商 base_url 与 key |

---

## 测试

```bash
make verify        # ruff + pytest（56 项）
make test          # 仅测试
bash scripts/smoke.sh   # 真起进程的端到端 23 项断言
```

测试全部走 `mock` / `mock-alt` 两个离线 provider，不触网、不需要 key、可离线跑。

```
tests/
├── test_security.py       # key 生成 / 哈希 / 格式
├── test_metering.py       # Decimal 成本、token 估算
├── test_guard.py          # 4 类 PII 规则、幂等性、出网前脱敏
├── test_quota.py          # 令牌桶语义、429 且不产生上游调用、预算硬限
├── test_stream.py         # SSE 帧、TTFT、首 token 前降级、流中断不可恢复
├── test_circuit.py        # 熔断单元 + 打开后不再调用死掉的供应商
├── test_cache.py          # 准入规则、L1/L2、租户隔离、降级不缓存
├── test_admin.py          # 控制面鉴权、聚合口径、Key 生命周期
└── test_chat_endpoint.py  # 鉴权 401 / 越权 403 / 降级 / 记账一致性
```

---

## 已知限制（写出来，而不是藏起来）

- **熔断状态是进程内的**：多副本部署时每个副本各自判断。要做集群级熔断，状态得挪进 Redis（`docs/adr/0004-*`）。
- **熔断按 provider 粒度**：`mock` 熔断会连带跳过所有路由到 `mock` 的策略。生产里 fallback 应指向**另一家**供应商，否则等于没有 fallback。
- **流式响应无法回滚**：首 token 后上游挂掉，只能发 `gateway.stream_interrupted` 并如实记账，不能假装成功。
- 表结构目前用 `create_all`，Alembic 迁移排在 Stage 3。
- **语义缓存用 JSON 存向量 + 暴力余弦**（万级以下 <10ms）。pgvector 索引留到有 PG 环境时替换，
  接口已隔离（ADR-0005）。这是有意识的选择，不是没做完。
- **阈值必须跟着 embedding 模型走**：hash embedding 的相似度分布整体偏低（同义句 ~0.71），
  直接套 bge-m3 的 0.92 会让离线缓存永远不命中。所以阈值挂在 embedder 上，配置项留空即用校准值。
- **L2 命中可能比不缓存更慢**，见上文实测表。
- **SQLite 单写者仍是并发上限**：批量记账把 p95 从 1.5s 压到 0.6s、错误归零，但吞吐只
  从 42 涨到 51 rps。下一步是把批次写移出事件循环 / 换 PostgreSQL，见 ADR-0007。
- 压测脚本默认阈值（p95 ≤ 800ms）是 **PostgreSQL 口径**；在 SQLite 演示配置下用
  `--max-p95-ms 2000` 才是合理断言。这两个数字都写在 README 里，不藏。
- **控制台的 admin token 在浏览器里**（`NEXT_PUBLIC_*`）。开发/演示可以，生产必须换成服务端会话。
- 控制台的聚合在 Python 里算：万级以下无感，百万级要落物化日表或换 OLAP。

---

## 目录

```
src/aigate/
├── main.py            # app 工厂、中间件顺序、异常归一化
├── config.py          # 所有配置唯一入口
├── db.py              # 引擎懒加载（测试可换库）
├── models.py          # 7 张表
├── guard.py           # PII 规则（出网 + trace 共用同一套）
├── metering.py        # Decimal 成本 / token 估算
├── metrics.py         # Prometheus 文本指标
├── core/
│   ├── quota.py       # 令牌桶（memory/redis+Lua）+ 月度预算
│   ├── circuit.py     # 熔断：closed → open → half-open
│   ├── embedding.py   # hash（离线）/ API（bge-m3），阈值随 embedder 校准
│   └── cache.py       # 语义缓存 L1 精确 + L2 向量，含准入规则
├── providers/         # base(ABC) + openai_compat + mock（两个实例）
└── api/               # deps / health / v1_chat
```

---

## Roadmap

- [x] **Stage 1** 骨架、鉴权、路由、降级、记账、Trace
- [x] **Stage 2** 令牌桶配额、预算硬限、流式 SSE、PII 脱敏、熔断、40 项测试
- [x] **Stage 3a** 语义缓存（L1/L2 + embedder 抽象 + 成本对照脚本）、50 项测试、23 项冒烟
- [x] **Stage 3b** 控制台（Next.js 5 页）+ `/admin/v1` 控制面 API
- [x] **Stage 3c** Alembic 迁移（upgrade / downgrade / 幂等已验证）
- [x] **Stage 4** 评测执行器（20 例）+ 压测脚本（Python / k6）+ 值班手册
- [x] **Stage 5** 批量异步记账（p95 −60%、错误归零，代价见 ADR-0007）
- [ ] **Next** 写入移出事件循环、预算聚合 TTL 缓存、pgvector 索引、演示视频

设计决策见 [`docs/adr/`](docs/adr/)：
[0001 技术选型](docs/adr/0001-stack-choices.md) ·
[0002 为什么不用 LiteLLM](docs/adr/0002-why-not-litellm.md) ·
[0003 限流后端](docs/adr/0003-rate-limiter-backends.md) ·
[0004 熔断粒度](docs/adr/0004-circuit-breaker-granularity.md)

---

## License

Apache-2.0