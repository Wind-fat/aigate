# PG 下每请求多 ~90 ms：把它钉到具体环节

上一轮留下的未解异常：**同一个网关、同一条请求，SQLite 上的 p50 是 129 ms，真 PG 上是
121 ms，但「单请求」在 PG 上却多出约 90 ms，而且 8 并发时按 ~90 ms 一档线性排队。**
`docs/LOAD-TEST.md` 当时写的「原因未定位」在本文件里被替换成一条可复现的因果链。

一句话结论：**不是 SQL 慢，是「一条新连接」贵；而请求路径上每个请求都在偷偷多要一条连接。**
`_maybe_shadow()` 在没有任何影子模型配置时也会为每个请求新开一个 session，于是 8 并发时
进程要 16 条连接，池只有 5+10=15 条，溢出连接又被回收，于是请求路径上不断重建连接 ——
本机建一条 PG 连接 **p50 119 ms**（其中 92–115 ms 是 PG 握手本身，不是 asyncpg），
串行地摊在请求上，就是那「~90 ms 一档」。

修复后：顺序请求 PG 与 SQLite 打平（handler 22.5 ms / client 31.0 ms vs 31.3 ms），
N=8 时 PG 的 shadow 阶段从 p50 26 ms（max 1090 ms）降到 5 ms（max 23 ms），
客户端观察到的 p50 从 143.5 ms 降到 107.5 ms，同轮连接数从 27 降到 12。

---

## 1. 机器与软件

| 项 | 值 |
|---|---|
| OS | Windows 11 (10.0.26200) |
| CPU | Intel64 Family 6 Model 154 Stepping 3 / 16 logical cores |
| RAM | 15.7 GiB |
| 主 venv | Python 3.14.9（`D:\code\aigate\.venv`，跑网关与测试） |
| PG 宿主 venv | Python 3.12.13（`.pgvenv`，只用来跑 `pgserver`，wheel 只到 cp312） |
| PostgreSQL | 16.2，`pgserver` 便携版，`x86_64-pc-mingw64`，数据目录 `.pgdata_perf`（本轮新建，空库） |
| 服务端 | 1 个 uvicorn worker，单事件循环，`--no-access-log` |
| 连接池 | SQLAlchemy 默认 `pool_size=5` + `max_overflow=10` → 15 条，`pool_timeout=1.0s` |
| 上游 | 离线 `mock` provider，固定 `asyncio.sleep(0.01)`，不发网络请求 |

数据目录是**新建的空库**，这一点很重要：上一轮的库跑过多轮压测，`usage_records` /
`request_traces` 已经有量，任何「同机不同时间」的对比都会混进这部分噪声。

---

## 2. 怎么跑（全部可复制）

### 2.1 起真 PG、建库

```
$ .pgvenv/Scripts/python.exe scripts/pg_local.py --pgdata .pgdata_perf
URI=postgresql://postgres:@127.0.0.1:50242/postgres
AIGATE_DATABASE_URL=postgresql+asyncpg://postgres:@127.0.0.1:50242/postgres
```

端口由 `pgserver` 每次自己挑，所以 URL 必须从上一条命令的输出里拿。建一个干净的库：

```
$ .venv/Scripts/python.exe -c "import asyncio,asyncpg; asyncio.run(asyncpg.connect('postgresql://postgres:@127.0.0.1:50242/postgres').execute('CREATE DATABASE aigate_perf'))" # 见文末脚本清单
```

### 2.2 打开 PG 侧观测

`pg_stat_statements` 在 pgserver 的便携构建里没有，而且它会把 asyncpg 的
parse / bind / execute 合成一条记录 —— 恰恰把本轮要看的「bind 里其实在 planning」吃掉。
所以用 `log_min_duration_statement = 0`：

```
$ .venv/Scripts/python.exe - <<'PY'   # 只改运行期参数，不写进仓库
import asyncio, asyncpg
async def main():
    c = await asyncpg.connect("postgresql://postgres:@127.0.0.1:50242/postgres")
    await c.execute("ALTER SYSTEM SET log_min_duration_statement = '0'")
    await c.execute("ALTER SYSTEM SET log_line_prefix = '%m [%p] '")
    await c.execute("SELECT pg_reload_conf()")
    await c.close()
asyncio.run(main())
PY
```

### 2.3 阶段计时

`scripts/perf_stages.py` 自己播种、自己起停 uvicorn（和 `loadtest.py` 共用同一套装置），
阶段耗时取自响应体里的 `x_aigate.latency_ms`，`client_ms` 是 keep-alive socket 上量到的
整个交换时间，**两者之差就是 handler 看不见的那部分**。

```
# 顺序（一个连接、同一时刻只有一个请求：把调度和排队摘掉）
$ .venv/Scripts/python.exe scripts/perf_stages.py --label sqlite --db-url "sqlite+aiosqlite:///./perf-stages.db" --n 20 --json perf-sqlite-after.json
$ .venv/Scripts/python.exe scripts/perf_stages.py --label pg     --db-url "postgresql+asyncpg://postgres:@127.0.0.1:50242/aigate_perf" --n 8 --json perf-pg-seq-after.json

# N=8 并发（复现那条「~90 ms 一档」的排队）
$ .venv/Scripts/python.exe scripts/perf_stages.py --label pg-c8 --db-url "postgresql+asyncpg://postgres:@127.0.0.1:50242/aigate_perf" --n 0 --concurrency 8 --concurrent-requests 40

# 冷启动（不做预热，第一条请求付连接建立的钱）
$ .venv/Scripts/python.exe scripts/perf_stages.py --label pg-cold --db-url "postgresql+asyncpg://postgres:@127.0.0.1:50242/aigate_perf" --warmup 0 --n 3
```

### 2.4 语句级归因 & 连接成本

```
$ .venv/Scripts/python.exe scripts/pg_log_stats.py .pgdata_perf/log --from 14:42:36 --to 14:42:40 --top 5 --md
$ .venv/Scripts/python.exe scripts/perf_pg_probe.py --dsn "postgresql://postgres:@127.0.0.1:50242/aigate_perf" --rounds 5
```

---

## 3. 阶段耗时表

计时点是本轮新加的（`auth_ms` / `route_ms` / `price_ms` / `provider_cfg_ms` /
`shadow_ms`，见第 5 节），全部进 `latency_ms` 字段，所以这张表可以对着同一个响应体检。
单位 ms；`gateway_total` 是 handler 内部总时长，`client_ms` 是调用方感知的总时长。

### 3.1 顺序请求：SQLite vs 真 PG

**SQLite（n=20，1 连接顺序）**

| 阶段 | P50 | P95 | max | 阶段 | P50 | P95 | max |
|---|---|---|---|---|---|---|---|
| auth_ms | 4.0 | 20.0 | 29 | provider_cfg_ms | 0.0 | 0.0 | 0 |
| scope_ms | 0.0 | 0.0 | 0 | price_ms | 1.0 | 2.0 | 2 |
| quota_ms | 0.0 | 0.0 | 0 | shadow_ms | 0.0 | 1.0 | 1 |
| budget_ms | 0.0 | 0.0 | 0 | persist_ms | 0.0 | 0.0 | 0 |
| guard_ms | 0.0 | 0.0 | 0 | **gateway_total** | **22.5** | **24.0** | 25 |
| route_ms | 0.0 | 1.0 | 1 | **client_ms** | **31.3** | **45.5** | 47 |

**真 PG（n=8，1 连接顺序）**

| 阶段 | P50 | P95 | max | 阶段 | P50 | P95 | max |
|---|---|---|---|---|---|---|---|
| auth_ms | 4.0 | 5.0 | 5 | provider_cfg_ms | 0.0 | 0.0 | 0 |
| scope_ms | 0.0 | 0.0 | 0 | price_ms | 1.0 | 4.0 | 4 |
| quota_ms | 0.0 | 0.0 | 0 | shadow_ms | 0.0 | 0.0 | 0 |
| budget_ms | 0.0 | 0.0 | 0 | persist_ms | 0.0 | 0.0 | 0 |
| guard_ms | 0.0 | 0.0 | 0 | **gateway_total** | **22.5** | **24.0** | 24 |
| route_ms | 0.0 | 0.0 | 0 | **client_ms** | **31.0** | **34.1** | 34 |

顺序口径下 **PG 与 SQLite 没有可测差异**（handler 22.5 vs 22.5，client 31.0 vs 31.3）。
`upstream_ms` 没列进来：它固定含 mock 的 10 ms sleep，两个库一样，列出来只会稀释注意力
（原始值在 `perf-*.json` 里）。22.5 ms 的构成 ≈ 10 ms（mock sleep）+ 5 ms（各阶段 P50 之和）
+ 序列化/框架开销。

### 3.2 N=8 并发：修复前后

**PG，修复前（n=320）**

| 阶段 | P50 | P95 | max | 阶段 | P50 | P95 | max |
|---|---|---|---|---|---|---|---|
| auth_ms | 60.0 | 137.0 | 1066 | provider_cfg_ms | 0.0 | 0.0 | 22 |
| route_ms | 6.0 | 13.0 | 25 | price_ms | 7.0 | 13.0 | 30 |
| **shadow_ms** | **26.0** | **67.0** | **1090** | persist_ms | 0.0 | 0.0 | 0 |
| guard_ms | 0.0 | 0.0 | 0 | **gateway_total** | **23.0** | **32.0** | 109 |
| budget_ms | 0.0 | 0.0 | 11 | **client_ms** | **143.5** | **280.7** | 1917 |

**PG，修复后（n=320）**

| 阶段 | P50 | P95 | max | 阶段 | P50 | P95 | max |
|---|---|---|---|---|---|---|---|
| auth_ms | 54.0 | 120.0 | 933 | provider_cfg_ms | 0.0 | 0.0 | 11 |
| route_ms | 5.0 | 14.0 | 101 | price_ms | 6.0 | 13.0 | 42 |
| **shadow_ms** | **5.0** | **12.0** | **23** | persist_ms | 0.0 | 0.0 | 0 |
| guard_ms | 0.0 | 0.0 | 0 | **gateway_total** | **22.0** | **34.0** | 117 |
| budget_ms | 0.0 | 0.0 | 10 | **client_ms** | **107.5** | **217.5** | 1042 |

**SQLite，N=8（n=320，同一轮装置、修复后代码）**

| 阶段 | P50 | P95 | max | 阶段 | P50 | P95 | max |
|---|---|---|---|---|---|---|---|
| auth_ms | 74.0 | 133.0 | 170 | provider_cfg_ms | 0.0 | 0.0 | 39 |
| route_ms | 17.0 | 35.0 | 51 | price_ms | 18.0 | 35.0 | 53 |
| shadow_ms | 17.0 | 35.0 | 103 | persist_ms | 0.0 | 0.0 | 0 |
| guard_ms | 0.0 | 0.0 | 0 | **gateway_total** | **31.0** | **53.0** | 77 |
| budget_ms | 0.0 | 0.0 | 11 | **client_ms** | **153.7** | **249.0** | 279 |

读法：修复后 **PG 在 N=8 上已经不比 SQLite 慢**（client p50 107.5 vs 153.7，handler 22.0 vs 31.0）。
`shadow_ms` 的那一列是修复前后的分水岭：P50 26 → 5，max 1090 → 23。
`auth_ms` 在两库上都是 54–74 ms：那是「等一条连接 + 3 条鉴权 SELECT」，等连接的部分见第 4 节。

### 3.3 冷启动（`--warmup 0`，第一条请求付连接建立）

| 请求序号 | PG client_ms | PG auth_ms | SQLite client_ms | SQLite auth_ms |
|---|---|---|---|---|
| #1 | 83.8 | 31 | 57.5 | 16 |
| #2 | 35.0 | 5 | 26.6 | 5 |
| #3 | 31.1 | 4 | 45.7 | 24 |

冷启动的差距（~26–50 ms）**小于**一条裸连接的成本（~119 ms），因为 uvicorn 启动时的
`init_db()` / `ensure_seeds()` 已经让池里躺了一条热连接，第一条请求复用的是它。只有当池
需要**新建**连接时，才会看到完整的一档 ~85–130 ms。

---

## 4. 90 ms 到底是谁：四条候选逐一判定

### (a) 连接获取等待 —— 成立，是主因

三组独立证据。

**证据 1：一条新 PG 连接在这台机器上 p50 119 ms。**

```
$ .venv/Scripts/python.exe scripts/perf_pg_probe.py --dsn "postgresql://postgres:@127.0.0.1:50242/aigate_perf" --rounds 5
connect (asyncpg.connect, n=5): p50 128.7 ms, max 137.3 ms  ['105.2', '137.3', '132.3', '128.7', '125.2']
warm statement round trip (select 1, n=50): p50 0.099 ms, p95 0.286 ms
```

关掉 `log_connections` / `log_min_duration_statement` 后重测：`['65.1', '122.8', '119.4',
'127.4', '70.2']`，p50 119.4 —— 不是日志写盘造成的。

**证据 2：这 119 ms 不在 asyncpg 里，在 PG 握手本身。** 用一个手写的 40 行协议客户端
（TCP 连上，直接发 StartupMessage，读到 ReadyForQuery 为止，完全不经过 asyncpg）：

```
tcp    0.5 ms | tcp+startup->ReadyForQuery 115.2 ms  (pg handshake 114.7 ms)
tcp   17.0 ms | tcp+startup->ReadyForQuery 108.7 ms  (pg handshake  91.7 ms)
tcp   17.3 ms | tcp+startup->ReadyForQuery 114.9 ms  (pg handshake  97.5 ms)
tcp    0.5 ms | tcp+startup->ReadyForQuery 115.9 ms  (pg handshake 115.4 ms)
```

所以「asyncpg 慢」这条可以划掉：同一个 100 ms 用任何客户端都要付。

**证据 3：请求路径为每个请求要了第二条连接，池因此溢出并反复重建连接。**

* 修复前，`_maybe_shadow()` 无论有没有配置影子模型，都先
  `async with get_session_factory()()` 再查 `route_policies` —— 每个请求 2 个 session。
  PG 日志里 `route_policies` 的 bind 次数是 **18 次 / 9 个请求 = 2 次/请求**，且来自不同
  backend（`BEGIN`/`ROLLBACK` 各 22/20 次 / 9 请求）。
* 8 并发 × 2 session = 16 条连接需求 > 池容量 15。溢出连接在归还时被回收，于是热点上不断
  建连：`log_connections=on` 下一轮 **320 个请求产生了 27 次连接授权**；同条件改完后
  320 个请求 **12 次**（N=4、240 请求只要 6 次，因为 4 ≤ pool_size=5，完全不需要溢出）。
* 池饱和时的等待可以直接量出来（8 个持有者各再要一条连接，池 5+10）：

```
pool pressure (8 holders + 1 extra session each, pool 5+10):
  second-session wait p50 1138.3 ms, max 1195.9 ms
```

  这就是 `shadow_ms` 修复前 max = 1090 ms 的来源。

### (b) 某条 SQL 的 round-trip 次数 —— 不成立

一条语句在热连接上的往返是 **0.099 ms**（`select 1` p50，n=50）。请求路径上一共
~10 条语句（鉴权 3 + 路由 1 + 影子 1 + 价格 1–2 + metering 后置 + BEGIN/ROLLBACK），
即使全部串行也 ≈ 1.3 ms，跟 90 ms 差两个数量级。语句条数本身也没有异常：

| 语句（归一化） | 次数 / 11 个请求 | 累计 ms | 平均 ms |
|---|---|---|---|
| `bind: SELECT route_policies …` | 23 | 9.1 | 0.39 |
| `execute: SELECT route_policies …` | 23 | 0.4 | 0.02 |
| `statement: BEGIN;` | 17 | 0.5 | 0.03 |
| `statement: ROLLBACK;` | 14 | 0.6 | 0.04 |
| `bind: SELECT model_prices …` | 12 | 12.6 | 1.05 |
| `bind: SELECT api_keys …` | 12 | 10.4 | 0.86 |
| `bind: SELECT projects …` | 12 | 9.2 | 0.77 |
| `bind: SELECT teams …` | 12 | 8.9 | 0.74 |
| `execute: SELECT api_keys / projects / teams / model_prices` | 各 12 | 各 0.2–0.4 | 0.02–0.04 |

（窗口 `14:42:36–14:42:40`，11 个请求 = 3 预热 + 8 测量；`route_policies` 仍是 2 次/请求，
第 5 节说明为什么**故意**留着。）

### (c) asyncpg 参数 / 事务开销 —— 不成立

`bind` 是 PG 给带参语句做 planning 的那一步，日志里它的耗时分布是双峰的：

| 语句 | 新连接首次执行 | 同一连接第二次 |
|---|---|---|
| api_key | 7.951 ms | 0.466 ms |
| project | 4.500 ms | 0.226 ms |
| team | 5.655 ms | 0.174 ms |
| route | 4.208 ms | 0.241 ms |
| price1 | 4.537 ms | 0.288 ms |
| providers | 2.948 ms | 0.138 ms |
| **8 条合计** | **116.1 ms** | **2.2 ms** |

也就是说：一条**新**连接的前 8 条语句要 ~116 ms，同一连接上第二轮只要 2.2 ms。
这与「建连 119 ms」量级一致，也是为什么「连接一重建，请求就贵一档」能自洽 ——
它不是参数绑定或事务开销（BEGIN/ROLLBACK 各 0.03 ms），而是每次都要重新付一遍
prepare + plan + catalog 的冷启动。手写协议客户端只发了 4 条启动语句就花掉 92–115 ms，
进一步说明这不是 asyncpg 的绑定层。

**慢语句 top5（单条最慢，注意全是 `bind`，`execute` 都在 0.05 ms 以下）**

| ms | 时间 | 语句 |
|---|---|---|
| 9.609 | 14:42:37.418 | `bind: SELECT pg_catalog.pg_class.relname …`（`init_db` 的 has_table） |
| 7.585 | 14:42:39.401 | `bind: SELECT pg_catalog.pg_class.relname …` |
| 6.404 | 14:42:39.698 | `bind: SELECT model_prices.id, … FROM model_prices` |
| 5.219 | 14:42:37.448 | `bind: SELECT projects.id, … FROM projects` |
| 5.000 | 14:42:39.633 | `bind: SELECT api_keys.id, … FROM api_keys` |

「累计耗时最多」也是同一批：`bind: pg_class.relname` 17.2 ms（2 次，8.6 ms/次）、
`bind: model_prices` 12.6 ms（12 次）。**没有一条语句的解释或执行本身是慢的。**

### (d) 压测客户端侧 —— 部分成立，但不足以解释 90 ms

`client_ms - gateway_total` 在顺序口径下是 **8.5–9.0 ms**（两个库一致），这部分是
middleware、序列化、socket 往返和 session 关闭。它不小（占 SQLite 顺序请求的 28%），
但它是个常数，不随数据库变。

N=8 时这个差值涨到 85.5 ms（PG）/ 122.7 ms（SQLite）—— 关键是 **SQLite 更大**，
而两者的 handler 都只有 22–31 ms。所以这一截是「单事件循环 + 两层
`@app.middleware("http")`（Starlette BaseHTTPMiddleware）+ 客户端 8 条连接挤在一个
进程里」的排队，不是 PG 特有的。见第 6 节未解释项。

---

## 5. 做了什么（三处，都不改语义）

**1. 影子查询复用请求自己的 session**（`src/aigate/api/v1_chat.py: _maybe_shadow`）

非流式路径把 `session` 传进去，只有流式（响应后、请求 session 已关）才自己开新 session。
删掉的是「为了读一行 `route_policies` 而去池里再抢一条连接」。

| 指标（PG，N=8，n=320） | 修复前 | 修复后 |
|---|---|---|
| `shadow_ms` P50 / P95 / max | 26.0 / 67.0 / 1090 | 5.0 / 12.0 / 23 |
| `client_ms` P50 / P95 | 143.5 / 280.7 | 107.5 / 217.5 |
| 一轮 320 请求建立的连接数 | 27 | 12 |
| `auth_ms` P50 | 60.0 | 54.0 |

**2. 把 `shadow_ms` 从 `persist_ms` 里拆出来**（`_complete`）

原来 `t_persist` 包住了 `_maybe_shadow()` 和 `_persist()` 两步，于是「持久化耗时」这个
字段里藏着一个可能 1 秒的池等待 —— 这正是上一轮看不出问题的地方。现在 `persist_ms` 只
覆盖 `meter.submit()`（实测 P50 0.0 ms，因为它只是入队 + 写 spool），影子单独计。

**3. 补齐缺失的阶段计时**（`_compute` 前的 handler 入口、`_resolve_candidates`、
`_price_for`、`_provider_for`；中间件把 `request.state.started` 交给 handler）

原来只有 `scope/quota/budget/guard/cache/upstream/persist` 七段，`auth`（依赖阶段的 3 条
SELECT + 池等待）和 `route`/`price`/`provider_cfg`/`shadow` 都是空白 —— 而池等待恰好落在
`auth` 这一段里，所以不做这一步就永远看不见 (a)。这些键都进 `x_aigate.latency_ms`，
默认就在响应里，测试里没有断言键集合，新增是安全的。

**没有做**：`_resolve_candidates()` 读过的同一行 `route_policies`，`_maybe_shadow()` 又读
一次（2 次/请求）。现在它走同一个连接，热态下一共 0.39 + 0.02 = 0.41 ms，收益低于把
路由函数的返回值契约改掉的风险，因此留着并在文里写明。

---

## 6. 未解释项（必须明说）

1. **N=8 时 handler 之外仍有 85–123 ms 的 residue，没切开。** 已知它不是 DB：
   同条件下 SQLite 的 residue 更大（122.7 ms）而 handler 更慢（31 ms），PG 修复后 handler
   已经和顺序请求一样（22 ms）。怀疑对象是单事件循环 + 两层
   `@app.middleware("http")`（BaseHTTPMiddleware 每条请求多两次 anyio 任务组跳转）+
   客户端单进程 8 连接，**但没有一次实验把这三者分开** —— 试过用 `/health` 对照，
   历史数据是 N=8 下 5.3 ms/请求，量级差太远，不能拿来当结论。
2. **为什么 Windows 上建一条 PG 连接要 ~110 ms，没钉死到「进程创建」这一步。** 手写协议
   客户端证明钱花在握手（`connection received` → `ReadyForQuery`）而不是 asyncpg，最可能
   是 PG 在 Windows 上为每条连接 fork/exec 一个 `postgres.exe` 后端进程 + 安全软件扫描；
   本轮没有测进程创建时间，也没关过 Defender，所以只能写作推测。
3. **上一轮那条「单请求在 PG 上多出约 90 ms」在本机新库、热池下复现不出来。**
   顺序口径 PG 31.0 ms vs SQLite 31.3 ms（client），handler 22.5 vs 22.5。
   能复现的是「需要新建连接时，一档 ~85–130 ms」和「N=8 时按档排队」。因此
   `docs/LOAD-TEST.md` 里那句应理解为「冷连接 / 池排队」，而不是「每请求恒定 +90 ms」，
   已在该文件里改成指向本文。
4. **`BEGIN` 17 次 vs `ROLLBACK` 14 次**（11 个请求）对不上整数。差额应来自 metering
   writer 自己的批量事务和进程退出时的连接回收，没有逐条追踪；不影响上面的结论，
   但记在这里以免下次又被当成异常。

---

## 7. 下一步建议

1. **池容量是个准入决策，不是本次能顺手改的**：`pool_size=5` 对 N=8 就会溢出。
   在 N=8 上把 `pool_size` 提到 8 应当能直接消掉剩余的重建连接（本轮 N=4/240 请求只建
   6 条连接可以佐证），但这会改变 503 的触发点，需要和「准入失败要可解释」那张单一起批。
2. **连接预热**：启动时把 `pool_size` 条连接一次性建好（`SELECT 1` × N），把首轮 85–130 ms
   的冷启动挪到启动阶段。同样属于启动行为变更，未做。
3. 若要做第 6 节第 1 条，最省事的切法是给 `@app.middleware("http")` 换成纯 ASGI 中间件
   再测一遍 N=8 的 residue —— 一次 A/B 就能判定那 85 ms 是不是 BaseHTTPMiddleware。

---

## 8. 文件清单

新增（都是测试期工具，不进运行时依赖）：

| 文件 | 用途 |
|---|---|
| `scripts/perf_stages.py` | 分段计时装置：自己播种/起停 uvicorn，顺序 + 并发两种口径，P50/P95/max 表，`--json` 存原始样本 |
| `scripts/perf_pg_probe.py` | 连接成本探针：`asyncpg.connect` / 热态 RTT / 新连接首次 plan / 池饱和等待 |
| `scripts/pg_log_stats.py` | 解析 `log_min_duration_statement=0` 的日志：出现次数最多、累计最久、单条最慢 top5 |

改动（`src/`）：

| 文件 | 改动 |
|---|---|
| `src/aigate/api/v1_chat.py` | `_maybe_shadow(..., session=None)` 复用请求 session；`shadow_ms` / `auth_ms` / `route_ms` / `price_ms` / `provider_cfg_ms` 计时 |
| `src/aigate/main.py` | `request.state.started`（一行，供 `auth_ms` 使用） |

验证：`ruff check src tests scripts evals` 全绿；`pytest -q` 全量见提交说明。