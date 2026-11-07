# 怎么用（团队内部版）

这是给一个团队内部用的工具，不是对外服务。跑在一台机器上（或者团队内网的一台 VM），
SQLite 起步，PostgreSQL 可选，一条命令起停。

**它适合**：团队里几个项目共用一个模型入口，看清每次调用的成本、缓存和降级；谁签的 Key、
谁改了配置、谁花的钱，都能查到人。
**它不适合**：对外收费、多租户 SaaS、有 SLA 承诺的场景（原因见 `docs/adr/0007` 与
README 的「已知限制」）。

---

## 1. 启动（首次约 3 分钟，之后 5 秒）

```bash
git clone https://github.com/Wind-fat/aigate && cd aigate
uv sync --all-groups
uv run python scripts/setup.py --email you@team.local   # 建 .env、建库、建第一个 owner
./scripts/up.sh
```

`setup.py` 会：生成随机 admin token 与 session secret → 写 `.env` 与
`console/.env.local` → 建库并灌示例数据 → 建 owner 账号（没给 `--password` 就随机生成，
**只打印一次**）。

`up.sh` 会：起网关（:8080）和控制台（:3000）→ 打开浏览器。**Ctrl-C 一起停。**

浏览器打开 <http://127.0.0.1:3000>，先用上面那个邮箱密码登录，再进「工作台」→ 点
「签发临时 Key」→ 直接发一条请求，右下角能看到 `provider / 成本 / 缓存 / 降级 / 上游耗时`。

> 不想开浏览器：`GATEWAY_PORT=8081 ./scripts/up.sh`，或只用 API（`/docs` 有 Swagger）。
> 忘了密码：`uv run python scripts/setup.py --email you@team.local --force` 重设。

## 1.5 加人、分权、查账

```bash
# 加一个只读成员（财务、实习生）
uv run python scripts/setup.py --add-user finance@team.local --role viewer

# 加一个能签发 Key、但创建不了 owner 的成员
uv run python scripts/setup.py --add-user dev@team.local --role admin
```

登录后左侧「成员」页能做同样的事，还能一键停用（停用后下一次请求就 401，不用等会话过期）。
「审计」页是只追加的写操作流水：谁、什么时候、对哪个接口、返回什么、`request_id` 是多少。
按 `actor` 可以查出某个人的全部动作。

## 2. 接上自己的代码

OpenAI SDK 的**对话补全**能直接用，只改两行（`tools` / `response_format` 等网关不转发的
字段会被 400 `gateway.unsupported_parameter` 拒绝，见 README「API · 请求参数是白名单」）：

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8080/v1",   # ← 只改这里
    api_key="sk-ag-...",                    # ← 控制台「Key 管理」里签发的那把
)

r = client.chat.completions.create(model="chat-default", messages=[{"role": "user", "content": "hi"}])
print(r.choices[0].message.content)
```

```bash
curl -s http://127.0.0.1:8080/v1/chat/completions \
  -H "Authorization: Bearer sk-ag-..." -H 'Content-Type: application/json' \
  -d '{"model":"chat-default","messages":[{"role":"user","content":"hi"}]}'
```

响应里多一个 `x_aigate` 块（成本、缓存命中、是否降级、分段耗时），不影响任何 SDK 解析。

## 3. 换成真实模型

默认是离线 `mock`（不需要任何 key，能跑通全链路）。要用真模型：

```bash
# 1) 把 key 写进 .env（不要写在命令行里，会进 shell history）
echo 'AIGATE_DASHSCOPE_API_KEY=sk-xxxx' >> .env

# 2) 把 chat-default 指到真实模型
uv run python scripts/use_provider.py dashscope qwen-plus

# 3) 验证一条真实调用（会自动签一把临时 key）
uv run python scripts/use_provider.py dashscope qwen-plus --verify
```

支持 `dashscope` / `deepseek` / `vllm`（本地模型），`--list` 看默认模型。
切歪了想回离线模式：`uv run python scripts/use_provider.py mock`。

**每次切换都会保留 `mock-alt` 作为 fallback**，所以供应商挂了你能看到 `degraded=true`
而不是报错页——这正是这个工具存在的意义。

## 4. 日常看什么

| 页面 | 什么时候看 |
|---|---|
| 概览 | 今天花了多少钱、成功率、P95、缓存命中率 |
| 调用明细 | 某条请求花了多少 token / 钱，是否走了缓存或降级 |
| Trace | 一次请求的各段耗时（配额/脱敏/缓存/上游/落库），以及路由尝试了几次 |
| Key 管理 | 发新 key、吊销旧 key、看最后使用时间（识别僵尸 key） |

## 5. 出问题

先看 [`docs/runbook.md`](runbook.md)，覆盖 6 个常见场景（429、熔断、缓存错答、
账单对不上、DB 锁、供应商区域故障），每条都有可直接复制的命令。

最快的三条自检：

```bash
curl -s --noproxy '*' http://127.0.0.1:8080/health          # 网关心跳
curl -s --noproxy '*' http://127.0.0.1:8080/metrics | head  # 限流/错误/缓存计数
uv run python evals/run.py smoke --base-url http://127.0.0.1:8080  # 20 条自检
```

## 6. 重置

```bash
rm -f aigate.db*          # 清库（下次启动会重新 seed）
rm -f .env console/.env.local   # 重置 token（顺便让控制台重启后重新读）
./scripts/up.sh --force   # 或者：uv run python scripts/setup.py --force
```

---

## 和「生产版」的区别（先说清楚）

这台机器上的版本是**单用户本地工具**：控制台的 token 只存在 Next.js 服务端进程里，
不发给浏览器（这一点已经修好），但限流/熔断状态仍在进程内存、记账队列在内存、
数据在 SQLite。

要给别人用之前必须先补的六件事，README 的「已知限制」里逐条列了，不要跳。