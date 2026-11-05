# AIGate 值班手册（Runbook）

面向值班同学：每个场景都是「症状 → 先看哪里 → 处置 → 回滚」四段，命令可直接复制。
概念解释不在这里，代码语义见 `src/aigate/`，架构决策见 `docs/adr/`。

---

## 0. 开工前必须知道的 6 件事

这些事实决定了下面的判断方式，都是代码行为，不是经验之谈：

1. **被限流（429）的请求不落库**。限流在写 `usage_records` 之前就 return 了，所以
   「客户端打了 1000 次、报表只有 60 次」是限流的正常表现，不是丢数据。计数看 `/metrics`
   的 `aigate_rate_limited_total`。
2. **`usage_records` 才是账单**，`request_traces` 是排查用的。缓存命中的请求也会写
   `usage_records`，只是 `cost_cny=0`、`status='cached'`。
3. **响应里的 `latency_ms.gateway_total` 不含落库时间**（它在 `_persist()` 之前计算），
   落库耗时单独在 `latency_ms.persist_ms` 里。真实用户延迟 = `gateway_total + persist_ms`，
   或直接看访问日志每行的 `-> 200 123ms` / 客户端 p95。高并发下 `persist_ms` 会远大于
   `gateway_total`，这是判断「慢在网关还是在库」的第一眼依据。
4. **限流是「每项目」的**，不是每 key；`memory` 后端是**每进程**一份，N 个副本的实际额度是
   `N × rpm_limit`。
5. **熔断器状态在进程内存里**，没有 admin 接口可以复位，唯一复位手段是重启进程。
   阈值/冷却期（`AIGATE_CIRCUIT_THRESHOLD` / `AIGATE_CIRCUIT_COOLDOWN_S`）只在启动时读。
6. **`route_policies`、`projects`、`model_prices`、`semantic_cache_entries` 每请求实时读库**，
   改表立即生效；`AIGATE_*` 环境变量只在启动时读，改 env 必须重启。

```bash
# 下文统一用这几个变量（生产环境按实际地址/token 替换）
export GW=http://127.0.0.1:8080
export ADMIN_TOKEN=dev-admin-token
export DB=./aigate.db
export DB_DIR=.
export AIGATE_KEY=sk-ag-xxxxxxxx   # 一把可用的数据面 key（压测/验证用）
export A="X-Admin-Token: $ADMIN_TOKEN"

# 没有 sqlite3 命令行时，用 python 执行同一句 SQL：
#   python -c "import sqlite3,sys;print(sqlite3.connect('$DB').execute(sys.argv[1]).fetchall())" "<SQL>"
```

一分钟分诊表：

| 现象 | 大概率是 | 跳到 |
|---|---|---|
| 429，body `gateway.quota_exceeded` | 限流 | 场景 1 |
| 429，body `gateway.budget_exhausted` | 月度预算打满 | 场景 1 |
| 502，`route_path` 里有 `circuit_open` | 熔断 | 场景 2 |
| 答案内容不对，但 200 | 语义缓存错答 | 场景 3 |
| 网关账单和厂商账单差一截 | 定价/漏记 | 场景 4 |
| 500 + 日志 `database is locked`，延迟毛刺 | SQLite 写锁 | 场景 5 |
| 某 provider 错误率飙升，`degraded=true` 变多 | 上游区域故障 | 场景 6 |

---

## 场景 0：记账 spool 异常（只在它出问题时看）

### 症状

- `ls aigate_metering.spool` 文件存在且**持续变大**（正常情况下稳态只有几十行，提交后即清空）
- 日志出现 `metering batch failed` / `replaying N un-acknowledged metering events`
- 报表请求数 < 客户端请求数，且 `/metrics` 里没有对应 429

### 先看哪里

```bash
wc -l "$DB_DIR/aigate_metering.spool"        # 待提交事件数
tail -3 "$DB_DIR/aigate_metering.spool"       # 最后几条（含 request_id / cost_cny）
python -c "import sqlite3;print(sqlite3.connect('$DB').execute('select count(*) from usage_records').fetchone())"
```

### 处置

spool 里的事件在**下次启动时会自动回放**，所以第一动作是重启网关，而不是手工删文件：

```bash
# 重启后确认回放成功
grep "replaying" <gateway log>
python -c "import sqlite3;c=sqlite3.connect('$DB');print(c.execute('select count(*) from usage_records').fetchone())"
```

只有确认回放成功、spool 已清空后，才允许删文件。**永远不要先删 spool。**
如果回放一直失败（数据库写不进去），先修数据库，spool 会一直保留数据——这是设计意图。

### 回滚

spool 是纯文本 NDJSON，可人工核对后转成 SQL 补录；格式与 `usage_records` 字段一一对应。

---

## 场景 1：大量 429（限流误触发 vs 真超量）

### 症状

- 客户端拿到 429，body：`{"error":{"code":"gateway.quota_exceeded","message":"rate limit 60/min exceeded"}}`
  （具体秒数在响应头 `Retry-After`，body 里没有 `retry_after_s`）
- 或 `gateway.budget_exhausted`（这个是钱用完了，和限流不是一回事）
- `/metrics` 里 `aigate_rate_limited_total{project="..."}` 快速上涨
- 报表请求数远小于客户端实际请求数（429 不落库，见「开工前」第 1 条）

### 先看哪里

```bash
# 1) 区分两种 429：把客户端的错误 body 拿来对 code
#    gateway.quota_exceeded  -> 限流      gateway.budget_exhausted -> 预算

# 2) 这个项目的额度到底是多少（项目级，不是 key 级）
curl -s -H "$A" "$GW/admin/v1/projects" | python -m json.tool

# 3) 限流计数（唯一能看到 429 的地方）
curl -s "$GW/metrics" | grep -E "rate_limited|http_requests_total"

# 4) 判断「真超量」还是「误触发」：看这个项目最近一分钟的实际成功量
sqlite3 "$DB" "select strftime('%H:%M', created_at) m, count(*)
 from usage_records
 where created_at >= datetime('now','-10 minutes')
 group by m order by m desc limit 10;"

# 5) 预算是否打满（budget_exhausted 时必看）
sqlite3 "$DB" "select project_id, round(sum(cost_cny),4) spent, count(*) reqs
 from usage_records where created_at >= date('now','start of month')
 group by project_id order by spent desc;"
sqlite3 "$DB" "select slug, monthly_budget_cny, rpm_limit from projects;"
```

判据：

- **真超量**：单分钟成功量已经贴着 `rpm_limit`，或业务方确认在跑批量任务 → 提额。
- **误触发**：`rpm_limit=60` 但线上有 3 个副本（实际额度 180，仍可能被单副本打满），或者
  某个客户端在重试风暴（同一 key 短时间内 3~5 倍流量），或者两个服务共用一个 project
  （限流是项目级，A 服务把 B 服务的额度花掉了）。

### 处置

```bash
# A. 真超量 / 需要临时放开：改表立即生效，不用重启
sqlite3 "$DB" "update projects set rpm_limit=600 where slug='support-bot';"

# B. 误触发的根因是多副本 + memory 限流器：切 redis（共享桶），必须重启
#    先确认 redis 可用
redis-cli -u redis://127.0.0.1:6379/0 ping
#    然后改 env 并滚动重启（AIGATE_RATE_LIMITER / AIGATE_REDIS_URL 只在启动时读）
export AIGATE_RATE_LIMITER=redis
export AIGATE_REDIS_URL=redis://127.0.0.1:6379/0
uv run uvicorn aigate.main:app --port 8080

# C. 预算打满：要么提额，要么让业务方等到下月（月初按 UTC 重置）
sqlite3 "$DB" "update projects set monthly_budget_cny=500.00 where slug='support-bot';"

# D. 客户端侧：告诉调用方遵守 Retry-After（body 里的 retry_after_s），
#    指数退避 + 抖动，不要在 429 上做立即重试。
```

### 回滚

```bash
# 改过的额度改回去（先记下原值，projects 表没有历史）
sqlite3 "$DB" "select slug, rpm_limit, monthly_budget_cny from projects;"
sqlite3 "$DB" "update projects set rpm_limit=60 where slug='support-bot';"

# 切回 memory 限流器：改回 env 再重启；注意多副本下额度会重新变成 N×rpm
export AIGATE_RATE_LIMITER=memory
```

---

## 场景 2：熔断打开导致全路由 502

### 症状

- 502，body `{"error":{"code":"upstream_5xx" | "upstream_timeout" | "gateway.no_candidate", ...}}`
- `request_traces.route_path` 里出现 `{"provider":"dashscope","skipped":"circuit_open","ok":false}`
- `/metrics` 的 `aigate_upstream_errors_total` 在涨，`aigate_fallbacks_total` 不涨（说明
  fallback 也没接住，或者根本没配 fallback）

### 先看哪里

```bash
# 1) 抓一个 request_id，看它到底走了哪条路（有 request_id 就从响应头/body 拿）
curl -s -H "$A" "$GW/admin/v1/traces/req_xxxxxxxx" | python -m json.tool
#    看 route_path：skipped=circuit_open 就是熔断挡掉的；ok=false+error=upstream_5xx 是上游真错

# 2) 这条路由配了哪些候选、有没有 fallback（空数组 = 没有兜底）
curl -s -H "$A" "$GW/admin/v1/models" | python -m json.tool

# 3) 按 provider 看错误分布（确认是单个供应商挂了，还是网关自己挂了）
sqlite3 "$DB" "select provider, error_code, count(*) c
 from usage_records
 where created_at >= datetime('now','-30 minutes') and status='error'
 group by 1,2 order by c desc;"

# 4) 上游到底通不通（用同一把 key / 同一个 base_url）
curl -sS -m 5 "$AIGATE_DASHSCOPE_BASE_URL/models" -H "Authorization: Bearer $AIGATE_DASHSCOPE_API_KEY" | head -c 300
```

判据：连续 3 次 `retryable` 失败才打开熔断（`AIGATE_CIRCUIT_THRESHOLD`），打开后冷却
30s（`AIGATE_CIRCUIT_COOLDOWN_S`），冷却结束**每个请求都会带一次探测**（不是只探测一次）。
所以「刚打开 30 秒内必 502」是设计行为，超过 30s 还全 502，说明上游真的没好。

### 处置

```bash
# A. 上游确实挂了：把好供应商临时顶到第一位（改表实时生效，不用重启）
sqlite3 "$DB" "update route_policies
 set candidates='[{\"provider\":\"mock-alt\",\"model\":\"mock-ok\"}]'
 where match_model='qwen-plus';"

# B. 没有兜底的路由，立刻加 fallback（同上，实时生效）
sqlite3 "$DB" "update route_policies
 set fallback='[{\"provider\":\"mock-alt\",\"model\":\"mock-ok\"}]'
 where match_model='qwen-plus';"

# C. 复位熔断：没有 admin 接口，只能重启进程（单进程时）
#    systemctl restart aigate          # 生产
#    本地：
for pid in $(netstat -ano | grep ":8080" | grep LISTENING | awk '{print $5}' | sort -u); do
  MSYS_NO_PATHCONV=1 taskkill /F /PID $pid
done
uv run uvicorn aigate.main:app --port 8080

# D. 确认恢复
curl -s -o /dev/null -w "%{http_code}\n" -X POST "$GW/v1/chat/completions" \
  -H "Authorization: Bearer $AIGATE_KEY" -H "content-type: application/json" \
  -d '{"model":"qwen-plus","messages":[{"role":"user","content":"ping"}]}'
curl -s "$GW/metrics" | grep -E "upstream_errors|fallbacks"
```

### 回滚

```bash
# 还原 candidates / fallback（改之前先把它存下来！）
sqlite3 "$DB" "select match_model, candidates, fallback from route_policies;" > /tmp/policies.bak
sqlite3 "$DB" "update route_policies
 set candidates='[{\"provider\":\"dashscope\",\"model\":\"qwen-plus\"}]',
     fallback='[{\"provider\":\"mock-alt\",\"model\":\"mock-ok\"}]'
 where match_model='qwen-plus';"

# 重启会清空所有熔断状态（包括你想要的“继续挡着”的状态），
# 重启后 30 秒内观察 upstream_errors 是否重新上涨。
```

---

## 场景 3：语义缓存答错（止血 + 定位是哪条缓存）

### 症状

- 用户反馈「答案不对 / 答的是别人的问题」，但 HTTP 200、上游没有报错
- 响应体 `x_aigate.cache_hit` 为 `"L1"` 或 `"L2"`，L2 时还有 `cache_similarity`
  （0.65~0.75 这种值最可疑）
- `/metrics` 的 `aigate_cache_hits_total{layer="L2"}` 占比异常升高

### 先看哪里

```bash
# 1) 先确认是不是缓存：拿出问题的 request_id 看 trace（route_path 里会写 cache + similarity）
curl -s -H "$A" "$GW/admin/v1/traces/req_xxxxxxxx" | python -m json.tool
#    route_path: [{"cache":"L2","similarity":0.7123,"ok":true}]

# 2) 哪些回答是缓存给的（按请求时间倒序）
sqlite3 "$DB" "select request_id, model_requested, cache_hit, cost_cny, created_at
 from usage_records where cache_hit is not null
 order by created_at desc limit 20;"

# 3) 命中次数最多的缓存条目 = 最可能被错误扩散的那条
sqlite3 "$DB" "select id, model, hit_count, substr(prompt_preview,1,60), created_at
 from semantic_cache_entries order by hit_count desc limit 20;"

# 4) 按问题文本定位（prompt_preview 是归一化后的原文前 300 字）
sqlite3 "$DB" "select id, hit_count, substr(prompt_preview,1,120)
 from semantic_cache_entries where prompt_preview like '%退订%';"
```

要点：L1 是 sha256 精确命中（**不会错**，错了说明是上游当时就答错了），**只有 L2 是相似度
命中**，错答几乎都出在 L2。阈值由 embedder 的 `recommended_threshold` 决定，当前 `main.py`
固定用 `HashEmbedder`（0.65，偏松）；`APIEmbedder` 的 0.92 已有实现但没接线。
0.65 意味着「退订流程是什么」和「不退订怎么操作」可能落到同一条。

### 处置

```bash
# A. 最快止血：整体关缓存（env 只在启动时读，必须重启）
export AIGATE_SEMANTIC_CACHE_ENABLED=false
# 重启进程（命令同场景 2-C）

# B. 只想干掉坏条目（不用重启，缓存每请求查库，删掉立刻生效）
sqlite3 "$DB" "delete from semantic_cache_entries where id='<entry_id>';"

# C. 某个模型的缓存整体不可信（比如换了 prompt 模板）
sqlite3 "$DB" "delete from semantic_cache_entries where model='chat-default';"

# D. 修根因：把阈值收紧后重开（HashEmbedder 0.65 -> 0.9 以上基本只剩近似重复句）
export AIGATE_SEMANTIC_CACHE_ENABLED=true
export AIGATE_CACHE_THRESHOLD=0.92
# 重启进程

# E. 兜底：缓存 TTL 到期自动失效（默认 86400s），量大时可以靠 TTL 等，但别等。
```

### 回滚

```bash
# 重新打开缓存前，务必先删掉坏条目，否则开回来第一秒就 again 命中同一条。
sqlite3 "$DB" "delete from semantic_cache_entries where hit_count > 50;"
export AIGATE_SEMANTIC_CACHE_ENABLED=true
unset AIGATE_CACHE_THRESHOLD     # 回到 embedder 的推荐阈值
# 重启进程，然后验证：连发两次 temperature=0 的同一问题，第二次 cache_hit 应为 L1
```

---

## 场景 4：记账与厂商账单对不上

### 症状

- 月末对账，网关 `sum(usage_records.cost_cny)` 与供应商账单差几个百分点到几倍
- 或某天成本曲线突然掉一半（常见原因：缓存命中变多，或价格行被改）
- `/admin/v1/overview` 的 `cost_cny` 和财务口径不一致

### 先看哪里

```bash
# 1) 按 provider/model 拆账（差异几乎总能收敛到某一行）
sqlite3 "$DB" "select provider, model_used, count(*) reqs,
 round(sum(input_tokens)/1000.0,1) kin, round(sum(output_tokens)/1000.0,1) kout,
 round(sum(cost_cny),4) cny
 from usage_records where created_at >= date('now','start of month')
 group by 1,2 order by cny desc;"

# 2) 未定价的行：cost_cny=0 却是 ok/fallback —— model_prices 里缺这一行
sqlite3 "$DB" "select provider, model_used, status, count(*)
 from usage_records
 where cost_cny=0 and status in ('ok','fallback')
   and created_at >= date('now','start of month')
 group by 1,2,3 order by 4 desc;"

# 3) 当前价格表（注意 currency 默认 CNY，单位是「每 1K token」）
sqlite3 "$DB" "select provider, model, input_price_per_1k, output_price_per_1k, currency, enabled
 from model_prices order by provider, model;"

# 4) 缓存省下来的钱（这部分本来就不该出现在厂商账单里）
sqlite3 "$DB" "select count(*) cached_calls, round(sum(input_tokens+output_tokens)/1000.0,1) ktokens
 from usage_records where status='cached' and created_at >= date('now','start of month');"

# 5) 时间窗口径：budget/month_spend 用 UTC 月初；厂商账单可能按本地自然月
sqlite3 "$DB" "select min(created_at), max(created_at), count(*) from usage_records;"
```

对不上的四种典型原因，按概率排序：

1. **未定价**（第 2 条 SQL 有输出）：供应商上了新模型 / 新 provider 没插 `model_prices`，
   网关按 0 计费。差额方向：**网关 < 厂商**。
2. **缓存命中**：`status='cached'` 的行 cost 是 0，因为确实没调上游。差额方向：**网关 < 厂商**
   只有在厂商账单里出现了这些请求时才说明"缓存其实没生效"（回到场景 3 查）。
3. **失败但上游已计费**：`status='error'` 的行 cost 恒为 0，但流式中断
   （`gateway.stream_interrupted`）上游可能已经出了 token。差额方向：网关 < 厂商。
4. **价格过期**：`model_prices` 有 `effective_from`，但**表上有
   `unique(provider, model)`，不能追加版本行，只能 UPDATE** —— 改价会连带把历史口径改掉。
   差额方向：两边都可能。

### 处置

```bash
# A. 补价格（缺行才 insert；已存在就 update）
sqlite3 "$DB" "insert into model_prices
 (id, provider, model, input_price_per_1k, output_price_per_1k, currency, context_window, enabled, effective_from)
 values (lower(hex(randomblob(16))), 'dashscope', 'qwen-plus', 0.0008, 0.002, 'CNY', 131072, 1, datetime('now'));"

# B. 改价前先备份，改完立刻记录时间点（没有审计表，只能靠人）
sqlite3 "$DB" ".backup '/tmp/aigate-before-price-$DATE.db'"
sqlite3 "$DB" "update model_prices set input_price_per_1k=0.0008, output_price_per_1k=0.002
 where provider='dashscope' and model='qwen-plus';"

# C. 定位"漏记"：网关行数 vs 厂商请求数
#    按天对齐（厂商导出 CSV 后按天 join），差多少条、差在哪个 provider
sqlite3 "$DB" "select date(created_at) d, provider, count(*) from usage_records
 where created_at >= date('now','-7 days') group by 1,2 order by 1 desc;"

# D. 流式请求是后台落库（_persist_stream），进程被 kill -9 会丢行：
#    查启动日志里有没有非正常退出，那段时间的 stream 请求数按访问日志估算后手工补记。
```

### 回滚

```bash
# 价格改错了：从备份取回原值
sqlite3 "$DB" "select input_price_per_1k, output_price_per_1k from model_prices
 where provider='dashscope' and model='qwen-plus';"
sqlite3 "$DB" "update model_prices set input_price_per_1k=<原值>, output_price_per_1k=<原值>
 where provider='dashscope' and model='qwen-plus';"

# 注意：usage_records 里的 cost_cny 是写入时算好的快照，改价不会重算历史。
# 需要按新价重算，只能导出 usage_records 后用同一套 compute_cost 离线重算再对账。
```

---

## 场景 5：SQLite / 数据库锁或慢查询

### 症状

- 500，日志里 `sqlite3.OperationalError: database is locked`，紧跟
  `ERROR aigate | unhandled error rid=... path=/v1/chat/completions`
- p95 从几百毫秒涨到几秒，但响应体里的 `latency_ms.gateway_total` 还很正常（**因为它在落库前
  就算好了**，见「开工前」第 3 条）——这是最容易误判成"网关没问题、是客户端慢"的坑
- 并发翻倍但 rps 不涨（当前实测：5 并发 24 rps，20 并发 29 rps，上游 mock 只要 11ms）
- `/ready` 变慢或超时

### 先看哪里

```bash
# 1) 日志里锁冲突的频次（最重要的一条）
grep -c "database is locked" /var/log/aigate.log
grep -n "database is locked" -B2 /var/log/aigate.log | tail -20   # 看是哪条 SQL 撞的

# 2) 真实用户延迟：访问日志的 ms（这个数包含落库），不是 latency_ms.gateway_total
grep "POST /v1/chat/completions" /var/log/aigate.log | tail -50 | awk '{print $NF, $0}' | tail -5

# 3) 表有多大（每条请求写 2 行 + 1 次 api_keys update）
sqlite3 "$DB" "select 'usage_records', count(*) from usage_records
 union all select 'request_traces', count(*) from request_traces
 union all select 'semantic_cache_entries', count(*) from semantic_cache_entries;"

# 4) 当前日志模式与锁等待（delete = 读也阻塞写；wal = 读写不互斥）
#    busy_timeout 是「每连接」参数，aiosqlite 默认就是 5000ms —— 它是 5000 还报
#    database is locked，说明撞的是 COMMIT 阶段的写冲突（超时参数救不了，只能减并发或换库）
sqlite3 "$DB" "pragma journal_mode; pragma busy_timeout; pragma synchronous;"

# 5) 连接池只有 5+10 条连接，20 并发就开始排队（AsyncAdaptedQueuePool）
uv run python -c "
from sqlalchemy.ext.asyncio import create_async_engine
e = create_async_engine('sqlite+aiosqlite:///$DB')
print(type(e.pool).__name__, e.pool.size(), e.pool._max_overflow)"
```

### 处置

```bash
# A. 立刻缓解：切 WAL（对本库持久生效，不用重启；需要无其他活跃写连接时才能切成功）
#    实测：rps 26~29 -> 37.3，p95 2.1~2.4s -> 1.7s，本轮 500 归零；但根治不了单写者
sqlite3 "$DB" "pragma journal_mode=WAL;"
#    注意：busy_timeout 不能这样"设一次就好"——它是每连接参数，
#    应用侧默认已是 5000ms（aiosqlite 的 timeout 默认值），别把它当成解法。

# B. 清历史：trace 只用于排查，可以按窗口删（先备份）
sqlite3 "$DB" ".backup '/tmp/aigate-before-prune.db'"
sqlite3 "$DB" "delete from request_traces where created_at < datetime('now','-7 days');"
sqlite3 "$DB" "vacuum;"     # 会锁库，低峰期做

# C. 止血：降并发 / 单写者意味着加副本没用（SQLite 文件不能多副本共享）

# D. 根治：切 PostgreSQL（代码同一套，只换 URL）
export AIGATE_DATABASE_URL="postgresql+asyncpg://aigate:pass@127.0.0.1:5432/aigate"
uv run uvicorn aigate.main:app --port 8080
#   PostgreSQL 侧建议补索引（usage_records 只会越来越大）：
#   create index concurrently ix_usage_project_created on usage_records (project_id, created_at desc);
#   慢查询排查：
#   select pid, now()-query_start dur, left(query,80) from pg_stat_activity
#    where state='active' order by dur desc limit 10;
```

### 回滚

```bash
# 从 WAL 退回 delete（必须在没有其他连接时做，否则报 database is locked）
sqlite3 "$DB" "pragma journal_mode=delete;"

# 从 PG 退回 SQLite：改回 env 重启；切换期间的 PG 写入需要回灌，
# 否则 usage_records 会出现缺口（这正是场景 4 里"漏记"的来源之一）。
export AIGATE_DATABASE_URL="sqlite+aiosqlite:///./aigate.db"
```

---

## 场景 6：上游供应商区域性故障（降级链路是否生效）

### 症状

- 某个 provider 的错误率飙升，`usage_records.status='fallback'` 与 `degraded=1` 的比例上升
- 响应体 `x_aigate.degraded=true`，`provider` 已经是备用供应商
- `/metrics` 的 `aigate_fallbacks_total{provider=...}` 上涨
- 如果 fallback 没配或者也挂了 → 502，回到场景 2

### 先看哪里

```bash
# 1) 降级是否真的生效（有 degraded=1 且 http 200，就说明接住了）
sqlite3 "$DB" "select provider, model_used, status, count(*) c
 from usage_records where created_at >= datetime('now','-30 minutes')
 group by 1,2,3 order by c desc;"

# 2) 错误集中在哪个供应商/错误码（区域性故障通常是一个 provider 的 5xx/timeout）
sqlite3 "$DB" "select provider, error_code, count(*) c
 from usage_records
 where created_at >= datetime('now','-30 minutes') and status='error'
 group by 1,2 order by c desc;"

# 3) 抽一条降级请求，确认 route_path 是「第 1 次失败、第 2 次成功」
sqlite3 "$DB" "select request_id from usage_records
 where status='fallback' order by created_at desc limit 1;"
curl -s -H "$A" "$GW/admin/v1/traces/<request_id>" | python -m json.tool

# 4) 路由配置（fallback 为空 = 没有降级链路）
curl -s -H "$A" "$GW/admin/v1/models" | python -m json.tool

# 5) 直接打供应商的区域端点，区分「区域挂了」和「网关配置错了」
curl -sS -m 5 -o /dev/null -w "%{http_code} %{time_total}s\n" \
  "$AIGATE_DASHSCOPE_BASE_URL/models" -H "Authorization: Bearer $AIGATE_DASHSCOPE_API_KEY"
```

判据：`degraded=true` 的请求数 > 0 且这些请求返回 200 → 降级链路**生效**，问题只是上游质量下降。
`degraded` 一直是 0 且错误率涨 → 降级链路没接上（fallback 为空，或备用供应商也被熔断）。

### 处置

```bash
# A. 换区域：供应商给了备用 base_url（env 只在启动时读，必须重启）
export AIGATE_DASHSCOPE_BASE_URL="https://dashscope-cn-beijing.aliyuncs.com/compatible-mode/v1"
# 重启进程

# B. 不重启的临时切换：把主候选摘掉，改表实时生效
sqlite3 "$DB" "update route_policies
 set candidates='[{\"provider\":\"mock-alt\",\"model\":\"mock-ok\"}]'
 where match_model='qwen-plus';"

# C. 确认降级生效（跑 20 次，至少要有 200 且 degraded 有值）
curl -s -X POST "$GW/v1/chat/completions" \
  -H "Authorization: Bearer $AIGATE_KEY" -H "content-type: application/json" \
  -d '{"model":"qwen-plus","messages":[{"role":"user","content":"ping"}]}' \
  | python -c "import sys,json;m=json.load(sys.stdin)['x_aigate'];print(m['provider'],m['degraded'])"

# D. 压一下确认容量：降级供应商通常配额更小
uv run python tests/load/loadtest.py --base "$GW" --key "$AIGATE_KEY" --model chat-default \
  --concurrency 5 --requests 100
```

### 回滚

```bash
# 区域恢复后：还原 base_url / candidates，重启，然后盯 15 分钟
sqlite3 "$DB" "update route_policies
 set candidates='[{\"provider\":\"dashscope\",\"model\":\"qwen-plus\"}]'
 where match_model='qwen-plus';"
unset AIGATE_DASHSCOPE_BASE_URL
# 重启进程

# 观察降级比例回落到 0：
sqlite3 "$DB" "select status, count(*) from usage_records
 where created_at >= datetime('now','-5 minutes') group by 1;"
```

注意：降级期间的答案**不会写进语义缓存**（`_complete()` 里 `degraded` 时不 store），
所以回滚后不会继续吐备用模型的答案 —— 这点不用额外处理。

---

## 附：压测与容量基线

```bash
# 纯 Python（httpx + asyncio），无额外依赖
uv run python tests/load/loadtest.py --base http://127.0.0.1:8080 --key "$AIGATE_KEY" \
  --concurrency 20 --requests 200

# 有 k6 的环境（等价的 shared-iterations 场景）
k6 run -e KEY="$AIGATE_KEY" -e CONCURRENCY=20 -e REQUESTS=200 tests/load/k6.js
```

脚本的判定口径与本文一致：**429 记为 throttled，不计入 error_rate**，但两者各自有阈值，
任一超标都返回非零退出码（可直接接 CI）。`--cached` 模式用固定 prompt 池 + `temperature=0`
才能命中缓存。

本机基线（mock 上游 11ms，SQLite 默认 `journal_mode=delete`，单进程 uvicorn；除注明外均为
20 并发 × 200 请求）：

| 场景 | 并发 | 总请求 | rps | p50 | p95 | 备注 |
|---|---|---|---|---|---|---|
| 默认 60 rpm | 20 | 200 | 48.3 | 220ms | 1364ms | 141/200 被限流，测的是限流器 |
| 放开 rpm | 20 | 200 | 26.0 | 341ms | 2138ms | 200/200 成功、0 错误，瓶颈是 SQLite 写 |
| 放开 rpm + WAL | 20 | 200 | 37.3 | 229ms | 1697ms | p95 降 ~25%，仍不达标 |
| 放开 rpm（低并发） | 5 | 200 | 24.1 | 163ms | 331ms | 唯一通过阈值的一档 |
| 放 rpm + 缓存 | 5 | 100 | 34.4 | 86ms | 375ms | 100% L1 命中，上游 0ms |

读法：mock 上游 11ms，网关自身处理（`gateway_total`）p50 约 37ms，剩下的 200~500ms 全是
**落库排队**（每请求 2 行写 + 1 次 update，SQLite 单写者）。所以"网关慢"在这套
默认配置下几乎总是 DB 慢 —— 先看场景 5。并发从 5 加到 20，rps 基本不动（24 → 26），
延迟 p95 从 331ms 涨到 2138ms，这就是单写者饱和的典型形状。
---

## 场景 7：有人离职 / 怀疑账号被盗（停用与吊销）

### 症状

- HR 说某人今天last day，要继续用系统；或有人报告「控制台里出现了我没做过的操作」
- 审计页出现异常时间的写操作，或某个 key 的用量突然翻倍

### 先看哪里

```bash
# 1) 这个人到底做过什么（审计按邮箱过滤，时间倒序）
sqlite3 "$DB" "select created_at, action, target, status_code, ip
 from audit_logs where actor='someone@team.local'
 order by created_at desc limit 50;"

# 2) 他的账号状态与最后登录时间
sqlite3 "$DB" "select email, role, status, last_login_at from console_users;"

# 3) 他名下签过哪些 Key（含已吊销）
curl -s -H "$A" "$GW/admin/v1/keys" | python -m json.tool | grep -E '"(name|prefix|status|last_used_at)"'
```

### 处置

```bash
# 停用账号：下一次请求就 401，不用等会话过期（会话是无状态 cookie，靠每请求查 status）
curl -s -X POST -H "$A" "$GW/console/v1/users/<user_id>/disable"

# 吊销他签的 Key（逐把，按 prefix 确认）
curl -s -X POST -H "$A" "$GW/admin/v1/keys/<key_id>/revoke"

# 紧急情况：轮换 session secret，所有人立即下线
sed -i 's/^AIGATE_SESSION_SECRET=.*/AIGATE_SESSION_SECRET='"$(openssl rand -base64 32)"'/' .env
# 然后重启网关（up.sh 会做）
```

### 回滚

停用是软删除（`status='disabled'`），把人改回 `active` 即可；不需要重建账号。
Key 吊销**不可逆**——对方要重新签发一把，历史账单仍挂在旧 key 上，不要为「好看」而删行。

---

## 场景 8：账单对不上（先分清是丢账、漏记还是没算）

### 症状

- 财务说「这个月某团队的费用比预期高 / 低」
- 业务说「我打了 1 万次，报表只有 6 千次」

### 先看哪里

```bash
# 1) 有没有丢账：spool 里还有没有未 ack 的行（正常应为 0 或只有刚写入的几毫秒）
wc -l aigate_metering.spool 2>/dev/null || echo "no spool"

# 2) 总数对不对：usage_records（账单）vs request_traces（排查）vs 网关日志行数
sqlite3 "$DB" "select count(*) from usage_records where created_at >= date('now','start of month');"
sqlite3 "$DB" "select count(*) from request_traces where created_at >= date('now','start of month');"

# 3) 按状态拆：cached 是成本 0 的命中行，fallback 是降级，error 是不该计费的
sqlite3 "$DB" "select status, count(*) c, round(sum(cost_cny),6) cost
 from usage_records where created_at >= date('now','start of month')
 group by 1 order by c desc;"

# 4) 限流与预算拦截的请求不落库，要单独看
curl -s "$GW/metrics" | grep -E "aigate_(rate_limited|budget_denied)_total"
```

判据：`request_traces` 与 `usage_records` 条数差异只能来自「限流/预算拒绝」（这两类在写库前就 return），
差额应等于 `rate_limited_total + budget_denied_total`。**不等就是丢账**，回场景 0。

### 处置

- 差额有解释 → 正常，不要重算。
- 差额无解释 → 先看 spool 有无残留，再查 `journal_mode`（应为 WAL）与 `database is locked`
  在日志里的出现次数。
- 客户质疑单价 → `model_prices` 是按 provider+model 的单价表，改价只影响之后的行，
  历史行保留当时的单价快照。

### 回滚

调价：插入新的 `model_prices` 行并停用旧的，**不要 UPDATE 历史**。
补账：只有确认丢账才补，补的时候带上原 `request_id`，否则会重复计费（回放去重靠它）。
