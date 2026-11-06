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