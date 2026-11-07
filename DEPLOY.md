# 部署

三种形态，按「给谁用」选，别混着用：

| 形态 | 适合 | 命令 | 状态 |
|---|---|---|---|
| 本机 | 一个人用、演示、面试现场 | `./scripts/up.sh` | ✅ 长期在跑 |
| 单机 Compose | 团队内网一台 VM（5–20 人） | 见下 | ⚠️ 文件齐全，本机无 Docker，**未实跑** |
| 多副本 / K8s | 有 SLA 的对外服务 | 不在本仓库范围 | ❌ 见「不适用」 |

---

## 1. 单机 Compose（团队内网）

```bash
git clone https://github.com/Wind-fat/aigate && cd aigate
cp .env.production.example .env.production
# 生成两个密钥，粘贴进 .env.production
openssl rand -base64 32   # -> AIGATE_ADMIN_TOKEN
openssl rand -base64 48   # -> AIGATE_SESSION_SECRET
# 顺手改掉 POSTGRES_PASSWORD
uv run python scripts/check_deploy.py --env-file .env.production   # 先静态检查
docker compose --env-file .env.production up -d --build
docker compose --env-file .env.production ps
```

起来之后：

- 控制台 `http://<host>:3000` —— 第一次要建 owner：
  `docker compose exec gateway python scripts/setup.py --email you@team.local`
  （`setup.py` 幂等，会跳过已存在的用户和文件）
- 网关 `http://<host>:8080/docs` —— 业务代码把 `base_url` 指向这里
  （兼容面是文本补全：`messages`/`temperature`/`top_p`/`max_tokens`/`stop`/`user`/`stream`。
  `tools`/`response_format` 等字段会被 400 `gateway.unsupported_parameter` 拒掉，不会静默忽略）
- 健康检查 `curl -f http://<host>:8080/health`

**启动顺序是设计过的**：`db`/`redis` 健康 → `migrate` 跑 `alembic upgrade head` 并退出 0 →
`gateway` 起 → `console` 起。少任何一环，`docker compose up` 会直接报错而不是起来一个半残的服务。

### 反向代理（HTTPS 必须）

控制台的会话 cookie 在 `AIGATE_COOKIE_SECURE=true` 时只走 HTTPS，而 `AIGATE_ENV=prod`
会强制它。所以对外一定套一层 TLS。

Caddy（自动证书，最省事）：

```caddyfile
aigate.internal {
    handle /console* {
        reverse_proxy console:3000
    }
    reverse_proxy gateway:8080
}
```

Nginx：

```nginx
server {
    listen 443 ssl;
    server_name aigate.internal;
    client_max_body_size 20m;

    location / {
        proxy_pass http://127.0.0.1:3000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }

    # 流式响应必须关缓冲，否则 SSE 会被攒成一次性输出
    location /v1/ {
        proxy_pass http://127.0.0.1:8080;
        proxy_buffering off;
        proxy_read_timeout 300s;
        proxy_set_header Host $host;
    }
}
```

> `proxy_buffering off` 这一行是流式的开关。少了它，Playground 的“逐字输出”会变成
> “等 20 秒一次性吐出来”，然后被当成网关 bug。

---

---

## 1.5 Redis：本机怎么跑、生产怎么跑

限流与熔断都支持 `memory`（单进程）和 `redis`（多副本共享）。默认 `memory`——单人开发不需要外部依赖；
**两个副本以上必须换 redis**，否则额度变成 `N × rpm`、熔断阈值也变成 `N × threshold`。

### A. 本机（Windows、无 Docker、无管理员权限）

Windows 没有官方 redis-server。这里用便携版（zip，免安装、不需要管理员）：

```bash
./scripts/redis.sh up       # 首次自动下载 v5.0.14.1 到 tools/redis/ 并启动
./scripts/redis.sh status   # PONG + 版本
./scripts/redis.sh down
```

然后让网关用 Redis 后端（写进 `.env`，或启动时内联）：

```bash
AIGATE_RATE_LIMITER=redis \
AIGATE_CIRCUIT_BACKEND=redis \
AIGATE_REDIS_URL=redis://127.0.0.1:6379/0 \
./scripts/up.sh
```

验证（两步都要看）：

1. **熔断后端**：打开 `/ops`，「熔断」卡片应显示 `redis · 阈值 3 次 / 冷却 30s`。
   仍是 `memory` 说明环境变量没生效（`uv sync --extra redis` 装了客户端吗？）。
2. **额度真共享**：把某个项目 RPM 调到 5，起两个副本（8080 / 8081）交替请求 8 次，
   应当 **第 6 次开始 429**。若每个副本各放过 5 次，就是还在用内存后端。

> **已知坑（已修）**：`redis-py 8` 连接时默认发 `HELLO 3`（RESP3），而 Windows 能拿到的
> Redis 5.0.x 不认识这个命令，表现为每个请求 500 `unknown command HELLO`。
> 代码里已固定 `protocol=2`；换到 Redis 7 也不需要改。

### B. 服务器（Docker Compose，推荐）

compose 里已经有 `redis:7-alpine`（`--save "" --appendonly no`，纯缓存/计数器，不需要持久化）。

```bash
cp .env.production.example .env.production    # 填 4 个密钥：ADMIN_TOKEN / SESSION_SECRET / SECRET_KEY / POSTGRES_PASSWORD
docker compose --env-file .env.production build
docker compose --env-file .env.production up -d
docker compose --env-file .env.production run --rm migrate   # alembic upgrade head
```

网关的 `AIGATE_REDIS_URL` 在 compose 里是 `redis://redis:6379/0`（走 compose 网络，不暴露端口），
`AIGATE_RATE_LIMITER` 与 `AIGATE_CIRCUIT_BACKEND` 都是 `redis`。

验证：

```bash
curl -s localhost:8080/health
curl -s -H "X-Admin-Token: $AIGATE_ADMIN_TOKEN" localhost:8080/admin/v1/ops-health | jq '.breaker, .db'
# 期望 breaker.backend = "redis"，db.backend 含 postgresql
```

### C. 不用 Docker 的服务器部署（systemd）

内部团队常见做法，比 compose 少一层：

```ini
# /etc/systemd/system/aigate.service
[Unit]
Description=AIGate gateway
After=network-online.target postgresql.service redis.service

[Service]
User=aigate
WorkingDirectory=/opt/aigate
EnvironmentFile=/etc/aigate/aigate.env        # AIGATE_ENV=prod + 上面那几个密钥
ExecStart=/opt/aigate/.venv/bin/uvicorn aigate.main:app --host 127.0.0.1 --port 8080 --workers 2
Restart=always
RestartSec=3
# 让 spool 有机会落盘（记账不丢账的前提）
KillSignal=SIGTERM
TimeoutStopSec=20

[Install]
WantedBy=multi-user.target
```

- `--workers 2` 就是两个副本：**必须**把限流/熔断切到 redis（见上）。
- 控制台单独跑：`cd console && npm ci && npm run build && npm start -- -p 3000`，
  用 nginx 把 `/` 反代到 3000、`/v1` 反代到 8080。
- 生产必须 `AIGATE_SECRET_KEY` 与 `AIGATE_SESSION_SECRET` 分开（`check_deploy.py` 会拦）。

### D. 本仓库没有验证的部分

- 本机**没有 Docker**（也没有管理员权限装 WSL2 / Docker Desktop），所以两个镜像**没有 build 过**。
  能做的是静态校验：`uv run python scripts/check_deploy.py --env-file .env.production`（29 项）
  加 `next build` 与 `uv sync` 的产物检查。
- Redis 的 `redis` 后端**已在本机实测**（便携版 5.0.14）：共享额度、共享熔断都验过；
  但**没有**在 Redis 7 的高并发下压过。
- PostgreSQL：试过 Windows 便携版二进制（EDB 16.4），`initdb` 能过，但 `pg_ctl start` 时
  postgres 子进程以 `0xC0000142`（DLL 初始化失败）退出——是二进制/运行时环境的问题，
  不是网关代码的问题。所以 **PG 路径仍然只有离线 DDL 与迁移测试**，没有真机跑过。
  要验的话最省事的是在一台 Linux 上 `apt install postgresql` 或直接 `docker compose up db`。

---

## 2. 备份与恢复

两个东西要备份，缺一不可：

```bash
# 1) 账单与配置（PostgreSQL）
docker compose exec -T db pg_dump -U aigate aigate | gzip > backup-$(date +%F).sql.gz

# 2) 记账 spool（未落库的最后几毫秒）
docker run --rm -v aigate_spool:/data -v "$PWD":/out alpine \
  tar czf /out/spool-$(date +%F).tgz -C /data .
```

恢复：

```bash
gunzip -c backup-2026-11-06.sql.gz | docker compose exec -T db psql -U aigate -d aigate
docker run --rm -v aigate_spool:/data -v "$PWD":/in alpine tar xzf /in/spool-2026-11-06.tgz -C /data
```

**为什么要单独备 spool**：账单写入是「先落盘 → 再批量入库 → 成功后才 ack」。进程在
ack 前被杀，未提交的那几笔只在 spool 里。只备数据库，这几笔就没了——一次部署重启
丢掉几十块钱的账，是财务最不能接受的形态。

---

## 3. 升级与回滚

```bash
git pull
docker compose --env-file .env.production build
docker compose --env-file .env.production up -d        # 只重建有变化的容器
```

- 迁移是**只向前**的：`alembic upgrade head` 在启动序列里自动跑；回滚一个版本用
  `alembic downgrade -1`，但只在确认老代码能读新表结构时才有意义。
- 回滚代码：`git checkout <上一个 tag>` → 重新 build。**先回滚代码再降迁移**，顺序反了
  会出现「旧代码读新列」的 500。
- 灰度：本 compose 是单副本，没有灰度能力。要灰度就得放到 K8s 或加一层负载均衡，
  而多副本的第一道坎不是编排，是配额/熔断状态（现在在进程内存里，见 ADR-0003/0004）。

---

## 4. 日常巡检（3 条命令）

```bash
curl -s localhost:8080/health                 # {"status":"ok",...}
curl -s localhost:8080/metrics | grep -E "rate_limited|fallback"
docker compose logs --tail 50 gateway | grep -i error
```

控制台「概览」页看四件事：今日成本、成功率、P95、降级次数。预算告警会出现在同一页顶部
（按「范围 / 指标 / 周期」去重，warn 升到 critical 时会重新变成未确认）。异常时的处置见
[`docs/runbook.md`](docs/runbook.md)。

### 连接池：预热、超时与 503

`AIGATE_DB_POOL_WARM`（默认开）让网关在 lifespan 里按 `pool_size` 并发建连，避免冷启动时首屏那 3–5 个并发请求各自付一次 ~110 ms 握手。结果写在 `/health` 的 `pool` 字段：`state` 是 `warm` / `partial` / `failed` / `timeout` / `skipped`（`:memory:` 库）/ `cold`（还没预热过），`warm`/`target` 是实际条数与目标条数，`ms` 是耗时。同样的两个值以 `aigate_pool_warm_ms`、`aigate_pool_warm_connections` 进 `/metrics`，超时另计 `aigate_pool_warm_timeout_total`。预热失败或超时**都不影响启动**：池没热只意味着更慢，证据在 `/health` 里，不需要让进程起不来。

`/metrics` 上这两个 gauge 的形状（同一行文本，`grep` 得到）：

```bash
$ curl -s http://<host>:8080/metrics | grep '^aigate_pool_warm'
aigate_pool_warm_connections 5
aigate_pool_warm_ms 33.8
```

（上面这两行是本机 SQLite 实测：`state=warm / warm=5 / target=5 / ms=33.8`，与 `/health.pool`
逐字段一致。PG 上单条连接要 ~110 ms，数字会大一个量级——口径看 `/health`，别跨库比。）

这两个值来自最近一次预热（gauge 覆盖写，不累加），与 `/health.pool.warm` / `.ms` 同源；
超时次数是 counter，重启后从 0 重新开始。`tests/test_usage_stats_contract.py::test_pool_warm_gauges_are_actually_in_the_metrics_text`
确实起 ASGI 应用、预热一次、再从 `/metrics` 响应体里解析这两行——不是只查内部 `snapshot()`。

预热有硬上限 `2 × AIGATE_DB_POOL_TIMEOUT`（默认 1s，即最多 2s），到点收工、`state` 记为 `timeout`。`AIGATE_DB_POOL_TIMEOUT` 同时也是"池满时等一条连接多久"的闸值：超了不会挂成 500，而是 `503` + `Retry-After`（秒值由同一个配置向上取整），错误体是 `gateway.pool_timeout` 并带 `retry_after_s`。调用方按 `Retry-After` 退避重试即可，值班不要把它当故障告警；只有连续 503 才说明 `workers × (pool_size + max_overflow)` 真的不够了。

### 池容量：从哪来、怎么配、什么时候会 503

`pool_size` 与 `max_overflow` 走环境变量（默认 `5` / `10`，就是 SQLAlchemy 的出厂值，不配等于没改）：

| 变量 | 默认 | 含义 |
|---|---|---|
| `AIGATE_DB_POOL_SIZE` | `5` | 每个进程常驻连接数（池不缩回去的那部分） |
| `AIGATE_DB_POOL_OVERFLOW` | `10` | 每进程允许临时多开几条（用完即关） |
| `AIGATE_DB_WORKERS` | `1` | 共用这个库的进程数，填 `uvicorn --workers` 的值；只参与容量校验 |
| `AIGATE_DB_MAX_CONNECTIONS` | `100` | 数据库端上限（PG 的 `SHOW max_connections`，出厂 100）；填 `0` = 不校验 |

启动时按公式算一遍，**只判定，不改行为**：

```
workers × (pool_size + max_overflow) + 1（运维预留） ≤ max_connections × 0.8
```

不满足 → 启动日志一条 `WARNING db pool capacity over budget ...`，并把算式写进 `GET /health` 的 `db_capacity`（`verdict: over` + `warning` 字段）。进程照常启动、照常服务：配置不合理是运维问题，不该变成不可用。

参考值（`max_connections=100` 的 PG；判定列是该行的实际结果）：

| 场景 | pool_size | max_overflow | workers | 峰值连接 | 判定 |
|---|---|---|---|---|---|
| PG 单副本 | 10 | 20 | 1 | 10+20+1 = **31** ≤ 80 | ok |
| PG 2 副本（`--workers 2`） | 10 | 10 | 2 | 2×20+1 = **41** ≤ 80 | ok |
| PG 4 副本 | 10 | 10 | 4 | 4×20+1 = **81** > 80 | over（要加 max_connections 或降 pool_size） |
| SQLite（默认，单写者） | 5 | 10 | 1 | 16（仅参考） | ok / `enforced:false` |

SQLite 没有 `max_connections`，真正的闸是单写者锁；那一行的 `enforced` 为 `false`，数字只能说明"这组配置相对不健康"，改 `pool_size` 不会提升写并发。

**什么时候会 503**：某进程池里的 `pool_size + max_overflow` 条连接全被占住、并且等 `AIGATE_DB_POOL_TIMEOUT`（默认 1s）还拿不到连接。返回 `503` + `Retry-After` + `error.code=gateway.pool_timeout`（body 带 `retry_after_s`），同时 `aigate_pool_timeout_total` +1。偶发一次是准入设计；**连续 503 就是容量真的不够**，先看 `/health.db_capacity` 的判定，再调上面四个值——调完必须重启生效。

### Grafana（可选，但有了更好交差）

§1 的 compose 不带 Prometheus/Grafana——内部团队通常已经有自己的一套，重复装一份只会多一个要维护的东西。
接进现有监控只需两步：

```yaml
# prometheus.yml 里加
scrape_configs:
  - job_name: aigate
    metrics_path: /metrics
    static_configs:
      - targets: ["aigate-gateway:8080"]
```

然后导入 [`deploy/grafana/aigate-dashboard.json`](deploy/grafana/aigate-dashboard.json)。
看板里**故意没有 p95 和金额**，原因写在 [`deploy/grafana/README.md`](deploy/grafana/README.md)：
直方图的基数代价和账本归属。p95 在控制台现算（`usage_records`），比指标更准。

---

## 5. 容量参考（别当成承诺）

单副本、SQLite 起量的实测值（`tests/load/loadtest.py`，20 并发 × 200 请求）：

| 指标 | 值 | 说明 |
|---|---|---|
| 吞吐 | 51 rps | SQLite 单写者上限；PostgreSQL 未实测 |
| 客户端 p50 / p95 | 238 / 687 ms | 含落库排队 |
| 网关自身 p50 / p95 | 34 / 59 ms | 不含落库 |
| 错误率 | 0% | `database is locked` 已修（WAL + timeout） |

换算：51 rps ≈ 每天 440 万次调用 ≈ 一个 20 人团队用不完。真到这个量级，先换
PostgreSQL，再谈多副本。

---

## 6. 本仓库**没有**验证的部分（写清楚，而不是藏起来）

1. **Compose 从未在本机跑过**——开发机没有 Docker。所以有了
   `scripts/check_deploy.py`：它检查构建上下文、COPY 源文件、必填环境变量、
   端口冲突、`depends_on` 的健康检查依赖、以及「生产必须 postgres+redis」。
   它检查不了的是镜像能不能真的构建、容器能不能真的互通。
2. **PostgreSQL 路径未实测**。ORM 是同一套（ADR-0001），Alembic 迁移在 SQLite 上
   验过 upgrade/downgrade/幂等，但 `asyncpg` + pgvector 的真实连接没有跑过。
3. **多副本不支持**：限流与熔断状态在进程内，两个副本的实际配额是 `2 × rpm`。
4. **没有备份自动化**：上面的命令是手工的，生产该挂 cron。
5. **没有 K8s manifests**：不为没有的需求造 YAML。

第一次真机部署时，按这个顺序验证，能把风险压到最小：

```bash
docker compose build                              # 1. 镜像能不能构建
docker compose up -d db redis                     # 2. 存储能不能起来
docker compose run --rm migrate                   # 3. 迁移能不能跑
docker compose up -d gateway                      # 4. 网关健康检查
curl -X POST localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer <key>" -H "Content-Type: application/json" \
  -d '{"model":"chat-default","messages":[{"role":"user","content":"hi"}]}'   # 5. 真打一条
docker compose up -d console                      # 6. 控制台登录
```