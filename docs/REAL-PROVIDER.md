# 真实供应商验证（DashScope / qwen3.8-flash）

这份文档记录一次**真的打外部厂商**的验证：不是 mock，不是 MockTransport，请求真的出了本机、
账单真的是供应商算的。每一步都给了命令和原始输出，可以照着重跑。

- 日期：2026-11-08（Asia/Shanghai）
- 基线：`8679ebf`，`291 passed`
- 上游：DashScope compatible-mode `https://dashscope.aliyuncs.com/compatible-mode/v1`
- 模型：`qwen3.8-flash`（推理模型；vendor 白名单里 `Qwen (China)` 提供的 6 个模型之一）
- 真实调用次数：**5 次**（4 次非流式 + 1 次流式），每次 `max_tokens ≤ 96`
- 密钥：运行时从 `~/.config/magpie/providers.json` 的 `Qwen (China).key` 读，经管理 API
  以 Fernet 密文落库；**没有任何一步把明文写进仓库文件**

---

## 1. 起一套可复现的实例

用独立的库，避免污染开发库（`real-provider.db` 在 `.gitignore` 的 `*.db` 里）：

```bash
cd D:/code/aigate
export AIGATE_DATABASE_URL="sqlite+aiosqlite:///./real-provider.db"
export AIGATE_SECRET_KEY="<任意长字符串，供应商密钥的加密主密钥>"
export AIGATE_ADMIN_TOKEN="dev-admin-token"
export AIGATE_SEMANTIC_CACHE_ENABLED=false      # 语义缓存会让重复提问看起来像真调
./.venv/Scripts/python.exe scripts/seed.py     # 打印一次 demo key（只存 sha256）
./.venv/Scripts/python.exe -m uvicorn aigate.main:app --host 127.0.0.1 --port 8099
```

`/health` 返回 `200`，`pool.state=warm`：

```json
{"status":"ok","app":"AIGate","version":"0.3.0","env":"dev",
 "pool":{"state":"warm","warm":5,"target":5,"ms":38.1,"error":null},
 "db_capacity":{"verdict":"ok","fits":true,"dialect":"sqlite","enforced":false}}
```

## 2. provider 与路由：全部走管理 API

密钥只在 shell 变量里过一次，不进任何文件：

```bash
DS_KEY=$(python -c "import json,os;p=json.load(open(os.path.expanduser('~/.config/magpie/providers.json'),encoding='utf-8'));print(next(x for x in p['providers'] if x['name']=='Qwen (China)')['key'])")
H='-H X-Admin-Token:dev-admin-token -H content-type:application/json'

# ① 编辑内置 dashscope 这一行的连接信息
curl -s -X PUT localhost:8099/admin/v1/providers/dashscope $H \
  -d '{"base_url":"https://dashscope.aliyuncs.com/compatible-mode/v1"}'

# ② 录密钥（密文落库，读接口只回末四位）
curl -s -X PUT localhost:8099/admin/v1/providers/dashscope/secret $H -d "{\"api_key\":\"$DS_KEY\"}"

# ③ 真打一次连通性测试
curl -s -X POST localhost:8099/admin/v1/providers/dashscope/test $H

# ④ 登记模型与单价（control plane 里 cost_first / 预算都要先知道单价）
curl -s -X POST localhost:8099/admin/v1/models $H \
  -d '{"provider":"dashscope","model":"qwen3.8-flash","input_price_per_1k":0.0003,"output_price_per_1k":0.0006,"context_window":131072}'

# ⑤ 虚拟模型 -> 候选
curl -s -X POST 'localhost:8099/admin/v1/routes?match_model=chat-real&name=real%20dashscope&candidates=dashscope/qwen3.8-flash' $H
```

输出（密钥只出现末四位）：

```text
PUT /admin/v1/providers/dashscope -> 200
  {"item":{"name":"dashscope","label":"通义千问（DashScope，实测）","base_url":"https://dashscope.aliyuncs.com/compatible-mode/v1","configured":true,"key_source":"db"}}   # 录密钥之后
PUT /admin/v1/providers/dashscope/secret -> 200
  {"name":"dashscope","key_source":"db","key_hint":"…q3Ra","configured":true,
   "note":"密钥已加密存储，只会以末四位回显"}
POST /admin/v1/providers/dashscope/test -> 200
  {"ok":true,"status":200,"latency_ms":443,"detail":"可用"}
POST /admin/v1/models -> 201
  {"provider":"dashscope","model":"qwen3.8-flash","input_price_per_1k":0.0003,"output_price_per_1k":0.0006}
POST /admin/v1/routes -> 201
  {"item":{"match_model":"chat-real","candidates":[{"provider":"dashscope","model":"qwen3.8-flash"}],"enabled":true},"warnings":[]}
```

> **发现（未修，属控制面契约问题）**：`POST /admin/v1/providers` 可以登记一个**新** provider 名，
> 但随后 `POST /admin/v1/routes` 会 400 `gateway.unknown_provider`——`admin_ops._validate_candidates()`
> 校验用的是静态 `providers.known_providers()`（`src/aigate/api/admin_ops.py:215`），不认库里的
> `provider_configs` 行。运行时 `_provider_for()` 其实是能路由到自定义名的，被卡住的只有保存这一步。
> 所以本次的真实供应商配置在**内置 `dashscope` 行**上完成。修复见第 10 节。

## 3. `enable_thinking`：新增的供应商专有透传字段

`qwen3.8-flash` 默认会生成思考链（completion 里含 reasoning tokens）。DashScope 接受请求体顶层
`enable_thinking: false` 关掉它。本次把它做成一等透传字段：

| 位置 | 改动 |
|---|---|
| `src/aigate/schemas.py:38` | `SUPPORTED_PARAMS` 登记 `"enable_thinking"` |
| `src/aigate/schemas.py:72` | `ChatCompletionRequest.enable_thinking: bool \| None = None`（无默认值 = 不替调用方决定） |
| `src/aigate/providers/base.py:98,122` | `chat()` / `stream()` 签名带上它（签名就是透传契约） |
| `src/aigate/providers/openai_compat.py:102-108` | `_build_payload` 里 `payload["enable_thinking"] = ...`，`None` 时不写这个键 |
| `src/aigate/api/v1_chat.py:330,638,884` | 非流式 / 流式 / shadow 三条路径都传 |
| `src/aigate/shadow.py:55,88` | shadow 调用也镜像该参数（比对两个模型时采样条件必须一致） |

**语义是「原样转发，不归一化」**：

- 不翻译成 OpenAI 的 `reasoning_effort`——那是替厂商编语义；
- 不静默丢弃——这正是 `schemas.py` 模块注释里那个「收下就扔」的老 bug；
- 不认这个字段的上游会回 400，`openai_compat._raise_for_status()` 把它归为
  `upstream_4xx`（`retryable=False`，不重试、不降级，因为它换一家也一样会 400），
  调用方拿到 502 + `{"code":"upstream_4xx"}`。
  回归用例：`tests/test_param_passthrough.py::test_vendor_rejecting_the_vendor_specific_field_is_reported_not_hidden`
  （同一用例先断言「请求确实带着 `enable_thinking` 发出去了」，再断言 400 被如实上报）。

效果（同一 prompt，同一 `max_tokens=64`）：

| 调用 | `enable_thinking` | prompt | completion | total | 上游耗时 |
|---|---|---|---|---|---|
| R1 | `false` | 33 | 34 | 67 | 2434 ms |
| R2 | 不传（厂商默认） | 69 | 94 | 163 | 1886 ms |

completion 从 94 降到 34（−64%），这是**字段真的到了厂商**的直接证据——同一个 prompt、同一个
网关、同一个模型，唯一变量就是它。注意 R2 的 `prompt_tokens` 也更高（69 vs 33）：厂商在思考模式下
把额外输入算进了 prompt。这一点只作为观测记录，没有深究原因。

## 4. 五次真实调用

调用方是 `POST /v1/chat/completions`（Bearer = seed 打印的 demo key），路由 `chat-real`
→ `dashscope/qwen3.8-flash`。

### R1 — 非流式，`enable_thinking=false`

```text
HTTP 200 wall_ms=2460 x-request-id: req_22e11e71632e4263a44b
finish_reason: stop | model: qwen3.8-flash
usage: {"prompt_tokens": 33, "completion_tokens": 34, "total_tokens": 67}
content: "网关熔断器通过监控服务调用状态，在检测到下游服务故障或响应超时达到阈值时自动切断请求，从而防止故障扩散并保护系统整体稳定性。"
x_aigate: {"request_id":"req_22e11e71632e4263a44b","provider":"dashscope","model_used":"qwen3.8-flash",
           "degraded":false,"usage_source":"upstream","cost_cny":3e-05,"priced":true}
timings: {"gateway_total":2438,"upstream_ms":2434,"provider_cfg_ms":94,"price_ms":1,"persist_ms":1}
```

### R2 — 非流式，不传 `enable_thinking`

```text
HTTP 200 wall_ms=1903 x-request-id: req_86f75d65ef214d868bcc
finish_reason: stop | model: qwen3.8-flash
usage: {"prompt_tokens": 69, "completion_tokens": 94, "total_tokens": 163}
x_aigate: {"provider":"dashscope","degraded":false,"usage_source":"upstream","cost_cny":7.7e-05,"priced":true}
```

### R3 — 非流式，`max_tokens=8`（截断语义）

```text
HTTP 200 wall_ms=1037 x-request-id: req_74240ca02f734d03af4b
finish_reason: length | model: qwen3.8-flash
usage: {"prompt_tokens": 35, "completion_tokens": 8, "total_tokens": 43}
content: "1\n2\n3\n4\n"
x_aigate: {"provider":"dashscope","degraded":false,"usage_source":"upstream","cost_cny":1.5e-05}
```

`finish_reason` 是厂商的 `length` 而不是网关自己编的 `stop`——这正是 `8679ebf` 要的「响应侧说真话」，
在真实厂商上成立。

### R4 — 流式（见第 5 节）

```text
HTTP 200, ttft_ms=2004, finish_reason=stop
usage chunk: {"prompt_tokens": 34, "completion_tokens": 82, "total_tokens": 116}
request_id: req_3d54070aeb4e463dbc38
```

### R5 — 官方 `openai` SDK（见第 9 节）

```text
finish_reason: stop | model: qwen3.8-flash
usage: {"prompt_tokens": 37, "completion_tokens": 41, "total_tokens": 78}
request_id: req_a64a5ccb2bfd4354a49a
```

顺带一条反向证据：把路由指向本地回显上游（第 6 节）时，请求并没有真的到 DashScope，
所以那条 `usage_records` 行记的是回显服务编的 11/2——**网关不区分上游是谁，它只记上游说的话**。

## 5. 流式：真实 SSE 帧

```bash
curl -sN -X POST localhost:8099/v1/chat/completions -H "Authorization: Bearer $KEY" \
  -H 'content-type: application/json' \
  -d '{"model":"chat-real","stream":true,"stream_options":{"include_usage":true},
       "enable_thinking":false,"max_tokens":96,"temperature":0.3,
       "messages":[{"role":"user","content":"用三句话解释什么是幂等性。"}]}'
```

31 帧，首帧 2086 ms（推理模型首 token 慢，`ttft_ms` 报的是 2004）：

```text
2086ms data: {"id": "chatcmpl-req_3d54070aeb4e463dbc38", "object": "chat.completion.chunk", "model": "qwen3.8-flash",
              "choices": [{"index": 0, "delta": {"content": "幂"}, "finish_reason": null}]}

2086ms data: {... "delta": {"content": "等性"}, "finish_reason": null}

3563ms data: {"id": "chatcmpl-req_3d54070aeb4e463dbc38", "object": "chat.completion.chunk", "model": "qwen3.8-flash",
              "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}

3563ms data: {"id": "chatcmpl-req_3d54070aeb4e463dbc38", "object": "chat.completion.chunk", "model": "qwen3.8-flash",
              "choices": [], "usage": {"prompt_tokens": 34, "completion_tokens": 82, "total_tokens": 116}}

3563ms : aigate {"request_id": "req_3d54070aeb4e463dbc38", "provider": "dashscope", "model_used": "qwen3.8-flash",
                 "degraded": false, "ttft_ms": 2004, "finish_reason": "stop", "usage_source": "upstream",
                 "cost_cny": 5.9e-05, "priced": true, "pii_masked": 0}

3564ms data: [DONE]
```

顺序是 OpenAI 的顺序：delta… → finish chunk → usage-only chunk → `: aigate`（SSE 注释，客户端会忽略）
→ `[DONE]`。usage chunk 里的数字来自**上游的 usage chunk**（`stream_options.include_usage` 由
provider 层自己注入，不需要调用方要求；调用方要求的只是「把这份 usage 也给我」）。

## 6. `top_p` / `stop` / `enable_thinking` 确实到了上游：回显原始 body

真实厂商不会把收到的请求体回显给你，所以这里用一个**只记录请求体**的本地上游
（`ThreadingHTTPServer`，非网关代码）：把 `dashscope` 行的 base_url 指到它，发一次请求，
读它记下的 JSON。这条路径除了目标地址之外和真调完全一样——同样的路由、同样的 provider 适配器、
同样的 `_build_payload`。

```bash
curl -s -X PUT localhost:8099/admin/v1/providers/dashscope -H "X-Admin-Token: dev-admin-token" \
  -H 'content-type: application/json' -d '{"base_url":"http://127.0.0.1:8097/v1"}'
# 然后把同样的请求发一次；回显服务把 body 写进 echo_bodies.jsonl
```

回显服务记到的 body（非流式）：

```json
{
  "model": "qwen3.8-flash",
  "messages": [{"role": "user", "content": "echo my parameters"}],
  "stream": false,
  "temperature": 0.3,
  "max_tokens": 42,
  "top_p": 0.01,
  "stop": ["\n\n@@NEVER@@", "###END###"],
  "user": "verify-echo-1",
  "enable_thinking": false
}
```

流式路径（同一个回显服务，`stream:true`）：

```json
{"model": "qwen3.8-flash", "messages": [...], "stream": true,
 "stream_options": {"include_usage": true}, "temperature": 1.0, "max_tokens": 24,
 "top_p": 0.5, "stop": ["\n"], "enable_thinking": false}
```

- `top_p` 是 `0.01`（强效果值），`stop` 是两个任意字符串（上游根本不认这两个终止符，
  所以「模型恰好停在这里」解释不了它出现在 body 里这件事）；
- `stream_options.include_usage` 只出现在流式 body 里，说明两条路径确实各自构造、各自完整；
- `enable_thinking` 两条路径都在；
- `authorization_present: true`（回显服务只记「有没有」，不记内容）。

随后把 base_url 改回真实地址继续真调。

## 7. 故障切除：**这次是我手工造的死地址**

不是自然发生的上游故障。做法是把 `dashscope` 行的 base_url 改成 `http://127.0.0.1:9/v1`
（discard 端口，没人监听），路由、候选、超时全不动：

```bash
curl -s -X PUT localhost:8099/admin/v1/providers/dashscope ... -d '{"base_url":"http://127.0.0.1:9/v1"}'
```

### ① 没有 fallback 的路由（`chat-dead`）

```text
POST model=chat-dead -> HTTP 502 (2177 ms)
{"error": {"code": "upstream_connection_error", "message": "All connection attempts failed",
           "request_id": "req_bb550f5a85224056ac77"}}
trace.route_path: [{"attempt":1,"provider":"dashscope","model":"qwen3.8-flash","ok":false,
                    "error":"upstream_connection_error","latency_ms":2113}]
usage_records: provider=dashscope, model_used="", status="error", input/output=0/0
```

⚠️ **是 502，不是 503**：`v1_chat._complete()` 在「候选全挂」时统一 `502 BAD_GATEWAY`
（`src/aigate/api/v1_chat.py:679`）。503 在本项目里另有含义——连接池耗尽
（`gateway.pool_timeout` + `Retry-After`）。两者别混。

### ② 配了 fallback 的路由（`chat-dead-fallback` → `mock-alt/mock-ok`）

```text
POST model=chat-dead-fallback -> HTTP 200 (2101 ms)
x_aigate: provider="mock-alt", model_used="mock-ok", degraded=true
trace.route_path: [{"attempt":1,...,"error":"upstream_connection_error","latency_ms":2043},
                   {"attempt":2,"provider":"mock-alt","model":"mock-ok","ok":true,"latency_ms":27}]
usage_records: status="fallback", degraded=true, 5/5 tokens（mock 自己报的）
```

### ③ 熔断器（threshold=3）

第三次死连接之后，主候选被跳过，请求 30~37 ms 就返回：

```text
trace.route_path: [{"attempt":1,"provider":"dashscope","skipped":"circuit_open","ok":false},
                   {"attempt":2,"provider":"mock-alt","model":"mock-ok","ok":true,"latency_ms":11}]
```

`GET /admin/v1/ops-health`（即 `admin_health.py` 的 `/admin/v1/ops-health`）：

```json
{"breaker": {"backend":"memory","threshold":3,"cooldown_s":30.0,
             "state":{"dashscope":{"open":true,"consecutive_failures":3,"opened_times":1},
                      "mock-alt":{"open":false,"consecutive_failures":0,"opened_times":0}},
             "error":null},
 "counters": {"cache_hits":0,"cache_served":0,"fallbacks":4.0,"upstream_errors":3.0,"rate_limited":0},
 "providers":[{"name":"dashscope","enabled":true,"configured":true,"key_source":"db",
               "circuit":{"open":true,"consecutive_failures":3,"opened_times":1}}, ...]}
```

### ④ 「告警」的实际表现：没有

```json
{"alerts": {"open": 0, "items": []}}
```

本项目里 `alerts` 表只由**预算**触发（`core/quota.py: raise_alert_if_needed`）。上游故障不会
产生告警行，也没有 WARNING 日志——网关日志里只有启动那一行 `circuit breaker backend: memory`。
能观测到的只有：`ops-health` 的 `breaker.state` / `counters.upstream_errors` / `counters.fallbacks`、
响应体的 `x_aigate.degraded`、`usage_records.status`。
**如果你要的是「供应商挂了要有人被叫醒」，这一层目前不存在**（见第 10 节建议）。

恢复：把 base_url 改回真实地址即可，`provider_config.invalidate()` 让下一次请求立刻用新配置；
熔断器是进程内存态，冷却 30 s 或重启进程后自动闭合。

## 8. 对账：网关记账 vs 供应商 usage

`usage_records` 按 `request_id` 取，供应商数字来自同一次响应（非流式取响应体 `usage`，
流式取 SSE 的 usage chunk，SDK 那条取 SDK 打印的 `usage`）：

| 调用 | 供应商 prompt/completion/total | 网关 input/output/total | 偏差 total | 记账 status |
|---|---|---|---|---|
| R1 非流式 `enable_thinking=false` | 33 / 34 / **67** | 33 / 34 / **67** | **0** | ok |
| R2 非流式 默认思考 | 69 / 94 / **163** | 69 / 94 / **163** | **0** | ok |
| R3 非流式 `max_tokens=8` | 35 / 8 / **43** | 35 / 8 / **43** | **0** | ok |
| R4 流式 | 34 / 82 / **116** | 16 / 141 / **157** | **+41（+35%）** | ok |
| R5 openai SDK | 37 / 41 / **78** | 37 / 41 / **78** | **0** | ok |

### 偏差的原因和修复

四次非流式逐 token 相等（网关只透传，不重算）。流式那次偏了 41 个 token，原因在
`v1_chat._persist_stream()`：它在响应结束后**重新估算**了一遍 token，而不是用 `_event_stream()`
刚刚从上游 usage chunk 里拿到的数字（那份数字同一条响应已经发给客户端了）。也就是说同一个
答案有两份互相矛盾的 token 数。

修复（`src/aigate/api/v1_chat.py:1021-1065`）：`_persist_stream()` 接受调用方已解析好的
`input_tokens/output_tokens`，只有「中断的流」才回退到估算（那种情况下确实没有上游数字，
估算并标注 `usage_source` 是诚实的答案）。回归用例
`tests/test_stream.py::test_stream_usage_row_matches_the_usage_chunk_the_client_saw`
用 DashScope 的真实数字（34/82）构造 SSE，断言记账行必须等于 34/82。

修复后的验证（**没有为此再花一次真调**，用本地回显上游走同一条流式代码路径）：

```text
回显上游报的 usage: 11 prompt / 2 completion
修复前 usage_records: (9, 1)        ← 估算
修复后 usage_records: (11, 2)       ← 上游报的
```

所以第 8 节表格里 R4 的 +41 是**修复前**那一跑的实测偏差，保留原样；修复后的行为由上表末尾
这条 + 单测覆盖。要拿到「修复后 DashScope 流式也逐 token 相等」的实测，需要再花一次流式真调。

## 9. 官方 `openai` SDK（之前标 HOLLOW 的那条）

不写进 `pyproject.toml`，用 `uv` 的临时环境：

```bash
cd <scratch>
uv run --with openai --with httpx python sdk_check2.py real
```

（`--with httpx` 是因为 openai 2.x 不自带 httpx；`OpenAI(http_client=httpx.Client(trust_env=False))`
避免本机代理把 `127.0.0.1` 也代理出去。）

```text
Installed 17 packages in 1.24s
sdk_type: ChatCompletion
id: chatcmpl-req_a64a5ccb2bfd4354a49a
model: qwen3.8-flash
finish_reason: stop
content: "幂等性键（Idempotency Key）用于确保同一请求即使被多次发送，服务器也只会执行一次操作，从而防止因网络重试或用户重复点击导致的数据重复处理。"
usage: {"completion_tokens": 41, "prompt_tokens": 37, "total_tokens": 78,
        "completion_tokens_details": null, "prompt_tokens_details": null}
x_aigate via model_extra: {"request_id": "req_a64a5ccb2bfd4354a49a", "team": "acme", "project": "support-bot",
  "provider": "dashscope", "model_requested": "chat-real", "model_used": "qwen3.8-flash",
  "degraded": false, "cache_hit": null, "pii_masked": 0, "usage_source": "upstream",
  "cost_cny": 3.6e-05, "priced": true, "latency_ms": {...}}
keys seen by sdk: ['x_aigate']
```

SDK 能做三件事：解析成 `ChatCompletion`、`usage` 是标准 `CompletionUsage`、`x_aigate` 落在
`model_extra` 里（不会丢，也不破坏模型校验）。对上游是**真厂商**，所以这条不再 HOLLOW。

SDK 的**流式**解析也验证了，但跑在本地回显上游上（零成本，帧由网关生成、帧形状一致）：

```text
sdk_stream_chunks: 4
sdk_stream_text: "echo!"
sdk_stream_finish: stop
sdk_stream_usage: {"completion_tokens": 2, "prompt_tokens": 11, "total_tokens": 13, ...}
```

## 10. 复现脚本与产物

验证用的脚本放在仓库外（`D:/Software/Hermes/cache/scratch/`），因为它们含本地路径、不该进仓库：

| 文件 | 作用 |
|---|---|
| `gwenv.sh` | 本文第 1 节的环境变量（不含密钥） |
| `configure2.py` | 管理 API 配置（setup / echo-on / dead-on / real-on 四个动作） |
| `run_calls.py` | R1–R3，逐次打印状态/usage/x_aigate，并写 `evidence.jsonl` |
| `run_stream.py` | R4，打印关键 SSE 帧，写 `stream_frames.txt` |
| `sdk_check2.py` | R5（`real`）/ SDK 流式解析（`stream`） |
| `echo_upstream.py` | 只记录请求体的本地上游（第 6 节） |
| `reconcile.py` | 第 8 节对账表 |
| `failover.py` | 第 7 节故障切除 |

密钥没进任何文件：仓库里不含 `sk-` 明文；`provider_configs.api_key_cipher` 是 Fernet 密文，
主密钥在进程环境（`AIGATE_SECRET_KEY`）里。

## 11. 风险与不确定

1. **`enable_thinking` 是供应商专有字段**。转发它意味着网关的请求体不再是「OpenAI 子集」。
   换到不认识它的上游（DeepSeek、vLLM、OpenAI）会 400 → `upstream_4xx`，调用方需要自己知道
   这个参数只对 DashScope 有效。目前没有「按 provider 声明可接受字段」的机制，也没有在
   `/v1/models` 里暴露这个能力——这是设计上留的口子。
2. **R2 的 prompt_tokens（69）比 R1（33）高**，同一个 prompt。推测是思考模式的额外输入被计入，
   没有查证；对账不受影响（两边记的是同一个数），但成本估算会随 `enable_thinking` 变化，
   这点在成本页上无法区分。
3. **流式记账修复只在本地上游 + 单测上验证**，没有再花一次真实流式调用。DashScope 那个
   usage chunk 的形状（`choices: []` + `usage`）与单测里构造的完全一致，但「真厂商跑出来也逐
   token 相等」这句话严格说还没实测。
4. **上游故障不产生告警**（第 7 节 ④）。只有计数器、熔断状态和响应字段。夜间没有人在看
   `ops-health` 的话，供应商挂了不会有人被叫醒。
5. **管理面不能给新 provider 建路由**（第 2 节）。控制台点「新增供应商」之后仍然无法把它放进
   路由，除非改代码里的静态 `known_providers()`。这是契约不一致，不是配置错误。
6. **推理模型的 `reasoning_content` 没有透传**。DashScope 的流式 delta 里有
   `reasoning_content` 字段，`openai_compat.stream()` 只转发 `delta.content`（`openai_compat.py:224`），
   所以思考链在网关这里被丢弃。非流式的 `message.reasoning_content` 同理（只取 `content`）。
   对计费无影响（usage 里已包含），但对「想看到推理过程」的调用方是缺的。
7. **`trust_env`**：`OpenAICompatibleProvider` 对 loopback 地址强制 `trust_env=False`，对远端
   仍是 `trust_env=True`（尊重 `HTTPS_PROXY`）。本次环境没有代理变量，所以两种设置行为一致；
   在挂代理的机器上，真实厂商走代理、本地回显不走代理，这个区别是有意的（见
   `openai_compat.is_loopback_url` 的注释）。
8. **成本数字**：单价是本次手工登记的（0.0003 / 0.0006 每 1k，与 seed 里 qwen-flash 同档），
   不是从 DashScope 账单核对的。`cost_cny` 只能当量级看。

## 12. 建议（下一步）

1. `_validate_candidates()` 改成「静态 known_providers ∪ 库里 enabled 的 provider_configs」，
   并在 `_provider_for()` 里对未配置 key 的自定义 provider 给明确错误码——否则「新增供应商」
   这个功能在控制台上是个死胡同。
2. 上游连续失败时落一条 `Alert`（scope=provider），并把 `counters.upstream_errors` 接进
   `/admin/v1/ops-health` 的告警摘要——现在这一层只有数字，没有「叫醒」。
3. 如果 `enable_thinking` 这类专有字段还会增多，在 provider 配置行里加一列
   `extra_request_fields`（JSON），`_build_payload` 只在白名单 provider 上合并它，
   避免每加一个厂商字段就改一次公共 schema。